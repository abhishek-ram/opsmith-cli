"""Tests for the functions the interactive menu and the headless subcommands both call.

The strategy here is a fake that records what it was asked to do and returns fixed results, so
these tests are about orchestration and nothing else: which questions are asked, what the
configuration ends up holding, when the answer store is bound, and what comes back. What a real
strategy does with a provisioner is ``test_monolithic_strategy.py``'s subject.
"""

import ast
import io
import json
from pathlib import Path
from typing import Literal, Type
from unittest.mock import MagicMock, patch

import pytest
import yaml
from pydantic import Field
from rich.console import Console
from typer.testing import CliRunner

from opsmith.cli import app as app_module
from opsmith.cli.commands import deploy as deploy_command
from opsmith.cli.commands import env as env_command
from opsmith.cli.interaction import TerminalInteraction
from opsmith.cli.output import TextRenderer
from opsmith.cloud_providers import CLOUD_PROVIDER_REGISTRY
from opsmith.cloud_providers.base import (
    BaseCloudProvider,
    BaseCloudProviderDetail,
    MachineTypeList,
)
from opsmith.core import operations
from opsmith.core.answers import DELETE_CONFIRMATION, AnswerSources
from opsmith.core.context import OpsmithContext
from opsmith.core.errors import InvalidArgument, InvalidConfig, UnknownService
from opsmith.core.interaction import HeadlessInteraction
from opsmith.core.results import (
    DestroyResult,
    EnvCreateResult,
    EnvStatusResult,
    ReleaseResult,
    Resource,
    ResourceKind,
    RunResult,
    UpdateResult,
)
from opsmith.deployment_strategies import DEPLOYMENT_STRATEGY_REGISTRY
from opsmith.deployment_strategies.base import BaseDeploymentStrategy
from opsmith.models import MODEL_REGISTRY
from opsmith.tests.conftest import FakeGitRepo, FakeInteraction, FakeProvisionerFactory
from opsmith.types import (
    DeploymentConfig,
    DeploymentEnvironment,
    DomainInfo,
    ServiceInfo,
    ServiceList,
    ServiceTypeEnum,
)
from opsmith.utils import ExternalToolReport

API_SLUG = "api"
WEB_SLUG = "web"

#: The two machines the recording strategy claims to raise. Kept as a constant because deploy
#: reports them, destroy reports tearing the same ones down, and run reports having picked the
#: first - three claims that have to agree for the result models to mean anything.
RECORDED_MACHINES = (
    Resource(
        kind=ResourceKind.VIRTUAL_MACHINE,
        id="i-000000001",
        address="203.0.113.10",
        size="t4g.small",
    ),
    Resource(
        kind=ResourceKind.VIRTUAL_MACHINE,
        id="i-000000002",
        address="203.0.113.11",
        size="t4g.small",
    ),
)


class RecordingCloudDetail(BaseCloudProviderDetail):
    """The provider detail for the test provider: a region and nothing else."""

    name: Literal["RECORDING"] = Field(default="RECORDING", description="Provider name.")


class RecordingCloudProvider(BaseCloudProvider):
    """A cloud provider that answers from memory, so no test needs credentials."""

    @classmethod
    def name(cls) -> str:
        """The name the deployment config refers to this provider by."""
        return "RECORDING"

    @classmethod
    def description(cls) -> str:
        """What a menu would show next to the name."""
        return "A provider that exists only in the test suite."

    @classmethod
    def get_detail_model(cls) -> Type[RecordingCloudDetail]:
        """The detail model for this provider."""
        return RecordingCloudDetail

    @classmethod
    def get_account_details(cls, ctx: OpsmithContext) -> RecordingCloudDetail:
        """Returns fixed account details, asking the region the way a real provider would."""
        region = ctx.interact.ask("env.region", "Select a region", default="us-test-1")
        return RecordingCloudDetail(region=region)

    def get_instance_types(self) -> MachineTypeList:
        """No machine types: nothing in these tests sizes a machine."""
        return MachineTypeList(machines=[])


