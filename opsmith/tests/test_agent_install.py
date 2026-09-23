"""Tests for ``opsmith agent install|uninstall|status``.

Two properties matter more than the rest and are what most of this file is about. Installing twice
must leave no diff, because a project commits what it installs. And uninstalling must remove
exactly what installing wrote and nothing else, because the directories it writes into belong to
somebody else's tool.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
from typer.testing import CliRunner

from opsmith.cli import agent_install
from opsmith.cli import app as app_module
from opsmith.settings import settings


def _install_every_target(runner: CliRunner, cli, *args: str) -> None:
    """
    Installs for every harness in the table, one run each - ``--target`` names only one.

    :param args: Further arguments given to every run.
    """
    for location in agent_install.SKILL_LOCATIONS:
        _invoke(runner, cli, "agent", "install", "--target", location.target, *args)


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """
    A home directory of this test's own, for the user scope.

    :return: Where a user-scope install will write.
    """
    directory = tmp_path / "home"
    directory.mkdir()
    monkeypatch.setattr(agent_install, "user_home", lambda: directory)
    return directory


@pytest.fixture
def cli(monkeypatch, tmp_project: Path, home: Path):
    """The real application, in a project of this test's own."""
    monkeypatch.chdir(tmp_project)
    return app_module.app


def _invoke(runner: CliRunner, cli, *args: str) -> Tuple[Any, Dict]:
    """
    Runs an agent command in JSON mode, with no model configured.

    No ``--model`` and no ``--api-key``: installing a skill must not require either, and a test
    that passed them would not prove it.

    :param runner: The CLI runner.
    :param cli: The application.
    :param args: The command and its options.
    :return: What the runner saw, and the parsed envelope.
    """
    result = runner.invoke(cli, ["--output", "json", *args])
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one document on stdout, got {lines}"
    return result, json.loads(lines[0])


def _tree(directory: Path) -> Dict[str, bytes]:
    """
    :param directory: A directory to describe.
    :return: Every file under it, by relative path, with its contents.
    """
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def test_installing_with_no_model_configured_works(runner, cli, tmp_project):
    """
    ``agent install`` runs without a model, which no other command does.

    It copies files into a harness's skill directory and is the first command a new user runs;
    demanding an API key for it would be absurd. This is the test that stops the ``@no_model`` tag
    being quietly removed.
    """
    result, envelope = _invoke(runner, cli, "agent", "install", "--target", "claude")

    assert result.exit_code == 0
    assert envelope["ok"] is True
    assert (tmp_project / ".claude" / "skills" / "opsmith" / "SKILL.md").is_file()


def test_installing_writes_every_target(runner, cli, tmp_project):
    """
    One run per harness gives each location in the table the whole skill, references and all,
    and the record accumulates every one of them rather than keeping only the last.
    """
    _install_every_target(runner, cli)

    record = json.loads((tmp_project / ".opsmith" / "agent-install.json").read_text())
    recorded = {entry["target"] for entry in record["installs"]}
    assert recorded == {location.target for location in agent_install.SKILL_LOCATIONS}

    for location in agent_install.SKILL_LOCATIONS:
        skill = tmp_project / location.project / "opsmith"
        assert (skill / "SKILL.md").is_file()
        assert (skill / "references" / "commands.md").is_file()


def test_installing_twice_leaves_no_diff(runner, cli, tmp_project):
    """
    The second install writes the same bytes as the first, everywhere, including the record.

    This is what lets a project commit the skill: a re-run after upgrading Opsmith is a real diff,
    and a re-run without one is nothing at all.
    """
    _install_every_target(runner, cli, "--agents-md")
    first = _tree(tmp_project)

    _install_every_target(runner, cli, "--agents-md")

    assert _tree(tmp_project) == first


def test_the_record_carries_no_timestamp(runner, cli, tmp_project):
    """
    Nothing in the record changes between two installs of the same version.

    A timestamp is the obvious thing to record and the one thing that would break the property
    above, so it is asserted directly rather than left to the diff.
    """
    _invoke(runner, cli, "agent", "install", "--target", "claude")

    record = json.loads((tmp_project / ".opsmith" / "agent-install.json").read_text())

    assert record["skill"] == "opsmith"
    assert record["installs"][0]["path"] == ".claude/skills/opsmith"
    assert not any("time" in key or "date" in key for key in record)


