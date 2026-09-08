# Opsmith documentation

Contributor-facing documentation. The user-facing README is at the repository root, and the engineering conventions are in [`CONVENTIONS.md`](../CONVENTIONS.md).

Top-level directories are kinds of document, not projects. Inside them, every entry starts with the date it was written, `YYYY-MM-DD`, whether it is a file or a directory.

| Directory | Holds | Written when | Lifetime |
|-----------|-------|--------------|----------|
| [`specs/`](specs/) | designs meant to be built against: what changes, why, and how you know it works | before the work | kept after the work as the record of what was intended |
| [`notes/`](notes/) | plans, investigations, anything written to think with | while figuring something out | kept while it explains a decision; deleted once it does not |
| [`reference/`](reference/) | how the project works today: architecture, plugin contracts, operational procedure | after the behaviour is real | maintained; wrong reference is worse than none |

The distinction that matters is tense. A spec is written in the future tense and stops changing once it ships. Reference is written in the present tense and changes whenever the code does. If a document is describing behaviour that already exists, it belongs in `reference/`, even if it started life as a spec.

## What is here now

- [`notes/2026-09-04-migration-plan.md`](notes/2026-09-04-migration-plan.md) — the plan to reach 1.0: recipes, harness integration and agentic repo analysis, plus the foundation work all three depend on. Start here.
- [`specs/`](specs/) — the nine phase specs that plan describes, phase 0 split into seven parts.
