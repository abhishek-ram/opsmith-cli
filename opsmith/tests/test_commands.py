"""Tests for how the commands read the shared state the callback builds."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from opsmith.cli import app as app_module
from opsmith.cli.interaction import TerminalInteraction
from opsmith.cli.output import OutputFormat
from opsmith.cli.state import CliState
from opsmith.core.context import OpsmithContext
from opsmith.core.errors import EXIT_CODES
from opsmith.core.interaction import HeadlessInteraction
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
    Runs setup far enough to build the detector, then lets it stop at the first question.

    A test runner has no terminal, so the run is headless and nothing answers the application
    name: setup stops there with a missing answer. That is after the detector is built, which is
    all this needs.

    :param cli: The Typer app under test.
    :param runner: The CLI runner.
    :param extra_args: Global options to pass before the command.
    :return: The patched ServiceDetector class, to read its call arguments from.
    """
    with patch("opsmith.cli.commands.setup.ServiceDetector") as detector_class:
        with patch("opsmith.cli.commands.setup.DeploymentConfig") as config_class:
            config_class.load.return_value = None
            result = runner.invoke(cli, _base_args(*extra_args, "setup"))

    assert result.exit_code == EXIT_CODES["MISSING_ANSWER"]
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


def _interaction_of(cli, runner, *extra_args: str):
    """
    Runs a command far enough to build the interaction, and returns the one it built.

    :param cli: The Typer app under test.
    :param runner: The CLI runner.
    :param extra_args: Global options to pass before the command.
    :return: The interaction on the run's context.
    """
    captured = {}

    def probe(ctx: typer.Context):
        """Records how this run was told to reach a person."""
        captured["interact"] = ctx.obj.context.interact

    cli.command()(app_module.handle_errors(probe))
    result = runner.invoke(cli, _base_args(*extra_args, "probe"))
    assert result.exit_code == 0, result.output
    return captured["interact"]


def test_a_run_without_a_terminal_resolves_answers_instead_of_asking(cli, runner):
    """
    A test runner's stdin is not a terminal, which is the same situation as a pipe or a CI job:
    there is nobody to prompt, so answers come from what the run was told.
    """
    assert isinstance(_interaction_of(cli, runner), HeadlessInteraction)


def test_a_run_with_a_terminal_prompts(cli, runner, monkeypatch):
    """
    With somebody there, the questions are put to them.

    The rule that decides this is exercised below; what is under test here is that the callback
    honours it, which a test runner cannot show by having a terminal - it does not have one.
    """
    monkeypatch.setattr("opsmith.cli.app._is_headless", lambda *args: False)

    assert isinstance(_interaction_of(cli, runner), TerminalInteraction)


@pytest.mark.parametrize(
    "non_interactive, output, a_terminal, headless",
    [
        (False, OutputFormat.TEXT, True, False),
        (False, OutputFormat.TEXT, False, True),
        (True, OutputFormat.TEXT, True, True),
        (False, OutputFormat.JSON, True, True),
    ],
    ids=["at a terminal", "stdin redirected", "--non-interactive", "--output json"],
)
def test_what_decides_that_nobody_can_be_asked(non_interactive, output, a_terminal, headless):
    """
    A run is headless when it was told to be, when stdin is not a terminal, or when it is writing
    JSON - because a prompt draws on the stdout that mode reserves for one envelope.
    """
    stdin = _ATerminal() if a_terminal else _APipe()

    assert app_module._is_headless(non_interactive, output, stdin) is headless


class _ATerminal:
    """Stands in for a stdin that somebody is sitting in front of."""

    @staticmethod
    def isatty() -> bool:
        """Reports that there is a terminal."""
        return True


class _APipe:
    """Stands in for a stdin coming from a file, a pipe or a job runner."""

    @staticmethod
    def isatty() -> bool:
        """Reports that there is no terminal."""
        return False


def test_the_resume_command_repeats_the_run_without_repeating_the_key():
    """
    Every stop reports the command to run again, and that string is printed in an error envelope
    on stdout. The answers already supplied are what make the resume worth having; the
    credentials are the one thing that must not travel with them.
    """
    resume = app_module._resume_command(
        [
            "/opt/homebrew/bin/opsmith",
            "--api-key",
            "sk-super-secret",
            "--answer",
            "env.region=us-east-1",
            "--logfire-token=lf-secret",
            "deploy",
        ]
    )

    assert resume == "opsmith --answer env.region=us-east-1 deploy"
