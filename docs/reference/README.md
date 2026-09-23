# Reference

How opsmith works today. Architecture, the plugin contracts for cloud providers and deployment strategies, the layout of `.opsmith/`, operational procedure.

Reference is written in the present tense and is maintained: when the behaviour changes, the page changes in the same commit. A reference page that has drifted is worse than no page, because it is trusted. If you cannot keep a page current, make it a dated note in [`notes/`](../notes/) instead.

The boundary with [`specs/`](../specs/) is tense, not subject. A spec describes what will be built and is frozen once it ships; reference describes what exists. The same subject usually appears in both, at different times.

## What is not here

- User-facing installation and usage: the repository [`README.md`](../../README.md).
- Architecture, engineering conventions and project layout: [`CLAUDE.md`](../../CLAUDE.md), read from the repository root by the coding agents that work here.

## What is here now

- [`2026-09-21-agent-skill.md`](2026-09-21-agent-skill.md) — the Agent Skill: what ships, where it
  installs, what is generated from the code and what has to be written by hand.

The plugin contracts will most likely be next, once the migration's phase 3 has finished changing
them.
