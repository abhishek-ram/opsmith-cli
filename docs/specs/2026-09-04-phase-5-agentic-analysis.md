# Phase 5: Agentic repo analysis

**Goal:** the built-in agent explores a repository with the same kind of tools a coding harness uses, instead of a pre-computed map, and detection quality is measured against fixture repositories.
**Depends on:** phase 0 (model configuration, events), phase 1 (schema v2 output).
**Size:** M.
**Ships as:** 0.9.0.

## Scope

1. A repo index with allow-list semantics that works with or without git.
2. Agent tools: list files, grep, read file by range, outline, inventory.
3. Prompt files replacing string constants, with schema-derived field documentation.
4. Guardrails: usage limits, output caps, duplicate-call detection.
5. Post-processing validation of detection output.
6. An evaluation harness with fixture repositories.
7. Removal of `repo_map.py` and its dependencies.

## Non-goals

- Deterministic heuristics that decide service boundaries. The inventory tool informs; the model decides.
- Replacing Dockerfile generation with buildpacks. Noted as a later option.

## Design

### Tools versus heuristics

Tools are the primary mechanism. One deterministic tool, `inventory`, exists to save the model a dozen orientation calls and to give headless commands a JSON summary. It never assigns service types or frameworks.

### Repo index

`opsmith/core/repo.py`:

```python
class RepoIndex:
    def __init__(self, root: Path, git: Optional[GitRepo])
    files: list[RelPath]                 # git ls-files when git is present, else os.walk with ignore rules
    def resolve(self, rel: str) -> Path  # rejects absolute paths, traversal, and paths not in the index
    def is_text(self, rel: str) -> bool
```

Ignore rules without git: `.gitignore` files via `pathspec`, plus built-in ignores (`.git`, `node_modules`, `.venv`, `venv`, `dist`, `build`, `target`, `__pycache__`, `.opsmith/environments`). Files over 2 MB and binary files are indexed for listing but refused by `read_file`.

### Tools

Registered on the agent in `opsmith/core/agent_tools.py`, replacing `read_repo_files` and `generate_secret`:

| Tool | Signature | Behaviour |
|------|-----------|-----------|
| `list_files` | `(glob: str = "**/*", max_results: int = 200)` | matches against the index; returns paths and `truncated` |
| `grep` | `(pattern: str, glob: str | None = None, max_results: int = 100, context: int = 0, case_sensitive: bool = False)` | ripgrep with `--json` when on PATH and restricted to indexed files, else Python `re`; returns `{path, line, text, context}` |
| `read_file` | `(path: str, start_line: int = 1, end_line: int | None = None)` | max 400 lines or 20 KB per call; returns `total_lines` and a hint when truncated |
| `outline` | `(path: str)` | tree-sitter definitions using the existing `queries/tree-sitter-languages/*.scm`; returns `{kind, name, line, end_line}` |
| `inventory` | `()` | the deterministic summary below |

Every tool result is capped and every cap is stated in the tool description so the model can page.

### Inventory

`RepoInventory` (also the output of `opsmith analyze --output json`):

- top-level tree to depth 2 with file counts;
- language mix by extension count;
- manifests with parsed facts: `package.json` (name, workspaces, scripts, engines, dependency names), `pyproject.toml`/`requirements*.txt`/`Pipfile`/`uv.lock` (dependency names, python version), `go.mod`, `Cargo.toml`, `Gemfile`, `pom.xml`/`build.gradle*`, `composer.json`, `mix.exs`;
- lockfiles present;
- container and infra assets: Dockerfiles, compose files, Procfile, `.env*` names, CI workflow files, Terraform or Kubernetes files;
- version files: `.nvmrc`, `.node-version`, `.python-version`, `.tool-versions`, `.ruby-version`;
- entrypoint candidates by well-known names (`manage.py`, `main.py`, `app.py`, `server.js`, `index.ts`, `main.go`, `Application.java`, `config.ru`).

Target size: under 2k tokens; lists are truncated with counts.

### Prompts as files

`opsmith/prompts/` with `repo_analysis.md`, `dockerfile_generation.md`, `dockerfile_validation.md`, `capacity_estimate.md`, `deploy_failure_explanation.md`, loaded through `importlib.resources`. Each prompt receives:

- the JSON schema of its output type, generated from pydantic, so field docs never drift;
- the inventory as initial context (repo analysis and Dockerfile generation);
- an exploration strategy: start from manifests and entrypoints, confirm ports and env vars by grep, read only the ranges needed, stop when every required field has evidence;
- the reference grammar from phase 1, instructing `value: "{{ infra.<instance>.url }}"` style references for infra-derived env vars and `routes` for web services.

The compose generation prompt was deleted in phase 1 and the machine-list prompt became the capacity-estimate prompt. `SYSTEM_PROMPT` moves to `system.md`.

