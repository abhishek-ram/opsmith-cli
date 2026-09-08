# Changelog

All notable changes to Opsmith are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

The headless core, phase 0 of the [migration to 1.0](docs/notes/2026-09-04-migration-plan.md).
It ships as 0.5.0 once every part has landed; parts 0a, 0b and 0c are in.

### Added

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

- `--model` and `--api-key` no longer prompt when they are missing. A run with no model
  configured exits 2 and names every way to supply one, so a headless run can never hang on a
  prompt. Option order no longer matters either, and `--help` no longer needs either of them.
- docker and terraform are checked per command rather than for every command, so commands that
  do not deploy anything run on a machine without them. A missing tool now exits 2 rather than 1.
- The rules that the `setup` editors enforced — a provider left as `user_choice`, the same
  provider listed twice — are now applied by `config validate` as well, against the same code.

### Removed

- The unused `networkx` and `pick` dependencies.
- `pydantic-ai` in favour of `pydantic-ai-slim[anthropic,google,openai]`, which is what the
  bundled models need. `boto3` and `pydantic-settings`, previously used but undeclared, are now
  declared. A third-party model plugin for a provider outside OpenAI, Anthropic and Google must
  now declare the pydantic-ai extra it needs.
