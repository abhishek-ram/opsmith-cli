# Changelog

All notable changes to Opsmith are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

The headless core, phase 0 of the [migration to 1.0](docs/notes/2026-09-04-migration-plan.md).
It ships as 0.5.0 once every part has landed; parts 0a to 0e are in.

### Added

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
- `--yes` accepts the confirmations that guard something destructive, including the typed
  deletion gate. `--accept-defaults` never does.
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