### Guardrails

- `UsageLimits(request_limit=settings.max_agent_requests)` with default 60 for detection and 40 for Dockerfile generation; exceeding it raises `LLM_GAVE_UP` with the partial tool trace in `details`.
- Keep `is_duplicate_tool_call`; extend it to `read_file` ranges.
- Per-run wall clock timeout from settings.
- Tool call trace emitted as `log` events so `--output text` shows what the model is doing.

### Post-processing

After detection, before the user review step:

- `build_dir`, `source.context`, and `source.dockerfile` paths must exist in the index; otherwise the field is cleared and a warning is attached.
- Reference validation from phase 1.
- Slug generation as today.
- In headless mode the warnings go into the JSON result; `--strict` turns them into `INVALID_CONFIG`.

### `opsmith analyze` and `setup`

- `opsmith analyze [--output json]` prints the inventory, a deterministic summary that involves no detection run. Replaces `repomap`.
- `opsmith setup --skeleton` writes a `deployments.yml` with one placeholder service per detected manifest root, filled from inventory facts only, for a person or a harness agent to complete, without a detection run.
- `opsmith setup` with an LLM runs detection with the tools; `--yes` accepts the result.

### Evaluation harness

- `opsmith/tests/fixtures/repos/<name>/` small synthetic repositories: `django-react-monorepo`, `fastapi-worker-redis`, `express-postgres`, `nextjs-fullstack`, `go-api`, each with `expected.yml`.
- Unit tests drive the agent with `FunctionModel` scripts that issue deterministic tool calls and assert tool behaviour and post-processing.
- `scripts/eval_detection.py --model <name>` runs real models over the fixtures and reports field-level accuracy (service count, type, port, framework, infra instances, route presence) to `docs/evals.md`. Run manually before releases.

### Removals

- `opsmith/repo_map.py`, `RepoMap`, the `repomap` command.
- Dependencies `grep-ast`, `tqdm`. `tree-sitter` and `tree-sitter-language-pack` stay for `outline`. Add `pathspec`.
- `constants.py::ROOT_IMPORTANT_FILES` is folded into the inventory's manifest and asset lists.

## Compatibility with existing environments

- `setup` on an existing project passes the current config to the agent as before; the result still goes through the review step, and nothing changes on any environment until `update` runs.
- A rescan may introduce `value` references for infra-derived variables where the v1 config held literal defaults. The review step shows them, and `compose diff` from phase 1 shows the effect on an environment before it is applied.
- The `repomap` command is removed; `analyze` replaces it. Scripts that call `repomap` must change.

## Code changes by file

| File | Change |
|------|--------|
| `opsmith/core/repo.py` | new index |
| `opsmith/core/agent_tools.py` | new tools; `agent.py` registers them |
| `opsmith/core/inventory.py` | inventory builder and model |
| `opsmith/prompts/*.md`, `opsmith/core/prompts.py` | prompt loading with schema injection; delete `prompts.py` constants |
| `opsmith/service_detector.py` | inventory as context; tools; post-processing; usage limits |
| `opsmith/agent.py` | `AgentDeps` carries `RepoIndex`; tool registration moves out |
| `opsmith/cli/commands/analyze.py` | `analyze`, `setup --skeleton` |
| `opsmith/settings.py` | `max_agent_requests`, `agent_timeout_s` |
| `pyproject.toml` | dependency changes |
| `scripts/eval_detection.py`, `docs/evals.md` | evaluation |

## Acceptance criteria

1. Detection on each fixture repository with the reference model reaches the accuracy thresholds recorded in `docs/evals.md` at the time of the phase's merge; thresholds are set from the first run and must not regress.
2. Detection on a fixture with ripgrep absent produces the same result through the Python fallback.
3. A detection run never exceeds the request limit and, when it does, exits 6 with the tool trace.
4. `opsmith analyze --output json` on a repository without git works and lists manifests.
5. `read_file` refuses paths outside the index and files over the size cap with clear messages.

## Tests

- `test_repo_index.py`: git and non-git listing, ignore rules, traversal refusal, binary detection.
- `test_agent_tools.py`: caps, truncation hints, ripgrep and fallback parity on a fixture.
- `test_inventory.py`: parsed facts per manifest type.
- `test_detection_postprocess.py`: path clearing, warnings, strict mode.
- `test_detection_scripted.py`: `FunctionModel` runs over fixtures.

## Risks and open questions

- Agentic loops cost more tokens than the old one-shot map; the inventory context, request limits and harness mode (phase 6) are the mitigations.
- Model behaviour varies across providers; the eval script is what keeps regressions visible, so it must run before releases.
- Buildpack-style deterministic Dockerfiles (for example Railpack) would remove the LLM from the last generation step for common stacks; track as a follow-up after this phase.
