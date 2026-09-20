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
| `cli/` | Everything that knows about a terminal: `app.py` (Typer assembly, global options, the error handler), `output.py` (renderers), `interaction.py` (`TerminalInteraction`), `state.py` (`CliState`), `flags.py` (the flag-to-answer table), `commands/` (one per command, plus the `@requires` declaration in its `__init__.py`) |
| `core/` | Orchestration that never touches a terminal: `errors.py`, `events.py`, `interaction.py`, `context.py`, `provisioners.py`, `llm.py`, `config.py`, `answers.py`, `steps.py`, `results.py`, `operations.py` |
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
`rich.status` or `rich.prompt`. `opsmith/tests/test_boundaries.py` enforces it. Its allowlist of
modules not yet converted is **empty** as of part `0d`: an entry there is now a regression.

Everything else follows from that rule:

- **Progress is events, not prints.** Core code calls `ctx.events.log/step/warning/output(...)`, or
  `with ctx.events.waiting(step, message):` for a slow call. `Event.message` is plain text with no
  markup; `opsmith/cli/output.py` decides how it looks. `TextRenderer` styles by event kind and
  turns a `waiting` pair into a rich spinner; `JsonRenderer` streams NDJSON to stderr so stdout
  carries exactly one envelope.
- **Questions are keyed interactions, not prompts.** Core code calls
  `ctx.interact.ask/select/confirm/edit/wait_for/notify(...)`, inline, wherever an answer is
  needed. Every call carries a stable key from the table in the migration plan, because only a
  question that can be named can be answered by a flag, a file or an agent;
  `opsmith/tests/test_interaction_keys.py` walks the package and fails on a key the table does not
  declare. There are two implementations: `opsmith/cli/interaction.py` maps the primitives onto
  `inquirer`, and `HeadlessInteraction` in `opsmith/core/interaction.py` resolves an answer from
  `--answer`, `--env-file`, `--answers`, `OPSMITH_ANSWER_<KEY>`, the environment's answer store
  and — under `--accept-defaults` — the question's own default, in that order. A cancelled prompt
  is `InteractionCancelled` (exit 3), never a `None` a call site has to check for; a question
  nothing answers is `MissingAnswerError` (also 3), and an answer that was supplied and refused is
  `InvalidArgument` (exit 2), because telling a driver to supply what it just supplied would loop
  forever.
- **Every answer is remembered, outside the repository.** `ctx.answers`
  (`opsmith/core/answers.py`) writes each answer through to `answers.yml` — a flat mapping
  `--answers` can read back — the moment it is given, in both modes. A secret goes to
  `secrets.yml` beside it instead, and for the monolithic strategy is superseded by the `.env` on
  the machine as soon as `release` fetches it back. Both live under
  `~/.opsmith/projects/<name>-<digest>/environments/<env>/`, resolved by
  `project_state_dir` (`opsmith/utils.py`) and overridable through `state_dir:` in
  `.opsmith.conf.yml` — a repository holds what a person wrote plus the state of what is
  deployed, and none of this is either. Keys that describe one invocation rather than the
  environment (`env.action`, `delete.confirm`, the destructive confirmations) are never
  persisted. The store is bound to an environment by `deploy` as soon as one is named; before
  that, answers are held in memory. `opsmith/tests/conftest.py` has an autouse fixture pointing
  `state_dir` at `tmp_path`, so no test can reach a developer's own.
- **Validation is `core/config.py`.** Parsing and validating `deployments.yml` returns a list of
  `ConfigIssue`, never a print and never a bool. The `setup` editors and `opsmith config validate`
  call the same functions, so a config written by an agent meets the rules a person editing one
  meets.
- **Failures are `OpsmithError`.** `opsmith/core/errors.py` holds the hierarchy and `EXIT_CODES`,
  the single code-to-exit-code map. Core code raises; `handle_errors` in `opsmith/cli/app.py` turns
  the error into an envelope and an exit code. Anything not an `OpsmithError` becomes `INTERNAL`/1.
  Adding an error class means adding its code to `EXIT_CODES` — a test walks the module and fails
  otherwise.
