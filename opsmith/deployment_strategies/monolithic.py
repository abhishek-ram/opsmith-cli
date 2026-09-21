import base64
import binascii
import functools
import json
import shutil
import time
from io import StringIO
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import jinja2
import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, Field
from pydantic_ai.messages import ModelMessage

from opsmith.cloud_providers.base import BaseCloudProvider, MachineType, MachineTypeList
from opsmith.core.errors import AnsibleFailed, LlmGaveUp, UnknownEnvironment
from opsmith.core.events import (
    STEP_COMPOSE,
    STEP_DESTROY,
    STEP_DNS,
    STEP_FRONTEND,
    STEP_REGISTRY,
    STEP_RUN,
    STEP_SETUP,
    STEP_VM,
)
from opsmith.core.questions import Question, Resolution, Variant
from opsmith.core.results import (
    DeployedService,
    DestroyResult,
    DnsRecord,
    EnvCreateResult,
    EnvStatusResult,
    ReleaseResult,
    Resource,
    ResourceKind,
    RunResult,
    UpdateResult,
)
from opsmith.deployment_strategies.base import BaseDeploymentStrategy
from opsmith.prompts import (
    DOCKER_COMPOSE_GENERATION_PROMPT_TEMPLATE,
    DOCKER_COMPOSE_LOG_VALIDATION_PROMPT_TEMPLATE,
    MONOLITHIC_MACHINE_REQUIREMENTS_PROMPT_TEMPLATE,
)
from opsmith.settings import settings
from opsmith.types import (
    DeploymentConfig,
    DeploymentEnvironment,
    DomainInfo,
    FrontendCDNState,
    MonolithicDeploymentState,
    ServiceInfo,
    ServiceTypeEnum,
    VirtualMachineState,
)
from opsmith.utils import dns_record_is_published, slugify


class DockerComposeLogValidation(BaseModel):
    """The result of validating the logs from a docker-compose deployment."""

    is_successful: bool = Field(
        ..., description="Whether the deployment is considered successful based on container logs."
    )
    reason: Optional[str] = Field(
        None, description="If not successful, an explanation of what went wrong."
    )


class DockerComposeContent(BaseModel):
    """Describes the generated docker-compose.yml file content."""

    content: str = Field(..., description="The final generated docker-compose.yml content.")
    env_file_content: str = Field(
        ...,
        description=(
            "The content of the .env file. This includes generated secrets for infrastructure and"
            " composed variables for application services."
        ),
    )
    reason: Optional[str] = Field(
        None, description="The reason for the failure of the last deployment attempt."
    )
    give_up: bool = Field(
        False,
        description=(
            "Set this to true if you are unable to fix the docker-compose.yml file based on the"
            " provided feedback, either because of an issue in the code or because you cannot"
            " determine a solution."
        ),
    )


def _decode_stream(encoded: Optional[str]) -> str:
    """
    Reads back one of the streams the playbook handed over.

    They arrive base64 encoded because the marker the provisioner matches stops at the first
    double quote, and arbitrary program output is full of them. A value that does not decode is
    returned as it arrived rather than thrown away: a tail of output is worth more than nothing,
    and nothing here is load-bearing.

    :param encoded: What the marker carried, or None when the playbook reported no such stream.
    :return: What the command wrote, as text.
    """
    if not encoded:
        return ""

    try:
        return base64.b64decode(encoded).decode("utf-8", errors="replace")
    except (ValueError, binascii.Error):
        return encoded


def _reported_exit_code(outputs: Dict[str, str]) -> int:
    """
    Reads the exit status the playbook reported.

    Zero when there is nothing to read: the playbook fails outright when it cannot run the command,
    so reaching here without a status means the command ran and said nothing to the contrary.

    :param outputs: The values the playbook handed back through its markers.
    :return: What the command exited with.
    """
    try:
        return int(outputs.get("rc", 0))
    except (TypeError, ValueError):
        return 0


def _machine_resource(machine: VirtualMachineState, region: str) -> Resource:
    """
    Describes the machine an environment runs on.

    Building this in one place is what keeps ``deploy``, ``status`` and ``destroy`` describing the
    same machine the same way, rather than each naming its own subset of it.

    :param machine: The machine, as the state file holds it.
    :param region: The region the environment deploys into.
    :return: The machine as a resource.
    """
    return Resource(
        kind=ResourceKind.VIRTUAL_MACHINE,
        id=machine.instance_id,
        region=region,
        address=machine.public_ip,
        size=machine.instance_type,
        details={
            "cpu": str(machine.cpu),
            "ram_gb": str(machine.ram_gb),
            "architecture": machine.architecture.value,
            "user": machine.user,
            **({"private_ip": machine.private_ip} if machine.private_ip else {}),
        },
    )


def _registry_resource(registry_url: str, region: str) -> Resource:
    """
    Describes the container registry the environment's images live in.

    :param registry_url: The registry, which is both what addresses it and where it is reached.
    :param region: The region the registry was created in.
    :return: The registry as a resource.
    """
    return Resource(
        kind=ResourceKind.CONTAINER_REGISTRY,
        id=registry_url,
        region=region,
        address=registry_url,
    )


def _cdn_resource(cdn: FrontendCDNState, region: str) -> Resource:
    """
    Describes the content delivery network serving one frontend service.

    :param cdn: The network, as the state file holds it.
    :param region: The region the environment deploys into.
    :return: The network as a resource.
    """
    return Resource(
        kind=ResourceKind.CONTENT_DELIVERY_NETWORK,
        id=cdn.cdn_distribution_id or cdn.bucket_name,
        name=cdn.service_name_slug,
        region=region,
        address=cdn.cdn_domain_name or cdn.domain_name,
        details={
            "bucket_name": cdn.bucket_name,
            "domain_name": cdn.domain_name,
            **({"cdn_ip_address": cdn.cdn_ip_address} if cdn.cdn_ip_address else {}),
            **({"certificate_id": cdn.certificate_id} if cdn.certificate_id else {}),
        },
    )


def _dns_record_slug(record: Dict[str, str]) -> str:
    """
    Names one DNS record, for the key its wait is recorded under.

    The wait is keyed on the record rather than on the service it belongs to, because one frontend
    service needs two unrelated records - one that validates its certificate and one that points
    at its CDN - and a key they shared would let the first one satisfy the second.

    :param record: The record, with its type, name and value.
    :return: The slug naming it.
    """
    name = (record.get("name") or "record").replace(".", "-")
    return slugify(name).strip("-") or "record"


