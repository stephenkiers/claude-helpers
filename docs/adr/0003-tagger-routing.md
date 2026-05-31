# ADR-0003: Tagger-based reviewer routing

**Status:** Accepted

## Context

Most diffs are relevant to only a few reviewers. Running every persona against every section wastes
tokens and dilutes results (see [ADR-0001](0001-progressive-disclosure.md)). We need a cheap way to
decide *which* reviewers see *which* parts of a diff.

## Decision

A dedicated **tagger** step (a Haiku subagent, prompt in `prompts/tagger.md`) runs after the
summarizer and before the reviewers. It maps each section of the diff to the reviewers whose
`triggers` in `reviewers/index.yaml` match, and writes the routing to `tagged-sections.md`.

Reviewers then receive only their tagged sections. Two deliberate exceptions bypass routing and always
receive the full diff because their value is cross-cutting:

- **Sam System** — traces composition/data-flow across files.
- **Code Rot Cody** — greps the whole repo for orphaned/uncalled symbols.

`--all` overrides routing to force every reviewer; `--full` reviews the whole codebase, not just the
diff.

## Consequences

- **Good:** Cheap routing (Haiku) gates expensive expert passes (Opus). Triggers live in one
  lightweight index, easy to tune.
- **Cost:** Routing is heuristic. Broad triggers over-include (safe, wasteful); narrow triggers
  under-include (cheap, risky). Keep triggers generous and rely on Pass 2 to drop noise.
- **Constraint on contributors:** New personas must declare `triggers` in `index.yaml` or they will
  never be routed any sections.
