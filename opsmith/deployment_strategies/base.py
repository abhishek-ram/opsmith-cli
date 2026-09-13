import abc
import base64
import json
import os
import platform
import time
from importlib.metadata import entry_points
from pathlib import Path
from typing import Dict, List, Optional, Type

from opsmith.agent import AgentDeps
from opsmith.cloud_providers.base import BaseCloudProvider, MachineType
from opsmith.core.context import OpsmithContext
from opsmith.core.errors import AnsibleFailed, InvalidArgument, TerraformFailed
from opsmith.core.events import (
    STEP_BUILD,
    STEP_DESTROY,
    STEP_REGISTRY,
    STEP_VM,
    BufferingSink,
)
from opsmith.core.interaction import Choice
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

    #: Plugins load at import time, before the CLI has a renderer, so what happens during the
    #: load is buffered here and drained once there is somewhere to report it.
    pending_events: BufferingSink

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._strategies = {}
            cls._instance.pending_events = BufferingSink()
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
                self.pending_events.log(
                    STEP_REGISTRY, f"Loaded deployment strategy: {strategy_cls.name()}"
                )
            except Exception as e:
                self.pending_events.warning(
                    STEP_REGISTRY,
                    (
                        "Failed to load deployment strategy from entry point"
                        f" '{entry_point.name}': {e}"
                    ),
                    entry_point=entry_point.name,
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
    def choices(self) -> List[Choice]:
        """
        Describes the registered strategies as options a person can be asked to pick from.

        :return: One choice per strategy, by name.
        """
        return [
            Choice(label=f"{name} - {strategy_class.description()}", value=name)
            for name, strategy_class in sorted(self._strategies.items())
        ]


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

    def __init__(self, ctx: OpsmithContext):
        """
        :param ctx: The run's context. A strategy takes nothing else, so constructing one
            touches neither the filesystem nor the network.
        """
        self.ctx = ctx
        self.events = ctx.events
        self.provisioners = ctx.provisioner_factory
        self.interact = ctx.interact
        self.answers = ctx.answers
        self.steps = ctx.steps
        self.agent = ctx.agent
        self.agent_deps = AgentDeps(src_dir=ctx.src_dir)
        self.src_dir = ctx.src_dir
        self.deployments_path = ctx.deployments_path
        self.templates_dir = Path(__file__).parent.parent / "templates"

    @property
    def git_repo(self):
        """The repository being deployed, opened on first use by the context."""
        return self.ctx.git_repo

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

        # The directory name keeps the provider's own casing: it addresses terraform state that
        # already exists in a user's project, so it cannot be normalised after the fact.
        registry_infra_path = (
            self.deployments_path
            / "environments"
            / "global"
            / f"{cloud_provider_instance.name()}-{environment.cloud_provider_detail.region}"
            / "container_registry"
        )
        tf = self.provisioners.terraform(registry_infra_path, step=STEP_REGISTRY)

        variables = {
            "app_name": app_name,
            "registry_name": registry_name,
            "force_delete": "true",
        }
        env_vars = cloud_provider_instance.provider_detail.model_dump(mode="json")

        if not any(registry_infra_path.iterdir()):
            tf.copy_template("container_registry", provider_name)

        tf.init_and_apply(variables, env_vars=env_vars)

        outputs = tf.get_output()
        registry_url = outputs.get("registry_url")
        if not registry_url:
            raise TerraformFailed(
                "Could not find 'registry_url' in the container registry outputs.",
                hint="The container registry template did not declare the output opsmith needs.",
                details={"working_dir": str(registry_infra_path)},
            )

        self.events.log(STEP_REGISTRY, f"Container registry created. URL: {registry_url}")
        return registry_url

    def _build_and_push_images(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
        registry_url: str,
    ) -> Dict[str, str]:
        """Builds and pushes Docker images for each service."""
        self.events.step(STEP_BUILD, "Build and push container images to the registry")

        images = {}

        buildable_service_types = [
            ServiceTypeEnum.BACKEND_API,
            ServiceTypeEnum.FULL_STACK,
            ServiceTypeEnum.BACKEND_WORKER,
        ]

        with self.git_repo.git_archive_context() as clean_context_path:
            for service in deployment_config.services:
                if service.service_type not in buildable_service_types:
                    continue

                # This logic is from generate_dockerfiles
                service_dir_slug = service.name_slug
                dockerfile_path_abs = (
                    self.deployments_path / "docker" / service_dir_slug / "Dockerfile"
                )

                if not dockerfile_path_abs.exists():
                    self.events.warning(
                        STEP_BUILD,
                        (
                            f"Dockerfile for {service_dir_slug} not found at {dockerfile_path_abs},"
                            " skipping build."
                        ),
                    )
                    continue

                image_name_slug = service_dir_slug

                self.events.step(STEP_BUILD, f"Building and pushing image for {image_name_slug}...")

                build_infra_path = (
                    self.deployments_path
                    / "environments"
                    / environment.name
                    / "docker_build_push"
                    / image_name_slug
                )

                ansible_runner = self.provisioners.ansible(build_infra_path, step=STEP_BUILD)

                provider_name = environment.cloud_provider_detail.name

                extra_vars = {
                    "docker_path": str(clean_context_path),
                    "dockerfile_path": str(dockerfile_path_abs),
                    "image_name_slug": image_name_slug,
                    "image_tag_name": "latest",
                    "registry_url": registry_url,
                }
                extra_vars.update(environment.cloud_provider_instance.provider_detail_dump)
                ansible_runner.copy_template("docker_build_push", provider_name)
                outputs = ansible_runner.run_playbook("main.yml", extra_vars)

                self.events.log(
                    STEP_BUILD, f"Successfully built and pushed image for {image_name_slug}"
                )

                if "image_url" in outputs:
                    images[image_name_slug] = outputs["image_url"]
                else:
                    self.events.warning(
                        STEP_BUILD, f"Could not determine image URL for {image_name_slug}"
                    )

        return images

    def _get_ssh_public_key(self) -> str:
        """
        Checks for the existence of a local SSH public key file on the system and returns its
        contents if found. It searches through platform-specific commonly used directories
        and file names for SSH public keys and verifies their existence. If no public key
        is found, an error is raised guiding the user to generate one.

        :raises InvalidArgument: If no SSH public key is found in the specified directories
            or with the expected names.
        :return: The content of the found SSH public key as a string.
        :rtype: str
        """
        self.events.step(STEP_VM, "Looking up the local SSH public key")
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
                        self.events.log(STEP_VM, f"SSH public key found at {key_path}.")
                        return f.read().strip()

        raise InvalidArgument(
            "No SSH public key found on this machine.",
            hint="Generate one with 'ssh-keygen', then run the command again.",
            details={"searched": [str(directory) for directory in ssh_dirs]},
        )

    def _create_virtual_machine(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
        machine_type: MachineType,
        cloud_provider: BaseCloudProvider,
    ) -> VirtualMachineState:
        """Creates a new virtual machine for the deployment."""
        provider_name = cloud_provider.name()
        infra_path = self.deployments_path / "environments" / environment.name / "virtual_machine"
        tf = self.provisioners.terraform(infra_path, step=STEP_VM)

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
        self.events.log(STEP_VM, "Monolithic infrastructure provisioned successfully.")

        virtual_machine_state = VirtualMachineState(
            ram_gb=machine_type.ram_gb,
            cpu=machine_type.cpu,
            instance_type=machine_type.name,
            architecture=machine_type.architecture,
            **outputs,
        )

        with self.events.waiting(STEP_VM, "Waiting 15 seconds for instance to become ready..."):
            time.sleep(15)

        self.events.step(STEP_VM, "Setting up Docker on the newly created VM")

        setup_docker_path = (
            self.deployments_path / "environments" / environment.name / "virtual_machine_setup"
        )
        ansible_runner = self.provisioners.ansible(setup_docker_path, step=STEP_VM)
        ansible_runner.copy_template("virtual_machine_setup", provider_name)

        extra_vars = {
            "environment_name": environment.name,
            "app_name": deployment_config.app_name_slug,
            **cloud_provider.provider_detail_dump,
            **virtual_machine_state.model_dump(mode="json"),
        }

        ansible_runner.run_playbook(
            "main.yml",
            extra_vars=extra_vars,
        )
        self.events.log(STEP_VM, "Docker setup complete.")

        return virtual_machine_state

    def _fetch_remote_deployment_files(
        self,
        deployment_config: DeploymentConfig,
        environment: DeploymentEnvironment,
        virtual_machine: VirtualMachineState,
        remote_files: List[str],
    ) -> List[str]:
        self.events.step(STEP_VM, "Fetching current deployment files from server...")
        fetch_files_path = (
            self.deployments_path / "environments" / environment.name / "fetch_remote_files"
        )

        ansible_runner = self.provisioners.ansible(fetch_files_path, step=STEP_VM)
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
            raise AnsibleFailed(
                "Could not fetch the existing deployment files from the server.",
                hint="Run `opsmith deploy` again on this environment to recreate them.",
                details={"remote_files": remote_files},
            )

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
        self.events.step(STEP_DESTROY, f"Emptying bucket '{bucket_name}' before deletion...")
        delete_bucket_path = (
            self.deployments_path
            / "environments"
            / environment.name
            / "cloud_storage_cleanup"
            / service_name_slug
        )
        delete_bucket_path.mkdir(parents=True, exist_ok=True)

        ansible_runner = self.provisioners.ansible(delete_bucket_path, step=STEP_DESTROY)
        ansible_runner.copy_template("cloud_storage_cleanup", cloud_provider.name())

        extra_vars = {
            "bucket_name": bucket_name,
        }
        extra_vars.update(environment.cloud_provider_instance.provider_detail_dump)
        ansible_runner.run_playbook("main.yml", extra_vars=extra_vars, inventory="localhost")
        self.events.log(STEP_DESTROY, f"Bucket '{bucket_name}' emptied successfully.")

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
