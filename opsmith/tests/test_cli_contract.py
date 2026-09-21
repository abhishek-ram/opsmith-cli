"""Tests for the CLI contract: the error hierarchy, the exit codes and the JSON envelope."""

import json
import sys
from unittest.mock import patch

import pytest
import rich
import typer
from typer.testing import CliRunner

from opsmith.cli import app as app_module
from opsmith.cli.app import handle_errors
from opsmith.cli.interaction import TerminalInteraction
from opsmith.cli.output import JsonRenderer, TextRenderer
from opsmith.core import errors, operations
from opsmith.core.errors import (
    EXIT_CODES,
    CloudCredentialsError,
    InvalidConfig,
    LlmGaveUp,
    OpsmithError,
)
from opsmith.core.results import EnvironmentSummary, EnvListResult, RunResult
from opsmith.models import MODEL_REGISTRY
from opsmith.tests.conftest import hint_of


def _all_error_classes():
    """Yields every OpsmithError subclass declared in opsmith.core.errors, plus the base."""
    for value in vars(errors).values():
        if isinstance(value, type) and issubclass(value, OpsmithError):
            yield value


def test_every_error_class_has_an_exit_code():
    """
    Walks every error class declared in core.errors and asserts its code is mapped in
    EXIT_CODES, so no error can be raised without a defined exit code.
    """
    for error_class in _all_error_classes():
        assert error_class.code in EXIT_CODES, f"{error_class.__name__} has an unmapped code"


def test_exit_codes_match_the_cli_contract():
    """
    Asserts the code-to-exit-code map is exactly the table in the migration plan, for the
    codes declared so far. The recipe, template, capacity and remote state codes belong to
    later phases and are not here yet.
    """
    assert EXIT_CODES == {
        "INTERNAL": 1,
        "INVALID_CONFIG": 2,
        "INVALID_ARGUMENT": 2,
        "UNKNOWN_ENVIRONMENT": 2,
        "UNKNOWN_SERVICE": 2,
        "INTERACTION_CANCELLED": 3,
        "MISSING_ANSWER": 3,
        "TERRAFORM_FAILED": 4,
        "ANSIBLE_FAILED": 4,
        "DOCKER_FAILED": 4,
        "DEPLOY_UNHEALTHY": 4,
        "EDIT_REQUIRED": 4,
        "CLOUD_CREDENTIALS": 5,
        "CLOUD_PERMISSION": 5,
        "LLM_GAVE_UP": 6,
        "PENDING_ACTION": 8,
    }


