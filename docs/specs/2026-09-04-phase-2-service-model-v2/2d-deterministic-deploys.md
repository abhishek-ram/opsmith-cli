# Phase 2d: Deterministic deploys

**Goal:** `env create` and `update` deploy the renderer's output instead of a compose file the model writes, with secrets generated once before rendering, env values asked only when empty or new, and a first render of a v1 environment that cannot silently drop what the model had added.
**Depends on:** 2c.
**Size:** L.
**Lands as:** a part of phase 2; the phase ships as 1.0.0 with phase 3 once 2h lands.

## Scope

1. Rendering in `env create` and `update`: secrets generated and env values asked before rendering, and rendered files written outside the repository.
2. The model-driven compose loop deleted, with its prompt, snippets and tool.
3. `compose_renderer_version`, counted by change detection.
4. The first-render check for environments deployed before this phase.
5. Domains asked for by routes.
6. Detection writing v2: the generated field list, references and `secret()`.

## Non-goals

- The new validation ([2f](2f-validation-repair-and-init-jobs.md)). Until it lands, today's log-validation prompt decides whether a deploy succeeded, with no regeneration: a failed deploy is `DEPLOY_UNHEALTHY`, with the `compose.edit` fallback in a terminal.
- `release` ([2e](2e-release-and-env-values.md)). Until it lands, `release` keeps redeploying the compose file it fetches from the machine, which after this part is the file `update` rendered.

## Design

### Deploying the render

`MonolithicDeploymentStrategy._generate_docker_compose` is replaced by these steps in `env create` and `update`:

1. Generate the secrets not yet persisted and ask for the env vars with no `value` that are empty or new, both into `answers`, as [2b](2b-references-secrets-and-providers.md) describes. Then `bundle = renderer.render(...)`.
2. Write `docker-compose.yml` under `environments/<env>/docker_compose_deploy/`; it holds only `${KEY}` references. Write `files/**` outside the repository, under the environment's state directory beside `answers.yml` (`project_state_dir`), because a file template may resolve a password and the working directory is inside the repository.
3. Run the deploy playbook, which gains copying `files/` alongside the compose file, and judge the result with today's log-validation prompt until 2f replaces it.

### Asking for env values

The env vars with no `value` are asked before rendering as `envvar.<KEY>`, but only when their value is empty or they are new, meaning not yet in the machine's `.env`. On `env create` every one is new. A new key is asked with its `default_value` as the default; a value already set is kept, and changed with `env vars` (2e); a value given with `--env-var` is always applied.

### What goes

The model-driven compose loop, `DOCKER_COMPOSE_GENERATION_PROMPT_TEMPLATE`, the per-type compose snippets that [2a](2a-schema-v2-and-upgrade.md) renamed, and the `generate_secret` agent tool, which only that prompt used. `secret()` keeps using `generate_secret_string` for `alnum`.

### The renderer's version

`_detect_configuration_changes` also counts the compose file as changed when `state.yml` records no `compose_renderer_version`, or one older than this opsmith's renderer. `env create` and `update` write that version with the deployed snapshots. So the first `update` after upgrading renders even when the config is unchanged, after the check below, and a renderer whose output changes in a later release reaches each environment the same way.