class RecordingStrategy(BaseDeploymentStrategy):
    """A strategy that records what it was asked to do and returns fixed results."""

    #: What every instance records into, so a test can read it without holding the instance.
    calls: list = []

    @classmethod
    def name(cls) -> str:
        """The name the deployment config refers to this strategy by."""
        return "Recording"

    @classmethod
    def description(cls) -> str:
        """What a menu would show next to the name."""
        return "A strategy that exists only in the test suite."

    def deploy(self, deployment_config, environment) -> EnvCreateResult:
        """Records the call and reports the pair of machines it never created.

        Two rather than one on purpose: the monolithic strategy raises a single machine, so it
        alone would never notice a result shape that can only describe one. A strategy that scales
        horizontally is the case these models exist to survive.
        """
        RecordingStrategy.calls.append(("deploy", environment.name))
        return EnvCreateResult(
            environment=environment.name,
            provider=str(environment.cloud_provider.get("name", "")),
            region=environment.cloud_provider_detail.region,
            strategy=environment.strategy,
            resources=list(RECORDED_MACHINES),
        )

    def release(self, deployment_config, environment) -> ReleaseResult:
        """Records the call and reports one service released."""
        RecordingStrategy.calls.append(("release", environment.name))
        return ReleaseResult(environment=environment.name, services=[API_SLUG], validated=True)

    def update(self, deployment_config, environment) -> UpdateResult:
        """Records the call and reports that it applied."""
        RecordingStrategy.calls.append(("update", environment.name))
        return UpdateResult(environment=environment.name, applied=True)

    def run(self, deployment_config, environment, service_name_slug, command) -> RunResult:
        """Records the call and reports the command failing, so the exit code has to travel."""
        RecordingStrategy.calls.append(("run", environment.name, service_name_slug, command))
        return RunResult(
            environment=environment.name,
            service=service_name_slug,
            target=RECORDED_MACHINES[0].address,
            command=command,
            exit_code=7,
            stdout_tail="out",
            stderr_tail="err",
        )

    def destroy(self, deployment_config, environment) -> DestroyResult:
        """Records the call and reports the machine torn down."""
        RecordingStrategy.calls.append(("destroy", environment.name))
        return DestroyResult(environment=environment.name, destroyed=list(RECORDED_MACHINES))

    def status(self, deployment_config, environment) -> EnvStatusResult:
        """Reports the pair of machines it claims to be running, and nothing more."""
        RecordingStrategy.calls.append(("status", environment.name))
        return EnvStatusResult(
            environment=environment.name,
            provider=str(environment.cloud_provider.get("name", "")),
            region=environment.cloud_provider_detail.region,
            strategy=environment.strategy,
            deployed=operations.is_deployed(self.ctx, environment.name),
            resources=list(RECORDED_MACHINES),
        )


@pytest.fixture(autouse=True)
def registered_plugins():
    """Registers the test provider and strategy, the way a plugin package registers itself."""
    CLOUD_PROVIDER_REGISTRY.register(RecordingCloudProvider)
    DEPLOYMENT_STRATEGY_REGISTRY.register(RecordingStrategy)
    RecordingStrategy.calls = []
    return RecordingStrategy


@pytest.fixture
def deployment_config() -> DeploymentConfig:
    """One routed backend and one frontend, so domain collection has something to ask about."""
    return DeploymentConfig(
        app_name="Acme App",
        app_name_slug="acme-app",
        services=[
            ServiceInfo(
                name_slug=API_SLUG,
                language="python",
                service_type=ServiceTypeEnum.BACKEND_API,
                service_port=8000,
            ),
            ServiceInfo(
                name_slug=WEB_SLUG,
                language="javascript",
                service_type=ServiceTypeEnum.FRONTEND,
                build_cmd="npm run build",
                build_dir="dist",
            ),
        ],
    )


@pytest.fixture
def deployed(deployment_config: DeploymentConfig, ctx: OpsmithContext) -> DeploymentEnvironment:
    """An environment that exists and has been deployed, so it has a state file."""
    environment = DeploymentEnvironment(
        name="prod",
        cloud_provider={"name": "RECORDING", "region": "us-test-1"},
        strategy="Recording",
        domain_email="ops@example.test",
        domains=[
            DomainInfo(service_name_slug=API_SLUG, domain_name="api.example.test"),
            DomainInfo(service_name_slug=WEB_SLUG, domain_name="www.example.test"),
        ],
    )
    deployment_config.environments.append(environment)

    state = operations.state_path(ctx, environment.name)
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("registry_url: registry.test/acme-app\n", encoding="utf-8")
    return environment


@pytest.fixture
def ctx(tmp_path: Path, events, provisioners, interact: FakeInteraction) -> OpsmithContext:
    """A context wired to the fakes, with nothing on disk but the deployments directory."""
    return OpsmithContext(
        src_dir=tmp_path,
        deployments_path=tmp_path / ".opsmith",
        events=events,
        agent=MagicMock(),
        provisioner_factory=provisioners,
        interact=interact,
        git_repo=FakeGitRepo(archive_path=tmp_path),
    )


