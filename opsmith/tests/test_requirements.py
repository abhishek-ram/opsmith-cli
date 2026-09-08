"""Tests for the per-command external tool requirements.

The check used to run in the callback for every command, so a machine without docker could not
run anything at all. Each command now declares what it needs, and only what it declares is
probed.
"""

import json
import subprocess

import pytest
import typer
from typer.testing import CliRunner

from opsmith.cli import app as app_module
from opsmith.cli.app import handle_errors
from opsmith.cli.commands import requires
from opsmith.models import MODEL_REGISTRY
from opsmith.utils import ExternalToolReport, check_external_tools

TERRAFORM_JSON = json.dumps({"terraform_version": "1.12.2", "platform": "darwin_arm64"})


class _FakeCompletedProcess:
    """Stands in for what subprocess.run returns, carrying only what the probes read."""

    def __init__(self, stdout: str = ""):
        self.stdout = stdout.encode()


def _fake_run(installed: dict):
    """
    Builds a stand-in for subprocess.run that only knows about the tools it is given.

    :param installed: Maps a command name to the stdout it prints, or to None when running it
        should fail the way a broken tool does.
    :return: A function with subprocess.run's shape.
    """

    def run(command, check=True, capture_output=True):
        name = command[0]
        if name not in installed:
            raise FileNotFoundError(name)
        if installed[name] is None:
            raise subprocess.CalledProcessError(1, command)
        return _FakeCompletedProcess(installed[name])

    return run


def test_the_terraform_version_is_read_from_its_json(monkeypatch):
    """terraform is probed with -json so the version is recorded, not just its presence."""
    monkeypatch.setattr(subprocess, "run", _fake_run({"terraform": TERRAFORM_JSON}))

    report = check_external_tools(["terraform"])

    assert report.missing == []
    assert report.versions == {"terraform": "1.12.2"}


def test_a_terraform_too_old_for_json_still_counts_as_installed(monkeypatch):
    """Only the version is unknown when -json prints something else; the tool is there."""
    monkeypatch.setattr(subprocess, "run", _fake_run({"terraform": "Terraform v0.12.31"}))

    report = check_external_tools(["terraform"])

    assert report.missing == []
    assert report.versions == {}


def test_a_docker_that_is_installed_but_not_running_is_missing(monkeypatch):
    """`docker info` failing means the daemon is down, which is as good as not having docker."""
    monkeypatch.setattr(subprocess, "run", _fake_run({"docker": None}))

    report = check_external_tools(["docker", "terraform"])

    assert report.missing == ["docker", "terraform"]


def test_any_other_tool_is_looked_up_on_the_path(monkeypatch):
    """A tool with no dedicated probe is checked with shutil.which."""
    monkeypatch.setattr("opsmith.utils.shutil.which", lambda name: None)

    assert check_external_tools(["ansible"]).missing == ["ansible"]


@pytest.fixture
def probe_app(monkeypatch, tmp_project):
    """
    Builds a Typer app with the real callback and two probe commands: one that declares no
    external tools and one that declares terraform.
    """
    monkeypatch.setattr(app_module, "configure_agent", lambda *args, **kwargs: object())
    monkeypatch.chdir(tmp_project)

    def needs_nothing(ctx: typer.Context):
        """A command that declares no external tools, as the config commands do."""

    @requires("terraform")
    def needs_terraform(ctx: typer.Context):
        """A command that declares terraform, as deploy does."""
        return {"terraform_version": ctx.obj.context.terraform_version}

    probe = typer.Typer(pretty_exceptions_show_locals=False)
    probe.callback()(app_module.main)
    probe.command()(handle_errors(needs_nothing))
    probe.command()(handle_errors(needs_terraform))
    return probe


def _invoke(runner: CliRunner, probe_app, command: str):
    """Runs a probe command in JSON mode and returns (result, parsed envelope)."""
    result = runner.invoke(
        probe_app,
        [
            "--model",
            MODEL_REGISTRY.model_names[0],
            "--api-key",
            "test-key",
            "--output",
            "json",
            command,
        ],
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one document on stdout, got {lines}"
    return result, json.loads(lines[0])


def test_a_command_that_declares_nothing_is_never_probed(monkeypatch, runner, probe_app):
    """
    The point of the change: with an empty PATH and every probe rigged to explode, a command
    that declares no tools still runs. This is what lets `config validate` work on a machine
    with neither docker nor terraform.
    """

    def explode(*args, **kwargs):
        raise AssertionError("a command that declares no tools must not probe for any")

    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr("opsmith.utils.shutil.which", explode)

    result, envelope = _invoke(runner, probe_app, "needs-nothing")

    assert result.exit_code == 0
    assert envelope["ok"] is True


def test_a_missing_declared_tool_is_an_invalid_argument(monkeypatch, runner, probe_app):
    """A command whose tool is missing fails before its body, with the install hint."""
    monkeypatch.setattr(subprocess, "run", _fake_run({}))

    result, envelope = _invoke(runner, probe_app, "needs-terraform")

    assert result.exit_code == 2
    assert envelope["error"]["code"] == "INVALID_ARGUMENT"
    assert "terraform" in envelope["error"]["message"]
    assert "PATH" in envelope["error"]["hint"]
    assert envelope["error"]["details"]["missing"] == ["terraform"]


def test_the_terraform_version_reaches_the_context(monkeypatch, runner, probe_app):
    """
    The version parsed by the check is recorded on the context, so a later step can read it
    without probing terraform a second time.
    """
    monkeypatch.setattr(subprocess, "run", _fake_run({"terraform": TERRAFORM_JSON}))

    result, envelope = _invoke(runner, probe_app, "needs-terraform")

    assert result.exit_code == 0
    assert envelope["result"] == {"terraform_version": "1.12.2"}


def test_the_check_is_skipped_when_nothing_is_declared():
    """check_external_tools asked for nothing probes nothing and reports nothing."""
    report = check_external_tools([])

    assert report == ExternalToolReport(missing=[], versions={})
