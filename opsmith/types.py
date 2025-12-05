from enum import Enum
from pathlib import Path
from typing import List, Optional, Type

import yaml
from pydantic import BaseModel, Field, model_validator
from rich import print

from opsmith.cloud_providers import CLOUD_PROVIDER_REGISTRY
from opsmith.cloud_providers.base import (
    BaseCloudProvider,
    BaseCloudProviderDetail,
    CpuArchitectureEnum,
)
from opsmith.exceptions import MonolithicDeploymentError
from opsmith.settings import settings


class ServiceTypeEnum(str, Enum):
    """Enum for the different types of services that can be deployed."""

    BACKEND_API = "BACKEND_API"
    FRONTEND = "FRONTEND"
    FULL_STACK = "FULL_STACK"
    BACKEND_WORKER = "BACKEND_WORKER"


class DependencyTypeEnum(str, Enum):
    """Enum for the different types of infrastructure dependencies."""

    DATABASE = "DATABASE"
    CACHE = "CACHE"
    MESSAGE_QUEUE = "MESSAGE_QUEUE"
    SEARCH_ENGINE = "SEARCH_ENGINE"


class InfrastructureProviderEnum(str, Enum):
    """Enum for the different types of infrastructure providers."""

    POSTGRESQL = "postgresql"
    MYSQL = "mysql"
    MONGODB = "mongodb"
    REDIS = "redis"
    RABBITMQ = "rabbitmq"
    KAFKA = "kafka"
    ELASTICSEARCH = "elasticsearch"
    WEAVIATE = "weaviate"
    USER_CHOICE = "user_choice"


COMPATIBLE_PROVIDERS = {
    DependencyTypeEnum.DATABASE: [
        InfrastructureProviderEnum.POSTGRESQL,
        InfrastructureProviderEnum.MYSQL,
        InfrastructureProviderEnum.MONGODB,
    ],
    DependencyTypeEnum.CACHE: [InfrastructureProviderEnum.REDIS],
    DependencyTypeEnum.MESSAGE_QUEUE: [
        InfrastructureProviderEnum.RABBITMQ,
        InfrastructureProviderEnum.KAFKA,
        InfrastructureProviderEnum.REDIS,
    ],
    DependencyTypeEnum.SEARCH_ENGINE: [
        InfrastructureProviderEnum.ELASTICSEARCH,
        InfrastructureProviderEnum.WEAVIATE,
    ],
}


class InfrastructureDependency(BaseModel):
    """Describes an infrastructure dependency for a service."""

    dependency_type: DependencyTypeEnum = Field(
        ..., description="The type of the infrastructure dependency."
    )
    provider: InfrastructureProviderEnum = Field(
        ...,
        description="The specific provider of the dependency.",
    )
    version: str = Field(
        "latest", description="The version of the infrastructure dependency, if identifiable."
    )


class EnvVarConfig(BaseModel):
    """Describes an environment variable configuration for a service."""

    key: str = Field(..., description="The name of the environment variable.")
    is_secret: bool = Field(
        ..., description="Whether the environment variable should be treated as a secret."
    )
    default_value: Optional[str] = Field(
        None,
        description="The default value of the environment variable, if present in the code.",
    )


