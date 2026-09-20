"""What a command hands back, in a shape a machine can read.

Every command returns one of these, and ``handle_errors`` in ``opsmith/cli/app.py`` puts it in the
``result`` field of the JSON envelope. They exist because a coding harness driving Opsmith needs
the public IP, the registry URL and the urls of what it just deployed, and until this part those
facts only ever existed as text inside an event message.

Two things every result carries, whatever the command: the ``notices`` a run raised through
:meth:`Interaction.notify`, and the ``next_steps`` it asked for. A terminal user reads those as
they scroll past; a driver reads them here.
"""

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from opsmith.core.interaction import Notice
from opsmith.types import InfrastructureDependency, ServiceInfo


class ConfigIssue(BaseModel):
    """One problem found in a configuration, at one place in it."""

    path: str = Field(
        ..., description="Where the problem is, as a dotted path such as services.0.service_port."
    )
    message: str = Field(..., description="What is wrong there.")


class DnsRecord(BaseModel):
    """One DNS record a deployment needs somebody to create."""

    type: str = Field(..., description="The record type, such as A or CNAME.")
    name: str = Field(..., description="The name the record is created under.")
    value: str = Field(..., description="What the record points at.")


class ResourceKind(str, Enum):
    """The kinds of infrastructure Opsmith itself knows how to talk about.

    :class:`Resource` types its ``kind`` as a plain string rather than as this enum, because a
    strategy that makes something not listed here has to be able to say so without this enum
    growing first. Use a member where one fits, so the common kinds are spelled one way.
    """

    VIRTUAL_MACHINE = "virtual_machine"
    CONTAINER_REGISTRY = "container_registry"
    CONTENT_DELIVERY_NETWORK = "content_delivery_network"
    LOAD_BALANCER = "load_balancer"
    CLUSTER = "cluster"
    SERVICE = "service"
    WORKING_DIRECTORY = "working_directory"


class Resource(BaseModel):
    """One piece of infrastructure a strategy made, and enough to address it.

    Strategies report what they created as a list of these rather than as named fields, because
    a named ``public_ip`` is a claim that there is exactly one machine and that it has an IP.
    A strategy that raises several machines reports several of these; one that deploys to a
    cluster or to a serverless platform reports what it actually made and leaves the fields that
    do not apply unset.
    """

    kind: str = Field(
        ...,
        description=(
            "What sort of thing this is: a ResourceKind value, or a strategy's own word for"
            " something Opsmith does not name."
        ),
    )
    id: str = Field(..., description="How the provider addresses it, such as an instance ID.")
    name: Optional[str] = Field(
        None, description="What it is called, when it has a name separate from its ID."
    )
    region: Optional[str] = Field(None, description="Where it was created.")
    address: Optional[str] = Field(
        None, description="Where it is reached, as an IP or a hostname, when it is reachable."
    )
    size: Optional[str] = Field(
        None, description="How big it is, as the provider names it: an instance type or a tier."
    )
    details: Dict[str, str] = Field(
        default_factory=dict,
        description="Whatever else this kind carries that the fields above do not name.",
    )


class DeployedService(BaseModel):
    """One service an environment is currently running."""

    name_slug: str = Field(..., description="The slug the service is addressed by.")
    image: Optional[str] = Field(
        None, description="The image it is running, when the strategy records which one."
    )
    url: Optional[str] = Field(None, description="Where it is reachable, when it is routed.")


class OperationResult(BaseModel):
    """What every command reports, whatever else it reports."""

    notices: List[Notice] = Field(
        default_factory=list, description="What the run told the user while it worked."
    )
    next_steps: List[str] = Field(
        default_factory=list, description="What the user or the driver should do next."
    )


class ValidateResult(OperationResult):
    """What ``opsmith config validate`` found."""

    ok: bool = Field(..., description="Whether the configuration is usable.")
    errors: List[ConfigIssue] = Field(
        default_factory=list, description="Problems that stop the configuration being used."
    )
    warnings: List[ConfigIssue] = Field(
        default_factory=list, description="Problems that are suspicious but not fatal."
    )


class InitResult(OperationResult):
    """What ``opsmith init`` created."""

    app_name: str = Field(..., description="The application name that was recorded.")
    app_name_slug: str = Field(..., description="The slug the name was reduced to.")
    config_path: str = Field(..., description="The configuration file that was written.")


class SetupResult(OperationResult):
    """What ``opsmith setup`` detected and wrote."""

    app_name: str = Field(..., description="The application the configuration is for.")
    services: List[ServiceInfo] = Field(
        default_factory=list, description="The services the run confirmed."
    )
    dockerfiles: List[str] = Field(
        default_factory=list, description="The Dockerfiles written, one per service that needs one."
    )
    infra_deps: List[InfrastructureDependency] = Field(
        default_factory=list, description="The infrastructure dependencies the run confirmed."
    )
    config_path: str = Field(..., description="The configuration file that was written.")


class EnvironmentSummary(BaseModel):
    """One line of ``opsmith env list``."""

    name: str = Field(..., description="The environment's name.")
    provider: str = Field(..., description="The cloud provider it deploys to.")
    region: str = Field(..., description="The region it deploys into.")
    strategy: str = Field(..., description="The deployment strategy it uses.")
    deployed: bool = Field(
        ..., description="Whether it has been deployed, which is whether it has a state file."
    )


class EnvListResult(OperationResult):
    """What ``opsmith env list`` found in the configuration."""

    environments: List[EnvironmentSummary] = Field(
        default_factory=list, description="Every environment the configuration declares."
    )


