# Phase 3: Remote state and config sync

**Goal:** Terraform state, the deployment config and environment state survive the loss of the user's machine, and concurrent runs are locked, whether or not the project is in git.
**Depends on:** phase 0; phase 2 recommended first, because its materialization step is what regenerates working directories after a pull.
**Size:** M.
**Ships as:** 0.7.0.

## Scope

1. A per-app state bucket in the user's cloud account, created by opsmith.
2. Terraform remote backends for every module opsmith runs.
3. A copy of `deployments.yml` and each `environments/<env>/state.yml` in the bucket, synced on save.
4. `opsmith pull` to restore a project on a fresh machine.
5. `opsmith state migrate` for existing local-state projects.
6. A local-state escape hatch.

## Non-goals

- Backups of application data (phase 8 uses the same bucket).
- Multi-user workflows beyond locking.

## Design

### Why git was never the safety net

`GitRepo.ensure_gitignore` adds `**/*.tfstate` to `.gitignore`, so the state has always lived only on the machine that ran the deploy. Losing it orphans cloud resources. This phase fixes that independently of git; git remains recommended for versioning the config.

### State backend abstraction

`opsmith/core/state_backend.py`:

```python
class StateBackend(Protocol):
    name: Literal["local", "cloud"]
    def ensure(self) -> None                                   # create bucket if missing, idempotent
    def terraform_backend_hcl(self, module_key: str) -> str     # backend block for a module
    def put(self, key: str, data: bytes) -> None
    def get(self, key: str) -> Optional[bytes]
    def head(self, key: str) -> Optional[ObjectInfo]           # etag, last_modified
    def delete_prefix(self, prefix: str) -> None

class LocalStateBackend: no-ops; terraform_backend_hcl returns ""      # behaviour today
class S3StateBackend(bucket, region, account_id)
class GCSStateBackend(bucket, location, project_id)
```

Each cloud provider class returns its backend: `BaseCloudProvider.state_backend(app_slug) -> StateBackend`.

### Bucket naming

Deterministic from app slug and account, so a fresh machine can find it:

```
name = f"opsmith-{app_slug}-{account_ref}"           # account_id for AWS, project_id for GCP
if len(name) > 63:
    name = f"opsmith-{app_slug[:24]}-{sha1(app_slug + account_ref)[:10]}"
```

Lowercase, hyphens only. One bucket per (provider, account) per app; environments in the same account share it. Recorded in `DeploymentConfig`:

```yaml
state_backends:
  "AWS:123456789012": { bucket: opsmith-myapp-123456789012, region: us-east-1 }
  "GCP:my-project":   { bucket: opsmith-myapp-my-project, location: us-central1 }
```

### Bucket creation

Through the SDKs already in the dependency list. Idempotent: an existing bucket owned by the account is accepted; a name owned by someone else raises `CLOUD_PERMISSION` with a hint to set `state_backends` manually.

- **S3:** `create_bucket` with `LocationConstraint` outside `us-east-1`, versioning enabled, default SSE-S3 encryption, public access block on all four settings, tags `ManagedBy=Opsmith`, `App=<slug>`.
- **GCS:** `create_bucket(location=region)`, versioning enabled, uniform bucket-level access, public access prevention enforced, labels as above.

### Terraform backends

`TerraformProvisioner.init_and_apply` and `destroy` take a `backend_hcl` argument and write it to `backend.tf` in the working directory before `init`. `backend.tf` is exempt from the phase 2 stale-file cleanup. Module keys:

```
<app_slug>/tfstate/<env>/virtual_machine/terraform.tfstate
<app_slug>/tfstate/<env>/frontend_bucket_cert/<service_slug>/terraform.tfstate
<app_slug>/tfstate/<env>/frontend_cdn/<service_slug>/terraform.tfstate
<app_slug>/tfstate/global/<provider>-<region>/container_registry/terraform.tfstate
```

Backend blocks:

```hcl
terraform {
  backend "s3" {
    bucket       = "<bucket>"
    key          = "<module_key>"
    region       = "<bucket region>"
    encrypt      = true
    use_lockfile = true
  }
}
```

```hcl
terraform {
  backend "gcs" {
    bucket = "<bucket>"
    prefix = "<module_key without the trailing filename>"
  }
}
```

`use_lockfile` needs Terraform 1.10 or newer; the per-command requirement check from phase 0 enforces `terraform >= 1.10` when the cloud backend is in use and fails early with a hint otherwise. No DynamoDB table.

### Config and state sync

Object layout in the bucket:

```
<app_slug>/config/deployments.yml
<app_slug>/config/environments/<env>/state.yml
<app_slug>/config/environments/<env>/answers.yml
<app_slug>/tfstate/...
```

