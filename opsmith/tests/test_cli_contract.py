"""Tests for the CLI contract: the error hierarchy, the exit codes and the JSON envelope."""

import json

import pytest
import rich
import typer
from typer.testing import CliRunner

from opsmith.cli import app as app_module
from opsmith.cli.app import handle_errors
from opsmith.cli.output import JsonRenderer, TextRenderer
from opsmith.core import errors
from opsmith.core.errors import (
    EXIT_CODES,
    CloudCredentialsError,
    InvalidConfig,
    LlmGaveUp,
    OpsmithError,
)
from opsmith.models import MODEL_REGISTRY


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
    codes this part declares. Codes 3 and 8 arrive with headless mode in part 0e.
    """
    assert EXIT_CODES == {
        "INTERNAL": 1,
        "INVALID_CONFIG": 2,
        "INVALID_ARGUMENT": 2,
        "UNKNOWN_ENVIRONMENT": 2,
        "UNKNOWN_SERVICE": 2,
        "TERRAFORM_FAILED": 4,
        "ANSIBLE_FAILED": 4,
        "DOCKER_FAILED": 4,
        "DEPLOY_UNHEALTHY": 4,
        "CLOUD_CREDENTIALS": 5,
        "CLOUD_PERMISSION": 5,
        "LLM_GAVE_UP": 6,
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

    def raises_value_error(ctx: typer.Context):
        """A command that raises an exception opsmith does not anticipate."""
        raise ValueError("something nobody planned for")

    def exits_non_zero(ctx: typer.Context):
        """A command that aborts with a bare typer.Exit, as the deploy menu does today."""
        raise typer.Exit(code=1)

    probe = typer.Typer(pretty_exceptions_show_locals=False)
    probe.callback()(app_module.main)
    for command in (
        succeeds,
        prints_like_a_core_module,
        raises_invalid_config,
        raises_cloud_credentials,
        raises_llm_gave_up,
        raises_value_error,
        exits_non_zero,
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
    assert "https://example.test/creds" in error.hint
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
