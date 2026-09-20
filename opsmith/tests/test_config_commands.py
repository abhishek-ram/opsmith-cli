"""Tests for `opsmith config validate|schema|show`.

These are the commands a coding harness runs before it has a cloud account, so what they must
prove is that they work with the model configured from the environment alone and with no
external tool installed.
"""

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from opsmith.cli import app as app_module
from opsmith.core.llm import MODEL_ENV_VAR
from opsmith.models import MODEL_REGISTRY

VALID_CONFIG = {
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
            "env_vars": [{"key": "DATABASE_URL", "is_secret": True}],
        }
    ],
    "infra_deps": [{"dependency_type": "DATABASE", "provider": "postgresql", "version": "16"}],
    "environments": [],
}


def _write_config(project: Path, config: dict) -> Path:
    """
    Writes a deployment configuration into a project.

    :param project: The project directory.
    :param config: What to write.
    :return: The path written.
    """
    path = project / ".opsmith" / "deployments.yml"
    path.write_text(yaml.dump(config))
    return path


@pytest.fixture
def cli(monkeypatch, tmp_project):
    """Returns the real Typer app with only the agent build stubbed out."""
    monkeypatch.setattr(app_module, "configure_agent", lambda *args, **kwargs: object())
    monkeypatch.chdir(tmp_project)
    return app_module.app


def _invoke(runner: CliRunner, cli, *args: str):
    """Runs a config command in JSON mode and returns (result, parsed envelope)."""
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


def test_validate_accepts_a_good_configuration(runner, cli, tmp_project):
    """A valid file exits 0 and puts the result in the envelope."""
    _write_config(tmp_project, VALID_CONFIG)

    result, envelope = _invoke(runner, cli, "config", "validate")

    assert result.exit_code == 0
    assert envelope["command"] == "config validate"
    assert envelope["result"] == {
        "ok": True,
        "errors": [],
        "warnings": [],
        "notices": [],
        "next_steps": [],
    }


def test_validate_reports_a_schema_error_with_its_path(runner, cli, tmp_project):
    """A field of the wrong type exits 2, with the dotted path to it in the details."""
    broken = json.loads(json.dumps(VALID_CONFIG))
    broken["services"][0]["service_port"] = "not-a-number"
    _write_config(tmp_project, broken)

    result, envelope = _invoke(runner, cli, "config", "validate")

    assert result.exit_code == 2
    assert envelope["error"]["code"] == "INVALID_CONFIG"
    errors = envelope["error"]["details"]["errors"]
    assert errors[0]["path"] == "services.0.service_port"
    assert errors[0]["message"]


def test_validate_catches_the_rules_the_editors_enforce(runner, cli, tmp_project):
    """
    The rules that used to live in the setup editors - an unreplaced 'user_choice' provider and
    a provider listed twice - now also fail a validate run, so a config written by an agent is
    held to the same standard as one edited by hand.
    """
    broken = json.loads(json.dumps(VALID_CONFIG))
    broken["infra_deps"] = [
        {"dependency_type": "DATABASE", "provider": "user_choice"},
        {"dependency_type": "CACHE", "provider": "redis"},
        {"dependency_type": "MESSAGE_QUEUE", "provider": "redis"},
    ]
    _write_config(tmp_project, broken)

    result, envelope = _invoke(runner, cli, "config", "validate")

    assert result.exit_code == 2
    paths = [error["path"] for error in envelope["error"]["details"]["errors"]]
    assert paths == ["infra_deps.0.provider", "infra_deps.2.provider"]


def test_validate_warns_about_an_unusual_provider(runner, cli, tmp_project):
    """A provider that does not match its dependency type is a warning, not a failure."""
    unusual = json.loads(json.dumps(VALID_CONFIG))
    unusual["infra_deps"] = [{"dependency_type": "SEARCH_ENGINE", "provider": "redis"}]
    _write_config(tmp_project, unusual)

    result, envelope = _invoke(runner, cli, "config", "validate")

    assert result.exit_code == 0
    assert envelope["result"]["ok"] is True
    assert envelope["result"]["warnings"][0]["path"] == "infra_deps.0.provider"


