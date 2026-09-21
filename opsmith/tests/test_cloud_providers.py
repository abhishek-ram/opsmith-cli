"""Tests for the three things a cloud provider does: detect, declare, and build a detail.

Each is tested on its own, because splitting them is the point: detection reaches the account and
asks nobody anything, the declaration is data, and building the detail is arithmetic over the two.
The questions are walked through the same :func:`ask_all` a real run uses, so what these assert
about a cancelled prompt is what a person at a terminal would get.
"""

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import NoCredentialsError
from google.auth.credentials import Credentials

from opsmith.cloud_providers.aws import AWSAccountInfo, AWSProvider
from opsmith.cloud_providers.gcp import GCPAccountInfo, GCPProvider
from opsmith.core.context import OpsmithContext
from opsmith.core.errors import CloudCredentialsError, InteractionCancelled
from opsmith.core.interaction import Choice
from opsmith.core.questions import Resolution, ask_all
from opsmith.tests.conftest import FakeInteraction, FakeProvisionerFactory

AWS_REGIONS = [Choice(label="US East (us-east-1)", value="us-east-1")]
GCP_ZONES = ["us-central1-a", "us-central1-b"]


def fake_credentials() -> Credentials:
    """
    :return: A stand-in for Google credentials that is still a Credentials, because the account
        model holds the real type and a bare mock would be accepted by neither.
    """
    return MagicMock(spec=Credentials)


class CancellingInteraction(FakeInteraction):
    """Answers every question the way a user pressing Ctrl-C does."""

    def ask(self, key, message, **kwargs):
        """:raises InteractionCancelled: Always."""
        raise InteractionCancelled(key, message)

    def select(self, key, message, choices, **kwargs):
        """:raises InteractionCancelled: Always."""
        raise InteractionCancelled(key, message)


def _context(tmp_path, events, interact) -> OpsmithContext:
    """
    :param tmp_path: pytest's per-test temporary directory.
    :param events: The recording sink.
    :param interact: How the run reaches a person.
    :return: A context that touches neither a repository nor a cloud.
    """
    return OpsmithContext(
        src_dir=tmp_path,
        deployments_path=tmp_path / ".opsmith",
        events=events,
        interact=interact,
        provisioner_factory=FakeProvisionerFactory(),
    )


@pytest.fixture
def ctx(tmp_path, events) -> OpsmithContext:
    """A context whose user answers with whatever the question offered."""
    return _context(tmp_path, events, FakeInteraction())


@pytest.fixture
def cancelling_ctx(tmp_path, events) -> OpsmithContext:
    """A context whose user cancels whatever they are asked."""
    return _context(tmp_path, events, CancellingInteraction())


# --- AWS --------------------------------------------------------------------------------------


def test_aws_detection_reads_the_account_and_asks_nothing(ctx):
    """
    Detecting an AWS account finds the account id and the plugin path from the SDK and the PATH,
    and asks no question at all - which is what lets `env plan` call it.
    """
    with patch("opsmith.cloud_providers.aws.shutil.which", return_value="/usr/bin/ssm"):
        with patch("opsmith.cloud_providers.aws.boto3.client") as client:
            client.return_value.get_caller_identity.return_value = {"Account": "123456789012"}
            account = AWSProvider.detect_account(ctx)

    assert account == AWSAccountInfo(account_id="123456789012", ssm_plugin="/usr/bin/ssm")
    assert ctx.interact.asked == []


def test_aws_detection_without_the_session_manager_plugin_is_a_credentials_error(ctx):
    """A missing session-manager-plugin is reported with the link that installs it."""
    with patch("opsmith.cloud_providers.aws.shutil.which", return_value=None):
        with pytest.raises(CloudCredentialsError) as raised:
            AWSProvider.detect_account(ctx)

    assert "session-manager-plugin" in raised.value.message
    assert "session-manager" in raised.value.help_url


def test_aws_declares_the_region_and_lists_it_from_the_account():
    """
    AWS declares exactly one question, the region, and its loader is the live region listing.
    """
    questions = AWSProvider.questions()

    assert [question.key for question in questions] == ["env.region"]
    assert questions[0].primitive == "select"
    assert questions[0].asked_by == "AWS"

    region_choices = questions[0].choices
    assert region_choices is not None
    with patch.object(AWSProvider, "get_regions", return_value=AWS_REGIONS):
        assert region_choices(Resolution()) == AWS_REGIONS


def test_aws_builds_its_detail_from_an_answers_mapping(ctx):
    """
    Building the detail is arithmetic over what was detected and what was answered, so it can be
    tested without a cloud, a prompt or a run.
    """
    account = AWSAccountInfo(account_id="123456789012", ssm_plugin="/usr/bin/ssm")

    detail = AWSProvider.build_detail(ctx, account, {"env.region": "us-east-1"})

    assert detail.name == "AWS"
    assert detail.account_id == "123456789012"
    assert detail.ssm_plugin == "/usr/bin/ssm"
    assert detail.region == "us-east-1"


def test_cancelling_the_aws_region_is_not_a_credentials_error(cancelling_ctx):
    """
    Cancelling the region prompt used to fall into the catch-all that reports broken AWS
    credentials, sending the user to the wrong documentation. It reports the cancellation now,
    which is what splitting the SDK guard away from the question is for.
    """
    with patch.object(AWSProvider, "get_regions", return_value=AWS_REGIONS):
        with pytest.raises(InteractionCancelled) as raised:
            ask_all(cancelling_ctx.interact, AWSProvider.questions(), Resolution())

    assert raised.value.details["key"] == "env.region"


