<!-- Hand-written. A later release generates this from the ownership manifest in the code; until
     then it is maintained by hand and is the rule for what may be edited. -->

# What is yours, and what is Opsmith's

Everything Opsmith writes into a repository goes under `.opsmith/`, and every path there belongs
to exactly one of three categories.

## Owned — people and agents edit these

| Path | What it is |
|---|---|
| `.opsmith/deployments.yml` | The configuration: services, dependencies, environments. |
| `.opsmith/docker/<service>/Dockerfile` | One per service that is built into an image. Generated once, then yours. |

Edit these freely, and validate afterwards:

```shell
opsmith --output json config validate
opsmith --output json dockerfile validate --service <slug>
```

Both are committed to the repository. They are the whole of what a person wrote.

## Generated — Opsmith rebuilds these on every run

| Path | What it is |
|---|---|
| `.opsmith/environments/<env>/<module>/` | Terraform and Ansible working directories, one per module. |
| `.opsmith/environments/<env>/<module>/*.tf`, `*.yml`, `ansible.cfg` | Copied from the package on every run. |

**Do not edit anything here.** The next run overwrites it, so an edit is not so much forbidden as
futile. There is no override mechanism in this release; one arrives with the customization layer.

## State — Opsmith's alone, never edited by hand

| Path | What it is |
|---|---|
| `.opsmith/environments/<env>/state.yml` | What the environment is: its machine, its registry, its images. Its existence is what says the environment has been deployed. |
| `.opsmith/environments/<env>/**/terraform.tfstate*` | Terraform's own record of the infrastructure. |

Editing either of these makes Opsmith's picture of the world disagree with the cloud's, and there
is no command that reconciles them. `opsmith env status --env <name>` reads `state.yml` and
reports it; that is the supported way to see what is in it.

## Outside the repository entirely

None of this is authored, and some of it must never be committed by any means, so it does not live
in the tree at all. It is under `~/.opsmith/projects/<name>-<digest>/environments/<env>/`:

| File | What it is |
|---|---|
| `answers.yml` | Every non-secret answer this environment has given, so a re-run never asks twice. |
| `secrets.yml` | The secrets it needs until the machine itself holds them. |
| `steps.yml` | Which steps of a run have finished, so a resumed run does not redo them. |

You can read `answers.yml` to see what an environment was told, and pass a copy back with
`--answers`. Do not copy any of it into the repository, and do not write `secrets.yml` anywhere a
build context or an archive could pick it up.

## The rule, in one line

If Opsmith wrote it under `environments/`, leave it alone. If a person wrote it, it is
`deployments.yml` or a Dockerfile, and you may edit it — then validate.
