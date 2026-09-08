# Phase 0d: Interaction API and the terminal implementation

**Goal:** every question a person is asked goes through one API with a stable key, the terminal implementation lives in `opsmith/cli/`, and the boundary test has nothing left to allow.
**Depends on:** 0b.
**Size:** M.
**Lands as:** an internal change; the phase ships as 0.5.0 when 0g lands.

## Scope

1. `opsmith/core/interaction.py`: the `Interaction` protocol and `Choice`.
2. `opsmith/cli/interaction.py`: `TerminalInteraction`, on `inquirer` as today.
3. Convert all 23 `inquirer.prompt()` sites to interaction calls with keys from the table in the [migration plan](../../notes/2026-09-04-migration-plan.md).
4. Close the boundary test.

## Non-goals

- Headless behaviour. `HeadlessInteraction`, the answer sources and the answer store are 0e. A run with no TTY still behaves as it does today until then.
- Changing what is asked, in what order, or with what defaults. A user at a terminal must not be able to tell this part landed.

## Design

### The API

Five primitives, called inline wherever the code calls `inquirer` today. This is the whole contract for third-party strategies: they neither declare questions ahead of time nor restructure their flow.

```python
class Choice(BaseModel):
    label: str
    value: Any
    recommended: bool = False

class Interaction(Protocol):
    def ask(self, key: str, message: str, *, default: str | None = None, secret: bool = False,
            validate: Callable[[str], str | None] | None = None) -> str: ...
    def select(self, key: str, message: str, choices: list[Choice], *, default: Any = None) -> Any: ...
    def confirm(self, key: str, message: str, *, details: Any = None, default: bool = False) -> bool: ...
    def edit(self, key: str, message: str, *, path: Path, on_headless: Literal["accept", "fail"]) -> str: ...
    def wait_for(self, key: str, message: str, *, check: Callable[[], bool], details: Any = None,
                 timeout_s: int | None = None) -> None: ...
    def notify(self, message: str, *, details: Any = None) -> None: ...
```

`TerminalInteraction` maps them onto `inquirer`: `ask` to `Text` or `Password`, `select` to `List`, `confirm` to `Confirm`, `edit` to `Editor`, `wait_for` to printing `details` and polling `check` with a keypress to re-check or skip, `notify` to a print. It reaches the code through `ctx.interact`, the attribute 0b reserved on the context.

`validate` changes shape. Inquirer validators are `(answers, value) -> bool` that print their own errors; an interaction validator is `(value) -> str | None`, returning the message. The two config validators already moved to `core/config.py` in 0c, so this is a signature change at the call site only.

### Call sites

Every one of the 23 `inquirer.prompt()` calls becomes an interaction call, keyed per the overview's table.

| Where | Sites | Keys |
|-------|-------|------|
| `cli/commands/setup.py` | 4 | `setup.action`, `app.name`, `service.<slug>.confirm`, `infra_deps.confirm` |
| `cli/commands/deploy.py` | 9 | `env.domain_email`, `env.domain.<slug>`, `env.action`, `env.cloud_provider`, `env.name`, `env.strategy`, `run.service`, `run.command`, `delete.confirm` |
| `deployment_strategies/monolithic.py` | 6 | `envvar.<KEY>`, `compose.edit`, `build_env.<slug>.<KEY>`, `env.instance_type`, `dns.<slug>`, `update.confirm_infra_changes` |
| `service_detector.py` | 1 | `dockerfile.edit` |
| `cloud_providers/aws.py`, `gcp.py` | 3 | `env.region`, `env.project_id` |

The command modules are inside `opsmith/cli/` and could keep calling `inquirer` directly, but they must not: 0f drives the same flows headlessly, and only keyed calls can be answered from a flag or a file.

`MachineTypeList.as_options()` (`cloud_providers/base.py:34-55`) builds inquirer-shaped `(label, value)` tuples inside a core module. It returns `list[Choice]`, with the model's recommendation marked `recommended=True` instead of being passed as a bare default.

The two editors that already exist in `monolithic.py:361` and `service_detector.py:230` are declared `on_headless="fail"`; the three review editors in `setup` are declared `on_headless="accept"`. Neither flag does anything until 0e, but classifying them here means 0e adds no new judgement calls.

### A cancelled prompt is not a credentials error

`aws.py:170` and the equivalent in `gcp.py` raise `ValueError` when the user cancels a region prompt, and the `except Exception` at `aws.py:183` / `gcp.py:229` converts it into `CloudCredentialsError` with an install URL — a cancel is reported as broken credentials. With the prompts behind the API, a cancel raises its own error and the catch-all narrows to the SDK calls it was meant to cover.

### The boundary rule closes

No module outside `opsmith/cli/` may import `inquirer`, `typer`, `click`, `rich.prompt`, `rich.print`, `rich.console` or `rich.status`. `test_boundaries.py` walks the package and fails on a violation.

The test file is added in 0a with an allowlist of the modules that still import UI, and each part removes its own entries: 0b removes the provisioners, the strategies, the detector, `repo_map.py` and `utils.py`; 0c removes what is left of the model options; this part empties it. A non-empty allowlist after this part is a failing test, not a to-do.

## Code changes by file

| File | Change |
|------|--------|
| `opsmith/core/interaction.py` | new: `Interaction`, `Choice` |
| `opsmith/cli/interaction.py` | new: `TerminalInteraction` |
| `opsmith/cli/app.py` | constructs the interaction and puts it on the context |
| `opsmith/cli/commands/{setup,deploy}.py` | 13 inquirer sites become keyed interaction calls |
| `opsmith/deployment_strategies/monolithic.py` | 6 sites; `inquirer` import removed |
| `opsmith/service_detector.py` | 1 site; `inquirer` import removed |
| `opsmith/cloud_providers/{aws,gcp}.py` | 3 sites; `inquirer` import removed; the cancel path no longer reports a credentials error |
| `opsmith/cloud_providers/base.py` | `as_options()` returns `list[Choice]` |
| `opsmith/tests/test_boundaries.py` | allowlist emptied |

## Acceptance criteria

1. Interactive `setup` and `deploy` ask the same questions in the same order with the same defaults as before.
2. `test_boundaries.py` passes with an empty allowlist (phase acceptance criterion 4).
3. `grep -rn "import inquirer" opsmith/` returns nothing.
4. Every key used by an interaction call appears in the key table in the migration overview, asserted by a test that collects the keys from the source.
5. Cancelling the AWS region prompt reports a cancelled interaction, not `CLOUD_CREDENTIALS`.

## Tests

- `test_interaction.py`: the terminal implementation maps each primitive onto the expected inquirer question with a mocked `inquirer.prompt`; validator adaptation; `select` returns the choice value, not its label.
- `test_boundaries.py`: the import rule, allowlist empty.
- `test_cloud_providers.py`: a cancelled region selection does not become `CloudCredentialsError`.
