# The Agent Skill

How Opsmith presents itself to a coding harness: what ships, where it installs, what is generated
and what has to be written by hand.

The user-facing version is the "Use with Claude Code, Codex and other agents" section of the
[README](../../README.md). This page is for people changing it.

## What ships

```
opsmith/skill/
  SKILL.md                    hand-written; frontmatter carries the literal version
  references/
    commands.md               generated from the Typer application
    config-schema.md          generated from the pydantic models
    ownership.md              hand-written
    workflows.md              hand-written
```

`opsmith/skill/` holds the skill's files directly, and the installer copies that directory to
`<skills-root>/opsmith/`. The name at the destination comes from `SKILL_NAME` in
`opsmith/cli/agent_install.py`, and `test_skill_refs.py` asserts the frontmatter `name` matches it,
because a harness finds a skill by its directory name.

The whole tree ships in the wheel without any packaging change: hatchling is configured with
`packages = ["opsmith"]` and no include list, so every git-tracked file under `opsmith/` is
included, which is how `opsmith/templates/**` has always shipped.

## What is generated, and what enforces it

`opsmith/cli/skill_refs.py` renders the generated references. It lives under `cli/` because it
walks the Typer application and `test_boundaries.py` forbids importing typer anywhere else, and it
takes the app as an argument rather than importing it, so a command module can import it without
closing a cycle.

| Reference | Source |
|---|---|
| `commands.md` | `typer.main.get_command(app)`, walked; `EXIT_CODES`; the error classes in `core/errors.py`; the result models named by each command's return annotation |
| `config-schema.md` | `schema_to_markdown(config_json_schema())`, the same call `opsmith config schema --format markdown` makes |

Adding a reference is adding one entry to `GENERATED_REFERENCES`. The freshness test iterates that
mapping, so a later phase's reference inherits the check without the test being edited.

Two things keep it honest:

- **`opsmith/tests/test_skill_refs.py`** compares each committed reference against the generator,
  byte for byte. CI runs pytest and nothing else, so this is the enforcement.
- **A pre-commit hook** regenerates rather than complaining, so a contributor who edits a field
  description gets the updated reference in the same commit.

The prose cannot be generated, so it is checked differently: every `opsmith <command>` and every
`--flag` quoted in `SKILL.md` and the hand-written references has to resolve in the application,
and every YAML configuration example has to pass `validate_config_data`. The region between
`<!-- opsmith:future-start -->` and `<!-- opsmith:future-end -->` is excluded, since naming what
this release does not have is its whole purpose.

### Gotchas in the generator

- **Typer vendors its own click.** `typer.main.get_command(app)` returns a `typer.core.TyperGroup`
  that is not an instance of the installed `click.Group`, so an `isinstance` check silently skips
  every sub-app. The walk duck-types on `.commands` instead, and the generator imports no click.
- **Determinism is not optional.** CI generates on three Python versions, so nothing is `repr`'d:
  an `Enum` default is rendered from its `.value`, commands are sorted, parameters keep declaration
  order, and no timestamp or absolute path is written.
- **`--install-completion` and `--show-completion`** are Typer's, not Opsmith's, and are skipped.
- **The `run` caveat is derived.** Any command whose result model exposes `process_exit_code` gets
  the "this exits with the status of the command it ran" note, because that is exactly what
  `handle_errors` keys its pass-through exit on. A second such command inherits it.

## Where it installs

`SKILL_LOCATIONS` in `opsmith/cli/agent_install.py` is one frozen dataclass per harness, and each
says whether its path has been verified against the harness itself. `opsmith agent status` prints
that, so a user can see which entries are guesses.

| Target | Project | User | Verified |
|---|---|---|---|
| `claude` | `.claude/skills/opsmith` | `~/.claude/skills/opsmith` | yes |
| `agents` | `.agents/skills/opsmith` | `~/.agents/skills/opsmith` | no |
| `codex` | `.codex/skills/opsmith` | `~/.codex/skills/opsmith` | no |
| `opencode` | `.opencode/skill/opsmith` | `~/.config/opencode/skill/opsmith` | no |
| `cursor` | `.cursor/skills/opsmith` | `~/.cursor/skills/opsmith` | no |
| `gemini` | `.gemini/skills/opsmith` | `~/.gemini/skills/opsmith` | no |

`--target` takes one of those, or `auto`, which installs only where the harness's own directory
already exists, plus the shared location. It names one harness per run: a project that uses two
runs the install twice, and the record keeps both. There is no `all`. **It is required on `install`** - writing six
directories into somebody's project because a flag was left off is not a good default - and
optional on `uninstall`, which is bounded by the record and means "everything you put here".

**Verifying a path is a one-line change.** Install into a scratch project, confirm the harness
lists the skill, and flip `verified=True`.

## The rules the installer keeps

- **Install replaces rather than merges.** The destination is removed and copied afresh, so a
  reference a later release stops shipping does not survive forever in an installed copy.
- **Nothing is removed that is not ours.** A destination is only deleted when the record names it
  or when its `SKILL.md` says `name: opsmith`. Anything else stops the install with a `--force`
  hint rather than being deleted.
- **Installing twice leaves no diff.** No timestamp anywhere, no version the installer invents,
  and whether Opsmith created `AGENTS.md` is recorded as a fact about history rather than about
  the current run. A test asserts the whole project tree is byte-identical after a second install.
- **`AGENTS.md` and `CLAUDE.md` are opt-in**, behind `--agents-md`, and both edits live between
  `<!-- opsmith:start -->` and `<!-- opsmith:end -->` so a re-run rewrites in place.
- **The record goes where the install went.** A project install records to
  `.opsmith/agent-install.json`; a user install records to `~/.opsmith/agent-install.json`, never
  inside a repository, because it is not about any one project and may happen outside one.

## The version stamp

`SKILL.md` carries the literal version in its frontmatter, and `test_skill_refs.py` compares it
with `pyproject.toml`. Nothing is stamped at install time: a placeholder would leave `agent status`
with nothing to compare against, and would make an installed copy differ from the packaged one,
which is what the "is this directory ours" check depends on.

Two sources, each used where it is correct. The test compares two committed files. The runtime -
`agent status`, and the record - reads `importlib.metadata`, because a wheel has no
`pyproject.toml`.

**Releasing means bumping both**, and the test is what says so.

## `opsmith agent` needs no model

`handle_errors` resolves the model before every command body, which would have made
`opsmith agent install` demand an API key to copy files - as the first command a new user runs.
The `@no_model` tag in `opsmith/cli/commands/__init__.py` is the exception, and it reads as the
same sentence as `@requires`: a command that declares no tools is never probed for any, and a
command that declares no model is never asked for one.

## Before a release

1. `uv run python scripts/build_skill_refs.py --check`
2. `uv run pytest opsmith/tests/test_skill_refs.py`
3. Install into a scratch project for each target, confirm the harness lists the skill, and flip
   any `verified` that is now true.
4. **The manual acceptance check.** In a fixture repository with the skill installed, give a
   harness session only "deploy this to AWS dev" and confirm it authors a valid
   `deployments.yml`, passing Dockerfiles, and a successful `env create` without being told the
   commands. Do this with at least two harnesses. It cannot be a pytest, and it is the only test
   of whether the skill is any good.
