---
name: opsmith
description: Deploy applications to AWS or GCP with the opsmith CLI. Use when the user asks to deploy, release, set up infrastructure, create or destroy an environment, add a database, run a command on a deployed service, or edit anything under .opsmith/.
license: GPL-3.0-only
compatibility: Requires the opsmith CLI, docker, terraform >= 1.10 and cloud credentials
allowed-tools: Bash(opsmith:*)
metadata:
  version: "0.6.0"
---

# Opsmith

Opsmith takes a repository and deploys it to the user's own AWS or GCP account. It writes the
Dockerfiles, provisions the infrastructure with Terraform, configures the machines with Ansible,
and runs the containers behind Traefik with TLS.

You are driving it from a shell. Everything below assumes `--output json`, which is what makes it
machine-readable and what stops it ever prompting.

## The mental model

Three things, and they are worth keeping straight:

1. **The configuration** — `.opsmith/deployments.yml`. What this repository consists of: its
   services, their ports and environment variables, its infrastructure dependencies such as a
   database, and its environments. Written by a person or by you, committed to the repository.
2. **An environment** — a named deployment of that configuration, such as `dev` or `prod`, with a
   cloud provider, a region and a strategy. Its state lives in
   `.opsmith/environments/<env>/state.yml` and is Opsmith's alone.
3. **A strategy** — how an environment is actually built. `Monolithic` is the one that ships: one
   VM, one Docker Compose stack, one Traefik in front of it.

Opsmith needs a model configured for almost everything it does, because it analyses the repository
and generates Dockerfiles. Pass `--model` and `--api-key`, or set `OPSMITH_MODEL` and the
provider's own key variable. The `agent` commands are the exception and need neither.

## What you may edit

| Path | Whose | Rule |
|---|---|---|
| `.opsmith/deployments.yml` | yours | Edit freely. Validate after every edit. |
| `.opsmith/docker/<service>/Dockerfile` | yours | Edit freely. Validate after every edit. |
| `.opsmith/environments/<env>/<module>/` | Opsmith's | Generated working directories. Rebuilt on every run; edits are lost. |
| `.opsmith/environments/<env>/state.yml` | Opsmith's | Never edit. It is what says the environment exists. |
| Terraform state, `.terraform/` | Opsmith's | Never touch. |

The longer version, including where answers and secrets are kept, is in
[references/ownership.md](references/ownership.md).

## The golden workflow

Run each step, read the envelope, act on the exit code. Nothing has to be planned in advance.

```shell
# 1. Is the configuration usable?
opsmith --output json config validate

# 2. What will creating an environment ask for?
opsmith --output json env plan --name dev --provider AWS --strategy Monolithic

# 3. Create it. Add the answers the plan said were missing.
opsmith --output json env create --name dev --provider AWS --region us-east-1 \
  --strategy Monolithic --domain api=api.example.com --domain-email you@example.com

# 4. Build the current code and deploy it.
opsmith --output json release --env dev

# 5. What is it running?
opsmith --output json env status --env dev
```

Then, as the work continues:

- `opsmith --output json update --env dev` — reconcile a deployed environment with a changed
  configuration, after editing `deployments.yml`.
