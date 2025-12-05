import abc
import base64
import json
import os
import platform
import subprocess
import time
from importlib.metadata import entry_points
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Type

from pydantic_ai import Agent
from rich import print

from opsmith.agent import AgentDeps
from opsmith.cloud_providers.base import BaseCloudProvider, MachineType
from opsmith.infra_provisioners.ansible_provisioner import AnsibleProvisioner
from opsmith.infra_provisioners.terraform_provisioner import TerraformProvisioner
from opsmith.settings import settings
from opsmith.types import (
    DeploymentConfig,
    DeploymentEnvironment,
    ServiceTypeEnum,
    VirtualMachineState,
)
from opsmith.utils import slugify


class DeploymentStrategyRegistry:
    """A singleton registry for deployment strategies."""

    _instance: Optional["DeploymentStrategyRegistry"] = None
    _strategies: Dict[str, Type["BaseDeploymentStrategy"]]

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._strategies = {}
            cls._instance._load_builtin_strategies()
            cls._instance._load_plugin_strategies()
        return cls._instance

    def _load_builtin_strategies(self):
        """Load built-in strategies"""
        from opsmith.deployment_strategies.monolithic import (
            MonolithicDeploymentStrategy,
        )

        for strategy_cls in [MonolithicDeploymentStrategy]:
            self.register(strategy_cls)

    def _load_plugin_strategies(self):
        """Load strategies from installed packages via entry points"""
        discovered_entry_points = entry_points(group="opsmith.deployment_strategies")
        for entry_point in discovered_entry_points:
            try:
                strategy_cls = entry_point.load()
                self.register(strategy_cls)
                print(f"Loaded deployment strategy: {strategy_cls.name()}")
            except Exception as e:
                print(
                    "[yellow]Warning: Failed to load deployment strategy from entry point"
                    f" '{entry_point.name}': {e}[/yellow]"
                )

    def register(self, strategy_class: Type["BaseDeploymentStrategy"]):
        """Registers a deployment strategy."""
        # Not raising error on overwrite allows for easy extension/replacement
        self._strategies[strategy_class.name()] = strategy_class

    def get_strategy_class(self, strategy_name: str) -> Type["BaseDeploymentStrategy"]:
        """Retrieves a strategy class from the registry."""
        if strategy_name not in self._strategies:
            raise ValueError(f"Strategy '{strategy_name}' not found.")
        return self._strategies[strategy_name]

    @property
    def choices(self) -> List[Tuple[str, str]]:
        """Returns a list of (display text, value) tuples for use in prompts."""
        choices_list = []
        for name, strategy_class in sorted(self._strategies.items()):
            display_text = f"{name} - {strategy_class.description()}"
            choices_list.append((display_text, name))
        return choices_list