- **Nothing constructs its own dependencies.** `OpsmithContext` (`opsmith/core/context.py`)
  requires `src_dir`, `deployments_path`, `events`, `interact` and `provisioner_factory` — how a run
  reaches the world, so no run is without them. Only two are optional, and for a reason: `agent`,
  because a real run has none until the model is resolved after the context is built, and
  `git_repo`, because it is opened lazily so a command that needs no repository does not fail
  outside one. A strategy, `ServiceDetector` and `RepoMap` each take a context and reach for nothing
  else, so constructing them touches neither the filesystem nor the network.

`opsmith/cli/state.py` holds `CliState` — the terminal-only half (renderer, output mode, the
headless flags) — and nests the `OpsmithContext` that every core call receives.

- **Every operation is a function, and the menus are dispatchers.** `opsmith/core/operations.py`
  holds one function per action, each taking the context and returning a typed result from
  `opsmith/core/results.py`. The subcommands and the `deploy` menu both call these and nothing
  else - **the menu never reaches a strategy or a provisioner**, which is what stops the two
  surfaces drifting, and `opsmith/tests/test_operations.py` proves they arrive at the same
  function with the same arguments. Every result carries `notices` and `next_steps`, collected
  from `notify` by both interaction implementations.
- **A result names no topology.** Infrastructure is a `List[Resource]` — `kind`, `id`, and
  whatever of `name`, `region`, `address`, `size` and `details` applies — shared by `env create`,
  `env status` and `destroy`, which is why none of them has a `public_ip` or a `virtual_machine`
  field. A named scalar would be a claim that every strategy raises exactly one machine and that
  it has an IP, which is the same over-constraint the minimal strategy contract exists to avoid;
  `RECORDED_MACHINES` in `test_operations.py` is a two-machine strategy kept precisely so the
  single-machine built-in cannot be the only thing the models are checked against. `ResourceKind`
  names the kinds Opsmith knows and `kind` is a plain string so a strategy can use its own.
- **Every flag is sugar for an answer.** `opsmith/cli/flags.py` holds `FLAG_KEYS`: `--region` is
  `--answer env.region=`, `--domain api=host` is `--answer env.domain.api=`. A subcommand calls
  `flags.supply(state, ...)`, which folds its options into `AnswerSources.inline` and rebuilds the
  interaction; an explicit `--answer` still wins. `opsmith/tests/test_flag_mapping.py` checks the
  table against the flag-shortcut column of the migration plan's key table, in both directions.

### How a deployment actually runs

`setup` detects services and writes `.opsmith/deployments.yml`; `deploy` is an interactive menu over
one environment (create / release / update / run / delete) that dispatches to a strategy. Each of
those actions is also a subcommand - `env create`, `release`, `update`, `run`, `destroy` - over the
same functions. The menu stays terminal-only for creation, because it asks `env.name` twice under
one key; a headless run stopping there is told to use `opsmith env create`.

`BaseDeploymentStrategy` (`opsmith/deployment_strategies/base.py`) holds the steps every strategy
shares — container registry, image build and push, VM creation, fetching remote files, bucket
cleanup — and `MonolithicDeploymentStrategy` composes them into `deploy`/`release`/`update`/`run`/
`destroy`. A monolithic deploy is: container registry (Terraform) → build and push each image
(Ansible) → select and create a VM (LLM + Terraform) → install Docker (Ansible) → confirm DNS →
generate and deploy the compose stack (LLM + Ansible), with the state written to
`.opsmith/environments/<env>/state.yml`.

A strategy implements six methods, all of which return a result: `deploy`, `release`, `update`,
`run`, `destroy` and `status`. `status` reads the environment's own `state.yml` and must not
contact a cloud - `opsmith env status` declares no external tools and is expected to answer with an
empty `PATH`. Monolithic builds its resources through the three module helpers `_machine_resource`,
`_registry_resource` and `_cdn_resource`, so `deploy`, `status` and `destroy` describe the same
thing the same way rather than each naming its own subset. `run` is the one command whose result
decides the process exit code: the playbook
hands back the remote command's status and base64 stdout/stderr tails through `OPSMITH_OUTPUT_`
markers, and `handle_errors` exits with it after writing the success envelope.

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
`environments/<env>/state.yml` and Terraform state are opsmith's alone.
`GitRepo.ensure_gitignore` adds the Terraform state patterns — note it bails on the sentinel
`**/.terraform/` and is only called on a first `setup`, so a pattern added to that block would
never reach an existing project. Nothing that has to be ignored is put there for that reason;
`answers.yml`, `secrets.yml` and `steps.yml` are outside the repository entirely.

