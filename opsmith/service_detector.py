import subprocess
import tempfile
import threading
import uuid
from collections import defaultdict
from pathlib import Path
from typing import List, Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_ai.messages import ModelMessage

from opsmith.agent import AgentDeps
from opsmith.core.context import OpsmithContext
from opsmith.core.events import STEP_BUILD, STEP_DETECT
from opsmith.prompts import (
    DOCKERFILE_GENERATION_PROMPT_TEMPLATE,
    DOCKERFILE_VALIDATION_PROMPT_TEMPLATE,
    REPO_ANALYSIS_PROMPT_TEMPLATE,
)
from opsmith.repo_map import RepoMap
from opsmith.settings import settings
from opsmith.types import ServiceInfo, ServiceList, ServiceTypeEnum


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
        self.agent = ctx.agent
        self.repo_map = RepoMap(ctx=ctx)
        self.agent_deps = AgentDeps(
            src_dir=Path(ctx.src_dir), tracked_files=self.repo_map.tracked_files
        )
        self.verbose = ctx.verbose

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

        base_slug_counts = defaultdict(int)
        for service in service_list.services:
            base_slug = f"{service.language}_{service.service_type.value}".replace(" ", "_").lower()
            count = base_slug_counts[base_slug] + 1
            base_slug_counts[base_slug] = count
            service.name_slug = f"{base_slug}_{count}"

        return service_list

    def generate_dockerfile(self, service: ServiceInfo):
        """Generates Dockerfiles for each service in the deployment configuration."""
        buildable_service_types = [
            ServiceTypeEnum.BACKEND_API,
            ServiceTypeEnum.FULL_STACK,
            ServiceTypeEnum.BACKEND_WORKER,
        ]
        if service.service_type not in buildable_service_types:
            self.events.warning(
                STEP_BUILD,
                f"Dockerfile not needed for service {service.service_type}, skipping.",
            )
            return

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
        messages = []
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
        output_lines = []
        timed_out = False

        def stream_reader():
            for line in iter(process.stdout.readline, ""):
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

    def _validate_dockerfile(
        self, dockerfile_content: str
    ) -> tuple[bool, str, Optional[list[ModelMessage]]]:
        """
        Validates a Dockerfile by building and running it.
        Returns success status, reason for status
        """
        repo_root = self.agent_deps.src_dir.resolve()
        image_tag = f"opsmith-build-test-{uuid.uuid4()}"
        build_output_str = ""
        run_output_str = ""
        is_successful = True

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
                    build_command, 30 * 60
                )

            # Build failed
            if build_rc != 0:
                is_successful = False
            else:
                # Build successful, now try to run the image
                self.events.step(STEP_BUILD, "Build successful. Attempting to run the container...")
                run_command = ["docker", "run", "--rm", image_tag]
                run_rc, run_output_str, timed_out = self._run_command_with_streaming_output(
                    run_command, timeout=60
                )

                if timed_out:
                    self.events.log(STEP_BUILD, "Container running for 60s, assuming success.")

                # Run failed.
                if run_rc != 0:
                    is_successful = False
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

        if not is_successful:
            validation_prompt = DOCKERFILE_VALIDATION_PROMPT_TEMPLATE.format(
                build_output=build_output_str,
                run_output=run_output_str,
            )
            with self.events.waiting(
                STEP_BUILD, "Waiting for the LLM to validate the Docker build output"
            ):
                validation_response = self.agent.run_sync(
                    validation_prompt,
                    output_type=DockerfileValidation,
                    deps=self.agent_deps,
                )
            return (
                validation_response.output.is_successful,
                validation_response.output.reason,
                validation_response.new_messages(),
            )

        return True, "", []