def test_validate_takes_a_file_option(runner, cli, tmp_path, tmp_project):
    """--file validates a configuration that is not the project's own."""
    elsewhere = tmp_path / "elsewhere.yml"
    elsewhere.write_text(yaml.dump(VALID_CONFIG))

    result, envelope = _invoke(runner, cli, "config", "validate", "--file", str(elsewhere))

    assert result.exit_code == 0
    assert envelope["result"]["ok"] is True


def test_validate_says_so_when_there_is_no_configuration(runner, cli):
    """A project that has never been set up fails with the command that would fix it."""
    result, envelope = _invoke(runner, cli, "config", "validate")

    assert result.exit_code == 2
    assert envelope["error"]["code"] == "INVALID_CONFIG"
    assert "opsmith setup" in envelope["error"]["hint"]


def test_schema_emits_json_schema(runner, cli):
    """The default format is the JSON Schema pydantic generates from DeploymentConfig."""
    result, envelope = _invoke(runner, cli, "config", "schema")

    assert result.exit_code == 0
    schema = envelope["result"]["schema"]
    assert schema["title"] == "DeploymentConfig"
    assert "services" in schema["properties"]
    assert "ServiceInfo" in schema["$defs"]


def test_schema_renders_markdown_for_the_skill(runner, cli):
    """--format markdown renders the same schema as the document phase 1 will ship."""
    result, envelope = _invoke(runner, cli, "config", "schema", "--format", "markdown")

    assert result.exit_code == 0
    markdown = envelope["result"]["markdown"]
    assert markdown.startswith("# DeploymentConfig")
    assert "## ServiceInfo" in markdown
    assert "| `service_port` |" in markdown


def test_schema_writes_the_document_to_stdout_in_text_mode(runner, cli):
    """
    In text mode the document is the whole point of the command, so it lands on stdout intact
    and `opsmith config schema --format markdown > SCHEMA.md` produces the file.
    """
    result = runner.invoke(
        cli,
        [
            "--model",
            MODEL_REGISTRY.model_names[0],
            "--api-key",
            "test-key",
            "config",
            "schema",
            "--format",
            "markdown",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout.lstrip().startswith("# DeploymentConfig")


def test_show_returns_the_configuration(runner, cli, tmp_project):
    """show reads the file and returns it as Opsmith understands it, defaults filled in."""
    _write_config(tmp_project, VALID_CONFIG)

    result, envelope = _invoke(runner, cli, "config", "show")

    assert result.exit_code == 0
    config = envelope["result"]["config"]
    assert config["app_name"] == "Demo App"
    assert config["infra_deps"][0]["provider"] == "postgresql"


def test_validate_runs_on_a_machine_with_no_tools_and_no_flags(monkeypatch, runner, tmp_project):
    """
    The phase's second acceptance criterion, end to end: the model comes only from the
    environment, no external tool is installed or even probed, and stdout carries exactly one
    envelope. The agent is built for real here, because supplying the key through the
    environment is the thing being proved.
    """
    model_name = MODEL_REGISTRY.model_names[0]
    key_variable = MODEL_REGISTRY.get_model_class(model_name)().api_key_env_var

    def explode(*args, **kwargs):
        raise AssertionError("config validate must not probe for external tools")

    monkeypatch.chdir(tmp_project)
    monkeypatch.setenv(MODEL_ENV_VAR, model_name)
    monkeypatch.setenv(key_variable, "test-key")
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(subprocess, "run", explode)
    _write_config(tmp_project, VALID_CONFIG)

    result = runner.invoke(app_module.app, ["--output", "json", "config", "validate"])

    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert result.exit_code == 0, result.stdout
    assert len(lines) == 1
    assert json.loads(lines[0])["result"]["ok"] is True


def test_help_does_not_need_a_configured_model(runner, monkeypatch, tmp_project):
    """
    Click runs the group callback before it reaches a subcommand's --help, so reading the help
    must not insist on the model the help is there to explain.
    """
    monkeypatch.chdir(tmp_project)
    monkeypatch.delenv("OPSMITH_MODEL", raising=False)

    result = runner.invoke(app_module.app, ["config", "validate", "--help"])

    assert result.exit_code == 0
    assert "--file" in result.stdout
