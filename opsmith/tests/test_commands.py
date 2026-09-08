"""Tests for how the commands read the shared state the callback builds."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from opsmith.cli import app as app_module
from opsmith.cli.output import OutputFormat
from opsmith.cli.state import CliState
from opsmith.core.context import OpsmithContext
from opsmith.utils import ExternalToolReport


@pytest.fixture
def cli(monkeypatch, tmp_project):
    """
    Returns the real Typer app with the two steps that need a deployment machine stubbed out:
    building the agent, and probing for docker and terraform.
    """
    monkeypatch.setattr(app_module, "configure_agent", lambda *args, **kwargs: MagicMock())
    monkeypatch.setattr(
        app_module, "check_external_tools", lambda tools: ExternalToolReport(missing=[])
    )
    monkeypatch.chdir(tmp_project)
    return app_module.app


def _base_args(*extra: str):
    """Builds the global options every invocation needs, plus any extras."""
    from opsmith.models import MODEL_REGISTRY

    return ["--model", MODEL_REGISTRY.model_names[0], "--api-key", "test-key", *extra]


def test_callback_builds_the_state_from_the_global_options(cli, runner, tmp_project):
    """
    The callback resolves src_dir, the deployments path and every global option onto a
    CliState that the command reads through ctx.obj.
    """
    captured = {}

    def probe(ctx: typer.Context):
        """Records the state the callback built."""
        captured["state"] = ctx.obj

    registered_before = list(cli.registered_commands)
    cli.command()(app_module.handle_errors(probe))
    try:
        result = runner.invoke(cli, _base_args("--verbose", "--wait-timeout", "42", "probe"))
    finally:
        cli.registered_commands = registered_before

    assert result.exit_code == 0
    state: CliState = captured["state"]
    assert isinstance(state, CliState)
    assert isinstance(state.context, OpsmithContext)
    assert state.context.src_dir == tmp_project
    assert state.context.deployments_path == tmp_project / ".opsmith"
    assert state.context.verbose is True
    assert state.wait_timeout == 42
    assert state.output is OutputFormat.TEXT
    assert state.context.agent is not None
    assert state.context.provisioner_factory is not None
    assert state.context.events is state.renderer


def _run_setup_capturing_the_detector(cli, runner, *extra_args: str):
    """
    Runs setup far enough to build the detector, then aborts at the first prompt.

    :param cli: The Typer app under test.
    :param runner: The CLI runner.
    :param extra_args: Global options to pass before the command.
    :return: The patched ServiceDetector class, to read its call arguments from.
    """
    with patch("opsmith.cli.commands.setup.ServiceDetector") as detector_class:
        with patch("opsmith.cli.commands.setup.DeploymentConfig") as config_class:
            config_class.load.return_value = None
            with patch("opsmith.cli.commands.setup.inquirer") as inquirer_module:
                # Abort at the application name prompt, before anything calls the model.
                inquirer_module.prompt.return_value = None
                runner.invoke(cli, _base_args(*extra_args, "setup"))
    return detector_class


def test_setup_passes_verbose_through_to_the_detector(cli, runner):
    """
    --verbose reaches ServiceDetector on the context, which forwards it to RepoMap. Before the
    split the flag existed but setup never passed it on, so detection was never verbose.
    """
    detector_class = _run_setup_capturing_the_detector(cli, runner, "--verbose")

    assert detector_class.call_args.kwargs["ctx"].verbose is True


def test_setup_defaults_to_quiet_detection(cli, runner):
    """Without --verbose the detector is built quiet, as it always was."""
    detector_class = _run_setup_capturing_the_detector(cli, runner)

    assert detector_class.call_args.kwargs["ctx"].verbose is False


def test_repomap_reads_verbose_from_the_state(cli, runner):
    """
    repomap used to reach for ctx.parent.params["verbose"], which broke whenever the command
    was called without a parent context. It reads the shared state now.
    """
    with patch("opsmith.cli.commands.analyze.RepoMap") as repo_map_class:
        repo_map_class.return_value.get_repo_map.return_value = "a map"
        result = runner.invoke(cli, _base_args("--verbose", "repomap"))

    assert result.exit_code == 0
    assert repo_map_class.call_args.kwargs["ctx"].verbose is True
    assert "a map" in result.stdout


def test_repomap_returns_the_map_in_the_json_envelope(cli, runner):
    """In JSON mode the map is the command's payload, so it belongs inside the envelope."""
    with patch("opsmith.cli.commands.analyze.RepoMap") as repo_map_class:
        repo_map_class.return_value.get_repo_map.return_value = "a map"
        result = runner.invoke(cli, _base_args("--output", "json", "repomap"))

    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0]) == {
        "ok": True,
        "command": "repomap",
        "result": {"repo_map": "a map"},
        "warnings": [],
    }


def test_deploy_without_a_configuration_reports_a_failure(cli, runner: CliRunner):
    """
    Running deploy before setup aborts with typer.Exit(1) as it always has, which the handler
    now reports as a failed envelope rather than a bare exit.
    """
    result = runner.invoke(cli, _base_args("--output", "json", "deploy"))

    envelope = json.loads([line for line in result.stdout.splitlines() if line.strip()][0])
    assert result.exit_code == 1
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "INTERNAL"


def test_not_a_git_repository_exits_two(cli, runner, tmp_path: Path, monkeypatch):
    """
    Pointing --src-dir at a directory outside a repository fails with INVALID_ARGUMENT and
    exit code 2. It used to print a message and exit 0.
    """
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()

    result = runner.invoke(
        cli, _base_args("--src-dir", str(plain_dir), "--output", "json", "repomap")
    )

    envelope = json.loads([line for line in result.stdout.splitlines() if line.strip()][0])
    assert result.exit_code == 2
    assert envelope["error"]["code"] == "INVALID_ARGUMENT"
    assert envelope["error"]["details"]["src_dir"] == str(plain_dir)
