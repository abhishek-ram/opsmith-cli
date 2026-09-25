# Phase 2g: Capacity planning

**Goal:** the machine an environment runs on comes from the model's estimate of what the workload needs and the strategy's plan for meeting it, replacing the model's pick from the whole instance catalog.
**Depends on:** 2c.
**Size:** L.
**Lands as:** a part of phase 2, buildable alongside 2d, 2e and 2f; the phase ships as 1.0.0 with phase 3 once 2h lands.

## Scope

1. `WorkloadProfile` on the environment, and its questions.
2. The capacity estimate, a model step.
3. `plan_capacity`, a strategy hook, with the monolithic plan and its architecture rule.
4. Re-planning on `update`, and `env resize`.
5. Replicas rendered as `deploy.replicas`.

## Non-goals

- A multi-node strategy. The hook is shaped for one, but only monolithic implements it.

## Design

### Capacity planning

How many machines an environment needs, and of what kind, is a strategy decision. The monolithic strategy needs exactly one VM. A Kubernetes strategy would need node pools with counts and sizes. A managed-container strategy would need per-service CPU and memory settings and no machines at all. The core therefore owns the inputs and the model's estimate, and the strategy owns the plan.

**Inputs.**

- `ServiceInfo.resources`: what one instance needs at light load. Declared by recipes or detection, or estimated by the model below and written into the config after confirmation.
- `InfraProviderSpec` defaults per infra instance, plus `settings`.
- `DeploymentEnvironment.workload`: what the environment is expected to serve. Per environment, because staging and production differ.

```python
class WorkloadProfile(BaseModel):
    description: Optional[str] = None                  # the user's own words, kept for re-planning
    tier: Literal["hobby", "internal", "production"] = "hobby"
    peak_concurrent_users: Optional[int] = None
    peak_requests_per_second: Optional[float] = None
    availability: Literal["single", "high"] = "single"
    data_size_gb: Optional[float] = None               # expected stored data over the first year
```

`env create` prompts `env.workload.tier` and `env.workload.description` (flags `--workload-tier`, `--workload`); the numeric fields are optional prompts `env.workload.<field>` whose defaults come from the tier. `update` re-prompts only when asked with `--workload`.

**Estimate, a model step.** Turning a workload into replica counts and per-replica sizes depends on the language, framework, service type and dependencies, which is judgment:

```python
class ServiceCapacity(BaseModel):
    slug: str
    replicas: int                                       # recommended for this workload
    cpu: float                                          # per replica
    ram_gb: float                                       # per replica
    storage_gb: Optional[float] = None
    bound_by: Literal["cpu", "memory", "io"]
    rationale: str

class InfraCapacity(BaseModel):
    instance: str
    cpu: float
    ram_gb: float
    storage_gb: float
    rationale: str

class CapacityEstimate(BaseModel):
    services: List[ServiceCapacity]
    infra: List[InfraCapacity]
    baseline_resources: Dict[str, Resources]            # for services that had none; written to config after confirmation
```

`opsmith/core/capacity.py::estimate_capacity(config, environment, agent) -> CapacityEstimate` runs the `capacity_estimate` prompt once with the services, infra instances and workload profile as input. It never sees machine types; it reasons about the application only, which is what keeps it strategy-neutral. It replaces the machine-list prompt, whose input was the whole instance catalog.

**Plan, a strategy step.**

```python
class MachinePlan(BaseModel):
    role: str                                           # "app" for monolithic; pool names for Kubernetes
    instance_type: str
    architecture: CpuArchitectureEnum
    count: int

class CapacityPlan(BaseModel):
    services: Dict[str, ServiceCapacity]                # as adjusted by the strategy, e.g. replicas clamped
    infra: Dict[str, InfraCapacity]
    machines: List[MachinePlan]
    warnings: List[str]
    unsupported: List[str]                              # requirements the strategy cannot meet
    alternatives: List[MachinePlan] = []

class BaseDeploymentStrategy:
    @abc.abstractmethod
    def plan_capacity(self, config, environment, estimate: CapacityEstimate,
                      machine_types: MachineTypeList) -> CapacityPlan: ...
```

The plan is shown with its rationale and confirmed with `env.capacity.confirm` (`--answer env.capacity.confirm=true` headless); for monolithic, `--instance-type` overrides the machine choice. It is stored in `state.yml` as `capacity_plan` together with the workload profile it was derived from.

Monolithic implementation of `plan_capacity`:

