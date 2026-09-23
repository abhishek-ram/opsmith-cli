"""Putting the Agent Skill where a coding harness will read it.

The skill ships inside the package, at ``opsmith/skill/``. This module copies it into the
directories the harnesses read skills from, records exactly what it wrote so that removing it is
exact, and reports what is installed and whether it is current.

Two rules run through all of it. **Nothing is written that was not asked for**: the managed block
in ``AGENTS.md`` and the import line in ``CLAUDE.md`` happen only under ``--agents-md``. And
**nothing is removed that Opsmith did not put there**: a directory is only deleted when the record
names it, or when the ``SKILL.md`` inside it says it is this skill.

The path table is the part that will age. Skill directory conventions are still moving, so every
entry says whether it has been verified against the harness itself, ``opsmith agent status``
prints that, and correcting one is editing one string.
"""

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from opsmith.core.context import OpsmithContext
from opsmith.core.errors import InvalidArgument
from opsmith.core.events import STEP_SETUP
from opsmith.core.operations import reported
from opsmith.core.results import (
    AgentInstallResult,
    AgentLocation,
    AgentLocationState,
    AgentStatusResult,
    AgentUninstallResult,
)
from opsmith.settings import settings
from opsmith.utils import package_version

#: What the skill installs as. It is the directory name at the destination and the ``name`` in the
#: frontmatter, and the two have to agree for a harness to load it.
SKILL_NAME = "opsmith"

#: The skill as it ships. Beside ``opsmith/templates``, and found the same way.
PACKAGE_SKILL_DIR = Path(__file__).parent.parent / "skill"

#: What the install record is called, in the project and in the user's state directory.
RECORD_FILENAME = "agent-install.json"

AGENTS_FILENAME = "AGENTS.md"
CLAUDE_FILENAME = "CLAUDE.md"

#: What surrounds the section Opsmith manages in ``AGENTS.md``. Re-running rewrites what is
#: between them and leaves everything else alone.
BLOCK_START = "<!-- opsmith:start -->"
BLOCK_END = "<!-- opsmith:end -->"

#: The line that makes ``CLAUDE.md`` read ``AGENTS.md``.
IMPORT_LINE = "@AGENTS.md"

#: The target that is not one harness: wherever a harness is already set up here.
AUTO_TARGETS = "auto"

PROJECT_SCOPE = "project"
USER_SCOPE = "user"
SCOPES = (PROJECT_SCOPE, USER_SCOPE)


@dataclass(frozen=True)
class SkillLocation:
    """One place a harness reads skills from."""

    target: str
    label: str

    #: Where the harness looks inside a project, relative to its root.
    project: str

    #: Where it looks for the user, relative to the home directory.
    user: str

    #: Whether this path has been confirmed against the harness itself. An unverified entry is the
    #: best reading of a convention that is still moving, and is reported as such.
    verified: bool

    note: str = ""


#: The path table. One line per harness; correcting a moved convention is one string.
SKILL_LOCATIONS: Tuple[SkillLocation, ...] = (
    SkillLocation(
        target="claude",
        label="Claude Code",
        project=".claude/skills",
        user=".claude/skills",
        verified=True,
    ),
    SkillLocation(
        target="agents",
        label="Shared agent skills",
        project=".agents/skills",
        user=".agents/skills",
        verified=False,
        note="The cross-harness location. Harmless where it is not read.",
    ),
    SkillLocation(
        target="codex",
        label="Codex",
        project=".codex/skills",
        user=".codex/skills",
        verified=False,
    ),
    SkillLocation(
        target="opencode",
        label="OpenCode",
        project=".opencode/skill",
        user=".config/opencode/skill",
        verified=False,
        note="XDG based for the user scope, and the directory has been singular in some versions.",
    ),
    SkillLocation(
        target="cursor",
        label="Cursor",
        project=".cursor/skills",
        user=".cursor/skills",
        verified=False,
    ),
    SkillLocation(
        target="gemini",
        label="Gemini CLI",
        project=".gemini/skills",
        user=".gemini/skills",
        verified=False,
    ),
)

TARGET_NAMES = tuple(location.target for location in SKILL_LOCATIONS)

