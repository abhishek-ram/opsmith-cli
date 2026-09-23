# Phase 2: Service model v2 and deterministic rendering

**Goal:** the config describes services in a strategy-neutral way, including prebuilt images, and the monolithic strategy renders docker-compose, env files and mounted files deterministically, with the model reserved for judgment: estimating what the expected workload needs and explaining failures.
**Depends on:** phase 0.
**Size:** XL.
**Ships as:** 1.0.0 (schema version 2), in the same release as phase 3.

## Scope

1. Schema v2 for `deployments.yml`: image sources, routes, volumes, files, command, healthcheck, resources, init jobs, depends-on, infra instances with settings, env values with references.
2. The binding contract between config and strategies.
3. A deterministic compose renderer replacing `_generate_docker_compose`'s LLM loop.
4. Deterministic post-deploy validation, with the model explaining failures.
5. Capacity planning as a strategy hook: the model estimates what the workload needs, the strategy decides the machines.
6. Deploy flow changes: registry and build only when something is buildable.
7. In-memory upgrade of v1 configs.

## Non-goals

- Recipes themselves (phase 5). This phase makes them possible.
- Changing detection prompts beyond what the schema forces (phase 6 rewrites them).
- Kubernetes. The model is designed so a future strategy maps volumes to PVCs, routes to ingress, files to config maps, healthchecks to probes, resources to requests, and init jobs to jobs, and plans node pools through the capacity hook, without schema changes.

## Design

### Schema v2

`DeploymentConfig` gains `schema_version: int = 2`. New and changed models in `opsmith/types.py`:

```python
class BuildSource(BaseModel):
    kind: Literal["build"] = "build"
    context: str = "."                      # build context relative to repo root
    dockerfile: Optional[str] = None        # default .opsmith/docker/<slug>/Dockerfile

class ImageSource(BaseModel):
    kind: Literal["image"] = "image"
    image: str                              # "odoo", "ghcr.io/org/app"
    tag: str = "latest"
    platforms: List[str] = ["linux/amd64"]  # used for architecture selection

class Route(BaseModel):
    path_prefix: str = "/"
    port: int
    strip_prefix: bool = False

class VolumeMount(BaseModel):
    name: str                               # [a-z0-9_]+, unique per config
    path: str                               # absolute path inside the container
    backup: bool = False                    # consumed by phase 8

class FileMount(BaseModel):
    path: str                               # absolute path inside the container
    content: Optional[str] = None           # template, resolved with the render context
    source: Optional[str] = None            # path under .opsmith/files/ or a recipe's files/ dir; exclusive with content
    mode: str = "0644"

class HealthCheck(BaseModel):
    http_path: Optional[str] = None
    cmd: Optional[List[str]] = None         # exclusive with http_path
    port: Optional[int] = None
    interval_s: int = 10
    timeout_s: int = 5
    retries: int = 5
    start_period_s: int = 30

class Resources(BaseModel):
    """What one instance of the service needs at light load. Scaled per environment by the capacity plan."""
    min_ram_gb: float
    recommended_ram_gb: Optional[float] = None
    min_cpu: float = 0.25
    storage_gb: Optional[float] = None       # for services with volumes

class InitJob(BaseModel):
    name: str
    command: List[str]
    when: Literal["first_deploy", "every_release"] = "every_release"

class EnvVarConfig(BaseModel):
    key: str
    is_secret: bool = False
    value: Optional[str] = None             # template; resolved at render time; never prompted
    default_value: Optional[str] = None     # prompted with this default when value is None

class ServiceInfo(BaseModel):
    name_slug: str
    source: Union[BuildSource, ImageSource] = Field(default_factory=BuildSource, discriminator="kind")
    service_type: ServiceTypeEnum
    language: Optional[str] = None          # required when source.kind == "build"
    language_version: Optional[str] = None
    framework: Optional[str] = None
    service_port: Optional[int] = None
    build_cmd / build_dir / build_path      # unchanged, FRONTEND only
    routes: List[Route] = []
    env_vars: List[EnvVarConfig] = []
    volumes: List[VolumeMount] = []
    files: List[FileMount] = []
    command: Optional[List[str]] = None
    entrypoint: Optional[List[str]] = None
    healthcheck: Optional[HealthCheck] = None
    resources: Optional[Resources] = None
    init_jobs: List[InitJob] = []
    depends_on: List[str] = []              # service slugs or infra instance names

class InfrastructureDependency(BaseModel):
    instance: str                           # default: provider value; unique per config
    dependency_type: DependencyTypeEnum
    provider: InfrastructureProviderEnum
    version: str = "latest"
    settings: Dict[str, str] = {}           # provider-specific: username, database, extra env
```

