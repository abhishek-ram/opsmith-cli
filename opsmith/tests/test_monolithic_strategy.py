"""Tests for the monolithic strategy, driven entirely by fakes.

One API service plus a postgres dependency is the smallest deployment that exercises every
provisioner the strategy uses: a container registry, an image build, a virtual machine, the
docker setup on it, and the compose stack. The strategy never touches a git repository, a cloud
or a subprocess here, so what it *would* have applied is what the tests assert on.
"""

import base64
import json
from pathlib import Path
from typing import Literal, Type
from unittest.mock import MagicMock, patch

import pytest
import yaml
from pydantic import Field

from opsmith.cloud_providers import CLOUD_PROVIDER_REGISTRY
from opsmith.cloud_providers.base import (
    BaseCloudProvider,
    BaseCloudProviderDetail,
    CpuArchitectureEnum,
    MachineType,
    MachineTypeList,
)
from opsmith.core.context import OpsmithContext
from opsmith.core.errors import AnsibleFailed
from opsmith.deployment_strategies.monolithic import (
    DockerComposeContent,
    DockerComposeLogValidation,
    MonolithicDeploymentStrategy,
)
from opsmith.tests.conftest import (
    FakeGitRepo,
    FakeInteraction,
    FakeProvisionerFactory,
    RecordingSink,
)
from opsmith.types import (
    DependencyTypeEnum,
    DeploymentConfig,
    DeploymentEnvironment,
    DomainInfo,
    EnvVarConfig,
    InfrastructureDependency,
    InfrastructureProviderEnum,
    ServiceInfo,
    ServiceTypeEnum,
)

SERVICE_SLUG = "python_backend_api_1"
REGISTRY_URL = "registry.test/acme-app"
IMAGE_URL = f"{REGISTRY_URL}/{SERVICE_SLUG}:latest"
COMPOSE_CONTENT = "services:\n  api:\n    image: api\n"
ENV_FILE_CONTENT = 'DATABASE_URL="postgres://localhost/app"\n'


class FakeCloudDetail(BaseCloudProviderDetail):
    """The provider detail for the test provider: a region and nothing else."""

    name: Literal["FAKE"] = Field(default="FAKE", description="Provider name, 'FAKE'.")


class FakeCloudProvider(BaseCloudProvider):
    """A cloud provider that answers from memory, so no test needs credentials."""

    @classmethod
    def name(cls) -> str:
        """The name the deployment config refers to this provider by."""
        return "FAKE"

    @classmethod
    def description(cls) -> str:
        """What a menu would show next to the name."""
        return "A provider that exists only in the test suite."

    @classmethod
    def get_detail_model(cls) -> Type[FakeCloudDetail]:
        """The detail model for this provider."""
        return FakeCloudDetail

    @classmethod
    def get_account_details(cls, ctx: OpsmithContext) -> FakeCloudDetail:
        """Returns fixed account details without asking anything."""
        return FakeCloudDetail(region="us-test-1")

    def get_instance_types(self) -> MachineTypeList:
        """Returns two machine types, so selection has something to choose between."""
        return MachineTypeList(machines=[SMALL_MACHINE, LARGE_MACHINE])


SMALL_MACHINE = MachineType(
    name="t4g.small", cpu=2, ram_gb=2.0, architecture=CpuArchitectureEnum.ARM64
)
LARGE_MACHINE = MachineType(
    name="t4g.medium",
    cpu=2,
    ram_gb=4.0,
    architecture=CpuArchitectureEnum.ARM64,
    is_recommended=True,
)


@pytest.fixture(autouse=True)
def registered_provider():
    """Registers the test provider, the way a provider plugin registers itself."""
    CLOUD_PROVIDER_REGISTRY.register(FakeCloudProvider)
    return FakeCloudProvider


@pytest.fixture(autouse=True)
def no_waiting():
    """Removes the fixed sleeps the strategy uses to let cloud resources settle."""
    with patch("opsmith.deployment_strategies.base.time.sleep"):
        with patch("opsmith.deployment_strategies.monolithic.time.sleep"):
            yield


@pytest.fixture
def deployment_config() -> DeploymentConfig:
    """One backend API service that needs a database, with one environment variable."""
    return DeploymentConfig(
        app_name="Acme App",
        app_name_slug="acme-app",
        services=[
            ServiceInfo(
                name_slug=SERVICE_SLUG,
                language="python",
                service_type=ServiceTypeEnum.BACKEND_API,
                service_port=8000,
                env_vars=[
                    EnvVarConfig(
                        key="DATABASE_URL",
                        is_secret=False,
                        default_value="postgres://localhost/app",
                    )
                ],
            )
        ],
        infra_deps=[
            InfrastructureDependency(
                dependency_type=DependencyTypeEnum.DATABASE,
                provider=InfrastructureProviderEnum.POSTGRESQL,
                version="16",
            )
        ],
    )


