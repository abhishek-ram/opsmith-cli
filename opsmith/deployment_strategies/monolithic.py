import base64
import json
import shutil
import subprocess
import time
from io import StringIO
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import inquirer
import jinja2
import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, Field
from pydantic_ai.messages import ModelMessage
from rich import print

from opsmith.cloud_providers.base import BaseCloudProvider, MachineType, MachineTypeList
from opsmith.deployment_strategies.base import BaseDeploymentStrategy
from opsmith.exceptions import MonolithicDeploymentError, OpsmithException
from opsmith.infra_provisioners.ansible_provisioner import AnsibleProvisioner
from opsmith.infra_provisioners.terraform_provisioner import TerraformProvisioner
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
)
from opsmith.utils import WaitingSpinner, slugify


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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.docker_compose_snippets_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(self.templates_dir / "docker_compose_snippets"),
            autoescape=False,
        )

    @staticmethod
    def _confirm_env_vars(
        deployment_config: DeploymentConfig,
        env_file_content: str,
    ) -> str:
        """
        Parses environment variables from LLM response, confirms with user, and returns updated content.
        """
        # Parse env_file_content from LLM
        env_file_vars = dotenv_values(stream=StringIO(env_file_content))

        env_defaults = deployment_config.get_env_var_defaults()

        # Prepare questions for inquirer
        questions = []
        for key, value in sorted(env_file_vars.items()):
            # Precedence: llm > code default
            default_value = value or env_defaults.get(key)
            questions.append(
                inquirer.Text(
                    name=key,
                    message=f"Enter value for {key}",
                    default=default_value,
                )
            )

        # Prompt user
        print("\n[bold]Please confirm or provide values for environment variables:[/bold]")
        answers = inquirer.prompt(questions)

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
        print("\n[bold blue]Deploying docker-compose stack to the VM \n[/bold blue]")
        ansible_user = environment_state.virtual_machine.user
        deploy_compose_path, docker_compose_path = self._get_deploy_docker_compose_path(environment)

        ansible_runner = AnsibleProvisioner(working_dir=deploy_compose_path)
        ansible_runner.copy_template(
            "docker_compose_deploy", environment.cloud_provider_instance.name().lower()
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
            "registry_host_url": environment_state.registry_url.split("/")[0],
            "traefik_yml_content": traefik_content,
            **environment_state.virtual_machine.model_dump(mode="json"),
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
        except subprocess.CalledProcessError as e:
            return f"Ansible playbook execution failed.\nStdout:\n{e.stdout}\n\nStderr:\n{e.stderr}"

    def _deploy_validate_docker_compose(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
        environment_state: MonolithicDeploymentState,
        docker_compose_content: DockerComposeContent,
    ) -> Tuple[bool, str, list[ModelMessage], str]:
        deploy_compose_path, docker_compose_path = self._get_deploy_docker_compose_path(environment)
        with open(docker_compose_path, "w", encoding="utf-8") as f:
            f.write(docker_compose_content.content)
        print(f"[bold green]docker-compose.yml generated at {docker_compose_path}[/bold green]")

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

        with WaitingSpinner("Validating deployment logs with LLM..."):
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
    ):
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
                architecture=environment_state.virtual_machine.architecture.value,
                environment_name=environment.name,
            )
            infra_snippets_list.append(f"# {infra.provider}\n{content}")
        infra_snippets = "\n\n".join(infra_snippets_list)

        services_info_yaml = yaml.dump(services_info)

        confirmed_env_content = existing_env_content or "N/A"
        is_successful = False
        docker_compose_content = None
        messages = []
        for attempt in range(settings.max_docker_compose_gen_attempts):
            print(
                "\n[bold blue]Generating docker-compose file, Attempt"
                f" {attempt + 1}/{settings.max_docker_compose_gen_attempts}[/bold blue]"
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
            with WaitingSpinner(text=spinner_text):
                docker_compose_response = self.agent.run_sync(
                    prompt,
                    output_type=DockerComposeContent,
                    deps=self.agent_deps,
                    message_history=messages,
                )
                docker_compose_content = docker_compose_response.output

            if docker_compose_content.give_up:
                print(
                    "[bold yellow]LLM indicated it cannot fix the docker-compose file"
                    f" further: \n{docker_compose_content.reason}.[/bold yellow]"
                )
                break

            is_successful, reason, validation_messages, confirmed_env_content = (
                self._deploy_validate_docker_compose(
                    deployment_config, environment, environment_state, docker_compose_content
                )
            )

            if is_successful:
                print("[bold green]Docker compose deployment was successful.[/bold green]")
                break
            print(f"[red]Docker compose validation 'failed' with reason[/red]: \n {reason}.")

            messages = docker_compose_response.new_messages() + validation_messages
        else:
            print(
                "[bold red]Failed to generate and deploy a valid docker-compose file after"
                f" {settings.max_docker_compose_gen_attempts} attempts.[/bold red]"
            )

            while not is_successful:
                editor_questions = [
                    inquirer.Editor(
                        "docker_compose_file",
                        message="Would you like to manually edit the Docker Compose file?",
                        default=lambda _: docker_compose_content.content,  # last generated content
                    )
                ]
                editor_answers = inquirer.prompt(editor_questions)
                if not editor_answers:
                    raise OpsmithException("Docker compose generation aborted by user.")
                docker_compose_content.content = editor_answers["docker_compose_file"]

                is_successful, reason, _, docker_compose_content.env_file_content = (
                    self._deploy_validate_docker_compose(
                        deployment_config,
                        environment,
                        environment_state,
                        docker_compose_content,
                    )
                )

                print(
                    f"Dockerfile validation {'succeeded' if is_successful else 'failed'} "
                    f"with reason: \n {reason}."
                )

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
        changes = {
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
        print(f"\n[bold green]Deployment state saved to {env_state_path}[/bold green]")

    @staticmethod
    def _prompt_for_build_env_vars(
        service: ServiceInfo, existing_vars: Optional[dict] = None
    ) -> dict:
        """Prompt user for build environment variables for services."""
        service_vars = existing_vars.copy() if existing_vars else {}
        if not service.env_vars:
            return service_vars

        print(
            "\n[bold]Configuring build-time environment variables for service"
            f" `{service.name_slug}`:[/bold]"
        )
        for env_var in service.env_vars:
            default_val = service_vars.get(env_var.key, env_var.default_value)

            if env_var.is_secret:
                question = inquirer.Password(
                    env_var.key,
                    message=f"  Enter value for secret '{env_var.key}'",
                    default=default_val,
                )
            else:
                question = inquirer.Text(
                    env_var.key,
                    message=f"  Enter value for '{env_var.key}'",
                    default=default_val,
                )

            answers = inquirer.prompt([question])
            if answers and answers.get(env_var.key) is not None:
                service_vars[env_var.key] = answers[env_var.key]

        return service_vars

    def _build_and_upload_frontend_assets(
        self,
        service: ServiceInfo,
        cloud_provider: BaseCloudProvider,
        environment: DeploymentEnvironment,
        cdn_state: FrontendCDNState,
    ):
        """Builds frontend assets and uploads them to cloud storage."""
        print(
            f"\n[bold blue]Building and deploying assets for '{service.name_slug}'...[/bold blue]"
        )

        deploy_path = (
            self.deployments_path
            / "environments"
            / environment.name
            / "frontend_deploy"
            / service.name_slug
        )
        deploy_path.mkdir(parents=True, exist_ok=True)

        ansible_runner = AnsibleProvisioner(working_dir=deploy_path)
        provider_name = cloud_provider.name().lower()
        ansible_runner.copy_template("frontend_deploy", provider_name)
        extra_vars = {
            "build_cmd": service.build_cmd,
            "build_dir": service.build_dir,
            "build_path": service.build_path,
            "bucket_name": cdn_state.bucket_name,
            "project_root": self.src_dir,
            "build_env_vars": cdn_state.build_env_vars,
            "cdn_distribution_id": cdn_state.cdn_distribution_id,
            "cdn_url_map": cdn_state.cdn_url_map,
        }
        extra_vars.update(cloud_provider.provider_detail_dump)
        ansible_runner.run_playbook("main.yml", extra_vars=extra_vars, inventory="localhost")
        print(f"[bold green]Assets for '{service.name_slug}' deployed successfully.[/bold green]")

    def _create_frontend_bucket_cert(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
        service_info: ServiceInfo,
        domain_info: DomainInfo,
        cloud_provider: BaseCloudProvider,
    ):
        """Creates CDN part 1 (bucket and cert) and cloud storage for a frontend service."""
        print(
            "\n[bold blue]Creating CDN part 1 (bucket, cert) for service"
            f" '{service_info.name_slug}'  '({domain_info.domain_name})'...[/bold blue]"
        )
        infra_path = (
            self.deployments_path
            / "environments"
            / environment.name
            / "frontend_bucket_cert"
            / service_info.name_slug
        )
        infra_path.mkdir(parents=True, exist_ok=True)

        tf = TerraformProvisioner(working_dir=infra_path)
        tf.copy_template("frontend_bucket_cert", cloud_provider.name().lower())

        variables = {
            "app_name": deployment_config.app_name_slug,
            "domain_name": domain_info.domain_name,
        }

        env_vars = cloud_provider.provider_detail_dump
        tf.init_and_apply(variables, env_vars=env_vars)
        outputs = tf.get_output()

        dns_records_json = outputs.get("dns_records")
        if dns_records_json:
            self._confirm_dns_records(json.loads(dns_records_json))
            print("\n[bold blue]Waiting 15 seconds for DNS propagation...[/bold blue]")
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
        print(
            "\n[bold blue]Creating CDN part 2 (distribution) for service"
            f" '{service_info.name_slug}'  '({domain_info.domain_name})'...[/bold blue]"
        )
        infra_path = (
            self.deployments_path
            / "environments"
            / environment.name
            / "frontend_cdn"
            / service_info.name_slug
        )
        infra_path.mkdir(parents=True, exist_ok=True)

        tf = TerraformProvisioner(working_dir=infra_path)
        tf.copy_template("frontend_cdn", cloud_provider.name().lower())

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
            self._confirm_dns_records(json.loads(dns_records_json))

        return outputs

    def _select_virtual_machine_type(
        self,
        deployment_config: DeploymentConfig,
        cloud_provider: BaseCloudProvider,
    ) -> MachineType:
        """Selects a virtual machine type for a new deployment environment."""
        with WaitingSpinner(text="Fetching available instance types"):
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

        with WaitingSpinner(text="Waiting for LLM to select machine types"):
            response = self.agent.run_sync(
                prompt, output_type=MachineTypeList, deps=self.agent_deps
            )

        suggested_machine_types = response.output
        choices, recommended_instance = suggested_machine_types.as_options()

        if not choices:
            raise MonolithicDeploymentError("No suitable instance types found.")

        questions = [
            inquirer.List(
                "instance_type",
                message="Select an instance type for the new environment",
                choices=choices,
                default=recommended_instance,
            )
        ]
        answers = inquirer.prompt(questions)
        return answers["instance_type"]

    @staticmethod
    def _confirm_dns_records(
        dns_records: List[Dict[str, str]],
    ):
        """Confirms the DNS records for the created service/s."""
        print(
            "\n[bold blue]Please configure the following DNS records for your domain:[/bold blue]"
        )

        for record in dns_records:
            print("\n[cyan]----------------------------------------[/cyan]")
            if record.get("comment"):
                print(f"  [bold]Comment:[/bold] {record.get('comment')}")
            print(f"  [bold]Type:[/bold]    {record.get('type')} Record")
            print(f"  [bold]Name:[/bold]    {record.get('name')}")
            print(f"  [bold]Value:[/bold]   {record.get('value')}")
            print("[cyan]----------------------------------------[/cyan]")

        confirm_question = [
            inquirer.Confirm(
                "dns_configured",
                message=(
                    "Have you configured the DNS records as shown above? (This might take"
                    " a few minutes to propagate)"
                ),
                default=True,
            )
        ]
        answers = inquirer.prompt(confirm_question)
        if not answers or not answers.get("dns_configured"):
            print("[bold red]DNS configuration not confirmed. Aborting deployment.[/bold red]")
            raise MonolithicDeploymentError("User did not confirm DNS configuration.")

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
            build_env_vars=build_env_vars,
        )
        env_state.frontend_cdn.append(cdn_state)

        self._build_and_upload_frontend_assets(
            service,
            cloud_provider,
            environment,
            cdn_state,
        )
        print(
            "\n[bold green]Your website is available at:"
            f" https://{domain_info.domain_name}[/bold green]"
        )

    def deploy(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
    ):
        """
        Creates a monolithic deployment environment using the provided deployment configuration and
        environment details. This function includes steps for setting up a container registry,
        building and pushing images, estimating resource requirements, selecting cloud provider
        instance types, creating a virtual machine, and generating Docker Compose configurations
        for deployment.

        :param deployment_config: Configuration object containing details of services, infrastructure
            dependencies, and other deployment settings.
        :type deployment_config: DeploymentConfig
        :param environment: Deployment environment details, including region and other configurations.
        :type environment: DeploymentEnvironment
        :return: None
        """
        frontend_services = self._get_frontend_services(deployment_config)
        other_services = self._get_backend_services(deployment_config)

        cloud_provider = environment.cloud_provider_instance
        env_state_path = self._get_env_state_path(environment.name)

        env_state = MonolithicDeploymentState()
        if frontend_services:
            print("\n[bold blue]Deploying frontend services...[/bold blue]")
            domains_map = {d.service_name_slug: d for d in environment.domains}

            for service in frontend_services:
                domain_info = domains_map.get(service.name_slug)
                if not domain_info:
                    print(
                        f"[bold red]No domain configured for frontend service {service.name_slug}."
                        " Skipping.[/bold red]"
                    )
                    continue
                self._deploy_frontend_service(
                    deployment_config, environment, domain_info, cloud_provider, service, env_state
                )

        if other_services:
            original_services = deployment_config.services
            deployment_config.services = other_services

            print(
                "\n[bold blue]Setting up container registry for region"
                f" '{environment.cloud_provider_detail.region}'... \n[/bold blue]"
            )
            registry_url = self._setup_container_registry(deployment_config, environment)
            images = self._build_and_push_images(deployment_config, environment, registry_url)

            print(f"\n[bold blue]Selecting instance type on {cloud_provider.name()}...[/bold blue]")
            selected_machine_type = self._select_virtual_machine_type(
                deployment_config, cloud_provider
            )
            instance_type = selected_machine_type.name
            instance_arch = selected_machine_type.architecture
            print(
                f"[bold green]Selected instance type: {instance_type} ({instance_arch.value})[/bold"
                " green]"
            )

            print(
                "\n[bold blue]Creating new virtual machine for monolithic deployment...[/bold blue]"
            )
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
            self._confirm_dns_records(dns_records)

            env_state.registry_url = registry_url
            env_state.virtual_machine = virtual_machine_state
            self._generate_docker_compose(deployment_config, environment, images, env_state)

            for domain in environment.get_domains_for_services(other_services):
                print(
                    "\n[bold green]Your website is available at:"
                    f" https://{domain.domain_name}[/bold green]"
                )

        # Save config snapshots for change detection
        self._save_deployment_state(env_state, deployment_config, env_state_path)

    def release(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
    ):
        """Deploys the application."""
        env_state_path = self._get_env_state_path(environment.name)
        env_state = MonolithicDeploymentState.load(env_state_path)
        cloud_provider = environment.cloud_provider_instance
        state_updated = False

        # Release frontend services
        frontend_services = self._get_frontend_services(deployment_config)
        if frontend_services:
            print("\n[bold blue]Releasing frontend services...[/bold blue]")
            cdn_state_map = {cdn.service_name_slug: cdn for cdn in env_state.frontend_cdn}

            for service in frontend_services:
                cdn_state = cdn_state_map.get(service.name_slug)
                if not cdn_state:
                    print(
                        "[bold yellow]No existing CDN state found for frontend service"
                        f" '{service.name_slug}'. Skipping release.[/bold yellow]"
                    )
                    continue

                build_env_vars = self._prompt_for_build_env_vars(
                    service, existing_vars=cdn_state.build_env_vars
                )
                if cdn_state.build_env_vars != build_env_vars:
                    cdn_state.build_env_vars = build_env_vars
                    state_updated = True

                self._build_and_upload_frontend_assets(
                    service,
                    cloud_provider,
                    environment,
                    cdn_state,
                )
                print(
                    "\n[bold green]Your website is available at:"
                    f" https://{cdn_state.domain_name}[/bold green]"
                )

        # Release other services
        other_services = self._get_backend_services(deployment_config)

        if other_services:
            if env_state.virtual_machine:
                self._build_and_push_images(deployment_config, environment, env_state.registry_url)

                env_file_path = f"/home/{env_state.virtual_machine.user}/app/.env"
                fetched_files = self._fetch_remote_deployment_files(
                    deployment_config,
                    environment,
                    env_state.virtual_machine,
                    [env_file_path],
                )

                self._deploy_docker_compose(
                    deployment_config,
                    environment,
                    env_state,
                    fetched_files[0],
                )
            else:
                # This can happen if only frontend was deployed
                print(
                    "[bold yellow]No virtual machine provisioned for this environment. Skipping"
                    " release of other services.[/bold yellow]"
                )

        if state_updated:
            env_state.save(env_state_path)

    def destroy(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
    ):
        """Destroys the environment's infrastructure."""
        print("\n[bold blue]Destroying monolithic environment...[/bold blue]")
        cloud_provider = environment.cloud_provider_instance

        env_state_path = self._get_env_state_path(environment.name)
        if not env_state_path.exists():
            print(
                f"[bold yellow]No state file found for environment '{environment.name}'. Skipping"
                " infrastructure destruction.[/bold yellow]"
            )
            return

        env_state = MonolithicDeploymentState.load(env_state_path)

        # Destroy frontend CDNs
        for cdn_state in env_state.frontend_cdn:
            print(
                "\n[bold blue]Destroying content delivery network for service"
                f" '{cdn_state.service_name_slug}'...[/bold blue]"
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
                tf_p2 = TerraformProvisioner(working_dir=infra_path_p2)
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
                tf_p1 = TerraformProvisioner(working_dir=infra_path_p1)
                variables_p1 = {
                    "app_name": deployment_config.app_name_slug,
                    "domain_name": cdn_state.domain_name,
                }
                env_vars = cloud_provider.provider_detail_dump
                tf_p1.destroy(variables_p1, env_vars=env_vars)
            else:
                print(
                    "[bold yellow]No content delivery network infrastructure found for"
                    f" service '{cdn_state.service_name_slug}'. Skipping destruction.[/bold"
                    " yellow]"
                )

        # Destroy virtual machine
        if env_state.virtual_machine:
            infra_path = (
                self.deployments_path / "environments" / environment.name / "virtual_machine"
            )
            if infra_path.exists():
                tf = TerraformProvisioner(working_dir=infra_path)

                variables = {
                    "app_name": deployment_config.app_name_slug,
                    "environment": environment.name,
                    "instance_type": env_state.virtual_machine.instance_type,
                    "instance_arch": env_state.virtual_machine.architecture.value,
                    "ssh_pub_key": self._get_ssh_public_key(),
                }
                env_vars = cloud_provider.provider_detail.model_dump(mode="json")
                tf.destroy(variables, env_vars=env_vars)
            else:
                print(
                    "[bold yellow]No virtual machine infrastructure found for environment"
                    f" '{environment.name}'. Skipping VM destruction.[/bold yellow]"
                )

        # Clean up environment directory
        env_dir_path = self.deployments_path / "environments" / environment.name
        if env_dir_path.exists():
            try:
                shutil.rmtree(env_dir_path)
                print(f"[bold green]Environment directory '{env_dir_path}' deleted.[/bold green]")
            except OSError as e:
                print(
                    f"[bold red]Error deleting environment directory {env_dir_path}: {e}[/bold red]"
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
            print(
                "\n[bold blue]Last environment in region"
                f" '{environment.cloud_provider_detail.region}'. Destroying container"
                " registry...[/bold blue]"
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
                tf = TerraformProvisioner(working_dir=registry_infra_path)
                variables = {
                    "app_name": app_name,
                    "registry_name": registry_name,
                    "force_delete": "true",
                }
                env_vars = cloud_provider.provider_detail_dump
                tf.destroy(variables, env_vars=env_vars)
                print("[bold green]Container registry destroyed successfully.[/bold green]")
                try:
                    shutil.rmtree(registry_infra_path.parent)
                    print(
                        f"[bold green]Global region directory '{registry_infra_path.parent}'"
                        " deleted.[/bold green]"
                    )
                except OSError as e:
                    print(
                        "[bold red]Error deleting global region directory"
                        f" {registry_infra_path.parent}: {e}[/bold red]"
                    )
            else:
                print(
                    "[bold yellow]Container registry infrastructure path not found. Skipping"
                    " destruction.[/bold yellow]"
                )

        # Clean up the deployment config
        deployment_config.environments = remaining_environments
        deployment_config.save(self.deployments_path)

    def run(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
        service_name_slug: str,
        command: str,
    ):
        """Runs a command on a specific service."""
        print(f"\n[bold blue]Running command on '{service_name_slug}': {command}[/bold blue]")
        env_state_path = self._get_env_state_path(environment.name)
        env_state = MonolithicDeploymentState.load(env_state_path)

        if not env_state.virtual_machine:
            raise MonolithicDeploymentError(
                "Virtual machine is not provisioned for this environment."
            )

        ansible_user = env_state.virtual_machine.user

        run_command_path = (
            self.deployments_path / "environments" / environment.name / "docker_compose_run"
        )
        ansible_runner = AnsibleProvisioner(working_dir=run_command_path)
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
        ansible_runner.run_playbook(
            "main.yml",
            extra_vars=extra_vars,
        )

    def update(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
    ):
        """
        Updates service configuration for an existing deployment.

        Handles:
        - Service additions/removals
        - Infrastructure dependency changes
        - Port/configuration changes
        """
        print("\n[bold blue]Starting configuration update...[/bold blue]")

        # Load existing state
        env_state_path = self._get_env_state_path(environment.name)
        if not env_state_path.exists():
            print(
                "[bold red]No deployment found for this environment. Please run 'deploy'"
                " first.[/bold red]"
            )
            raise MonolithicDeploymentError(
                f"No state file found at {env_state_path}. Run 'deploy' first."
            )

        env_state = MonolithicDeploymentState.load(env_state_path)

        # Detect changes
        has_changes, changes = self._detect_configuration_changes(deployment_config, env_state)

        if not has_changes:
            print("[bold green]No configuration changes detected. Nothing to update.[/bold green]")
            return

        # Display detected changes
        print("\n[bold]Configuration changes detected:[/bold]")
        if changes["services_added"]:
            print(f"  [green]Services added:[/green] {', '.join(changes['services_added'])}")
        if changes["services_removed"]:
            print(f"  [red]Services removed:[/red] {', '.join(changes['services_removed'])}")
        if changes["services_modified"]:
            print(
                f"  [yellow]Services modified:[/yellow] {', '.join(changes['services_modified'])}"
            )
        if changes["infra_added"]:
            print(f"  [green]Infrastructure added:[/green] {', '.join(changes['infra_added'])}")
        if changes["infra_removed"]:
            print(f"  [red]Infrastructure removed:[/red] {', '.join(changes['infra_removed'])}")
        if changes["infra_modified"]:
            print(
                "  [yellow]Infrastructure modified:[/yellow]"
                f" {', '.join(changes['infra_modified'])}"
            )

        # Warn about infrastructure changes
        infra_changes = (
            changes["infra_added"] or changes["infra_removed"] or changes["infra_modified"]
        )
        if infra_changes:
            print(
                "\n[bold yellow]WARNING: Infrastructure changes detected. Existing data in affected"
                " services may be lost.[/bold yellow]"
            )
            confirm_questions = [
                inquirer.Confirm(
                    "continue",
                    message="Do you want to continue with the update?",
                    default=False,
                )
            ]
            confirm_answers = inquirer.prompt(confirm_questions)
            if not confirm_answers or not confirm_answers.get("continue"):
                print("[bold yellow]Update cancelled by user.[/bold yellow]")
                return

        cloud_provider = environment.cloud_provider_instance

        # Handle frontend services
        frontend_services = self._get_frontend_services(deployment_config)
        if frontend_services:
            print("\n[bold blue]Updating frontend services...[/bold blue]")
            cdn_state_map = {cdn.service_name_slug: cdn for cdn in env_state.frontend_cdn}
            domains_map = {d.service_name_slug: d for d in environment.domains}

            for service in frontend_services:
                cdn_state = cdn_state_map.get(service.name_slug)
                if not cdn_state:
                    # New frontend service - create CDN
                    domain_info = domains_map.get(service.name_slug)
                    if not domain_info:
                        # This should not happen if main.py did its job
                        print(
                            f"[bold yellow]No domain configured for '{service.name_slug}'. "
                            "Skipping CDN creation.[/bold yellow]"
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
                    if cdn_state.build_env_vars != build_env_vars:
                        cdn_state.build_env_vars = build_env_vars

                    self._build_and_upload_frontend_assets(
                        service, cloud_provider, environment, cdn_state
                    )
                    print(
                        f"[bold green]Frontend service '{service.name_slug}' updated"
                        " successfully.[/bold green]"
                    )

        # Handle backend services
        other_services = [
            s for s in deployment_config.services if s.service_type != ServiceTypeEnum.FRONTEND
        ]

        if other_services:
            if not env_state.virtual_machine:
                print(
                    "[bold yellow]No virtual machine provisioned for this environment. Cannot"
                    " update backend services.[/bold yellow]"
                )
                return

            print("\n[bold blue]Updating backend services...[/bold blue]")

            # Rebuild and push images
            images = self._build_and_push_images(
                deployment_config, environment, env_state.registry_url
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

            # Regenerate docker-compose with existing env as starting point
            print("\n[bold blue]Regenerating docker-compose configuration...[/bold blue]")
            self._generate_docker_compose(
                deployment_config,
                environment,
                images,
                env_state,
                existing_env_content=existing_env_content,
            )

            print("[bold green]Backend services updated successfully.[/bold green]")

        # Update state with new config snapshots
        self._save_deployment_state(env_state, deployment_config, env_state_path)
