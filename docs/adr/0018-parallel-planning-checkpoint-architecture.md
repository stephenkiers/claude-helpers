# ADR-0018: Parallel planning with checkpoint-based isolation

**Status:** Accepted

## Context

`/expert-plan` (v1) runs experts sequentially in the main thread, accumulating their contributions
to the orchestrator's context. With 5-7 experts per planning session, context balloons quickly —
the user waits 5+ minutes watching an ever-growing conversation before seeing a plan.

Two improvements are desirable but conflict under v1's architecture:

1. **Parallelism** — spawn multiple experts at once, halving wall-clock time
2. **Isolation** — each expert blind to others' input, so reasoning stays independent

Both require subagents, and subagents writing to the orchestrator's context defeats the goal
(context doubles: once in the tool result, again in the completion notification).

## Decision

Create `/expert-plan-v2` as a parallel-only architecture with checkpoint-based communication:

- Each expert runs as a **separate subagent** (isolated from every other)
- Subagents write to **checkpoint files** in `~/.claude/plan-sessions/{slug}/`
- The orchestrator reads only **file paths**, never pasted content
- All subagents return **one-line receipts**, never full reports
- The checkpoint format is specified by `plan-contribution-contract.md` — a parallel input to persona YAMLs for planning roles

**A/B deployment:** Both v1 and v2 coexist. Users choose by preference. v1 remains the default
(`/expert-plan`); v2 is opt-in (`/expert-plan-v2`). No deprecation or retirement of v1.

## Consequences

- **Faster planning** (v2) for teams that can tolerate seeing experts' independent perspectives
  before synthesis (some projects prefer v1's sequential consolidation for single-lens reasoning).
- **Architecture mirrors code review** — `/expert-review`'s subagent blind-first panel
  (`expert-review-panel.md`) is the template; planning borrows its join-barrier pattern and
  "the file is the contract" discipline (documented in `agents/expert-reviewer.md`).
- **Effort ladder** (per ADR-0012) applies to both v1 and v2; v2's implementation covers efforts 4-5
  fully today, with efforts 1-3 (swarm/pod modes) planned as future work.
