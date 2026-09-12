# ADR-0020: Expert Plan v3 — Focused Panel with Two-Level Effort Ladder

**Status:** Accepted

## Context

The planning system has two existing implementations: `/expert-plan` (v1) and `/expert-plan-v2`. They span opposite trade-offs.

- **v1** costs $4.37 for a 92-line plan but allows indeterminate-status decisions to silently drift out of sync between steps (no consistency check). Everything runs in the main thread; sequential expertise accumulates large context.
- **v2** costs $13.39 at effort 4 and its alignment pass catches cross-domain risks (a probe assumed side-effect-free, an FFI timeout budget — real issues that help). But this costs 3x as much as v1, and fixes come as appended notes rather than being reconciled into the plan. v2 effort 2 (pod path) costs almost as much as v1 ($4.20) but uses 39% more total tokens because pods still load full shared context per pod and gain nothing from isolation.

Both have limitations for different use cases:
- **v1 limitation**: no consistency check, sequential bottleneck, drifting decisions
- **v2 limitation**: 5-level ladder with effort 1/2 overhead despite cheap model tier, router/digest/synthesis subagent chain, high cost for effort 4

There is a gap for: genuinely isolated per-expert contributions + a simpler pipeline + a consistency check, without the 3x cost.

## Decision

A third planning command, `/expert-plan-v3`, implements a leaner alternative:

### Architecture

- **Main thread**: Sonnet-pinned (at frontmatter level). The main thread does only orchestration work — gathering context, directly selecting a panel of 3 experts (no router subagent), building a decision index by reading contribution files once, copying the final plan. None of this benefits from Opus.
- **Judgment steps**: Each dispatched as a separate one-shot Opus subagent:
  - **Step 6 — Synthesize & Consistency check** (`plan-synthesize-and-check.md`): reads all contributions + decisions, writes the plan template, then verifies requirement/decision tracking and error-handling agreement in the same dispatch. The self-check explicitly documents itself as such — the receipt notes this is not independent audit.
  - **Step 7 — Audit** (`plan-audit.md`): effort 3 only (or effort-2 escalation), independently checks requirement fidelity, assumption validity, contradictions, verification adequacy. This is the independent second opinion.

This mirrors v2's existing precedent: v2 pins Router and Digest to Sonnet for dispatch/bookkeeping, reserving Opus specifically for Synthesis and Alignment. v3 extends this split — Sonnet for orchestration (the main thread itself, which the command's frontmatter fixes), Opus for judgment (Synthesize, Consistency-check, and optionally Audit).

**Why separate Opus dispatches?** A command's frontmatter `model:` pins the **entire invocation**, so getting cheap early steps and expensive late steps in one continuous session is not possible. The two heavy steps have to be their own dispatches. This is the same constraint v2 worked under.

### Effort Ladder

Two levels only (not five):
- **Effort 2 (default)**: 3 focused experts + Contrarian Carl + main-thread Consistency-check. No independent auditor. Cost target: cheaper than v1 while including a consistency check.
- **Effort 3**: Effort 2 baseline + independent Audit step. Cost target: better internal consistency than v1 at roughly v1's cost or cheaper than v2 effort 4.

**Why not 1, 4, or 5?** Effort 1 and 2 on the review side work because they have dedicated mechanical paths (swarm, pods) that are fundamentally different from the standard per-expert path. Planning doesn't have equivalent mechanical variants — all efforts run the same contributor pipeline. Effort 1 would mean 1 expert, which is not a panel. Effort 5 (everyone) is not scoped for the initial v3 implementation — evaluation deferred to a follow-up phase.

The 2-level ladder is simpler to test, reason about, and cost-model. If v3 proves cost-effective, a later phase can evaluate whether to add more levels.

### Effort 2 vs. Effort 3

- **Effort 2**: User makes calls at the Step 5 checkpoint; main-thread consistency check verifies the plan against requirements; no second opinion. Cost is low; decision burden stays on the user.
- **Effort 3**: Adds an independent auditor who re-reads contributions and plan, checking requirement fidelity, assumption validity, contradictions, and verification adequacy. Finds things the synthesis might have compressed or missed. Cost is higher but still cheaper than v2 effort 4.

If a material disagreement remains unresolved during v3 effort 2, the user can escalate to the same role prompt (plan-audit.md), scoped to a single concrete question, as a recorded exception — not silent scope creep into effort 3's behavior.