# --- reading what the repository declares -------------------------------------------------------


def test_load_config_without_one_points_at_setup(ctx: OpsmithContext):
    """
    A repository that has not been set up is a configuration error with an actionable hint, not
    an internal failure, because running setup is exactly what fixes it.
    """
    with pytest.raises(InvalidConfig) as raised:
        operations.load_config(ctx)

    assert "opsmith setup" in raised.value.hint


def test_list_environments_says_which_of_them_exist(
    ctx: OpsmithContext, deployment_config: DeploymentConfig, deployed: DeploymentEnvironment
):
    """
    An environment is deployed when it has a state file, and only then - so one written to the
    config but never deployed is listed as not deployed rather than left out.
    """
    deployment_config.environments.append(
        DeploymentEnvironment(
            name="staging",
            cloud_provider={"name": "RECORDING", "region": "us-test-2"},
            strategy="Recording",
        )
    )

    result = operations.list_environments(ctx, deployment_config)

    assert [(env.name, env.deployed) for env in result.environments] == [
        ("prod", True),
        ("staging", False),
    ]
    assert [env.region for env in result.environments] == ["us-test-1", "us-test-2"]


def test_status_reads_the_state_file_and_reaches_nothing_else(
    ctx: OpsmithContext,
    deployment_config: DeploymentConfig,
    deployed: DeploymentEnvironment,
    provisioners: FakeProvisionerFactory,
):
    """
    Status has to answer on a machine with no tooling, so it must not touch a provisioner. The
    factory records every call it is asked for, and this asserts it was asked for none.
    """
    result = operations.environment_status(ctx, deployment_config, "prod")

    assert result.deployed is True
    assert result.environment == "prod"
    assert provisioners.actions() == []


def test_status_of_an_unknown_environment_names_the_ones_that_exist(
    ctx: OpsmithContext, deployment_config: DeploymentConfig, deployed: DeploymentEnvironment
):
    """A name that is not in the config exits 2, and the details list the names that are."""
    from opsmith.core.errors import UnknownEnvironment

    with pytest.raises(UnknownEnvironment) as raised:
        operations.environment_status(ctx, deployment_config, "nope")

    assert raised.value.details["known"] == ["prod"]


# --- creating an environment ---------------------------------------------------------------------


def test_create_environment_deploys_it_and_saves_the_config(
    ctx: OpsmithContext, deployment_config: DeploymentConfig, interact: FakeInteraction
):
    """
    Creating asks for the provider, the account's region, the name, the strategy and a domain per
    routed service, then deploys and writes the configuration - in that order, because the
    configuration is only worth writing once there is something deployed to describe.
    """
    interact.answers.update(
        {
            "env.cloud_provider": "RECORDING",
            "env.name": "prod",
            "env.strategy": "Recording",
            "env.domain_email": "ops@example.test",
            f"env.domain.{API_SLUG}": "api.example.test",
            f"env.domain.{WEB_SLUG}": "www.example.test",
        }
    )

    result = operations.create_environment(ctx, deployment_config)

    assert RecordingStrategy.calls == [("deploy", "prod")]
    assert [resource.address for resource in result.resources] == [
        "203.0.113.10",
        "203.0.113.11",
    ]
    assert result.deployed is True

    saved = yaml.safe_load((ctx.deployments_path / "deployments.yml").read_text())
    assert [env["name"] for env in saved["environments"]] == ["prod"]
    assert {d["service_name_slug"] for d in saved["environments"][0]["domains"]} == {
        API_SLUG,
        WEB_SLUG,
    }


def test_create_environment_with_no_deploy_writes_the_config_and_stops(
    ctx: OpsmithContext, deployment_config: DeploymentConfig, interact: FakeInteraction
):
    """
    --no-deploy is the half that costs nothing. The environment is in the configuration, the
    strategy was never asked for anything, and the result says it was not deployed.
    """
    interact.answers.update(
        {
            "env.cloud_provider": "RECORDING",
            "env.name": "prod",
            "env.strategy": "Recording",
            "env.domain_email": "ops@example.test",
            f"env.domain.{API_SLUG}": "api.example.test",
            f"env.domain.{WEB_SLUG}": "www.example.test",
        }
    )

    result = operations.create_environment(ctx, deployment_config, deploy=False)

    assert RecordingStrategy.calls == []
    assert result.deployed is False
    saved = yaml.safe_load((ctx.deployments_path / "deployments.yml").read_text())
    assert [env["name"] for env in saved["environments"]] == ["prod"]


