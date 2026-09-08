# Phase 0f: Headless subcommands and typed results

**Goal:** everything the interactive menus can do is reachable as a subcommand with flags, each returning a typed result in the JSON envelope, and the `deploy` menu becomes a thin dispatcher over the same functions.
**Depends on:** 0e.
**Size:** L.
**Lands as:** an internal change; the phase ships as 0.5.0 when 0g lands.

## Scope

1. Core functions for each operation, with the menus and the subcommands both calling them.
2. The subcommand surface below.
3. `opsmith/core/results.py`: a typed result per command.
4. The interactive `deploy` menu re-implemented as a dispatcher.

## Non-goals

- `env plan`, and the declared question trees it needs. That is 0g; the subcommands here work through the exit-3 loop.
- Changing the config schema or how compose is generated. Both are phase 1.

## Design

### The surface

```
opsmith init --app-name "My App"                       # creates .opsmith/deployments.yml, no scan
opsmith setup [--rescan] [--yes]                       # detection + Dockerfiles; --yes accepts detected config
opsmith config validate [--file PATH]                  # from 0c
opsmith config schema [--format json|markdown]
opsmith config show [--output json]
opsmith env list
opsmith env create --name dev --provider AWS --region us-east-1 --strategy Monolithic
                   [--project-id X --zone Y] [--instance-type T] [--domain slug=host]...
                   [--domain-email E] [--env-var K=V]... [--env-file FILE] [--answers FILE]
                   [--accept-defaults] [--wait-timeout S] [--no-deploy]
opsmith env status --env dev
opsmith release --env dev
opsmith update  --env dev [--domain slug=host]... [--yes]
opsmith run     --env dev --service SLUG -- CMD [ARGS...]
opsmith destroy --env dev --yes
```

`env create` creates the environment entry and deploys it, which is what `deploy` does today for a new environment; `--no-deploy` writes the config only. Running `env create` again for an environment whose creation stopped resumes it, on the rules from 0e.

Every flag is sugar for an answer: `--region us-east-1` is `--answer env.region=us-east-1`, `--domain api=api.example.com` is `--answer env.domain.api=api.example.com`. The mapping is the flag-shortcut column of the key table in the [migration plan](../../notes/2026-09-04-migration-plan.md), and a test asserts the two agree.

### Core functions

The strategy already exposes `deploy`, `release`, `destroy`, `run` and `update` (`monolithic.py:768`, `:864`, `:966`, `:1131`, `:1172`). What lives only inside the `deploy` command today is the surrounding orchestration: environment selection (`main.py:387-446`), provider account detection (`main.py:419-430`), domain collection (`_collect_domain_configuration`, `main.py:307`), and the action menu (`main.py:487-575`). Each becomes a function in `opsmith/core/` taking the context and returning a result.

The rule that keeps the two entry points from drifting: **the menu never calls a provisioner or a strategy method directly**. It resolves a choice to one of these functions and calls it, exactly as the subcommand does.

### Results

Each command returns a pydantic model that becomes the `result` field of the envelope:

- `SetupResult`: services detected, Dockerfiles written, infra dependencies.
- `EnvCreateResult`: environment name, provider, region, strategy, instance type, public IP, registry URL, urls per service, and the DNS records that were requested.
- `ReleaseResult`: images built, services released, validation status, urls.
- `RunResult`: exit code, stdout tail, stderr tail.
- `DestroyResult`: what was destroyed.
- `ValidateResult`: from 0c, unchanged.

Every result also carries `notices` and `next_steps`, collected from `notify`. The errors for exit 3 and exit 8 carry `resume`, the exact command to run again.

`env status` reads `state.yml` and reports the VM, the registry, the deployed services and their urls without touching the cloud, so it needs no external tools.

## Code changes by file

| File | Change |
|------|--------|
| `opsmith/core/results.py` | new: the result models |
| `opsmith/core/operations.py` | new: environment selection, creation, release, update, run, destroy, status, as functions over the context |
| `opsmith/cli/commands/env.py` | new: `env list|create|status` |
| `opsmith/cli/commands/release.py` | new: `release`, `update`, `run`, `destroy` |
| `opsmith/cli/commands/setup.py` | gains `init`, `--rescan`, `--yes` |
| `opsmith/cli/commands/deploy.py` | the menu becomes a dispatcher over `core/operations.py` |
| `opsmith/cli/app.py` | flag-to-answer mapping |
| `README.md` | a headless usage section |

## Acceptance criteria

1. `OPSMITH_NON_INTERACTIVE=1 opsmith env create --name dev --provider AWS --region us-east-1 --strategy Monolithic --instance-type t4g.small --domain api=api.example.com --domain-email me@example.com --output json` completes a deployment with no prompts, or exits 3 with a `MISSING_ANSWER` error naming the key (phase acceptance criterion 1).
2. The interactive `setup` and `deploy` flows behave as before for a user at a terminal (phase acceptance criterion 5).
3. `grep` finds no provisioner or strategy-method call in `cli/commands/deploy.py`.
4. Every flag shortcut resolves to the key the overview's table gives it.
5. `env list` and `env status` run with an empty `PATH`.
6. `opsmith run --env dev --service api -- ls -la` returns the command's exit code as its own.

## Tests

- `test_cli_contract.py`, extended: the envelope carries a typed result for each command; `notices` and `next_steps` are populated from `notify`; exit-3 and exit-8 envelopes carry `resume`.
- `test_operations.py`: each core function against fakes; the menu and the subcommand for the same action reach the same function with the same arguments.
- `test_flag_mapping.py`: flags map onto the keys the overview declares, in both directions.
