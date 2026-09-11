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
- **Effort ladder** (per ADR-0012) applies to both v1 and v2; v2's implementation covers efforts 1-5
  fully today, including swarm/pod modes for efforts 1-3.

## Amendment — Plan Mode guard and multi-checkpoint write restriction (2026-09-10)

Plan Mode, when active in a session, restricts the `Write` tool to a single designated plan file. This works well for v1 (`/expert-plan`), which runs sequentially in the main thread and produces exactly one artifact. v2, however, is fundamentally different: parallel subagents each write their own checkpoint file under `~/.claude/plan-sessions/` (the router writes `selected-experts.md`, each contributor writes `{expert}-contribution.md`, the digest writes `open-questions.md`, synthesis writes `plan.md`, and the alignment pass writes `{expert}-alignment.md`). If Plan Mode were left active when v2 runs, the first write action attempting to create its checkpoint file would hit the single-file restriction and fail, blocking the entire pipeline outright.

**Decision: Fix in place (option B).** v2's Step 0 guard explicitly exits Plan Mode before any subagent work begins (if the invoking session is already in Plan Mode). This guard is documented in detail in `commands/expert-plan-v2.md` Step 0's Plan Mode guard paragraph. The guard's failure-path behavior: if the user declines `ExitPlanMode`, stop cleanly; if `ExitPlanMode` errors after detecting Plan Mode, stop and ask the user to press Shift+Tab and re-run; if no Plan Mode signal is found, proceed without calling the tool.

v2 no longer enters or ends in Plan Mode. Step 6 (the hard-stop checkpoint with `AskUserQuestion`) is v2's human gate, serving the role that Plan Mode serves for v1. The orchestrator's writes are confined to `plan-sessions/` and `~/.claude/plans/` — no code changes at any point. Subagent write-scope enforcement is documented as a prompt convention (residual risk, not a tool-enforced guarantee) in CLAUDE.md's "Panel agents are capability-restricted, not dialog-gated" section.

The guard belongs here (to v2's architecture decision) rather than to a cross-cutting session-management ADR because it is specific to the tension between v2's multi-file checkpoint pipeline and Plan Mode's single-file constraint.