Validation rules, all enforced by `config validate`:

- A service is **exposed** iff `routes` is non-empty. `FRONTEND` services never have routes or containers; they keep the CDN path.
- `route.port` must be `service_port` or another port the container serves; duplicates of `path_prefix` within a service are errors.
- `volumes[].name` unique across the config; `files[].path` unique per service; `content` xor `source`.
- `depends_on` entries must exist as a service slug or an infra instance.
- `language` required for build sources.
- Infra `instance` unique; `provider` must be compatible with `dependency_type` (existing `COMPATIBLE_PROVIDERS`).
- Every `{{ ... }}` reference in `env_vars[].value` and `files[].content` must resolve against the reference grammar below (checked without a strategy).

### Reference grammar and bindings

Templates use Jinja2 with `StrictUndefined`, expression-only, plus one function `secret(length=32)`. Allowed roots:

| Root | Attributes | Bound by |
|------|-----------|----------|
| `infra.<instance>` | `host`, `port`, `username`, `password`, `database`, `url` | strategy |
| `services.<slug>` | `host`, `port` | strategy |
| `domains.<slug>` | string | environment config |
| `inputs.<KEY>` | string | recipe inputs (phase 5); empty until then |
| `env` | environment name | config |
| `app` | app slug | config |

Core types in `opsmith/core/render.py`:

```python
class InfraBinding(BaseModel):
    host: str; port: int; username: Optional[str]; password: Optional[str]; database: Optional[str]; url: Optional[str]

class ServiceBinding(BaseModel):
    host: str; port: Optional[int]

class RenderContext(BaseModel):
    infra: Dict[str, InfraBinding]; services: Dict[str, ServiceBinding]; domains: Dict[str, str]
    inputs: Dict[str, str]; env: str; app: str

def validate_references(config: DeploymentConfig) -> list[ReferenceError]
def resolve_env_vars(service: ServiceInfo, ctx: RenderContext, answers: Dict[str, str]) -> Dict[str, str]
def resolve_files(service: ServiceInfo, ctx: RenderContext) -> Dict[str, str]     # container path -> content
```

`secret()` is stable per environment and key: generated once, persisted in the environment's env store (the `.env` on the VM for the monolithic strategy), reused on later renders. The renderer receives previously persisted values as `answers`.

### Infra provider specs

Provider knowledge moves out of prompts and snippets into one registry, `opsmith/infra/providers.py`:

```python
class InfraProviderSpec(BaseModel):
    provider: InfrastructureProviderEnum
    default_port: int
    url_template: Optional[str]         # "postgresql://{username}:{password}@{host}:{port}/{database}"
    default_username: Optional[str]     # "{app}" or fixed
    default_database: Optional[str]
    secret_env: Dict[str, str]          # legacy env key -> binding attribute, e.g. {"POSTGRES_PASSWORD": "password"}
    default_ram_gb: float               # for sizing
    default_cpu: float
    image_for(version, arch) -> str
    settings_keys: list[str]            # accepted keys in InfrastructureDependency.settings
```