### Model Override: `--models opus`

By default (`--models balanced`), contributors run Sonnet unless individually flagged during Step 2 selection as needing Opus (novel architecture, security-critical, etc.). Carl, Synthesize, Consistency-check, and Auditor are always Opus.

The flag `--models opus` escalates every dispatched **subagent** (contributors, Carl, Synthesize, Consistency-check, Auditor) to Opus — a user's manual "this plan is too dangerous for Sonnet" lever.

**Critical note:** This flag does NOT escalate the main-thread orchestration shell itself (the command's frontmatter `model: sonnet`), which the Claude Code harness fixes before the command body runs. If the user wants the main shell on Opus too, they switch their own session's model before invoking the command. This is intentional — the orchestration work (context gathering, expert selection, decision indexing, plan copying) is cheap bookkeeping that never benefits from Opus.

### Collision-Resistant Session Paths

Fix for a real v2 bug (issue #122): two effort-4 runs on the same ticket could silently overwrite each other's final plan at `~/.claude/plans/{slug}.md`. v3 generates an invocation ID up front:

```
INVOCATION_ID="$(date +%Y%m%dT%H%M%S)-$(printf '%05d' $RANDOM)"
SESSION_DIR="$HOME/.claude/plan-sessions/${REPO_KEY}/${SLUG}-${INVOCATION_ID}"
FINAL_PLAN_PATH="$HOME/.claude/plans/${SLUG}-${INVOCATION_ID}.md"
```

Because `FINAL_PLAN_PATH` includes the invocation ID, two runs on the same ticket never collide.

### Reuse: No New Agent Needed

v3 uses `agents/expert-reviewer.md`'s existing hybrid dispatch mode (persona + contract file) for contributors and Carl, and role-prompt-only mode for Synthesize, Consistency-check, and Audit. No new agent is needed — the same `expert-reviewer` agent already handles all dispatch shapes.

## Consequences

### Positive

- **Leaner cost profile than v2**: No router or digest subagents. Synthesize and Consistency-check are Opus, but that is cheaper than v2's 5-subagent stack (router + contributors + digest + synthesis + alignment).
- **Consistency guarantee**: Unlike v1, every plan gets a main-thread consistency check (Step 7) verifying that requirements reach steps, decisions trace to implementation, and error handling is consistent.
- **Genuinely isolated contributions**: Unlike v2 effort 2 (pods), each contributor runs blind and isolated — no context sharing between contributors.
- **Simple effort ladder**: Two levels are easier to reason about, test, and cost-model than five.
- **Collision-resistant paths**: The invocation ID fixes the real v2 collision bug.

### Trade-offs

- **No router**: The main thread picks 3 experts directly. This is a judgment call, not a data-driven decision. For familiar domains, this is fine; for novel architectures, it might miss a relevant expert. Mitigation: v3 is opt-in; users can still use v1 or v2 for high-uncertainty plans.
- **No digest subagent**: The main thread reads contributions once and builds the decision index inline. This is less scalable than v2's digest subagent if the panel size grows beyond 3 experts. Mitigation: the 3-expert cap is intentional.
- **No alignment pass**: Only effort 3 (or escalations) get an independent auditor. Effort 2 users rely on main-thread consistency check + user judgment. This is a deliberate cost optimization; it's trade off for users comfortable with lower cost.

### Non-blocking Future Work

- **Phase 5 evaluation**: A matched quality/token comparison study (v1 vs. v2 effort 2 vs. v2 effort 4 vs. v3 effort 2 vs. v3 effort 3) is deferred to a separately-authorized follow-up, after v3 accumulates dogfooding data.
- **Effort expansion**: If v3 effort 2/3 prove cost-effective and users request more selectivity, a later phase can add effort 1 (1 expert, haiku) or effort 4 (full panel, ~6 experts). This is not ruled out; it's just deferred.
- **`--view summary`**: A summary presentation mode showing just decisions and high-level approach (skipping full contributions) is deliberately deferred as future work.

## References

- ADR-0018: Parallel planning with checkpoint-based isolation (v2 design, which v3 builds on)
- ADR-0004: Model cost routing (the Opus-for-judgment principle that v3 extends)
- `/expert-plan-v2`: The comprehensive reference implementation that v3 adapts (not replaces)
- `commands/expert-plan-v3.md`: The command specification
- `prompts/plan-synthesize-and-check.md`, `plan-audit.md`: Role prompts
