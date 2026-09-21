import shutil
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator, List, Literal, Mapping, Type

import boto3
import botocore.session
from botocore.exceptions import ClientError, NoCredentialsError
from pydantic import Field

from opsmith.cloud_providers.base import (
    AccountInfo,
    BaseCloudProvider,
    BaseCloudProviderDetail,
    CpuArchitectureEnum,
    MachineType,
    MachineTypeList,
)
from opsmith.core.errors import CloudCredentialsError, OpsmithError
from opsmith.core.events import STEP_VM, EventSink
from opsmith.core.interaction import Choice
from opsmith.core.questions import Question, Resolution

if TYPE_CHECKING:
    from opsmith.core.context import OpsmithContext

#: Where a reader is sent when AWS will not answer.
AWS_CREDENTIALS_HELP = (
    "https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-quickstart.html"
)


@contextmanager
def aws_errors(doing: str) -> Iterator[None]:
    """
    Reports an AWS SDK failure as a credentials error, and lets Opsmith's own errors through.

    It wraps each call to AWS separately rather than a whole flow, so that a cancelled prompt in
    between two of them is still reported as a cancellation - which is what it used to not be.

    :param doing: What was being attempted, for the message.
    :raises CloudCredentialsError: The SDK call failed.
    """
    try:
        yield
    except OpsmithError:
        # A cancelled prompt, or anything else Opsmith describes for itself, is reported as what
        # it is. Only the SDK failures below become a credentials error.
        raise
    except (NoCredentialsError, ClientError) as e:
        raise CloudCredentialsError(
            message=f"AWS credentials error: {e}", help_url=AWS_CREDENTIALS_HELP
        )
    except Exception as e:
        raise CloudCredentialsError(
            message=f"An unexpected error occurred while {doing}: {e}",
            help_url=AWS_CREDENTIALS_HELP,
        )


class AWSAccountInfo(AccountInfo):
    """The AWS account a run deploys into, and the plugin it reaches instances through."""

    account_id: str = Field(..., description="AWS Account ID.")
    ssm_plugin: str = Field(..., description="Path to session-manager-plugin executable.")


class AWSCloudDetail(BaseCloudProviderDetail):
    name: Literal["AWS"] = Field(default="AWS", description="Provider name, 'AWS'")
    account_id: str = Field(..., description="AWS Account ID.")
    ssm_plugin: str = Field(..., description="Path to session-manager-plugin executable.")


