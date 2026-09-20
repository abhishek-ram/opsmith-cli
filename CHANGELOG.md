# Changelog

All notable changes to Opsmith are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0] - 2026-09-20

The headless core, phase 0 of the [migration to 1.0](docs/notes/2026-09-04-migration-plan.md),
complete: parts 0a to 0g.

### Added

- `opsmith env plan` reports every answer creating an environment will need, before anything is
  created. For each one it gives the key, the question, the options where it can list them, the
  default where there is one, and the environment variable a secret should arrive in - and says
  which of them this run would actually stop for, which depends on whether `--accept-defaults`
  is in use. It writes nothing, creates nothing, and needs neither docker nor terraform.
  It does read from your cloud account where it can, to list the regions the account really has,
  but a machine that has no credentials yet still gets the plan and is told which parts of it
  went unlisted.
- Which questions exist depends on the cloud provider and the deployment strategy, so a plan
  that has not been told those two reports what it can, names them as the answers to give first,
  and says `complete: false` rather than passing a short list off as the whole one. The same
  holds for a provider or strategy that declares no questions: `partial_reasons` names it.
- `opsmith env plan --write-answers FILE` writes that list as a YAML file to fill in and hand
  back with `--answers`. Anything with a default is written with it; anything still to be
  answered is written commented out, because a key present with an empty value counts as an
  answer of the empty string and would quietly satisfy the question it was meant to leave open.
  A secret is always commented out - those belong in a `--env-file`.
- Cloud providers declare their questions as data. `AWS` declares the region, `GCP` the project,
  the region and the zone, each saying which earlier answer its listing depends on. A question
  declared this way is asked through exactly the same interaction as one asked inline, so a
  flag, a file or an environment variable answers it identically; what is new is only that it
  can be reported before it is reached.
- Naming a cloud provider or a deployment strategy that is not installed is now
  `INVALID_ARGUMENT` and exit 2, with the names that would have worked in the details. It used
  to be an unhandled `ValueError` reported as an internal failure.

- Everything the `setup` and `deploy` menus can do is now a subcommand that takes flags and never
  prompts: `opsmith init`, `env list`, `env create`, `env status`, `release`, `update`, `run` and
  `destroy`. The menus remain, and dispatch to exactly the same functions, so the two cannot
  drift. See "Running Opsmith without a terminal" in the README.
- Every flag is shorthand for an answer, on the keys the questions already had: `--region
  us-east-1` is `--answer env.region=us-east-1`, and `--domain api=api.example.com` is `--answer
  env.domain.api=api.example.com`. Also `--name`, `--provider`, `--strategy`, `--project-id`,
  `--zone`, `--instance-type`, `--domain-email`, `--env-var KEY=VALUE`, `--build-env
  slug:KEY=VALUE`, `--app-name` and `--rescan`.
- Every command returns a typed result, which is what `--output json` now puts in the envelope's
  `result` field: the infrastructure, the registry, the urls per service and the DNS records a
  creation asked for; the images and health verdict of a release; what a destroy tore down. Each
  also carries `notices` and `next_steps`, so a driver reading only stdout gets what a person
  watching the terminal would have seen.
- Infrastructure is reported as a list of `Resource` — a `kind`, an `id`, and whatever of `name`,
  `region`, `address`, `size` and `details` applies — rather than as fields shaped for a single
  virtual machine. A strategy that raises several machines reports several of them; one that
  deploys to a cluster or a serverless platform reports what it actually made. `destroy` reports
  the same shape it created, in place of the strings it used to name things with.
- `opsmith run` reports which host the command ran on, in the result's `target`.
- `opsmith run` exits with the exit code of the command it ran, and reports the tail of its
  stdout and stderr in the result. A non-zero exit is no longer reported as a failed playbook.
- `opsmith env list` and `opsmith env status` read the repository and nothing else. They need no
  cloud account, no docker and no terraform, and run with an empty `PATH`.
- `opsmith init --app-name "My App"` writes the deployment configuration without scanning the
  repository, for a harness that intends to author the services itself and have Opsmith validate
  them.
- `opsmith env create --no-deploy` writes the environment to the configuration without creating
  anything. Running `env create` again for an environment that has not been deployed picks it up
  rather than refusing the name, so a creation that stopped part way can be finished.