@pytest.fixture
def probe_app(monkeypatch, tmp_project):
    """
    Builds a Typer app that uses the real callback and the real error handler, with a set of
    probe commands that fail in each of the ways the contract describes.

    Building the agent, the one thing the callback does that needs a configured model, is
    stubbed out. The probe commands declare no external tools, so nothing looks for docker.
    """
    monkeypatch.setattr(app_module, "configure_agent", lambda *args, **kwargs: object())
    monkeypatch.chdir(tmp_project)

    def succeeds(ctx: typer.Context):
        """A command that returns normally."""

    def prints_like_a_core_module(ctx: typer.Context):
        """A command that prints through rich, as the core modules still do until part 0b."""
        rich.print("[bold]progress from somewhere in the core[/bold]")

    def raises_invalid_config(ctx: typer.Context):
        """A command that raises an error mapped to exit code 2."""
        raise InvalidConfig("bad config", hint="fix it", details={"path": "deployments.yml"})

    def raises_cloud_credentials(ctx: typer.Context):
        """A command that raises an error mapped to exit code 5."""
        raise CloudCredentialsError("no credentials", "https://example.test/creds")

    def raises_llm_gave_up(ctx: typer.Context):
        """A command that raises an error mapped to exit code 6."""
        raise LlmGaveUp("the model produced nothing usable")

    def cancels_a_prompt(ctx: typer.Context):
        """A command whose user interrupts a question, through the terminal interaction.

        It builds that interaction rather than using the run's, because a test runner has no
        terminal and the run therefore chose the headless one. What is under test here is the
        route from a cancelled prompt to an exit code, not how the implementation is chosen.
        """
        interact = TerminalInteraction(ctx.obj.renderer)
        with patch("opsmith.cli.interaction.inquirer.prompt", side_effect=KeyboardInterrupt):
            interact.ask("app.name", "Enter the application name")

    def raises_value_error(ctx: typer.Context):
        """A command that raises an exception opsmith does not anticipate."""
        raise ValueError("something nobody planned for")

    def exits_non_zero(ctx: typer.Context):
        """A command that aborts with a bare typer.Exit, as the deploy menu does today."""
        raise typer.Exit(code=1)

    def returns_a_typed_result(ctx: typer.Context):
        """A command that returns a result model, as every command does from part 0f."""
        ctx.obj.context.interact.notify(
            "Your site is live", details={"next_steps": ["point your domain at it"]}
        )
        return operations.reported(
            ctx.obj.context,
            EnvListResult(
                environments=[
                    EnvironmentSummary(
                        name="prod",
                        provider="AWS",
                        region="us-east-1",
                        strategy="Monolithic",
                        deployed=True,
                    )
                ]
            ),
        )

    def exits_with_the_commands_own_code(ctx: typer.Context):
        """A command whose result decides the exit code, as `opsmith run` does."""
        return RunResult(
            environment="prod", service="api", command="ls", exit_code=7, stdout_tail="out"
        )

    def stops_for_a_missing_answer(ctx: typer.Context):
        """A command that reaches a question nothing answered."""
        ctx.obj.context.interact.ask("env.region", "Select a region")

    def stops_for_an_external_action(ctx: typer.Context):
        """A command waiting on something only a person can do, which never happens."""
        ctx.obj.context.interact.wait_for(
            "dns.api",
            "Create the A record for api.example.test",
            check=lambda: False,
            details={"records": [{"type": "A", "name": "api.example.test"}]},
            timeout_s=0,
        )

    probe = typer.Typer(pretty_exceptions_show_locals=False)
    probe.callback()(app_module.main)
    for command in (
        succeeds,
        prints_like_a_core_module,
        raises_invalid_config,
        raises_cloud_credentials,
        raises_llm_gave_up,
        cancels_a_prompt,
        raises_value_error,
        exits_non_zero,
        returns_a_typed_result,
        exits_with_the_commands_own_code,
        stops_for_a_missing_answer,
        stops_for_an_external_action,
    ):
        probe.command()(handle_errors(command))
    return probe


def _invoke(runner: CliRunner, probe_app, command: str, *extra_args: str):
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
            *extra_args,
            command,
        ],
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one document on stdout, got {lines}"
    return result, json.loads(lines[0])


def test_success_writes_one_ok_envelope(runner, probe_app):
    """A command that returns normally exits 0 and writes a single ok envelope on stdout."""
    result, envelope = _invoke(runner, probe_app, "succeeds")

    assert result.exit_code == 0
    assert envelope == {
        "ok": True,
        "command": "succeeds",
        "result": {},
        "warnings": [],
    }


@pytest.mark.parametrize(
    "command, expected_code, expected_exit",
    [
        ("raises-invalid-config", "INVALID_CONFIG", 2),
        ("raises-cloud-credentials", "CLOUD_CREDENTIALS", 5),
        ("raises-llm-gave-up", "LLM_GAVE_UP", 6),
        ("raises-value-error", "INTERNAL", 1),
        ("exits-non-zero", "INTERNAL", 1),
    ],
)
def test_errors_map_to_their_exit_codes(runner, probe_app, command, expected_code, expected_exit):
    """
    Runs one probe command per exit class this part declares and asserts stdout carries a
    single error envelope with the right code, and the process exits with the mapped code.
    """
    result, envelope = _invoke(runner, probe_app, command)

    assert result.exit_code == expected_exit
    assert envelope["ok"] is False
    assert envelope["command"] == command
    assert envelope["error"]["code"] == expected_code
    assert envelope["error"]["message"]