#: What ``--agents-md`` writes between the markers. No version and no date, so running it twice
#: writes the same bytes, and nothing here names a command that this release does not have.
AGENTS_BLOCK = f"""## Opsmith

This project deploys with [Opsmith](https://github.com/abhishek-ram/opsmith). Configuration lives
in `.opsmith/deployments.yml` and Dockerfiles in `.opsmith/docker/<service>/Dockerfile`; both are
yours to edit. Everything under `.opsmith/environments/<env>/` is Opsmith's: working directories
are rebuilt on every run and `state.yml` is never edited by hand.

Read the `{SKILL_NAME}` skill before changing any of it. Start with:

```shell
opsmith --output json config validate
opsmith --output json env plan --name dev
```

Every command takes `--output json`, never prompts when driven that way, and is safe to run again
after it stops. Exit 3 means an answer is missing; exit 8 means something outside Opsmith has to
happen first.
"""


def _skill_frontmatter(skill_md: Path) -> Dict:
    """
    Reads the frontmatter of a ``SKILL.md``.

    :param skill_md: The file to read.
    :return: What its frontmatter declares, or an empty mapping when it has none.
    """
    if not skill_md.is_file():
        return {}

    text = skill_md.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}

    _, _, rest = text.partition("---")
    document, _, _ = rest.partition("\n---")
    parsed = yaml.safe_load(document)
    return parsed if isinstance(parsed, dict) else {}


def packaged_skill_version() -> str:
    """
    :return: The version the packaged skill declares.
    """
    metadata = _skill_frontmatter(PACKAGE_SKILL_DIR / "SKILL.md").get("metadata") or {}
    return str(metadata.get("version", "unknown"))


def installed_skill_version(directory: Path) -> Optional[str]:
    """
    :param directory: An installed skill directory.
    :return: The version it declares, or None when nothing is installed there.
    """
    metadata = _skill_frontmatter(directory / "SKILL.md").get("metadata") or {}
    version = metadata.get("version")
    return str(version) if version is not None else None


def is_our_skill(directory: Path) -> bool:
    """
    Decides whether a directory holds this skill, and may therefore be replaced or removed.

    :param directory: The directory in question.
    :return: Whether its ``SKILL.md`` says it is this skill.
    """
    return _skill_frontmatter(directory / "SKILL.md").get("name") == SKILL_NAME


def resolve_scope(scope: str) -> str:
    """
    :param scope: What was asked for.
    :return: The same scope, once it is known to be one.
    :raises InvalidArgument: It is not a scope.
    """
    if scope not in SCOPES:
        raise InvalidArgument(
            f"'{scope}' is not a scope.",
            hint=f"Use one of: {', '.join(SCOPES)}.",
            details={"scope": scope, "known": list(SCOPES)},
        )
    return scope


def user_home() -> Path:
    """
    Returns the home directory a user-scope install writes under.

    A function rather than ``Path.home()`` at the call site so that a test can point it somewhere
    else: a test that wrote into a developer's own home would be a bug with consequences.

    :return: The home directory.
    """
    return Path.home()


def skill_root(location: SkillLocation, scope: str, project_root: Path) -> Path:
    """
    :param location: The harness location.
    :param scope: Whether this is the project or the user location.
    :param project_root: The root of the repository being worked on.
    :return: The directory that harness reads skills from.
    """
    if scope == PROJECT_SCOPE:
        return project_root / location.project
    return user_home() / location.user


def install_path(location: SkillLocation, scope: str, project_root: Path) -> Path:
    """
    :param location: The harness location.
    :param scope: Whether this is the project or the user location.
    :param project_root: The root of the repository being worked on.
    :return: Where the skill itself goes.
    """
    return skill_root(location, scope, project_root) / SKILL_NAME


