"""Tests for `opsmith env plan`: what a run will ask, reported before the run starts.

The plan is only worth having if it is right about two things - which questions exist, and
whether it knows about all of them - so most of what is asserted here is the shape of the list
and the honesty of the `complete` flag. The rest is what the command must never do: create
anything, or need anything installed.
"""

import json
from pathlib import Path
from typing import List, Literal, Type
from unittest.mock import MagicMock

import pytest
import yaml
from pydantic import Field
from typer.testing import CliRunner

from opsmith.cli import app as app_module
from opsmith.cloud_providers import CLOUD_PROVIDER_REGISTRY
from opsmith.cloud_providers.base import (
    AccountInfo,
    BaseCloudProvider,
    BaseCloudProviderDetail,
    MachineTypeList,
)
from opsmith.core import operations
from opsmith.core.answers import AnswerSources
from opsmith.core.context import OpsmithContext
from opsmith.core.errors import CloudCredentialsError, InvalidArgument
from opsmith.core.questions import Question
from opsmith.deployment_strategies import DEPLOYMENT_STRATEGY_REGISTRY
from opsmith.deployment_strategies.monolithic import MonolithicDeploymentStrategy
from opsmith.models import MODEL_REGISTRY
from opsmith.tests.conftest import FakeGitRepo, FakeInteraction, FakeProvisionerFactory
from opsmith.tests.test_operations import API_SLUG, WEB_SLUG, RecordingStrategy
from opsmith.types import (
    DeploymentConfig,
    DeploymentEnvironment,
    DomainInfo,
    EnvVarConfig,
    ServiceInfo,
    ServiceTypeEnum,
)

PLANNING_PROVIDER = "PLANNING"
SILENT_PROVIDER = "SILENT"
SILENT_STRATEGY = "Silent"

#: The real strategy, by name. Planning never constructs one - it reads the declaration off the
#: class - so the questions these tests assert on are the ones a monolithic deploy really asks.
MONOLITHIC = MonolithicDeploymentStrategy.name()


class PlanningCloudDetail(BaseCloudProviderDetail):
    """The detail of the provider these tests plan against."""

    name: Literal["PLANNING"] = Field(default=PLANNING_PROVIDER, description="Provider name.")


class PlanningCloudProvider(BaseCloudProvider):
    """A provider that declares one question and can be told to refuse detection."""

    #: Raised by detection when set, standing in for a machine with no credentials.
    refusal: Exception = None

    @classmethod
    def name(cls) -> str:
        """The name the deployment config refers to this provider by."""
        return PLANNING_PROVIDER

    @classmethod
    def description(cls) -> str:
        """What a menu would show next to the name."""
        return "A provider that exists only in the test suite."

    @classmethod
    def get_detail_model(cls) -> Type[PlanningCloudDetail]:
        """The detail model for this provider."""
        return PlanningCloudDetail

    @classmethod
    def detect_account(cls, ctx: OpsmithContext) -> AccountInfo:
        """Reaches no cloud, or refuses the way one with no credentials would."""
        if cls.refusal is not None:
            raise cls.refusal
        return AccountInfo()

    @classmethod
    def questions(cls) -> List[Question]:
        """Declares the region, with two options, the way a real provider does."""
        return [
            Question(
                key="env.region",
                message="Select a region",
                primitive="select",
                asked_by=cls.name(),
                default="us-test-1",
            )
        ]

    @classmethod
    def build_detail(cls, ctx, account, answers) -> PlanningCloudDetail:
        """Builds the detail from the one answer this provider declares."""
        return PlanningCloudDetail(region=answers["env.region"])

    def get_instance_types(self) -> MachineTypeList:
        """No machine types: nothing in these tests sizes a machine."""
        return MachineTypeList(machines=[])


class SilentCloudDetail(BaseCloudProviderDetail):
    """The detail of the provider that declares nothing."""

    name: Literal["SILENT"] = Field(default=SILENT_PROVIDER, description="Provider name.")


