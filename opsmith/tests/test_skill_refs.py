"""The Agent Skill says what the code says.

Two of the skill's references are generated from the Typer application and the pydantic models, so
a command, flag, exit code or schema field that changes without a regenerate must fail here rather
than reach a harness as a lie. The prose half cannot be generated, so it is checked a different
way: every command and flag it quotes has to resolve in the application.
"""

import re
import tomllib
from pathlib import Path
from typing import Dict, List, Set

import pytest
import typer.main
import yaml

from opsmith.cli import agent_install
from opsmith.cli.app import app
from opsmith.cli.skill_refs import (
    GENERATED_REFERENCES,
    SKILL_REFERENCES_DIR,
    generate,
    walk_commands,
)
from opsmith.core.config import validate_config_data

REGENERATE = "Run: uv run python scripts/build_skill_refs.py"

SKILL_DIR = agent_install.PACKAGE_SKILL_DIR
SKILL_MD = SKILL_DIR / "SKILL.md"

#: The hand-written half. Everything else under references/ is generated.
HAND_WRITTEN = ("ownership.md", "workflows.md")

#: What wraps the part of the skill that names what a later release will add. Commands inside it
#: are deliberately ones this release does not have.
FUTURE_START = "<!-- opsmith:future-start -->"
FUTURE_END = "<!-- opsmith:future-end -->"

#: Words that follow "opsmith" in prose rather than naming a command.
NOT_COMMANDS = {"--output", "--answer", "--verbose", "--model", "--api-key"}


def _frontmatter(text: str) -> Dict:
    """
    :param text: The contents of a SKILL.md.
    :return: What its frontmatter declares.
    """
    _, _, rest = text.partition("---")
    document, _, _ = rest.partition("\n---")
    return yaml.safe_load(document)


def _without_future(text: str) -> str:
    """
    :param text: A skill document.
    :return: The same, without the region that names what this release does not have.
    """
    before, _, rest = text.partition(FUTURE_START)
    _, _, after = rest.partition(FUTURE_END)
    return before + after


def _command_paths() -> Set[str]:
    """
    :return: Every command the application answers to, as the path a user types.
    """
    root = typer.main.get_command(app)
    return {path for path, _ in walk_commands(root)}


def _flags_of_app() -> Dict[str, Set[str]]:
    """
    :return: The flags of each command, plus the global ones under the empty path.
    """
    root = typer.main.get_command(app)
    flags: Dict[str, Set[str]] = {"": {opt for param in root.params for opt in param.opts or []}}
    for path, command in walk_commands(root):
        flags[path] = {opt for param in command.params for opt in param.opts or []}
    return flags


@pytest.mark.parametrize("name", sorted(GENERATED_REFERENCES))
def test_the_committed_reference_matches_the_generator(name: str):
    """
    Each generated reference on disk is byte for byte what the generator produces now.

    This is what makes a schema or flag change without a regenerate a CI failure.
    """
    expected = generate(app)[name]
    path = SKILL_REFERENCES_DIR / name

    assert path.is_file(), f"{name} has never been generated. {REGENERATE}"
    assert path.read_text(encoding="utf-8") == expected, f"{name} is out of date. {REGENERATE}"


def test_every_reference_is_accounted_for():
    """
    The references directory holds the generated set and the hand-written set, and nothing else.

    A renderer added without its file, a file left behind by a renderer that was removed, and a
    reference shipped empty by a phase that cannot generate it yet all fail here.
    """
    present = {path.name for path in SKILL_REFERENCES_DIR.glob("*.md")}

    assert present == set(GENERATED_REFERENCES) | set(HAND_WRITTEN)


def test_no_reference_is_empty():
    """Every reference holds something; a generator that wrote nothing is a silent failure."""
    for path in sorted(SKILL_REFERENCES_DIR.glob("*.md")):
        assert path.read_text(encoding="utf-8").strip(), f"{path.name} is empty"