def test_error_envelope_carries_hint_and_details(runner, probe_app):
    """An OpsmithError's hint and details reach the envelope unchanged."""
    _, envelope = _invoke(runner, probe_app, "raises-invalid-config")

    assert envelope["error"]["hint"] == "fix it"
    assert envelope["error"]["details"] == {"path": "deployments.yml"}


def test_unexpected_exception_hides_the_traceback_by_default(runner, probe_app):
    """An unanticipated exception reports INTERNAL without a traceback unless asked."""
    _, envelope = _invoke(runner, probe_app, "raises-value-error")

    assert "traceback" not in envelope["error"]["details"]
    assert "something nobody planned for" in envelope["error"]["message"]


def test_verbose_adds_the_traceback_to_the_details(runner, probe_app):
    """--verbose puts the formatted traceback into the error envelope's details."""
    _, envelope = _invoke(runner, probe_app, "raises-value-error", "--verbose")

    assert "ValueError: something nobody planned for" in envelope["error"]["details"]["traceback"]


def test_cloud_credentials_error_keeps_its_two_argument_constructor():
    """
    The provider modules raise CloudCredentialsError(message, help_url). That signature is
    preserved by the move into the hierarchy, with help_url surfacing as the hint.
    """
    error = CloudCredentialsError("no credentials", "https://example.test/creds")

    assert error.code == "CLOUD_CREDENTIALS"
    assert error.message == "no credentials"
    assert "https://example.test/creds" in hint_of(error)
    assert error.details == {"help_url": "https://example.test/creds"}


def test_json_mode_keeps_rich_output_off_stdout(runner, probe_app):
    """
    A core module printing through rich must not break the single document guarantee. In
    JSON mode the global rich console is redirected, so the print lands on stderr and stdout
    carries the envelope alone.
    """
    result, envelope = _invoke(runner, probe_app, "prints-like-a-core-module")

    assert result.exit_code == 0
    assert envelope["ok"] is True
    assert "progress from somewhere in the core" in result.stderr


def test_text_mode_leaves_rich_output_on_stdout(runner, probe_app):
    """
    Text mode is unchanged: rich still prints to stdout, and no envelope is written, so a
    user at a terminal sees exactly what they saw before the split.
    """
    result = runner.invoke(
        probe_app,
        [
            "--model",
            MODEL_REGISTRY.model_names[0],
            "--api-key",
            "test-key",
            "prints-like-a-core-module",
        ],
    )

    assert result.exit_code == 0
    assert "progress from somewhere in the core" in result.stdout
    assert "{" not in result.stdout


def test_text_mode_stays_quiet_on_a_bare_abort(runner, probe_app):
    """
    A command that raises typer.Exit(1) after printing its own message must not gain a
    second, synthetic line in text mode. The exit code still says it failed.
    """
    result = runner.invoke(
        probe_app,
        [
            "--model",
            MODEL_REGISTRY.model_names[0],
            "--api-key",
            "test-key",
            "exits-non-zero",
        ],
    )

    assert result.exit_code == 1
    assert result.stdout.strip() == ""


def test_every_renderer_implements_the_whole_interface():
    """
    Both renderers must implement every method, so a new reporting path cannot be added to
    one mode and forgotten in the other.
    """
    for renderer_class in (TextRenderer, JsonRenderer):
        assert not getattr(renderer_class, "__abstractmethods__", set())


