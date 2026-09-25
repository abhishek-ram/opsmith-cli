# Phase 2a: Schema v2 and the v1 upgrade

**Goal:** `deployments.yml` gains schema version 2, with every field the later parts render, the renamed service types and the structural validation rules, and a v1 config is upgraded in memory on load and rewritten on the first save.
**Depends on:** phase 0.
**Size:** M.
**Lands as:** the first part of phase 2; the phase ships as 1.0.0 with phase 3 once 2h lands.

## Scope

1. The schema v2 models in `opsmith/types.py`, and `schema_version`.
2. The service types renamed, with `FULL_STACK` merged into `WEB_SERVICE`.
3. The structural validation rules, enforced by `config validate`.
4. `upgrade_config`, the upgrade of deployed snapshots, and the first save with a backup.
5. Change detection over the whole service and infra models.

## Non-goals

- Honouring the new fields. Routes, volumes, files, healthchecks, init jobs, resources and image sources are accepted and validated here, and take effect in the part that renders or runs them: 2c and 2d, 2f, 2g and 2h.
- References and `secret()` (2b), and detection writing them (2d).

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
    build_cmd / build_dir / build_path      # unchanged, STATIC_SITE only
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

`ServiceTypeEnum` is renamed for what each type still decides once `routes` carry exposure. `FULL_STACK` is merged into the type that replaces `BACKEND_API`, because the two already rendered the same way and no Dockerfile template told them apart:

| v1 | v2 | Meaning |
|----|----|---------|
| `BACKEND_API`, `FULL_STACK` | `WEB_SERVICE` | a container that listens on a port and serves requests, whether it is exposed through routes or only reached by other services |
| `BACKEND_WORKER` | `WORKER` | a container that listens on no port, such as a queue consumer or a scheduler |
| `FRONTEND` | `STATIC_SITE` | built into static files and served from a CDN, with no container |

- The meanings go into the `service_type` field's description, which detection reads through the generated field list. So an internal API is a `WEB_SERVICE` although it is not public, and a Next.js or Nuxt app that renders on a server is a `WEB_SERVICE`, not a `STATIC_SITE`.
- The type selects the Dockerfile template, and `python_backend_api` and `python_backend_worker` become `python_web_service` and `python_worker`. For a `STATIC_SITE` it also selects the CDN path.
- Slugs are not renamed. Detection builds them from the type, so new services get names such as `python_web_service_1`, but an existing slug names compose services, Dockerfile directories and CDN state, and stays as it is.
- Event step names such as `frontend` are part of the event schema, and keep their names too.

### Validation rules

Enforced by `config validate`. [2b](2b-references-secrets-and-providers.md) adds the three rules about references, `secret()` and duplicate env var keys.

- A service is **exposed** iff `routes` is non-empty. `STATIC_SITE` services never have routes or containers; they keep the CDN path.
- `route.port` must be `service_port` or another port the container serves; duplicates of `path_prefix` within a service are errors.
- `volumes[].name` unique across the config; `files[].path` unique per service; `content` xor `source`.
- `depends_on` entries must exist as a service slug or an infra instance.
- `language` required for build sources.
- Infra `instance` unique; `provider` must be compatible with `dependency_type` (existing `COMPATIBLE_PROVIDERS`).

### Upgrade from v1

`opsmith/core/config.py::upgrade_config(data: dict) -> dict`, applied on load when `schema_version` is missing:

- every service gets `source: {kind: build}`;
- `BACKEND_API` and `FULL_STACK` with `service_port` get `routes: [{path_prefix: "/", port: service_port}]`; `BACKEND_WORKER` gets none;
- `service_type` is renamed as in the table above: `BACKEND_API` and `FULL_STACK` become `WEB_SERVICE`, `BACKEND_WORKER` becomes `WORKER`, and `FRONTEND` becomes `STATIC_SITE`. Slugs are left alone;
- infra deps get `instance = provider`;
- every container service gets `depends_on` listing every infra instance, which is what the LLM-generated compose files declared;
- env vars keep `default_value`; no `value` is invented.
- `MonolithicDeploymentState.deployed_services` snapshots are upgraded the same way on load so change detection does not report a spurious full change.

The first save after an upgrade asks `config.upgrade` (`--answer config.upgrade=true` headless) and writes `.opsmith/deployments.v1.bak.yml`.

### Change detection

`_detect_configuration_changes` compares the full `model_dump` of each service and infra instance, keyed by slug and instance, and reports changed field names. Because the deployed snapshots are upgraded the same way as the config, an environment deployed before this part reports no spurious change. [2d](2d-deterministic-deploys.md) adds the compose renderer's version to what counts as a change.

### Until 2d: the LLM compose path

The model still writes the compose file until 2d replaces it, from per-type snippets that it looks up by `service_type`. So the snippets are renamed with the types: `services/backend_api.yml` becomes `services/web_service.yml`, `services/backend_worker.yml` becomes `services/worker.yml`, and `services/full_stack.yml`, which differed from the API snippet only by a trailing blank line, is deleted. Domains are still asked for by type, now `WEB_SERVICE` and `STATIC_SITE`, until 2d asks by routes.

### Detection

The hand-written field list in `REPO_ANALYSIS_PROMPT_TEMPLATE` gets the new type names with their meanings, and asks for `routes` on web services, so a freshly detected config is exposed the same way an upgraded one is. 2d replaces the list with one generated from the schema, together with the instructions for references and `secret()`.

## Code changes by file

| File | Change |
|------|--------|
| `opsmith/types.py` | the v2 models; `schema_version`; `ServiceTypeEnum` renamed, with `FULL_STACK` merged; structural validators; deployed snapshots upgraded on load |
| `opsmith/core/config.py` | load with upgrade, save with backup |
| `opsmith/deployment_strategies/monolithic.py` | the type renames; change detection over the full models |
| `opsmith/core/operations.py`, `opsmith/service_detector.py` | the type renames, including `ROUTED_SERVICE_TYPES`, `BUILDABLE_SERVICE_TYPES` and the slugs detection builds |
| `opsmith/templates/docker_compose_snippets/services/` | the per-type snippets renamed; `full_stack.yml` deleted |
| `opsmith/templates/dockerfiles/` | `python_backend_api` and `python_backend_worker` renamed to `python_web_service` and `python_worker` |
| `opsmith/prompts.py` | the detection field list's type names and `routes` |
| `README.md` | schema v2 documentation |

## Acceptance criteria

1. A v1 project loads, validates, and deploys without edits; the first save writes a v2 file and a backup (phase acceptance criterion 1, which every later part keeps true).
2. An environment deployed before this part reports no service or infra changes on `update`.
3. `config validate` rejects a violation of each structural rule with the path to the offending field.

## Tests

- `test_types_v2.py`: validators, discriminator, uniqueness rules.
- `test_config_upgrade.py`: v1 fixtures upgrade to expected v2 dicts, including the type renames and the `FULL_STACK` merge with slugs unchanged; backup written once.
- Change detection: an upgraded config against its upgraded snapshots reports no change.

## Harness surface

- `references/config-schema.md` regenerates for schema v2. The freshness test fails until it is committed.
- `SKILL.md`: the hand-authoring section gets the renamed service types. The skill must say that a v1 config is upgraded in memory and rewritten on the next save, because a harness will meet both shapes in repositories it did not write.