1. Clamp `replicas` to 1 for services with volumes. Other services keep the estimate's replicas, rendered as compose `deploy.replicas`, which Traefik load-balances across.
2. RAM = 0.5 (OS and docker) + Σ services replicas × ram_gb + Σ infra ram_gb, times 1.3 headroom. CPU likewise, rounded up.
3. Architecture from the image-platform rule below.
4. The smallest machine type by RAM then CPU that satisfies both; the next two larger are `alternatives`.
5. `availability: high` goes into `unsupported` with the advice to choose a multi-node strategy when one exists. A plan that fits no available machine type fails with `CAPACITY_UNSATISFIABLE` before any infrastructure is created.

The image-platform rule: build sources are built for both platforms already, and the VM architecture is arm64 only if every image source lists `linux/arm64`.

A Kubernetes strategy would implement the same hook differently: a system pool with a fixed overhead, application pools bin-packed from replicas × per-replica requests with headroom, at least three nodes per pool for `availability: high`, and one `MachinePlan` per pool. Neither the config nor the estimate changes.

**Re-planning.** `update` re-runs the estimate when services, infra instances or the workload profile changed, and shows the difference against the stored plan. For monolithic, a changed instance type is applied only through `env resize --env NAME [--instance-type T]`, a Terraform apply of the new type with a stop and start of the VM, never implicitly.

### Where it runs

`_select_virtual_machine_type` is replaced by the capacity planning step above, run before the registry and the builds since it depends only on the config and the workload.

`CAPACITY_UNSATISFIABLE` is exit 2, as the migration plan's exit code table already lists; `opsmith/core/errors.py` leaves it to this part. Existing environments are covered by compatibility guarantee 7 in the [README](README.md#compatibility-with-existing-environments).

## Code changes by file

| File | Change |
|------|--------|
| `opsmith/types.py` | `WorkloadProfile` on the environment; `capacity_plan` in `MonolithicDeploymentState` |
| `opsmith/core/capacity.py` | estimate models and the `capacity_estimate` model step |
| `opsmith/core/errors.py` | the `CAPACITY_UNSATISFIABLE` error and its exit code |
| `opsmith/deployment_strategies/base.py` | `plan_capacity` abstract hook |
| `opsmith/deployment_strategies/monolithic.py` | implement `plan_capacity` and `resize`; remove the machine-list step |
| `opsmith/deployment_strategies/compose_renderer.py` | `deploy.replicas` from the capacity plan |
| `opsmith/prompts.py` | replace the machine-list prompt with the capacity-estimate prompt |
| `opsmith/templates/virtual_machine/gcp/main.tf` | `allow_stopping_for_update` so a machine type change can be applied |
| `opsmith/cli/commands/`, `opsmith/cli/flags.py` | `env create` gains the `--instance-type` semantics above, `--workload-tier` and `--workload`; `env resize` |
| `opsmith/tests/test_flag_mapping.py` | `--workload-tier` and `--workload` leave the list of flags that belong to a later phase |

## Acceptance criteria

1. `env create` for a config with one `WEB_SERVICE` build service, one worker, postgres and redis makes exactly two model calls between detection and a healthy result, the capacity estimate and the post-deploy judgment, and renders a compose file identical to the golden file (phase acceptance criterion 2, once 2f has landed).
2. A service without `resources` gets its baseline from the capacity estimate, written to the config after confirmation and not requested again; the estimate runs once per `env create` and on `update` only when services, infra instances or the workload changed (phase acceptance criterion 9).
3. A workload with `availability: high` produces a plan whose `unsupported` list names it, and `env create` proceeds only after the user confirms the plan; a plan that fits no machine type fails with `CAPACITY_UNSATISFIABLE` before any infrastructure is created (phase acceptance criterion 10).
4. A stub strategy implementing `plan_capacity` with two node pools receives the same `CapacityEstimate` as the monolithic strategy for the same config and workload (phase acceptance criterion 11).

## Tests

- `test_capacity.py`: estimate prompt inputs with a scripted model; monolithic plan arithmetic, replica clamping, architecture rule, alternatives, `unsupported` and `CAPACITY_UNSATISFIABLE`; re-plan triggers on `update`; a stub multi-pool strategy against the same estimate.

## Harness surface

- `references/commands.md` regenerates for `env resize` and for `env plan`'s extended result.
- `SKILL.md`: the hand-authoring section covers `resources`, and the troubleshooting table gains `CAPACITY_UNSATISFIABLE`, with the command that follows it.