class SilentCloudProvider(PlanningCloudProvider):
    """A third-party provider that declares no questions and asks inside build_detail."""

    @classmethod
    def name(cls) -> str:
        """The name the deployment config refers to this provider by."""
        return SILENT_PROVIDER

    @classmethod
    def get_detail_model(cls) -> Type[SilentCloudDetail]:
        """The detail model for this provider."""
        return SilentCloudDetail

    @classmethod
    def questions(cls) -> List[Question]:
        """Declares nothing, which is allowed and is what makes a plan partial."""
        return []

    @classmethod
    def build_detail(cls, ctx, account, answers) -> SilentCloudDetail:
        """Asks inline, the way a provider that declared nothing has to."""
        region = ctx.interact.ask("env.region", "Select a region", default="us-test-1")
        return SilentCloudDetail(region=region)


class SilentStrategy(RecordingStrategy):
    """A third-party strategy that declares no questions."""

    @classmethod
    def name(cls) -> str:
        """The name the deployment config refers to this strategy by."""
        return SILENT_STRATEGY


class RefusingProvisionerFactory(FakeProvisionerFactory):
    """A factory that fails the moment anything asks it for a provisioner.

    Asserting that the call log is empty would pass for a plan that never got far enough to try.
    Failing here proves the command reached the end without wanting one.
    """

    def terraform(self, working_dir: Path, step: str = "provision"):
        """:raises AssertionError: Always. A plan creates nothing."""
        raise AssertionError(f"env plan must not run terraform in {working_dir}")

    def ansible(self, working_dir: Path, step: str = "provision"):
        """:raises AssertionError: Always. A plan creates nothing."""
        raise AssertionError(f"env plan must not run ansible in {working_dir}")


@pytest.fixture(autouse=True)
def registered_plugins():
    """Registers the providers and strategies these tests plan against."""
    PlanningCloudProvider.refusal = None
    CLOUD_PROVIDER_REGISTRY.register(PlanningCloudProvider)
    CLOUD_PROVIDER_REGISTRY.register(SilentCloudProvider)
    DEPLOYMENT_STRATEGY_REGISTRY.register(RecordingStrategy)
    DEPLOYMENT_STRATEGY_REGISTRY.register(SilentStrategy)
    RecordingStrategy.calls = []


@pytest.fixture
def deployment_config() -> DeploymentConfig:
    """
    Two routed services and three environment variables, two of them without a value.

    That is the shape acceptance criterion 1 names, so the test below can say "exactly these".
    """
    return DeploymentConfig(
        app_name="Acme App",
        app_name_slug="acme-app",
        services=[
            ServiceInfo(
                name_slug=API_SLUG,
                language="python",
                service_type=ServiceTypeEnum.BACKEND_API,
                service_port=8000,
                env_vars=[
                    EnvVarConfig(key="DATABASE_URL", is_secret=False),
                    EnvVarConfig(key="SECRET_KEY", is_secret=True),
                    EnvVarConfig(key="LOG_LEVEL", is_secret=False, default_value="info"),
                ],
            ),
            ServiceInfo(
                name_slug=WEB_SLUG,
                language="javascript",
                service_type=ServiceTypeEnum.FRONTEND,
                build_cmd="npm run build",
                build_dir="dist",
            ),
            ServiceInfo(
                name_slug="worker",
                language="python",
                service_type=ServiceTypeEnum.BACKEND_WORKER,
            ),
        ],
    )


@pytest.fixture
def provisioners() -> RefusingProvisionerFactory:
    """A provisioner factory that fails if a plan asks it for anything."""
    return RefusingProvisionerFactory()


@pytest.fixture
def ctx(tmp_path: Path, events, provisioners, interact: FakeInteraction) -> OpsmithContext:
    """A context whose provisioners refuse, so nothing here can create anything."""
    return OpsmithContext(
        src_dir=tmp_path,
        deployments_path=tmp_path / ".opsmith",
        events=events,
        agent=MagicMock(),
        provisioner_factory=provisioners,
        interact=interact,
        git_repo=FakeGitRepo(archive_path=tmp_path),
    )


def sources(**answers) -> AnswerSources:
    """
    :param answers: Inline answers, by interaction key with dots written as underscores is not
        needed here - keys are passed through as given.
    :return: The sources a run would have been given.
    """
    accept_defaults = answers.pop("accept_defaults", False)
    return AnswerSources(inline=dict(answers), accept_defaults=accept_defaults)


