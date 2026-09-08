# Phase 6: Coding-harness integration

**Goal:** a coding harness (Claude Code, Codex, OpenCode, Cursor, Gemini CLI and others) knows how opsmith works, what it may edit, and which commands to run, and can complete a deployment end to end using its own model for analysis.
**Depends on:** phase 0 (headless CLI), phase 1 (stable schema), phase 2 (ownership manifest and template registry for the generated references); phase 5 is a soft dependency for `setup --skeleton`.
**Size:** M.
**Ships as:** 1.0.0.

## Scope

1. An Agent Skill shipped inside the package.
2. Generated reference files so the skill never drifts from the code.
3. Validator commands that let the harness's model do generation while opsmith checks.
4. An installer that places the skill and an `AGENTS.md` section into a project or user scope.
5. Harness-mode conventions in the CLI.

## Non-goals

- MCP (phase 7).
- Any harness-specific plugin code. Everything here is files plus the CLI.

## Design

### Why a skill first

The knowledge a harness needs is instructions: the ownership map of `.opsmith/`, the schema, the workflow, the commands. The Agent Skills format is supported across the harnesses named above, so one skill directory serves all of them. Tools come later through MCP for clients that cannot run a shell.

### Skill layout

Source of truth is inside the package so it ships with every release:

```
opsmith/skills/opsmith/
  SKILL.md
  references/
    ownership.md        generated from the phase 2 ownership manifest: editable, generated, never by hand
    config-schema.md    generated: `opsmith config schema --format markdown`
    commands.md         generated: every command, flags, exit codes, JSON envelope shapes
    recipes.md          generated: bundled catalog with inputs
    workflows.md        hand-written: new project, add recipe, release, update, troubleshoot, rollback
    references.md       the reference grammar (infra, services, domains, inputs) with examples
    templates.md        generated from the phase 2 template registry: every template path, its variables and hook points
```

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
3. Golden workflow with exact commands, always headless: `config validate`, `env plan`, `env create` with flags, `release`, `env status`, how to read `--output json`, and the run-again loop: exit 3 means add the one missing answer and run again, exit 8 means perform the action in `details` and run again.
4. Writing `deployments.yml` by hand: pointer to the schema reference, one build-source example, one image-source example, the reference grammar in three lines.
5. Validate-before-deploy loop: `config validate`, `dockerfile validate`, `compose render`, `compose diff`, `env plan`.
6. Customizing: prefer `overrides/` extension points; eject a template only when a whole file must change; run `template check` afterwards; environment-scoped changes go under `environments/<env>/`.
7. Never do: edit generated working directories, `state.yml` or tfstate; run terraform or ansible directly in `.opsmith/environments`; commit secrets; use interactive commands.
8. Troubleshooting by exit code and error code, with what to run next.
9. When to use a recipe versus detection.

### Generated references

`scripts/build_skill_refs.py` regenerates the generated files from the CLI and the pydantic models. A test asserts the committed files match the generator output, so a schema or flag change without a regenerate fails CI. The generated `commands.md` comes from walking the Typer app, including the per-command `requires` list and the result model of each command. `templates.md` and `ownership.md` come from the phase 2 registry and ownership manifest.

### Validator and helper commands

| Command | Behaviour |
|---------|-----------|
| `opsmith config validate` | phase 0, plus reference validation from phase 1 |
| `opsmith dockerfile validate --service SLUG [--timeout S]` | wraps the existing build-and-run smoke check from `ServiceDetector._validate_dockerfile`; JSON result has build ok, run ok, log tails, and the model's `explanation` of any failure |
| `opsmith compose render --env NAME` | phase 1: prints the rendered compose file and the env keys with secret values masked |
| `opsmith compose diff --env NAME` | phase 1: rendered file against the one on the VM |
| `opsmith compose validate --env NAME` | renders and runs `docker compose config` locally |
| `opsmith env plan --env NAME` or with `env create` flags | phase 0: the answers still needed with choices and defaults, plus registry needed, images to build, the capacity plan with its rationale, and the domains that will need DNS records |
| `opsmith setup --skeleton` | phase 5: inventory-informed placeholder config, written by code for the harness to complete |
| `opsmith template check [--deep]` | phase 2: validates overlays and override files, reports drift and hand edits in generated directories |

The skill's default workflow for a source repository becomes: run `setup --skeleton` or write the config directly, fill fields from the harness's own exploration, `config validate`, write Dockerfiles, `dockerfile validate`, then `env create`. Opsmith's own model still runs its own steps in that flow, such as explaining a failed validation, but the harness does the exploration and authoring, so the detection run is skipped.

### Harness-mode conventions in the CLI

- Non-TTY detection from phase 0 guarantees no hangs.
- Every stop for an answer or an external action is resumable by running the same command again, and the error payload carries the exact `resume` command.
- Every error's `hint` names the exact command or flag to run next.
- `--output json` results include `next_steps: [string]` where helpful (DNS records to create, post-deploy messages).
- Long commands stream progress to stderr so the harness's shell tool shows activity and the user can interrupt.

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

### Documentation

`README.md` gets a "Use with Claude Code, Codex and other agents" section: install command, what the agent will do, the safety rules. `docs/agents.md` holds the long version and the path table.

## Code changes by file

| File | Change |
|------|--------|
| `opsmith/skills/opsmith/**` | skill source |
| `scripts/build_skill_refs.py` | generator; wired into pre-commit and CI |
| `opsmith/cli/commands/validate.py` | `dockerfile validate`, `compose validate`; `env plan` exists since phase 0, `compose render` and `compose diff` since phase 1 |
| `opsmith/cli/agent_install.py`, `opsmith/cli/commands/agent.py` | installer |
| `opsmith/service_detector.py` | expose `validate_dockerfile(service) -> DockerfileValidationResult` for the command |
| `opsmith/deployment_strategies/monolithic.py` | extend the phase 0 `plan()` with registry, image and capacity details, still without side effects |
| `opsmith/tests/test_skill_refs.py` | freshness test |
| `README.md`, `docs/agents.md` | docs |

## Acceptance criteria

1. In a fresh checkout of a fixture repository, a harness session given only "deploy this to AWS dev" and the installed skill produces a valid `deployments.yml`, passing Dockerfiles, and a successful `env create`, with opsmith configured as usual. Verified manually with at least two harnesses before release.
2. `skills-ref validate opsmith/skills/opsmith` passes.
3. Editing a pydantic field description without regenerating references fails CI.
4. `opsmith agent install --target all --scope project` is idempotent: running it twice yields no diff.
5. Every command exercised by the skill returns a `hint` on failure that names a runnable next command.

## Tests

- `test_agent_install.py`: idempotency, marker handling in `AGENTS.md`, `CLAUDE.md` import line, uninstall exactness, in temp directories.
- `test_validate_commands.py`: `dockerfile validate` with mocked docker; `compose validate` golden; `env plan` produces no provisioner calls.
- `test_skill_refs.py`: freshness.

## Risks and open questions

- Skill directory conventions differ per harness and may change; the single path module and the status command limit the blast radius.
- A harness may run interactive commands anyway; the non-TTY rule and the skill's "never" list are the defence.
- Frontend services still need a local build toolchain on the machine running `release`; the skill must say so.
