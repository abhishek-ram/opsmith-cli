# Phase 0a: Errors, exit codes and the CLI package split

**Goal:** `opsmith/cli/` exists and owns every terminal concern, `opsmith/core/errors.py` defines the error hierarchy and the exit-code map, and every command returns through one envelope. A user at a terminal sees no change.
**Depends on:** nothing.
**Size:** M.
**Lands as:** an internal change; the phase ships as 0.5.0 when 0g lands.

## Scope

1. `opsmith/core/errors.py`: `OpsmithError(code, message, hint, details)`, a subclass per error code, and the code-to-exit-code map.
2. `opsmith/cli/app.py`: the Typer app, global options, renderer selection, and the single handler that turns an error into an envelope and an exit code.
3. `opsmith/cli/output.py`: `TextRenderer` reproducing today's rich output, `JsonRenderer` writing the envelope.
4. `opsmith/cli/commands/`: today's `setup`, `deploy` and `repomap` moved out of `main.py` with their bodies unchanged. `main.py` becomes a re-export.
5. `GitRepo` stops raising `typer.Exit`.
6. Test infrastructure: pytest configuration, a `conftest.py`, and a CI job that runs the tests.

## Non-goals

- Removing prompts or prints from core modules. `opsmith/cli/commands/*` may keep calling `inquirer` and `rich` directly; they are inside the boundary. Core modules keep printing until 0b.
- Handling `--non-interactive` and the answer flags. They are declared as global options here so the surface is stable, and 0e implements them.

## Design

### Error hierarchy

One base class carrying exactly the four fields the envelope needs:

```python
class OpsmithError(Exception):
    code: str
    message: str
    hint: str | None = None
    details: dict = {}
```

Subclasses for the codes this phase can raise: `InvalidConfig`, `InvalidArgument`, `UnknownEnvironment`, `UnknownService` (exit 2); `TerraformFailed`, `AnsibleFailed`, `DockerFailed`, `DeployUnhealthy` (exit 4); `CloudCredentials`, `CloudPermission` (exit 5); `LlmGaveUp` (exit 6). `MISSING_ANSWER` (3) and `PENDING_ACTION` (8) arrive in 0e. Codes owned by later phases (`UNKNOWN_RECIPE`, `TEMPLATE_*`, `CAPACITY_UNSATISFIABLE`, `STATE_*`) are not declared yet.

`EXIT_CODES: dict[str, int]` is the one mapping, and a test asserts every declared subclass appears in it. Anything that is not an `OpsmithError` is reported as `INTERNAL`, exit 1.

The existing `CloudCredentialsError` (`cloud_providers/base.py:122`) becomes a subclass so its raise sites in `aws.py` and `gcp.py` keep working untouched.

### The app and the callback

`cli/app.py` holds `app = typer.Typer(pretty_exceptions_show_locals=False)` and the callback. The callback keeps doing what `main.py:105-158` does today — logo, optional logfire, `src_dir`, the shared object, the external-dependency check — with two changes: the logo prints only in text mode on a TTY, and the callback also selects the renderer. Model resolution stays exactly as it is; 0c replaces it.

Every command body is wrapped by one decorator that catches `OpsmithError`, hands it to the renderer, and exits with `EXIT_CODES[err.code]`; anything else becomes `INTERNAL`. A `typer.Exit` raised inside a command passes through.

### Output

`TextRenderer` writes what the code writes today. `JsonRenderer` buffers and writes exactly one document to stdout at the end:

```json
{"ok": true,  "command": "setup", "result": {}, "warnings": []}
{"ok": false, "command": "setup", "error": {"code": "INVALID_CONFIG", "message": "...", "hint": "...", "details": {}}}
```

`result` is an empty object until 0f defines the typed results. Under `--output json` everything `cli/` would have printed goes to stderr instead. Core modules still print directly to stdout at this point, so the "exactly one document on stdout" guarantee is only complete after 0b; state that in the part-0b acceptance criteria, not here.

### Commands

`setup` (`main.py:199`), `deploy` (`main.py:373`) and `repomap` (`main.py:577`) move to `cli/commands/setup.py`, `cli/commands/deploy.py` and `cli/commands/analyze.py` with their bodies unchanged, taking their helpers with them: `_collect_domain_configuration` (`main.py:307`) to `deploy.py`, `_validate_service_config` and `_validate_infra_deps_config` (`main.py:161`, `:171`) to `setup.py`. 0c extracts the validation itself into core, where `config validate` can reuse it.

`opsmith/main.py` becomes `from opsmith.cli.app import app`, so `opsmith = "opsmith.main:app"` in `pyproject.toml` keeps resolving.

### GitRepo stops exiting

`GitRepo.__init__` raises `typer.Exit()` (`git_repo.py:33`) when the directory is not a repository, which means constructing a deployment strategy can raise a CLI exception from library code. It raises `NotAGitRepository` (`INVALID_ARGUMENT`) instead. Its three prints stay until 0b.

### verbose

`--verbose` is declared on the callback, but `repomap` reads it through `ctx.parent.params["verbose"]` (`main.py:586`) and `setup` never passes it to `ServiceDetector`, so detection is never verbose regardless of the flag. Carry it in the object the callback builds and pass it through to the detector.

### Test infrastructure

There is one test file, no `conftest.py`, no pytest configuration, and the only workflow is `.github/workflows/python-publish.yml`. Add `[tool.pytest.ini_options]` to `pyproject.toml`, a `conftest.py` with a temporary-project fixture (a `src_dir` containing a git repository and an empty `.opsmith/`), and a CI job that runs `pytest`. Every later part depends on this existing.

## Code changes by file

| File | Change |
|------|--------|
| `opsmith/core/errors.py` | new: `OpsmithError`, subclasses, `EXIT_CODES` |
| `opsmith/cli/app.py` | new: app assembly, callback, global options, renderer selection, error-to-exit-code handler |
| `opsmith/cli/output.py` | new: `TextRenderer`, `JsonRenderer` |
| `opsmith/cli/commands/{setup,deploy,analyze}.py` | new: today's command bodies moved verbatim |
| `opsmith/main.py` | becomes `from opsmith.cli.app import app` |
| `opsmith/cloud_providers/base.py` | `CloudCredentialsError` subclasses `OpsmithError` |
| `opsmith/git_repo.py` | `typer.Exit` becomes `NotAGitRepository` |
| `pyproject.toml` | `[tool.pytest.ini_options]` |
| `.github/workflows/` | a test job |

## Acceptance criteria

1. Interactive `setup`, `deploy` and `repomap` behave exactly as they do today for a user at a terminal.
2. A command that raises an `OpsmithError` under `--output json` prints exactly one envelope on stdout and exits with the code that `EXIT_CODES` maps.
3. An unexpected exception produces an `INTERNAL` envelope and exit 1, not a traceback, unless `--verbose`.
4. `opsmith --help` works from an editable install, proving the `opsmith.main:app` entry point still resolves.
5. `opsmith repomap --verbose` and `opsmith setup --verbose` actually produce verbose output.
6. `pytest` runs in CI and the existing detector tests pass unchanged.

## Tests

- `test_cli_contract.py`: envelope shape for success and for one error per exit class that exists in this part; every `OpsmithError` subclass has an entry in `EXIT_CODES`; an unexpected exception maps to `INTERNAL`/1.
- `test_git_repo.py`: a directory that is not a repository raises `NotAGitRepository`.
- `test_service_detector.py`: unchanged, still green.
