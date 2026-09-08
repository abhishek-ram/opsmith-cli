import shutil
from typing import TYPE_CHECKING, Literal, Type

import boto3
import botocore.session
import inquirer
from botocore.exceptions import ClientError, NoCredentialsError
from pydantic import Field

from opsmith.cloud_providers.base import (
    BaseCloudProvider,
    BaseCloudProviderDetail,
    CpuArchitectureEnum,
    MachineType,
    MachineTypeList,
)
from opsmith.core.errors import CloudCredentialsError
from opsmith.core.events import STEP_VM, EventSink

if TYPE_CHECKING:
    from opsmith.core.context import OpsmithContext


class AWSCloudDetail(BaseCloudProviderDetail):
    name: Literal["AWS"] = Field(default="AWS", description="Provider name, 'AWS'")
    account_id: str = Field(..., description="AWS Account ID.")
    ssm_plugin: str = Field(..., description="Path to session-manager-plugin executable.")


class AWSProvider(BaseCloudProvider):
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
    def get_regions(events: EventSink) -> list[tuple[str, str]]:
        """
        Retrieves a list of available AWS regions with their display names.

        :param events: Sink to report the wait on the AWS API to.
        :return: (display name, region code) pairs, sorted by code.
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
                regions.append((f"{description} ({code})", code))

            return sorted(regions, key=lambda x: x[1])

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
    def get_account_details(cls, ctx: "OpsmithContext") -> AWSCloudDetail:
        """
        Retrieves structured AWS account details.

        :param ctx: The run's context, for reporting the wait on the AWS API.
        :return: The account, region and session-manager plugin path to deploy with.
        """
        try:
            ssm_plugin_path = shutil.which("session-manager-plugin")
            if not ssm_plugin_path:
                raise CloudCredentialsError(
                    message=(
                        "'session-manager-plugin' not found. Please install the AWS Session Manager"
                        " plugin."
                    ),
                    help_url="https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html",
                )

            sts_client = boto3.client("sts")
            identity = sts_client.get_caller_identity()
            account_id = identity.get("Account")
            if not account_id:
                raise CloudCredentialsError(
                    message=(
                        "AWS account ID could not be determined. This might indicate an issue with"
                        " the credentials or permissions."
                    ),
                    help_url="https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-quickstart.html",
                )

            regions = AWSProvider.get_regions(ctx.events)
            questions = [
                inquirer.List(
                    "region",
                    message="Select an AWS region",
                    choices=regions,
                ),
            ]
            answers = inquirer.prompt(questions)
            if not answers or not answers.get("region"):
                raise ValueError("AWS region selection is required. Aborting.")
            selected_region = answers["region"]

            return AWSCloudDetail(
                account_id=account_id, ssm_plugin=ssm_plugin_path, region=selected_region
            )
        except (NoCredentialsError, ClientError) as e:
            raise CloudCredentialsError(
                message=f"AWS credentials error: {e}",
                help_url=(
                    "https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-quickstart.html"
                ),
            )
        except Exception as e:
            # Catching other unexpected exceptions during AWS interaction
            raise CloudCredentialsError(
                message=f"An unexpected error occurred while fetching AWS account details: {e}",
                help_url=(
                    "https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-quickstart.html"
                ),
            )
