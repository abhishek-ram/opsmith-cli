# Phase 2h: Image sources and the deploy flow

**Goal:** a config whose services all run prebuilt images deploys without a registry or a build, and the DNS wait finishes the change phase 0 started.
**Depends on:** 2d, 2g.
**Size:** S.
**Lands as:** the last part of phase 2; the phase ships as 1.0.0 with phase 3 once it lands.

## Scope

1. The registry and builds only when something is buildable.
2. Image references for image sources.
3. What is left of the DNS wait.

## Non-goals

- Recipes (phase 5), which are the main users of image sources.

## Design

### Building only what is buildable

- `buildable = [s for s in services if s.source.kind == "build" and s.service_type != STATIC_SITE]`. Registry setup and `_build_and_push_images` run only when `buildable` is non-empty. Image sources populate `images[slug] = f"{image}:{tag}"`.
- With no registry, the deploy playbook skips its registry login, and the deploy stops requiring `require_registry_url()`.
- The VM architecture follows the image-platform rule in [2g](2g-capacity-planning.md), which reads each image source's `platforms`.

### DNS

Phase 0 already waits for each record with `interact.wait_for` and a resolver check, `dns_record_is_published`. What is left of this phase's change:

- The check queries the zone's authoritative nameservers first.
- The fixed fifteen-second sleep after the certificate records (`monolithic.py:975`) goes.
- Certificate validation on AWS is the only step that blocks on the records; the Traefik and GCP records are advisory, so the run continues and reports them.

Allocating the static IP before the VM so the records are known early is an optional internal improvement, not part of the strategy contract.

## Code changes by file

| File | Change |
|------|--------|
| `opsmith/deployment_strategies/base.py` | buildable filter; images for image sources |
| `opsmith/deployment_strategies/monolithic.py` | the registry and builds skipped when nothing is buildable; the DNS changes |
| `opsmith/templates/docker_compose_deploy/*/main.yml` | registry login skipped without a registry |
| `opsmith/utils.py` | authoritative nameservers first in `dns_record_is_published` |
| `README.md` | an image-source example beside the build-source one |

## Acceptance criteria

1. A config with a single image source and no build sources runs `env create` without a registry or a build (phase acceptance criterion 3).
2. A Traefik record that does not resolve yet is reported, and the run goes on.

## Tests

- An image-only `env create` against the fake provisioners creates no registry and runs no build playbook.
- The DNS check asks the authoritative nameservers first, and the advisory records are reported without a wait.

## Harness surface

- `SKILL.md`: the hand-authoring section covers image sources.