- Headless runs. A run with no terminal never blocks on a question: it resolves the answer, or it
  stops with the key that is missing and the command to run again. Answers come from `--answer
  key=value`, `--env-file`, `--answers`, `OPSMITH_ANSWER_<KEY>`, and what the environment has
  already been asked, in that order; `--accept-defaults` takes each question's own default.
  A run is headless when `--non-interactive` or `OPSMITH_NON_INTERACTIVE` says so, when stdin is
  not a terminal, or when `--output json` is in use.
- Two new stops, both resumable by running the same command again: `MISSING_ANSWER` (exit 3),
  which names the key, the choices and the variable that would carry a secret; and
  `PENDING_ACTION` (exit 8), which names something outside Opsmith that has to happen first. Both
  carry the command to resume with, which is the invocation that stopped, minus its credentials.
- An answer store per environment. Every answer is written the moment it is given, in both
  interactive and headless runs, so a run that stops never asks for the same thing twice.
- Opsmith now keeps what it remembers outside your repository, under
  `~/.opsmith/projects/<name>-<digest>/environments/<env>/`: the answers an environment has given,
  the secrets it needs until the environment itself holds them, and the steps a run has finished.
  None of it is authored and none of it belongs in a diff; keeping the secrets out of the tree is
  also the only promise that holds, since an ignore rule says nothing about `git add -f`, an
  archive of the directory, or a build context that never read it. Set `state_dir:` in
  `.opsmith.conf.yml` to put it somewhere else.
- `opsmith setup --accept-detected` takes the detected services and dependencies as they are,
  instead of opening an editor to review each one.
- `ctx.steps.once("vm.create")`, a ledger for a step that cannot simply be run again, kept in
  `.opsmith/environments/<env>/steps.yml`. It is a convenience for strategy authors; a strategy
  whose steps are idempotent needs nothing.
- `opsmith config validate|schema|show`: check, describe and print the deployment configuration
  without a cloud account, docker or terraform. `config schema --format markdown` renders the
  schema as a document.