def resolve_target(requested: Optional[str], scope: str, project_root: Path) -> List[SkillLocation]:
    """
    Works out which harness locations a run is about.

    Asking for nothing is an error rather than a default. Installing writes into directories that
    belong to other people's tools, and six of them at once in a project that uses one harness is
    not something to do because a flag was left off. One harness is named per run; a project that
    uses two runs the install twice, and the record accumulates both.

    :param requested: What ``--target`` asked for: one harness name, or "auto".
    :param scope: The scope being installed into, which is what "auto" looks at.
    :param project_root: The root of the repository being worked on.
    :return: The locations, in table order: one, or what "auto" found.
    :raises InvalidArgument: Nothing was asked for, or the name is not one of the harnesses.
    """
    wanted = (requested or "").strip().lower()
    if not wanted:
        raise InvalidArgument(
            "Say which harness to install for.",
            hint=(
                f"Use --target with one of: {', '.join(TARGET_NAMES)}."
                " --target auto installs only where a harness is already set up here."
            ),
            details={"known": list(TARGET_NAMES) + [AUTO_TARGETS]},
        )

    if wanted == AUTO_TARGETS:
        return [
            location
            for location in SKILL_LOCATIONS
            if location.target == "agents"
            or skill_root(location, scope, project_root).parent.exists()
        ]

    if wanted not in TARGET_NAMES:
        raise InvalidArgument(
            f"'{wanted}' is not a harness Opsmith knows where to install for.",
            hint=f"Use one of: {', '.join(TARGET_NAMES)}, or auto.",
            details={"target": wanted, "known": list(TARGET_NAMES) + [AUTO_TARGETS]},
        )

    return [location for location in SKILL_LOCATIONS if location.target == wanted]


# --- the record ---------------------------------------------------------------------------------


def record_path(ctx: OpsmithContext, scope: str) -> Path:
    """
    Returns where the install record for a scope lives.

    A project install is recorded in the repository, because the skill it installed is in the
    repository too and is committed with it. A user install is recorded beside the rest of what
    Opsmith remembers about this machine: it is not about any one project, and may well have
    happened outside a repository altogether.

    :param ctx: The run's context.
    :param scope: The scope in question.
    :return: The file the record is kept in.
    """
    if scope == PROJECT_SCOPE:
        return ctx.deployments_path / RECORD_FILENAME
    return Path(settings.state_dir).expanduser() / RECORD_FILENAME


def read_record(ctx: OpsmithContext, scope: str) -> Dict:
    """
    :param ctx: The run's context.
    :param scope: The scope in question.
    :return: What a previous install recorded, or an empty record.
    """
    path = record_path(ctx, scope)
    if not path.is_file():
        return {}

    parsed = json.loads(path.read_text(encoding="utf-8"))
    return parsed if isinstance(parsed, dict) else {}


def write_record(ctx: OpsmithContext, scope: str, record: Dict):
    """
    Writes the install record, or removes it when nothing is left to record.

    It carries no timestamp, deliberately: running an install twice has to leave no diff.

    :param ctx: The run's context.
    :param scope: The scope in question.
    :param record: What to record.
    """
    path = record_path(ctx, scope)
    if not record.get("installs") and not record.get("agents_md") and not record.get("claude_md"):
        path.unlink(missing_ok=True)
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _recorded_path(ctx: OpsmithContext, entry: Dict) -> Path:
    """
    :param ctx: The run's context.
    :param entry: One recorded install.
    :return: The absolute path it names. A project entry is recorded relative to the repository,
        so that the record can be committed and still be true on somebody else's machine.
    """
    path = Path(entry["path"])
    if path.is_absolute():
        return path
    return Path(ctx.src_dir) / path


def _record_entry(ctx: OpsmithContext, scope: str, path: Path, location: SkillLocation) -> Dict:
    """
    :param ctx: The run's context.
    :param scope: The scope being recorded.
    :param path: Where the skill was installed.
    :param location: The harness it was installed for.
    :return: The entry to record, with a project path kept relative to the repository.
    """
    recorded = path
    if scope == PROJECT_SCOPE:
        recorded = path.relative_to(Path(ctx.src_dir))
    return {
        "target": location.target,
        "scope": scope,
        "path": str(recorded),
        "installed_version": package_version(),
    }


# --- AGENTS.md and CLAUDE.md --------------------------------------------------------------------