@pytest.fixture
def environment(deployment_config: DeploymentConfig) -> DeploymentEnvironment:
    """A production environment on the test provider, with a domain for the API."""
    env = DeploymentEnvironment(
        name="prod",
        cloud_provider={"name": "FAKE", "region": "us-test-1"},
        strategy="Monolithic",
        domain_email="ops@example.test",
        domains=[DomainInfo(service_name_slug=SERVICE_SLUG, domain_name="api.example.test")],
    )
    deployment_config.environments.append(env)
    return env


def _fetched_files(env_content: str, compose_content: str) -> str:
    """
    Encodes remote files the way the fetch playbook hands them back.

    :param env_content: The contents of the remote .env file.
    :param compose_content: The contents of the remote docker-compose.yml.
    :return: The base64 blob the strategy decodes.
    """
    encoded = [
        base64.b64encode(content.encode("utf-8")).decode("ascii")
        for content in (env_content, compose_content)
    ]
    return base64.b64encode(json.dumps(encoded).encode("utf-8")).decode("ascii")


@pytest.fixture
def provisioners() -> FakeProvisionerFactory:
    """A provisioner factory scripted with what each step's tooling would have returned."""
    return FakeProvisionerFactory(
        terraform_outputs={
            "container_registry": {"registry_url": REGISTRY_URL},
            "virtual_machine": {
                "public_ip": "203.0.113.10",
                "private_ip": "10.0.0.10",
                "user": "ubuntu",
                "instance_id": "i-0123456789",
            },
        },
        ansible_outputs={
            "docker_build_push": {"image_url": IMAGE_URL},
            "virtual_machine_setup": {},
            "docker_compose_deploy": {
                "docker_logs": base64.b64encode(b"api | listening on 8000").decode("ascii")
            },
            "fetch_remote_files": {
                "fetched_files": _fetched_files(ENV_FILE_CONTENT, COMPOSE_CONTENT)
            },
            "docker_compose_run": {},
        },
    )


@pytest.fixture
def agent() -> MagicMock:
    """
    A model that answers each request by the shape it was asked for.

    One deploy asks for a machine type, a compose file and a verdict on the container logs, so
    dispatching on ``output_type`` is what lets a single fake serve the whole run.
    """
    outputs = {
        MachineTypeList: MachineTypeList(machines=[SMALL_MACHINE, LARGE_MACHINE]),
        DockerComposeContent: DockerComposeContent(
            content=COMPOSE_CONTENT, env_file_content=ENV_FILE_CONTENT
        ),
        DockerComposeLogValidation: DockerComposeLogValidation(is_successful=True),
    }

    def run_sync(prompt, output_type=None, **kwargs):
        response = MagicMock()
        response.output = outputs[output_type]
        response.new_messages.return_value = []
        return response

    agent = MagicMock()
    agent.run_sync.side_effect = run_sync
    return agent


@pytest.fixture
def ctx(
    tmp_path: Path, events: RecordingSink, provisioners, agent, interact: FakeInteraction
) -> OpsmithContext:
    """A context wired to the fakes, with a Dockerfile already generated for the service."""
    deployments_path = tmp_path / ".opsmith"
    dockerfile = deployments_path / "docker" / SERVICE_SLUG / "Dockerfile"
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text("FROM python:3.13\n")

    return OpsmithContext(
        src_dir=tmp_path,
        deployments_path=deployments_path,
        events=events,
        agent=agent,
        provisioner_factory=provisioners,
        interact=interact,
        git_repo=FakeGitRepo(archive_path=tmp_path),
    )


@pytest.fixture
def strategy(ctx: OpsmithContext) -> MonolithicDeploymentStrategy:
    """The strategy under test, with the SSH key lookup stubbed out."""
    strategy = MonolithicDeploymentStrategy(ctx)
    strategy._get_ssh_public_key = lambda: "ssh-ed25519 AAAAfake test@opsmith"
    return strategy


