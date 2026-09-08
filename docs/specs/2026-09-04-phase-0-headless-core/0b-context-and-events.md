# Phase 0b: Context, events and provisioner injection

**Goal:** core code no longer prints and no longer builds its own provisioners; it receives an `OpsmithContext` and emits events, so a strategy can be driven and asserted on in a test.
**Depends on:** 0a.
**Size:** M.
**Lands as:** an internal change; the phase ships as 0.5.0 when 0g lands.

## Scope

1. `opsmith/core/events.py` and `opsmith/core/context.py`.
2. `ProvisionerFactory`, and `BaseDeploymentStrategy.__init__(ctx)` in place of the positional `(agent, src_dir)` constructor.
3. Convert every print in core modules to an event, including subprocess streaming.
4. Replace `WaitingSpinner` with events.
5. Terraform and Ansible failures become `OpsmithError` subclasses.
6. `test_monolithic_strategy.py` with fake provisioners.

## Non-goals

- Touching prompts. `inquirer` calls stay where they are until 0d.
- Changing what any command does. This part is a rewiring; the text output must be recognisably the same.

## Design

### Events

```python
class Event(BaseModel):
    kind: Literal["step", "log", "warning", "output"]
    step: str
    message: str
    data: dict = {}
```

`EventSink` has one method, `emit(event)`. `NullSink` discards, and is the default in tests. The renderers from 0a consume events: `TextRenderer` prints them the way the code prints today, `JsonRenderer` streams them to stderr as NDJSON. With core prints gone, the "exactly one JSON document on stdout" guarantee from 0a is finally complete, which is why it is asserted here.

### Context

```python
OpsmithContext(src_dir, deployments_path, events, agent, provisioner_factory, git_repo, verbose)
```

Built once in `cli/app.py`, replacing the three-key `ctx.obj` dict (`main.py:153-157`). `interact`, `answers` and `steps` are added to it by 0d and 0e; declare the attributes now as optional so later parts only fill them in.

### Provisioner injection

Provisioners are created ad hoc at eleven sites today, each one `Provisioner(working_dir=...)` followed by `copy_template(...)`: `base.py:132`, `:211`, `:322`, `:356`, `:386`, `:436`, and `monolithic.py:162`, `:542`, `:581`, `:624`, `:1000`, `:1025`, `:1101`, `:1153`. All of them go through `ctx.provisioner_factory.terraform(working_dir)` / `.ansible(working_dir)`.

`GitRepo` moves out of the base constructor (`base.py:109`) and onto the context, so constructing a strategy no longer touches the filesystem and a test can substitute a fake.

`BaseDeploymentStrategy.__init__(ctx)` replaces `__init__(agent, src_dir)`; `src_dir`, `deployments_path` and `agent` are read from the context. The two registry call sites in the deploy command (`main.py:470-473`, `:501-504`) are updated.

While these call sites move, fix the template-subdirectory casing: most sites use `provider.name().lower()` but `base.py:129`, `base.py:388`, `monolithic.py:1096` and `monolithic.py:1155` use the bare `name()`. Normalise in the factory so a template directory is looked up one way only.

### Prints become events

There are about 155 `print(` calls in the package, all of them `rich`'s print, spread over eleven modules — `monolithic.py` (71), `main.py` (30, already inside the boundary after 0a), `base.py` (21), `service_detector.py` (16), the provisioners (9), `git_repo.py` (3), `models.py` (2), `cloud_providers/base.py` (2) and `types.py` (1). Each becomes `ctx.events.emit(Event(kind=..., step=..., message=...))` with a step name from the small set already implied by the code: `registry`, `build`, `vm`, `compose`, `dns`, `frontend`, `detect`, `destroy`.

Two of them are line-by-line subprocess streams and emit `kind="output"` per line: `BaseInfrastructureProvisioner._run_command` (`base_provisioner.py:72`) and `ServiceDetector._run_command_with_streaming_output` (`service_detector.py:264`).

`DeploymentConfig.save` (`types.py:258`) and `_save_deployment_state` (`monolithic.py:470-484`) stop printing; persistence is silent and the caller reports it.

`repo_map.py` is the module the original spec missed: nine `typer.echo` calls (`L105`, `156`, `171`, `181`, `193`, `246`, `266`, `335`, `343`), several with `err=True`. They become events like the rest, which is what lets `repo_map.py` pass the boundary test in 0d.

### The spinner is a third UI channel

`WaitingSpinner` (`utils.py:94-114`) wraps `rich.console.Console` and `rich.status.Status` and is used from five core modules: `monolithic.py:220,319,649,664`, `service_detector.py:104,191,353`, `aws.py:50`, `gcp.py:111`, `repo_map.py:164`. Each use becomes a pair of `kind="step"` events, one on enter and one on exit, and `TextRenderer` renders the pair as the spinner it is today. `utils.py` loses its `rich` imports; `build_logo` (`utils.py:65`) moves to `opsmith/cli/`.

### Provisioner failures

`TerraformError` and `AnsibleError` subclass `OpsmithError` with codes `TERRAFORM_FAILED` and `ANSIBLE_FAILED`, both exit 4, carrying the command and the tail of its output in `details`.

## Code changes by file

| File | Change |
|------|--------|
| `opsmith/core/events.py`, `opsmith/core/context.py` | new |
| `opsmith/core/provisioners.py` | new: `ProvisionerFactory` |
| `opsmith/cli/app.py` | builds the context; renderers subscribe to the sink; `build_logo` lands here |
| `opsmith/deployment_strategies/base.py` | constructor takes the context; provisioners from the factory; 21 prints become events; `GitRepo` from the context |
| `opsmith/deployment_strategies/monolithic.py` | provisioners from the factory; 71 prints and 4 spinners become events |
| `opsmith/service_detector.py` | takes the context; 16 prints, 3 spinners and the streaming output become events |
| `opsmith/infra_provisioners/*.py` | stream through events; raise `TerraformError` / `AnsibleError` |
| `opsmith/repo_map.py` | 9 `typer.echo` calls and 1 spinner become events; `typer` import removed |
| `opsmith/utils.py` | `WaitingSpinner` removed, `build_logo` moved out, `rich` imports gone |
| `opsmith/types.py`, `opsmith/models.py`, `opsmith/git_repo.py`, `opsmith/cloud_providers/base.py` | remaining prints become events |

## Acceptance criteria

1. Text output for `setup` and `deploy` is recognisably what it is today, spinners included.
2. Under `--output json`, stdout carries exactly one JSON document and every progress line, including subprocess output, arrives on stderr as NDJSON.
3. `grep -rn "from rich import print" opsmith/` returns nothing outside `opsmith/cli/`, and `opsmith/utils.py` imports no `rich`.
4. A monolithic deploy of one API service plus postgres runs against fake provisioners in a test, with the terraform variables and ansible extra-vars asserted (phase acceptance criterion 6).
5. A strategy can be constructed in a test without a git repository on disk.

## Tests

- `test_monolithic_strategy.py`: deploy, release and destroy with fakes; the order of provisioner calls, the terraform variables and the ansible extra-vars for one API service plus postgres.
- `test_events.py`: `NullSink` discards; `JsonRenderer` emits one NDJSON line per event on stderr; `TextRenderer` renders a step pair as a spinner.
- `conftest.py` gains the fake terraform and ansible provisioners and a fake `GitRepo`, used by every later part.