def write_agents_block(path: Path) -> bool:
    """
    Writes the managed block into ``AGENTS.md``, in place if it is already there.

    :param path: The ``AGENTS.md`` to write.
    :return: Whether the file had to be created.
    :raises InvalidArgument: The file holds two managed blocks, which means an earlier write went
        wrong and a person should say which one to keep.
    """
    block = f"{BLOCK_START}\n{AGENTS_BLOCK}{BLOCK_END}\n"

    if not path.exists():
        path.write_text(block, encoding="utf-8")
        return True

    text = path.read_text(encoding="utf-8")
    if text.count(BLOCK_START) > 1 or text.count(BLOCK_END) > 1:
        raise InvalidArgument(
            f"{path.name} holds more than one Opsmith block.",
            hint="Remove all but one of them, then run 'opsmith agent install --agents-md' again.",
            details={"path": str(path)},
        )

    if BLOCK_START in text and BLOCK_END in text:
        before, _, rest = text.partition(BLOCK_START)
        _, _, after = rest.partition(BLOCK_END)
        path.write_text(f"{before}{block}{after.lstrip(chr(10))}", encoding="utf-8")
        return False

    separator = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    path.write_text(f"{text}{separator}{block}", encoding="utf-8")
    return False


def strip_agents_block(path: Path) -> bool:
    """
    Removes the managed block from ``AGENTS.md``, leaving everything else.

    :param path: The ``AGENTS.md`` to edit.
    :return: Whether a block was found and removed.
    """
    if not path.is_file():
        return False

    text = path.read_text(encoding="utf-8")
    if BLOCK_START not in text or BLOCK_END not in text:
        return False

    before, _, rest = text.partition(BLOCK_START)
    _, _, after = rest.partition(BLOCK_END)
    remainder = f"{before.rstrip()}\n{after.lstrip()}".strip()
    if remainder:
        path.write_text(remainder + "\n", encoding="utf-8")
    else:
        path.unlink()
    return True


def add_import_line(path: Path, *, create: bool) -> Tuple[bool, bool]:
    """
    Makes ``CLAUDE.md`` read ``AGENTS.md``.

    An existing file is never rewritten, only appended to, and only when the line is not already
    there.

    :param path: The ``CLAUDE.md`` to edit.
    :param create: Whether to write one when there is none.
    :return: Whether the line was added, and whether the file was created.
    """
    if not path.exists():
        if not create:
            return False, False
        path.write_text(f"{IMPORT_LINE}\n", encoding="utf-8")
        return True, True

    text = path.read_text(encoding="utf-8")
    if any(line.strip() == IMPORT_LINE for line in text.splitlines()):
        return False, False

    separator = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    path.write_text(f"{text}{separator}{IMPORT_LINE}\n", encoding="utf-8")
    return True, False


def remove_import_line(path: Path) -> bool:
    """
    :param path: The ``CLAUDE.md`` to edit.
    :return: Whether the import line was found and removed.
    """
    if not path.is_file():
        return False

    lines = path.read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines if line.strip() != IMPORT_LINE]
    if len(kept) == len(lines):
        return False

    remainder = "\n".join(kept).strip()
    if remainder:
        path.write_text(remainder + "\n", encoding="utf-8")
    else:
        path.unlink()
    return True


# --- the three commands -------------------------------------------------------------------------


