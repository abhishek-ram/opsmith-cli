"""Tests for the cloud providers' account details, where the user is asked for a region."""

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import NoCredentialsError

from opsmith.cloud_providers.aws import AWSProvider
from opsmith.cloud_providers.gcp import GCPProvider
from opsmith.core.context import OpsmithContext
from opsmith.core.errors import CloudCredentialsError, InteractionCancelled
from opsmith.core.interaction import Choice
from opsmith.tests.conftest import FakeInteraction, FakeProvisionerFactory


class CancellingInteraction(FakeInteraction):
    """Answers every question the way a user pressing Ctrl-C does."""

    def ask(self, key, message, **kwargs):
        """:raises InteractionCancelled: Always."""
        raise InteractionCancelled(key, message)

    def select(self, key, message, choices, **kwargs):
        """:raises InteractionCancelled: Always."""
        raise InteractionCancelled(key, message)


@pytest.fixture
def ctx(tmp_path, events) -> OpsmithContext:
    """A context whose user cancels whatever they are asked."""
    return OpsmithContext(
        src_dir=tmp_path,
        deployments_path=tmp_path / ".opsmith",
        events=events,
        interact=CancellingInteraction(),
        provisioner_factory=FakeProvisionerFactory(),
    )


def test_cancelling_the_aws_region_is_not_a_credentials_error(ctx):
    """
    Cancelling the region prompt used to fall into the catch-all that reports broken AWS
    credentials, sending the user to the wrong documentation. It reports the cancellation now.
    """
    with patch("opsmith.cloud_providers.aws.shutil.which", return_value="/usr/bin/ssm"):
        with patch("opsmith.cloud_providers.aws.boto3.client") as client:
            client.return_value.get_caller_identity.return_value = {"Account": "123456789012"}
            with patch.object(
                AWSProvider, "get_regions", return_value=[Choice(label="a", value="a")]
            ):
                with pytest.raises(InteractionCancelled) as raised:
                    AWSProvider.get_account_details(ctx)

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
                AWSProvider.get_account_details(ctx)

    assert raised.value.help_url


def test_cancelling_the_gcp_project_is_not_a_credentials_error(ctx):
    """The GCP provider had the same catch-all, and reports a cancellation as one too."""
    with patch("opsmith.cloud_providers.gcp.google.auth.default", return_value=(MagicMock(), None)):
        with pytest.raises(InteractionCancelled) as raised:
            GCPProvider.get_account_details(ctx)

    assert raised.value.details["key"] == "env.project_id"
