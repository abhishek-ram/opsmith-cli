"""Tests for ``opsmith dockerfile validate``.

The interesting thing about this command is not that it runs docker, it is what it does with what
docker said. A build that fails is an error the caller must fix; a container that exits because
the database it wants does not exist yet is not, and the model is what tells the two apart. Both
paths are exercised here, with docker faked at the boundary where it is actually run, so the
result building, the truncation and the judging all happen for real.
"""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml
from typer.testing import CliRunner

from opsmith.cli import app as app_module
from opsmith.models import MODEL_REGISTRY
from opsmith.service_detector import (
    LOG_TAIL_LINES,
    DockerfileValidation,
    ServiceDetector,
)

CONFIG = {
    "app_name": "Demo App",
    "app_name_slug": "demo-app",
    "services": [
        {
            "name_slug": "api",
            "language": "python",
            "language_version": "3.12",
            "service_type": "BACKEND_API",
            "framework": "fastapi",
            "service_port": 8000,
            "env_vars": [],
        },
        {
            "name_slug": "worker",
            "language": "python",
            "language_version": "3.12",
            "service_type": "BACKEND_WORKER",
            "framework": "celery",
            "env_vars": [],
        },
        {
            "name_slug": "web",
            "language": "javascript",
            "language_version": "22",
            "service_type": "FRONTEND",
            "framework": "react",
            "build_cmd": "npm run build",
            "build_dir": "dist",
            "env_vars": [],
        },
    ],
    "infra_deps": [],
    "environments": [],
}


class FakeAgent:
    """Stands in for the model that judges a failed build.

    It is only ever asked one thing - whether a docker failure is the Dockerfile's fault - so it
    holds one verdict and counts how often it was consulted. A test that expects docker to have
    succeeded asserts that count is zero.
    """

    def __init__(self, verdict: DockerfileValidation):
        self.verdict = verdict
        self.calls = 0

    def run_sync(self, *args, **kwargs):
        """
        :return: A response shaped like the agent's, carrying the scripted verdict.
        """
        self.calls += 1
        return SimpleNamespace(output=self.verdict, new_messages=lambda: [])


class FakeDocker:
    """The two docker commands a validation runs, scripted by outcome.

    It stands where ``_run_command_with_streaming_output`` stands, so everything the detector does
    with the exit codes and the output still runs: a build that fails here fails the way a real one
    does, and the run is never attempted, exactly as docker would leave it.
    """

    def __init__(
        self,
        *,
        build_rc: int = 0,
        run_rc: int = 0,
        timed_out: bool = False,
        build_lines: int = 3,
    ):
        self.build_rc = build_rc
        self.run_rc = run_rc
        self.timed_out = timed_out
        self.build_lines = build_lines
        self.commands: List[List[str]] = []

    def as_method(self):
        """
        Wraps this fake in a plain function, so that replacing a method still binds ``self``.

        A callable object assigned to a class attribute is not a descriptor: the detector would
        never be passed, and the arguments would all arrive one place to the left.

        :return: A function to put in place of ``_run_command_with_streaming_output``.
        """

        def run(detector, command: List[str], timeout: Optional[int] = None):
            return self(command, timeout)

        return run

    def __call__(self, command: List[str], timeout: Optional[int] = None):
        """
        :param command: The docker command being run.
        :param timeout: How long it would be given.
        :return: The exit code, the output, and whether it timed out.
        """
        self.commands.append(command)
        if command[1] == "build":
            output = "\n".join(f"build line {number}" for number in range(self.build_lines))
            return self.build_rc, output, False
        return self.run_rc, "run line 1\nrun line 2", self.timed_out


@pytest.fixture
def docker() -> FakeDocker:
    """Docker, doing what it is told to do."""
    return FakeDocker()


@pytest.fixture
def agent() -> FakeAgent:
    """The model, with nothing to say until a test gives it a verdict."""
    return FakeAgent(DockerfileValidation(is_successful=False, reason="The base image is wrong."))


@pytest.fixture
def cli(monkeypatch, tmp_project: Path, docker: FakeDocker, agent: FakeAgent):
    """The real application, with docker and the model replaced and nothing else."""
    monkeypatch.setattr(app_module, "configure_agent", lambda *args, **kwargs: agent)
    monkeypatch.setattr(ServiceDetector, "_run_command_with_streaming_output", docker.as_method())
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        ),
    )
    monkeypatch.chdir(tmp_project)
    return app_module.app


