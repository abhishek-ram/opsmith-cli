# Phase 0g: Cloud provider questions and `env plan`

**Goal:** cloud providers declare their questions as data instead of prompting inside themselves, and `opsmith env plan` reports every answer a run will need before the run starts.
**Depends on:** 0f.
**Size:** M.
**Lands as:** the last part; the phase ships as 0.5.0.

## Scope

1. Split `get_account_details()` into detection, declared questions, and detail construction.
2. `opsmith/core/questions.py`: the question tree and its evaluation.
3. `opsmith env plan`, with `--accept-defaults` and `--write-answers`.
4. The updated plugin contract in the README, and the changelog note that it breaks third-party providers.

## Non-goals

- Requiring a question tree of anyone. A strategy or provider that declares nothing still works through the exit-3 loop from 0e; it loses `plan` coverage and nothing else. This is the property that keeps the strategy contract small.

## Design

### Providers stop prompting

`AWSProvider.get_account_details()` (`aws.py:132-190`) and `GCPProvider.get_account_details()` (`gcp.py:177-233`) mix three things: a credentials check, a prompt, and the construction of a detail object. Split them:

```python
@classmethod
def detect_account(cls) -> AccountInfo            # credentials check, account id, ssm plugin path; no interaction
@classmethod
def questions(cls) -> list[Question]              # region; project id, region and zone for GCP, with choice loaders
@classmethod
def build_detail(cls, account: AccountInfo, answers: dict) -> BaseCloudProviderDetail
```

For AWS, `detect_account` keeps the `session-manager-plugin` check (`aws.py:138`) and the STS call (`aws.py:148`); `questions()` declares `env.region` with `get_regions` as its choice loader. For GCP, `detect_account` keeps `google.auth.default()`; `questions()` declares `env.project_id`, `env.region` and `env.zone`. Note that the GCP project is a free-text field today despite the docstring claiming it lists projects — declaring it as a question with a loader is the point at which listing becomes possible, but listing itself is out of scope here.

`env create` walks `questions()` through the interaction API and then calls `build_detail`. A third-party provider may instead call `ctx.interact` directly inside `build_detail`.

### The question tree

```python
Question("env.cloud_provider", choices=registry_choices)
Question("env.project_id", when=provider_is("GCP"))
Question("env.region", depends_on=["env.cloud_provider"], choices=provider_regions)
Question("env.zone", depends_on=["env.region"], when=provider_is("GCP"), choices=provider_zones, default=first_choice)
Question("env.domain.<slug>", for_each=routed_services)
Question("envvar.<KEY>", for_each=env_vars_without_value, secret=from_config)
```

Choice loaders are side-effect-free reads: listing regions, reading the config. Evaluating a tree must never create anything.

`plan` evaluates the tree against the answers known so far and reports `answers_needed`, each with key, message, choices, default and whether it is required. It reports in rounds, because the tree forks: the cloud provider and the strategy determine which branches exist, so until they are answered the rest cannot be enumerated. Once they are given, everything else follows from the config.

`--write-answers <file>` emits a skeleton with defaults filled in and placeholders for the required values, which is the file a harness edits and passes back as `--answers`.

### `env plan`

```
opsmith env plan --name dev --provider AWS --strategy Monolithic [--accept-defaults] [--write-answers FILE]
```

It creates nothing and requires no external tools. A strategy without a declared tree still completes through the exit-3 loop; `plan` then reports only what it can derive from the config, and says so rather than claiming the list is complete.

### Plugin contract

The provider contract in the README changes: `get_account_details` is replaced by the three classmethods above. This breaks third-party provider packages. There are none known; note it in `CHANGELOG.md` as a breaking change, acceptable before 1.0.

## Code changes by file

| File | Change |
|------|--------|
| `opsmith/core/questions.py` | new: `Question`, `when`, `depends_on`, `for_each`, choice loaders, round-based evaluation, skeleton output |
| `opsmith/cloud_providers/base.py` | the three-classmethod contract replaces `get_account_details` |
| `opsmith/cloud_providers/aws.py` | `detect_account`, `questions`, `build_detail` |
| `opsmith/cloud_providers/gcp.py` | the same; the zone default stays `zones[0]` |
| `opsmith/deployment_strategies/monolithic.py` | declares its questions: instance type, domains, env vars |
| `opsmith/cli/commands/env.py` | `env plan`, `--write-answers` |
| `opsmith/core/operations.py` | `env create` walks `questions()` then calls `build_detail` |
| `README.md` | the updated provider plugin contract |
| `CHANGELOG.md` | the breaking change |

## Acceptance criteria

1. `opsmith env plan --provider AWS --strategy Monolithic --accept-defaults` lists exactly the domains of routed services and the env vars without values as required answers (phase acceptance criterion 9).
2. A stub third-party strategy that declares no questions still completes an environment through the exit-3 loop, and `plan` says its list is partial.
3. `env plan` creates nothing: no terraform working directory, no cloud resource, no config write, verified by running it against a fake provisioner factory that fails on any call.
4. `--write-answers` produces a file that `--answers` accepts, and a run using it asks nothing.
5. `env plan` runs with an empty `PATH`.

## Tests

- `test_questions.py`: tree evaluation with partial answers, `when` and `for_each` expansion, round boundaries, skeleton output.
- `test_cloud_providers.py`: `detect_account` with mocked SDK calls, `questions()` shape, `build_detail` from an answers dict, for both AWS and GCP.
- `test_env_plan.py`: the plan for a config with two routed services and three env vars; a strategy with no declared tree.
