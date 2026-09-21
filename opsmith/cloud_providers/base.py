import abc
from enum import Enum
from importlib.metadata import entry_points
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Generic,
    List,
    Mapping,
    Optional,
    Type,
    TypeVar,
)

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from opsmith.core.errors import InvalidArgument
from opsmith.core.events import STEP_REGISTRY, BufferingSink
from opsmith.core.interaction import Choice
from opsmith.core.questions import Question

if TYPE_CHECKING:
    from opsmith.core.context import OpsmithContext


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

    def as_options(self) -> List[Choice]:
        """
        Describes the machines as options a person can be asked to pick from.

        How a recommendation is shown is the asker's business, so it is marked on the choice
        rather than written into the label.

        :return: One choice per machine, cheapest first.
        """
        # sort machines by cpu and ram to have a consistent order for user
        sorted_machines = sorted(self.machines, key=lambda m: (m.cpu, m.ram_gb))

        return [
            Choice(
                label=(
                    f"{machine.name} ({machine.cpu} vCPUs, {machine.ram_gb} GB RAM,"
                    f" {machine.architecture.value})"
                ),
                value=machine,
                recommended=machine.is_recommended,
            )
            for machine in sorted_machines
        ]


class BaseCloudProviderDetail(BaseModel):
    name: str = Field(..., description="Provider name")
    region: str = Field(..., description="The cloud provider region for this environment.")


class AccountInfo(BaseModel):
    """What detecting a cloud account found, before anybody has been asked anything.

    It is the provider's own: AWS puts an account id and the path to a plugin here, GCP puts the
    credentials it authenticated with. None of it is written down - it may hold a live SDK object,
    and it is rebuilt on every run - so nothing here reaches ``deployments.yml``.
    :meth:`BaseCloudProvider.build_detail` is what produces the detail that does.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)


class CloudProviderRegistry:
    """A singleton registry for cloud providers."""

    _instance: Optional["CloudProviderRegistry"] = None
    _providers: Dict[str, Type["BaseCloudProvider"]]

    #: Plugins load at import time, before the CLI has a renderer, so what happens during the
    #: load is buffered here and drained once there is somewhere to report it.
    pending_events: BufferingSink

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._providers = {}
            cls._instance.pending_events = BufferingSink()
            cls._instance._load_builtin_providers()
            cls._instance._load_plugin_providers()
        return cls._instance

    def register(self, provider_class: Type["BaseCloudProvider"]):
        """Registers a cloud provider."""
        # Not raising error on overwrite allows for easy extension/replacement
        self._providers[provider_class.name()] = provider_class

    def get_provider_class(self, provider_name: str) -> Type["BaseCloudProvider"]:
        """
        Retrieves a provider class from the registry.

        :param provider_name: The name the configuration or ``--provider`` gave.
        :return: The provider class.
        :raises InvalidArgument: No provider is registered under that name. A usage error rather
            than a ValueError, because the name usually came off a flag and a driver reading the
            envelope needs the names that would have worked.
        """
        if provider_name not in self._providers:
            raise InvalidArgument(
                f"There is no cloud provider named '{provider_name}'.",
                hint="Install its plugin, or use one of the providers listed in the details.",
                details={"provider": provider_name, "known": sorted(self._providers)},
            )
        return self._providers[provider_name]

    @property
    def choices(self) -> List[Choice]:
        """
        Describes the registered providers as options a person can be asked to pick from.

        :return: One choice per provider, by name.
        """
        return [
            Choice(label=f"{name} - {provider_class.description()}", value=name)
            for name, provider_class in sorted(self._providers.items())
        ]

    def _load_builtin_providers(self):
        """Load built-in strategies"""

        from opsmith.cloud_providers.aws import AWSProvider
        from opsmith.cloud_providers.gcp import GCPProvider

        builtin_providers: List[Type[BaseCloudProvider[Any, Any]]] = [AWSProvider, GCPProvider]
        for provider_cls in builtin_providers:
            self.register(provider_cls)

    def _load_plugin_providers(self):
        """Loads providers from 'opsmith.cloud_providers' entry points."""
        discovered_entry_points = entry_points(group="opsmith.cloud_providers")

        for entry_point in discovered_entry_points:
            try:
                provider_class = entry_point.load()
                self.register(provider_class)
                self.pending_events.log(
                    STEP_REGISTRY, f"Loaded cloud provider: {provider_class.name()}"
                )
            except Exception as e:
                self.pending_events.warning(
                    STEP_REGISTRY,
                    f"Failed to load cloud provider from entry point '{entry_point.name}': {e}",
                    entry_point=entry_point.name,
                )


AccountT = TypeVar("AccountT", bound=AccountInfo)
"""What a provider's :meth:`BaseCloudProvider.detect_account` found.

Each provider detects its own shape and builds its detail from that same shape, so the two are
one choice rather than two. Parameterising the base on it is what lets a provider narrow both
signatures at once without violating the substitution rule. A provider that subclasses the base
bare is still valid - it is simply read as ``BaseCloudProvider[AccountInfo]``.
"""


DetailT = TypeVar("DetailT", bound=BaseCloudProviderDetail)
"""What this provider records in ``deployments.yml``.

Paired with :data:`AccountT` for the same reason: ``get_detail_model`` and ``provider_detail``
are the same choice named twice, and parameterising on it is what lets a provider read its own
fields - a GCP project id, an AWS session-manager plugin - without a cast at every use.
"""


class BaseCloudProvider(abc.ABC, Generic[AccountT, DetailT]):
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
    def get_detail_model(cls) -> Type[DetailT]:
        """The cloud provider detail model."""
        raise NotImplementedError

    @classmethod
    @abc.abstractmethod
    def detect_account(cls, ctx: "OpsmithContext") -> AccountT:
        """
        Finds out what it can about the account, without asking anybody anything.

        This is where a provider checks that it has credentials and reads the facts that are not
        a matter of choice - the account id, the path to a helper it needs. It must not interact:
        ``opsmith env plan`` calls it to enrich its report, and a plan that prompted would not be
        a plan.

        :param ctx: The run's context, for reporting the wait on the provider's API.
        :return: What was found, in whatever shape this provider builds a detail from.
        :raises CloudCredentialsError: The account could not be reached.
        """
        raise NotImplementedError

    @classmethod
    def questions(cls) -> List[Question]:
        """
        Declares what this provider needs a person to choose, without asking it.

        Declaring is optional. A provider that returns nothing here still works: it asks whatever
        it needs through ``ctx.interact`` inside :meth:`build_detail`, and the only thing it gives
        up is being reported by ``opsmith env plan``, which says its list is partial.

        :return: The questions, in the order they should be asked. A question whose options
            depend on an earlier answer declares that with ``depends_on``.
        """
        return []

    @classmethod
    @abc.abstractmethod
    def build_detail(
        cls, ctx: "OpsmithContext", account: AccountT, answers: Mapping[str, Any]
    ) -> "BaseCloudProviderDetail":
        """
        Assembles what the environment records about this provider.

        :param ctx: The run's context. A provider that declared no questions asks them here,
            through ``ctx.interact``.
        :param account: What :meth:`detect_account` found.
        :param answers: The answers to :meth:`questions`, by interaction key.
        :return: The detail that is written to ``deployments.yml``.
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
        self.provider_detail: DetailT = TypeAdapter(self.get_detail_model()).validate_python(
            provider_detail
        )
        self.provider_detail_dump = self.provider_detail.model_dump(mode="json", exclude={"name"})