def test_the_agents_block_is_written_and_rewritten_in_place(runner, cli, tmp_project):
    """
    ``--agents-md`` writes the managed block, and a second run replaces only what is between the
    markers.

    The text around it is somebody else's, so it has to survive.
    """
    agents = tmp_project / "AGENTS.md"
    agents.write_text("# House rules\n\nRun the tests before pushing.\n")

    _invoke(runner, cli, "agent", "install", "--target", "claude", "--agents-md")
    after_first = agents.read_text()

    assert "# House rules" in after_first
    assert agent_install.BLOCK_START in after_first
    assert "## Opsmith" in after_first

    _invoke(runner, cli, "agent", "install", "--target", "claude", "--agents-md")

    assert agents.read_text() == after_first
    assert after_first.count(agent_install.BLOCK_START) == 1


def test_the_claude_import_line_is_added_once(runner, cli, tmp_project):
    """An existing CLAUDE.md gains the import line, is not rewritten, and never gains it twice."""
    claude = tmp_project / "CLAUDE.md"
    claude.write_text("# This project\n\nSomething a person wrote.\n")

    _invoke(runner, cli, "agent", "install", "--target", "claude", "--agents-md")
    _invoke(runner, cli, "agent", "install", "--target", "claude", "--agents-md")

    text = claude.read_text()
    assert text.startswith("# This project")
    assert text.count(agent_install.IMPORT_LINE) == 1


def test_the_agents_block_needs_the_project_scope(runner, cli):
    """AGENTS.md is a project file, so asking for it with the user scope is a usage error."""
    result, envelope = _invoke(
        runner, cli, "agent", "install", "--target", "claude", "--scope", "user", "--agents-md"
    )

    assert result.exit_code == 2
    assert envelope["error"]["code"] == "INVALID_ARGUMENT"


def test_the_user_scope_writes_outside_the_repository(runner, cli, tmp_project, home):
    """
    A user-scope install writes under the home directory, and records itself outside the project.

    A user install is not about any one project and may happen outside a repository altogether, so
    a record inside one would be in the wrong place and would get committed by somebody.
    """
    _invoke(runner, cli, "agent", "install", "--target", "claude", "--scope", "user")

    assert (home / ".claude" / "skills" / "opsmith" / "SKILL.md").is_file()
    assert not (tmp_project / ".claude").exists()
    assert not (tmp_project / ".opsmith" / "agent-install.json").exists()

    record = Path(settings.state_dir).expanduser() / "agent-install.json"
    assert json.loads(record.read_text())["installs"][0]["scope"] == "user"


def test_uninstalling_removes_what_was_installed_and_nothing_else(runner, cli, tmp_project):
    """
    Uninstalling takes the skill, the block and the import line, and leaves everything else.

    The skills directory belongs to the harness, so a sibling skill has to survive.
    """
    sibling = tmp_project / ".claude" / "skills" / "somebody-else"
    sibling.mkdir(parents=True)
    (sibling / "SKILL.md").write_text("---\nname: somebody-else\n---\n")
    agents = tmp_project / "AGENTS.md"
    agents.write_text("# House rules\n")

    _invoke(runner, cli, "agent", "install", "--target", "claude", "--agents-md")
    result, envelope = _invoke(runner, cli, "agent", "uninstall", "--target", "claude")

    assert result.exit_code == 0
    assert not (tmp_project / ".claude" / "skills" / "opsmith").exists()
    assert (sibling / "SKILL.md").is_file()
    assert agents.read_text().strip() == "# House rules"
    assert not (tmp_project / ".opsmith" / "agent-install.json").exists()
    assert envelope["result"]["removed"][0]["target"] == "claude"


def test_a_directory_that_is_not_ours_is_never_replaced(runner, cli, tmp_project):
    """
    A skill directory belonging to something else stops the install, and says how to proceed.

    The installer removes a destination before writing it, so this is the rule that stops that
    being a way to delete somebody's work.
    """
    foreign = tmp_project / ".claude" / "skills" / "opsmith"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("---\nname: not-opsmith\n---\n")

    result, envelope = _invoke(runner, cli, "agent", "install", "--target", "claude")

    assert result.exit_code == 2
    assert envelope["error"]["code"] == "INVALID_ARGUMENT"
    assert "--force" in envelope["error"]["hint"]
    assert (foreign / "SKILL.md").read_text() == "---\nname: not-opsmith\n---\n"

    result, _ = _invoke(runner, cli, "agent", "install", "--target", "claude", "--force")

    assert result.exit_code == 0
    assert "name: opsmith" in (foreign / "SKILL.md").read_text()


