# ADR-0005: Three-layer context cascade

**Status:** Accepted

## Context

The same personas need to work across wildly different projects — a Rust audio engine, a TypeScript
web app, a Python data pipeline. A persona hard-coded to one project's conventions is not reusable; a
persona with no project knowledge gives generic, low-value feedback. We need personas that are generic
by default but sharpen themselves with project-specific knowledge when it exists.

## Decision

Context cascades through three layers, each optional and each overriding the one above:

1. **Global persona** — `reviewers/{name}.yaml` in this repo (symlinked into `~/.claude/`). The
   reusable character and checklist. Knows nothing about your project.
2. **Project context** — `.claude/project.yaml` in the project being reviewed (`techStack`, `adrs`,
   `invariants`, `redLines`, `terminology`, plus per-reviewer fields like `fragility.*`, `docStyle`).
   See `prompts/project.yaml.template` and the `project-example-*.yaml` files.
3. **Per-reviewer local override** — `.claude/reviewers/{name}-local.yaml` in the project, for
   project-specific extensions to a single persona (e.g. `north-star-nick-local.yaml` pointing at the
   project's ADR index; `mozart-local.yaml` for project-specific event patterns).

Each reviewer reads only the layers relevant to it, on demand (see [ADR-0001](0001-progressive-disclosure.md)).

## Consequences

- **Good:** One set of personas works everywhere; projects add precision without forking the personas.
  Cascade overrides also let a project replace any command/reviewer wholesale by shadowing it in its
  own `.claude/`.
- **Cost:** Three places a fact could live. Convention: generic truth in the persona, project truth in
  `project.yaml`, single-persona project truth in the local override.
- **Dogfooding:** This repo wires its own `.claude/reviewers/north-star-nick-local.yaml` →
  `docs/adr/` so North Star Nick reviews changes here against these very ADRs.
