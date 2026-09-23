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
from opsmith.core.questions import ChoiceOption, PlannedQuestion
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


class ConfigSchemaResult(OperationResult):
    """The schema of the deployment configuration, in the rendering that was asked for."""

    format: str = Field(..., description="Which rendering this is: json or markdown.")
    #: Named ``schema_document`` in Python and ``schema`` on the wire: a field called ``schema``
    #: shadows a method of ``BaseModel``, and the envelope has always called this ``schema``.
    schema_document: Optional[Dict] = Field(
        None,
        serialization_alias="schema",
        description="The JSON Schema itself, when the JSON rendering was asked for.",
    )
    markdown: Optional[str] = Field(
        None, description="The markdown rendering, when that was asked for."
    )


class ConfigShowResult(OperationResult):
    """The deployment configuration, as Opsmith reads it."""

    config: Dict = Field(..., description="The configuration, upgraded and normalised.")


class DockerfileCheck(BaseModel):
    """What building and running one service's Dockerfile did.

    ``ok`` is the verdict to act on and is not the same as ``build_ok and run_ok``: when docker
    fails, the model is asked whether the failure is the Dockerfile's fault, and a container that
    exits because the database it wants does not exist yet is not. ``dockerfile_at_fault`` is what
    keeps ``build_ok: false, ok: true`` legible.
    """

    service: str = Field(..., description="The slug of the service that was checked.")
    dockerfile: str = Field(
        ..., description="The Dockerfile that was checked, relative to the repo."
    )
    ok: bool = Field(..., description="Whether the Dockerfile is usable as it stands.")
    build_ok: bool = Field(..., description="Whether docker build succeeded.")
    run_ok: Optional[bool] = Field(
        None,
        description=(
            "Whether the container ran. Unset when the build failed, because the run never"
            " happened."
        ),
    )
    run_timed_out: bool = Field(
        False,
        description=(
            "Whether the container was still up when the watch ended, which counts as healthy."
        ),
    )
    dockerfile_at_fault: Optional[bool] = Field(
        None,
        description=(
            "Whether the model judged the failure fixable in the Dockerfile. Unset when docker"
            " succeeded and nothing needed judging."
        ),
    )
    explanation: Optional[str] = Field(
        None, description="What the model said went wrong, when something did."
    )
    build_tail: str = Field("", description="The last lines of the build output.")
    run_tail: str = Field("", description="The last lines of the run output.")


