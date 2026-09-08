from typing import TYPE_CHECKING, Literal, Type

import google.auth
import inquirer
from google.auth.credentials import Credentials
from google.auth.exceptions import DefaultCredentialsError
from google.cloud import compute_v1
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

GCP_REGION_DESCRIPTIONS = {
    "africa-south1": "Johannesburg, South Africa",
    "asia-east1": "Changhua County, Taiwan",
    "asia-east2": "Hong Kong",
    "asia-northeast1": "Tokyo, Japan",
    "asia-northeast2": "Osaka, Japan",
    "asia-northeast3": "Seoul, South Korea",
    "asia-south1": "Mumbai, India",
    "asia-south2": "Delhi, India",
    "asia-southeast1": "Jurong West, Singapore",
    "asia-southeast2": "Jakarta, Indonesia",
    "australia-southeast1": "Sydney, Australia",
    "australia-southeast2": "Melbourne, Australia",
    "europe-central2": "Warsaw, Poland",
    "europe-north1": "Hamina, Finland",
    "europe-southwest1": "Madrid, Spain",
    "europe-west1": "St. Ghislain, Belgium",
    "europe-west2": "London, UK",
    "europe-west3": "Frankfurt, Germany",
    "europe-west4": "Eemshaven, Netherlands",
    "europe-west6": "Zürich, Switzerland",
    "europe-west8": "Milan, Italy",
    "europe-west9": "Paris, France",
    "europe-west12": "Turin, Italy",
    "israel-central1": "Tel Aviv, Israel",
    "me-central1": "Doha, Qatar",
    "me-west1": "Tel Aviv, Israel",
    "northamerica-northeast1": "Montréal, Québec, Canada",
    "northamerica-northeast2": "Toronto, Ontario, Canada",
    "southamerica-east1": "São Paulo, Brazil",
    "southamerica-west1": "Santiago, Chile",
    "us-central1": "Council Bluffs, Iowa, USA",
    "us-east1": "Moncks Corner, South Carolina, USA",
    "us-east4": "Ashburn, Virginia, USA",
    "us-east5": "Columbus, Ohio, USA",
    "us-south1": "Dallas, Texas, USA",
    "us-west1": "The Dalles, Oregon, USA",
    "us-west2": "Los Angeles, California, USA",
    "us-west3": "Salt Lake City, Utah, USA",
    "us-west4": "Las Vegas, Nevada, USA",
}


class GCPCloudDetail(BaseCloudProviderDetail):
    name: Literal["GCP"] = Field(default="GCP", description="Provider name, 'GCP'")
    project_id: str = Field(..., description="GCP Project ID.")
    zone: str = Field(..., description="The GCP zone for this environment.")