def install(
    ctx: OpsmithContext,
    *,
    target: Optional[str],
    scope: str,
    agents_md: bool = False,
    force: bool = False,
    mcp: bool = False,
) -> AgentInstallResult:
    """
    Copies the skill into every selected harness location.

    The destination is removed and rewritten rather than merged into, so a reference file a later
    release stops shipping does not survive forever in an installed copy. Only a directory that is
    this skill is ever removed that way.

    :param ctx: The run's context.
    :param target: The harness to install for, or "auto".
    :param scope: Whether to install into the project or for the user.
    :param agents_md: Whether to write the managed block into AGENTS.md and the import into
        CLAUDE.md.
    :param force: Whether to replace a directory that does not identify itself as this skill.
    :param mcp: Whether MCP configuration was asked for, which has nothing to write yet.
    :return: What was written, and what was not.
    :raises InvalidArgument: A target or scope is unknown, or a destination belongs to something
        else.
    """
    scope = resolve_scope(scope)
    project_root = Path(ctx.src_dir)
    locations = resolve_target(target, scope, project_root)

    if agents_md and scope != PROJECT_SCOPE:
        raise InvalidArgument(
            "AGENTS.md is a project file, so --agents-md needs the project scope.",
            hint="Run 'opsmith agent install --scope project --agents-md'.",
            details={"scope": scope},
        )

    installed: List[AgentLocation] = []
    skipped: List[AgentLocation] = []
    record = read_record(ctx, scope)
    entries = {
        (entry["target"], entry["scope"]): entry for entry in record.get("installs", []) or []
    }

    for location in locations:
        destination = install_path(location, scope, project_root)
        if destination.exists() and not is_our_skill(destination) and not force:
            raise InvalidArgument(
                f"{destination} holds something that is not the Opsmith skill.",
                hint="Move it aside, or run 'opsmith agent install --force' to replace it.",
                details={"path": str(destination), "target": location.target},
            )

        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(PACKAGE_SKILL_DIR, destination)
        ctx.events.log(STEP_SETUP, f"Installed the {SKILL_NAME} skill for {location.label}.")

        entries[(location.target, scope)] = _record_entry(ctx, scope, destination, location)
        installed.append(
            _location_result(location, scope, destination, AgentLocationState.INSTALLED)
        )

    agents_path: Optional[str] = None
    claude_path: Optional[str] = None
    if agents_md:
        agents_file = project_root / AGENTS_FILENAME
        # Whether Opsmith brought the file into existence is a fact about history, not about this
        # run: a second install finds it there and must not record that somebody else wrote it.
        previously = record.get("agents_md") or {}
        created = write_agents_block(agents_file) or bool(previously.get("created"))
        record["agents_md"] = {"path": AGENTS_FILENAME, "created": created}
        agents_path = str(agents_file)

        claude_file = project_root / CLAUDE_FILENAME
        wants_claude = any(location.target == "claude" for location in locations)
        added, claude_created = add_import_line(claude_file, create=wants_claude)
        if added:
            record["claude_md"] = {"path": CLAUDE_FILENAME, "created": claude_created}
            claude_path = str(claude_file)

    record["skill"] = SKILL_NAME
    record["version"] = package_version()
    record["installs"] = [entries[key] for key in sorted(entries)]
    write_record(ctx, scope, record)

    if mcp:
        ctx.interact.notify(
            (
                "MCP configuration is not written yet: it arrives with 'opsmith mcp'. Nothing was"
                " written for --mcp."
            ),
            details={"next_steps": ["Use the skill through your harness's shell tool for now."]},
        )

    return reported(
        ctx,
        AgentInstallResult(
            skill=SKILL_NAME,
            version=package_version(),
            installed=installed,
            skipped=skipped,
            agents_md=agents_path,
            claude_md=claude_path,
        ),
    )


def uninstall(ctx: OpsmithContext, *, target: Optional[str], scope: str) -> AgentUninstallResult:
    """
    Removes what an install wrote, and nothing else.

    What the record names is what is removed, and only while it still identifies itself as this
    skill. Without a record the table is used instead, under the same rule, so an install from an
    older release can still be undone.

    :param ctx: The run's context.
    :param target: The harness to remove for, or "auto". Every location in the table when
        not given - removing from everywhere is the default here, where installing into everywhere is
        not, because this can only take back what the record says Opsmith put there.
    :param scope: Which scope to remove from.
    :return: What was removed.
    """
    scope = resolve_scope(scope)
    project_root = Path(ctx.src_dir)
    if target:
        locations = resolve_target(target, scope, project_root)
    else:
        locations = list(SKILL_LOCATIONS)
    wanted = {location.target for location in locations}

    record = read_record(ctx, scope)
    entries = {
        (entry["target"], entry["scope"]): entry for entry in record.get("installs", []) or []
    }

    removed: List[AgentLocation] = []
    for location in locations:
        entry = entries.get((location.target, scope))
        destination = (
            _recorded_path(ctx, entry)
            if entry is not None
            else install_path(location, scope, project_root)
        )

        if destination.is_dir() and is_our_skill(destination):
            shutil.rmtree(destination)
            ctx.events.log(STEP_SETUP, f"Removed the {SKILL_NAME} skill for {location.label}.")
            removed.append(
                _location_result(location, scope, destination, AgentLocationState.MISSING)
            )
        entries.pop((location.target, scope), None)

    agents_path: Optional[str] = None
    claude_path: Optional[str] = None
    if scope == PROJECT_SCOPE and wanted:
        agents_record = record.get("agents_md")
        if agents_record and strip_agents_block(project_root / agents_record["path"]):
            agents_path = str(project_root / agents_record["path"])
            record.pop("agents_md", None)

        claude_record = record.get("claude_md")
        if claude_record and remove_import_line(project_root / claude_record["path"]):
            claude_path = str(project_root / claude_record["path"])
            record.pop("claude_md", None)

    record["installs"] = [entries[key] for key in sorted(entries)]
    if not record["installs"]:
        record.pop("skill", None)
        record.pop("version", None)
    write_record(ctx, scope, record)

    return reported(
        ctx, AgentUninstallResult(removed=removed, agents_md=agents_path, claude_md=claude_path)
    )