- `--output json`: exactly one JSON envelope on stdout, with progress streamed to stderr as NDJSON.
- The model may be configured without typing it: `--model` falls back to `OPSMITH_MODEL` and then
  to `model:` in `.opsmith.conf.yml`, and `--api-key` falls back to the provider's own key
  variable (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`).
- Stable error codes and exit codes: 2 for a usage or validation error, 4 for a failing external
  tool, 5 for cloud credentials, 6 for a model that gave up. Every failure carries a code, a
  message, a hint and machine-readable details.
- Tracing is an extra: `pip install "opsmith-cli[logfire]"` to use `--logfire-token`.

### Changed

- **Breaking, for third-party cloud providers.** `get_account_details` is replaced by three
  classmethods on `BaseCloudProvider`, because it was doing three things that happen at three
  different times: `detect_account` reaches the account and reads the facts that are not a
  matter of choice, and must not ask anything; `questions` declares what needs choosing, as
  data; and `build_detail` turns both into the record the environment keeps. Only the first and
  last are required - a provider that declares no questions and asks inside `build_detail`
  through `ctx.interact` works exactly as before, and gives up nothing but its coverage in
  `env plan`. There are no known third-party providers; this is acceptable before 1.0.
- **Breaking, for GCP environments created headlessly.** The zone used to be taken silently as
  the first of the region's zones, which is why `--zone` existed and did nothing. It is a real
  question now: at a terminal the first zone is preselected, so pressing enter gives the old
  answer, and a headless run supplies `--zone` or takes the same first zone with
  `--accept-defaults`. A headless GCP run that did neither now stops with `MISSING_ANSWER`
  naming `env.zone`, which `opsmith env plan` will have told it about beforehand.
- Splitting the providers up narrows their error handling as a side effect worth having: each
  call to a cloud SDK is guarded on its own rather than a whole flow at once, so a failure is
  reported against the thing that actually failed - reaching the account, or listing the regions
  - instead of everything in the block being reported as broken credentials.
- **Breaking, for third-party deployment strategies.** The five action methods on
  `BaseDeploymentStrategy` now return a result model from `opsmith.core.results` — `deploy` an
  `EnvCreateResult`, `release` a `ReleaseResult`, `update` an `UpdateResult`, `run` a `RunResult`,
  `destroy` a `DestroyResult` — and a sixth method, `status`, reports what an environment is
  running by reading its own state file without contacting a cloud. A strategy still has to
  declare nothing ahead of time and still asks whatever it needs through `ctx.interact`; what
  changed is that it now describes what it did, because the CLI reports that. It may now also
  implement `questions`, listing what it is going to ask so that `opsmith env plan` can report
  it - which is optional, is never asked on the strategy's behalf, and costs a strategy that
  skips it nothing but a line in the plan saying the list is partial. Those descriptions name no
  topology — infrastructure is a list of `Resource`, not a public IP and an instance type — so a
  strategy that is not one machine can still answer honestly. There are no known third-party
  strategies; this is acceptable before 1.0.
- There is no blanket `--yes`. The draft CLI contract carried one, meaning "accept every
  destructive confirmation in this run"; it is not in 0.5.0. It was pure sugar - `--answer
  delete.confirm=DELETE` already says the same thing for one gate rather than all of them - and
  a flag that generic cannot be read where it is written: `opsmith --yes destroy --env dev` puts
  the approval nowhere near what it approves, and the word says nothing about which of a run's
  confirmations it covers. Anything irreversible is now answered by name, and `--accept-defaults`
  still refuses those keys, so no blanket option approves an irreversible action.
- Running `deploy` in a repository that has not been set up now exits 2 with `INVALID_CONFIG` and
  a hint naming `opsmith setup`, rather than exiting 1 with no machine-readable reason.
- Asking for an environment the configuration does not declare is `UNKNOWN_ENVIRONMENT` and exit
  2, with the names that do exist in the details. It used to be an unhandled `ValueError`.
- Importing Opsmith no longer requires `git` on the `PATH`. GitPython looks for the executable
  while it is being imported, so a module-level import made every command fail on a machine
  without git, long before anything asked for a repository. It is imported where a repository is
  opened instead, which is what lets the commands that need no tooling run without any.
- A missing or unrunnable `git` is now a usage error naming git, exit 2, rather than an unhandled
  crash reported as a bug in Opsmith. It is distinct from "this is not a git repository", because
  the two are fixed differently: one wants git installed, the other wants `git init`.
- The DNS step waits instead of asking. Opsmith shows the records, then polls until they resolve
  rather than asking whether they were created. Interactively it re-checks when you say so;
  headlessly it polls until `--wait-timeout` and then exits 8 with the records that are still
  missing, and creating them and running the command again resumes at the wait.
- `--model` and `--api-key` no longer prompt when they are missing. A run with no model
  configured exits 2 and names every way to supply one, so a headless run can never hang on a
  prompt. Option order no longer matters either, and `--help` no longer needs either of them.
- docker and terraform are checked per command rather than for every command, so commands that
  do not deploy anything run on a machine without them. A missing tool now exits 2 rather than 1.
- The rules that the `setup` editors enforced — a provider left as `user_choice`, the same
  provider listed twice — are now applied by `config validate` as well, against the same code.

### Fixed

- Reporting a problem with an edited service or dependency no longer crashes. The details of a
  notice are spread across the event's own fields, and a `ConfigIssue` carries a `message`, which
  collided with the event's - so the one path that reports an invalid edit raised a `TypeError`
  instead of saying what was wrong. Details named after a field of the event are nested now.
- Ansible variables no longer travel on the command line. They carry the whole compose `.env`
  body, which was readable by every process on the machine and was repeated in the details of a
  failed playbook. They go in a file only the current user can read, removed when the run ends.
- Build-time environment variables are no longer written to `state.yml` in the clear. They move
  to the environment's answer store, secrets to its secret half. An existing `state.yml` hands
  its values over on the next release or update, so nothing is asked for again, and the field is
  never written back.

### Removed

- The unused `networkx` and `pick` dependencies.
- `pydantic-ai` in favour of `pydantic-ai-slim[anthropic,google,openai]`, which is what the
  bundled models need. `boto3` and `pydantic-settings`, previously used but undeclared, are now
  declared. A third-party model plugin for a provider outside OpenAI, Anthropic and Google must
  now declare the pydantic-ai extra it needs.