def test_create_environment_picks_up_one_whose_creation_stopped(
    ctx: OpsmithContext, deployment_config: DeploymentConfig, interact: FakeInteraction
):
    """
    An environment in the configuration with no state file is a creation that stopped part way.
    Running create again finishes it rather than refusing the name, and leaves one entry, not two.
    """
    interact.answers.update(
        {
            "env.cloud_provider": "RECORDING",
            "env.name": "prod",
            "env.strategy": "Recording",
            "env.domain_email": "ops@example.test",
            f"env.domain.{API_SLUG}": "api.example.test",
            f"env.domain.{WEB_SLUG}": "www.example.test",
        }
    )
    operations.create_environment(ctx, deployment_config, deploy=False)

    operations.create_environment(ctx, deployment_config)

    assert RecordingStrategy.calls == [("deploy", "prod")]
    assert [env.name for env in deployment_config.environments] == ["prod"]
    assert len(deployment_config.environments[0].domains) == 2


def test_create_environment_refuses_a_name_that_is_already_deployed(
    ctx: OpsmithContext,
    deployment_config: DeploymentConfig,
    deployed: DeploymentEnvironment,
    interact: FakeInteraction,
):
    """
    A deployed environment is not a half-finished one, so its name is taken. The fake interaction
    applies the call site's own validator, which is the rule a terminal would have applied.
    """
    interact.answers.update({"env.cloud_provider": "RECORDING", "env.name": "prod"})

    with pytest.raises(AssertionError, match="already exists"):
        operations.create_environment(ctx, deployment_config)


def test_creating_binds_the_answer_store_as_soon_as_the_name_is_known(
    ctx: OpsmithContext, deployment_config: DeploymentConfig, events
):
    """
    Everything asked before the name is buffered and flushed on binding, so the region chosen
    while detecting the account ends up in the environment it turned out to belong to.

    A real headless interaction rather than the fake, because what is being proved here is the
    write-through, and the fake does not write anything.
    """
    ctx.interact = HeadlessInteraction(
        AnswerSources(
            inline={
                "env.cloud_provider": "RECORDING",
                "env.region": "us-test-9",
                "env.name": "prod",
                "env.strategy": "Recording",
                "env.domain_email": "ops@example.test",
                f"env.domain.{API_SLUG}": "api.example.test",
                f"env.domain.{WEB_SLUG}": "www.example.test",
            }
        ),
        ctx.answers,
        events=events,
        resume="opsmith env create",
    )

    operations.create_environment(ctx, deployment_config, deploy=False)

    assert ctx.answers.environment == "prod"
    remembered = yaml.safe_load(ctx.answers.answers_path.read_text(encoding="utf-8"))
    assert remembered["env.region"] == "us-test-9"
    assert remembered["env.cloud_provider"] == "RECORDING"


# --- acting on one that exists --------------------------------------------------------------------


def test_release_binds_the_store_and_calls_the_strategy(
    ctx: OpsmithContext, deployment_config: DeploymentConfig, deployed: DeploymentEnvironment
):
    """A release is the strategy's own; all this adds is binding the store to the environment."""
    result = operations.release(ctx, deployment_config, "prod")

    assert RecordingStrategy.calls == [("release", "prod")]
    assert ctx.answers.environment == "prod"
    assert result.services == [API_SLUG]


def test_update_saves_new_domains_before_the_strategy_runs(
    ctx: OpsmithContext,
    deployment_config: DeploymentConfig,
    deployed: DeploymentEnvironment,
    interact: FakeInteraction,
):
    """
    A service added since the last deploy has no domain, so update collects one and writes it
    before updating - the strategy must see a complete configuration, not ask about it half way.
    """
    deployment_config.services.append(
        ServiceInfo(
            name_slug="admin",
            language="python",
            service_type=ServiceTypeEnum.BACKEND_API,
            service_port=9000,
        )
    )
    interact.answers["env.domain.admin"] = "admin.example.test"

    operations.update(ctx, deployment_config, "prod")

    saved = yaml.safe_load((ctx.deployments_path / "deployments.yml").read_text())
    domains = {
        d["service_name_slug"]: d["domain_name"] for d in saved["environments"][0]["domains"]
    }
    assert domains["admin"] == "admin.example.test"
    assert RecordingStrategy.calls == [("update", "prod")]


