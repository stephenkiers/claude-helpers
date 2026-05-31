# ADR-0001: Progressive disclosure of experts

**Status:** Accepted

## Context

There are 27 reviewer personas. Each persona is a rich prompt (character, principles, anti-patterns,
review checklist, output format). Loading all of them, fully, for every review would:

- Burn an enormous amount of context on personas irrelevant to the diff (a CSS change does not need
  the concurrency expert or the event-driven-architecture expert).
- Make the main review thread slow and expensive.
- Bury the signal — most personas would have nothing to say and still consume tokens.

The system should pay for an expert's full attention only when the change actually warrants it.

## Decision

Load experts progressively — lazily and in layers — rather than all-at-once:

1. **Discovery, not preloading.** Reviewers are discovered from `reviewers/` and described by the
   lightweight `reviewers/index.yaml` (name, triggers, useWhen). The full persona file is read only
   when that persona is actually going to run.
2. **Routing before depth.** A cheap **tagger** pass (see [ADR-0003](0003-tagger-routing.md)) maps
   diff sections to the handful of relevant reviewers. Personas with no matching sections are skipped
   entirely — their full prompt is never loaded.
3. **Each expert loads its own context on demand.** When a reviewer runs, it reads only the
   project-local context relevant to it (e.g. Contract Chris reads `docStyle`, Fragile Feynman reads
   `fragility.*`, North Star Nick reads the ADR index). See
   [ADR-0005](0005-three-layer-context-cascade.md).
4. **Offload mechanical work to cheap background subagents.** Summarizing, tagging, consistency
   checking, dead-code scanning, and per-reviewer Q&A run as separate Haiku subagents (the `Task`
   tool), keeping the expensive main thread focused on expert judgment. See
   [ADR-0004](0004-model-cost-routing.md).

The direction of travel is *more* of this: push as much expert work as possible into on-demand
background workers, and keep the foreground thread thin.

## Consequences

- **Good:** Reviews scale to many personas without scaling cost linearly. Unused experts cost nothing.
  Artifacts are checkpointed per step so a failed subagent doesn't lose the others' work.
- **Cost:** Routing can mis-route — a section tagged for the wrong reviewer, or a relevant reviewer not
  triggered. Mitigated by `--all` (force every reviewer) and by keeping `index.yaml` triggers broad.
- **Constraint on contributors:** A new persona must register lightweight triggers in `index.yaml`, and
  must read its own context lazily rather than assuming everything is preloaded.