def test_a_strategy_constructs_without_a_repository_on_disk(tmp_path: Path, events):
    """
    Constructing a strategy used to open a git repository, which is why none of this could be
    tested. It takes the context now, and the repository is only opened if something asks.
    """
    ctx = OpsmithContext(
        src_dir=tmp_path / "nowhere",
        deployments_path=tmp_path / "nowhere" / ".opsmith",
        events=events,
        interact=FakeInteraction(),
        provisioner_factory=FakeProvisionerFactory(),
        git_repo=FakeGitRepo(),
    )

    strategy = MonolithicDeploymentStrategy(ctx)

    assert strategy.src_dir == tmp_path / "nowhere"
    assert isinstance(strategy.git_repo, FakeGitRepo)


def test_deploy_runs_the_provisioners_in_order(
    strategy, deployment_config, environment, provisioners
):
    """
    A deploy sets up the registry, builds the image, creates the VM, installs docker on it and
    then brings the compose stack up - in that order, because each step needs the one before it.
    """
    strategy.deploy(deployment_config, environment)

    assert provisioners.actions() == [
        "terraform:copy_template:container_registry",
        "terraform:apply:container_registry",
        "terraform:get_output:container_registry",
        f"ansible:copy_template:{SERVICE_SLUG}",
        f"ansible:run_playbook:{SERVICE_SLUG}",
        "terraform:copy_template:virtual_machine",
        "terraform:apply:virtual_machine",
        "terraform:get_output:virtual_machine",
        "ansible:copy_template:virtual_machine_setup",
        "ansible:run_playbook:virtual_machine_setup",
        "ansible:copy_template:docker_compose_deploy",
        "ansible:run_playbook:docker_compose_deploy",
    ]


def test_deploy_applies_the_expected_terraform_variables(
    strategy, deployment_config, environment, provisioners
):
    """The registry and the VM are created with the variables their templates declare."""
    strategy.deploy(deployment_config, environment)

    registry = provisioners.find("apply", "container_registry")
    assert registry["variables"] == {
        "app_name": "acme-app",
        "registry_name": "acme-app-us-test-1",
        "force_delete": "true",
    }
    assert registry["env_vars"] == {"name": "FAKE", "region": "us-test-1"}

    virtual_machine = provisioners.find("apply", "virtual_machine")
    assert virtual_machine["variables"] == {
        "app_name": "acme-app",
        "environment": "prod",
        "instance_type": "t4g.medium",
        "instance_arch": "arm64",
        "ssh_pub_key": "ssh-ed25519 AAAAfake test@opsmith",
    }
    assert virtual_machine["env_vars"] == {"region": "us-test-1"}


def test_deploy_passes_the_expected_ansible_extra_vars(
    strategy, deployment_config, environment, provisioners, tmp_path
):
    """
    The three playbooks a deploy runs each get the variables their templates read: where the
    build context is, how to reach the VM, and what to write into the compose stack's .env.
    """
    strategy.deploy(deployment_config, environment)

    build = provisioners.find("run_playbook", SERVICE_SLUG)
    assert build["extra_vars"] == {
        "docker_path": str(tmp_path),
        "dockerfile_path": str(tmp_path / ".opsmith" / "docker" / SERVICE_SLUG / "Dockerfile"),
        "image_name_slug": SERVICE_SLUG,
        "image_tag_name": "latest",
        "registry_url": REGISTRY_URL,
        "region": "us-test-1",
    }

    vm_setup = provisioners.find("run_playbook", "virtual_machine_setup")
    assert vm_setup["extra_vars"] == {
        "environment_name": "prod",
        "app_name": "acme-app",
        "region": "us-test-1",
        "cpu": 2,
        "ram_gb": 4.0,
        "instance_type": "t4g.medium",
        "architecture": "arm64",
        "public_ip": "203.0.113.10",
        "private_ip": "10.0.0.10",
        "user": "ubuntu",
        "instance_id": "i-0123456789",
    }

    compose = provisioners.find("run_playbook", "docker_compose_deploy")
    assert compose["extra_vars"]["app_name"] == "acme-app"
    assert compose["extra_vars"]["environment_name"] == "prod"
    assert compose["extra_vars"]["ansible_user"] == "ubuntu"
    assert compose["extra_vars"]["dest_env_file"] == "/home/ubuntu/app/.env"
    assert compose["extra_vars"]["dest_docker_compose"] == "/home/ubuntu/app/docker-compose.yml"
    assert compose["extra_vars"]["registry_host_url"] == "registry.test"
    assert compose["extra_vars"]["env_file_content"] == 'DATABASE_URL="postgres://localhost/app"'
    assert "ops@example.test" in compose["extra_vars"]["traefik_yml_content"]