def test_run_passes_the_service_and_command_straight_through(
    ctx: OpsmithContext, deployment_config: DeploymentConfig, deployed: DeploymentEnvironment
):
    """
    The service and the command describe this invocation, so they are arguments rather than
    answers - nothing is asked when they are given, and the exit code comes back on the result.
    """
    result = operations.run_command(
        ctx, deployment_config, "prod", service_name_slug=API_SLUG, command="ls -la"
    )

    assert RecordingStrategy.calls == [("run", "prod", API_SLUG, "ls -la")]
    assert result.exit_code == 7
    assert result.process_exit_code == 7


def test_run_refuses_a_service_that_cannot_run_a_command(
    ctx: OpsmithContext, deployment_config: DeploymentConfig, deployed: DeploymentEnvironment
):
    """
    A frontend is static files in a bucket; there is no container to run anything in. Naming one
    is a usage error, and the details list what could have been named instead.
    """
    with pytest.raises(UnknownService) as raised:
        operations.run_command(
            ctx, deployment_config, "prod", service_name_slug=WEB_SLUG, command="ls"
        )

    assert raised.value.details["runnable"] == [API_SLUG]
    assert RecordingStrategy.calls == []


def test_destroy_needs_the_word_typed(
    ctx: OpsmithContext,
    deployment_config: DeploymentConfig,
    deployed: DeploymentEnvironment,
    interact: FakeInteraction,
):
    """The gate is answered by typing the word, and then the strategy tears the environment down."""
    interact.answers["delete.confirm"] = DELETE_CONFIRMATION

    result = operations.destroy_environment(ctx, deployment_config, "prod")

    assert RecordingStrategy.calls == [("destroy", "prod")]
    assert [resource.id for resource in result.destroyed] == ["i-000000001", "i-000000002"]


def test_destroy_with_the_wrong_word_destroys_nothing(
    ctx: OpsmithContext,
    deployment_config: DeploymentConfig,
    deployed: DeploymentEnvironment,
    interact: FakeInteraction,
):
    """
    A wrong answer is a refused answer, not a missing one: exit 2, because telling a driver to
    supply what it just supplied would loop forever.
    """
    interact.answers["delete.confirm"] = "delete"

    with pytest.raises(InvalidArgument) as raised:
        operations.destroy_environment(ctx, deployment_config, "prod")

    assert raised.value.details["key"] == "delete.confirm"
    assert RecordingStrategy.calls == []


def test_every_result_carries_what_the_run_told_the_user(
    ctx: OpsmithContext, deployment_config: DeploymentConfig, deployed: DeploymentEnvironment
):
    """
    A notice reaches a terminal user as it happens and a driver through the result, so whatever
    the run said on the way is on the way out too.
    """
    ctx.interact.notify("Your site is live", details={"next_steps": ["point your domain at it"]})

    result = operations.release(ctx, deployment_config, "prod")

    assert [notice.message for notice in result.notices] == ["Your site is live"]
    assert result.next_steps == ["point your domain at it"]


# --- the menu and the subcommands end up in the same place -----------------------------------------


@pytest.fixture
def cli(monkeypatch, tmp_project, deployment_config: DeploymentConfig):
    """
    The real app, with the two steps that need a deployment machine stubbed out, pointed at a
    repository holding one deployed environment.
    """
    monkeypatch.setattr(app_module, "configure_agent", lambda *args, **kwargs: MagicMock())
    monkeypatch.setattr(
        app_module, "check_external_tools", lambda tools: ExternalToolReport(missing=[])
    )

    deployment_config.environments.append(
        DeploymentEnvironment(
            name="prod",
            cloud_provider={"name": "RECORDING", "region": "us-test-1"},
            strategy="Recording",
            domain_email="ops@example.test",
            domains=[
                DomainInfo(service_name_slug=API_SLUG, domain_name="api.example.test"),
                DomainInfo(service_name_slug=WEB_SLUG, domain_name="www.example.test"),
            ],
        )
    )
    deployment_config.save(tmp_project / ".opsmith")

    monkeypatch.chdir(tmp_project)
    return app_module.app


def _base_args(*extra: str):
    """Builds the global options every invocation needs, plus any extras."""
    return ["--model", MODEL_REGISTRY.model_names[0], "--api-key", "test-key", *extra]