class ServiceInfo(BaseModel):
    """Describes a single service to be deployed."""

    name_slug: str = Field(
        "slug", description="The unique slug for the service, e.g. python_backend_api_1"
    )
    language: str = Field(..., description="The primary programming language of the service.")
    language_version: Optional[str] = Field(
        None, description="The specific version of the language, if identifiable."
    )
    service_type: ServiceTypeEnum = Field(..., description="The type of the service.")
    framework: Optional[str] = Field(
        None, description="The primary framework or library used, if any."
    )
    service_port: Optional[int] = Field(
        None, description="The port the service listens on, if applicable."
    )
    build_cmd: Optional[str] = Field(
        None,
        description=(
            "The command to build the service, if applicable (e.g., 'npm run build'). This is"
            " required for FRONTEND services."
        ),
    )
    build_dir: Optional[str] = Field(
        None,
        description=(
            "The directory where build artifacts are located, relative to the repository root"
            " (e.g., 'frontend/dist'). This is required for FRONTEND services."
        ),
    )
    build_path: Optional[str] = Field(
        None,
        description=(
            "The path to be added to the PATH environment variable so that dependencies for the"
            " build are available (e.g., 'node_modules/.bin')."
        ),
    )
    env_vars: List[EnvVarConfig] = Field(
        default_factory=list,
        description="A list of environment variable configurations required by the service.",
    )

    @model_validator(mode="after")
    def check_frontend_build_fields(self) -> "ServiceInfo":
        """Validate that build_cmd and build_dir are present for FRONTEND services."""
        if self.service_type == ServiceTypeEnum.FRONTEND:
            if not self.build_cmd:
                raise ValueError("'build_cmd' is required for FRONTEND service type")
            if not self.build_dir:
                raise ValueError("'build_dir' is required for FRONTEND service type")
        return self


class ServiceList(BaseModel):
    """List of services discovered within the repository."""

    services: List[ServiceInfo] = Field(
        default_factory=list, description="A list of services identified in the repository."
    )
    infra_deps: List[InfrastructureDependency] = Field(
        default_factory=list,
        description="A list of consolidated infrastructure dependencies required by all services.",
    )


class DomainInfo(BaseModel):
    """Describes a domain configuration for a service."""

    service_name_slug: str = Field(..., description="The slug of the service this domain is for.")
    domain_name: str = Field(..., description="The domain name for the service.")


class DeploymentEnvironment(BaseModel):
    """Describes a deployment environment."""

    name: str = Field(
        ..., description="The name of the environment (e.g., 'staging', 'production')."
    )
    cloud_provider: dict = Field(..., description="Cloud provider specific details.")
    strategy: str = Field(..., description="The deployment strategy for this environment.")
    domain_email: Optional[str] = Field(
        None,
        description="The email for SSL certificate registration with services like Let's Encrypt.",
    )
    domains: List[DomainInfo] = Field(
        default_factory=list, description="A list of domain configurations for services."
    )

    def get_domains_for_services(self, services: List[ServiceInfo]) -> List[DomainInfo]:
        """Retrieves a list of domains for the given services."""
        domains = []
        domains_by_service_name_slug = {d.service_name_slug: d for d in self.domains}
        for service in services:
            if service.name_slug in domains_by_service_name_slug:
                domains.append(domains_by_service_name_slug[service.name_slug])
        return domains

    @property
    def cloud_provider_instance(self) -> BaseCloudProvider:
        """Retrieves a cloud provider instance for the environment."""
        provider_cls = CLOUD_PROVIDER_REGISTRY.get_provider_class(self.cloud_provider.get("name"))
        return provider_cls(self.cloud_provider)

    @property
    def cloud_provider_detail(self) -> BaseCloudProviderDetail:
        """Retrieves the cloud provider details for the environment."""
        return self.cloud_provider_instance.provider_detail


