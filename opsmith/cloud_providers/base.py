import abc
from enum import Enum
from importlib.metadata import entry_points
from typing import Dict, List, Optional, Tuple, Type

from pydantic import BaseModel, Field, TypeAdapter
from rich import print


class CpuArchitectureEnum(str, Enum):
    """Enum for CPU architectures."""

    ARM64 = "arm64"
    X86_64 = "x86_64"


class MachineType(BaseModel):
    """Describes a machine type."""

    name: str = Field(..., description="The name of the instance type.")
    cpu: int = Field(..., description="The number of virtual CPU cores.")
    ram_gb: float = Field(..., description="The amount of RAM in gigabytes.")
    architecture: CpuArchitectureEnum = Field(..., description="The CPU architecture.")
    is_recommended: bool = Field(
        False, description="Whether this is the recommended instance type."
    )


class MachineTypeList(BaseModel):
    """A list of machine types."""

    machines: List[MachineType]

    def as_options(self) -> Tuple[List[Tuple[str, "MachineType"]], Optional["MachineType"]]:
        """
        Formats the machine list into options for inquirer.
        It returns a list of choices for inquirer and the recommended machine.
        """
        choices = []
        recommended_machine_choice = None

        # sort machines by cpu and ram to have a consistent order for user
        sorted_machines = sorted(self.machines, key=lambda m: (m.cpu, m.ram_gb))

        for option in sorted_machines:
            choice_text = (
                f"{option.name} ({option.cpu} vCPUs, {option.ram_gb} GB RAM,"
                f" {option.architecture.value})"
            )
            if option.is_recommended:
                choice_text += " (Recommended)"
                recommended_machine_choice = option
            choices.append((choice_text, option))

        return choices, recommended_machine_choice


class BaseCloudProviderDetail(BaseModel):
    name: str = Field(..., description="Provider name")
    region: str = Field(..., description="The cloud provider region for this environment.")


class CloudProviderRegistry:
    """A singleton registry for cloud providers."""

    _instance: Optional["CloudProviderRegistry"] = None
    _providers: Dict[str, Type["BaseCloudProvider"]]

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._providers = {}
            cls._instance._load_builtin_providers()
            cls._instance._load_plugin_providers()
        return cls._instance

    def register(self, provider_class: Type["BaseCloudProvider"]):
        """Registers a cloud provider."""
        # Not raising error on overwrite allows for easy extension/replacement
        self._providers[provider_class.name()] = provider_class

    def get_provider_class(self, provider_name: str) -> Type["BaseCloudProvider"]:
        """Retrieves a provider class from the registry."""
        if provider_name not in self._providers:
            raise ValueError(f"Provider '{provider_name}' not found.")
        return self._providers[provider_name]

    @property
    def choices(self) -> List[Tuple[str, str]]:
        """Returns a list of (display text, value) tuples for use in prompts."""
        choices_list = []
        for name, provider_class in sorted(self._providers.items()):
            display_text = f"{name} - {provider_class.description()}"
            choices_list.append((display_text, name))
        return choices_list

    def _load_builtin_providers(self):
        """Load built-in strategies"""

        from opsmith.cloud_providers.aws import AWSProvider
        from opsmith.cloud_providers.gcp import GCPProvider

        for provider_cls in [AWSProvider, GCPProvider]:
            self.register(provider_cls)

    def _load_plugin_providers(self):
        """Loads providers from 'opsmith.cloud_providers' entry points."""
        discovered_entry_points = entry_points(group="opsmith.cloud_providers")

        for entry_point in discovered_entry_points:
            try:
                provider_class = entry_point.load()
                self.register(provider_class)
                print(f"Loaded cloud provider: {provider_class.name()}")
            except Exception as e:
                print(
                    "[yellow]Warning: Failed to load cloud provider from entry point"
                    f" '{entry_point.name}': {e}[/yellow]"
                )


class BaseCloudProvider(abc.ABC):
    """Abstract base class for cloud providers."""

    @classmethod
    @abc.abstractmethod
    def name(cls) -> str:
        """The name of the cloud provider."""
        raise NotImplementedError

    @classmethod
    @abc.abstractmethod
    def description(cls) -> str:
        """A brief description of the cloud provider."""
        raise NotImplementedError

    @classmethod
    @abc.abstractmethod
    def get_detail_model(cls) -> Type["BaseCloudProviderDetail"]:
        """The cloud provider detail model."""
        raise NotImplementedError

    @classmethod
    @abc.abstractmethod
    def get_account_details(cls) -> "BaseCloudProviderDetail":
        """
        Retrieves structured account details for the cloud provider.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_instance_types(self) -> "MachineTypeList":
        """
        Retrieves a list of available instance types for the given region.
        """
        raise NotImplementedError

    def __init__(self, provider_detail: dict, *args, **kwargs):
        """
        Initializes the cloud provider.
        Subclasses should implement specific authentication and setup.
        """
        self.provider_detail = TypeAdapter(self.get_detail_model()).validate_python(provider_detail)
        self.provider_detail_dump = self.provider_detail.model_dump(mode="json", exclude={"name"})
