import os
import subprocess
import tempfile
import threading
import uuid
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import DefaultDict, List, Optional, Tuple

import yaml
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage

from opsmith.agent import AgentDeps
from opsmith.core.context import OpsmithContext
from opsmith.core.events import STEP_BUILD, STEP_DETECT
from opsmith.core.results import DockerfileCheck
from opsmith.prompts import (
    DOCKERFILE_GENERATION_PROMPT_TEMPLATE,
    DOCKERFILE_VALIDATION_PROMPT_TEMPLATE,
    REPO_ANALYSIS_PROMPT_TEMPLATE,
)
from opsmith.repo_map import RepoMap
from opsmith.settings import settings
from opsmith.types import BUILDABLE_SERVICE_TYPES, ServiceInfo, ServiceList

#: How long a Dockerfile is given to build. A ceiling rather than a setting: shortening it turns a
#: slow but correct build into a failure, which is the worst thing a validator can do.
DOCKER_BUILD_TIMEOUT_S = 30 * 60

#: How long the container is watched before it counts as healthy. ``dockerfile validate`` lets a
#: caller lengthen this, because how long a service takes to boot is its own business.
DOCKER_RUN_TIMEOUT_S = 60

#: Trailing lines of build and run output that travel in a result. The same count as
#: ``OUTPUT_TAIL_LINES`` in ``infra_provisioners/base_provisioner.py``, for the same reason: enough
#: to show what went wrong without putting a whole build log into a JSON envelope.
LOG_TAIL_LINES = 50


def _tail(output: str, lines: int = LOG_TAIL_LINES) -> str:
    """
    :param output: Everything a command wrote.
    :param lines: How many trailing lines to keep.
    :return: The last lines of it, which is what a result carries.
    """
    if not output:
        return ""
    return "\n".join(output.splitlines()[-lines:])


class DockerfileContent(BaseModel):
    """Describes the dockerfile response from the agent, including the generated Dockerfile content and reasoning for the selection."""

    content: str = Field(
        ...,
        description="The final generated Dockerfile content.",
    )
    reason: Optional[str] = Field(
        None, description="The reasoning for the selection of the final Dockerfile content."
    )
    give_up: bool = Field(
        False,
        description=(
            "Set this to true if you are unable to fix the Dockerfile based on the provided"
            " feedback, either because of an issue in the code or because you cannot determine a"
            " solution."
        ),
    )


class DockerfileValidation(BaseModel):
    """The result of validating the logs from a docker build and run."""

    is_successful: bool = Field(
        ...,
        description="Whether the build and run is considered successful based on container logs.",
    )
    reason: Optional[str] = Field(
        None, description="If not successful, an explanation of what went wrong."
    )


@dataclass
class _DockerRun:
    """What building and running one Dockerfile did, before anything judges it.

    These are docker's own facts. Whether they amount to a usable Dockerfile is a separate
    question, asked of the model, because a container that exits for want of a database it has not
    been given is not a Dockerfile problem.
    """

    build_ok: bool
    run_ok: Optional[bool]
    build_output: str
    run_output: str
    run_timed_out: bool

    @property
    def docker_ok(self) -> bool:
        """
        :return: Whether docker was happy: the image built and the container did not exit non-zero.
        """
        return self.build_ok and self.run_ok is True


