# Phase 2f: Validation, repair and init jobs

**Goal:** whether a deploy came up is judged by the model over facts that code gathers, a compose file at fault is repaired, and init jobs run once a deploy is healthy.
**Depends on:** 2d.
**Size:** M.
**Lands as:** a part of phase 2; the phase ships as 1.0.0 with phase 3 once 2h lands.

## Scope

1. The health wait, and the facts the deploy playbook returns.
2. The failure floor.
3. The model's judgment, and repair of a compose file at fault.
4. What a failed deploy carries.
5. Init jobs.

## Non-goals

- Keeping a repair past the next `update`, and applying a fix whose cause is in `deployments.yml` to that file. Both are left to phase 3.

## Design

These steps replace the stopgap judgment from [2d](2d-deterministic-deploys.md) after every deploy: `env create`, `update`, `release` and `env vars` alike. `_deploy_validate_docker_compose` goes with it.

1. Health wait: after `compose up`, the deploy playbook polls until Docker has reached a verdict, `healthy` or `unhealthy`, on every service with a healthcheck, bounding each at `start_period_s + (retries + 1) × (interval_s + timeout_s)`; a service still `starting` at its bound counts as unhealthy. It waits at least 60 seconds in all, so a service without a healthcheck has had time to crash. It returns the final `docker compose ps --format json`, with each container's `RestartCount` from `docker inspect`, as `OPSMITH_OUTPUT_COMPOSE_PS`, and the logs, capped at the last 200 lines per service.
2. Facts: code marks a service failed when it has exited non-zero, is `restarting` or is `unhealthy`. This is a floor under the judgment below: a deploy with a failed service is never a success, whatever the model says. Restart counts are not part of the floor, because a service that crashed once while its database was starting and then came up is healthy. They go to the model, which can tell that apart from a crash loop that `ps` happened to catch between restarts.
3. Judgment: the model reads the facts, the logs and the deployed `docker-compose.yml` through the log-validation prompt, rewritten for this, and returns:

   ```python
   class ComposeValidation(BaseModel):
       is_successful: bool
       compose_at_fault: bool = False       # the failure is fixable in the compose file
       reason: Optional[str] = None         # what failed and why, when it failed
       docker_compose: Optional[str] = None # the whole repaired docker-compose.yml, when compose_at_fault
   ```

   It catches what the facts cannot, such as a container that is up but logs a refused connection on every request. When something failed it says whether the compose file caused it, which is the distinction `dockerfile validate` draws with `dockerfile_at_fault`.
4. Repair: when the compose file is at fault, the model's `docker_compose` overwrites `docker-compose.yml` in the working directory and the stack is deployed again, up to `max_docker_compose_gen_attempts` times, each attempt carrying the previous facts and judgment in the message history. The repair is reported as a notice that says what changed and, when the cause is a field of `deployments.yml` such as a healthcheck path, a port or an env value, names the field. Because it is written to the working directory, later releases deploy it too, until the next `update` renders from the config again and drops it. Until then `compose diff` shows it, since the file on the VM is no longer the rendered one. How a repair survives an `update`, and how a fix whose cause is in `deployments.yml` is applied there, is left to phase 3.
5. Failure: when the model finds the compose file not at fault, returns no repaired file, or the attempts run out, the command fails with `DEPLOY_UNHEALTHY` (exit 4). `details` carries `reason`, `compose_at_fault`, the ps table and the last 200 log lines of each failed service. That tells a person or a harness where to act: the application, the config or a Dockerfile when the compose file is not at fault, and what the compose file needed when it is. Before failing, a terminal user is offered `interact.edit("compose.edit", ...)` on `docker-compose.yml` and a redeploy; headless exits 4.
6. Init jobs: after a successful validation, run `first_deploy` jobs on `env create` and `every_release` jobs on `release` and `update`, using the `docker_compose_run` playbook, in config order. A failing job fails the command with `DEPLOY_UNHEALTHY`.

## Code changes by file

| File | Change |
|------|--------|
| `opsmith/deployment_strategies/monolithic.py` | the failure floor, the model's judgment and repair of the deployed compose file, and init-job steps, shared by every command that deploys |
| `opsmith/templates/docker_compose_deploy/*/main.yml` | the health wait, `compose ps` with restart counts |
| `opsmith/prompts.py` | rewrite the log-validation prompt to judge the facts and logs and return a repaired compose file |

## Acceptance criteria

1. A service with `healthcheck.http_path` that never becomes healthy fails with `DEPLOY_UNHEALTHY` and includes its logs in `details`, even when a scripted model calls the deploy successful (phase acceptance criterion 4).
2. An `every_release` init job runs on `release` and its failure fails the release (phase acceptance criterion 5).
3. When a scripted model finds the compose file at fault and returns a repaired one, that file is deployed and validated again, and the result carries a notice of the repair; the next `release` deploys the repaired file again, and the next `update` renders `docker-compose.yml` from the config (phase acceptance criterion 12).
4. When the model finds the compose file not at fault, the command fails with `DEPLOY_UNHEALTHY` carrying its `reason` and `compose_at_fault: false`, without redeploying (phase acceptance criterion 13).

## Tests

- `test_monolithic_validation.py`: the failure floor over a ps-table matrix, including a service still `starting` at its bound; with a scripted model, success, a repair of the deployed compose file, a failure not at fault, and attempts running out; init job ordering with fake provisioners.

## Harness surface

- `SKILL.md`: the hand-authoring section covers `init_jobs`, and the troubleshooting table gains `DEPLOY_UNHEALTHY`, with the command that follows it. It reads `compose_at_fault` first: false means the fix is in the application, the config or a Dockerfile, and true means the repairs ran out and `reason` says what the compose file needed.
- The skill must say that a repair made during a deploy lasts until the next `update`, and that when a repair's notice names a `deployments.yml` field, the fix belongs there. The generated compose file is never the place to edit, even though `release` deploys it.