@pytest.mark.parametrize(
    "action, function, subcommand",
    [
        ("release", "release", ["release", "--env", "prod"]),
        ("update", "update", ["update", "--env", "prod"]),
        ("delete", "destroy_environment", ["destroy", "--env", "prod"]),
    ],
)
def test_the_menu_and_the_subcommand_reach_the_same_function(
    cli, runner: CliRunner, action, function, subcommand
):
    """
    The menu is a dispatcher, so choosing an action and running the matching subcommand have to
    arrive at one function with one set of arguments. Patching that function and comparing what it
    was called with is the only way to prove they have not drifted.
    """
    calls = []

    def record(context, config, name, **kwargs):
        """Stands in for the operation, keeping what it was handed."""
        calls.append((name, kwargs))
        return None

    with patch.object(operations, function, side_effect=record):
        menu = runner.invoke(
            cli,
            _base_args(
                "--answer",
                "env.name=prod",
                "--answer",
                f"env.action={action}",
                "deploy",
            ),
        )
        direct = runner.invoke(cli, _base_args(*subcommand))

    assert menu.exit_code == 0, menu.output
    assert direct.exit_code == 0, direct.output
    assert len(calls) == 2
    assert calls[0] == calls[1]


def test_run_exits_with_the_code_the_command_exited_with(cli, runner: CliRunner):
    """
    Acceptance criterion 6: opsmith run returns the remote command's exit code as its own, so a
    script can use it exactly as it would use the command itself. The envelope still says ok,
    because opsmith did what it was asked - it is the command that failed.
    """
    result = runner.invoke(
        cli,
        _base_args(
            "--output", "json", "run", "--env", "prod", "--service", API_SLUG, "--", "ls", "-la"
        ),
    )

    assert result.exit_code == 7
    envelope = json.loads([line for line in result.stdout.splitlines() if line.strip()][0])
    assert envelope["ok"] is True
    assert envelope["result"]["exit_code"] == 7
    assert envelope["result"]["stdout_tail"] == "out"

    # Which of the strategy's machines answered. With one machine it is a convenience; with two
    # it is the only way to tell where the failure above happened.
    assert envelope["result"]["target"] == "203.0.113.10"
    assert RecordingStrategy.calls == [("run", "prod", API_SLUG, "ls -la")]


def test_a_strategy_that_made_several_machines_reports_them_all(cli, runner: CliRunner):
    """
    The result models describe infrastructure as a list, so a strategy that is not monolithic can
    say what it actually made. The recording strategy raises two machines; both have to survive
    the trip through the operation, the command and the envelope, with the fields that address
    them intact.
    """
    result = runner.invoke(cli, _base_args("--output", "json", "env", "status", "--env", "prod"))

    assert result.exit_code == 0, result.output
    resources = json.loads(result.stdout.splitlines()[0])["result"]["resources"]

    assert [(r["kind"], r["id"], r["address"]) for r in resources] == [
        ("virtual_machine", "i-000000001", "203.0.113.10"),
        ("virtual_machine", "i-000000002", "203.0.113.11"),
    ]


def test_env_list_and_status_need_no_external_tools(cli, runner: CliRunner, monkeypatch):
    """
    Acceptance criterion 5: neither declares a tool, so neither is probed for one. Making the
    probe fail outright is what proves it was never called, rather than called and satisfied.
    """

    def refuse(tools):
        """Fails the way a machine with nothing installed would."""
        raise AssertionError(f"env list and env status must not probe for {tools}")

    monkeypatch.setattr(app_module, "check_external_tools", refuse)

    listed = runner.invoke(cli, _base_args("--output", "json", "env", "list"))
    status = runner.invoke(cli, _base_args("--output", "json", "env", "status", "--env", "prod"))

    assert listed.exit_code == 0, listed.output
    assert status.exit_code == 0, status.output
    assert json.loads(listed.stdout.splitlines()[0])["result"]["environments"][0]["name"] == "prod"


# --- writing the configuration in the first place -------------------------------------------


@pytest.fixture
def detector(monkeypatch, deployment_config: DeploymentConfig):
    """
    A detector that finds the two services the config fixture describes, and writes no Dockerfile.

    Detection is the model's job and is tested where the detector is; what these tests are about
    is what setup does with what comes back.
    """
    fake = MagicMock()
    fake.detect_services.return_value = ServiceList(
        services=deployment_config.services, infra_deps=[]
    )
    fake.generate_dockerfile.return_value = None
    monkeypatch.setattr(operations, "ServiceDetector", lambda ctx: fake)
    return fake