class MonolithicDeploymentStrategy(BaseDeploymentStrategy):
    """Monolithic deployment strategy."""

    @classmethod
    def name(cls) -> str:
        return "Monolithic"

    @classmethod
    def description(cls) -> str:
        return (
            "Deploys the entire application as a single unit. Best used for experiments and hobby"
            " applications."
        )

    @classmethod
    def questions(cls) -> List[Question]:
        """
        Declares what this strategy asks on top of what making an environment asks anyway.

        The three are the machine to run on, the runtime value of every environment variable the
        services declare, and the build-time value of every one a frontend declares. None of them
        is asked from here - each is asked inline at the point in the deploy that needs it - so
        these declarations exist to be reported by ``opsmith env plan`` and are checked against
        the call sites by the test suite.

        :return: The instance type, the runtime variables and the frontend build variables.
        """
        return [
            Question(
                key="env.instance_type",
                message="Select an instance type for the new environment",
                primitive="select",
                asked_by=cls.name(),
            ),
            Question(
                key="envvar.<KEY>",
                message="Enter value for a runtime environment variable",
                asked_by=cls.name(),
                for_each=cls.runtime_variables,
            ),
            Question(
                key="build_env.<slug>.<KEY>",
                message="Enter a frontend's build-time environment variable",
                asked_by=cls.name(),
                for_each=cls.build_variables,
            ),
        ]

    @staticmethod
    def runtime_variables(resolution: Resolution) -> List[Variant]:
        """
        Expands the runtime environment variables the services between them declare.

        What a deploy actually asks for is the intersection of these and the compose file the
        model writes, which cannot be known before the run. These are the ones the configuration
        declares, which is the honest answer to "what will it want", and is a superset.

        :param resolution: What is known, for the configuration to read the variables from.
        :return: One variant per configured variable, cheapest first to read: by name.
        """
        if resolution.deployment_config is None:
            return []

        configured = resolution.deployment_config.get_env_var_configs()
        return [
            Variant(
                token=key,
                message=f"Enter value for {key}",
                default=config.default_value,
                secret=config.is_secret,
            )
            for key, config in sorted(configured.items())
        ]

    @staticmethod
    def build_variables(resolution: Resolution) -> List[Variant]:
        """
        Expands the build-time environment variables each frontend declares.

        :param resolution: What is known, for the configuration to read the services from.
        :return: One variant per variable per frontend service.
        """
        if resolution.deployment_config is None:
            return []

        variants = []
        for service in resolution.deployment_config.services:
            if service.service_type != ServiceTypeEnum.FRONTEND:
                continue
            for env_var in service.env_vars:
                # Worded without the indentation the deploy prompts with: that indent belongs to
                # a terminal running down a list of one service's variables, and a plan is a
                # different list entirely.
                if env_var.is_secret:
                    subject = f"secret '{env_var.key}'"
                else:
                    subject = f"'{env_var.key}'"
                variants.append(
                    Variant(
                        token=f"{service.name_slug}.{env_var.key}",
                        message=(
                            f"Enter the build-time value for {subject} of '{service.name_slug}'"
                        ),
                        default=env_var.default_value,
                        secret=env_var.is_secret,
                    )
                )
        return variants

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.docker_compose_snippets_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(self.templates_dir / "docker_compose_snippets"),
            autoescape=False,
        )

        # Every record this run asked somebody to create, from wherever it asked. A frontend
        # service needs two that terraform only reports mid-run, so collecting them at the one
        # place that waits on them is the only way the result carries all of them.
        self.requested_dns_records: List[DnsRecord] = []

    def _collect_urls(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
        env_state: MonolithicDeploymentState,
    ) -> Dict[str, str]:
        """
        Works out where each routed service is reachable.

        A frontend is reached at the domain its content delivery network was created for, which
        the state file holds; a backend at the domain the environment configures for it.

        :param deployment_config: What the repository deploys.
        :param environment: The environment the services are deployed in.
        :param env_state: The environment's state, holding the frontend deployments.
        :return: The url of each routed service, by slug.
        """
        urls = {
            cdn.service_name_slug: f"https://{cdn.domain_name}" for cdn in env_state.frontend_cdn
        }
        backend_services = self._get_backend_services(deployment_config)
        for domain in environment.get_domains_for_services(backend_services):
            urls[domain.service_name_slug] = f"https://{domain.domain_name}"
        return urls

    def _collect_resources(
        self,
        environment: DeploymentEnvironment,
        env_state: MonolithicDeploymentState,
    ) -> List[Resource]:
        """
        Lists everything this environment holds, in the order it was created.

        A monolithic environment holds at most one machine, so this list is short; it is a list
        anyway because that is what the result promises, and a strategy that raises several
        machines has somewhere to report them all.

        :param environment: The environment the resources belong to.
        :param env_state: The environment's state, which is the record of what exists.
        :return: Every resource the environment holds.
        """
        region = environment.cloud_provider_detail.region
        resources = [_cdn_resource(cdn, region) for cdn in env_state.frontend_cdn]
        if env_state.registry_url:
            resources.append(_registry_resource(env_state.registry_url, region))
        if env_state.virtual_machine:
            resources.append(_machine_resource(env_state.virtual_machine, region))
        return resources

    def _collect_services(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
        env_state: MonolithicDeploymentState,
    ) -> List[DeployedService]:
        """
        Lists the services the last deploy or update put on the environment.

        The image each one runs is not reported: the state file records which services were
        deployed, not which image each was deployed from, so claiming one here would be inventing
        it. ``opsmith release`` reports the images it built, which is where that fact is true.

        :param deployment_config: What the repository deploys.
        :param environment: The environment the services run in.
        :param env_state: The environment's state, holding the deployed-service snapshot.
        :return: Each deployed service, with its url when it has one.
        """
        urls = self._collect_urls(deployment_config, environment, env_state)
        return [
            DeployedService(name_slug=str(service["name_slug"]), url=urls.get(service["name_slug"]))
            for service in env_state.deployed_services or []
            if service.get("name_slug")
        ]

    def _confirm_env_vars(
        self,
        deployment_config: DeploymentConfig,
        env_file_content: str,
    ) -> str:
        """
        Parses environment variables from LLM response, confirms with user, and returns updated content.
        """
        # Parse env_file_content from LLM
        env_file_vars = dotenv_values(stream=StringIO(env_file_content))

        configured_env = deployment_config.get_env_var_configs()

        self.events.step(
            STEP_COMPOSE, "Please confirm or provide values for environment variables:"
        )

        answers = {}
        for key, value in sorted(env_file_vars.items()):
            # Exclude any custom env that is not configured
            if key not in configured_env:
                continue

            config = configured_env[key]

            # Precedence: what this environment is already running with > llm > code default.
            # The model regenerates this file on every attempt, so without the first of those a
            # retry would offer a freshly invented password for a database that already has one.
            remembered = self.answers.get(f"envvar.{key}")
            default_value = remembered or value or config.default_value

            answers[key] = self.interact.ask(
                f"envvar.{key}",
                f"Enter value for {key}",
                default=default_value,
                secret=config.is_secret,
            )

        # For the .env file, merge with precedence: user answers > llm
        final_env_vars_for_file = {**env_file_vars, **answers}

        # Reconstruct env file content
        env_lines = [f'{key}="{value}"' for key, value in final_env_vars_for_file.items()]
        return "\n".join(env_lines)

    def _get_deploy_docker_compose_path(
        self, environment: DeploymentEnvironment
    ) -> Tuple[Path, Path]:
        deploy_compose_path = (
            self.deployments_path / "environments" / environment.name / "docker_compose_deploy"
        )
        deploy_compose_path.mkdir(parents=True, exist_ok=True)
        docker_compose_path = deploy_compose_path / "docker-compose.yml"

        return deploy_compose_path, docker_compose_path

    def _deploy_docker_compose(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
        environment_state: MonolithicDeploymentState,
        env_file_content: str,
    ) -> str:
        """
        Deploys the docker-compose stack and returns container logs for validation.
        """
        self.events.step(STEP_COMPOSE, "Deploying docker-compose stack to the VM")
        virtual_machine = environment_state.require_virtual_machine()
        ansible_user = virtual_machine.user
        deploy_compose_path, docker_compose_path = self._get_deploy_docker_compose_path(environment)

        ansible_runner = self.provisioners.ansible(deploy_compose_path, step=STEP_COMPOSE)
        ansible_runner.copy_template(
            "docker_compose_deploy", environment.cloud_provider_instance.name()
        )

        traefik_template = self.docker_compose_snippets_env.get_template("traefik.yml")
        traefik_content = traefik_template.render(domain_email=environment.domain_email or "")

        extra_vars = {
            "app_name": deployment_config.app_name_slug,
            "environment_name": environment.name,
            "src_docker_compose": str(docker_compose_path),
            "dest_docker_compose": f"/home/{ansible_user}/app/docker-compose.yml",
            "env_file_content": env_file_content,
            "dest_env_file": f"/home/{ansible_user}/app/.env",
            "ansible_user": ansible_user,
            "registry_host_url": environment_state.require_registry_url().split("/")[0],
            "traefik_yml_content": traefik_content,
            **virtual_machine.model_dump(mode="json"),
            **environment.cloud_provider_instance.provider_detail_dump,
        }
        extra_vars.update(environment.cloud_provider_instance.provider_detail_dump)
        try:
            outputs = ansible_runner.run_playbook(
                "main.yml",
                extra_vars=extra_vars,
            )
            logs_b64 = outputs.get("docker_logs", "")
            if logs_b64:
                return base64.b64decode(logs_b64.encode("ascii")).decode("utf-8")
            return ""
        except AnsibleFailed as err:
            # A failed deploy is not the end of the run: the output is fed back to the model so it
            # can repair the compose file and try again.
            return f"Ansible playbook execution failed.\n{err.details.get('output_tail', '')}"

    def _deploy_validate_docker_compose(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
        environment_state: MonolithicDeploymentState,
        docker_compose_content: DockerComposeContent,
    ) -> Tuple[bool, Optional[str], list[ModelMessage], str]:
        deploy_compose_path, docker_compose_path = self._get_deploy_docker_compose_path(environment)
        with open(docker_compose_path, "w", encoding="utf-8") as f:
            f.write(docker_compose_content.content)
        self.events.log(STEP_COMPOSE, f"docker-compose.yml generated at {docker_compose_path}")

        confirmed_env_content = self._confirm_env_vars(
            deployment_config,
            docker_compose_content.env_file_content,
        )

        deployment_output = self._deploy_docker_compose(
            deployment_config,
            environment,
            environment_state,
            confirmed_env_content,
        )

        with self.events.waiting(STEP_COMPOSE, "Validating deployment logs with LLM..."):
            log_validation_prompt = DOCKER_COMPOSE_LOG_VALIDATION_PROMPT_TEMPLATE.format(
                container_logs=deployment_output
            )
            log_validation_response = self.agent.run_sync(
                log_validation_prompt,
                output_type=DockerComposeLogValidation,
                deps=self.agent_deps,
            )

        return (
            log_validation_response.output.is_successful,
            log_validation_response.output.reason,
            log_validation_response.new_messages(),
            confirmed_env_content,
        )

    def _generate_docker_compose(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
        images: Dict[str, str],
        environment_state: MonolithicDeploymentState,
        existing_env_content: Optional[str] = None,
        initial_messages: Optional[list[ModelMessage]] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Generates a compose stack, deploys it, and corrects it until it comes up.

        :param deployment_config: What the repository deploys.
        :param environment: The environment being deployed to.
        :param images: The image built for each service, by slug.
        :param environment_state: The environment's state, for the machine to deploy onto.
        :param existing_env_content: The .env already on the machine, when there is one.
        :param initial_messages: What a previous validation told the model, when resuming from one.
        :return: Whether the stack came up, and what stopped it when it did not. It can return
            False: the model may give up, and a person may be handed the file to fix.
        """
        base_compose_template = self.docker_compose_snippets_env.get_template("base.yml")
        base_compose = base_compose_template.render(app_name=deployment_config.app_name_slug)

        services_info = {}
        service_snippets_list = []
        domains_map = {d.service_name_slug: d for d in environment.domains}
        for service in deployment_config.services:
            services_info[service.name_slug] = service.model_dump(mode="json")
            service_type_slug = service.service_type.value.lower()

            if service.service_type not in [
                ServiceTypeEnum.BACKEND_API,
                ServiceTypeEnum.BACKEND_WORKER,
                ServiceTypeEnum.FULL_STACK,
            ]:
                continue

            template = self.docker_compose_snippets_env.get_template(
                f"services/{service_type_slug}.yml"
            )
            image_url = images[service.name_slug]

            domain_info = domains_map.get(service.name_slug)
            domain = domain_info.domain_name if domain_info else None

            content = template.render(
                image_name=image_url,
                port=service.service_port,
                domain=domain,
                service_name_slug=service.name_slug,
                app_name=deployment_config.app_name_slug,
                environment_name=environment.name,
            )
            service_snippets_list.append(f"# {service.name_slug}\n{content}")
        service_snippets = "\n\n".join(service_snippets_list)

        infra_snippets_list = []
        for infra in deployment_config.infra_deps:
            template = self.docker_compose_snippets_env.get_template(f"{infra.provider.value}.yml")

            content = template.render(
                version=infra.version,
                app_name=deployment_config.app_name_slug,
                architecture=environment_state.require_virtual_machine().architecture.value,
                environment_name=environment.name,
            )
            infra_snippets_list.append(f"# {infra.provider}\n{content}")
        infra_snippets = "\n\n".join(infra_snippets_list)

        services_info_yaml = yaml.dump(services_info)

        confirmed_env_content = existing_env_content or "N/A"
        is_successful = False
        reason: Optional[str] = None
        docker_compose_content = None
        messages = initial_messages or []
        for attempt in range(settings.max_docker_compose_gen_attempts):
            self.events.step(
                STEP_COMPOSE,
                (
                    "Generating docker-compose file, Attempt"
                    f" {attempt + 1}/{settings.max_docker_compose_gen_attempts}"
                ),
            )
            prompt = DOCKER_COMPOSE_GENERATION_PROMPT_TEMPLATE.format(
                base_compose=base_compose,
                services_info_yaml=services_info_yaml,
                service_snippets=service_snippets,
                infra_snippets=infra_snippets,
                previously_confirmed_env_vars=confirmed_env_content,
            )

            spinner_text = (
                "Waiting for LLM to generate docker-compose.yml"
                if attempt == 0
                else "Waiting for LLM to correct docker-compose.yml"
            )
            with self.events.waiting(STEP_COMPOSE, spinner_text):
                docker_compose_response = self.agent.run_sync(
                    prompt,
                    output_type=DockerComposeContent,
                    deps=self.agent_deps,
                    message_history=messages,
                )
                docker_compose_content = docker_compose_response.output

            if docker_compose_content.give_up:
                self.events.warning(
                    STEP_COMPOSE,
                    (
                        "LLM indicated it cannot fix the docker-compose file further:"
                        f" {docker_compose_content.reason}."
                    ),
                )
                break

            is_successful, reason, validation_messages, confirmed_env_content = (
                self._deploy_validate_docker_compose(
                    deployment_config, environment, environment_state, docker_compose_content
                )
            )

            if is_successful:
                self.events.log(STEP_COMPOSE, "Docker compose deployment was successful.")
                break
            self.events.warning(
                STEP_COMPOSE, f"Docker compose validation failed with reason: {reason}."
            )

            messages = docker_compose_response.new_messages() + validation_messages
        else:
            self.events.warning(
                STEP_COMPOSE,
                (
                    "Failed to generate and deploy a valid docker-compose file after"
                    f" {settings.max_docker_compose_gen_attempts} attempts."
                ),
            )

            _, docker_compose_path = self._get_deploy_docker_compose_path(environment)

            # The loop above assigns this on every pass, and it runs at least once for any
            # sensible attempt count, so reaching the manual editor without a file to edit is a
            # wiring mistake rather than anything the run did.
            assert docker_compose_content is not None

            while not is_successful:
                docker_compose_content.content = self.interact.edit(
                    "compose.edit",
                    "Would you like to manually edit the Docker Compose file?",
                    content=docker_compose_content.content,  # last generated content
                    path=docker_compose_path,
                    on_headless="fail",
                )

                is_successful, reason, _, docker_compose_content.env_file_content = (
                    self._deploy_validate_docker_compose(
                        deployment_config,
                        environment,
                        environment_state,
                        docker_compose_content,
                    )
                )

                self.events.log(
                    STEP_COMPOSE,
                    (
                        "Docker compose validation"
                        f" {'succeeded' if is_successful else 'failed'} with reason: {reason}."
                    ),
                )

        return is_successful, reason

    @staticmethod
    def _detect_configuration_changes(
        current_config: DeploymentConfig,
        deployed_state: MonolithicDeploymentState,
    ) -> Tuple[bool, Dict[str, List]]:
        """
        Detects changes between current config and deployed state.

        Returns:
            (has_changes, change_details)
        """
        changes: Dict[str, List] = {
            "services_added": [],
            "services_removed": [],
            "services_modified": [],
            "infra_added": [],
            "infra_removed": [],
            "infra_modified": [],
        }

        # Get deployed snapshots
        deployed_services = deployed_state.deployed_services or []
        deployed_infra_deps = deployed_state.deployed_infra_deps or []

        # Create lookup maps for deployed state
        deployed_services_map = {s["name_slug"]: s for s in deployed_services}
        deployed_infra_map = {i["provider"]: i for i in deployed_infra_deps}

        # Create lookup maps for current config
        current_services_map = {
            s.name_slug: s.model_dump(mode="json") for s in current_config.services
        }
        current_infra_map = {
            i.provider.value: i.model_dump(mode="json") for i in current_config.infra_deps
        }

        # Detect service changes
        for name_slug, current_service in current_services_map.items():
            if name_slug not in deployed_services_map:
                changes["services_added"].append(name_slug)
            else:
                deployed_service = deployed_services_map[name_slug]
                # Check for modifications (port, service_type, env_vars)
                if (
                    current_service.get("service_port") != deployed_service.get("service_port")
                    or current_service.get("service_type") != deployed_service.get("service_type")
                    or current_service.get("env_vars") != deployed_service.get("env_vars")
                ):
                    changes["services_modified"].append(name_slug)

        for name_slug in deployed_services_map:
            if name_slug not in current_services_map:
                changes["services_removed"].append(name_slug)

        # Detect infrastructure changes
        for provider, current_infra in current_infra_map.items():
            if provider not in deployed_infra_map:
                changes["infra_added"].append(provider)
            else:
                deployed_infra = deployed_infra_map[provider]
                # Check for version changes
                if current_infra.get("version") != deployed_infra.get("version"):
                    changes["infra_modified"].append(provider)

        for provider in deployed_infra_map:
            if provider not in current_infra_map:
                changes["infra_removed"].append(provider)

        # Determine if there are any changes
        has_changes = any(
            changes["services_added"]
            or changes["services_removed"]
            or changes["services_modified"]
            or changes["infra_added"]
            or changes["infra_removed"]
            or changes["infra_modified"]
        )

        return has_changes, changes

    @staticmethod
    def _get_frontend_services(deployment_config: DeploymentConfig) -> List[ServiceInfo]:
        """Returns list of frontend services."""
        return [s for s in deployment_config.services if s.service_type == ServiceTypeEnum.FRONTEND]

    @staticmethod
    def _get_backend_services(deployment_config: DeploymentConfig) -> List[ServiceInfo]:
        """Returns list of non-frontend services."""
        return [s for s in deployment_config.services if s.service_type != ServiceTypeEnum.FRONTEND]

    @staticmethod
    def _save_deployment_state(
        env_state: MonolithicDeploymentState,
        deployment_config: DeploymentConfig,
        env_state_path: Path,
    ):
        """Saves deployment state with current config snapshots."""
        env_state.deployed_services = [
            s.model_dump(mode="json") for s in deployment_config.services
        ]
        env_state.deployed_infra_deps = [
            i.model_dump(mode="json") for i in deployment_config.infra_deps
        ]
        env_state.save(env_state_path)

    def _prompt_for_build_env_vars(
        self, service: ServiceInfo, existing_vars: Optional[dict] = None
    ) -> dict:
        """
        Collects the environment variables a frontend needs while it is being built.

        The values live in the environment's answer store, where the secrets among them are kept
        apart. They used to live in ``state.yml``, in the clear; ``existing_vars`` is how a state
        file written by an older Opsmith hands them over, once.

        :param service: The frontend service being built.
        :param existing_vars: What an older state file still holds for it, if anything.
        :return: The value of each variable the service declares.
        """
        service_vars = existing_vars.copy() if existing_vars else {}
        if not service.env_vars:
            return service_vars

        self.events.step(
            STEP_FRONTEND,
            f"Configuring build-time environment variables for service `{service.name_slug}`:",
        )
        for env_var in service.env_vars:
            key = f"build_env.{service.name_slug}.{env_var.key}"

            # An older Opsmith kept these in state.yml. Moving them into the store here is the
            # whole migration: from this run on they are answers like any other, which is also
            # what stops a run with nobody at the keyboard from asking for them again.
            legacy = service_vars.get(env_var.key)
            if legacy is not None and self.answers.get(key) is None:
                self.answers.record(key, legacy, secret=env_var.is_secret)

            remembered = self.answers.get(key)
            default_val = remembered if remembered is not None else env_var.default_value

            if env_var.is_secret:
                message = f"  Enter value for secret '{env_var.key}'"
            else:
                message = f"  Enter value for '{env_var.key}'"

            service_vars[env_var.key] = self.interact.ask(
                key,
                message,
                default=default_val,
                secret=env_var.is_secret,
            )

        return service_vars

    def _build_and_upload_frontend_assets(
        self,
        service: ServiceInfo,
        cloud_provider: BaseCloudProvider,
        environment: DeploymentEnvironment,
        cdn_state: FrontendCDNState,
        build_env_vars: dict,
    ):
        """
        Builds frontend assets and uploads them to cloud storage.

        :param service: The frontend service being built.
        :param cloud_provider: The provider holding the bucket and the CDN.
        :param environment: The environment being deployed to.
        :param cdn_state: Where the built assets go.
        :param build_env_vars: The variables the build needs, which come from the answer store
            rather than from ``cdn_state``, because some of them are secret.
        """
        self.events.step(
            STEP_FRONTEND, f"Building and deploying assets for '{service.name_slug}'..."
        )

        deploy_path = (
            self.deployments_path
            / "environments"
            / environment.name
            / "frontend_deploy"
            / service.name_slug
        )
        deploy_path.mkdir(parents=True, exist_ok=True)

        ansible_runner = self.provisioners.ansible(deploy_path, step=STEP_FRONTEND)
        ansible_runner.copy_template("frontend_deploy", cloud_provider.name())
        extra_vars = {
            "build_cmd": service.build_cmd,
            "build_dir": service.build_dir,
            "build_path": service.build_path,
            "bucket_name": cdn_state.bucket_name,
            "project_root": str(self.src_dir),
            "build_env_vars": build_env_vars,
            "cdn_distribution_id": cdn_state.cdn_distribution_id,
            "cdn_url_map": cdn_state.cdn_url_map,
        }
        extra_vars.update(cloud_provider.provider_detail_dump)
        ansible_runner.run_playbook("main.yml", extra_vars=extra_vars, inventory="localhost")
        self.events.log(STEP_FRONTEND, f"Assets for '{service.name_slug}' deployed successfully.")

    def _create_frontend_bucket_cert(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
        service_info: ServiceInfo,
        domain_info: DomainInfo,
        cloud_provider: BaseCloudProvider,
    ):
        """Creates CDN part 1 (bucket and cert) and cloud storage for a frontend service."""
        self.events.step(
            STEP_FRONTEND,
            (
                f"Creating CDN part 1 (bucket, cert) for service '{service_info.name_slug}'"
                f" '({domain_info.domain_name})'..."
            ),
        )
        infra_path = (
            self.deployments_path
            / "environments"
            / environment.name
            / "frontend_bucket_cert"
            / service_info.name_slug
        )
        infra_path.mkdir(parents=True, exist_ok=True)

        tf = self.provisioners.terraform(infra_path, step=STEP_FRONTEND)
        tf.copy_template("frontend_bucket_cert", cloud_provider.name())

        variables = {
            "app_name": deployment_config.app_name_slug,
            "domain_name": domain_info.domain_name,
        }

        env_vars = cloud_provider.provider_detail_dump
        tf.init_and_apply(variables, env_vars=env_vars)
        outputs = tf.get_output()

        dns_records_json = outputs.get("dns_records")
        if dns_records_json:
            self._wait_for_dns_records(json.loads(dns_records_json))
            with self.events.waiting(STEP_DNS, "Waiting 15 seconds for DNS propagation..."):
                time.sleep(15)

        return outputs

    def _create_frontend_cdn(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
        service_info: ServiceInfo,
        domain_info: DomainInfo,
        cloud_provider: BaseCloudProvider,
        cdn_part1_outputs: dict,
    ):
        """Creates CDN part 2 (distribution) for a frontend service."""
        self.events.step(
            STEP_FRONTEND,
            (
                f"Creating CDN part 2 (distribution) for service '{service_info.name_slug}'"
                f" '({domain_info.domain_name})'..."
            ),
        )
        infra_path = (
            self.deployments_path
            / "environments"
            / environment.name
            / "frontend_cdn"
            / service_info.name_slug
        )
        infra_path.mkdir(parents=True, exist_ok=True)

        tf = self.provisioners.terraform(infra_path, step=STEP_FRONTEND)
        tf.copy_template("frontend_cdn", cloud_provider.name())

        variables = {
            "app_name": deployment_config.app_name_slug,
            "domain_name": domain_info.domain_name,
        }

        env_vars = cloud_provider.provider_detail_dump
        env_vars.update(cdn_part1_outputs)
        tf.init_and_apply(variables, env_vars=env_vars)
        outputs = tf.get_output()

        dns_records_json = outputs.get("dns_records")
        if dns_records_json:
            self._wait_for_dns_records(json.loads(dns_records_json))

        return outputs

    def _select_virtual_machine_type(
        self,
        deployment_config: DeploymentConfig,
        cloud_provider: BaseCloudProvider,
    ) -> MachineType:
        """Selects a virtual machine type for a new deployment environment."""
        with self.events.waiting(STEP_VM, "Fetching available instance types"):
            machine_type_list = cloud_provider.get_instance_types()

        services_yaml = yaml.dump([s.model_dump(mode="json") for s in deployment_config.services])
        infra_deps_yaml = yaml.dump(
            [i.model_dump(mode="json") for i in deployment_config.infra_deps]
        )
        machine_types_yaml = yaml.dump(machine_type_list.model_dump(mode="json"))

        prompt = MONOLITHIC_MACHINE_REQUIREMENTS_PROMPT_TEMPLATE.format(
            services_yaml=services_yaml,
            infra_deps_yaml=infra_deps_yaml,
            machine_types_yaml=machine_types_yaml,
        )

        with self.events.waiting(STEP_VM, "Waiting for LLM to select machine types"):
            response = self.agent.run_sync(
                prompt, output_type=MachineTypeList, deps=self.agent_deps
            )

        suggested_machine_types = response.output
        choices = suggested_machine_types.as_options()

        if not choices:
            raise LlmGaveUp(
                "No suitable instance types found.",
                hint="The model returned no machine type matching the detected services.",
            )

        return self.interact.select(
            "env.instance_type", "Select an instance type for the new environment", choices
        )

    def _wait_for_dns_records(
        self,
        dns_records: List[Dict[str, str]],
    ):
        """
        Shows the DNS records the user has to create, and waits until they resolve.

        Waiting rather than asking is what makes the step resumable: a run with nobody at the
        keyboard polls until its timeout and then stops with the outstanding records, and creating
        them and running the command again picks up here.

        :param dns_records: The records the user has to create, each with a type, name and value.
        :raises InteractionCancelled: The user stopped waiting.
        :raises PendingActionError: A record was still missing when the run gave up waiting.
        """
        self.events.step(
            STEP_DNS,
            "Please configure the following DNS records for your domain:",
            records=dns_records,
        )

        for record in dns_records:
            lines = ["----------------------------------------"]
            if record.get("comment"):
                lines.append(f"  Comment: {record.get('comment')}")
            lines.append(f"  Type:    {record.get('type')} Record")
            lines.append(f"  Name:    {record.get('name')}")
            lines.append(f"  Value:   {record.get('value')}")
            lines.append("----------------------------------------")
            self.events.log(STEP_DNS, "\n".join(lines), record=record)

        self.requested_dns_records.extend(
            DnsRecord(
                type=str(record.get("type", "")),
                name=str(record.get("name", "")),
                value=str(record.get("value", "")),
            )
            for record in dns_records
        )

        for record in dns_records:
            self.interact.wait_for(
                f"dns.{_dns_record_slug(record)}",
                (
                    f"Create the {record.get('type')} record for {record.get('name')}, pointing at"
                    f" {record.get('value')}. This can take a few minutes to propagate."
                ),
                check=functools.partial(dns_record_is_published, record),
                details={"records": [record]},
            )

    def _deploy_frontend_service(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
        domain_info: DomainInfo,
        cloud_provider: BaseCloudProvider,
        service: ServiceInfo,
        env_state: MonolithicDeploymentState,
    ):
        """Deploys a frontend service to a new deployment environment."""
        cdn_part1_outputs = self._create_frontend_bucket_cert(
            deployment_config, environment, service, domain_info, cloud_provider
        )

        cdn_part2_outputs = self._create_frontend_cdn(
            deployment_config,
            environment,
            service,
            domain_info,
            cloud_provider,
            cdn_part1_outputs,
        )
        cdn_outputs = {**cdn_part1_outputs, **cdn_part2_outputs}

        build_env_vars = self._prompt_for_build_env_vars(service)
        cdn_state = FrontendCDNState(
            service_name_slug=service.name_slug,
            domain_name=domain_info.domain_name,
            bucket_name=cdn_outputs.get("bucket_name"),
            cdn_domain_name=cdn_outputs.get("cdn_domain_name"),
            cdn_ip_address=cdn_outputs.get("cdn_ip_address"),
            cdn_distribution_id=cdn_outputs.get("cdn_distribution_id"),
            cdn_url_map=cdn_outputs.get("cdn_url_map"),
            certificate_id=cdn_outputs.get("certificate_id"),
        )
        env_state.frontend_cdn.append(cdn_state)

        self._build_and_upload_frontend_assets(
            service,
            cloud_provider,
            environment,
            cdn_state,
            build_env_vars,
        )
        self.events.log(
            STEP_FRONTEND,
            f"Your website is available at: https://{domain_info.domain_name}",
            url=f"https://{domain_info.domain_name}",
        )

    def deploy(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
    ) -> EnvCreateResult:
        """
        Creates a monolithic deployment environment using the provided deployment configuration and
        environment details. This function includes steps for setting up a container registry,
        building and pushing images, estimating resource requirements, selecting cloud provider
        instance types, creating a virtual machine, and generating Docker Compose configurations
        for deployment.

        :param deployment_config: Configuration object containing details of services, infrastructure
            dependencies, and other deployment settings.
        :param environment: Deployment environment details, including region and other configurations.
        :return: The machine and registry that were created, where each service is now reachable,
            and every DNS record the run asked somebody to create along the way.
        """
        frontend_services = self._get_frontend_services(deployment_config)
        other_services = self._get_backend_services(deployment_config)

        cloud_provider = environment.cloud_provider_instance
        env_state_path = self._get_env_state_path(environment.name)

        env_state = MonolithicDeploymentState()
        if frontend_services:
            self.events.step(STEP_FRONTEND, "Deploying frontend services...")
            domains_map = {d.service_name_slug: d for d in environment.domains}

            for service in frontend_services:
                domain_info = domains_map.get(service.name_slug)
                if not domain_info:
                    self.events.warning(
                        STEP_FRONTEND,
                        f"No domain configured for frontend service {service.name_slug}. Skipping.",
                    )
                    continue
                self._deploy_frontend_service(
                    deployment_config, environment, domain_info, cloud_provider, service, env_state
                )

        if other_services:
            original_services = deployment_config.services
            deployment_config.services = other_services

            self.events.step(
                STEP_REGISTRY,
                (
                    "Setting up container registry for region"
                    f" '{environment.cloud_provider_detail.region}'..."
                ),
            )
            registry_url = self._setup_container_registry(deployment_config, environment)
            images = self._build_and_push_images(deployment_config, environment, registry_url)

            self.events.step(STEP_VM, f"Selecting instance type on {cloud_provider.name()}...")
            selected_machine_type = self._select_virtual_machine_type(
                deployment_config, cloud_provider
            )
            instance_type = selected_machine_type.name

            instance_arch = selected_machine_type.architecture
            self.events.log(
                STEP_VM,
                f"Selected instance type: {instance_type} ({instance_arch.value})",
                instance_type=instance_type,
                architecture=instance_arch.value,
            )

            self.events.step(STEP_VM, "Creating new virtual machine for monolithic deployment...")
            virtual_machine_state = self._create_virtual_machine(
                deployment_config, environment, selected_machine_type, cloud_provider
            )
            deployment_config.services = original_services

            dns_records = []
            for domain in environment.get_domains_for_services(other_services):
                dns_records.append(
                    {
                        "type": "A",
                        "name": domain.domain_name,
                        "value": virtual_machine_state.public_ip,
                    }
                )
            self._wait_for_dns_records(dns_records)

            env_state.registry_url = registry_url
            env_state.virtual_machine = virtual_machine_state
            self._generate_docker_compose(deployment_config, environment, images, env_state)

            for domain in environment.get_domains_for_services(other_services):
                self.events.log(
                    STEP_COMPOSE,
                    f"Your website is available at: https://{domain.domain_name}",
                    url=f"https://{domain.domain_name}",
                )

        # Save config snapshots for change detection
        self._save_deployment_state(env_state, deployment_config, env_state_path)

        return EnvCreateResult(
            environment=environment.name,
            provider=cloud_provider.name(),
            region=environment.cloud_provider_detail.region,
            strategy=self.name(),
            resources=self._collect_resources(environment, env_state),
            registry_url=env_state.registry_url,
            urls=self._collect_urls(deployment_config, environment, env_state),
            dns_records=list(self.requested_dns_records),
        )

    def release(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
    ) -> ReleaseResult:
        """
        Deploys the application.

        :param deployment_config: What the repository deploys.
        :param environment: The environment being released to.
        :return: What was built and released, and whether the stack came up healthy.
        """
        env_state_path = self._get_env_state_path(environment.name)
        env_state = MonolithicDeploymentState.load(env_state_path)
        cloud_provider = environment.cloud_provider_instance

        released: List[str] = []
        images: Dict[str, str] = {}
        validated: Optional[bool] = None
        validation_reason: Optional[str] = None

        # Nothing here writes the state file. A release deploys what the configuration already
        # describes: the build-time values it collects belong to the answer store now, and the
        # snapshots of what is deployed are written by deploy and by update, which are the two
        # that can change them.

        # Release frontend services
        frontend_services = self._get_frontend_services(deployment_config)
        if frontend_services:
            self.events.step(STEP_FRONTEND, "Releasing frontend services...")
            cdn_state_map = {cdn.service_name_slug: cdn for cdn in env_state.frontend_cdn}

            for service in frontend_services:
                cdn_state = cdn_state_map.get(service.name_slug)
                if not cdn_state:
                    self.events.warning(
                        STEP_FRONTEND,
                        (
                            "No existing CDN state found for frontend service"
                            f" '{service.name_slug}'. Skipping release."
                        ),
                    )
                    continue

                build_env_vars = self._prompt_for_build_env_vars(
                    service, existing_vars=cdn_state.build_env_vars
                )

                self._build_and_upload_frontend_assets(
                    service,
                    cloud_provider,
                    environment,
                    cdn_state,
                    build_env_vars,
                )
                self.events.log(
                    STEP_FRONTEND,
                    f"Your website is available at: https://{cdn_state.domain_name}",
                    url=f"https://{cdn_state.domain_name}",
                )
                released.append(service.name_slug)

        # Release other services
        other_services = self._get_backend_services(deployment_config)

        if other_services:
            if env_state.virtual_machine:
                images = self._build_and_push_images(
                    deployment_config, environment, env_state.require_registry_url()
                )

                ansible_user = env_state.virtual_machine.user
                env_file_path = f"/home/{ansible_user}/app/.env"
                docker_compose_file_path = f"/home/{ansible_user}/app/docker-compose.yml"
                fetched_files = self._fetch_remote_deployment_files(
                    deployment_config,
                    environment,
                    env_state.virtual_machine,
                    [env_file_path, docker_compose_file_path],
                )
                existing_env_content = fetched_files[0]
                existing_compose_content = fetched_files[1]

                # The machine is what the application is actually running with, so its environment
                # file is the truth about these values from here on - not the local cache that
                # carried them until it existed.
                self.answers.adopt_env_file(existing_env_content)

                docker_compose_content = DockerComposeContent(
                    content=existing_compose_content,
                    env_file_content=existing_env_content,
                )

                is_successful, reason, validation_messages, confirmed_env_content = (
                    self._deploy_validate_docker_compose(
                        deployment_config,
                        environment,
                        env_state,
                        docker_compose_content,
                    )
                )

                if is_successful:
                    self.events.log(STEP_COMPOSE, "Release deployed successfully.")
                else:
                    self.events.warning(STEP_COMPOSE, f"Deployment validation failed: {reason}")
                    self.events.step(STEP_COMPOSE, "Regenerating docker-compose configuration...")
                    is_successful, reason = self._generate_docker_compose(
                        deployment_config,
                        environment,
                        images,
                        env_state,
                        existing_env_content=confirmed_env_content,
                        initial_messages=validation_messages,
                    )

                # A reason only describes a failure, so a release that came up carries none even
                # if the last validation had something to say on the way there.
                validated = is_successful
                validation_reason = None if is_successful else reason
                released.extend(service.name_slug for service in other_services)
            else:
                # This can happen if only frontend was deployed
                self.events.warning(
                    STEP_COMPOSE,
                    (
                        "No virtual machine provisioned for this environment. Skipping release of"
                        " other services."
                    ),
                )

        return ReleaseResult(
            environment=environment.name,
            images=images,
            services=released,
            validated=validated,
            validation_reason=validation_reason,
            urls=self._collect_urls(deployment_config, environment, env_state),
        )

    def destroy(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
    ) -> DestroyResult:
        """
        Destroys the environment's infrastructure.

        :param deployment_config: What the repository deploys.
        :param environment: The environment being destroyed.
        :return: What was torn down, named as it is addressed. An environment that was never
            deployed destroys nothing and still returns a result: there was nothing to fail at.
        """
        self.events.step(STEP_DESTROY, "Destroying monolithic environment...")
        cloud_provider = environment.cloud_provider_instance
        region = environment.cloud_provider_detail.region
        destroyed: List[Resource] = []

        env_state_path = self._get_env_state_path(environment.name)
        if not env_state_path.exists():
            self.events.warning(
                STEP_DESTROY,
                (
                    f"No state file found for environment '{environment.name}'. Skipping"
                    " infrastructure destruction."
                ),
            )
            return DestroyResult(environment=environment.name, destroyed=destroyed)

        env_state = MonolithicDeploymentState.load(env_state_path)

        # Destroy frontend CDNs
        for cdn_state in env_state.frontend_cdn:
            self.events.step(
                STEP_DESTROY,
                (
                    "Destroying content delivery network for service"
                    f" '{cdn_state.service_name_slug}'..."
                ),
            )
            # Destroy part 2 first
            infra_path_p2 = (
                self.deployments_path
                / "environments"
                / environment.name
                / "frontend_cdn"
                / cdn_state.service_name_slug
            )
            if infra_path_p2.exists():
                tf_p2 = self.provisioners.terraform(infra_path_p2, step=STEP_DESTROY)
                variables_p2 = {
                    "app_name": deployment_config.app_name_slug,
                    "domain_name": cdn_state.domain_name,
                    "bucket_name": cdn_state.bucket_name,
                    "certificate_id": cdn_state.certificate_id,
                }
                env_vars = cloud_provider.provider_detail_dump
                tf_p2.destroy(variables_p2, env_vars=env_vars)

            # Delete the bucket contents
            self._cleanup_cloud_storage(
                environment, cdn_state.service_name_slug, cloud_provider, cdn_state.bucket_name
            )

            # Destroy part 1
            infra_path_p1 = (
                self.deployments_path
                / "environments"
                / environment.name
                / "frontend_bucket_cert"
                / cdn_state.service_name_slug
            )

            if infra_path_p1.exists():
                tf_p1 = self.provisioners.terraform(infra_path_p1, step=STEP_DESTROY)
                variables_p1 = {
                    "app_name": deployment_config.app_name_slug,
                    "domain_name": cdn_state.domain_name,
                }
                env_vars = cloud_provider.provider_detail_dump
                tf_p1.destroy(variables_p1, env_vars=env_vars)
                destroyed.append(_cdn_resource(cdn_state, region))
            else:
                self.events.warning(
                    STEP_DESTROY,
                    (
                        "No content delivery network infrastructure found for service"
                        f" '{cdn_state.service_name_slug}'. Skipping destruction."
                    ),
                )

        # Destroy virtual machine
        if env_state.virtual_machine:
            infra_path = (
                self.deployments_path / "environments" / environment.name / "virtual_machine"
            )
            if infra_path.exists():
                tf = self.provisioners.terraform(infra_path, step=STEP_DESTROY)

                variables = {
                    "app_name": deployment_config.app_name_slug,
                    "environment": environment.name,
                    "instance_type": env_state.virtual_machine.instance_type,
                    "instance_arch": env_state.virtual_machine.architecture.value,
                    "ssh_pub_key": self._get_ssh_public_key(),
                }
                env_vars = cloud_provider.provider_detail.model_dump(mode="json")
                tf.destroy(variables, env_vars=env_vars)
                destroyed.append(_machine_resource(env_state.virtual_machine, region))
            else:
                self.events.warning(
                    STEP_DESTROY,
                    (
                        "No virtual machine infrastructure found for environment"
                        f" '{environment.name}'. Skipping VM destruction."
                    ),
                )

        # Clean up environment directory
        env_dir_path = self.deployments_path / "environments" / environment.name
        if env_dir_path.exists():
            try:
                shutil.rmtree(env_dir_path)
                destroyed.append(
                    Resource(
                        kind=ResourceKind.WORKING_DIRECTORY,
                        id=str(env_dir_path),
                        name=environment.name,
                    )
                )
                self.events.log(STEP_DESTROY, f"Environment directory '{env_dir_path}' deleted.")
            except OSError as e:
                self.events.warning(
                    STEP_DESTROY, f"Error deleting environment directory {env_dir_path}: {e}"
                )

        # Clean up the container registry if there are no more envs in that region
        remaining_environments = [
            e for e in deployment_config.environments if e.name != environment.name
        ]
        remaining_environments_in_region = [
            e
            for e in remaining_environments
            if e.cloud_provider_detail.region == environment.cloud_provider_detail.region
        ]

        if not remaining_environments_in_region and env_state.registry_url:
            self.events.step(
                STEP_DESTROY,
                (
                    f"Last environment in region '{environment.cloud_provider_detail.region}'."
                    " Destroying container registry..."
                ),
            )
            app_name = deployment_config.app_name_slug
            registry_name = slugify(f"{app_name}-{environment.cloud_provider_detail.region}")

            registry_infra_path = (
                self.deployments_path
                / "environments"
                / "global"
                / f"{cloud_provider.name()}-{environment.cloud_provider_detail.region}"
                / "container_registry"
            )

            if registry_infra_path.exists():
                tf = self.provisioners.terraform(registry_infra_path, step=STEP_DESTROY)
                variables = {
                    "app_name": app_name,
                    "registry_name": registry_name,
                    "force_delete": "true",
                }
                env_vars = cloud_provider.provider_detail_dump
                tf.destroy(variables, env_vars=env_vars)
                destroyed.append(_registry_resource(env_state.registry_url, region))
                self.events.log(STEP_DESTROY, "Container registry destroyed successfully.")
                try:
                    shutil.rmtree(registry_infra_path.parent)
                    self.events.log(
                        STEP_DESTROY,
                        f"Global region directory '{registry_infra_path.parent}' deleted.",
                    )
                except OSError as e:
                    self.events.warning(
                        STEP_DESTROY,
                        f"Error deleting global region directory {registry_infra_path.parent}: {e}",
                    )
            else:
                self.events.warning(
                    STEP_DESTROY,
                    "Container registry infrastructure path not found. Skipping destruction.",
                )

        # Clean up the deployment config
        deployment_config.environments = remaining_environments
        config_path = deployment_config.save(self.deployments_path)
        self.events.log(STEP_DESTROY, f"Deployment configuration saved to: {config_path}")

        return DestroyResult(environment=environment.name, destroyed=destroyed)

    def run(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
        service_name_slug: str,
        command: str,
    ) -> RunResult:
        """
        Runs a command on a specific service.

        The playbook no longer treats a non-zero exit as its own failure: it hands the status and
        the two streams back through the markers the provisioner watches for, and Opsmith exits
        with the status. A playbook that could not run the command at all still fails.

        :param deployment_config: What the repository deploys.
        :param environment: The environment the command runs in.
        :param service_name_slug: The service to run it on.
        :param command: The command to run.
        :return: What the command exited with, and the tail of what it wrote.
        """
        self.events.step(STEP_RUN, f"Running command on '{service_name_slug}': {command}")
        env_state_path = self._get_env_state_path(environment.name)
        env_state = MonolithicDeploymentState.load(env_state_path)

        if not env_state.virtual_machine:
            raise UnknownEnvironment(
                "Virtual machine is not provisioned for this environment.",
                hint="Run 'opsmith deploy' for this environment first.",
            )

        ansible_user = env_state.virtual_machine.user

        run_command_path = (
            self.deployments_path / "environments" / environment.name / "docker_compose_run"
        )
        ansible_runner = self.provisioners.ansible(run_command_path, step=STEP_RUN)
        ansible_runner.copy_template(
            "docker_compose_run", environment.cloud_provider_instance.name()
        )

        extra_vars = {
            "app_name": deployment_config.app_name_slug,
            "environment_name": environment.name,
            "service_name_slug": service_name_slug,
            "command_to_run": command,
            "ansible_user": ansible_user,
            **env_state.virtual_machine.model_dump(mode="json"),
            **environment.cloud_provider_instance.provider_detail_dump,
        }
        outputs = ansible_runner.run_playbook(
            "main.yml",
            extra_vars=extra_vars,
        )

        exit_code = _reported_exit_code(outputs)
        if exit_code != 0:
            self.events.warning(STEP_RUN, f"The command exited with code {exit_code}.")

        return RunResult(
            environment=environment.name,
            service=service_name_slug,
            target=env_state.virtual_machine.public_ip,
            command=command,
            exit_code=exit_code,
            stdout_tail=_decode_stream(outputs.get("stdout")),
            stderr_tail=_decode_stream(outputs.get("stderr")),
        )

    def update(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
    ) -> UpdateResult:
        """
        Updates service configuration for an existing deployment.

        Handles:
        - Service additions/removals
        - Infrastructure dependency changes
        - Port/configuration changes

        :param deployment_config: What the repository deploys.
        :param environment: The environment being updated.
        :return: What changed, or why nothing did. Finding no changes, being declined, or having
            no machine to update are three ways of doing nothing, and none of them is a failure -
            each comes back as a result saying which it was.
        """
        self.events.step(STEP_SETUP, "Starting configuration update...")
        images: Dict[str, str] = {}

        # Load existing state
        env_state_path = self._get_env_state_path(environment.name)
        if not env_state_path.exists():
            raise UnknownEnvironment(
                f"No state file found at {env_state_path}. Run 'deploy' first.",
                hint="Run 'opsmith deploy' for this environment first.",
                details={"path": str(env_state_path)},
            )

        env_state = MonolithicDeploymentState.load(env_state_path)

        # Detect changes
        has_changes, changes = self._detect_configuration_changes(deployment_config, env_state)

        if not has_changes:
            self.events.log(STEP_SETUP, "No configuration changes detected. Nothing to update.")
            return UpdateResult(
                environment=environment.name,
                applied=False,
                reason="No configuration changes detected.",
                changes=changes,
                urls=self._collect_urls(deployment_config, environment, env_state),
            )

        # Display detected changes
        self.events.step(STEP_SETUP, "Configuration changes detected:", changes=changes)
        change_labels = {
            "services_added": "Services added",
            "services_removed": "Services removed",
            "services_modified": "Services modified",
            "infra_added": "Infrastructure added",
            "infra_removed": "Infrastructure removed",
            "infra_modified": "Infrastructure modified",
        }
        for change_key, label in change_labels.items():
            if changes[change_key]:
                self.events.log(STEP_SETUP, f"  {label}: {', '.join(changes[change_key])}")

        # Warn about infrastructure changes
        infra_changes = (
            changes["infra_added"] or changes["infra_removed"] or changes["infra_modified"]
        )
        if infra_changes:
            self.events.warning(
                STEP_SETUP,
                "Infrastructure changes detected. Existing data in affected services may be lost.",
            )
            proceed = self.interact.confirm(
                "update.confirm_infra_changes",
                "Do you want to continue with the update?",
                details=changes,
                default=False,
            )
            if not proceed:
                self.events.warning(STEP_SETUP, "Update cancelled by user.")
                return UpdateResult(
                    environment=environment.name,
                    applied=False,
                    reason="The infrastructure changes were not confirmed.",
                    changes=changes,
                    urls=self._collect_urls(deployment_config, environment, env_state),
                )

        cloud_provider = environment.cloud_provider_instance

        # Handle frontend services
        frontend_services = self._get_frontend_services(deployment_config)
        if frontend_services:
            self.events.step(STEP_FRONTEND, "Updating frontend services...")
            cdn_state_map = {cdn.service_name_slug: cdn for cdn in env_state.frontend_cdn}
            domains_map = {d.service_name_slug: d for d in environment.domains}

            for service in frontend_services:
                cdn_state = cdn_state_map.get(service.name_slug)
                if not cdn_state:
                    # New frontend service - create CDN
                    domain_info = domains_map.get(service.name_slug)
                    if not domain_info:
                        # This should not happen if the deploy command did its job
                        self.events.warning(
                            STEP_FRONTEND,
                            (
                                f"No domain configured for '{service.name_slug}'. Skipping CDN"
                                " creation."
                            ),
                        )
                        continue
                    self._deploy_frontend_service(
                        deployment_config,
                        environment,
                        domain_info,
                        cloud_provider,
                        service,
                        env_state,
                    )
                else:
                    # Existing service - update build env vars and assets
                    build_env_vars = self._prompt_for_build_env_vars(
                        service, existing_vars=cdn_state.build_env_vars
                    )

                    self._build_and_upload_frontend_assets(
                        service, cloud_provider, environment, cdn_state, build_env_vars
                    )
                    self.events.log(
                        STEP_FRONTEND,
                        f"Frontend service '{service.name_slug}' updated successfully.",
                    )

        # Handle backend services
        other_services = [
            s for s in deployment_config.services if s.service_type != ServiceTypeEnum.FRONTEND
        ]

        if other_services:
            if not env_state.virtual_machine:
                self.events.warning(
                    STEP_COMPOSE,
                    (
                        "No virtual machine provisioned for this environment. Cannot update backend"
                        " services."
                    ),
                )
                return UpdateResult(
                    environment=environment.name,
                    applied=False,
                    reason="No virtual machine is provisioned for this environment.",
                    changes=changes,
                    urls=self._collect_urls(deployment_config, environment, env_state),
                )

            self.events.step(STEP_COMPOSE, "Updating backend services...")

            # Rebuild and push images
            images = self._build_and_push_images(
                deployment_config, environment, env_state.require_registry_url()
            )

            # Fetch existing .env file
            env_file_path = f"/home/{env_state.virtual_machine.user}/app/.env"
            fetched_files = self._fetch_remote_deployment_files(
                deployment_config,
                environment,
                env_state.virtual_machine,
                [env_file_path],
            )
            existing_env_content = fetched_files[0]
            # The machine is what the application is actually running with, so its environment
            # file is the truth about these values from here on - not the local cache that
            # carried them until it existed.
            self.answers.adopt_env_file(existing_env_content)

            # Regenerate docker-compose with existing env as starting point
            self.events.step(STEP_COMPOSE, "Regenerating docker-compose configuration...")
            self._generate_docker_compose(
                deployment_config,
                environment,
                images,
                env_state,
                existing_env_content=existing_env_content,
            )

            self.events.log(STEP_COMPOSE, "Backend services updated successfully.")

        # Update state with new config snapshots
        self._save_deployment_state(env_state, deployment_config, env_state_path)

        return UpdateResult(
            environment=environment.name,
            applied=True,
            changes=changes,
            images=images,
            urls=self._collect_urls(deployment_config, environment, env_state),
        )

    def status(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
    ) -> EnvStatusResult:
        """
        Reports what this environment is running, reading only the state file.

        Nothing here reaches a cloud or a machine, which is what lets ``opsmith env status``
        declare no external tools and answer on a host with an empty PATH. An environment with no
        state file has never been deployed, and says so rather than failing: asking after one is a
        reasonable thing to do.

        :param deployment_config: What the repository deploys.
        :param environment: The environment being reported on.
        :return: What the last deploy or update left behind.
        """
        described = {
            "environment": environment.name,
            "provider": str(environment.cloud_provider.get("name", "")),
            "region": environment.cloud_provider_detail.region,
            "strategy": environment.strategy,
        }

        env_state_path = self._get_env_state_path(environment.name)
        if not env_state_path.exists():
            return EnvStatusResult(**described, deployed=False)

        env_state = MonolithicDeploymentState.load(env_state_path)

        return EnvStatusResult(
            **described,
            deployed=True,
            resources=self._collect_resources(environment, env_state),
            registry_url=env_state.registry_url,
            services=self._collect_services(deployment_config, environment, env_state),
            urls=self._collect_urls(deployment_config, environment, env_state),
        )
