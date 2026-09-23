# Phase 3: Customization layer

**Goal:** every file opsmith uses to deploy can be customized inside the client repository, per project or per environment, without breaking upgrades, and the ownership of every path under `.opsmith/` is defined in code so people and coding agents know what they may edit.
**Depends on:** phase 0 (context, CLI contract), phase 2 (deterministic rendering, `files`).
**Size:** M.
**Ships as:** 1.0.0, in the same release as phase 2.

## Scope

1. Template overlays: replace any file under `opsmith/templates/**` with a copy in the project or in one environment.
2. Native extension points: compose override files, Terraform override and extra files, Ansible pre and post hooks, static files.
3. A template registry with descriptions and render variables, and commands to list, show, eject, diff, check, rebase, reset and adopt.
4. Upgrade safety: provenance for ejected files, drift detection, three-way rebase.
5. The ownership manifest for `.opsmith/` and the generated-directory policy.

## Non-goals

- Merging YAML or HCL ourselves. Where the tool has a native override mechanism, use it; otherwise the whole file is replaced.
- Customizing LLM prompts. Phase 6 moves them to files; overlaying them can reuse this mechanism later.

## Design

### The problem today

- `BaseInfrastructureProvisioner.copy_template` uses `shutil.copytree(dirs_exist_ok=True)`, so working directories are overwritten on every run. The exception is `container_registry`, which is copied only when its directory is empty and therefore never receives template fixes.
- Compose snippets, including `docker_compose_snippets/traefik.yml`, are loaded from the package by a Jinja `FileSystemLoader` and never reach the client repository. `traefik.yml` is rendered and passed to Ansible as an extra var.
- `AnsibleProvisioner.__init__` writes `ansible.cfg` inline on every run.
- The only customizable artifact is `.opsmith/docker/<slug>/Dockerfile`.

### Layout of `.opsmith/` after this phase

```
.opsmith/
  deployments.yml                         owned
  docker/<slug>/Dockerfile                owned, generated once
  templates/                              owned: overlays mirroring opsmith/templates/**
    .manifest.json                        owned: eject provenance
    .base/<path>                          owned: snapshot of the default at eject time
    docker_compose_snippets/traefik.yml
    virtual_machine/aws/main.tf
  overrides/                              owned: native extension points, all environments
    compose.override.yml
    terraform/<module>/*.tf, *.auto.tfvars
    ansible/<playbook>/pre.yml | post.yml
  files/                                  owned: static files for FileMount.source
  recipes/                                owned (phase 5)
  environments/<env>/
    templates/**                          owned: environment overlays, same mirror
    overrides/**                          owned: environment extension points
    files/**                              owned: environment static files
    state.yml                             state
    answers.yml                           state: persisted answers for re-runs
    <module>/                             generated, rebuilt on every run
      .opsmith-generated.json             generated: sources, origins, hashes
```

### Resolution order

For a template path such as `docker_compose_snippets/traefik.yml`:

1. `.opsmith/environments/<env>/templates/<path>`
2. `.opsmith/templates/<path>`
3. `opsmith/templates/<path>` inside the package

Provider-specific templates keep their `<template>/<provider>/<file>` shape, so the overlay path for the AWS VM module is `virtual_machine/aws/main.tf`.

### Template registry

`opsmith/templates/registry.py` declares every template the package ships:

```python
class TemplateSpec(BaseModel):
    path: str                       # "docker_compose_snippets/traefik.yml", "virtual_machine/aws/main.tf"
    kind: Literal["terraform", "ansible", "compose_snippet", "dockerfile", "ansible_cfg", "file"]
    description: str
    variables: Dict[str, str]       # render variables or extra-vars, each with a one-line description
    hooks: List[str] = []           # ["pre", "post"] for playbooks
    per_environment: bool           # whether its working dir is per environment or global
```

The registry is the single source for `template list` and `template show`, for validating `eject` paths, and for `references/templates.md` in the skill phase 1 shipped. A test asserts that every file under `opsmith/templates/` is registered and that every registered path exists.

### Resolver and materialization

`opsmith/core/templates.py`:

```python
class TemplateResolver:
    def __init__(self, package_root: Path, project_root: Path, environment: Optional[str])
    def resolve(self, path: str) -> ResolvedTemplate          # source path, origin: package | project | environment
    def jinja_env(self, subdir: str) -> jinja2.Environment    # ChoiceLoader in resolution order, StrictUndefined
    def materialize(self, template_dir: str, provider: str, working_dir: Path) -> MaterializeResult
```

`materialize` replaces `copy_template`. It writes each file from its resolved origin, then copies the matching extension-point files described below, then writes `.opsmith-generated.json` listing every produced file with its origin and sha256. Files present in the previous marker but no longer produced are deleted, so removed templates do not linger. Never touched: `terraform.tfstate*`, `.terraform/`, `.terraform.lock.hcl`, and `backend.tf` from phase 4.

Materialization runs before operations that apply templates: `env create`, `release`, `update`, `run`, and the image build step. `destroy` never materializes. It uses the working directory as last applied, which matches today's behaviour, so an environment created with older templates is destroyed with the configuration that created it and never fails on a variable the older resources did not have. When the working directory is missing, for example on a fresh machine after `opsmith pull`, `destroy` materializes once and warns that current templates are being used.

`ansible.cfg` becomes a template at `ansible/ansible.cfg` so it can be overlaid, for example to add SSH connection settings.

Compose snippets and `traefik.yml` render through `jinja_env("docker_compose_snippets")`. The rendered `traefik.yml` is materialized into the compose working dir and copied by the deploy playbook instead of travelling as an extra var.

### Native extension points

| Tool | Mechanism | Location | Applied how |
|------|-----------|----------|-------------|
| Compose | override files, merged by Compose itself | `overrides/compose.override.yml`, `environments/<env>/overrides/compose.override.yml` | rendered with the phase 2 render context, materialized as `compose.override.yml` and `compose.<env>.override.yml`, passed to `docker_compose_v2` in `files:` after `docker-compose.yml`, project before environment |
| Terraform | extra `.tf` files and `*_override.tf`, merged by Terraform; `*.auto.tfvars` loaded automatically | `overrides/terraform/<module>/`, `environments/<env>/overrides/terraform/<module>/` | copied into the module working dir before `init` |
| Ansible | `include_tasks` hook points | `overrides/ansible/<template>/pre.yml` and `post.yml`, environment equivalents | copied to `hooks/` in the working dir; every opsmith playbook includes `{{ opsmith_pre_tasks }}` at the start of its first play and `{{ opsmith_post_tasks }}` at the end of its last play when those vars are defined; project hooks run before environment hooks |
| Static files | `FileMount.source` | `files/`, `environments/<env>/files/` | resolved by the phase 2 renderer, environment first |

Rules for authors: override content may use `{{ }}` references from the phase 2 grammar; a `${KEY}` reference in a compose override must exist in the env file, so add the env var to the service in `deployments.yml` rather than inventing keys in overrides; new Terraform variables must have defaults or values in an `.auto.tfvars` file.

### Eject, provenance and drift

- `template eject <path>` copies the package default into the overlay, snapshots it under `templates/.base/<path>`, and records `{path, opsmith_version, base_sha256, ejected_at}` in `templates/.manifest.json`. With `--env`, the same happens under `environments/<env>/templates/` with its own manifest.
- On every command that materializes templates, and in `template check`, each overlay's recorded `base_sha256` is compared with the current package default. A mismatch is drift: a warning names the path, the version it was ejected from, and the two commands to run next, `template diff` and `template rebase`. `template check --strict` turns drift into `TEMPLATE_DRIFT` (exit 2).
- `template rebase <path>` performs a three-way merge of the base snapshot, the current default and the overlay with `git merge-file` when git is available, leaves conflict markers for the user, and updates the manifest and base snapshot once the file has no markers. Without git it prints the diff and instructions.
- `template reset <path>` removes the overlay, its base snapshot and its manifest entry.
- Extension-point files carry no provenance because they never replace opsmith files; they are validated, not diffed.

### Validation

`template check [--deep] [--strict] [--env NAME]`:

- renders every compose snippet overlay and every compose override with a sample render context and parses the result as YAML;
- parses playbook overlays and hooks as YAML;
- with `--deep`, runs `terraform validate` in a temporary materialization of each affected module and `ansible-playbook --syntax-check` on each affected playbook;
- reports drift as above;
- reports hand edits inside generated directories by comparing files with the marker hashes, with the `template adopt` hint.

Failures are `TEMPLATE_INVALID` (exit 2) with the path and message in `details`.

### Adopting existing hand edits

Projects created before this phase may carry edits inside generated directories. `template adopt --env NAME` compares each file in each working dir with the package default and, where they differ, moves the file into the environment overlay with provenance set to the current version, then re-materializes. Interactive runs confirm per file with `template.adopt.confirm`; headless runs answer it with `--answer template.adopt.confirm=true`.

### Generated-directory policy and git

Everything under `environments/<env>/<module>/` is reproducible from templates, overlays, config and state. `GitRepo.ensure_gitignore` upgrades its block to ignore `.opsmith/environments/*/*/` with negations for `state.yml`, `answers.yml` and for the `templates`, `overrides` and `files` directories, so only owned files and state are committed. Files already committed under generated paths are left alone; the upgrade prints the `git rm --cached` command once.

### Ownership manifest

`opsmith/core/ownership.py` declares path patterns with their category:

```python
class Ownership(BaseModel):
    pattern: str                                  # ".opsmith/environments/*/state.yml"
    category: Literal["owned", "generated", "state"]
    editable_by_agents: bool
    description: str
```

It is used by `opsmith paths`, by `template check` to detect edits under generated paths, and to generate `references/ownership.md`, which replaces the hand-written file phase 1 shipped.

### Template API stability

From this phase, template paths, their render variables and extra-vars, and the hook variable names are public. Renaming any of them requires accepting both names for one minor version plus a changelog entry, and `template check` warns when an overlay uses a deprecated name.

## Commands

```
opsmith template list   [--env NAME] [--output json]      # every template, origin, drift status
opsmith template show   <path> [--env NAME]               # resolved content, origin, variables, description
opsmith template eject  <path> [--env NAME] [--force]
opsmith template diff   <path> [--env NAME]
opsmith template check  [--deep] [--strict] [--env NAME]
opsmith template rebase <path> [--env NAME]
opsmith template reset  <path> [--env NAME] [--answer template.reset.confirm=true]
opsmith template adopt  --env NAME [--answer template.adopt.confirm=true]
opsmith paths [--output json]                             # ownership map of .opsmith/
```

## Worked example: customizing Traefik

```
opsmith template eject docker_compose_snippets/traefik.yml
# edit .opsmith/templates/docker_compose_snippets/traefik.yml
opsmith template check
opsmith update --env staging
```

For a staging-only change, such as pointing ACME at the Let's Encrypt staging CA, add `--env staging` to `eject`; the file lands under `.opsmith/environments/staging/templates/` and production keeps the default. To add a label or an extra service rather than change Traefik itself, write `overrides/compose.override.yml` instead of ejecting anything.

## Compatibility with existing environments

- Working directories under `environments/<env>/<module>/` are rebuilt on the next apply-type operation. State files and Terraform init artifacts are never touched, so existing Terraform state keeps matching its resources.
- Hand edits inside those directories are detected by `template check` and moved into overlays by `template adopt`; nothing is deleted before that step has been offered.
- `destroy` keeps using the last-applied directory, so environments created by older releases can always be torn down.
- Files already committed under generated paths stay in git until the user runs the printed `git rm --cached` command; nothing changes for them functionally.
- This phase ships in the same release as phase 2 so that the compose override file exists at the moment the first deterministic release overwrites hand-edited compose files.

## Harness surface

