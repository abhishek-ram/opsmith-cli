# Phase 0c: Model configuration, tool checks, `config` commands and dependency hygiene

**Goal:** the model is configured once from flags, environment or settings; each command declares the external tools it needs; and `config validate|schema|show` exist so a harness can check a repository without docker, terraform or a cloud account.
**Depends on:** 0a. Can be built in parallel with 0b and 0d.
**Size:** S.
**Lands as:** an internal change; the phase ships as 0.5.0 when 0g lands.

## Scope

1. `opsmith/core/llm.py`: resolve and validate the model and API key once at startup.
2. Per-command external-tool requirements replacing the global check.
3. `config validate|schema|show`, on validation logic extracted out of the editor callbacks.
4. Dependency hygiene and `CHANGELOG.md`.

## Non-goals

- Changing which steps call the model. Phase 1 replaces the rendering steps; this part only changes how the model is configured.

## Design

### Model configuration

`--model` and `--api-key` stay required, but may be satisfied without being typed. Resolution order, evaluated once in `core/llm.py`:

1. `--model` / `--api-key`
2. `OPSMITH_MODEL`, and the provider key variable `models.py` already reads (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`)
3. `.opsmith.conf.yml`

A missing or unknown model is `INVALID_ARGUMENT` (exit 2) with a hint listing `MODEL_REGISTRY.model_names`, instead of the current `typer.BadParameter`.

This removes two rough edges. `--model` and `--api-key` carry Typer `prompt=` today (`main.py:113`, `:124`), so a headless run with no flags hangs on a prompt; the prompts go, since the environment can now supply both. And `_api_key_callback` (`main.py:77-102`) reads `ctx.params["model"]` and fails with "The --model option must be specified before --api-key", making the CLI order-dependent; resolving both together in one place ends that.

`ensure_auth` still exports the provider key into the environment (`models.py:59-61`), because `pydantic-ai` reads it there. The agent is built once from the resolved configuration and, once 0b has landed, lives on the context; until then it stays in the object the callback builds.

### Per-command tool requirements

`_check_external_dependencies()` (`main.py:34-47`) runs in the callback for every command, so `config validate` would fail on a machine without docker. Replace it with a declaration:

```python
@command(requires=["docker", "terraform"])
```

`config validate`, `config schema`, `config show`, and later `env list` and `env status`, require nothing. The check reuses `get_missing_external_dependencies` (`utils.py`) and raises `InvalidArgument` with the install hint the current code prints. `terraform version -json` is parsed and the version recorded on the context, so phase 3 can enforce `terraform >= 1.10` without adding another probe.

### The `config` commands

```
opsmith config validate [--file PATH]
opsmith config schema [--format json|markdown]
opsmith config show [--output json]
```

The validation logic exists already but is trapped in UI: `_validate_service_config` (`main.py:161`) and `_validate_infra_deps_config` (`main.py:171`) have inquirer's `(answers, value) -> bool` signature and print their own errors. Extract the parsing and validation into `core/config.py` returning a list of `{path, message}`, and have both the editor callbacks and `config validate` call it. `config validate` returns a `ValidateResult` (`ok`, `errors`, `warnings`); the full typed-result machinery arrives in 0f, so define just this one here.

`config schema` emits the JSON Schema pydantic already generates from `DeploymentConfig`, and a markdown rendering of it for the phase 6 skill.

### Dependency hygiene

- Declare `boto3` and `pydantic-settings`, both imported but undeclared.
- Move `logfire` to an optional extra, `opsmith-cli[logfire]`, imported lazily where the callback configures it.
- Remove `networkx` and `pick`; nothing imports either.
- Remove the unwired `*_V2` constants from `prompts.py`; phase 5 rewrites prompts as files.
- Add `CHANGELOG.md`, with an entry for this phase.

## Code changes by file

| File | Change |
|------|--------|
| `opsmith/core/llm.py` | new: resolution order, validation, agent construction |
| `opsmith/core/config.py` | new: config parsing and validation, shared by the editors and `config validate` |
| `opsmith/cli/app.py` | model options lose `prompt=`; `_api_key_callback` and `_check_external_dependencies` removed; `requires` handled per command |
| `opsmith/cli/commands/config.py` | new: `validate`, `schema`, `show` |
| `opsmith/cli/commands/setup.py` | editor callbacks delegate to `core/config.py` |
| `opsmith/prompts.py` | `*_V2` constants removed |
| `pyproject.toml` | `boto3`, `pydantic-settings` declared; `logfire` extra; `networkx`, `pick` removed |
| `CHANGELOG.md` | new |

## Acceptance criteria

1. `opsmith config validate --output json` succeeds with the model supplied only through `OPSMITH_MODEL` and the provider key variable, and with neither docker nor terraform on `PATH` (phase acceptance criterion 2).
2. `opsmith --api-key K --model gpt-4o setup` works: option order no longer matters.
3. An unknown model name exits 2 with `INVALID_ARGUMENT` and a hint listing the registry names.
4. `opsmith deploy` on a machine without terraform still fails early with the install hint.
5. `pip install opsmith-cli` pulls neither `networkx`, `pick` nor `logfire`; `pip install "opsmith-cli[logfire]"` enables tracing.

## Tests

- `test_llm_config.py`: resolution order across flags, environment and settings file; unknown model raises `InvalidArgument` with the registry names in the hint.
- `test_requirements.py`: a command declaring no requirements runs with an empty `PATH`; one declaring `terraform` raises with a hint; the parsed terraform version reaches the context.
- `test_config_commands.py`: `validate` on a good and a broken `deployments.yml`; the errors carry `path` and `message`; `schema` emits valid JSON Schema.
