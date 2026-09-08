# Notes

Things written to think with: plans, investigations, measurements, comparisons of options, inventories taken before a refactor, findings that explain why something is the way it is.

A note records what was true when it was written, and its date is what makes it readable a year later. Most notes are not maintained — keep one while it explains a decision someone might otherwise reverse by accident, and delete it once the explanation has moved somewhere permanent. A planning note that a set of specs is still being built against is the exception: it stays current until the work it plans is done.

If a note turns into a design to build against, that design becomes a spec in [`specs/`](../specs/). If it turns into a description of how something works, it becomes a page in [`reference/`](../reference/).

## Naming

`YYYY-MM-DD-what-it-is-about.md`, dated the day it was written. Name it for the question, not the answer: `2026-09-04-why-repo-map-is-slow.md`, not `2026-09-04-repo-map-fix.md`. Open with the question in one line.

## What is here now

- [`2026-09-04-migration-plan.md`](2026-09-04-migration-plan.md) — the plan behind the nine phase specs: why a migration rather than three features, the target architecture, the phase order and dependency graph, the conventions every phase builds against (CLI contract, exit codes, events, the interaction key table, `.opsmith/` ownership, testing standard), and what is explicitly not being done.
