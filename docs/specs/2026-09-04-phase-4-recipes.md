# Phase 4: Recipes

**Goal:** deploy prebuilt open-source applications such as Odoo from public images, on any strategy, without a source repository and without any model-generated artifacts.
**Depends on:** phase 1 (service model v2), phase 2 (`files/` and customization of installed recipes), phase 3 (state without git).
**Size:** L.
**Ships as:** 0.8.0.

## Scope

1. The recipe format: a partial `deployments.yml` plus metadata and inputs.
2. Install, upgrade, remove, list, show, validate commands.
3. Catalog sources: bundled, project-local, git URL, plugin entry points.
4. App-only projects: no repo scan, git optional.
5. An initial bundled catalog with CI validation.

## Non-goals

- Backups and data disks (phase 8), although recipes mark volumes with `backup: true` now.
- Recipes that require building from source. Those are ordinary detected services.
- Templating at install time. Everything dynamic is a deploy-time reference (see below).

## Design

### Format

A recipe is a directory:

```
odoo/
  recipe.yml
  README.md          # shown by `recipe show`; setup steps a human should know
  files/             # optional static files referenced by FileMount.source
```

`recipe.yml`:

```yaml
recipe:
  name: odoo                       # [a-z0-9-]+, matches the directory
  version: "18.0"                  # recipe version, tracks the upstream release line
  description: Odoo ERP and CRM
  docs: https://hub.docker.com/_/odoo
  license: LGPL-3.0
  min_opsmith_version: "0.8.0"
  tags: [erp, crm]

inputs:
  - key: ODOO_MASTER_PASSWORD
    description: Master password for database management
    secret: true
    generate: true                 # generated per environment, never stored in config
  - key: ODOO_LANG
    description: Default language
    default: en_US
    choices: [en_US, fr_FR, de_DE]

infra_deps:
  - instance: odoo_db
    dependency_type: DATABASE
    provider: postgresql
    version: "16"
    settings: { username: odoo, database: postgres }

services:
  - name_slug: odoo
    source: { kind: image, image: odoo, tag: "18.0", platforms: [linux/amd64, linux/arm64] }
    service_type: BACKEND_API
    service_port: 8069
    routes:
      - { path_prefix: "/", port: 8069 }
      - { path_prefix: "/websocket", port: 8072 }
    env_vars:
      - { key: HOST, value: "{{ infra.odoo_db.host }}" }
      - { key: USER, value: "{{ infra.odoo_db.username }}" }
      - { key: PASSWORD, value: "{{ infra.odoo_db.password }}", is_secret: true }
    files:
      - path: /etc/odoo/odoo.conf
        content: |
          [options]
          admin_passwd = {{ inputs.ODOO_MASTER_PASSWORD }}
          proxy_mode = True
    volumes:
      - { name: odoo_web_data, path: /var/lib/odoo, backup: true }
      - { name: odoo_addons, path: /mnt/extra-addons }
    healthcheck: { http_path: /web/login, port: 8069, start_period_s: 90 }
    resources: { min_ram_gb: 2, recommended_ram_gb: 4, min_cpu: 1 }
    depends_on: [odoo_db]

post_deploy_message: |
  Open https://{{ domains.odoo }} and complete the database wizard.
  The master password is ODOO_MASTER_PASSWORD; run `opsmith env secrets --env <env>` to reveal it.

upgrade_notes:
  "18.0": "Run the Odoo upgrade wizard after the release completes."
```

`services` and `infra_deps` are exactly the phase 1 models. The only recipe-specific parts are `recipe`, `inputs`, `post_deploy_message` and `upgrade_notes`. Any strategy that can render the phase 1 model can deploy a recipe.

### No install-time templating

`{{ ... }}` references are resolved at deploy time by the phase 1 render context, which already has an `inputs` root. Recipe authors write literal values for anything static, including the image tag. Upgrades change literals through the provenance record, not through templates. This avoids a two-stage templating problem and keeps the installed config fully readable.

### Inputs

```python
class RecipeInput(BaseModel):
    key: str                        # [A-Z][A-Z0-9_]*
    description: str
    secret: bool = False
    generate: bool = False          # implies secret
    default: Optional[str] = None
    choices: Optional[List[str]] = None
    required: bool = True
```

- Non-secret inputs are prompted at `recipe add` with key `recipe.input.<KEY>` (flag `--input KEY=VALUE`) and stored in the config under the recipe's provenance record.
- Secret inputs are never stored in `deployments.yml`. `generate: true` values are created by the strategy's secret store per environment, the same mechanism phase 1 uses for infra passwords; secret inputs without `generate` are prompted per environment at `env create` with key `recipe.input.<KEY>` and persisted in the environment's env store.
- `inputs.<KEY>` binds to those values in the render context.

### Provenance and merge

Installing a recipe copies its services and infra dependencies into `deployments.yml` and records what it owns:

```yaml
recipes:
  - name: odoo
    version: "18.0"
    source: builtin                 # builtin | path:<relative> | git:<url>@<ref>
    inputs: { ODOO_LANG: en_US }     # non-secret only
    services: [odoo]
    infra_deps: [odoo_db]
```

Rules:

- Slug and instance collisions fail with `INVALID_ARGUMENT` and a `--slug-prefix` hint; with a prefix, every owned slug, instance, volume name and internal reference is rewritten consistently.
- After install, the services are ordinary config. Users may edit them. `recipe upgrade` computes a three-way diff between the installed recipe version, the new recipe version and the current config, applies non-conflicting changes, and lists conflicts for the user to resolve (interactive editor or `--keep-mine` / `--take-theirs` per field path in headless mode).
- `recipe remove` deletes owned services and infra instances after checking that no other service references them, and warns about named volumes that still hold data.