Values for the eight providers follow the existing snippets (postgresql 5432, mysql 3306, mongodb 27017, redis 6379, rabbitmq 5672, kafka 9092, elasticsearch 9200, weaviate 8080) with the sizing defaults from the current machine-requirements prompt.

`secret_env` records the env keys the current compose snippets already use, because the env file on every deployed VM holds secrets under exactly these names:

| Provider | `secret_env` |
|----------|--------------|
| postgresql | `POSTGRES_PASSWORD` → password |
| mysql | `MYSQL_PASSWORD` → password, `MYSQL_ROOT_PASSWORD` → root_password |
| mongodb | `MONGO_INITDB_ROOT_USERNAME` → username, `MONGO_INITDB_ROOT_PASSWORD` → password |
| rabbitmq | `RABBITMQ_DEFAULT_USER` → username, `RABBITMQ_DEFAULT_PASS` → password |
| redis, kafka, elasticsearch, weaviate | none; `password` binds to null and the URL carries no credentials |

### Monolithic bindings

```
infra.<instance>.host      = <instance>            (compose service key)
infra.<instance>.port      = spec.default_port
infra.<instance>.username  = settings.username, else the persisted secret for the provider's username key, else spec.default_username
infra.<instance>.password  = persisted secret under the provider's password key from secret_env
infra.<instance>.database  = settings.database or spec.default_database
infra.<instance>.url       = spec.url_template formatted
services.<slug>.host       = <slug>
services.<slug>.port       = service_port
```

Secret env keys are the legacy names from `secret_env` for the default instance of a provider, the one whose instance name equals the provider value, and `<INSTANCE_UPPER>_<KEY>` for any additional instance, for example `ODOO_DB_POSTGRES_PASSWORD`. A persisted value is always read from the env file fetched from the VM before a new one is generated, so an upgraded environment keeps the credentials its database was initialised with.

### Compose renderer

`opsmith/deployment_strategies/compose_renderer.py`:

