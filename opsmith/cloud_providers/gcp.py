from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator, List, Literal, Mapping, Optional, Type

import google.auth
from google.auth.credentials import Credentials
from google.auth.exceptions import DefaultCredentialsError
from google.cloud import compute_v1
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

#: Where a reader is sent when GCP will not answer.
GCP_CREDENTIALS_HELP = "https://cloud.google.com/docs/authentication/provide-credentials-adc"


@contextmanager
def gcp_errors(doing: str) -> Iterator[None]:
    """
    Reports a GCP SDK failure as a credentials error, and lets Opsmith's own errors through.

    It wraps each call to GCP separately rather than a whole flow, so that a cancelled prompt in
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
    except DefaultCredentialsError as e:
        raise CloudCredentialsError(
            message=f"GCP Application Default Credentials error: {e}",
            help_url=GCP_CREDENTIALS_HELP,
        )
    except Exception as e:
        raise CloudCredentialsError(
            message=f"An unexpected error occurred while {doing}: {e}",
            help_url=GCP_CREDENTIALS_HELP,
        )


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


class GCPAccountInfo(AccountInfo):
    """The credentials a GCP run was authenticated with.

    They are a live SDK object, which is exactly why an account is not written down: the region
    and zone listings need them, and the next run authenticates again.
    """

    credentials: Credentials = Field(..., description="The application default credentials.")


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
    def get_regions(project_id: str, credentials: Credentials, events: EventSink) -> List[Choice]:
        """
        Retrieves a list of available GCP regions using the GCP API.

        :param project_id: The project to list regions for.
        :param credentials: Credentials to call the API with.
        :param events: Sink to report the wait on the GCP API to.
        :return: One choice per region, labelled with its description, sorted by code.
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
                regions.append(Choice(label=f"{name} ({code})", value=code))

            return sorted(regions, key=lambda choice: choice.value)

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
    def detect_account(cls, ctx: "OpsmithContext") -> GCPAccountInfo:
        """
        Authenticates against GCP, without asking anybody anything.

        :param ctx: The run's context, for reporting the wait on the GCP API.
        :return: The credentials the region and zone listings are made with.
        :raises CloudCredentialsError: There are no application default credentials.
        """
        with gcp_errors("reading the GCP application default credentials"):
            credentials, _ = google.auth.default()

        return GCPAccountInfo(credentials=credentials)

    @classmethod
    def questions(cls) -> List[Question]:
        """
        Declares the three things GCP needs choosing, in the order each depends on the last.

        The zone used to be taken silently as the first of the region's zones, which is why
        ``--zone`` existed and did nothing. It is a question now, with the first zone offered as
        the recommendation, so a person keeps the old answer by pressing enter and a driver is
        told the choice exists.

        :return: The project, region and zone questions.
        """
        return [
            Question(
                key="env.project_id",
                message="Enter the GCP project you want to use",
                asked_by=cls.name(),
            ),
            Question(
                key="env.region",
                message="Select a GCP region",
                primitive="select",
                asked_by=cls.name(),
                depends_on=("env.project_id",),
                choices=cls.region_choices,
            ),
            Question(
                key="env.zone",
                message="Select a GCP zone",
                primitive="select",
                asked_by=cls.name(),
                depends_on=("env.project_id", "env.region"),
                choices=cls.zone_choices,
            ),
        ]

    @staticmethod
    def region_choices(resolution: Resolution) -> Optional[List[Choice]]:
        """
        Lists the regions of the project that was just named.

        :param resolution: What is known so far: the project, and the credentials to ask with.
        :return: One choice per region, or None when there is no account to ask with, which is
            what a plan on a machine with no credentials gets.
        :raises CloudCredentialsError: GCP would not answer.
        """
        if resolution.account is None:
            return None

        with gcp_errors("listing GCP regions"):
            return GCPProvider.get_regions(
                resolution.get("env.project_id"),
                resolution.account.credentials,
                resolution.events,
            )

    @staticmethod
    def zone_choices(resolution: Resolution) -> Optional[List[Choice]]:
        """
        Lists the zones of the region that was just chosen, recommending the first.

        :param resolution: What is known so far: the project, the region, and the credentials.
        :return: One choice per zone, or None when there is no account to ask with.
        :raises CloudCredentialsError: GCP would not answer, or the region has no zones.
        """
        if resolution.account is None:
            return None

        region = resolution.get("env.region")
        with gcp_errors("listing GCP zones"):
            zones = GCPProvider.get_zones(
                resolution.get("env.project_id"), region, resolution.account.credentials
            )

        if not zones:
            raise CloudCredentialsError(
                message=f"No zones found for region '{region}'.",
                help_url=GCP_CREDENTIALS_HELP,
            )

        # The first zone was what an older Opsmith took without asking, so it stays the answer a
        # person gets for pressing enter and a driver gets from --accept-defaults.
        return [
            Choice(label=zone, value=zone, recommended=index == 0)
            for index, zone in enumerate(zones)
        ]

    @classmethod
    def build_detail(
        cls, ctx: "OpsmithContext", account: GCPAccountInfo, answers: Mapping[str, Any]
    ) -> GCPCloudDetail:
        """
        Assembles what the environment records about GCP.

        :param ctx: The run's context. Unused: every question GCP asks is declared.
        :param account: What :meth:`detect_account` found.
        :param answers: The answers to :meth:`questions`.
        :return: The project, region and zone to deploy into.
        """
        return GCPCloudDetail(
            project_id=answers["env.project_id"],
            region=answers["env.region"],
            zone=answers["env.zone"],
        )