def test_aws_credentials_failures_are_still_reported_as_such(ctx):
    """
    Narrowing the catch-all must not stop it catching what it was written for: an SDK call that
    fails for want of credentials is still a CloudCredentialsError with the help URL.
    """
    with patch("opsmith.cloud_providers.aws.shutil.which", return_value="/usr/bin/ssm"):
        with patch("opsmith.cloud_providers.aws.boto3.client") as client:
            client.return_value.get_caller_identity.side_effect = NoCredentialsError()
            with pytest.raises(CloudCredentialsError) as raised:
                AWSProvider.detect_account(ctx)

    assert raised.value.help_url


def test_a_failing_aws_region_listing_is_a_credentials_error():
    """
    The loader is guarded too, so a region listing that fails for want of credentials reports the
    same thing detection would, rather than whatever boto3 raised.
    """
    with patch.object(AWSProvider, "get_regions", side_effect=NoCredentialsError()):
        with pytest.raises(CloudCredentialsError):
            AWSProvider.region_choices(Resolution())


# --- GCP --------------------------------------------------------------------------------------


def test_gcp_detection_returns_the_credentials_and_asks_nothing(ctx):
    """Detecting a GCP account authenticates and hands back the credentials, asking nothing."""
    credentials = fake_credentials()
    with patch("opsmith.cloud_providers.gcp.google.auth.default", return_value=(credentials, None)):
        account = GCPProvider.detect_account(ctx)

    assert account.credentials is credentials
    assert ctx.interact.asked == []


def test_gcp_declares_project_region_and_zone_in_dependency_order():
    """
    GCP's three questions each depend on the ones before: the region list belongs to a project,
    and the zone list to a region. Declaring that is what lets a plan say which to answer first.
    """
    questions = {question.key: question for question in GCPProvider.questions()}

    assert list(questions) == ["env.project_id", "env.region", "env.zone"]
    assert questions["env.region"].depends_on == ("env.project_id",)
    assert questions["env.zone"].depends_on == ("env.project_id", "env.region")


def test_the_gcp_zone_loader_recommends_the_first_zone():
    """
    The zone used to be taken silently as the first of the region's zones. It is a question now,
    and the first zone is the recommended option, so pressing enter still gives the old answer.
    """
    resolution = Resolution(
        answers={"env.project_id": "proj", "env.region": "us-central1"},
        account=GCPAccountInfo(credentials=fake_credentials()),
    )

    with patch.object(GCPProvider, "get_zones", return_value=GCP_ZONES):
        choices = GCPProvider.zone_choices(resolution)

    assert choices is not None
    assert [choice.value for choice in choices] == GCP_ZONES
    assert [choice.recommended for choice in choices] == [True, False]


def test_the_gcp_loaders_return_nothing_without_an_account():
    """
    Both GCP listings need credentials. Without an account they report that they cannot enumerate
    rather than failing, which is what lets a plan run before anybody has authenticated.
    """
    resolution = Resolution(answers={"env.project_id": "proj", "env.region": "us-central1"})

    assert GCPProvider.region_choices(resolution) is None
    assert GCPProvider.zone_choices(resolution) is None


def test_a_region_with_no_zones_is_a_credentials_error():
    """A region GCP lists no zones for is reported as such, not as an empty choice list."""
    resolution = Resolution(
        answers={"env.project_id": "proj", "env.region": "us-central1"},
        account=GCPAccountInfo(credentials=fake_credentials()),
    )

    with patch.object(GCPProvider, "get_zones", return_value=[]):
        with pytest.raises(CloudCredentialsError) as raised:
            GCPProvider.zone_choices(resolution)

    assert "us-central1" in raised.value.message


def test_gcp_builds_its_detail_from_an_answers_mapping(ctx):
    """Building the GCP detail reads the three answers and nothing else."""
    account = GCPAccountInfo(credentials=fake_credentials())

    detail = GCPProvider.build_detail(
        ctx,
        account,
        {"env.project_id": "proj", "env.region": "us-central1", "env.zone": "us-central1-a"},
    )

    assert detail.name == "GCP"
    assert detail.project_id == "proj"
    assert detail.region == "us-central1"
    assert detail.zone == "us-central1-a"


def test_cancelling_the_gcp_project_is_not_a_credentials_error(cancelling_ctx):
    """The GCP provider had the same catch-all, and reports a cancellation as one too."""
    with pytest.raises(InteractionCancelled) as raised:
        ask_all(cancelling_ctx.interact, GCPProvider.questions(), Resolution())

    assert raised.value.details["key"] == "env.project_id"


def test_gcp_credentials_failures_are_still_reported_as_such(ctx):
    """Missing application default credentials are still the credentials error, with its link."""
    from google.auth.exceptions import DefaultCredentialsError

    with patch(
        "opsmith.cloud_providers.gcp.google.auth.default",
        side_effect=DefaultCredentialsError("nope"),
    ):
        with pytest.raises(CloudCredentialsError) as raised:
            GCPProvider.detect_account(ctx)

    assert "cloud.google.com" in raised.value.help_url