def test_uninstalling_leaves_a_foreign_directory_alone(runner, cli, tmp_project):
    """Without a record, only a directory that says it is ours is removed."""
    foreign = tmp_project / ".claude" / "skills" / "opsmith"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("---\nname: not-opsmith\n---\n")

    _, envelope = _invoke(runner, cli, "agent", "uninstall", "--target", "claude")

    assert envelope["result"]["removed"] == []
    assert foreign.is_dir()


def test_status_reports_an_installed_skill_as_stale_when_the_version_moves_on(
    runner, cli, tmp_project, monkeypatch
):
    """
    ``status`` compares what is installed with what is running, and says which is which.

    That comparison is the whole point of stamping a version into the skill: a project that
    installed the skill two releases ago needs telling.
    """
    _invoke(runner, cli, "agent", "install", "--target", "claude")

    monkeypatch.setattr(agent_install, "package_version", lambda: "99.0.0")
    _, envelope = _invoke(runner, cli, "agent", "status", "--scope", "project")

    claude = next(
        location for location in envelope["result"]["locations"] if location["target"] == "claude"
    )
    assert claude["state"] == "stale"
    assert envelope["result"]["package_version"] == "99.0.0"


def test_installing_without_a_target_writes_nothing(runner, cli, tmp_project):
    """
    A bare install is a usage error, not an install into all six locations.

    Installing writes into directories belonging to other people's tools, and a project that uses
    one harness should not acquire five more because a flag was left off. The error lists what it
    would accept, including the word that means only-what-is-here.
    """
    result, envelope = _invoke(runner, cli, "agent", "install")

    assert result.exit_code == 2
    assert envelope["error"]["code"] == "INVALID_ARGUMENT"
    assert "--target" in envelope["error"]["hint"]
    assert {"claude", "auto"} <= set(envelope["error"]["details"]["known"])
    assert "all" not in envelope["error"]["details"]["known"]
    assert not (tmp_project / ".claude").exists()


def test_uninstalling_without_a_target_removes_everything_it_installed(runner, cli, tmp_project):
    """
    A bare uninstall takes back the whole install, where a bare install refuses to write.

    The asymmetry is the point: uninstall can only remove what the record names and what still
    says it is ours, so there is nothing for it to be careless with.
    """
    _install_every_target(runner, cli)

    _, envelope = _invoke(runner, cli, "agent", "uninstall")

    assert len(envelope["result"]["removed"]) == len(agent_install.SKILL_LOCATIONS)
    for location in agent_install.SKILL_LOCATIONS:
        assert not (tmp_project / location.project / "opsmith").exists()


def test_status_says_which_paths_are_guesses(runner, cli):
    """
    Every location reports whether its path has been verified against the harness.

    The conventions are still moving, so a user has to be able to see which entries Opsmith is
    confident about and which it is not.
    """
    _, envelope = _invoke(runner, cli, "agent", "status")

    verified = {
        location["target"]: location["verified"] for location in envelope["result"]["locations"]
    }
    assert verified["claude"] is True
    assert any(value is False for value in verified.values())


def test_installing_for_an_unknown_harness_is_a_usage_error(runner, cli):
    """A target that is not in the table exits 2 and lists the ones that are."""
    result, envelope = _invoke(runner, cli, "agent", "install", "--target", "emacs")

    assert result.exit_code == 2
    assert envelope["error"]["code"] == "INVALID_ARGUMENT"
    assert "claude" in envelope["error"]["details"]["known"]


def test_auto_installs_only_where_the_harness_already_is(runner, cli, tmp_project):
    """
    ``--target auto`` installs where a harness's own directory exists, plus the shared location.

    It is the answer to not wanting six skill directories in a project that uses one harness.
    """
    (tmp_project / ".cursor").mkdir()

    _, envelope = _invoke(runner, cli, "agent", "install", "--target", "auto")

    installed = {location["target"] for location in envelope["result"]["installed"]}
    assert installed == {"cursor", "agents"}


def test_mcp_writes_nothing_and_says_so(runner, cli, tmp_project):
    """
    ``--mcp`` is a placeholder until there is an MCP server, and must not pretend otherwise.

    It reports rather than failing, because a harness reading the flag in the documentation should
    get an explanation and not an unknown-option error.
    """
    _, envelope = _invoke(runner, cli, "agent", "install", "--target", "claude", "--mcp")

    notices: List[Dict] = envelope["result"]["notices"]
    assert any("MCP" in notice["message"] for notice in notices)
    assert envelope["result"]["next_steps"]
