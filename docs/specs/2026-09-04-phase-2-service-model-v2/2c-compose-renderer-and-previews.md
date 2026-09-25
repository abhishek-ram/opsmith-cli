# Phase 2c: Compose renderer and preview commands

**Goal:** a pure renderer turns the config, an environment's bindings and its answers into a compose file, an env file and mounted files, and three commands preview what it produces, before any deploy uses it.
**Depends on:** 2b.
**Size:** L.
**Lands as:** a part of phase 2; the phase ships as 1.0.0 with phase 3 once 2h lands.

## Scope

1. `ComposeRenderer` and `ComposeBundle`, with golden tests.
2. The compose templates: `base.yml`, `services/container.yml.j2`, and infra snippets that take `instance` and `settings`.
3. `opsmith compose render`, `compose diff` and `compose validate`.

## Non-goals

- Deploying with the renderer ([2d](2d-deterministic-deploys.md)). Until then the LLM compose path deploys, and the previews show what 2d will deploy.
- `deploy.replicas`, which [2g](2g-capacity-planning.md) adds from the capacity plan.

## Design

### Compose renderer

`opsmith/deployment_strategies/compose_renderer.py`:

```python
class ComposeBundle(BaseModel):
    compose_yaml: str
    env_file: Dict[str, str]                 # KEY -> value, secrets included
    secret_keys: List[str]                   # env_file keys holding a secret, from secret_keys(config)
    files: Dict[str, str]                    # relative path under files/ -> content
    init_jobs: List[Tuple[str, InitJob]]     # (service slug, job)

class ComposeRenderer:
    def render(self, config, environment, env_state, images: Dict[str, str], answers: Dict[str, str]) -> ComposeBundle
```

Rules:

- Build a dict, not text. Load `base.yml` (traefik, logging anchor, network), add one entry per container service from a single template `services/container.yml.j2`, one per infra instance from the provider spec, then `yaml.safe_dump`.
- Container service entry: `image` (from `images[slug]` for build sources, `image:tag` for image sources), `command`, `entrypoint`, `environment` as `KEY=${KEY}` references for every resolved env var, `volumes` for named volumes and `./files/<slug>/<n>:<path>:ro` for files, `healthcheck` translated to compose form, `depends_on`, `restart: always`, the logging anchor, and traefik labels per route: router `<slug>-r<i>` with rule ``Host(`<domain>`) && PathPrefix(`<prefix>`)``, service `<slug>-r<i>` pointing at `route.port`, plus the existing https redirect and security-headers middlewares. A service with routes but no domain in the environment is a validation error before rendering.
- Infra entry: compose key is the instance name; env from provider spec and `settings`; password from the persisted secret; named volume `<instance>-data`.
- Top-level `volumes:` lists every named volume once.
- `service_type` no longer affects rendering; it selects Dockerfile templates and, for `STATIC_SITE`, the CDN path only. The per-type snippets stay in place for the LLM path until 2d deletes them.
- `.env` composition: infra passwords and `secret()` values, resolved `value` templates, then the env vars with no `value`. All of them arrive in `answers`, and the renderer asks nothing and generates nothing: 2d generates the secrets and asks for the rest before rendering.

The renderer is pure: same inputs, same bundle. Golden tests cover it.

Override files are not merged by the renderer. Phase 3 hands them to Compose, which merges them at deploy time, so the renderer stays pure and overrides survive upgrades.

The golden render of an upgraded v1 config is what proves compatibility guarantees 1, 3 and 5, and the key names in guarantee 2, listed in the [README](README.md#compatibility-with-existing-environments).

### Preview commands

Three of the commands the [harness spec](../2026-09-04-phase-1-harness-integration.md) lists ship here, because each one needs this phase's renderer and none of them could be written before it:

```
opsmith compose render   --env NAME      # rendered docker-compose.yml plus env keys, secret values masked
opsmith compose diff     --env NAME      # rendered file against the one currently on the VM
opsmith compose validate --env NAME      # renders, then runs `docker compose config` locally
```

They also give the first deterministic `update` something to be previewed with. `compose diff` fetches the remote compose file with `_fetch_remote_deployment_files` and prints a unified diff without deploying. Each command returns a typed result, so the generated `commands.md` names its model.

`compose render` masks values by `ComposeBundle.secret_keys` rather than by `is_secret` alone, because its output is read by agents. None of the three generates, prompts or writes to the working directory, whose `docker-compose.yml` is what `release` deploys. A secret not yet generated, and an env var not yet answered, are shown as placeholders, so previewing a new environment creates nothing.

## Code changes by file

| File | Change |
|------|--------|
| `opsmith/deployment_strategies/compose_renderer.py` | new |
| `opsmith/templates/docker_compose_snippets/` | `services/container.yml.j2`; infra snippets take `instance` and `settings`, which the LLM path passes as the provider until 2d |
| `opsmith/core/operations.py`, `opsmith/core/results.py` | the three preview operations and their typed results |
| `opsmith/cli/commands/` | `compose render`, `compose diff` and `compose validate` |

## Acceptance criteria

1. A fixture environment deployed from a v1 config with a captured LLM-style compose file, whose env file holds the legacy secret keys, renders a compose file with identical volume names, infra service keys and secret env keys, and `compose diff` shows only label and formatting changes (phase acceptance criterion 7).
2. `compose diff --env dev` prints the difference between the rendered file and the file fetched from the VM without deploying (phase acceptance criterion 8).
3. `compose render` masks a `DATABASE_URL` whose value is `{{ infra.postgresql.url }}` although the config does not mark it secret (the masking in phase acceptance criterion 15; 2d proves the rest).
4. None of the three commands generates a secret, asks a question or writes to the working directory.

## Tests

- `test_compose_renderer.py`: golden files for api+worker+postgres+redis, image-only service with routes and files, two routes on one service; a renderer that never generates.
- `test_compat_v1_environment.py`: golden render of an upgraded v1 config asserting legacy volume names, infra keys, secret keys and `depends_on`; `compose diff` against a captured LLM-generated file.
- The preview commands: masking by derived secret keys, placeholders for what is not generated or answered yet, and nothing written.

## Harness surface

- `references/commands.md` regenerates for the `compose` commands.
- `SKILL.md`: the validate loop gains `compose render`, `compose diff` and `compose validate`.