`state.yml` existing is what says an environment has been deployed — `MonolithicDeploymentState.load`
raises `UnknownEnvironment` without it and `destroy` removes the directory — so nothing that has
to persist mid-run belongs in it. That is why the step ledger has its own file.

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
was built. Parts `0a` (CLI split and errors), `0b` (context, events, provisioner injection),
`0c` (model configuration, tool checks, the `config` commands), `0d` (the interaction API and its
terminal implementation), `0e` (headless mode, the answer store, resume) and `0f` (the headless
subcommands and typed results) have landed. `0g` is next: cloud providers declare their questions
as data instead of prompting inside themselves, and `env plan` reports what a run will need before
it starts.

Three deviations from the `0f` spec. The strategy contract change is **breaking** — the five action
methods return results and `status()` is new — where the spec left the return types unstated; there
are no known third-party strategies and `0g` breaks the provider contract in the same release, so
0.5.0 carries one plugin note rather than two.

And **there is no `--yes`**, which the spec's surface and the migration plan's global-options table
both assumed. It was dropped rather than implemented: it is pure sugar over
`--answer delete.confirm=DELETE`, which is precise about which gate it approves, and a flag that
generic cannot be read at the call site — `opsmith --yes destroy --env dev` puts the approval
nowhere near what it approves. Destructive keys are answered by name; `DESTRUCTIVE_KEYS` in
`opsmith/core/answers.py` is what still refuses them a default and refuses to persist them. The
review editors that `setup --yes` was meant to skip have their own flag, `setup --accept-detected`,
carried on `AnswerSources.accept_reviews` because it answers no question. The migration plan's
tables were updated to match, since `test_flag_mapping.py` and `test_interaction_keys.py` parse
them, and so were the six later specs that assumed the flag — `phase-1`, `phase-2`, `phase-3`,
`phase-4`, `phase-5` and `phase-8`. Amending those is not a breach of the rule above: a spec stops
changing *once it ships*, and none of them has been built, so they are still designs to be built
against rather than the record of anything. The shipped `0e` and `0f` specs keep their `--yes`,
which is what that rule is for; this paragraph is their erratum. Three of the six named no key for
their confirmation, so `state.migrate.confirm`, `recipe.add|upgrade|remove.confirm`,
`data_disk.migrate.confirm` and `backup.restore.confirm` are proposals made while editing, not
decisions — rename them freely when those phases are built.

And **infrastructure is a resource list, not named fields**. The spec gives `EnvCreateResult` an
"instance type, public IP" and has `env status` report "the VM"; both would have been a claim that
every strategy raises exactly one machine and that it has an IP, which is false for anything
horizontally scaled, for a cluster, and for serverless. `EnvStatusResult` was the worse of the two,
since it typed the field as monolithic's own `VirtualMachineState`, whose seven required fields a
third-party `status()` could neither fill nor honestly omit. They are `List[Resource]` instead,
and `DestroyResult.destroyed` — which had been encoding `kind` and `id` into strings like
`frontend_cdn:<slug>` — uses the same model. `RunResult` gained `target`, because a strategy with
more than one place to run a command has to say which it picked.

Three deviations from the `0e` spec, all deliberate. `ctx.steps.once(...)` yields whether the block
should run (`with ctx.steps.once("vm.create") as should_run:`) because a context manager cannot skip
its own body. The ledger lives in `steps.yml` rather than `state.yml` for the reason above. And the
answer store is outside the repository rather than a "gitignored local cache" inside it — **which
two later specs still assume it is not**: `phase-2` negates `answers.yml` out of its gitignore block
so it gets committed, and `phase-3` uploads it to the cloud bucket beside `deployments.yml` and
`state.yml`. Neither spec has been amended, because a spec is not rewritten to match what was built;
read this paragraph first when you start either.

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