class ServiceDetector:
    """Works out what a repository deploys, and writes a Dockerfile per service."""

    def __init__(self, ctx: OpsmithContext):
        """
        :param ctx: The run's context, supplying the source directory, the model, the event sink,
            the way to ask the user something, and whether the run was asked for verbose output.
        """
        self.ctx = ctx
        self.events = ctx.events
        self.interact = ctx.interact
        self.deployments_path = ctx.deployments_path
        self.repo_map = RepoMap(ctx=ctx)
        self.agent_deps = AgentDeps(
            src_dir=Path(ctx.src_dir), tracked_files=self.repo_map.tracked_files
        )
        self.verbose = ctx.verbose

    @property
    def agent(self) -> Agent[AgentDeps, str]:
        """The configured model. Read from the context on use, so constructing a detector does
        not require one - see ``OpsmithContext.require_agent``."""
        return self.ctx.require_agent()

    def detect_services(self, existing_config: Optional[ServiceList] = None) -> ServiceList:
        """
        Scans the repository to determine the services to be deployed, using the AI agent.

        Generates a repository map, then uses an AI agent with a file reading tool
        to identify services and their characteristics.

        Returns:
            A ServiceList object detailing the services to be deployed.
        """
        repo_map_str = self.repo_map.get_repo_map()
        if self.verbose:
            self.events.log(STEP_DETECT, "Repo map generated.")

        if existing_config:
            existing_config_yaml = yaml.dump(existing_config.model_dump(mode="json"), indent=2)
        else:
            existing_config_yaml = "N/A"

        prompt = REPO_ANALYSIS_PROMPT_TEMPLATE.format(
            repo_map_str=repo_map_str, existing_config_yaml=existing_config_yaml
        )

        self.events.log(
            STEP_DETECT, "Calling AI agent to analyse the repo and determine the services..."
        )
        with self.events.waiting(STEP_DETECT, "Waiting for the LLM"):
            run_result = self.agent.run_sync(prompt, output_type=ServiceList, deps=self.agent_deps)

        service_list = run_result.output

        base_slug_counts: DefaultDict[str, int] = defaultdict(int)
        for service in service_list.services:
            base_slug = f"{service.language}_{service.service_type.value}".replace(" ", "_").lower()
            count = base_slug_counts[base_slug] + 1
            base_slug_counts[base_slug] = count
            service.name_slug = f"{base_slug}_{count}"

        return service_list

    def generate_dockerfile(self, service: ServiceInfo) -> Optional[Path]:
        """
        Generates the Dockerfile for one service, when that service needs one.

        :param service: The service to build an image for.
        :return: Where the Dockerfile was written, or None for a service that needs none. The
            caller reports what was written, and only this knows which services those are.
        """
        if service.service_type not in BUILDABLE_SERVICE_TYPES:
            self.events.warning(
                STEP_BUILD,
                f"Dockerfile not needed for service {service.service_type}, skipping.",
            )
            return None

        service_dir_path = self.deployments_path / "docker" / service.name_slug
        service_dir_path.mkdir(parents=True, exist_ok=True)
        dockerfile_path_abs = service_dir_path / "Dockerfile"
        self.events.step(STEP_BUILD, f"Generating Dockerfile for service: {service.name_slug}...")

        template_name = f"{service.language.lower()}_{service.service_type.value.lower()}"
        template_path = Path(__file__).parent / "templates" / "dockerfiles" / template_name
        dockerfile_template = None
        if template_path.exists():
            with open(template_path, "r", encoding="utf-8") as f:
                dockerfile_template = f.read()
            self.events.log(STEP_BUILD, f"Using Dockerfile template: {template_name}")

        dockerfile_content = self._generate_and_validate_dockerfile(
            service, dockerfile_path_abs, dockerfile_template
        )

        with open(dockerfile_path_abs, "w", encoding="utf-8") as f:
            f.write(dockerfile_content)
        self.events.log(STEP_BUILD, f"Dockerfile saved to: {dockerfile_path_abs}")
        return dockerfile_path_abs

    def _generate_and_validate_dockerfile(
        self,
        service: ServiceInfo,
        dockerfile_path_abs: Path,
        dockerfile_template: Optional[str] = None,
    ) -> str:
        """Generates and validates a Dockerfile for a given service."""
        existing_dockerfile_content = "N/A"
        if dockerfile_path_abs.exists():
            with open(dockerfile_path_abs, "r", encoding="utf-8") as f:
                existing_dockerfile_content = f.read()

        service_info_yaml = yaml.dump(service.model_dump(mode="json"), indent=2)
        dockerfile_content = ""
        messages: list[ModelMessage] = []
        completed = False
        attempt = 0

        while attempt < settings.max_dockerfile_gen_attempts:
            attempt += 1
            self.events.step(
                STEP_BUILD, f"Attempt {attempt}/{settings.max_dockerfile_gen_attempts}..."
            )

            repo_map_str = self.repo_map.get_repo_map()
            template_section = ""
            if dockerfile_template:
                template_section = (
                    "A Dockerfile template is provided below. Use it as a guide.\n"
                    "```\n"
                    f"{dockerfile_template}\n"
                    "```\n"
                )

            prompt = DOCKERFILE_GENERATION_PROMPT_TEMPLATE.format(
                service_info_yaml=service_info_yaml,
                template_section=template_section,
                repo_map_str=repo_map_str,
                existing_dockerfile_content=existing_dockerfile_content,
            )
            with self.events.waiting(STEP_BUILD, "Waiting for the LLM to generate the Dockerfile"):
                response = self.agent.run_sync(
                    prompt,
                    deps=self.agent_deps,
                    output_type=DockerfileContent,
                    message_history=messages,
                )
                dockerfile_content = response.output.content
                give_up = response.output.give_up
                reason = response.output.reason

            if give_up:
                self.events.warning(
                    STEP_BUILD, f"LLM indicated it cannot fix the Dockerfile further: {reason}."
                )
                break

            is_successful, reason, validation_messages = self._validate_dockerfile(
                dockerfile_content
            )

            if is_successful:
                self.events.log(STEP_BUILD, f"Dockerfile validation successful: {reason}.")
                completed = True
                break

            self.events.warning(STEP_BUILD, f"Dockerfile validation failed with reason: {reason}.")

            messages = response.new_messages() + validation_messages

        while not completed:
            dockerfile_content = self.interact.edit(
                "dockerfile.edit",
                "Would you like to manually edit the Dockerfile?",
                content=dockerfile_content,  # last generated content
                path=dockerfile_path_abs,
                on_headless="fail",
            )

            completed, reason, _ = self._validate_dockerfile(dockerfile_content)
            self.events.log(
                STEP_BUILD,
                (
                    f"Dockerfile validation {'succeeded' if completed else 'failed'} with reason:"
                    f" {reason}."
                ),
            )

        return dockerfile_content

    def _run_command_with_streaming_output(
        self, command: List[str], timeout: int
    ) -> tuple[int, str, bool]:
        """
        Runs a command and streams its output, returning the exit code, full output, and timeout status.

        :param command: The argument vector to run.
        :param timeout: Seconds to wait before terminating the process.
        :return: The exit code, everything the command wrote, and whether it timed out.
        """
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )
        # Popen types stdout as Optional because it is None without a pipe; this call always
        # asks for one, so the stream is there.
        assert process.stdout is not None
        stdout = process.stdout

        output_lines: List[str] = []
        timed_out = False

        def stream_reader():
            for line in iter(stdout.readline, ""):
                stripped_line = line.strip()
                output_lines.append(stripped_line)
                self.events.output(STEP_BUILD, stripped_line)

        reader_thread = threading.Thread(target=stream_reader)
        reader_thread.daemon = True
        reader_thread.start()

        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            timed_out = True

        reader_thread.join(timeout=5)

        output_str = "\n".join(output_lines)
        return process.returncode, output_str, timed_out

    def validate_dockerfile(
        self,
        service: ServiceInfo,
        *,
        build_timeout_s: int = DOCKER_BUILD_TIMEOUT_S,
        run_timeout_s: int = DOCKER_RUN_TIMEOUT_S,
    ) -> DockerfileCheck:
        """
        Builds and runs one service's Dockerfile and reports what happened, without repairing it.

        This is the smoke check ``setup`` runs after generating a Dockerfile, exposed for
        ``opsmith dockerfile validate`` so a harness that wrote its own Dockerfile can have it
        checked. It never loops, never asks for an edit, and never regenerates anything: the caller
        wrote the file and the caller fixes it.

        :param service: The service whose Dockerfile to check.
        :param build_timeout_s: Seconds to allow the image to build.
        :param run_timeout_s: Seconds to watch the container before counting it as healthy.
        :return: What docker did, and what the model made of it if docker was unhappy.
        """
        dockerfile_path = self.deployments_path / "docker" / service.name_slug / "Dockerfile"
        self.events.step(STEP_BUILD, f"Validating the Dockerfile for {service.name_slug}...")

        docker_run = self._build_and_run(
            dockerfile_path.read_text(encoding="utf-8"),
            build_timeout_s=build_timeout_s,
            run_timeout_s=run_timeout_s,
        )

        ok = docker_run.docker_ok
        explanation: Optional[str] = None
        dockerfile_at_fault: Optional[bool] = None
        if not ok:
            verdict, _ = self._judge_docker_output(docker_run)
            ok = verdict.is_successful
            explanation = verdict.reason or None
            dockerfile_at_fault = not verdict.is_successful

        return DockerfileCheck(
            service=service.name_slug,
            dockerfile=os.path.relpath(dockerfile_path, self.ctx.src_dir),
            ok=ok,
            build_ok=docker_run.build_ok,
            run_ok=docker_run.run_ok,
            run_timed_out=docker_run.run_timed_out,
            dockerfile_at_fault=dockerfile_at_fault,
            explanation=explanation,
            build_tail=_tail(docker_run.build_output),
            run_tail=_tail(docker_run.run_output),
        )

    def _validate_dockerfile(
        self, dockerfile_content: str
    ) -> tuple[bool, Optional[str], list[ModelMessage]]:
        """
        Validates a Dockerfile by building and running it.
        Returns success status, reason for status
        """
        docker_run = self._build_and_run(dockerfile_content)
        if docker_run.docker_ok:
            return True, "", []

        verdict, messages = self._judge_docker_output(docker_run)
        return verdict.is_successful, verdict.reason, messages

    def _judge_docker_output(
        self, docker_run: _DockerRun
    ) -> Tuple[DockerfileValidation, list[ModelMessage]]:
        """
        Asks the model what a failed build or run means, and whether it is the Dockerfile's fault.

        :param docker_run: What docker did, with its output untruncated - the model reads all of it.
        :return: The model's verdict, and the messages it produced, which the repair loop feeds
            back into the next generation attempt.
        """
        validation_prompt = DOCKERFILE_VALIDATION_PROMPT_TEMPLATE.format(
            build_output=docker_run.build_output,
            run_output=docker_run.run_output,
        )
        with self.events.waiting(
            STEP_BUILD, "Waiting for the LLM to validate the Docker build output"
        ):
            validation_response = self.agent.run_sync(
                validation_prompt,
                output_type=DockerfileValidation,
                deps=self.agent_deps,
            )
        return validation_response.output, validation_response.new_messages()

    def _build_and_run(
        self,
        dockerfile_content: str,
        *,
        build_timeout_s: int = DOCKER_BUILD_TIMEOUT_S,
        run_timeout_s: int = DOCKER_RUN_TIMEOUT_S,
    ) -> _DockerRun:
        """
        Builds a Dockerfile and runs what it produced, and reports only what docker said.

        :param dockerfile_content: The Dockerfile to build, which need not be on disk.
        :param build_timeout_s: Seconds to allow the image to build.
        :param run_timeout_s: Seconds to watch the container before terminating it.
        :return: The build and run outcomes, with their output untruncated.
        """
        repo_root = self.agent_deps.src_dir.resolve()
        image_tag = f"opsmith-build-test-{uuid.uuid4()}"
        build_output_str = ""
        run_output_str = ""
        run_ok: Optional[bool] = None
        timed_out = False

        try:
            # Create temporary directory for Dockerfile
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_dockerfile = Path(temp_dir) / "Dockerfile"
                # Write Dockerfile content
                with open(temp_dockerfile, "w", encoding="utf-8") as f:
                    f.write(dockerfile_content)

                # Execute docker build command
                self.events.step(STEP_BUILD, "Attempting to build the Dockerfile...")
                build_command = [
                    "docker",
                    "build",
                    "-f",
                    str(temp_dockerfile),
                    "-t",
                    image_tag,
                    str(repo_root),
                ]
                build_rc, build_output_str, _ = self._run_command_with_streaming_output(
                    build_command, build_timeout_s
                )

            build_ok = build_rc == 0
            if build_ok:
                # Build successful, now try to run the image
                self.events.step(STEP_BUILD, "Build successful. Attempting to run the container...")
                run_command = ["docker", "run", "--rm", image_tag]
                run_rc, run_output_str, timed_out = self._run_command_with_streaming_output(
                    run_command, timeout=run_timeout_s
                )

                if timed_out:
                    self.events.log(
                        STEP_BUILD, f"Container running for {run_timeout_s}s, assuming success."
                    )

                run_ok = run_rc == 0
        finally:
            # Clean up image
            cleanup_image_process = subprocess.run(
                ["docker", "rmi", "-f", image_tag], capture_output=True, text=True
            )
            if (
                cleanup_image_process.returncode != 0
                and "no such image" not in cleanup_image_process.stderr.lower()
            ):
                self.events.warning(
                    STEP_BUILD,
                    (
                        f"Failed to remove Docker image {image_tag}:"
                        f" {cleanup_image_process.stderr.strip()}"
                    ),
                )

        return _DockerRun(
            build_ok=build_ok,
            run_ok=run_ok,
            build_output=build_output_str,
            run_output=run_output_str,
            run_timed_out=timed_out,
        )