def test_init_writes_a_configuration_without_scanning_anything(
    ctx: OpsmithContext, interact: FakeInteraction
):
    """
    init is the half of setup that costs nothing: a name, a file, and the gitignore entries the
    terraform state needs. A harness that intends to author the services itself starts here.
    """
    interact.answers["app.name"] = "Acme App"

    result = operations.init_config(ctx)

    assert result.app_name == "Acme App"
    assert result.app_name_slug == "acme-app"
    saved = yaml.safe_load(Path(result.config_path).read_text())
    assert saved["services"] == []
    assert ctx.git_repo.ensure_gitignore_calls == 1


def test_init_refuses_to_overwrite_a_configuration_that_exists(
    ctx: OpsmithContext, deployment_config: DeploymentConfig, interact: FakeInteraction
):
    """
    Writing a fresh configuration over one that exists would throw away everything in it, so it
    is refused with a hint naming the two commands that do change one.
    """
    deployment_config.save(ctx.deployments_path)
    interact.answers["app.name"] = "Acme App"

    with pytest.raises(InvalidConfig) as raised:
        operations.init_config(ctx)

    assert "opsmith setup" in raised.value.hint


def test_setup_confirms_each_service_and_writes_the_configuration(
    ctx: OpsmithContext, interact: FakeInteraction, detector
):
    """
    Setup reviews each detected service, generates a Dockerfile for it, and writes what came back
    - so the file holds what the user confirmed, not what the model first proposed.
    """
    interact.answers["app.name"] = "Acme App"

    result = operations.run_setup(ctx)

    assert [service.name_slug for service in result.services] == [API_SLUG, WEB_SLUG]
    assert detector.generate_dockerfile.call_count == 2
    reviewed = [entry["key"] for entry in interact.asked if entry["primitive"] == "edit"]
    assert reviewed == [f"service.{API_SLUG}.confirm", f"service.{WEB_SLUG}.confirm"]
    assert yaml.safe_load(Path(result.config_path).read_text())["app_name"] == "Acme App"


def test_setup_told_to_exit_changes_nothing(
    ctx: OpsmithContext, deployment_config: DeploymentConfig, interact: FakeInteraction, detector
):
    """
    Choosing exit at the menu is not a re-scan with no changes: nothing is detected, and the file
    the user declined to change is not rewritten.
    """
    deployment_config.save(ctx.deployments_path)
    written_at = (ctx.deployments_path / "deployments.yml").stat().st_mtime_ns
    interact.answers["setup.action"] = "exit"

    result = operations.run_setup(ctx)

    assert detector.detect_services.call_count == 0
    assert result.dockerfiles == []
    assert (ctx.deployments_path / "deployments.yml").stat().st_mtime_ns == written_at


def _setup_context(tmp_path: Path, events, provisioners, *, accept_reviews: bool):
    """
    Builds a context whose interaction is the real terminal one.

    A fake interaction would prove nothing here: what is under test is whether an editor opens,
    and only the implementation that opens editors can answer that. The console writes nowhere,
    so the test says nothing on the way past.

    :param tmp_path: Where the repository and its .opsmith directory live.
    :param events: The recording sink.
    :param provisioners: The recording provisioner factory.
    :param accept_reviews: Whether --accept-detected was given.
    :return: The context.
    """
    return OpsmithContext(
        src_dir=tmp_path,
        deployments_path=tmp_path / ".opsmith",
        events=events,
        agent=MagicMock(),
        provisioner_factory=provisioners,
        interact=TerminalInteraction(
            TextRenderer(console=Console(file=io.StringIO())),
            sources=AnswerSources(accept_reviews=accept_reviews),
        ),
        git_repo=FakeGitRepo(archive_path=tmp_path),
    )


def _answer_with_the_default(questions, **kwargs):
    """
    Stands in for the person at the terminal, accepting whatever the question offered.

    :param questions: The one-question list the interaction built.
    :return: The answer, by question name.
    """
    question = questions[0]
    return {question.name: question.default if question.default is not None else "Acme App"}


def test_accept_detected_takes_the_configuration_as_proposed(
    tmp_path: Path, events, provisioners, detector
):
    """
    A review editor is where a person checks what was detected. --accept-detected says they do
    not want to, so no editor opens - which is what makes setup one command rather than one
    editor per service.
    """
    ctx = _setup_context(tmp_path, events, provisioners, accept_reviews=True)

    with patch("opsmith.cli.interaction.inquirer.prompt") as prompt:
        prompt.side_effect = _answer_with_the_default
        result = operations.run_setup(ctx)

    opened = [call.args[0][0].name for call in prompt.call_args_list]
    assert opened == ["app.name"], f"no editor should have opened, these did: {opened}"
    assert [service.name_slug for service in result.services] == [API_SLUG, WEB_SLUG]