def test_the_skill_declares_the_version_of_the_package():
    """
    The skill's frontmatter carries the version in ``pyproject.toml``.

    Two committed files are compared, rather than reading the installed distribution, so a
    checkout whose environment has not been re-synced does not fail for a reason that is not real.
    """
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parent.parent.parent / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )

    declared = _frontmatter(SKILL_MD.read_text(encoding="utf-8"))["metadata"]["version"]

    assert declared == pyproject["project"]["version"]


def test_the_skill_frontmatter_is_well_formed():
    """
    The frontmatter carries what a harness needs to load the skill, and the name it installs as.

    The name has to equal the directory the installer writes, or the harness will not find it.
    """
    frontmatter = _frontmatter(SKILL_MD.read_text(encoding="utf-8"))

    assert frontmatter["name"] == agent_install.SKILL_NAME
    assert frontmatter["description"].strip()
    assert len(frontmatter["description"]) < 1024


def test_the_skill_names_no_command_that_does_not_exist():
    """
    Every ``opsmith <command>`` quoted in the prose resolves in the application.

    The generated references cannot drift, because they are generated. The prose can, and this is
    what stops it: renaming a command in a later phase fails here until the skill is rewritten.
    The region marked as naming what this release lacks is excluded, since that is its purpose.
    """
    known = _command_paths()
    documents = [SKILL_MD] + [SKILL_DIR / "references" / name for name in HAND_WRITTEN]

    for document in documents:
        text = _without_future(document.read_text(encoding="utf-8"))
        for match in re.finditer(r"opsmith ((?:[a-z][a-z-]*\s*){1,3})", text):
            words = [word for word in match.group(1).split() if word not in NOT_COMMANDS]
            if not words:
                continue

            longest = " ".join(words)
            candidates = [" ".join(words[:count]) for count in range(len(words), 0, -1)]
            assert any(
                candidate in known for candidate in candidates
            ), f"{document.name} names 'opsmith {longest}', which is not a command"


def test_the_configuration_the_skill_teaches_is_valid():
    """
    Every YAML example in the skill passes the validation Opsmith would put it through.

    A harness copies these examples, so an example that would be rejected is worse than none. It
    is also what fails when the schema changes under the prose: the next release replaces this
    shape, and this test is what insists the skill be rewritten with it.
    """
    documents = [SKILL_MD] + [SKILL_DIR / "references" / name for name in HAND_WRITTEN]

    examples = 0
    for document in documents:
        text = _without_future(document.read_text(encoding="utf-8"))
        for block in re.findall(r"```yaml\n(.*?)```", text, re.S):
            parsed = yaml.safe_load(block)
            if not isinstance(parsed, dict) or "app_name" not in parsed:
                continue

            examples += 1
            result = validate_config_data(parsed)
            assert result.ok, f"{document.name}: {[issue.message for issue in result.errors]}"

    assert examples, "the skill shows no configuration example at all"


def test_the_skill_names_no_flag_that_does_not_exist():
    """
    Every flag the prose quotes belongs to the command it is quoted under, or is global.

    Flags are checked against the whole application rather than against one command, because the
    prose names them in tables and in sentences as often as in a command line.
    """
    flags = _flags_of_app()
    every_flag = {flag for command_flags in flags.values() for flag in command_flags}
    documents = [SKILL_MD] + [SKILL_DIR / "references" / name for name in HAND_WRITTEN]

    for document in documents:
        text = _without_future(document.read_text(encoding="utf-8"))
        quoted: List[str] = []
        spans = re.findall(r"`([^`\n]+)`", text) + re.findall(r"```shell\n(.*?)```", text, re.S)
        for span in spans:
            # Everything after a bare "--" belongs to the command being run on the machine, whose
            # flags are its own business - `opsmith run ... -- manage.py ... --noinput`.
            quoted += re.findall(r"(--[a-z][a-z-]*)", span.split(" -- ")[0])

        for flag in sorted(set(quoted)):
            assert flag in every_flag, f"{document.name} names '{flag}', which no command takes"
