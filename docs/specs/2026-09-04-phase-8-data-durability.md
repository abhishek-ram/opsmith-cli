# Phase 8: Data durability

**Goal:** application data in a monolithic environment survives VM replacement, upgrades and mistakes, and can be restored.
**Depends on:** phase 2 (`volumes[].backup`), phase 4 (the bucket).
**Size:** M.
**Ships as:** 1.4.0.

Recipes make this necessary: an Odoo or Nextcloud environment holds real data, while today everything sits on the VM root disk with delete-on-termination and `destroy` removes it all.

## Scope

1. A separate data disk per monolithic environment, kept on destroy by default.
2. Docker named volumes placed on that disk.
3. Snapshot before destructive operations.
4. Backups of marked volumes and infra databases to the state bucket, on demand and on a schedule, with restore.

## Non-goals

- Point-in-time recovery for databases.
- Cross-region replication.

## Design

### Data disk

New Terraform module `templates/data_disk/<provider>/` creating an EBS gp3 volume or a GCP persistent disk with `prevent_destroy = true`, tagged like other resources, sized from a new environment setting `data_disk_gb` (default: the storage total of the environment's capacity plan rounded up to 10 GB, with a 30 GB minimum; prompt key `env.data_disk_gb`, flag `--data-disk-gb`). The VM module attaches it; the setup playbook formats it on first attach only, mounts it at `/data`, and configures Docker's `data-root` or bind-mounts named volumes under `/data/volumes/<name>` so every named volume from phase 2 lands on the disk.

`destroy` detaches and keeps the disk unless `--delete-data` is passed, and prints the disk id in the result. `env create` with `--attach-data-disk <id>` reuses an existing disk, which is also how a VM is replaced.

### Snapshots

Before `destroy`, before `update` when infra instances change, and before `recipe upgrade` releases, take a disk snapshot through the provider SDK and record its id in `state.yml`. `env snapshots --env NAME` lists them; retention keeps the last five by default.

### Migrating an existing environment onto a data disk

Environments created before this phase keep their volumes on the root disk and keep working. `opsmith data-disk migrate --env NAME [--size-gb N] [--answer data_disk.migrate.confirm=true]` moves them:

1. Snapshot the root volume through the provider SDK and record the snapshot id in `state.yml`.
2. Create and attach the data disk through the new Terraform module, format it once, mount it at `/data`.
3. Stop the compose stack and copy every named volume of the project from Docker's volume directory to `/data/volumes/<name>`, preserving ownership and modes.
4. Re-render with bind mounts under `/data/volumes`, start the stack, and run the phase 2 health validation.
5. On failure, restart the stack with the original mounts, leave the copied data in place for inspection, and report `DEPLOY_UNHEALTHY` naming the step that failed.

Backups need no migration: they read volumes and dump databases wherever the volumes live, so an environment can start taking backups before it moves to a data disk.

### Backups

```
opsmith backup create  --env NAME [--label L]
opsmith backup list    --env NAME
opsmith backup restore --env NAME --id ID [--answer backup.restore.confirm=true]
opsmith backup schedule --env NAME --cron "0 3 * * *" [--retain 14]
```

- A backup runs on the VM through a playbook: for each service volume with `backup: true`, a tar stream of the volume; for each infra instance, a logical dump using the provider's tool inside its container (`pg_dump`, `mysqldump`, `mongodump`, redis `SAVE` plus the dump file); uploaded to `<app_slug>/backups/<env>/<timestamp>/` in the state bucket using the VM's instance role, so no user credentials are copied to the VM.
- Schedules install a cron entry on the VM running the same script; retention prunes old prefixes.
- Restore stops the stack, restores volumes and databases, starts it, and runs the deterministic health validation from phase 2.

### Provider specs

`InfraProviderSpec` from phase 2 gains `dump_command` and `restore_command` templates; providers without a sensible logical dump (kafka, elasticsearch, weaviate) rely on volume tars and say so in the docs.

## Compatibility with existing environments

- Nothing changes for an existing environment until `data-disk migrate` is run; backups and schedules work on it immediately.
- `destroy` keeps its current behaviour for environments without a data disk.

## Harness surface

Per the [definition of done](../notes/2026-09-04-migration-plan.md#definition-of-done-for-a-phase):

- `references/commands.md` regenerates for the backup commands and the schedule flags.
- `references/config-schema.md` regenerates for `backup: true` on a volume.
- `SKILL.md`: the never-list and the destroy workflow say what a data disk keeps when an environment is destroyed, and what the restore path is. A harness that tells a user their data is gone when it is on a retained disk is worse than one that says nothing.

## Code changes by file

| File | Change |
|------|--------|
| `opsmith/templates/data_disk/*` | new Terraform module |
| `opsmith/templates/virtual_machine/*`, `virtual_machine_setup/*` | attach, format-once, mount, docker volume placement |
| `opsmith/templates/backup/*` | playbooks for backup, restore, schedule |
| `opsmith/types.py` | `data_disk` and `snapshots` in `MonolithicDeploymentState`; `data_disk_gb` on the environment |
| `opsmith/deployment_strategies/monolithic.py` | snapshot hooks, keep-disk destroy, backup and restore operations |
| `opsmith/infra/providers.py` | dump and restore commands |
| `opsmith/cli/commands/backup.py` | commands |
| `README.md`, `docs/recipes.md` | durability guarantees per recipe |

## Acceptance criteria

1. `destroy` followed by `env create --attach-data-disk <id>` brings an Odoo environment back with its data.
2. `backup create` then `backup restore` on a fresh environment reproduces the database and uploaded files.
3. A scheduled backup runs on the VM and prunes beyond retention.
4. A failed restore leaves the previous state intact and reports `DEPLOY_UNHEALTHY` with logs.
5. `data-disk migrate` on an environment created before this phase moves the volumes, the application sees its data afterwards, and a failed copy rolls back to the root-disk mounts.

## Tests

- `test_data_disk.py`: Terraform variables, keep versus delete paths with fake provisioners.
- `test_backup.py`: manifest of what gets backed up from a config; command generation per provider; retention pruning logic.
- `test_data_disk_migrate.py`: step ordering with fake provisioners; rollback path; snapshot recorded before any change.

## Risks and open questions

- Formatting the disk only on first attach must be robust; use a filesystem label check in the playbook and never format a labelled disk.
- Docker `data-root` on the data disk versus bind-mounted volumes: bind mounts keep images on the root disk and data on the data disk, which is the intended split; choose bind mounts.
- Backup size and bucket cost are the user's; the docs must state that nothing is deleted without retention rules the user set.