class DeploymentConfig(ServiceList):
    """Describes the deployment config for the repository, listing all services."""

    app_name: str = Field(..., description="The name of the application.")
    app_name_slug: str = Field(..., description="The slugified name of the application.")
    environments: List[DeploymentEnvironment] = Field(
        default_factory=list, description="A list of deployment environments."
    )

    @property
    def environment_names(self) -> List[str]:
        return [env.name for env in self.environments]

    def get_environment(self, name: str) -> DeploymentEnvironment:
        """Retrieves an environment by name."""
        for env in self.environments:
            if env.name == name:
                return env
        raise ValueError(f"Environment '{name}' not found in the deployment configuration.")

    def get_env_var_defaults(self) -> dict:
        """Retrieves a dictionary of environment variable defaults."""
        env_var_defaults = {}
        for service in self.services:
            for env_var in service.env_vars:
                if env_var.default_value:
                    env_var_defaults[env_var.key] = env_var.default_value
        return env_var_defaults

    @classmethod
    def load(cls: Type["DeploymentConfig"], deployments_path: Path) -> Optional["DeploymentConfig"]:
        """Loads the deployment configuration from a YAML file."""
        config_file_path = deployments_path / settings.config_filename

        if not config_file_path.exists():
            return None

        with open(config_file_path, "r") as f:
            config_data = yaml.safe_load(f)

        if config_data:
            return cls(**config_data)
        else:
            return None

    def save(self, deployments_path: Path):
        """Saves the deployment configuration to a YAML file."""
        config_file_path = deployments_path / settings.config_filename
        deployments_path.mkdir(parents=True, exist_ok=True)
        with open(config_file_path, "w") as f:
            yaml.dump(self.model_dump(mode="json"), f, indent=2)
        print(f"\n[bold blue]Deployment configuration saved to: {config_file_path}[/bold blue]")


class VirtualMachineState(BaseModel):
    """Describes the configuration of a virtual machine for a monolithic deployment."""

    cpu: int = Field(..., description="The number of virtual CPU cores for the machine.")
    ram_gb: float = Field(..., description="The amount of RAM in gigabytes for the machine.")
    instance_type: str = Field(..., description="The instance type of the virtual machine.")
    architecture: CpuArchitectureEnum = Field(
        ..., description="The CPU architecture of the virtual machine."
    )
    public_ip: str = Field(..., description="The public IP address of the virtual machine.")
    private_ip: Optional[str] = Field(
        ..., description="The private IP address of the virtual machine."
    )
    user: str = Field(..., description="The SSH user for the virtual machine.")
    instance_id: str = Field(..., description="The ID of the virtual machine instance.")


class FrontendCDNState(BaseModel):
    """State for a frontend service deployment."""

    service_name_slug: str = Field(..., description="The slug of the service.")
    domain_name: str = Field(..., description="The domain name for the service.")
    bucket_name: str = Field(..., description="The name of the cloud storage bucket.")
    cdn_domain_name: Optional[str] = Field(None, description="The domain name of the CDN.")
    cdn_ip_address: Optional[str] = Field(None, description="The IP address of the CDN.")
    cdn_distribution_id: Optional[str] = Field(
        None, description="The ID of the AWS CloudFront distribution."
    )
    cdn_url_map: Optional[str] = Field(None, description="The name of the GCP URL map.")
    certificate_id: Optional[str] = Field(None, description="The ID or ARN of the SSL certificate.")
    build_env_vars: dict = Field(
        default_factory=dict,
        description="Build-time environment variables for the service, keyed by service slug.",
    )


class MonolithicDeploymentState(BaseModel):
    """State for a monolithic deployment environment."""

    registry_url: Optional[str] = Field(None, description="The URL of the container registry.")
    virtual_machine: Optional[VirtualMachineState] = Field(
        None, description="The state of the virtual machine."
    )
    frontend_cdn: List[FrontendCDNState] = Field(
        default_factory=list, description="State of deployed frontend services."
    )
    deployed_services: Optional[List[dict]] = Field(
        default_factory=list, description="Snapshot of services deployed in docker-compose."
    )
    deployed_infra_deps: Optional[List[dict]] = Field(
        default_factory=list, description="Snapshot of infrastructure dependencies deployed."
    )

    @classmethod
    def load(cls: Type["MonolithicDeploymentState"], path: Path) -> "MonolithicDeploymentState":
        """Loads the monolithic deployment state from a YAML file."""
        if not path.exists():
            raise MonolithicDeploymentError(f"State file '{path}' does not exist.")

        with open(path, "r", encoding="utf-8") as f:
            config_data = yaml.safe_load(f)

        return cls(**config_data)

    def save(self, path: Path):
        """Saves the monolithic deployment configuration to a YAML file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(self.model_dump(mode="json", exclude_none=True), f, indent=2)