class AWSProvider(BaseCloudProvider[AWSAccountInfo, AWSCloudDetail]):
    """AWS cloud provider implementation."""

    @classmethod
    def name(cls) -> str:
        """The name of the cloud provider."""
        return "AWS"

    @classmethod
    def description(cls) -> str:
        """A brief description of the cloud provider."""
        return "Amazon Web Services, a comprehensive and broadly adopted cloud platform."

    @classmethod
    def get_detail_model(cls) -> Type[AWSCloudDetail]:
        """The cloud provider detail model."""
        return AWSCloudDetail

    @staticmethod
    def get_regions(events: EventSink) -> List[Choice]:
        """
        Retrieves a list of available AWS regions with their display names.

        :param events: Sink to report the wait on the AWS API to.
        :return: One choice per region, labelled with its description, sorted by code.
        """
        with events.waiting(STEP_VM, "Fetching available regions from AWS Cloud Provider..."):
            # Get available region codes from EC2
            ec2_client = boto3.client("ec2", region_name="us-east-1")
            response = ec2_client.describe_regions()
            available_region_codes = {region["RegionName"] for region in response["Regions"]}

            # Get region descriptions from botocore's packaged data
            session = botocore.session.get_session()
            # The first partition is 'aws' which contains all standard regions
            region_data = session.get_data("endpoints")["partitions"][0]["regions"]

            regions = []
            for code in available_region_codes:
                data = region_data[code]
                description = data.get("description", code.replace("-", " ").title())
                regions.append(Choice(label=f"{description} ({code})", value=code))

            return sorted(regions, key=lambda choice: choice.value)

    def get_instance_types(self) -> MachineTypeList:
        """
        Retrieves a list of available instance types for the given region using heuristics.
        """
        ec2_client = boto3.client("ec2", region_name=self.provider_detail.region)

        # Prioritize newer generation, general-purpose and compute-optimized instance families
        instance_families = [
            "t4g",
            "t3",
            "m7g",
            "m7i",
            "m6g",
            "m6i",
            "m5",
            "c5",
            "c5a",
            "c6g",
            "c6gn",
            "c7g",
            "c7gn",
            "c8g",
            "c8gn",
        ]
        instance_type_patterns = [f"{family}.*" for family in instance_families]

        paginator = ec2_client.get_paginator("describe_instance_types")
        pages = paginator.paginate(
            Filters=[
                {"Name": "instance-type", "Values": instance_type_patterns},
                {"Name": "current-generation", "Values": ["true"]},
            ]
        )

        all_instances = []
        for page in pages:
            for itype in page["InstanceTypes"]:
                if (
                    "VCpuInfo" in itype
                    and "DefaultVCpus" in itype["VCpuInfo"]
                    and "MemoryInfo" in itype
                    and "SizeInMiB" in itype["MemoryInfo"]
                    and "ProcessorInfo" in itype
                    and "SupportedArchitectures" in itype["ProcessorInfo"]
                    and itype["ProcessorInfo"]["SupportedArchitectures"]
                ):
                    instance_arch_str = itype["ProcessorInfo"]["SupportedArchitectures"][0]
                    try:
                        instance_arch = CpuArchitectureEnum(instance_arch_str)
                    except ValueError:
                        continue

                    all_instances.append(
                        MachineType(
                            name=itype["InstanceType"],
                            cpu=itype["VCpuInfo"]["DefaultVCpus"],
                            ram_gb=round(itype["MemoryInfo"]["SizeInMiB"] / 1024, 2),
                            architecture=instance_arch,
                        )
                    )
        sorted_machines = sorted(all_instances, key=lambda m: (m.cpu, m.ram_gb))
        return MachineTypeList(machines=sorted_machines)

    @classmethod
    def detect_account(cls, ctx: "OpsmithContext") -> AWSAccountInfo:
        """
        Checks that AWS can be reached, and reads the two facts that are not a matter of choice.

        :param ctx: The run's context, for reporting the wait on the AWS API.
        :return: The account id and the path to the session manager plugin.
        :raises CloudCredentialsError: The plugin is missing, or AWS refused the caller.
        """
        ssm_plugin_path = shutil.which("session-manager-plugin")
        if not ssm_plugin_path:
            raise CloudCredentialsError(
                message=(
                    "'session-manager-plugin' not found. Please install the AWS Session Manager"
                    " plugin."
                ),
                help_url="https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html",
            )

        with aws_errors("fetching AWS account details"):
            sts_client = boto3.client("sts")
            identity = sts_client.get_caller_identity()

        account_id = identity.get("Account")
        if not account_id:
            raise CloudCredentialsError(
                message=(
                    "AWS account ID could not be determined. This might indicate an issue with"
                    " the credentials or permissions."
                ),
                help_url=AWS_CREDENTIALS_HELP,
            )

        return AWSAccountInfo(account_id=account_id, ssm_plugin=ssm_plugin_path)

    @classmethod
    def questions(cls) -> List[Question]:
        """
        Declares the one thing AWS needs choosing: which region to deploy into.

        :return: The region question, with the live region list as its options.
        """
        return [
            Question(
                key="env.region",
                message="Select an AWS region",
                primitive="select",
                asked_by=cls.name(),
                choices=cls.region_choices,
            )
        ]

    @staticmethod
    def region_choices(resolution: Resolution) -> List[Choice]:
        """
        Lists the regions, for the region question.

        :param resolution: What is known so far, for the sink the wait is reported to.
        :return: One choice per region.
        :raises CloudCredentialsError: AWS would not answer. ``env create`` reports it; ``env
            plan`` tolerates it and says the options are unlisted.
        """
        with aws_errors("listing AWS regions"):
            return AWSProvider.get_regions(resolution.sink)

    @classmethod
    def build_detail(
        cls, ctx: "OpsmithContext", account: AWSAccountInfo, answers: Mapping[str, Any]
    ) -> AWSCloudDetail:
        """
        Assembles what the environment records about AWS.

        :param ctx: The run's context. Unused: every question AWS asks is declared.
        :param account: What :meth:`detect_account` found.
        :param answers: The answers to :meth:`questions`.
        :return: The account, region and session-manager plugin path to deploy with.
        """
        return AWSCloudDetail(
            account_id=account.account_id,
            ssm_plugin=account.ssm_plugin,
            region=answers["env.region"],
        )
