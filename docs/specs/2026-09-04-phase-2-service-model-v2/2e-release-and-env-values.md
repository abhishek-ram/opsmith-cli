# Phase 2e: Release and env values

**Goal:** `release` deploys the compose file already in the working directory without rendering and asks only for env values that are empty, and a new `env vars` command changes values that are already set.
**Depends on:** 2d.
**Size:** M.
**Lands as:** a part of phase 2; the phase ships as 1.0.0 with phase 3 once 2h lands.

## Scope

1. `release` without rendering.
2. `opsmith env vars`, and the strategy method behind it.

## Non-goals

- How a deploy is judged. Both commands use whatever [2d](2d-deterministic-deploys.md) or [2f](2f-validation-repair-and-init-jobs.md) provides when they run.

## Design

### Release

Only `env create` and `update` render, because they are the two commands that change what the stack is. `release` renders nothing, as today. It deploys the `docker-compose.yml` in the environment's working directory, the file the last `env create`, `update` or repair wrote, with the images it just built, and judges the result as `update` does:

- It does not compare that file with the machine's copy, because nothing on the way changes it. The playbook copies it byte for byte, and every value that varies, secrets and env values alike, stays out of it as a `${KEY}` reference that Compose resolves from the `.env` on the machine. After any deploy from a checkout, that checkout's copy and the machine's are the same. Hand edits to either copy are not supported. With no local copy, as in a fresh clone, the machine's copy is fetched and written to the working directory first.
- It deploys the `.env` fetched from the machine. Of the env vars with no `value`, it asks as `envvar.<KEY>` only for one whose value there is empty, such as a third-party key left blank at `env create`; a value that is already set is changed with `env vars` (below). A value given with `--env-var` is still applied, so the flag is never silently ignored. Secrets and resolved `value`s stay as fetched, and the files on the machine stay as the last `env create` or `update` left them.
- A config change therefore reaches the machine only through `update`, which detects it, asks for what it needs, such as a new service's domain, and confirms it. `release` compares the config with the last deployed snapshot and, when they differ, says so in a notice that points at `update`.

`_fetch_remote_deployment_files` stays.

### Changing env values

`release` asks only for env values that are empty, and `update` only for those that are empty or new, so a value that is already set is changed on purpose, with a command of its own:

```
opsmith env vars --env NAME [--env-var KEY=VALUE]...
```

- It covers the env vars with no `value` in the config: the settings and third-party keys a person supplies. A key with a `value`, whether a template or `secret()`, is refused with `INVALID_ARGUMENT`, because the config owns it and the next `update` would put it back. So is a key the config does not declare, because nothing would read it.
- With `--env-var` it sets exactly those keys and asks nothing else. In a terminal without any, it asks each key again as `envvar.<KEY>`, with the current value as the default. Headless, only `--env-var` changes anything.
- It builds nothing and renders nothing, so the compose file on the machine stays as it is, repairs included. For monolithic it fetches the `.env` from the machine, changes the given keys and writes it back. It then runs the deploy playbook without pulling and without forcing a recreate, so Compose recreates only the services whose resolved environment changed, and the result is judged as it is after any deploy. A value that breaks a service therefore fails the command with `DEPLOY_UNHEALTHY` and the model's reason.
- It is a strategy method, `set_env_vars`, concrete on the base and refusing with `INVALID_ARGUMENT` for a strategy that does not implement it, so existing plugins keep working. Its result, `EnvVarsResult`, names the keys changed and the services recreated, never the values. The interactive `deploy` menu gains it as an action that dispatches to the same operation.

## Code changes by file

| File | Change |
|------|--------|
| `opsmith/deployment_strategies/monolithic.py` | `release` deploys the working directory's compose file, fetching the machine's only when there is no local copy, and asks only for empty env values; `set_env_vars` |
| `opsmith/deployment_strategies/base.py` | `set_env_vars`, concrete and refusing by default |
| `opsmith/core/operations.py`, `opsmith/core/results.py` | the `env vars` operation and `EnvVarsResult`; the `deploy` menu gains the action |
| `opsmith/templates/docker_compose_deploy/*/main.yml` | a run that neither pulls nor forces a recreate, for `env vars` |
| `opsmith/cli/commands/` | `env vars` |

## Acceptance criteria

1. `release` renders nothing and deploys the working directory's `docker-compose.yml` byte for byte; with no local copy it fetches the machine's and writes it to the working directory first; it asks only for env values that are empty, while a value given with `--env-var` is still applied; and a config changed since the last `update` produces a notice pointing at `update` (phase acceptance criterion 16).
2. `env vars --env dev --env-var KEY=VALUE` changes that key in the machine's `.env` without a build or a render, recreates only the services that read it, and validates the result; a key with a `value` in the config, or one the config does not declare, is refused with `INVALID_ARGUMENT` (phase acceptance criterion 17).

## Tests

- `test_monolithic_release.py`: release deploys the working directory's file without calling the renderer; with no local copy it fetches the machine's and writes it back; only empty env values are asked, and `--env-var` still reaches the `.env`; a config changed since the last `update` produces a notice.
- `test_env_vars.py`: only the given keys change in the `.env`; nothing is built or rendered and the playbook runs without pull or forced recreate; keys with a `value` and undeclared keys are refused; a strategy without `set_env_vars` refuses; the menu and the subcommand arrive at the same operation.

## Harness surface

- `references/commands.md` regenerates for `env vars`.
- The skill must say that `release` deploys the working directory's compose file without rendering, so a config change needs `update`; and that `release` and `update` ask only for empty or new env values, so a set one is changed with `env vars`.
