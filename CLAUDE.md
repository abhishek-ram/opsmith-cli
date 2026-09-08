# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Opsmith (`opsmith-cli` on PyPI) is a Typer CLI that deploys an application to a cloud provider: it
uses an LLM to analyse a repository, generates Dockerfiles and a compose stack, and provisions the
infrastructure with Terraform and Ansible.

## Commands

```bash
uv sync --group dev                      # install, including test deps
uv run pytest                            # whole suite (what CI runs, on 3.12 and 3.13)
uv run pytest opsmith/tests/test_monolithic_strategy.py::test_deploy_runs_the_provisioners_in_order
uv run pytest -k "provisioner"           # by name

pre-commit run --all-files               # isort, black --preview -l 100, flake8, codespell

uv run opsmith --model anthropic:claude-sonnet-4-6 --api-key "$KEY" setup
uv run opsmith --model ... --api-key ... --output json repomap   # machine-readable mode

OPSMITH_MODEL=anthropic:claude-sonnet-4-6 ANTHROPIC_API_KEY="$KEY" \
  uv run opsmith --output json config validate    # no flags, no docker, no terraform
```

Format through `pre-commit`, not a locally installed `black`: the hook pins 23.3.0, and a newer
black disagrees with it about wrapping implicitly concatenated strings, so the two will fight.

A model is always required, but neither option has to be typed: `opsmith/core/llm.py` resolves the
model from `--model`, then `OPSMITH_MODEL`, then `model:` in `.opsmith.conf.yml`, and the key from
`--api-key`, then the provider's own variable. Both are resolved together, so option order does not
matter, and anything missing or unknown is `INVALID_ARGUMENT`. The settings file is never read for
a key.

`handle_errors` is what holds the preconditions of a command body: the external tools the command
declared with `@requires("docker", "terraform")`, then the agent. Both happen there, once, just
before the body, rather than in the callback — click runs the group callback before it reaches a
subcommand's `--help`, so resolving there would make `opsmith setup --help` demand the very
configuration it is explaining. A command that declares no tools is never probed for any, which is
what lets `opsmith config validate` run on a machine with neither docker nor terraform; the
terraform version the check parses is recorded on the context for phase 3.

## Architecture

### Module map

| Module | Holds |
|--------|-------|
| `cli/` | Everything that knows about a terminal: `app.py` (Typer assembly, global options, the error handler), `output.py` (renderers), `state.py` (`CliState`), `commands/` (one per command, plus the `@requires` declaration in its `__init__.py`) |
| `core/` | Orchestration that never touches a terminal: `errors.py`, `events.py`, `context.py`, `provisioners.py`, `llm.py`, `config.py` |
| `cloud_providers/` | AWS and GCP, plus the registry third parties plug into |
| `deployment_strategies/` | `base.py` holds the shared steps, `monolithic.py` composes them |
| `infra_provisioners/` | Terraform and Ansible wrappers; the only code that shells out |
| `templates/` | Terraform modules, Ansible playbooks and compose snippets, by step and provider |
| `agent.py`, `models.py`, `prompts.py` | The pydantic-ai agent, the LLM registry, the prompt templates |
| `types.py` | The Pydantic models for `deployments.yml` and `state.yml` |
| `service_detector.py`, `repo_map.py` | Detecting what a repository deploys, and the map fed to the model |
| `git_repo.py`, `settings.py`, `utils.py` | The git connector, settings, and helpers. None may touch a terminal |
| `main.py` | Re-exports the Typer app so the `opsmith.main:app` entry point resolves |

### The UI boundary is the organising rule

Only `opsmith/cli/` may import `inquirer`, `typer`, `click`, `rich.print`, `rich.console`,
`rich.status` or `rich.prompt`. `opsmith/tests/test_boundaries.py` enforces this with an allowlist
of modules not yet converted, and **the allowlist may only shrink** — a stale entry fails the test.

Everything else follows from that rule:

- **Progress is events, not prints.** Core code calls `ctx.events.log/step/warning/output(...)`, or
  `with ctx.events.waiting(step, message):` for a slow call. `Event.message` is plain text with no
  markup; `opsmith/cli/output.py` decides how it looks. `TextRenderer` styles by event kind and
  turns a `waiting` pair into a rich spinner; `JsonRenderer` streams NDJSON to stderr so stdout
  carries exactly one envelope.
- **Validation is `core/config.py`.** Parsing and validating `deployments.yml` returns a list of
  `ConfigIssue`, never a print and never a bool. The `setup` editors and `opsmith config validate`
  call the same functions, so a config written by an agent meets the rules a person editing one
  meets.
- **Failures are `OpsmithError`.** `opsmith/core/errors.py` holds the hierarchy and `EXIT_CODES`,
  the single code-to-exit-code map. Core code raises; `handle_errors` in `opsmith/cli/app.py` turns
  the error into an envelope and an exit code. Anything not an `OpsmithError` becomes `INTERNAL`/1.
  Adding an error class means adding its code to `EXIT_CODES` — a test walks the module and fails
  otherwise.