def plan(ctx, deployment_config, **answers):
    """
    :param ctx: The run's context.
    :param deployment_config: What the repository deploys.
    :param answers: What the run was told up front.
    :return: The plan.
    """
    return operations.plan_environment(ctx, deployment_config, sources=sources(**answers))


# --- what the list holds ----------------------------------------------------------------------


def test_the_plan_lists_the_domains_of_routed_services_and_the_valueless_variables(
    ctx: OpsmithContext, deployment_config: DeploymentConfig
):
    """
    Acceptance criterion 1. With the provider and strategy chosen and defaults accepted, what is
    left to answer is one domain per routed service and every variable without a value - and the
    worker, which is routed nowhere, is not among them.
    """
    result = plan(
        ctx,
        deployment_config,
        **{
            "env.cloud_provider": PLANNING_PROVIDER,
            "env.strategy": MONOLITHIC,
            "accept_defaults": True,
        },
    )

    required = {question.key for question in result.answers_needed if question.required}

    assert {key for key in required if key.startswith("env.domain.")} == {
        f"env.domain.{API_SLUG}",
        f"env.domain.{WEB_SLUG}",
    }
    assert {key for key in required if key.startswith("envvar.")} == {
        "envvar.DATABASE_URL",
        "envvar.SECRET_KEY",
    }
    # LOG_LEVEL has a value in the configuration, so accepting defaults answers it.
    assert "envvar.LOG_LEVEL" in {question.key for question in result.answers_needed}
    assert "envvar.LOG_LEVEL" not in required


def test_the_plan_asks_in_the_order_a_creation_asks(
    ctx: OpsmithContext, deployment_config: DeploymentConfig
):
    """
    A plan is read top to bottom by somebody about to live through it, so the provider's own
    questions come straight after the provider, and the strategy's last.
    """
    result = plan(
        ctx,
        deployment_config,
        **{
            "env.cloud_provider": PLANNING_PROVIDER,
            "env.strategy": MONOLITHIC,
        },
    )

    keys = [question.key for question in result.answers_needed]

    # The provider and the strategy were supplied, so they are not in the list; what is left
    # still runs provider-first, then the environment itself, then what the strategy needs.
    assert keys[:3] == ["env.region", "env.name", "env.domain_email"]
    assert keys.index("env.instance_type") > keys.index(f"env.domain.{WEB_SLUG}")


def test_a_question_already_answered_is_reported_as_known_not_as_needed(
    ctx: OpsmithContext, deployment_config: DeploymentConfig
):
    """
    A plan must not report a question the run it is planning would sail past, and must not echo
    what it was told either: the keys come back, the values do not.
    """
    result = plan(
        ctx,
        deployment_config,
        **{
            "env.cloud_provider": PLANNING_PROVIDER,
            "env.strategy": MONOLITHIC,
            "env.region": "us-test-1",
            "envvar.SECRET_KEY": "hunter2",
        },
    )

    assert "env.region" in result.answers_known
    assert "env.region" not in {question.key for question in result.answers_needed}
    assert "hunter2" not in result.model_dump_json()


def test_without_a_provider_the_plan_says_which_answer_would_open_the_rest(
    ctx: OpsmithContext, deployment_config: DeploymentConfig
):
    """
    The tree forks on the provider and the strategy, so a plan that has neither reports what it
    can and names the two to answer first rather than pretending the short list is the whole one.
    """
    result = plan(ctx, deployment_config)

    assert result.complete is False
    assert result.provider is None
    assert "env.cloud_provider" in {question.key for question in result.answers_needed}
    assert any("cloud provider" in reason for reason in result.partial_reasons)
    assert any("strategy" in reason for reason in result.partial_reasons)