class BaseDeploymentStrategy(abc.ABC):
    """Abstract base class for deployment strategies."""

    @classmethod
    @abc.abstractmethod
    def name(cls) -> str:
        """The name of the deployment strategy."""
        raise NotImplementedError

    @classmethod
    @abc.abstractmethod
    def description(cls) -> str:
        """A brief description of the deployment strategy."""
        raise NotImplementedError

    def __init__(self, agent: Agent, src_dir: Path):
        self.agent = agent
        self.agent_deps = AgentDeps(src_dir=src_dir)
        self.src_dir = src_dir
        self.deployments_path = src_dir.joinpath(settings.deployments_dir)
        self.templates_dir = Path(__file__).parent.parent / "templates"

    def _get_env_state_path(self, environment_name: str) -> Path:
        return self.deployments_path / "environments" / environment_name / "state.yml"

    def _setup_container_registry(
        self, deployment_config: DeploymentConfig, environment: DeploymentEnvironment
    ) -> str:
        """Sets up a container registry for the given region."""
        app_name = deployment_config.app_name_slug
        # Registry name should probably be unique per region for the app
        registry_name = slugify(f"{app_name}-{environment.cloud_provider_detail.region}")

        cloud_provider_instance = environment.cloud_provider_instance
        provider_name = cloud_provider_instance.name()

        registry_infra_path = (
            self.deployments_path
            / "environments"
            / "global"
            / f"{cloud_provider_instance.name()}-{environment.cloud_provider_detail.region}"
            / "container_registry"
        )
        tf = TerraformProvisioner(working_dir=registry_infra_path)

        variables = {
            "app_name": app_name,
            "registry_name": registry_name,
            "force_delete": "true",
        }
        env_vars = cloud_provider_instance.provider_detail.model_dump(mode="json")

        try:
            if not any(registry_infra_path.iterdir()):
                tf.copy_template("container_registry", provider_name)

            tf.init_and_apply(variables, env_vars=env_vars)

            outputs = tf.get_output()
            registry_url = outputs.get("registry_url")

            if registry_url:
                print(f"\n[bold green]Container registry created. URL: {registry_url}[/bold green]")
                return registry_url
            else:
                print(
                    "[bold red]Could not find 'registry_url' in TerraformProvisioner outputs.[/bold"
                    " red]"
                )
                raise ValueError("Could not find 'registry_url' in TerraformProvisioner outputs.")

        except (FileNotFoundError, subprocess.CalledProcessError) as e:
            print(f"[bold red]Failed to set up container registry: {e}[/bold red]")
            raise

    def _build_and_push_images(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
        registry_url: str,
    ) -> Dict[str, str]:
        """Builds and pushes Docker images for each service."""
        print("\n[bold blue]Build and push container images to the registry[/bold blue]")

        images = {}

        buildable_service_types = [
            ServiceTypeEnum.BACKEND_API,
            ServiceTypeEnum.FULL_STACK,
            ServiceTypeEnum.BACKEND_WORKER,
        ]

        for service in deployment_config.services:
            if service.service_type not in buildable_service_types:
                continue

            # This logic is from generate_dockerfiles
            service_dir_slug = service.name_slug
            dockerfile_path_abs = self.deployments_path / "docker" / service_dir_slug / "Dockerfile"

            if not dockerfile_path_abs.exists():
                print(
                    f"[bold yellow]Dockerfile for {service_dir_slug} not found at"
                    f" {dockerfile_path_abs}, skipping build.[/bold yellow]"
                )
                continue

            image_name_slug = service_dir_slug

            print(f"\n[bold]Building and pushing image for {image_name_slug}...[/bold]")

            build_infra_path = (
                self.deployments_path
                / "environments"
                / environment.name
                / "docker_build_push"
                / image_name_slug
            )

            ansible_runner = AnsibleProvisioner(working_dir=build_infra_path)

            provider_name = environment.cloud_provider_detail.name.lower()

            extra_vars = {
                "docker_path": str(self.agent_deps.src_dir),
                "dockerfile_path": str(dockerfile_path_abs),
                "image_name_slug": image_name_slug,
                "image_tag_name": "latest",
                "registry_url": registry_url,
            }
            extra_vars.update(environment.cloud_provider_instance.provider_detail_dump)
            ansible_runner.copy_template("docker_build_push", provider_name)
            outputs = ansible_runner.run_playbook("main.yml", extra_vars)

            print(f"[green]Successfully built and pushed image for {image_name_slug}[/green]")

            if "image_url" in outputs:
                images[image_name_slug] = outputs["image_url"]
            else:
                print(f"[bold red]Could not determine image URL for {image_name_slug}[/bold red]")

        return images

    @staticmethod
    def _get_ssh_public_key() -> str:
        """
        Checks for the existence of a local SSH public key file on the system and returns its
        contents if found. It searches through platform-specific commonly used directories
        and file names for SSH public keys and verifies their existence. If no public key
        is found, an error is raised guiding the user to generate one.

        :raises FileNotFoundError: If no SSH public key is found in the specified directories
            or with the expected names.
        :return: The content of the found SSH public key as a string.
        :rtype: str
        """
        print("\n[bold blue]Looking up the local SSH public key[/bold blue]")
        system = platform.system().lower()

        # Common SSH key names
        key_names = [
            "id_rsa",
            "id_dsa",
            "id_ecdsa",
            "id_ed25519",
            "id_rsa_github",
            "id_rsa_gitlab",
            "id_rsa_bitbucket",
            "github_rsa",
            "gitlab_rsa",
            "bitbucket_rsa",
        ]

        # Platform-specific SSH directory paths
        ssh_dirs = []

        if system in ["linux", "darwin"]:  # Linux and macOS
            home = Path.home()
            ssh_dirs.append(home / ".ssh")

            # Additional common locations on Unix-like systems
            if system == "linux":
                ssh_dirs.extend([Path("/etc/ssh"), Path("/usr/local/etc/ssh")])

        elif system == "windows":
            # Windows SSH key locations
            home = Path.home()
            ssh_dirs.extend(
                [
                    home / ".ssh",
                    home / "Documents" / ".ssh",
                    Path(os.environ.get("USERPROFILE", "")) / ".ssh",
                    Path("C:/ProgramData/ssh"),
                    Path("C:/Users") / os.environ.get("USERNAME", "") / ".ssh",
                ]
            )

            # OpenSSH for Windows locations
            if "PROGRAMFILES" in os.environ:
                ssh_dirs.append(Path(os.environ["PROGRAMFILES"]) / "OpenSSH")

        # Search in SSH directories
        for ssh_dir in ssh_dirs:
            if not ssh_dir.exists() or not ssh_dir.is_dir():
                continue
            # Look for specific key names with .pub extension
            for key_name in key_names:
                key_path = ssh_dir / f"{key_name}.pub"
                if key_path.exists() and key_path.is_file():
                    with open(key_path, "r", encoding="utf-8") as f:
                        print(f"[bold green]SSH public key found at {key_path}.[/bold green]")
                        return f.read().strip()

        print(
            "[bold red]SSH public key not found. Please generate one using 'ssh-keygen'.[/bold red]"
        )
        raise FileNotFoundError("SSH public key not found.")

    def _create_virtual_machine(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
        machine_type: MachineType,
        cloud_provider: BaseCloudProvider,
    ) -> VirtualMachineState:
        """Creates a new virtual machine for the deployment."""
        provider_name = cloud_provider.name().lower()
        infra_path = self.deployments_path / "environments" / environment.name / "virtual_machine"
        tf = TerraformProvisioner(working_dir=infra_path)

        variables = {
            "app_name": deployment_config.app_name_slug,
            "environment": environment.name,
            "instance_type": machine_type.name,
            "instance_arch": machine_type.architecture.value,
            "ssh_pub_key": self._get_ssh_public_key(),
        }
        env_vars = cloud_provider.provider_detail_dump

        tf.copy_template("virtual_machine", provider_name)

        tf.init_and_apply(variables, env_vars=env_vars)

        outputs = tf.get_output()
        print("[bold green]Monolithic infrastructure provisioned successfully.[/bold green]")

        virtual_machine_state = VirtualMachineState(
            ram_gb=machine_type.ram_gb,
            cpu=machine_type.cpu,
            instance_type=machine_type.name,
            architecture=machine_type.architecture,
            **outputs,
        )

        print("[bold blue]Waiting 15 seconds for instance to become ready...[/bold blue]")
        time.sleep(15)

        print("\n[bold blue]Setting up Docker on the newly created VM[/bold blue]")

        setup_docker_path = (
            self.deployments_path / "environments" / environment.name / "virtual_machine_setup"
        )
        ansible_runner = AnsibleProvisioner(working_dir=setup_docker_path)
        ansible_runner.copy_template("virtual_machine_setup", provider_name)

        extra_vars = {
            "environment_name": environment.name,
            "app_name": deployment_config.app_name_slug,
            **cloud_provider.provider_detail_dump,
            **virtual_machine_state.model_dump(mode="json"),
        }
        # extra_vars.update(virtual_machine_state.model_dump(mode="json"))
        # extra_vars.update(cloud_provider.provider_detail_dump)

        ansible_runner.run_playbook(
            "main.yml",
            extra_vars=extra_vars,
        )
        print("[bold green]Docker setup complete.[/bold green]")

        return virtual_machine_state

    def _fetch_remote_deployment_files(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
        virtual_machine: VirtualMachineState,
        remote_files: List[str],
    ) -> List[str]:
        print("\n[bold blue]Fetching current deployment files from server...[/bold blue]")
        fetch_files_path = (
            self.deployments_path / "environments" / environment.name / "fetch_remote_files"
        )

        ansible_runner = AnsibleProvisioner(working_dir=fetch_files_path)
        ansible_runner.copy_template(
            "fetch_remote_files", environment.cloud_provider_instance.name()
        )
        extra_vars = {
            "app_name": deployment_config.app_name_slug,
            "environment_name": environment.name,
            "remote_files": remote_files,
            **virtual_machine.model_dump(mode="json"),
            **environment.cloud_provider_instance.provider_detail_dump,
        }

        outputs = ansible_runner.run_playbook(
            "main.yml",
            extra_vars=extra_vars,
        )

        fetched_files_b64 = outputs.get("fetched_files", "")
        if not fetched_files_b64:
            print(
                "[bold red]Could not fetch existing deployment files from server. Please run"
                " `opsmith setup` again on this environment.[/bold red]"
            )
            raise ValueError("Failed to fetch deployment files.")

        fetched_files_json = base64.b64decode(fetched_files_b64.encode("ascii")).decode("utf-8")
        fetched_files = json.loads(fetched_files_json)

        return [
            base64.b64decode(file_content.encode("ascii")).decode("utf-8")
            for file_content in fetched_files
        ]

    def _cleanup_cloud_storage(
        self,
        environment: DeploymentEnvironment,
        service_name_slug: str,
        cloud_provider: BaseCloudProvider,
        bucket_name: str,
    ):
        print(f"\n[bold blue]Emptying bucket '{bucket_name}' before deletion...[/bold blue]")
        delete_bucket_path = (
            self.deployments_path
            / "environments"
            / environment.name
            / "cloud_storage_cleanup"
            / service_name_slug
        )
        delete_bucket_path.mkdir(parents=True, exist_ok=True)

        ansible_runner = AnsibleProvisioner(working_dir=delete_bucket_path)
        provider_name = cloud_provider.name().lower()
        ansible_runner.copy_template("cloud_storage_cleanup", provider_name)

        extra_vars = {
            "bucket_name": bucket_name,
        }
        extra_vars.update(environment.cloud_provider_instance.provider_detail_dump)
        ansible_runner.run_playbook("main.yml", extra_vars=extra_vars, inventory="localhost")
        print(f"[bold green]Bucket '{bucket_name}' emptied successfully.[/bold green]")

    @abc.abstractmethod
    def deploy(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
    ):
        """Sets up the infrastructure for the deployment."""
        raise NotImplementedError

    @abc.abstractmethod
    def release(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
    ):
        """Deploys the application."""
        raise NotImplementedError

    @abc.abstractmethod
    def destroy(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
    ):
        """Destroys the environment's infrastructure."""
        raise NotImplementedError

    @abc.abstractmethod
    def run(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
        service_name_slug: str,
        command: str,
    ):
        """Runs a command on a specific service."""
        raise NotImplementedError

    @abc.abstractmethod
    def update(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
    ):
        """Updates service configuration for an existing deployment."""
        raise NotImplementedError