def test_a_cancelled_prompt_is_reported_as_a_cancellation(runner, probe_app):
    """
    Interrupting a question stops the run with INTERACTION_CANCELLED and exit code 3, naming
    the key that went unanswered, so re-running with that answer is the obvious next move.
    """
    result, envelope = _invoke(runner, probe_app, "cancels-a-prompt")

    assert result.exit_code == 3
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "INTERACTION_CANCELLED"
    assert envelope["error"]["details"]["key"] == "app.name"


def test_a_typed_result_becomes_the_envelopes_result(runner, probe_app):
    """
    A command returns a model, and the envelope carries it as JSON. Before part 0f the result
    field was always an empty object, so everything a command produced reached a driver only as
    prose inside an event message.
    """
    result, envelope = _invoke(runner, probe_app, "returns-a-typed-result")

    assert result.exit_code == 0
    assert envelope["ok"] is True
    assert envelope["result"]["environments"] == [
        {
            "name": "prod",
            "provider": "AWS",
            "region": "us-east-1",
            "strategy": "Monolithic",
            "deployed": True,
        }
    ]


def test_a_result_carries_what_the_run_told_the_user(runner, probe_app):
    """
    Notices and next steps are part of every result, so a driver reading only stdout still gets
    what a terminal user would have watched go past.
    """
    _, envelope = _invoke(runner, probe_app, "returns-a-typed-result")

    assert envelope["result"]["notices"] == [
        {"message": "Your site is live", "details": {"next_steps": ["point your domain at it"]}}
    ]
    assert envelope["result"]["next_steps"] == ["point your domain at it"]


def test_a_command_can_exit_with_a_code_of_its_own(runner, probe_app):
    """
    `opsmith run` exits with whatever the remote command exited with. The envelope still reports
    success, because opsmith did what it was asked - the exit code belongs to the command.
    """
    result, envelope = _invoke(runner, probe_app, "exits-with-the-commands-own-code")

    assert result.exit_code == 7
    assert envelope["ok"] is True
    assert envelope["result"]["exit_code"] == 7


@pytest.fixture
def invoked_as(monkeypatch):
    """
    Makes the process argument vector look like the invocation under test.

    The resume command is built from ``sys.argv``, which under a CliRunner is pytest's own - so
    without this a test would assert that the envelope carries the command that ran the tests.
    """

    def as_if(*argv: str):
        monkeypatch.setattr(sys, "argv", ["opsmith", *argv])

    return as_if


def test_a_missing_answer_envelope_carries_the_command_to_run_again(runner, probe_app, invoked_as):
    """
    Exit 3 is half of the driver loop, and the half that makes it mechanical is the resume
    command: everything already supplied, so only the new answer has to be added.
    """
    invoked_as("--api-key", "secret", "--answer", "app.name=Acme", "stops-for-a-missing-answer")

    result, envelope = _invoke(runner, probe_app, "stops-for-a-missing-answer")

    assert result.exit_code == EXIT_CODES["MISSING_ANSWER"]
    assert envelope["error"]["code"] == "MISSING_ANSWER"
    assert envelope["error"]["details"]["key"] == "env.region"
    assert (
        envelope["error"]["details"]["resume"]
        == "opsmith --answer app.name=Acme stops-for-a-missing-answer"
    )


def test_a_pending_action_envelope_carries_the_resume_command_and_the_details(
    runner, probe_app, invoked_as
):
    """
    Exit 8 is the other half: what has to happen outside opsmith, and the command to run once it
    has. The details carry the records, so nobody has to read the event stream to find them.
    """
    invoked_as("stops-for-an-external-action")

    result, envelope = _invoke(runner, probe_app, "stops-for-an-external-action")

    assert result.exit_code == EXIT_CODES["PENDING_ACTION"]
    assert envelope["error"]["code"] == "PENDING_ACTION"
    assert envelope["error"]["details"]["key"] == "dns.api"
    assert envelope["error"]["details"]["records"] == [{"type": "A", "name": "api.example.test"}]
    assert envelope["error"]["details"]["resume"] == "opsmith stops-for-an-external-action"