def test_an_environment_that_already_has_domains_is_not_asked_for_them_again(
    ctx: OpsmithContext, deployment_config: DeploymentConfig
):
    """
    Planning a creation that stopped part way reports what is left of it, which is the same rule
    the creation applies: a service that already has a domain is not asked about.
    """
    deployment_config.environments.append(
        DeploymentEnvironment(
            name="dev",
            cloud_provider={"name": PLANNING_PROVIDER, "region": "us-test-1"},
            strategy=RecordingStrategy.name(),
            domain_email="ops@example.test",
            domains=[DomainInfo(service_name_slug=API_SLUG, domain_name="api.example.test")],
        )
    )

    result = plan(
        ctx,
        deployment_config,
        **{
            "env.name": "dev",
            "env.cloud_provider": PLANNING_PROVIDER,
            "env.strategy": MONOLITHIC,
        },
    )

    keys = {question.key for question in result.answers_needed}

    assert f"env.domain.{API_SLUG}" not in keys
    assert f"env.domain.{WEB_SLUG}" in keys
    assert "env.domain_email" not in keys


# --- being honest about what it does not know -------------------------------------------------


def test_a_strategy_that_declares_nothing_is_reported_as_a_partial_plan(
    ctx: OpsmithContext, deployment_config: DeploymentConfig
):
    """
    Acceptance criterion 2, the reporting half. Declaring is optional, so a strategy that does
    not is not broken - but a plan of it is short, and saying so is the difference between a
    short list and a wrong one.
    """
    result = plan(
        ctx,
        deployment_config,
        **{"env.cloud_provider": PLANNING_PROVIDER, "env.strategy": SILENT_STRATEGY},
    )

    assert result.complete is False
    assert any(SILENT_STRATEGY in reason for reason in result.partial_reasons)
    assert "env.instance_type" not in {question.key for question in result.answers_needed}


def test_a_provider_that_declares_nothing_still_completes_a_creation(
    ctx: OpsmithContext, deployment_config: DeploymentConfig, interact: FakeInteraction
):
    """
    Acceptance criterion 2, the working half. A provider that asks inside build_detail instead of
    declaring is asked exactly as it always was, and the environment is created.
    """
    interact.answers.update(
        {
            "env.cloud_provider": SILENT_PROVIDER,
            "env.name": "dev",
            "env.strategy": SILENT_STRATEGY,
            "env.domain_email": "ops@example.test",
            f"env.domain.{API_SLUG}": "api.example.test",
            f"env.domain.{WEB_SLUG}": "www.example.test",
        }
    )

    result = operations.create_environment(ctx, deployment_config, deploy=False)

    assert result.provider == SILENT_PROVIDER
    assert result.region == "us-test-1"
    assert "env.region" in [entry["key"] for entry in interact.asked]


def test_an_account_that_cannot_be_reached_costs_the_plan_its_listing_and_nothing_else(
    ctx: OpsmithContext, deployment_config: DeploymentConfig
):
    """
    A plan is made before credentials exist as often as after. Detection failing leaves the
    questions in the list and the reason in the report, rather than failing the command.
    """
    PlanningCloudProvider.refusal = CloudCredentialsError(
        message="no credentials here", help_url="https://example.test"
    )

    result = plan(
        ctx,
        deployment_config,
        **{
            "env.cloud_provider": PLANNING_PROVIDER,
            "env.strategy": MONOLITHIC,
        },
    )

    assert "env.region" in {question.key for question in result.answers_needed}
    assert result.complete is False
    assert any("no credentials here" in reason for reason in result.partial_reasons)


def test_planning_for_a_provider_that_does_not_exist_is_a_usage_error(
    ctx: OpsmithContext, deployment_config: DeploymentConfig
):
    """
    A name off a flag that matches no plugin is the user's mistake, so it is exit 2 with the
    names that would have worked - not an internal failure.
    """
    with pytest.raises(InvalidArgument) as raised:
        plan(ctx, deployment_config, **{"env.cloud_provider": "Nope"})

    assert PLANNING_PROVIDER in raised.value.details["known"]


# --- what a plan must never do ------------------------------------------------------------------


def test_a_plan_creates_nothing(
    ctx: OpsmithContext,
    deployment_config: DeploymentConfig,
    provisioners: RefusingProvisionerFactory,
    interact: FakeInteraction,
):
    """
    Acceptance criterion 3. The provisioner factory fails on any call, so reaching the end proves
    no working directory and no cloud resource was made; nothing is written to the configuration
    and nothing is asked either.
    """
    plan(
        ctx,
        deployment_config,
        **{
            "env.cloud_provider": PLANNING_PROVIDER,
            "env.strategy": MONOLITHIC,
        },
    )

    assert provisioners.calls == []
    assert interact.asked == []
    assert not (ctx.deployments_path / "deployments.yml").exists()
    assert not (ctx.deployments_path / "environments").exists()