### Catalog

Resolution order for `recipe add <name>`: project-local `.opsmith/recipes/<name>/`, plugin entry points in group `opsmith.recipes` (each returns a directory path), then bundled `opsmith/recipes/<name>/`. `--from <path|git-url>` copies a recipe into `.opsmith/recipes/` first, so a project always carries the recipes it uses.

### App-only projects

- `opsmith init --app-name "My ERP"` creates `.opsmith/deployments.yml` in the current directory; no repository scan.
- `GitRepo` becomes optional in `OpsmithContext`. It is required only for build sources (build context) and for `ensure_gitignore` when a `.git` directory exists. When absent, `init` prints a one-time recommendation to run `git init`; safety comes from phase 3.
- `env create` with only image sources makes one model call on the happy path, the capacity estimate, which takes the recipe's declared `resources` and the environment's workload profile as input; rendering and validation are deterministic (phase 1). The model also explains a failed deployment.

### Commands

```
opsmith recipe list [--output json]
opsmith recipe show <name>                        # README + inputs + services summary
opsmith recipe validate <path>                    # schema, references, `docker compose config` on a dry render
opsmith recipe add <name> [--from PATH|URL] [--input K=V]... [--slug-prefix P] [--yes]
opsmith recipe upgrade <name> [--to VERSION] [--keep-mine PATH]... [--take-theirs PATH]... [--yes]
opsmith recipe remove <name> [--yes]
opsmith env secrets --env NAME [--reveal]          # lists secret keys, values only with --reveal
```

`env create`, `release`, `update`, `run` and `destroy` are unchanged; they see recipe services as normal services. `post_deploy_message` is rendered and shown after a successful `env create` and after `recipe upgrade` releases, and included in the JSON result.

### Initial catalog

Ship six, each with a README and a validation run in CI:

| Recipe | Infra | Notes |
|--------|-------|-------|
| odoo | postgresql | two routes, config file, master password input |
| wordpress | mysql | one container, upload volume |
| ghost | mysql | `url` env from `domains.ghost` |
| gitea | postgresql | ssh port not exposed in v1; document |
| n8n | postgresql | encryption key generated input |
| metabase | postgresql | slow start, long `start_period_s` |

Selection criteria for later additions: official or well-maintained image, multi-arch preferred, single-node friendly, no host networking.

### Authoring guide

`docs/recipes.md`: format reference generated from the pydantic models, the reference grammar table from phase 1, conventions (literal tags, `backup: true` on data volumes, `start_period_s` realistic, `resources` describing one instance at light load since the capacity plan scales them per environment, README must list the first-run steps), and the CI check.

## Code changes by file

| File | Change |
|------|--------|
| `opsmith/core/recipes.py` | `Recipe` model, loader, catalog resolution, install/upgrade/remove logic, three-way diff |
| `opsmith/types.py` | `recipes` provenance list on `DeploymentConfig`; `inputs` root in reference validation |
| `opsmith/core/render.py` | `inputs` binding from config plus environment secret store |
| `opsmith/deployment_strategies/monolithic.py` | secret store for generated inputs alongside infra passwords; `post_deploy_message` in results |
| `opsmith/cli/commands/recipe.py`, `init` in `setup.py` | commands above |
| `opsmith/recipes/<name>/` | bundled catalog |
| `opsmith/git_repo.py`, `opsmith/core/context.py` | optional git |
| `.github/workflows/` | recipe validation job: render every bundled recipe and run `docker compose config` |
| `README.md`, `docs/recipes.md` | user and author docs |

## Acceptance criteria

1. In an empty directory without git: `opsmith init --app-name erp && opsmith recipe add odoo && opsmith env create --name prod --provider AWS --region eu-west-1 --strategy Monolithic --domain odoo=erp.example.com --domain-email me@example.com` deploys Odoo without a repository scan, a registry or an image build, and the post-deploy message prints.
2. The Odoo websocket route works through Traefik and the master password is retrievable with `env secrets --reveal`.
3. `recipe add odoo` into a project that already has a `postgresql` instance keeps both instances separate.
4. `recipe upgrade odoo --to 19.0` on an edited config applies the tag change and reports the user's edited `resources` field as kept.
5. Every bundled recipe passes `recipe validate` in CI.
6. `recipe remove odoo` refuses when another service references `infra.odoo_db`.

## Tests

- `test_recipes.py`: loader validation, catalog precedence, slug prefix rewriting including references, collision errors.
- `test_recipe_upgrade.py`: three-way diff cases: untouched, user-edited non-conflicting, conflicting.
- `test_recipe_secrets.py`: secret inputs absent from saved config; generated once per environment.
- Golden compose renders for each bundled recipe.

## Risks and open questions

- Apps that need host-level features (Docker socket, host networking, privileged) are out of scope for v1 recipes; the validator rejects such fields.
- Recipes do not ship overlays or override files in v1; users customize an installed recipe through the phase 2 mechanisms like any other service.
- Upstream images change env var contracts between majors; `upgrade_notes` and the README are the mitigation, plus pinned tags.
- Odoo specifics: the official image reads `HOST`, `USER`, `PASSWORD` for the database and `admin_passwd` from its config file; `proxy_mode` is required behind Traefik. Verify on the smoke run.