- `DeploymentConfig.save`, `MonolithicDeploymentState.save` and the answer store upload after the local write when the environment's backend is cloud. Upload failures are warnings, not errors, and are retried on the next save.
- `destroy` removes `<app_slug>/config/environments/<env>/` and `<app_slug>/tfstate/<env>/` after a successful Terraform destroy. The bucket itself is never deleted by opsmith; `destroy --delete-state-bucket` is offered only when no environments remain.
- Conflict detection: `.opsmith/.sync.json` records the etag of each object last written or pulled. On load, if the remote etag differs from the recorded one and the content differs from local, emit a warning with `opsmith pull` as the hint; in non-interactive mode fail with `STATE_CONFLICT` (exit 7) unless `--force-local`.

### `opsmith pull`

```
opsmith pull --provider AWS --app-slug myapp [--region R] [--project-id P]
```

Detects the account with the provider's `detect_account`, derives the bucket name, downloads `config/**` into `.opsmith/`, and writes `.sync.json`. Terraform and Ansible working directories are recreated on the next command by the phase 2 materialization step, and `terraform init` reattaches the state from the backend. Nothing else lives only on disk after this phase.

### `opsmith state migrate`

```
opsmith state migrate [--env NAME | --all] [--yes]
```

For each Terraform directory under `.opsmith/environments/`: ensure the bucket, write `backend.tf`, run `terraform init -migrate-state -force-copy`, verify `terraform state list` is non-empty, then upload config and state files. Existing local `terraform.tfstate` files are left in place (they stay gitignored) and a note points at them.

### Local escape hatch

`env create --state local` records `state_backends` as absent for that account and emits a warning on every deploy, release and update. Default is cloud for environments created after this phase; existing environments stay local until migrated.

### Locking

The backends lock natively. When Terraform reports a held lock, the provisioner maps it to `STATE_LOCKED` (exit 7) with the lock id and the `terraform force-unlock` hint in `details`. This also protects an agent and a person from applying the same environment at once.

### Permissions

Document the minimum IAM actions in `docs/permissions.md`: S3 `CreateBucket`, `PutBucketVersioning`, `PutEncryptionConfiguration`, `PutBucketPublicAccessBlock`, `GetObject`, `PutObject`, `DeleteObject`, `ListBucket`; GCS `storage.buckets.create`, `storage.objects.*`.

## Compatibility with existing environments

- Environments without an entry in `state_backends` keep local state. Every deploy, release and update warns once per run with the `state migrate` hint, and nothing else changes for them.
- `state migrate` is opt-in per project and can be run one environment at a time. Until it runs, the Terraform version requirement stays at today's minimum.
- The registry module under `environments/global/` migrates together with the first environment in its region.

## Code changes by file

| File | Change |
|------|--------|
| `opsmith/core/state_backend.py` | new |
| `opsmith/cloud_providers/base.py`, `aws.py`, `gcp.py` | `state_backend()`; bucket creation via boto3 and google-cloud-storage |
| `opsmith/infra_provisioners/terraform_provisioner.py` | `backend_hcl` handling, `-migrate-state`, lock error mapping, version check |
| `opsmith/types.py` | `state_backends` on `DeploymentConfig` |
| `opsmith/core/config.py` | upload on save, conflict detection, `.sync.json` |
| `opsmith/deployment_strategies/base.py`, `monolithic.py` | pass backend blocks for each module; cleanup on destroy |
| `opsmith/cli/commands/state.py` | `pull`, `state migrate`, `state show` |
| `README.md`, `docs/permissions.md` | where state lives, recovery steps, IAM |

## Acceptance criteria

1. `env create` on a new project creates the bucket, and every Terraform module has its state in the bucket and nothing in local `terraform.tfstate` files.
2. Deleting the local `.opsmith/` directory and running `opsmith pull` followed by `opsmith release --env dev` succeeds.
3. Two concurrent `release` runs: the second fails with `STATE_LOCKED` and does not touch infrastructure.
4. `state migrate` on a pre-phase-3 project moves all modules and a subsequent `release` works.
5. With Terraform older than 1.10, `env create` fails before creating anything, with the version hint.

## Tests

- `test_state_backend.py`: naming rules including truncation; HCL generation; idempotent ensure with mocked SDK errors.
- `test_config_sync.py`: upload on save, conflict detection paths, `--force-local`.
- `test_terraform_backend.py`: `backend.tf` written before init; migrate flags; lock error mapping.
- `test_pull.py`: bucket derivation and file restoration with a fake backend.

## Risks and open questions

- Bucket name collisions across accounts are possible only with the truncated form; the hash makes them unlikely and the manual override exists.
- GCS bucket location is fixed at creation; environments in other regions still use it. Acceptable for state-sized objects.
- Users with restrictive IAM will hit `CLOUD_PERMISSION` during bucket creation; the hint must show the exact actions and the `--state local` fallback.