def test_deploy_asks_for_a_template_per_step(
    strategy, deployment_config, environment, provisioners
):
    """
    Each step copies its own template tree, named after the provider. Call sites pass the
    provider's own casing; normalising it against the lower-case directories on disk is the
    provisioner's job, and test_provisioners.py covers that half.
    """
    strategy.deploy(deployment_config, environment)

    copies = [call for call in provisioners.calls if call["action"] == "copy_template"]
    assert [call["template"] for call in copies] == [
        "container_registry",
        "docker_build_push",
        "virtual_machine",
        "virtual_machine_setup",
        "docker_compose_deploy",
    ]
    assert {call["provider"] for call in copies} == {"FAKE"}


def test_deploy_saves_the_state_and_the_config_snapshot(
    strategy, deployment_config, environment, ctx
):
    """
    The state file records the registry, the machine and a snapshot of what was deployed, which
    is what a later update diffs against.
    """
    strategy.deploy(deployment_config, environment)

    state = yaml.safe_load(
        (ctx.deployments_path / "environments" / "prod" / "state.yml").read_text()
    )
    assert state["registry_url"] == REGISTRY_URL
    assert state["virtual_machine"]["instance_type"] == "t4g.medium"
    assert [service["name_slug"] for service in state["deployed_services"]] == [SERVICE_SLUG]
    assert [dep["provider"] for dep in state["deployed_infra_deps"]] == ["postgresql"]


def test_deploy_reports_progress_without_printing(strategy, deployment_config, environment, events):
    """
    Everything the deploy has to say arrives as events, so the same run renders on a terminal
    or as NDJSON without the strategy knowing which.
    """
    strategy.deploy(deployment_config, environment)

    messages = events.messages()
    assert f"Container registry created. URL: {REGISTRY_URL}" in messages
    assert "Docker setup complete." in messages
    assert "Your website is available at: https://api.example.test" in messages
    assert {event.step for event in events.events} >= {"registry", "build", "vm", "compose", "dns"}


def test_release_reuses_the_machine_and_redeploys(
    strategy, deployment_config, environment, provisioners
):
    """
    A release rebuilds the image against the existing registry and machine, fetches what is on
    the server and redeploys it. It must never create infrastructure.
    """
    strategy.deploy(deployment_config, environment)
    provisioners.calls.clear()

    strategy.release(deployment_config, environment)

    actions = provisioners.actions()
    assert "terraform:apply:virtual_machine" not in actions
    assert "terraform:apply:container_registry" not in actions
    assert actions == [
        f"ansible:copy_template:{SERVICE_SLUG}",
        f"ansible:run_playbook:{SERVICE_SLUG}",
        "ansible:copy_template:fetch_remote_files",
        "ansible:run_playbook:fetch_remote_files",
        "ansible:copy_template:docker_compose_deploy",
        "ansible:run_playbook:docker_compose_deploy",
    ]

    fetch = provisioners.find("run_playbook", "fetch_remote_files")
    assert fetch["extra_vars"]["remote_files"] == [
        "/home/ubuntu/app/.env",
        "/home/ubuntu/app/docker-compose.yml",
    ]


def test_destroy_tears_down_the_machine_and_the_last_registry(
    strategy, deployment_config, environment, provisioners, ctx
):
    """
    Destroying the only environment in a region takes the machine down and then the registry,
    because nothing else is left using it, and removes the environment's directory.
    """
    strategy.deploy(deployment_config, environment)
    provisioners.calls.clear()

    strategy.destroy(deployment_config, environment)

    assert provisioners.actions() == [
        "terraform:destroy:virtual_machine",
        "terraform:destroy:container_registry",
    ]
    assert provisioners.find("destroy", "virtual_machine")["variables"] == {
        "app_name": "acme-app",
        "environment": "prod",
        "instance_type": "t4g.medium",
        "instance_arch": "arm64",
        "ssh_pub_key": "ssh-ed25519 AAAAfake test@opsmith",
    }
    assert not (ctx.deployments_path / "environments" / "prod").exists()
    assert deployment_config.environments == []


def test_destroy_keeps_the_registry_while_another_environment_shares_the_region(
    strategy, deployment_config, environment, provisioners
):
    """The registry is regional, so it survives until the last environment in its region goes."""
    strategy.deploy(deployment_config, environment)
    deployment_config.environments.append(
        DeploymentEnvironment(
            name="staging",
            cloud_provider={"name": "FAKE", "region": "us-test-1"},
            strategy="Monolithic",
        )
    )
    provisioners.calls.clear()

    strategy.destroy(deployment_config, environment)

    assert provisioners.actions() == ["terraform:destroy:virtual_machine"]