class DockerfileValidateResult(OperationResult):
    """What ``opsmith dockerfile validate`` found, over every service it checked."""

    ok: bool = Field(..., description="Whether every service checked is usable as it stands.")
    checks: List[DockerfileCheck] = Field(
        default_factory=list, description="One check per service, in configuration order."
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


class EnvPlanResult(OperationResult):
    """What ``opsmith env plan`` worked out that a run is going to ask for.

    It is the exit-3 stop, reported all at once and before anything is created. Which questions
    exist depends on the cloud provider and the strategy, so a plan that does not know those two
    reports what it can and says the list is partial rather than claiming to be complete.
    """

    environment: Optional[str] = Field(
        None, description="The environment being planned, when one was named."
    )
    provider: Optional[str] = Field(
        None, description="The cloud provider the plan was made for, when one was chosen."
    )
    strategy: Optional[str] = Field(
        None, description="The deployment strategy the plan was made for, when one was chosen."
    )
    complete: bool = Field(
        ...,
        description=(
            "Whether this is the whole list. False when something still had to be chosen before"
            " the rest could be worked out, or when a provider or strategy declares no questions."
        ),
    )
    answers_needed: List[PlannedQuestion] = Field(
        default_factory=list, description="Every answer the run will stop for, in the order asked."
    )
    answers_known: List[str] = Field(
        default_factory=list,
        description=(
            "The keys that are already answered, by name. Values are not reported: some of them"
            " are secrets and this is printed."
        ),
    )
    blocked_on: List[str] = Field(
        default_factory=list,
        description="The keys to answer first, before the rest of the list can be worked out.",
    )
    partial_reasons: List[str] = Field(
        default_factory=list, description="Why the list is not complete, in plain text."
    )
    answers_file: Optional[str] = Field(
        None, description="Where the answers skeleton was written, when one was asked for."
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


class AgentLocationState(str, Enum):
    """What ``opsmith agent status`` found at one skill location."""

    INSTALLED = "installed"
    STALE = "stale"
    MISSING = "missing"
    FOREIGN = "foreign"
    NOT_APPLICABLE = "not_applicable"


class AgentLocation(BaseModel):
    """One place a harness reads skills from, and what Opsmith found there."""

    target: str = Field(..., description="The harness this location belongs to.")
    label: str = Field(..., description="What that harness is called, for a person reading.")
    scope: str = Field(..., description="Whether this is the project or the user location.")
    path: str = Field(..., description="The directory the skill is installed into.")
    state: AgentLocationState = Field(..., description="What is there now.")
    installed_version: Optional[str] = Field(
        None, description="The version of the skill installed there, when one is."
    )
    verified: bool = Field(
        ...,
        description=(
            "Whether this path has been confirmed against the harness itself. An unverified path"
            " is Opsmith's best reading of a convention that is still moving."
        ),
    )


class AgentInstallResult(OperationResult):
    """What ``opsmith agent install`` wrote."""

    skill: str = Field(..., description="The name the skill is installed under.")
    version: str = Field(..., description="The version of Opsmith the skill came from.")
    installed: List[AgentLocation] = Field(
        default_factory=list, description="Every location the skill was written to."
    )
    skipped: List[AgentLocation] = Field(
        default_factory=list, description="Locations that were not written, and why not."
    )
    agents_md: Optional[str] = Field(
        None, description="The AGENTS.md that was written, when --agents-md asked for it."
    )
    claude_md: Optional[str] = Field(
        None, description="The CLAUDE.md the import line was added to, when one was."
    )


class AgentUninstallResult(OperationResult):
    """What ``opsmith agent uninstall`` removed."""

    removed: List[AgentLocation] = Field(
        default_factory=list, description="Every location the skill was removed from."
    )
    agents_md: Optional[str] = Field(
        None, description="The AGENTS.md the managed block was stripped from, when there was one."
    )
    claude_md: Optional[str] = Field(
        None, description="The CLAUDE.md the import line was removed from, when there was one."
    )


class AgentStatusResult(OperationResult):
    """Where the skill is installed, and whether it is current."""

    skill: str = Field(..., description="The name the skill installs under.")
    package_version: str = Field(..., description="The version of Opsmith running.")
    skill_version: str = Field(..., description="The version the packaged skill declares.")
    locations: List[AgentLocation] = Field(
        default_factory=list, description="Every location Opsmith knows about, and its state."
    )
    agents_md: Optional[str] = Field(
        None, description="The AGENTS.md holding an Opsmith block, when there is one."
    )
    claude_md: Optional[str] = Field(
        None, description="The CLAUDE.md importing it, when there is one."
    )


#: Two re-exports. A notice is part of every result even though it is declared beside the
#: ``notify`` that produces it, and a planned question is part of a result even though it is
#: declared beside the tree it is worked out from.
__all__ = [
    "AgentInstallResult",
    "AgentLocation",
    "AgentLocationState",
    "AgentStatusResult",
    "AgentUninstallResult",
    "ChoiceOption",
    "ConfigIssue",
    "ConfigSchemaResult",
    "ConfigShowResult",
    "DeployedService",
    "DestroyResult",
    "DnsRecord",
    "DockerfileCheck",
    "DockerfileValidateResult",
    "EnvCreateResult",
    "EnvListResult",
    "EnvPlanResult",
    "EnvStatusResult",
    "EnvironmentSummary",
    "InitResult",
    "Notice",
    "OperationResult",
    "PlannedQuestion",
    "ReleaseResult",
    "Resource",
    "ResourceKind",
    "RunResult",
    "SetupResult",
    "UpdateResult",
    "ValidateResult",
]
