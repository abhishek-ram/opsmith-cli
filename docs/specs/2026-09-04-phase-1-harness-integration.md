# Phase 1: Coding-harness integration

**Goal:** a coding harness (Claude Code, Codex, OpenCode, Cursor, Gemini CLI and others) knows how opsmith works, what it may edit, and which commands to run, and can complete a deployment end to end using its own model for analysis.
**Depends on:** phase 0 (headless CLI), and nothing else. The skill describes the CLI contract and the schema as 0.5.0 leaves them; every later phase extends it as it lands, under the contract in [what later phases add](#what-later-phases-add).
**Size:** M.
**Ships as:** 0.6.0.

## Scope

1. An Agent Skill shipped inside the package.
2. Generated reference files so the skill never drifts from the code.
3. Validator commands that let the harness's model do generation while opsmith checks.
4. An installer that places the skill and an `AGENTS.md` section into a project or user scope.
5. Harness-mode conventions in the CLI.
6. The contract by which every later phase extends the skill, and the test that enforces it.

## Non-goals

- MCP (phase 7).
- Any harness-specific plugin code. Everything here is files plus the CLI.
- **Waiting for the schema and the customization story to settle.** This phase ships against schema v1 and the commands 0.5.0 has. Phase 2 replaces the schema, phase 3 adds the ownership manifest and the template registry, phase 5 adds recipes, phase 6 adds `setup --skeleton`; each regenerates what it invalidates. The references that cannot be generated yet are not shipped empty and are not faked by hand — they simply arrive with the phase that makes them possible.

## Design

### Why a skill, and why first

The knowledge a harness needs is instructions: the ownership map of `.opsmith/`, the schema, the workflow, the commands. The Agent Skills format is supported across the harnesses named above, so one skill directory serves all of them. Tools come later through MCP for clients that cannot run a shell.

It goes directly after phase 0 because phase 0 is what it needed. Every command a harness drives is headless, every stop is resumable, every failure is a typed code with a `hint`, and `config schema --format markdown` and the Typer app can already be walked into generated references. The parts of this spec that wait on phases 2 to 6 are additions to a working skill, not preconditions for one: without them a harness can still author `deployments.yml`, validate it, write Dockerfiles, validate those, plan an environment and create it. Shipping the agent surface last would mean the whole 0.x series was the part where opsmith could not be driven by the agents it is for, and would leave the skill's first release untested by real use at exactly the moment the CLI contract is easiest to change.

### Skill layout

Source of truth is inside the package so it ships with every release:

```
opsmith/skills/opsmith/
  SKILL.md
  references/
    commands.md         generated: every command, flags, exit codes, JSON envelope shapes
    config-schema.md    generated: `opsmith config schema --format markdown`
    ownership.md        hand-written now; regenerated from the phase 3 ownership manifest
    workflows.md        hand-written: new project, release, update, troubleshoot, rollback
    references.md       phase 2: the reference grammar (infra, services, domains, inputs) with examples
    templates.md        phase 3: generated from the template registry: every template path, its variables and hook points
    recipes.md          phase 5: generated bundled catalog with inputs
```

`ownership.md` is the one file written by hand before it can be generated, because a harness that does not know what it may edit is dangerous rather than merely limited, and the three categories are already settled in `CLAUDE.md`. It says so in its own header, and phase 3 replaces it with the generated file.

`SKILL.md` frontmatter:

```yaml
---
name: opsmith
description: Deploy applications and open-source apps to AWS or GCP with the opsmith CLI. Use when the user asks to deploy, release, set up infrastructure, add a database, deploy Odoo or another packaged app, or edit files under .opsmith/.
license: GPL-3.0-only
compatibility: Requires the opsmith CLI, docker, terraform >= 1.10 and cloud credentials
allowed-tools: Bash(opsmith:*)
metadata:
  version: "<package version>"
---
```

Body, under 500 lines:

1. What opsmith is and the one-paragraph mental model: config in `deployments.yml`, environments with state, strategies that render.
2. Ownership rules, repeated from `references/ownership.md` in short form.
3. Golden workflow with exact commands, always headless: `config validate`, `env plan`, `env create` with flags, `release`, `env status`, `update` when the config or the infrastructure changed, `run` for anything that has to happen on the deployed service — a migration, a superuser, a one-off check — how to read `--output json`, and the run-again loop: exit 3 means add the one missing answer and run again, exit 8 means perform the action in `details` and run again.
4. Writing `deployments.yml` by hand: pointer to the schema reference, one service example per type, and the note that `schema_version` is absent until phase 2 ships it.
5. Validate-before-deploy loop: `config validate`, `dockerfile validate`, `env plan`.
6. Never do: edit generated working directories, `state.yml` or tfstate; run terraform or ansible directly in `.opsmith/environments`; commit secrets; use interactive commands.
7. Troubleshooting by exit code and error code, with what to run next.
8. **`run` is the exception to the exit-code table**, and the skill says so where the table is. `opsmith run` exits with the status of the command it ran on the machine, after writing a successful envelope, so a non-zero exit with `ok: true` is the remote command failing and not opsmith failing — a harness that reads such an exit 2 as `INVALID_ARGUMENT` reports a broken CLI when what it has is a broken migration. What it ran, where, and the decoded output are in the result's `command`, `target`, `stdout_tail` and `stderr_tail`; the rule is to read `ok` before the exit code for this one command.

Two more sections arrive with the phases that give them something to say: customizing (phase 3) and when to use a recipe versus detection (phase 5).

### What later phases add

Each phase from 2 on owns its own part of the skill, and says so in a "Harness surface" section of its spec. Nothing here is work for this phase; it is the list this phase writes down so that the obligation exists before the skill does.

| Phase | Adds to the skill |
|------:|-------------------|
| 2 Service model v2 | `references/references.md`; regenerated `config-schema.md` for schema v2; `compose render`, `compose diff` and `compose validate` in the validate loop; the `plan()` result gains registry, images and the capacity plan; the hand-authoring section is rewritten for image sources, routes, volumes and files |
| 3 Customization | `references/ownership.md` becomes generated from the manifest; `references/templates.md` from the template registry; `template check` in the validate loop; the customizing section of `SKILL.md` |
| 4 Remote state | `env create --state`, `pull` and `state migrate` in `commands.md`; exit 7 (`STATE_LOCKED`, `STATE_CONFLICT`) in the troubleshooting table; the never-list gains hand-editing remote state |
| 5 Recipes | `references/recipes.md`; `recipe add|list|show` in the workflow; the recipe-versus-detection section |
| 6 Agentic repo analysis | `setup --skeleton` and `analyze` in the golden workflow, which is where the harness's own exploration replaces a detection run |
| 8 Data durability | backup and restore commands; `backup: true` on a volume; what destroying an environment does and does not delete |

Two things make this more than a promise. The freshness test below fails CI for any command, flag or schema field that changed without a regenerate, so the generated half cannot rot quietly. The hand-written half — `SKILL.md` and `workflows.md` — cannot be tested that way, which is why it is an explicit item in the migration plan's [definition of done](../notes/2026-09-04-migration-plan.md#definition-of-done-for-a-phase) rather than an assumption.

### Generated references

`scripts/build_skill_refs.py` regenerates the generated files from the CLI and the pydantic models. A test asserts the committed files match the generator output, so a schema or flag change without a regenerate fails CI. The generated `commands.md` comes from walking the Typer app, including the per-command `requires` list and the result model of each command; `config-schema.md` is `config schema --format markdown`, which 0c already ships. `templates.md`, `ownership.md` and `recipes.md` join the generator in phases 3, 3 and 5; until then the generator knows nothing about them and the freshness test covers only what exists.

### Validator and helper commands

Shipped by this phase:

| Command | Behaviour |
|---------|-----------|
| `opsmith config validate` | phase 0, unchanged; phase 2 adds reference validation |
| `opsmith dockerfile validate --service SLUG [--timeout S]` | wraps the existing build-and-run smoke check from `ServiceDetector._validate_dockerfile`; JSON result has build ok, run ok, log tails, and the model's `explanation` of any failure |
| `opsmith env plan --env NAME` or with `env create` flags | phase 0g, unchanged: the answers still needed with choices, defaults and which of them this run would stop for |

Arriving with a later phase, listed here because the skill's validate loop is written to grow into them:

| Command | Phase |
|---------|------:|
| `opsmith compose render --env NAME`, `opsmith compose diff --env NAME` | 2 |
| `opsmith compose validate --env NAME` — renders and runs `docker compose config` locally | 2 |
| `env plan` extended with registry needed, images to build, the capacity plan and its rationale, and the domains that will need DNS records | 2 |
| `opsmith template check [--deep]` — validates overlays and override files, reports drift and hand edits in generated directories | 3 |
| `opsmith setup --skeleton` — inventory-informed placeholder config, written by code for the harness to complete | 6 |

The skill's default workflow for a source repository is: write `deployments.yml` directly, filling fields from the harness's own exploration, `config validate`, write Dockerfiles, `dockerfile validate`, `env plan`, then `env create`. Opsmith's own model still runs its own steps in that flow, such as explaining a failed validation, but the harness does the exploration and authoring, so the detection run is skipped. Phase 6 adds `setup --skeleton` as the faster start to the same path.

### Harness-mode conventions in the CLI

- Non-TTY detection from phase 0 guarantees no hangs.
- Every stop for an answer or an external action is resumable by running the same command again, and the error payload carries the exact `resume` command.
- Every error's `hint` names the exact command or flag to run next.
- `--output json` results include `next_steps: [string]` where helpful (DNS records to create, post-deploy messages).
- Long commands stream progress to stderr so the harness's shell tool shows activity and the user can interrupt.

All five are true as of 0.5.0. This phase audits them command by command against the skill's workflow and fixes what it finds, rather than building them.

### Installer

```
opsmith agent install   [--target claude|codex|opencode|cursor|gemini|all] [--scope project|user] [--mcp]
opsmith agent uninstall [--target ...] [--scope ...]
opsmith agent status
```

- Copies the skill directory into each target's skill location. The path table lives in one module, `opsmith/cli/agent_install.py`, and must be verified at implementation time because conventions are still moving. Starting points: `.claude/skills/`, `.codex/skills/` and the shared `.agents/skills/`, `.opencode/skills/`, `.cursor/skills/`, `.gemini/skills/`; user scope under the home directory equivalents.
- Appends an `## Opsmith` section to the project's `AGENTS.md` between `<!-- opsmith:start -->` and `<!-- opsmith:end -->` markers so re-running updates in place. The section is five to ten lines: what `.opsmith/` is, the skill name, and the two commands to run first.
- Ensures `CLAUDE.md` imports `AGENTS.md` with an `@AGENTS.md` line when `CLAUDE.md` exists, or creates a minimal one when the Claude target is selected.
- `--mcp` writes the harness's MCP configuration for `opsmith mcp` once phase 7 ships; until then it prints instructions.
- Records installed locations in `.opsmith/agent-install.json` so uninstall is exact.
- Reinstalls in place when the package version differs from the installed skill's `metadata.version`, which is how a project picks up the references a later phase regenerated.

### Documentation

`README.md` gets a "Use with Claude Code, Codex and other agents" section: install command, what the agent will do, the safety rules. `docs/agents.md` holds the long version and the path table.

## Code changes by file

| File | Change |
|------|--------|
| `opsmith/skills/opsmith/**` | skill source |
| `scripts/build_skill_refs.py` | generator; wired into pre-commit and CI |
| `opsmith/cli/commands/validate.py` | `dockerfile validate`; `env plan` exists since phase 0g, the `compose` commands arrive in phase 2 |
| `opsmith/cli/agent_install.py`, `opsmith/cli/commands/agent.py` | installer |
| `opsmith/service_detector.py` | expose `validate_dockerfile(service) -> DockerfileValidationResult` for the command |
| `opsmith/tests/test_skill_refs.py` | freshness test |
| `README.md`, `docs/agents.md` | docs |

## Acceptance criteria

1. In a fresh checkout of a fixture repository, a harness session given only "deploy this to AWS dev" and the installed skill produces a valid `deployments.yml`, passing Dockerfiles, and a successful `env create`, with opsmith configured as usual. Verified manually with at least two harnesses before release.
2. `skills-ref validate opsmith/skills/opsmith` passes.
3. Editing a pydantic field description without regenerating references fails CI.
4. `opsmith agent install --target all --scope project` is idempotent: running it twice yields no diff.
5. Every command exercised by the skill returns a `hint` on failure that names a runnable next command.
6. No reference file, workflow step or troubleshooting row in the shipped skill names a command or a schema field that does not exist in this release. The table in [what later phases add](#what-later-phases-add) is the only place a later phase's surface is named, and it is marked as such.

## Tests

- `test_agent_install.py`: idempotency, marker handling in `AGENTS.md`, `CLAUDE.md` import line, uninstall exactness, in temp directories.
- `test_validate_commands.py`: `dockerfile validate` with mocked docker; `env plan` produces no provisioner calls.
- `test_skill_refs.py`: freshness. It walks the Typer app and the config models itself, so the phases that add commands and fields inherit the check without editing it.

## Risks and open questions

- Skill directory conventions differ per harness and may change; the single path module and the status command limit the blast radius.
- A harness may run interactive commands anyway; the non-TTY rule and the skill's "never" list are the defence.
- Frontend services still need a local build toolchain on the machine running `release`; the skill must say so.
- **The skill ships against schema v1, which phase 2 replaces.** Configs a harness authors at 0.6.0 are upgraded in memory and rewritten on the next save, which is the same path a hand-written config takes, so nothing breaks; what churns is the skill's own hand-authoring section and every harness transcript that quotes it. Mitigations: `config-schema.md` is generated, `metadata.version` pins the skill to a release, and the 1.0.0 changelog says plainly that the v1 examples are gone.
- Shipping the agent surface before the config schema is stable means the first outside reports arrive against a schema that is about to change. That is the intended trade: the reports are worth more before phase 2 is built than after.