def _write_config(project: Path, config: Optional[Dict] = None) -> Path:
    """
    :param project: The project directory.
    :param config: What to write, or the standard three-service configuration.
    :return: The path written.
    """
    path = project / ".opsmith" / "deployments.yml"
    path.write_text(yaml.dump(config if config is not None else CONFIG))
    return path


def _write_dockerfile(project: Path, slug: str) -> Path:
    """
    :param project: The project directory.
    :param slug: The service the Dockerfile belongs to.
    :return: The path written.
    """
    path = project / ".opsmith" / "docker" / slug / "Dockerfile"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('FROM python:3.12-slim\nCMD ["python", "-m", "http.server"]\n')
    return path


def _invoke(runner: CliRunner, cli, *args: str) -> Tuple[Any, Dict]:
    """
    Runs a command in JSON mode and returns the result and the one envelope it wrote.

    :param runner: The CLI runner.
    :param cli: The application.
    :param args: The command and its options.
    :return: What the runner saw, and the parsed envelope.
    """
    result = runner.invoke(
        cli,
        [
            "--model",
            MODEL_REGISTRY.model_names[0],
            "--api-key",
            "test-key",
            "--output",
            "json",
            *args,
        ],
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one document on stdout, got {lines}"
    return result, json.loads(lines[0])


def test_a_dockerfile_that_builds_and_runs_passes(runner, cli, tmp_project, agent, docker):
    """
    A clean build and a clean run report ok, and the model is never consulted.

    The model is only there to explain a failure, so a validation that had nothing to explain must
    not spend a request on it.
    """
    _write_config(tmp_project)
    _write_dockerfile(tmp_project, "api")

    result, envelope = _invoke(runner, cli, "dockerfile", "validate", "--service", "api")

    assert result.exit_code == 0
    assert envelope["ok"] is True
    assert envelope["result"]["ok"] is True
    check = envelope["result"]["checks"][0]
    assert (check["build_ok"], check["run_ok"], check["dockerfile_at_fault"]) == (True, True, None)
    assert agent.calls == 0
    assert [command[1] for command in docker.commands] == ["build", "run"]


def test_a_dockerfile_that_does_not_build_exits_four(runner, cli, tmp_project, docker, agent):
    """
    A build the model blames on the Dockerfile is an error, with the tails in the details.

    Exit 4 is the band for an external tool failing, and a Dockerfile that will not build is
    docker failing rather than the configuration being wrong.
    """
    docker.build_rc = 1
    _write_config(tmp_project)
    _write_dockerfile(tmp_project, "api")

    result, envelope = _invoke(runner, cli, "dockerfile", "validate", "--service", "api")

    assert result.exit_code == 4
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "DOCKER_FAILED"
    assert "api" in envelope["error"]["message"]
    assert "dockerfile validate" in envelope["error"]["hint"]

    check = envelope["error"]["details"]["checks"][0]
    assert check["build_ok"] is False
    assert check["run_ok"] is None, "the run is never attempted when the build failed"
    assert check["dockerfile_at_fault"] is True
    assert check["explanation"] == "The base image is wrong."
    assert "build line" in check["build_tail"]
    assert agent.calls == 1


def test_a_failure_the_model_excuses_is_not_a_failure(runner, cli, tmp_project, docker, agent):
    """
    A container that exits for a reason outside the Dockerfile succeeds, and says why.

    This is the case a harness must not try to fix: the image is fine, and the service simply has
    nothing to talk to yet. It exits 0 so that a driver does not start editing a correct file.
    """
    docker.run_rc = 1
    agent.verdict = DockerfileValidation(
        is_successful=True, reason="It exits because DATABASE_URL is unset, which is expected."
    )
    _write_config(tmp_project)
    _write_dockerfile(tmp_project, "api")

    result, envelope = _invoke(runner, cli, "dockerfile", "validate", "--service", "api")

    assert result.exit_code == 0
    assert envelope["result"]["ok"] is True
    check = envelope["result"]["checks"][0]
    assert (check["build_ok"], check["run_ok"]) == (True, False)
    assert check["dockerfile_at_fault"] is False
    assert any("not at fault" in notice["message"] for notice in envelope["result"]["notices"])


def test_the_tails_are_the_last_lines_only(runner, cli, tmp_project, docker, agent):
    """A result carries the end of a long build log, not the whole of it."""
    docker.build_rc = 1
    docker.build_lines = LOG_TAIL_LINES * 3
    _write_config(tmp_project)
    _write_dockerfile(tmp_project, "api")

    _, envelope = _invoke(runner, cli, "dockerfile", "validate", "--service", "api")

    tail = envelope["error"]["details"]["checks"][0]["build_tail"].splitlines()
    assert len(tail) == LOG_TAIL_LINES
    assert tail[-1] == f"build line {LOG_TAIL_LINES * 3 - 1}"


def test_every_service_is_checked_when_none_is_named(runner, cli, tmp_project, docker):
    """
    Without --service, every service built from a Dockerfile is checked, in configuration order.

    A frontend is built on the machine that runs a release rather than into an image, so it has no
    Dockerfile to check and is not one of them.
    """
    _write_config(tmp_project)
    _write_dockerfile(tmp_project, "api")
    _write_dockerfile(tmp_project, "worker")

    _, envelope = _invoke(runner, cli, "dockerfile", "validate")

    assert [check["service"] for check in envelope["result"]["checks"]] == ["api", "worker"]


def test_an_unknown_service_is_a_usage_error(runner, cli, tmp_project):
    """A slug the configuration does not declare exits 2, and says which ones it does."""
    _write_config(tmp_project)
    _write_dockerfile(tmp_project, "api")

    result, envelope = _invoke(runner, cli, "dockerfile", "validate", "--service", "nope")

    assert result.exit_code == 2
    assert envelope["error"]["code"] == "UNKNOWN_SERVICE"
    assert envelope["error"]["details"]["known"] == ["api", "worker", "web"]


def test_a_frontend_service_cannot_be_validated(runner, cli, tmp_project):
    """A service that is not built into an image exits 2, naming the ones that are."""
    _write_config(tmp_project)
    _write_dockerfile(tmp_project, "api")

    result, envelope = _invoke(runner, cli, "dockerfile", "validate", "--service", "web")

    assert result.exit_code == 2
    assert envelope["error"]["code"] == "INVALID_ARGUMENT"
    assert envelope["error"]["details"]["buildable"] == ["api", "worker"]


def test_a_named_service_without_a_dockerfile_names_the_path(runner, cli, tmp_project):
    """Asking for a service whose Dockerfile has not been written yet says where to write it."""
    _write_config(tmp_project)

    result, envelope = _invoke(runner, cli, "dockerfile", "validate", "--service", "api")

    assert result.exit_code == 2
    assert envelope["error"]["code"] == "INVALID_ARGUMENT"
    assert "docker/api/Dockerfile" in envelope["error"]["details"]["dockerfile"]


def test_validating_nothing_is_never_a_success(runner, cli, tmp_project):
    """
    With no Dockerfile anywhere, the command fails rather than reporting that all is well.

    A validator that validated nothing and said ok would be read by a driver as permission to
    deploy.
    """
    _write_config(tmp_project)

    result, envelope = _invoke(runner, cli, "dockerfile", "validate")

    assert result.exit_code == 2
    assert envelope["error"]["code"] == "INVALID_ARGUMENT"
    assert "no Dockerfile" in envelope["error"]["message"]


def test_the_container_watch_is_what_timeout_changes(runner, cli, tmp_project, monkeypatch):
    """
    --timeout lengthens the run watch and leaves the build ceiling alone.

    Shortening the build would turn a slow but correct build into a failure, which is the worst
    thing a validator can do.
    """
    seen: List[Tuple[str, Optional[int]]] = []

    def record(detector, command: List[str], timeout: Optional[int] = None):
        seen.append((command[1], timeout))
        return 0, "", False

    monkeypatch.setattr(ServiceDetector, "_run_command_with_streaming_output", record)
    _write_config(tmp_project)
    _write_dockerfile(tmp_project, "api")

    _invoke(runner, cli, "dockerfile", "validate", "--service", "api", "--timeout", "300")

    assert seen == [("build", 30 * 60), ("run", 300)]