- **Nothing constructs its own dependencies.** `OpsmithContext` (`opsmith/core/context.py`) carries
  `src_dir`, `deployments_path`, `events`, `agent`, `provisioner_factory`, `git_repo` (opened
  lazily) and `verbose`. A strategy, `ServiceDetector` and `RepoMap` each take one and reach for
  nothing else, so constructing them touches neither the filesystem nor the network.

`opsmith/cli/state.py` holds `CliState` — the terminal-only half (renderer, output mode, the
headless flags) — and nests the `OpsmithContext` that every core call receives.

### How a deployment actually runs

`setup` detects services and writes `.opsmith/deployments.yml`; `deploy` is an interactive menu over
one environment (create / release / update / run / delete) that dispatches to a strategy.

`BaseDeploymentStrategy` (`opsmith/deployment_strategies/base.py`) holds the steps every strategy
shares — container registry, image build and push, VM creation, fetching remote files, bucket
cleanup — and `MonolithicDeploymentStrategy` composes them into `deploy`/`release`/`update`/`run`/
`destroy`. A monolithic deploy is: container registry (Terraform) → build and push each image
(Ansible) → select and create a VM (LLM + Terraform) → install Docker (Ansible) → confirm DNS →
generate and deploy the compose stack (LLM + Ansible), with the state written to
`.opsmith/environments/<env>/state.yml`.

Provisioners come from `ctx.provisioner_factory.terraform(working_dir, step=...)` /
`.ansible(...)`, never constructed directly — that is what lets `test_monolithic_strategy.py` run a
whole deploy against fakes and assert the Terraform variables and Ansible extra-vars. Each
provisioner copies `opsmith/templates/<step>/<provider>/` into its working directory;
`copy_template` lower-cases the provider name, because call sites pass a mix of `name()` and
`name().lower()` while the directories on disk are all lower-case. Working-directory names built
from `name()` (`environments/global/AWS-us-east-1/`) keep the original casing — they address
Terraform state in users' existing projects.

### Plugins

Cloud providers, deployment strategies and LLM models are singleton registries loaded from entry
points (`opsmith.cloud_providers`, `opsmith.deployment_strategies`, `opsmith.models`). They load at
import time, before there is a renderer, so they report into a `BufferingSink` that
`_drain_registry_events` in `opsmith/cli/app.py` replays once the CLI can render.

### Where the LLM is used

`opsmith/agent.py` builds a `pydantic-ai` agent with two tools (read a repo-mapped file, generate a
secret). It is called for service detection, Dockerfile generation and repair, VM sizing, compose
generation, and judging container logs after a deploy. Prompts live in `opsmith/prompts.py`. Phase 1
of the migration below moves the rendering and arithmetic out of the model; judgment stays.

### `.opsmith/` is the user's, and is committed

`deployments.yml`, `docker/<slug>/Dockerfile` and templates are owned by people and agents;
`environments/<env>/<module>/` working directories are regenerated by opsmith on every run;
`environments/<env>/state.yml` and Terraform state are opsmith's alone. `GitRepo.ensure_gitignore`
adds the Terraform state patterns.

## Work in progress: the migration to 1.0

`docs/` is contributor-facing and is the source of truth for planned work — read it before starting
anything structural.

- `docs/notes/2026-09-04-migration-plan.md` — start here. Defines the conventions every phase builds
  against: the CLI contract, exit codes, the event schema, the interaction key table, `.opsmith/`
  ownership, the testing standard.
- `docs/specs/` — nine phase specs. Phase 0 (headless core) is split into seven parts, `0a`–`0g`,
  each its own merge unit; its `README.md` is the index and records which acceptance criterion each
  part proves.
- `docs/reference/` — how the system works today. Written in the present tense, maintained.

A spec is written before the work and stops changing once it ships; do not amend one to match what
was built. Parts `0a` (CLI split and errors), `0b` (context, events, provisioner injection) and
`0c` (model configuration, tool checks, the `config` commands) have landed. `0d` empties the boundary allowlist by moving the remaining `inquirer` calls behind an
`Interaction` API — leave prompts where they are until then.

## Conventions

When writing code:

- Write simple verbose code over terse, compact, dense code.
- Use types everywhere possible.
- Report progress where it makes debugging easy — through the event sink, never a print.
- Add a docstring to functions that describes their use.
- Add `try`/`except` blocks sparingly, only where it is essential.

When writing tests:

- Use pytest. Add a docstring to each test that describes its flow clearly.
- Mock out external calls and command calls. `opsmith/tests/conftest.py` has the shared fakes:
  `RecordingSink`, `FakeGitRepo`, and `FakeProvisionerFactory`, which records template copies,
  Terraform variables and Ansible extra-vars in one ordered log and can be scripted with outputs or
  a failure per step. `opsmith_context` wires them together.
- Do not assert on logs — or on event text as a proxy for behaviour.
- A test that passes because a fake raised early is worse than no test. Make the fake fail where the
  real thing would, so the code path under test actually runs.