```python
class ComposeBundle(BaseModel):
    compose_yaml: str
    env_file: Dict[str, str]                 # KEY -> value, secrets included
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
- The three files `services/backend_api.yml`, `backend_worker.yml`, `full_stack.yml` are deleted. `service_type` no longer affects rendering; it selects Dockerfile templates and the frontend path only.
- `.env` composition: infra passwords, `secret()` values, resolved `value` templates, then prompted keys (`envvar.<KEY>`) for env vars with no `value`, defaulting to persisted answers, then `default_value`.

The renderer is pure: same inputs, same bundle. Golden tests cover it.

Override files are not merged by the renderer. Phase 3 hands them to Compose, which merges them at deploy time, so the renderer stays pure and overrides survive upgrades.

### Deploy and validation flow

`MonolithicDeploymentStrategy._generate_docker_compose` and `_deploy_validate_docker_compose` are replaced by:

1. `bundle = renderer.render(...)`
2. Write `docker-compose.yml`, `files/**` under `environments/<env>/docker_compose_deploy/`.
3. Run the deploy playbook. It gains: copy `files/` alongside the compose file; after `compose up`, wait up to the largest `start_period_s` (default 60s) and return `docker compose ps --format json` as `OPSMITH_OUTPUT_COMPOSE_PS` in addition to logs.
4. Deterministic validation: every container service is `running` and, if it has a healthcheck, `healthy`; infra services `running`; no service `restarting` or `exited` non-zero. Failure raises `DEPLOY_UNHEALTHY` (exit 4) with the ps table and the last 200 log lines per unhealthy service in `details`.
5. Init jobs: after a healthy `compose up`, run `first_deploy` jobs on `env create` and `every_release` jobs on `release` and `update`, using the `docker_compose_run` playbook, in config order. A failing job fails the command with `DEPLOY_UNHEALTHY`.
6. Explanation: when validation fails, the model analyses the ps table and logs with the existing log-analysis prompt, and its `explanation` is attached to the error details and shown to the user. No regeneration loop.
7. Interactive fallback: `interact.edit("compose.edit", ...)` lets a terminal user patch the compose file and redeploy; headless mode exits 4.

`release` keeps fetching the remote `.env` so persisted secrets and confirmed answers survive; `_fetch_remote_deployment_files` stays.

### Preview commands

Three of the commands the [harness spec](2026-09-04-phase-1-harness-integration.md) lists ship here, because each one needs this phase's renderer and none of them could be written before it:

```
opsmith compose render   --env NAME      # rendered docker-compose.yml plus env keys, secret values masked
opsmith compose diff     --env NAME      # rendered file against the one currently on the VM
opsmith compose validate --env NAME      # renders, then runs `docker compose config` locally
```

They also give the first deterministic release something to be previewed with. `compose diff` fetches the remote compose file with `_fetch_remote_deployment_files` and prints a unified diff without deploying. The changelog for this release states that the first `release` or `update` regenerates the compose file on the VM and points at `compose diff`.

### Capacity planning

How many machines an environment needs, and of what kind, is a strategy decision. The monolithic strategy needs exactly one VM. A Kubernetes strategy would need node pools with counts and sizes. A managed-container strategy would need per-service CPU and memory settings and no machines at all. The core therefore owns the inputs and the model's estimate, and the strategy owns the plan.

**Inputs.**

- `ServiceInfo.resources`: what one instance needs at light load. Declared by recipes or detection, or estimated by the model below and written into the config after confirmation.
- `InfraProviderSpec` defaults per infra instance, plus `settings`.
- `DeploymentEnvironment.workload`: what the environment is expected to serve. Per environment, because staging and production differ.

```python
class WorkloadProfile(BaseModel):
    description: Optional[str] = None                  # the user's own words, kept for re-planning
    tier: Literal["hobby", "internal", "production"] = "hobby"
    peak_concurrent_users: Optional[int] = None
    peak_requests_per_second: Optional[float] = None
    availability: Literal["single", "high"] = "single"
    data_size_gb: Optional[float] = None               # expected stored data over the first year
```

`env create` prompts `env.workload.tier` and `env.workload.description` (flags `--workload-tier`, `--workload`); the numeric fields are optional prompts `env.workload.<field>` whose defaults come from the tier. `update` re-prompts only when asked with `--workload`.

**Estimate, a model step.** Turning a workload into replica counts and per-replica sizes depends on the language, framework, service type and dependencies, which is judgment:

```python
class ServiceCapacity(BaseModel):
    slug: str
    replicas: int                                       # recommended for this workload
    cpu: float                                          # per replica
    ram_gb: float                                       # per replica
    storage_gb: Optional[float] = None
    bound_by: Literal["cpu", "memory", "io"]
    rationale: str

class InfraCapacity(BaseModel):
    instance: str
    cpu: float
    ram_gb: float
    storage_gb: float
    rationale: str

class CapacityEstimate(BaseModel):
    services: List[ServiceCapacity]
    infra: List[InfraCapacity]
    baseline_resources: Dict[str, Resources]            # for services that had none; written to config after confirmation
```

`opsmith/core/capacity.py::estimate_capacity(config, environment, agent) -> CapacityEstimate` runs the `capacity_estimate` prompt once with the services, infra instances and workload profile as input. It never sees machine types; it reasons about the application only, which is what keeps it strategy-neutral. It replaces the machine-list prompt, whose input was the whole instance catalog.

**Plan, a strategy step.**

```python
class MachinePlan(BaseModel):
    role: str                                           # "app" for monolithic; pool names for Kubernetes
    instance_type: str
    architecture: CpuArchitectureEnum
    count: int

class CapacityPlan(BaseModel):
    services: Dict[str, ServiceCapacity]                # as adjusted by the strategy, e.g. replicas clamped
    infra: Dict[str, InfraCapacity]
    machines: List[MachinePlan]
    warnings: List[str]
    unsupported: List[str]                              # requirements the strategy cannot meet
    alternatives: List[MachinePlan] = []

class BaseDeploymentStrategy:
    @abc.abstractmethod
    def plan_capacity(self, config, environment, estimate: CapacityEstimate,
                      machine_types: MachineTypeList) -> CapacityPlan: ...
```

The plan is shown with its rationale and confirmed with `env.capacity.confirm` (`--answer env.capacity.confirm=true` headless); for monolithic, `--instance-type` overrides the machine choice. It is stored in `state.yml` as `capacity_plan` together with the workload profile it was derived from.

Monolithic implementation of `plan_capacity`:

1. Clamp `replicas` to 1 for services with volumes. Other services keep the estimate's replicas, rendered as compose `deploy.replicas`, which Traefik load-balances across.
2. RAM = 0.5 (OS and docker) + Σ services replicas × ram_gb + Σ infra ram_gb, times 1.3 headroom. CPU likewise, rounded up.
3. Architecture from the image-platform rule below.
4. The smallest machine type by RAM then CPU that satisfies both; the next two larger are `alternatives`.
5. `availability: high` goes into `unsupported` with the advice to choose a multi-node strategy when one exists. A plan that fits no available machine type fails with `CAPACITY_UNSATISFIABLE` before any infrastructure is created.

A Kubernetes strategy would implement the same hook differently: a system pool with a fixed overhead, application pools bin-packed from replicas × per-replica requests with headroom, at least three nodes per pool for `availability: high`, and one `MachinePlan` per pool. Neither the config nor the estimate changes.

**Re-planning.** `update` re-runs the estimate when services, infra instances or the workload profile changed, and shows the difference against the stored plan. For monolithic, a changed instance type is applied only through `env resize --env NAME [--instance-type T]`, a Terraform apply of the new type with a stop and start of the VM, never implicitly.

### Deploy flow changes

- `buildable = [s for s in services if s.source.kind == "build" and s.service_type != FRONTEND]`. Registry setup and `_build_and_push_images` run only when `buildable` is non-empty. Image sources populate `images[slug] = f"{image}:{tag}"`.
- Multi-arch: build sources are built for both platforms already. The VM architecture is arm64 only if every image source lists `linux/arm64`.
- `_select_virtual_machine_type` is replaced by the capacity planning step above, run before the registry and the builds since it depends only on the config and the workload.
- `_confirm_dns_records` becomes `interact.wait_for` with a resolver check per record, querying the zone's authoritative nameservers first, replacing the fixed fifteen-second sleep. Certificate validation on AWS is the only step that blocks on it; the Traefik and GCP records are advisory, so the run continues and reports them. Allocating the static IP before the VM so the records are known early is an optional internal improvement, not part of the strategy contract.
- `_collect_domain_configuration` asks for services with routes plus `FRONTEND` services.
- `_detect_configuration_changes` compares the full `model_dump` of each service and infra instance, keyed by slug and instance, and reports changed field names.

### Upgrade from v1

`opsmith/core/config.py::upgrade_config(data: dict) -> dict`, applied on load when `schema_version` is missing:

- every service gets `source: {kind: build}`;
- `BACKEND_API` and `FULL_STACK` with `service_port` get `routes: [{path_prefix: "/", port: service_port}]`; `BACKEND_WORKER` gets none;
- infra deps get `instance = provider`;
- every container service gets `depends_on` listing every infra instance, which is what the LLM-generated compose files declared;
- env vars keep `default_value`; no `value` is invented.
- `MonolithicDeploymentState.deployed_services` snapshots are upgraded the same way on load so change detection does not report a spurious full change.

The first save after an upgrade asks `config.upgrade` (`--answer config.upgrade=true` headless) and writes `.opsmith/deployments.v1.bak.yml`.

### Detection prompt adjustments

Minimal edits so detection emits v2: the field list in `REPO_ANALYSIS_PROMPT_TEMPLATE` is generated from the pydantic JSON schema instead of being hand-written; the prompt instructs that infra-derived env vars use references such as `{{ infra.postgresql.url }}` in `value` and that `routes` be filled for web services. Full prompt rewrite is phase 6.

## Compatibility with existing environments

An environment deployed before this phase has an LLM-generated compose file and an env file on the VM, data in Docker named volumes, and a v1 config. The first `release` or `update` after upgrading regenerates the compose file deterministically. The following invariants make that a no-op for data and credentials, and each has a test:

1. **Volume names.** Infra volumes are named `<instance>-data`, which for an upgraded v1 instance equals the legacy `<provider>-data` used by the current snippets. The compose project keeps living in `/home/<user>/app` on the VM, so Docker's derived volume names such as `app_postgresql-data` do not change and existing data is reattached.
2. **Secret keys.** Infra credentials are read from the fetched env file under the legacy keys in `secret_env` before anything is generated, so the database keeps the password it was initialised with and connection strings stay valid.
3. **Start ordering.** The v1 upgrade adds `depends_on` for every infra instance to every container service, matching the LLM-generated files.
4. **Application env values.** Confirmed values from the fetched env file are the defaults for every prompted key, and the upgrade invents no `value` templates, so no application variable changes unless the user edits the config.
5. **Traefik.** Router names change to the per-route scheme, which Traefik reconfigures live; `acme.json` on the VM is untouched, so certificates are kept.
6. **Change detection.** Deployed snapshots are upgraded the same way as the config, so the first `update` reports no spurious changes.
7. **Capacity.** Existing environments have no stored plan and a default hobby workload. `update` neither re-plans nor resizes them unless the workload profile is edited, and `env resize` is always explicit.

Hand edits to the generated compose file on the VM are overwritten by the first release. Phase 3 ships in the same release and provides `overrides/compose.override.yml` as the place to keep them; `compose diff` shows what would change before releasing.

## Harness surface

This phase invalidates more of the skill than any other, because it replaces the thing a harness
writes. Per the [definition of done](../notes/2026-09-04-migration-plan.md#definition-of-done-for-a-phase):

- `references/config-schema.md` regenerates for schema v2. The freshness test fails until it is committed.
- `references/references.md` is new: the reference grammar for infra, services, domains and inputs, with a worked example of each. Hand-written against the resolver in this phase, since nothing generates a grammar.
- `references/commands.md` regenerates for the `compose` commands and for `env plan`'s extended result.
- `SKILL.md`: the hand-authoring section is rewritten for image sources, routes, volumes, files, resources and init jobs; the validate loop gains `compose render`, `compose diff` and `compose validate`; the troubleshooting table gains `DEPLOY_UNHEALTHY` and `CAPACITY_UNSATISFIABLE`, each with the command that follows it.
- The skill must say that a v1 config is upgraded in memory and rewritten on the next save, because a harness will meet both shapes in repositories it did not write.

## Code changes by file

| File | Change |
|------|--------|
| `opsmith/types.py` | models above; `WorkloadProfile` on the environment; `capacity_plan` in `MonolithicDeploymentState`; validators; `schema_version` |
| `opsmith/core/capacity.py` | estimate models and the `capacity_estimate` model step |
| `opsmith/core/config.py` | load with upgrade, save with backup, `validate_references` hook |
| `opsmith/core/render.py` | bindings, reference grammar, resolvers |
| `opsmith/infra/providers.py` | provider spec registry |
| `opsmith/deployment_strategies/compose_renderer.py` | new |
| `opsmith/deployment_strategies/monolithic.py` | remove the model-driven compose loop and the machine-list prompt; implement `plan_capacity`, `resize`, env confirmation through renderer answers, deterministic validation, failure explanation and init-job steps |
| `opsmith/deployment_strategies/base.py` | `plan_capacity` abstract hook; buildable filter; images for image sources |
| `opsmith/templates/docker_compose_snippets/` | `services/container.yml.j2` replaces three files; infra snippets take `instance` and `settings` |
| `opsmith/templates/docker_compose_deploy/*/main.yml` | copy `files/`, wait loop, `compose ps` output |
| `opsmith/prompts.py` | delete the compose generation prompt; replace the machine-list prompt with the capacity-estimate prompt; generate field list |
| `opsmith/templates/virtual_machine/gcp/main.tf` | `allow_stopping_for_update` so a machine type change can be applied |
| `opsmith/main.py` / cli | `config validate` runs reference validation; `env create` gains `--instance-type` semantics above; `compose render`, `compose diff`, `compose validate` and `env resize` |
| `README.md` | schema v2 documentation with examples for build and image sources |

## Acceptance criteria

1. A v1 project loads, validates, and deploys without edits; the first save writes a v2 file and a backup.
2. `env create` for a config with one `BACKEND_API` build service, one worker, postgres and redis makes exactly one model call between detection and a healthy result, the capacity estimate, and renders a compose file identical to the golden file.
3. A config with a single image source and no build sources runs `env create` without a registry or a build.
4. A service with `healthcheck.http_path` that never becomes healthy fails with `DEPLOY_UNHEALTHY` and includes its logs in `details`.
5. An `every_release` init job runs on `release` and its failure fails the release.
6. `config validate` rejects a bad reference such as `{{ infra.nope.url }}` with the path to the offending env var.
7. A fixture environment deployed from a v1 config with a captured LLM-style compose file, whose env file holds the legacy secret keys, renders a compose file with identical volume names, infra service keys and secret env keys, and `compose diff` shows only label and formatting changes.
8. `compose diff --env dev` prints the difference between the rendered file and the file fetched from the VM without deploying.
9. A service without `resources` gets its baseline from the capacity estimate, written to the config after confirmation and not requested again; the estimate runs once per `env create` and on `update` only when services, infra instances or the workload changed.
10. A workload with `availability: high` produces a plan whose `unsupported` list names it, and `env create` proceeds only after the user confirms the plan; a plan that fits no machine type fails with `CAPACITY_UNSATISFIABLE` before any infrastructure is created.
11. A stub strategy implementing `plan_capacity` with two node pools receives the same `CapacityEstimate` as the monolithic strategy for the same config and workload.

## Tests

- `test_types_v2.py`: validators, discriminator, uniqueness rules.
- `test_config_upgrade.py`: v1 fixtures upgrade to expected v2 dicts; backup written once.
- `test_render.py`: reference validation, `resolve_env_vars` precedence, `secret()` stability with persisted answers.
- `test_compose_renderer.py`: golden files for api+worker+postgres+redis, image-only service with routes and files, two routes on one service.
- `test_monolithic_validation.py`: ps-table evaluation matrix; init job ordering with fake provisioners.
- `test_capacity.py`: estimate prompt inputs with a scripted model; monolithic plan arithmetic, replica clamping, architecture rule, alternatives, `unsupported` and `CAPACITY_UNSATISFIABLE`; re-plan triggers on `update`; a stub multi-pool strategy against the same estimate.
- `test_compat_v1_environment.py`: golden render of an upgraded v1 config asserting legacy volume names, infra keys, secret keys and `depends_on`; `compose diff` against a captured LLM-generated file.

## Risks and open questions

- Traefik router priorities with overlapping prefixes rely on rule length; document that recipes should list the most specific route last for readability, ordering does not matter functionally.
- `FULL_STACK` versus `BACKEND_API`: keep both types for Dockerfile template selection; rendering treats them identically. Revisit when templates are consolidated.
- Users who hand-edited the LLM-generated compose file lose those edits on the first release after upgrading. The changelog must say so, `compose diff` shows the change, and the compose override file from phase 3, shipped in the same release, is where such edits go.