def test_without_it_every_detected_service_is_put_up_for_review(
    tmp_path: Path, events, provisioners, detector
):
    """
    The other half, so the test above cannot pass because nothing reviews anything: by default a
    person is shown each service and each dependency, one editor at a time.
    """
    ctx = _setup_context(tmp_path, events, provisioners, accept_reviews=False)

    with patch("opsmith.cli.interaction.inquirer.prompt") as prompt:
        prompt.side_effect = _answer_with_the_default
        operations.run_setup(ctx)

    opened = [call.args[0][0].name for call in prompt.call_args_list]
    assert opened == ["app.name", f"service.{API_SLUG}.confirm", f"service.{WEB_SLUG}.confirm"]


def test_env_list_writes_the_listing_for_a_terminal(cli, runner: CliRunner):
    """
    The listing is the whole point of the command, and in text mode nothing else would write it -
    events narrate progress, and there is no progress to narrate.
    """
    result = runner.invoke(cli, _base_args("env", "list"))

    assert result.exit_code == 0, result.output
    assert "NAME" in result.stdout
    assert "prod" in result.stdout
    assert "deployed" in result.stdout


def test_env_list_writes_the_listing_only_once_in_json_mode(cli, runner: CliRunner):
    """
    In JSON mode the envelope carries the listing, so writing the table as well would put two
    documents on a stream that promises exactly one.
    """
    result = runner.invoke(cli, _base_args("--output", "json", "env", "list"))

    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["result"]["environments"][0]["name"] == "prod"


def test_env_list_says_so_when_there_are_no_environments(cli, runner: CliRunner, tmp_project):
    """An empty listing is an answer, and it names the command that changes it."""
    DeploymentConfig(app_name="Acme App", app_name_slug="acme-app").save(tmp_project / ".opsmith")

    result = runner.invoke(cli, _base_args("env", "list"))

    assert result.exit_code == 0, result.output
    assert "opsmith env create" in result.stdout


def test_the_menu_only_ever_calls_operations():
    """
    The rule that keeps the menu and the subcommands together, checked rather than trusted: the
    menu resolves a choice to a function in core.operations and calls that. If it ever reached a
    strategy or a provisioner directly, the two surfaces could do different things for the same
    choice, and only one of them would be tested.
    """
    source = Path(deploy_command.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    reached = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        receiver = node.func.value
        if isinstance(receiver, ast.Name):
            reached.add(f"{receiver.id}.{node.func.attr}")
        elif isinstance(receiver, ast.Attribute):
            reached.add(f"{receiver.attr}.{node.func.attr}")

    forbidden = sorted(
        call
        for call in reached
        if call.split(".")[0] in {"strategy", "provisioners", "provisioner_factory"}
        or call.endswith((".deploy", ".release", ".update", ".run", ".destroy", ".status"))
        and not call.startswith("operations.")
    )
    assert not forbidden, f"the deploy menu must go through operations, not {forbidden}"

    # And it does reach them, so the check above is not passing because nothing is called at all.
    assert {call for call in reached if call.startswith("operations.")} >= {
        "operations.load_config",
        "operations.create_environment",
        "operations.release",
        "operations.update",
        "operations.run_command",
        "operations.destroy_environment",
    }


def test_a_resource_kind_opsmith_has_never_heard_of_still_renders():
    """
    The terminal renderer knows nothing about virtual machines, which is what lets a third-party
    strategy print as readably as the built-in one. A kind that is not in ResourceKind, and
    details this file has never seen, still come out as a line somebody can read.
    """
    exotic = Resource(
        kind="edge_worker",
        id="worker-7",
        name="api-edge",
        size="128mb",
        address="api-edge.workers.test",
        details={"runtime": "v8", "colo": "LHR"},
    )

    assert env_command._rendered_resource(exotic) == [
        "  edge_worker: api-edge (128mb) at api-edge.workers.test",
        "    colo=LHR, runtime=v8",
    ]

    # Nothing but the kind and the id is required, and a resource with only those still renders.
    assert env_command._rendered_resource(Resource(kind="cluster", id="c-1")) == ["  cluster: c-1"]

    # A resource addressed by what it is reached at names it once, not twice.
    registry = Resource(
        kind="container_registry", id="registry.test/acme", address="registry.test/acme"
    )
    assert env_command._rendered_resource(registry) == ["  container_registry: registry.test/acme"]