class GCPProvider(BaseCloudProvider):
    """GCP cloud provider implementation."""

    @classmethod
    def name(cls) -> str:
        """The name of the cloud provider."""
        return "GCP"

    @classmethod
    def description(cls) -> str:
        """A brief description of the cloud provider."""
        return "Google Cloud Platform, a suite of cloud computing services from Google."

    @classmethod
    def get_detail_model(cls) -> Type[GCPCloudDetail]:
        """The cloud provider detail model."""
        return GCPCloudDetail

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._credentials = None

    def get_credentials(self) -> Credentials:
        """
        Provides the functionality to retrieve cached credentials or obtain default
        Google credentials if none are available.

        :raises google.auth.exceptions.GoogleAuthError: If authentication fails or
           valid credentials could not be obtained.
        :rtype: google.auth.credentials.Credentials
        :return: Returns the cached credentials if available, otherwise retrieves
           and returns the default credentials via Google's authentication library.
        """
        if not self._credentials:
            self._credentials, _ = google.auth.default()
        return self._credentials

    @staticmethod
    def get_regions(
        project_id: str, credentials: Credentials, events: EventSink
    ) -> list[tuple[str, str]]:
        """
        Retrieves a list of available GCP regions using the GCP API.

        :param project_id: The project to list regions for.
        :param credentials: Credentials to call the API with.
        :param events: Sink to report the wait on the GCP API to.
        :return: (display name, region code) pairs, sorted by code.
        """
        with events.waiting(STEP_VM, "Fetching available regions from GCP Cloud Provider..."):
            client = compute_v1.RegionsClient(credentials=credentials)

            request = compute_v1.ListRegionsRequest(project=project_id)
            pager = client.list(request=request)

            regions = []
            for region in pager:
                code = region.name
                name = (
                    GCP_REGION_DESCRIPTIONS.get(code)
                    or region.description
                    or code.replace("-", " ").title()
                )
                regions.append((f"{name} ({code})", code))

            return sorted(regions, key=lambda x: x[1])

    @staticmethod
    def get_zones(project_id: str, region_name: str, credentials: Credentials) -> list[str]:
        """
        Retrieves a list of available GCP zones for a given region.
        """
        client = compute_v1.RegionsClient(credentials=credentials)
        request = compute_v1.GetRegionRequest(project=project_id, region=region_name)
        region_details = client.get(request=request)
        zones = [zone.split("/")[-1] for zone in region_details.zones]
        return sorted(zones)

    def get_instance_types(self) -> MachineTypeList:
        """
        Retrieves a list of available instance types for the given zone using the GCP API.
        """
        client = compute_v1.MachineTypesClient(credentials=self.get_credentials())
        project_id = self.provider_detail.project_id
        zone = self.provider_detail.zone

        request = compute_v1.ListMachineTypesRequest(project=project_id, zone=zone)
        pager = client.list(request=request)

        all_machines = []
        for mtype in pager:
            if mtype.deprecated:
                continue

            # Filter for general-purpose, newer generation instance families
            arch = CpuArchitectureEnum.X86_64
            # Adding arm64 (t2a) support to be consistent with AWS provider
            if mtype.name.startswith(("t2a-", "c4a-")):
                arch = CpuArchitectureEnum.ARM64

            all_machines.append(
                MachineType(
                    name=mtype.name,
                    cpu=mtype.guest_cpus,
                    ram_gb=round(mtype.memory_mb / 1024, 2),
                    architecture=arch,
                )
            )

        if not all_machines:
            raise ValueError(f"Could not find any instance types in zone {zone}.")

        sorted_machines = sorted(all_machines, key=lambda m: (m.cpu, m.ram_gb))
        return MachineTypeList(machines=sorted_machines)

    @classmethod
    def get_account_details(cls, ctx: "OpsmithContext") -> GCPCloudDetail:
        """
        Retrieves structured GCP account details by listing available projects
        and prompting the user for selection.

        :param ctx: The run's context, for reporting the wait on the GCP API.
        :return: The project, region and zone to deploy into.
        """
        try:
            credentials, _ = google.auth.default()

            questions = [
                inquirer.Text(
                    name="project_id",
                    message="Enter the GCP project you want to use",
                ),
            ]

            answers = inquirer.prompt(questions)
            if not answers or not answers.get("project_id"):
                raise ValueError("GCP project selection is required. Aborting.")

            selected_project_id = answers["project_id"]

            regions = cls.get_regions(selected_project_id, credentials, ctx.events)
            region_questions = [
                inquirer.List(
                    "region",
                    message="Select a GCP region",
                    choices=regions,
                ),
            ]
            answers = inquirer.prompt(region_questions)
            if not answers or not answers.get("region"):
                raise ValueError("GCP region selection is required. Aborting.")

            selected_region = answers["region"]

            zones = cls.get_zones(selected_project_id, selected_region, credentials)
            if not zones:
                raise ValueError(f"No zones found for region '{selected_region}'.")

            # For now, just use the first zone.
            selected_zone = zones[0]

            return GCPCloudDetail(
                project_id=selected_project_id, region=selected_region, zone=selected_zone
            )

        except DefaultCredentialsError as e:
            raise CloudCredentialsError(
                message=f"GCP Application Default Credentials error: {e}",
                help_url="https://cloud.google.com/docs/authentication/provide-credentials-adc",
            )
        except Exception as e:
            raise CloudCredentialsError(
                message=f"An unexpected error occurred while fetching GCP project list: {e}",
                help_url="https://cloud.google.com/docs/authentication/provide-credentials-adc",
            )