class EnvCreateResult(OperationResult):
    """What ``opsmith env create`` created, and what it now needs from DNS."""

    environment: str = Field(..., description="The environment that was created.")
    provider: str = Field(..., description="The cloud provider it deploys to.")
    region: str = Field(..., description="The region it deploys into.")
    strategy: str = Field(..., description="The deployment strategy it uses.")
    deployed: bool = Field(
        True, description="Whether the environment was deployed, or only written to the config."
    )
    resources: List[Resource] = Field(
        default_factory=list, description="Every piece of infrastructure the run created."
    )
    registry_url: Optional[str] = Field(
        None,
        description=(
            "The container registry that holds the images. It is among the resources too; it is"
            " named here because pushing an image is the first thing a driver does next."
        ),
    )
    urls: Dict[str, str] = Field(
        default_factory=dict, description="The url each routed service is reachable at, by slug."
    )
    dns_records: List[DnsRecord] = Field(
        default_factory=list, description="The DNS records the deployment asked for."
    )


class EnvStatusResult(OperationResult):
    """What ``opsmith env status`` reads out of the environment's state file."""

    environment: str = Field(..., description="The environment being reported on.")
    provider: str = Field(..., description="The cloud provider it deploys to.")
    region: str = Field(..., description="The region it deploys into.")
    strategy: str = Field(..., description="The deployment strategy it uses.")
    deployed: bool = Field(..., description="Whether it has been deployed.")
    resources: List[Resource] = Field(
        default_factory=list, description="Every piece of infrastructure the environment holds."
    )
    registry_url: Optional[str] = Field(
        None,
        description=(
            "The container registry that holds the images. It is among the resources too; it is"
            " named here because pushing an image is the first thing a driver does next."
        ),
    )
    services: List[DeployedService] = Field(
        default_factory=list, description="The services the last deploy or update put on it."
    )
    urls: Dict[str, str] = Field(
        default_factory=dict, description="The url each routed service is reachable at, by slug."
    )


class ReleaseResult(OperationResult):
    """What ``opsmith release`` built and deployed."""

    environment: str = Field(..., description="The environment that was released to.")
    images: Dict[str, str] = Field(
        default_factory=dict, description="The image built for each service, by slug."
    )
    services: List[str] = Field(
        default_factory=list, description="The services that were released."
    )
    validated: Optional[bool] = Field(
        None,
        description=(
            "Whether the deployed stack came up healthy. None when nothing was deployed that"
            " could be validated."
        ),
    )
    validation_reason: Optional[str] = Field(
        None, description="What the validation found wrong, when it found something."
    )
    urls: Dict[str, str] = Field(
        default_factory=dict, description="The url each routed service is reachable at, by slug."
    )


class UpdateResult(OperationResult):
    """What ``opsmith update`` changed, or why it changed nothing."""

    environment: str = Field(..., description="The environment that was updated.")
    applied: bool = Field(..., description="Whether the update reached the environment.")
    reason: Optional[str] = Field(None, description="Why nothing was applied, when nothing was.")
    changes: Dict[str, List[str]] = Field(
        default_factory=dict,
        description=(
            "What the configuration changed, by kind: services and infra, added, removed and"
            " modified."
        ),
    )
    images: Dict[str, str] = Field(
        default_factory=dict, description="The image rebuilt for each service, by slug."
    )
    urls: Dict[str, str] = Field(
        default_factory=dict, description="The url each routed service is reachable at, by slug."
    )


class RunResult(OperationResult):
    """What a command run on a deployed service did.

    This is the one result that decides the process exit code: ``opsmith run`` returns what the
    remote command returned, so a script driving it reads the same code it would have read had it
    run the command itself.
    """

    environment: str = Field(..., description="The environment the command ran in.")
    service: str = Field(..., description="The service it ran on.")
    target: Optional[str] = Field(
        None,
        description=(
            "Where it ran, as the host or instance that answered. A strategy with more than one"
            " place to run a command picks one, and this is which one it picked."
        ),
    )
    command: str = Field(..., description="The command that was run.")
    exit_code: int = Field(..., description="What the remote command exited with.")
    stdout_tail: str = Field("", description="The last lines the command wrote to stdout.")
    stderr_tail: str = Field("", description="The last lines the command wrote to stderr.")

    @property
    def process_exit_code(self) -> int:
        """
        :return: The exit code Opsmith itself should exit with, which is the command's own.
        """
        return self.exit_code


class DestroyResult(OperationResult):
    """What ``opsmith destroy`` tore down."""

    environment: str = Field(..., description="The environment that was destroyed.")
    destroyed: List[Resource] = Field(
        default_factory=list,
        description=(
            "What was torn down, in the same shape the run that created it reported. An"
            " environment that was never deployed destroys nothing and says so with an empty list."
        ),
    )


#: Re-exported because a notice is part of every result, even though it is declared beside the
#: ``notify`` that produces it.
__all__ = [
    "ConfigIssue",
    "DeployedService",
    "DestroyResult",
    "DnsRecord",
    "EnvCreateResult",
    "EnvListResult",
    "EnvStatusResult",
    "EnvironmentSummary",
    "InitResult",
    "Notice",
    "OperationResult",
    "ReleaseResult",
    "Resource",
    "ResourceKind",
    "RunResult",
    "SetupResult",
    "UpdateResult",
    "ValidateResult",
]
