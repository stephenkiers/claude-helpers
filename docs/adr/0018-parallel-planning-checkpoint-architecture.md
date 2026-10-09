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
- **Known limitation (issue #122):** v2's final plan path can collide across concurrent runs; fixed
  in v3 — see ADR-0020's "Collision-Resistant Session Paths" section for the mechanism and fix.

## Amendment — Plan Mode guard and multi-checkpoint write restriction (2026-09-10)

Plan Mode, when active in a session, restricts the `Write` tool to a single designated plan file. This works well for v1 (`/expert-plan`), which runs sequentially in the main thread and produces exactly one artifact. v2, however, is fundamentally different: parallel subagents each write their own checkpoint file under `~/.claude/plan-sessions/` (the router writes `selected-experts.md`, each contributor writes `{expert}-contribution.md`, the digest writes `open-questions.md`, synthesis writes `plan.md`, and the alignment pass writes `{expert}-alignment.md`). If Plan Mode were left active when v2 runs, the first write action attempting to create its checkpoint file would hit the single-file restriction and fail, blocking the entire pipeline outright.

**Decision: Fix in place (option B).** v2's Step 0 guard explicitly exits Plan Mode before any subagent work begins (if the invoking session is already in Plan Mode). This guard is documented in detail in `commands/expert-plan-v2.md` Step 0's Plan Mode guard paragraph. The guard's failure-path behavior: if the user declines `ExitPlanMode`, stop cleanly; if `ExitPlanMode` errors after detecting Plan Mode, stop and ask the user to press Shift+Tab and re-run; if no Plan Mode signal is found, proceed without calling the tool.

v2 no longer enters or ends in Plan Mode. Step 6 (the hard-stop checkpoint with `AskUserQuestion`) is v2's human gate, serving the role that Plan Mode serves for v1. The orchestrator's writes are confined to `plan-sessions/` and `~/.claude/plans/` — no code changes at any point. Subagent write-scope enforcement is documented as a prompt convention (residual risk, not a tool-enforced guarantee) in CLAUDE.md's "Panel agents are capability-restricted, not dialog-gated" section.

The guard belongs here (to v2's architecture decision) rather than to a cross-cutting session-management ADR because it is specific to the tension between v2's multi-file checkpoint pipeline and Plan Mode's single-file constraint.

## Amendment — Harness-agnostic barrier waiting (2026-09-23)

**Prior assumption (removed):** Join barriers were documented as if subagents always returned their results in the launching turn — "they have all returned by the time you continue." This assumption held only for older harness versions returning results inline. Observed transcripts (2026-09) show every Agent call returns a launch acknowledgement, with the final report arriving separately: either a hand-back message or a task notification in a later turn. The false premise created confusion about what "waiting" meant.

**Decision: End-turn protocol.** Waiting means ending the turn and re-evaluating per-id state on each return or notification. The receipt (the id's final report) is the first message carrying it — a synchronous result containing the report, an `<agent-message from="{id}">` hand-back, or a task notification whose `<result>` contains the report. A synchronous result that only says the agent was launched ("Async agent launched", "working in the background") is a launch acknowledgement, not a return.

Per-id state progresses: `launched → returned → ok | bad`. The barrier closes when every id is `ok` or stood-in. At most one 1800s status-only `ScheduleWakeup` per phase (one dispatch batch) is permitted as a fallback; it tells the user which ids are missing, never retries or writes stand-ins.

The canonical text lives in `prompts/join-barrier-pattern.md` § "Waiting for the barrier". All commands referencing join barriers now point at that section via a fixed pointer sentence, replacing earlier contradictory inline wording.

Observed in sampled transcripts (2026-09), not a harness guarantee: calls return a launch ack regardless of `run_in_background` flag value, a notification follows for nearly all launched agents, and the flag made no observable waiting difference. The receipt/file/sentinel contract is unchanged.

## Amendment — Third sanctioned write prefix for /expert-spike (2026-10-09)

This section is the single full statement of the rule. It is the reciprocal of ADR-0022 §4 and §13, which introduced the prefix for `/expert-spike`.

**Decision: a third write prefix, scoped to one file per spike role.** Subagents may write to `${PROJECT_ROOT}/spikes/<spike-dir>/`, and only to the single file the orchestrator names in that directory. It applies only to `expert-reviewer` spike roles (contribution, assessment, audit). It is not a blanket `spikes/` grant.

- **`spike-researcher` is excluded.** It has no `Write` tool. It returns its research body, and the orchestrator writes that body to `research/<id>.md` (ADR-0022, 2026-10-09 amendment).
- **`Write` stays prompt-scoped, not tool-scoped.** The residual risk is unchanged (ADR-0022 §4). A hook does not close it, and the sanctioned-prefix list is a convention that the subagent is instructed to follow, as it is for the two existing prefixes (CLAUDE.md, "Panel agents are capability-restricted, not dialog-gated").
- **The PreToolUse hook is optional, per-machine, and a guard rather than a boundary.** The example in `agents/expert-reviewer.md` uses `<PROJECT_ROOT>` as a literal placeholder that the user replaces with an absolute path. The example uses raw `startswith` with no normalization, so it is a shape to adapt, not hardened code. A hardened matcher should resolve the path to an absolute form and compare against the prefix with a trailing `/`, so that `spikes-evil/` and `../` escapes fail.

**Failure modes (documented behavior):**

- **No hook configured:** nothing enforces the `spikes/` prefix. The rule is prompt-level only.
- **Hook configured without the `spikes/` prefix:** expert writes into the spike directory are rejected. The missing artifact leaves that stage as the manifest's resume point, because VERIFY fails (`commands/expert-spike.md`, Verify block). The spike stops visibly at that stage instead of continuing with a gap.

See ADR-0022 §4 (write prefixes and residual risk) and §13 (relationship to this ADR).
