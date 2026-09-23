<!-- Hand-written. Worked examples, in the order they actually happen. -->

# Workflows

Every command here takes `--output json`, which is omitted only to keep the lines short. Read the
envelope after each one.

## A repository Opsmith has never seen

You explore the repository; Opsmith validates and deploys. This is the path to prefer when you are
already reading the code.

```shell
opsmith init --app-name "My App"
```

That writes `.opsmith/deployments.yml` with no services. Fill it in from what you find: one entry
per deployable service, plus `infra_deps` for the databases and caches the code expects. The
fields are in [config-schema.md](config-schema.md) and an example is in the skill itself.

```shell
opsmith config validate
```

Fix whatever it names — `error.details.errors` gives a dotted path per problem — until it exits 0.

Now the Dockerfiles. Write one per service whose `service_type` is not `FRONTEND`, at
`.opsmith/docker/<name_slug>/Dockerfile`. Then:

```shell
opsmith dockerfile validate
```

Exit 4 means a Dockerfile is at fault: `error.details.checks[].build_tail` and `run_tail` say why.
Fix and run it again. Exit 0 with a notice about the container exiting means the image is fine and
the service simply has nothing to talk to yet — leave it alone.

The alternative, when you have not been asked to explore: `opsmith setup --accept-detected` runs
Opsmith's own detection and writes both the configuration and the Dockerfiles. It costs a model
run over the whole repository.

## The first environment

```shell
opsmith env plan --name dev --provider AWS --strategy Monolithic
```

`result.answers_needed` is what creating it will stop for, each with its key, the choices it will
accept and its default. `result.answers_known` is what it already has. `result.complete` says
whether the list is the whole list — it is not, until the provider and strategy are known, because
which questions exist depends on them.

Then create it, passing what the plan asked for:

```shell
opsmith env create --name dev --provider AWS --region us-east-1 --strategy Monolithic \
  --domain api=api.example.com --domain-email you@example.com
```

This provisions real infrastructure and takes minutes. Progress streams on stderr. It will stop at
least once:

- **Exit 3** — an answer is missing. `error.details.key` names it. Add `--answer <key>=<value>`
  and run the whole command again; everything already answered is remembered.
- **Exit 8** — a DNS record has to exist before TLS can be issued. `error.details` gives the exact
  record. The user creates it; then run the same command again.

`result.urls` is where each service ended up. `result.resources` is what was created.

## Releasing new code

```shell
opsmith release --env dev
```

Builds the current working tree into images, pushes them, and restarts the stack. A `FRONTEND`
service is built locally first, so the machine running this needs that toolchain installed.

## After changing deployments.yml

```shell
opsmith config validate
opsmith update --env dev
```

`update` reconciles a deployed environment with the configuration: new services, changed
environment variables, new dependencies. When the change touches infrastructure it stops for
`--answer update.confirm_infra_changes=true`, because that is a change to what exists in the cloud
rather than to what runs on it.

## Running something on the deployed service

```shell
opsmith run --env dev --service api -- python manage.py migrate
opsmith run --env dev --service api -- python manage.py createsuperuser --noinput
```

The exit code is the remote command's own. `ok: true` with a non-zero exit means the command
failed, not Opsmith. `result.stdout_tail` and `result.stderr_tail` carry the output.

## When something is wrong

```shell
opsmith env status --env dev
```

Reads `state.yml` and reports the resources and the deployed services. It contacts no cloud and
needs no credentials, so it works when everything else is failing.

For a deployment that came up unhealthy, the error carries the container state and the log tails
in `error.details`, and the model's reading of them in the explanation. Fix the cause — usually a
missing environment variable or a service that cannot reach its database — and run `release`
again.

## Tearing it down

Only when the user has asked for that environment by name:

```shell
opsmith destroy --env dev --answer delete.confirm=DELETE
```

This deletes the infrastructure and the data on it. There is no undo, and no flag that approves it
in advance.