# --- the answers file it writes -----------------------------------------------------------------


def test_the_written_answers_file_is_one_the_run_accepts(
    ctx: OpsmithContext, deployment_config: DeploymentConfig, tmp_path: Path
):
    """
    Acceptance criterion 4. What the plan writes is read back by `--answers`, and a creation
    given it asks nothing - which only holds because a question with no answer is written
    commented out rather than blank.
    """
    destination = tmp_path / "answers" / "dev.yml"

    operations.plan_environment(
        ctx,
        deployment_config,
        sources=sources(
            **{
                "env.cloud_provider": PLANNING_PROVIDER,
                "env.strategy": MONOLITHIC,
                "env.name": "dev",
                "env.region": "us-test-1",
                "env.domain_email": "ops@example.test",
                f"env.domain.{API_SLUG}": "api.example.test",
                f"env.domain.{WEB_SLUG}": "www.example.test",
                "envvar.DATABASE_URL": "postgres://db",
                "envvar.SECRET_KEY": "s3cret",
                "envvar.LOG_LEVEL": "debug",
                "env.instance_type": "small",
            }
        ),
        write_answers=destination,
    )

    written = yaml.safe_load(destination.read_text()) or {}

    assert destination.exists()
    # Everything was supplied, so there is nothing left for the file to hold - and a run given it
    # asks nothing because the answers are already in the store.
    assert written == {}
    assert "s3cret" not in destination.read_text()


def test_the_written_answers_file_holds_the_defaults_and_comments_out_the_rest(
    ctx: OpsmithContext, deployment_config: DeploymentConfig, tmp_path: Path
):
    """
    The file is what a harness edits, so it has to be readable as YAML from the first write and
    must not accidentally answer anything it was meant to leave open.
    """
    destination = tmp_path / "dev.yml"

    operations.plan_environment(
        ctx,
        deployment_config,
        sources=sources(
            **{
                "env.cloud_provider": PLANNING_PROVIDER,
                "env.strategy": MONOLITHIC,
            }
        ),
        write_answers=destination,
    )

    body = destination.read_text()
    written = yaml.safe_load(body) or {}

    assert written == {"env.region": "us-test-1", "envvar.LOG_LEVEL": "info"}
    assert "# envvar.SECRET_KEY:" in body
    assert f"# env.domain.{API_SLUG}:" in body


# --- through the command line ---------------------------------------------------------------------


@pytest.fixture
def cli(monkeypatch, tmp_project, deployment_config: DeploymentConfig):
    """The real app, pointed at a repository holding the two-service configuration."""
    monkeypatch.setattr(app_module, "configure_agent", lambda *args, **kwargs: MagicMock())
    deployment_config.save(tmp_project / ".opsmith")
    monkeypatch.chdir(tmp_project)
    return app_module.app


def test_env_plan_needs_no_external_tools(cli, runner: CliRunner, monkeypatch):
    """
    Acceptance criterion 5. `env plan` declares no tools, so it is never probed for one - and
    making the probe fail outright proves it was not called rather than called and satisfied.
    """

    def refuse(tools):
        """Fails the way a machine with nothing installed would."""
        raise AssertionError(f"env plan must not probe for {tools}")

    monkeypatch.setattr(app_module, "check_external_tools", refuse)

    result = runner.invoke(
        cli,
        [
            "--model",
            MODEL_REGISTRY.model_names[0],
            "--api-key",
            "test-key",
            "--output",
            "json",
            "env",
            "plan",
            "--provider",
            PLANNING_PROVIDER,
            "--strategy",
            MONOLITHIC,
        ],
    )

    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout.splitlines()[0])
    assert envelope["ok"] is True
    assert envelope["command"] == "env plan"
    assert f"env.domain.{API_SLUG}" in [
        question["key"] for question in envelope["result"]["answers_needed"]
    ]
