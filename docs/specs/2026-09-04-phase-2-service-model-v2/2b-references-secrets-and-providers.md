# Phase 2b: References, secrets and provider specs

**Goal:** env values and file contents can reference infrastructure, services and domains through one grammar, app secrets are asked for with `secret()`, and what opsmith knows about each infra provider lives in one registry, all checked by `config validate` without a strategy.
**Depends on:** 2a.
**Size:** M.
**Lands as:** a part of phase 2; the phase ships as 1.0.0 with phase 3 once 2h lands.

## Scope

1. The reference grammar and the resolvers in `opsmith/core/render.py`.
2. `secret()` and its formats, and the rules for which secrets are generated.
3. The infra provider registry, `opsmith/infra/providers.py`.
4. The monolithic bindings.
5. The validation rules for references, `secret()` and duplicate env var keys.

## Non-goals

- Rendering ([2c](2c-compose-renderer-and-previews.md)), and generating or asking for anything during a deploy ([2d](2d-deterministic-deploys.md)). This part is functions and their tests; no command deploys with them yet.
- Detection writing references or `secret()` (2d). Until then only a hand-written config holds them, and `config validate` accepts them.

## Design

### Reference grammar and bindings

Templates use Jinja2 with `StrictUndefined`, expression-only, plus one function, `secret(length=32, format='alnum')`, described below. Allowed roots:

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
def generated_env_vars(config: DeploymentConfig) -> Dict[str, EnvVarConfig]       # env vars whose value calls secret(), by key
def generate_value(env_var: EnvVarConfig) -> str                                  # its literal text around one fresh secret()
def secret_keys(config: DeploymentConfig) -> set[str]                             # env keys holding a secret, declared or derived
def resolve_env_vars(service: ServiceInfo, ctx: RenderContext, answers: Dict[str, str]) -> Dict[str, str]
def resolve_files(service: ServiceInfo, ctx: RenderContext) -> Dict[str, str]     # container path -> content
```

Generation is only for credentials opsmith owns: an infra instance's password, and an app secret that the config asks for with `secret()`. Each is generated once, the first time the environment is deployed, and read back from then on. A third-party key is an env var with `is_secret: true` and no `value`. It is always prompted as `envvar.<KEY>` and never generated, because no generated value is ever right for it.

Which of the two an env var is gets decided when the config is written, and the deploy only reads the answer. The model decides at detection (2d), a person can change it in the `setup` review, and a harness or a recipe that writes the config decides for itself. The test is whether any random value of the right format would work:

- A value the app creates and checks itself gets `secret()`. Examples: Django's `SECRET_KEY`, a JWT signing key, a session or encryption key.
- A value someone else issues is prompted. Examples: a Stripe key, an OAuth client secret, a webhook secret, an SMTP password, a Sentry DSN.
- When in doubt it is prompted, because the two mistakes do not cost the same. A generated third-party key deploys and only fails later, at runtime; a prompted app secret costs one question.

`secret()` may appear once in an env var's `value`, with nothing around it but literal text: `"{{ secret() }}"`, or `"base64:{{ secret(32, format='base64') }}"` for Laravel's `APP_KEY`. The env var's key names the whole resolved value, and that value is what is stored and read back. That is why `config validate` rejects a `secret()` beside another reference, a second `secret()` in one value, and any `secret()` in `files[].content`, where there is no key to store it under. `format` sets the shape:

| `format` | Produces | Example |
|----------|----------|---------|
| `alnum` (default) | `length` letters and digits, safe unquoted in URLs, shells and `.env` files | Django `SECRET_KEY`: `secret(50)` |
| `hex` | `length` random bytes as hex | Rails `SECRET_KEY_BASE`: `secret(64, format='hex')` |
| `base64` | `length` random bytes as standard base64 | Laravel `APP_KEY`: `base64:` followed by `secret(32, format='base64')` |
| `urlsafe` | `length` random bytes as URL-safe base64, padded | a Fernet key: `secret(32, format='urlsafe')` |

`length` counts characters for `alnum` and random bytes for the other three, because keys in those encodings are specified in bytes. A secret that needs a shape none of these produces is prompted instead.

Generation happens before rendering, never in it. The strategy generates every secret that has no persisted value, meaning infra credentials under its binding keys and each env var in `generated_env_vars(config)`, made with `generate_value`, and records each through `ctx.answers` as `envvar.<KEY>` with `secret=True`. A run that stops before the `.env` reaches the machine therefore keeps what it generated, and once the machine's `.env` is fetched back, `adopt_env_file` makes it the source of truth, as it already is for every other secret. The renderer receives all of these in `answers` and only reads them. 2d wires this into `env create` and `update`.

A value is a secret when its env var declares `is_secret`, or when its template reads an infra `password`, an infra `url` whose provider's `url_template` carries the password, or `secret()`. `secret_keys(config)` derives that set with the same walk `validate_references` does. It exists because a `DATABASE_URL` built from `{{ infra.postgresql.url }}` holds the password whether or not the config marks it secret.

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

### Validation rules

Added to the structural rules from [2a](2a-schema-v2-and-upgrade.md), and enforced by `config validate`:

- Every `{{ ... }}` reference in `env_vars[].value` and `files[].content` must resolve against the reference grammar above (checked without a strategy).
- `secret()` may appear once in an env var's `value`, with nothing around it but literal text: never beside another reference, never twice in one value, never in `files[].content`. Its `format` must be `alnum`, `hex`, `base64` or `urlsafe`.
- An env var key declared by more than one service must have the same `value` in each. The monolithic strategy writes one `.env` that every service reads as `KEY=${KEY}`, so two services whose `DATABASE_URL` points at different instances would otherwise both get whichever was written last.

## Code changes by file

| File | Change |
|------|--------|
| `opsmith/core/render.py` | new: bindings, reference grammar, `secret()` and its formats, resolvers, secret classification and the secrets to generate |
| `opsmith/infra/providers.py` | new: the provider spec registry |
| `opsmith/core/config.py` | the `validate_references` hook, and the `secret()` and duplicate-key rules |
| `opsmith/deployment_strategies/monolithic.py` | the monolithic bindings, building a `RenderContext` for an environment |

## Acceptance criteria

1. `config validate` rejects a bad reference such as `{{ infra.nope.url }}` with the path to the offending env var, a `secret()` beside another reference, twice in one value or in a file, an unknown `format`, and one env var key declared by two services with different values (phase acceptance criterion 6).
2. `secret(32, format='urlsafe')` decodes as URL-safe base64 to 32 bytes, as a Fernet key must, and `"base64:{{ secret(32, format='base64') }}"` has the shape of a Laravel `APP_KEY` (the shapes in phase acceptance criterion 18; 2d proves the rest).

## Tests

- `test_render.py`: reference validation, `resolve_env_vars` precedence, `secret()` stability with persisted answers, the `secret()` placement, format and duplicate-key rules, the shape of each format's output, and derived secret keys.
- The provider registry: every provider has a spec, and each `secret_env` names the keys the v1 snippets use.

## Harness surface

None. Nothing a harness writes with references or `secret()` is honoured until 2d, which documents them.
