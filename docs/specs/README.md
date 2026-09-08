# Specs

Designs written to be built against. A spec says what changes, why, how it is structured, and what has to be true before it can be called done.

A spec is not a description of the code. It is written before the work, in the future tense, and it stops changing once the work ships — at that point it becomes the record of what was intended, and anything still true about the running system moves to [`reference/`](../reference/). Amending a shipped spec to match what was actually built defeats the point of having one.

## Naming

Every entry here starts with the date the spec was drafted, `YYYY-MM-DD`, so the directory sorts chronologically and a stale spec is obvious at a glance. The date does not change when the spec is revised.

- A spec that fits in one document is one dated file: `2026-09-04-phase-3-remote-state.md`.
- A spec large enough to be built in several merge units is a dated directory whose `README.md` is the index: `2026-09-04-phase-0-headless-core/`, with `0a-…md` through `0g-…md` inside. Files inside a dated directory are not dated again; they are ordered by the sequence they are built in.

## Shape

A unit of work is something a person can build and merge on its own, in a sitting or two. If a spec cannot be built without being read three times, split it into a dated directory.

Every spec carries the same header and closing sections, so they can be skimmed against each other:

```markdown
**Goal:** one sentence.
**Depends on:** what must land first.
**Size:** S | M | L | XL.
**Ships as:** the version, or the merge unit it belongs to.

## Scope            numbered, what is in
## Non-goals        what a reader would reasonably assume is in, and is not
## Design           the substance
## Code changes by file
## Acceptance criteria    checkable, not aspirational
## Tests
## Risks and open questions
```

Conventions shared by a set of specs — CLI contracts, error codes, naming — are defined once in the document that plans them and referenced, never restated. For the phases below that document is [`notes/2026-09-04-migration-plan.md`](../notes/2026-09-04-migration-plan.md).

## What is here now

Nine phases from 0.4.x to 1.1.0, planned together in the [migration plan](../notes/2026-09-04-migration-plan.md).

| Spec | Depends on | Ships as |
|------|------------|----------|
| [Phase 0 — headless core](2026-09-04-phase-0-headless-core/) (7 parts) | – | 0.5.0 |
| [Phase 1 — service model v2](2026-09-04-phase-1-service-model-v2.md) | 0 | 0.6.0 |
| [Phase 2 — customization layer](2026-09-04-phase-2-customization.md) | 0, 1 | 0.6.0 |
| [Phase 3 — remote state](2026-09-04-phase-3-remote-state.md) | 0 | 0.7.0 |
| [Phase 4 — recipes](2026-09-04-phase-4-recipes.md) | 1, 2, 3 | 0.8.0 |
| [Phase 5 — agentic repo analysis](2026-09-04-phase-5-agentic-analysis.md) | 0, 1 | 0.9.0 |
| [Phase 6 — harness integration](2026-09-04-phase-6-harness-integration.md) | 0, 1, 2 | 1.0.0 |
| [Phase 7 — MCP server](2026-09-04-phase-7-mcp-server.md) | 6 | 1.0.x |
| [Phase 8 — data durability](2026-09-04-phase-8-data-durability.md) | 1, 3 | 1.1.0 |