The ownership manifest and the template registry are what two of the skill's references were always
meant to be generated from. Per the
[definition of done](../notes/2026-09-04-migration-plan.md#definition-of-done-for-a-phase):

- `references/ownership.md` stops being hand-written and is generated from the manifest; the hand-written file phase 1 shipped is deleted in the same commit, and `opsmith paths` is what the skill points a harness at for the live answer.
- `references/templates.md` is new, generated from the registry: every template path, its variables, its hook points.
- `references/commands.md` regenerates for `template list|show|eject|diff|check|rebase|reset|adopt`.
- `SKILL.md` gains the customizing section it has been missing: prefer the `overrides/` extension points, eject a template only when a whole file must change, run `template check` afterwards, and put environment-scoped changes under `environments/<env>/`.
- `SKILL.md`'s never-list narrows in the same commit. Phase 1 shipped "never edit anything under `.opsmith/environments/<env>/` except by running Opsmith", which would forbid the `environments/<env>/templates/` and `environments/<env>/overrides/` edits the customizing section recommends. It becomes: never edit the generated `environments/<env>/<module>/` directories or `state.yml`. No test catches this contradiction, since every command and flag in it still resolves, so it is checked by reading.
- `README.md`'s ownership paragraph is rewritten to match, since it is hand-written and nothing fails when it goes stale: `.opsmith/templates/**`, `.opsmith/overrides/**` and the `templates/`, `overrides/` and `files/` directories under `environments/<env>/` become the user's, beside `deployments.yml` and the Dockerfiles.

## Code changes by file

| File | Change |
|------|--------|
| `opsmith/templates/registry.py` | new |
| `opsmith/core/templates.py` | resolver, materialize, manifest, drift, rebase |
| `opsmith/core/ownership.py` | ownership manifest |
| `opsmith/infra_provisioners/base_provisioner.py` | `copy_template` replaced by `materialize` through the resolver; marker file |
| `opsmith/infra_provisioners/ansible_provisioner.py` | `ansible.cfg` from template; hook extra-vars |
| `opsmith/infra_provisioners/terraform_provisioner.py` | copy Terraform override files before `init` |
| `opsmith/templates/**/main.yml` | pre and post hook includes in every playbook |
| `opsmith/templates/ansible/ansible.cfg` | new |
| `opsmith/templates/docker_compose_deploy/*/main.yml` | copy `traefik.yml` and override files; `files:` list for compose |
| `opsmith/deployment_strategies/monolithic.py` | Jinja environment from the resolver; override rendering; drift warnings at the start of deploy, release and update |
| `opsmith/deployment_strategies/base.py` | registry module materialized on every run |
| `opsmith/git_repo.py` | gitignore block upgrade |
| `opsmith/cli/commands/template.py`, `paths.py` | commands |
| `README.md`, `docs/customization.md` | user guide with the worked example and the extension point table |

## Acceptance criteria

1. Ejecting `docker_compose_snippets/traefik.yml`, editing it, and running `update --env dev` deploys the edited file, and a later `release` keeps it.
2. An environment overlay wins over a project overlay, which wins over the package default, and `template list --env dev` shows the origin of each file.
3. After upgrading opsmith to a version whose default Traefik template changed, the next `release` prints a drift warning, `template rebase` merges the change, and `template check --strict` exits 2 until it is resolved.
4. A `compose.override.yml` adding a label to a service is applied without changing the generated `docker-compose.yml`, and `compose validate` (phase 2) validates the merged result.
5. A `*_override.tf` changing the VM root volume size is applied on the next `env create`.
6. A `post.yml` hook for `virtual_machine_setup` runs after opsmith's own tasks.
7. Hand-editing a file in a generated directory is reported by `template check` with the adopt hint, and `template adopt` moves it into the overlay.
8. `container_registry` templates are re-materialized on every apply-type run like every other module.
9. `destroy` on an environment whose working directory was materialized from older templates does not rewrite the directory and completes with the older configuration.

## Tests

- `test_template_registry.py`: registry completeness in both directions.
- `test_template_resolver.py`: precedence, materialize idempotency, stale file removal, protected files untouched.
- `test_template_manifest.py`: eject, drift detection, rebase with a fake git, reset.
- `test_extension_points.py`: override file ordering, Terraform copy, hook extra-vars.
- `test_ownership.py`: classification of sample paths; gitignore block content.
- `test_materialize_policy.py`: destroy leaves an existing working directory untouched and materializes only when it is missing.
- Golden tests from phase 2 extended with an override file.

## Risks and open questions

- Overlaying a Jinja template ties the user to its variables; the stability rule and the `template check` warning are the mitigation.
- Three-way merges need git; the fallback is a diff plus instructions.
- Whether recipes may ship their own overlays or override files is left to phase 5; this layout reserves nothing for it.