def status(ctx: OpsmithContext, *, scope: Optional[str] = None) -> AgentStatusResult:
    """
    Reports where the skill is installed, and whether it is the version now running.

    :param ctx: The run's context.
    :param scope: The one scope to report on, or None for both.
    :return: Every location Opsmith knows about, and what is there.
    """
    scopes = [resolve_scope(scope)] if scope is not None else list(SCOPES)
    project_root = Path(ctx.src_dir)
    running = package_version()

    locations: List[AgentLocation] = []
    for one_scope in scopes:
        for location in SKILL_LOCATIONS:
            destination = install_path(location, one_scope, project_root)
            locations.append(
                _location_result(
                    location,
                    one_scope,
                    destination,
                    _state_of(destination, running, project_root, location, one_scope),
                    installed_version=installed_skill_version(destination),
                )
            )

    agents_file = project_root / AGENTS_FILENAME
    claude_file = project_root / CLAUDE_FILENAME
    has_block = agents_file.is_file() and BLOCK_START in agents_file.read_text(encoding="utf-8")
    has_import = claude_file.is_file() and any(
        line.strip() == IMPORT_LINE for line in claude_file.read_text(encoding="utf-8").splitlines()
    )

    return reported(
        ctx,
        AgentStatusResult(
            skill=SKILL_NAME,
            package_version=running,
            skill_version=packaged_skill_version(),
            locations=locations,
            agents_md=str(agents_file) if has_block else None,
            claude_md=str(claude_file) if has_import else None,
        ),
    )


def _state_of(
    destination: Path,
    running: str,
    project_root: Path,
    location: SkillLocation,
    scope: str,
) -> AgentLocationState:
    """
    :param destination: Where the skill would be.
    :param running: The version of Opsmith running.
    :param project_root: The root of the repository being worked on.
    :param location: The harness location.
    :param scope: The scope being reported on.
    :return: What is there now.
    """
    if not destination.exists():
        if skill_root(location, scope, project_root).parent.exists():
            return AgentLocationState.MISSING
        return AgentLocationState.NOT_APPLICABLE
    if not is_our_skill(destination):
        return AgentLocationState.FOREIGN
    if installed_skill_version(destination) != running:
        return AgentLocationState.STALE
    return AgentLocationState.INSTALLED


def _location_result(
    location: SkillLocation,
    scope: str,
    destination: Path,
    state: AgentLocationState,
    installed_version: Optional[str] = None,
) -> AgentLocation:
    """
    :param location: The harness location.
    :param scope: The scope it was addressed in.
    :param destination: Where the skill goes.
    :param state: What is there.
    :param installed_version: What is installed there, when anything is.
    :return: The reportable shape of it.
    """
    return AgentLocation(
        target=location.target,
        label=location.label,
        scope=scope,
        path=str(destination),
        state=state,
        installed_version=(
            installed_version
            if installed_version is not None
            else (package_version() if state is AgentLocationState.INSTALLED else None)
        ),
        verified=location.verified,
    )