def test_a_failed_playbook_raises_an_error_the_cli_can_map(
    strategy, deployment_config, environment, provisioners
):
    """
    A playbook that exits non-zero is an AnsibleFailed carrying the command and the tail of the
    output, which is what the CLI turns into exit code 4 and a machine-readable envelope.
    """
    provisioners.ansible_failures["virtual_machine_setup"] = AnsibleFailed(
        "Ansible command failed with exit code 2.",
        details={"command": "ansible-playbook main.yml", "output_tail": "fatal: unreachable"},
    )

    with pytest.raises(AnsibleFailed) as raised:
        strategy.deploy(deployment_config, environment)

    assert raised.value.code == "ANSIBLE_FAILED"
    assert raised.value.details["output_tail"] == "fatal: unreachable"


def test_a_failed_compose_deploy_is_fed_back_to_the_model(
    strategy, deployment_config, environment, provisioners
):
    """
    The compose loop repairs itself. When the deploy playbook fails, the strategy must catch it
    and hand the output to the model as the container logs, so it can correct the compose file,
    rather than ending the run. That catch is the only place an AnsibleFailed is swallowed.
    """
    environment_state = MagicMock(
        virtual_machine=MagicMock(user="ubuntu"), registry_url=REGISTRY_URL
    )

    logs = strategy._deploy_docker_compose(
        deployment_config, environment, environment_state, ENV_FILE_CONTENT
    )
    assert "listening on 8000" in logs

    provisioners.ansible_failures["docker_compose_deploy"] = AnsibleFailed(
        "Ansible command failed with exit code 2.",
        details={"output_tail": "ERROR: no such image"},
    )

    logs = strategy._deploy_docker_compose(
        deployment_config, environment, environment_state, ENV_FILE_CONTENT
    )

    assert "Ansible playbook execution failed." in logs
    assert "ERROR: no such image" in logs


def test_a_generated_compose_file_that_fails_is_regenerated(
    strategy, deployment_config, environment, provisioners, agent
):
    """
    The full loop around that catch: a first attempt whose containers do not come up sends the
    model back for another compose file, and the second attempt is deployed too.
    """
    verdicts = [
        DockerComposeLogValidation(is_successful=False, reason="the api container exited"),
        DockerComposeLogValidation(is_successful=True),
    ]

    def run_sync(prompt, output_type=None, **kwargs):
        response = MagicMock()
        if output_type is DockerComposeLogValidation:
            response.output = verdicts.pop(0)
        elif output_type is DockerComposeContent:
            response.output = DockerComposeContent(
                content=COMPOSE_CONTENT, env_file_content=ENV_FILE_CONTENT
            )
        else:
            response.output = MachineTypeList(machines=[SMALL_MACHINE, LARGE_MACHINE])
        response.new_messages.return_value = []
        return response

    agent.run_sync.side_effect = run_sync

    strategy._generate_docker_compose(
        deployment_config,
        environment,
        {SERVICE_SLUG: IMAGE_URL},
        MagicMock(
            virtual_machine=MagicMock(user="ubuntu", architecture=CpuArchitectureEnum.ARM64),
            registry_url=REGISTRY_URL,
        ),
    )

    deploys = [
        action for action in provisioners.actions() if action.endswith("docker_compose_deploy")
    ]
    assert deploys.count("ansible:run_playbook:docker_compose_deploy") == 2
    assert not verdicts


def test_deploy_asks_the_user_through_the_interaction_api(
    strategy, deployment_config, environment, interact
):
    """
    A deploy asks three things: which machine to run on, whether the DNS records exist, and
    what each configured environment variable should hold. Each one carries the key a headless
    run answers it by, and the default the code offered.
    """
    strategy.deploy(deployment_config, environment)

    assert [(entry["primitive"], entry["key"]) for entry in interact.asked] == [
        ("select", "env.instance_type"),
        ("confirm", "dns.confirm"),
        ("ask", "envvar.DATABASE_URL"),
    ]

    machine, dns, env_var = interact.asked
    assert [choice.value for choice in machine["choices"]] == [SMALL_MACHINE, LARGE_MACHINE]
    assert dns["details"] == {
        "records": [{"type": "A", "name": "api.example.test", "value": "203.0.113.10"}]
    }
    assert env_var["default"] == "postgres://localhost/app"