- `opsmith --output json run --env dev --service api -- python manage.py migrate` — run a one-off
  command on a deployed service. Read [the exception](#the-one-exception-opsmith-run) first.

### Every envelope looks like this

```json
{"ok": true,  "command": "env create", "result": {"...": "..."}, "warnings": []}
{"ok": false, "command": "env create", "error": {"code": "MISSING_ANSWER", "message": "...", "hint": "...", "details": {"key": "env.region", "choices": ["us-east-1"], "resume": "opsmith env create --name dev"}}}
```

Exactly one document on stdout. Progress is NDJSON on stderr — read it to show the user what is
happening, and never parse stdout and stderr together.

### The run-again loop

Every command is safe to run again. That is the whole contract:

- **Exit 3** — a question had no answer. `error.details.key` names it, `choices` lists what it
  will accept, and `resume` is the command to run again. Add `--answer <key>=<value>` and re-run.
- **Exit 8** — something outside Opsmith has to happen first, such as a DNS record being created.
  `error.details` says what. Do it, then run the same command again.

Answers are remembered per environment, so a re-run never asks the same question twice.

```shell
until opsmith --output json env create --name dev --provider AWS > result.json; do
  case $? in
    3) ;;   # read result.json, add one --answer, run again
    8) ;;   # do what it asks, run again
    *) break ;;
  esac
done
```

### Answering without being asked

Every question has a stable key, and every typed flag is shorthand for one: `--region us-east-1`
is `--answer env.region=us-east-1`. Answers come from, in order of precedence:

| Source | For |
|---|---|
| `--answer key=value` | one question, repeatable, beats everything |
| `--env-file <file>` | the `envvar.<KEY>` questions, secrets included |
| `--answers <file>` | a YAML mapping of key to answer |
| `OPSMITH_ANSWER_<KEY>` | one question, dots upper-cased to underscores |
| `--accept-defaults` | each question's own default, where it has one |

`opsmith env plan --write-answers answers.yml` writes the whole list as a file to fill in and hand
back with `--answers`. Anything still to be answered is written commented out, so a blank entry
cannot silently count as an empty answer.

**There is no flag that says yes to everything, by design.** Anything irreversible is approved by
naming it: `--answer delete.confirm=DELETE`, `--answer update.confirm_infra_changes=true`.
`--accept-defaults` refuses those keys outright.

## Writing deployments.yml yourself

This is the path to prefer when you are the one exploring the repository. `opsmith setup` runs
Opsmith's own detection instead, which costs a model run and tells you nothing you could not read
yourself.

```shell
opsmith init --app-name "My App"     # writes .opsmith/deployments.yml with no services
```

Then write the services into it. The full field list is in
[references/config-schema.md](references/config-schema.md); this is the shape:

```yaml
app_name: My App
app_name_slug: my-app
services:
  - name_slug: api
    language: Python
    language_version: "3.12"
    service_type: BACKEND_API
    framework: Django
    service_port: 8000
    env_vars:
      - key: DJANGO_SETTINGS_MODULE
        is_secret: false
        default_value: myapp.settings.production
      - key: SECRET_KEY
        is_secret: true
  - name_slug: web
    language: JavaScript
    language_version: "22"
    service_type: FRONTEND
    framework: React
    build_cmd: npm run build
    build_dir: dist
infra_deps:
  - dependency_type: DATABASE
    provider: postgresql
    version: "16"
environments: []
```

- `service_type` is one of `BACKEND_API`, `FRONTEND`, `FULL_STACK`, `BACKEND_WORKER`.
- A `FRONTEND` service needs `build_cmd` and `build_dir`, and is built on the machine running
  `release`, not in a container. Everything else is built from
  `.opsmith/docker/<name_slug>/Dockerfile`.
- `is_secret: true` means the value is never written into the repository. Opsmith asks for it, or
  generates it, and keeps it outside the tree.
- `environments` is written by `env create`. Do not hand-write it.

Validate, then write the Dockerfiles, then validate those:

```shell
opsmith --output json config validate
opsmith --output json dockerfile validate --service api
```

## Validate before you deploy

| Command | Checks | Needs |
|---|---|---|
| `opsmith config validate` | the configuration parses and its rules hold | nothing |
| `opsmith dockerfile validate` | every Dockerfile builds and the container starts | docker |
| `opsmith env plan` | what creating an environment will ask for | nothing |

`config validate` and `env plan` run on a machine with no docker, no terraform and no cloud
credentials, so there is no reason not to run them.

`dockerfile validate` with no `--service` checks every service that has a Dockerfile. It exits 4
when a Dockerfile is at fault, with the build and run tails in `error.details.checks`. When docker
failed for a reason that is *not* the Dockerfile's fault — a container that exits because the
database it wants does not exist yet — it exits 0 and says so in the notices. Do not try to fix a
Dockerfile in that case; there is nothing wrong with it.

## Never

- **Never edit** anything under `.opsmith/environments/<env>/` except by running Opsmith.
  Working directories are regenerated on every run and `state.yml` is Opsmith's record of what
  exists.
- **Never run `terraform` or `ansible` yourself** inside `.opsmith/`. Opsmith owns that state, and
  a partial apply is the one failure it cannot reason about.
- **Never commit a secret.** Secrets live outside the repository, under
  `~/.opsmith/projects/<name>-<digest>/`, and on the deployed machine. Nothing puts them in the
  tree, so nothing should move them there.
- **Never destroy an environment the user has not named.** `opsmith destroy --env <name>` with
  `--answer delete.confirm=DELETE` deletes real infrastructure and the data on it. Run it only
  when the user has asked for that environment by name in this conversation, and never to clean up
  after yourself.
- **Never use the interactive commands.** `opsmith setup` without flags and `opsmith deploy` are
  menus for a person. Use the subcommands.

## Troubleshooting by exit code

| Exit | Means | Do |
|---|---|---|
| 0 | success | — |
| 1 | unexpected failure | Report it. Re-run with `--verbose` for a traceback. |
| 2 | usage or configuration error | Read `error.hint`; it names the command or flag to fix. |
| 3 | an answer is missing | Add `--answer <key>=<value>` from `error.details` and run again. |
| 4 | an external tool failed: docker, terraform or ansible | Read the tails in `error.details`. For a Dockerfile, fix it and validate again. |
| 5 | cloud credentials or permissions | The user has to fix their credentials. Say which account and region. |
| 6 | the model could not produce a usable result | Retry once; then hand it to the user with the explanation. |
| 7 | state conflict | Another run holds the lock, or the remote state is newer. |
| 8 | something outside Opsmith must happen first | Do what `error.details` says, then run again. |

Every failure carries a `hint` naming a runnable next command. Read it before deciding anything.
The full list of error codes is in [references/commands.md](references/commands.md).

## The one exception: opsmith run

`opsmith run` exits with the status of the command it ran on the machine, **after** writing a
successful envelope. So:

- `ok: true` with a non-zero exit means your migration failed, not Opsmith.
- Read `ok` before reading the exit code, for this command only.
- What ran, where, and its output are in `result.command`, `result.target`, `result.stdout_tail`
  and `result.stderr_tail`, already decoded.

```shell
opsmith --output json run --env dev --service api -- python manage.py migrate
```

The command goes after `--`, so its own flags are not read as Opsmith's.

<!-- opsmith:future-start -->
## Not in this release

Do not reach for these; they do not exist yet, and a command that does not exist is an exit 2:

- There is no `schema_version` in `deployments.yml`. It arrives with the next release, which also
  adds image-based services, routes, volumes and mounted files.
- There is no `compose render`, `compose diff` or `compose validate`. The compose file is
  generated by the model at deploy time in this release.
- There is no template or override mechanism, and no `opsmith template ...`.
- There are no recipes for deploying packaged applications such as Odoo.
- There is no MCP server. Drive the CLI through your shell.
<!-- opsmith:future-end -->

## References

- [references/commands.md](references/commands.md) — every command, flag, exit code, error code
  and result shape. Generated from the code, so it is never out of date.
- [references/config-schema.md](references/config-schema.md) — every field of
  `deployments.yml`. Also generated.
- [references/workflows.md](references/workflows.md) — worked examples: a new project, a release,
  a configuration change, a failure.
- [references/ownership.md](references/ownership.md) — what is yours, what is Opsmith's, and what
  never enters the repository.