This is what makes compatibility guarantee 6 in the [README](README.md#compatibility-with-existing-environments) hold: the first `update` reports no spurious service or infra changes, and the compose file is the one change it does report. Without it that `update` would do nothing, since `update` does nothing when it finds no changes.

### The first render of a v1 environment

The first render can still drop something the model added to the v1 file beyond what the config says. The v1 compose prompt had the model work out values itself, and the v1 env confirmation kept keys it invented, so an environment can be running with, say, a `DATABASE_URL` that detection never declared. While `state.yml` has no `compose_renderer_version`, `update` therefore checks its render before deploying it:

- It compares the render with the working directory's copy, which is what the machine runs, fetching the machine's copy first when there is no local one. For each service it lists what the render would drop: env keys, volumes, command, entrypoint and ports. Expected differences, such as Traefik router names, key order and formatting, are left out.
- An empty list goes ahead. Otherwise `update` shows the list and asks `update.confirm_compose_upgrade`. Declining ends the update without deploying, so the person can declare what to keep and run it again. Headless, it stops with exit 3 and the list in `details`. The key is destructive, so it takes no default and is never persisted.
- To keep an env var, declaring its key in `deployments.yml` is enough. Its current value is already in the machine's `.env`, so it is kept rather than asked for.

Every later render comes from the config, so the check runs only on the first.

### Domains

`_collect_domain_configuration` asks for services with routes plus `STATIC_SITE` services.

### Detection

Minimal edits so detection emits v2: the field list in `REPO_ANALYSIS_PROMPT_TEMPLATE` is generated from the pydantic JSON schema instead of being hand-written; the prompt instructs that infra-derived env vars use references such as `{{ infra.postgresql.url }}` in `value` and that `routes` be filled for web services.

It also has detection sort every secret env var with the test in 2b. A value the app creates and checks itself gets `secret()`, with a `format` where the framework needs one. A key someone else issues gets no `value`, and so does any secret detection is unsure about. The evidence is how the code uses the variable, not its name: an `API_KEY` checked against incoming requests is the app's own, while one sent in outgoing calls belongs to a vendor. Detection writes the declaration, never a value. Full prompt rewrite is phase 6.

## Code changes by file

| File | Change |
|------|--------|
| `opsmith/deployment_strategies/monolithic.py` | the model-driven compose loop removed; secret generation and env confirmation before rendering; rendered files outside the repository; `compose_renderer_version` in change detection; the first-render drop check; domains by routes |
| `opsmith/types.py` | `compose_renderer_version` in `MonolithicDeploymentState` |
| `opsmith/core/answers.py` | `update.confirm_compose_upgrade` joins `DESTRUCTIVE_KEYS`; its row is already in the migration plan's key table |
| `opsmith/templates/docker_compose_snippets/services/` | the per-type snippets deleted |
| `opsmith/templates/docker_compose_deploy/*/main.yml` | copy `files/` |
| `opsmith/prompts.py` | delete the compose generation prompt; generate the detection field list; tell detection when a secret gets `secret()` and when it is prompted |
| `opsmith/agent.py` | drop the `generate_secret` tool, which only the deleted compose prompt used; `secret()` keeps using `generate_secret_string` for `alnum` |

## Acceptance criteria

1. A v1 project still loads, validates and deploys without edits, now through the renderer (phase acceptance criterion 1).
2. Infra passwords and `secret()` values are generated once, recorded in `secrets.yml` before the stack is deployed, and reused on the next render; an `is_secret` env var with no `value` is prompted and never generated (phase acceptance criterion 14).
3. Rendered files are written outside the repository (the rest of phase acceptance criterion 15).
4. A `secret()` value of each format is generated once and read back unchanged on the next deploy (the rest of phase acceptance criterion 18).
5. `update` asks only for env values that are empty or not yet in the machine's `.env`; a value already set is kept, and one given with `--env-var` is applied (phase acceptance criterion 19).
6. On an environment deployed before this phase, with the config unchanged, the first `update` reports the compose file as its only change and renders it, and a second `update` reports no changes (phase acceptance criterion 20).
7. On an environment deployed before this phase whose compose file sets an env var the config does not declare, the first `update` lists it and asks `update.confirm_compose_upgrade` before deploying, and a headless run stops with exit 3 and the list in `details`. Once the key is declared in `deployments.yml`, the check passes and the value in the machine's `.env` is kept without a prompt (phase acceptance criterion 21).

## Tests

- `test_monolithic_strategy.py`: secrets recorded before the deploy playbook runs; rendered files written outside the repository.
- `test_monolithic_update.py`: only env values that are empty or new are asked, a value already set is kept, and `--env-var` is applied.
- `test_compat_v1_environment.py`: with the config unchanged, the first `update` renders and records `compose_renderer_version`, and the second reports no changes; the drop check against a captured file with an invented env key, a volume and a command: listed, declined, confirmed, stopping with exit 3 headless, and passing once the key is declared.

## Harness surface

- `references/references.md` is new: the reference grammar for infra, services, domains and inputs, and `secret()` with its formats, with a worked example of each. Hand-written against the resolver, since nothing generates a grammar.
- `SKILL.md`: the hand-authoring section covers routes, volumes, files and env values with references, and gives a harness the test detection uses. `secret()`, with a `format` where the framework needs one, is only for a value the app creates and checks itself; a key someone else issues gets no `value`, and so does any secret the harness is unsure about.
- The skill must say that the first `update` of an environment deployed before 1.0 can stop on `update.confirm_compose_upgrade` with a list of what its render would drop, and that declaring a listed env key in `deployments.yml` keeps it with its current value.
