# ADR-0003: Tagger-based reviewer routing

**Status:** Accepted

## Context

Most diffs are relevant to only a few reviewers. Running every persona against every section wastes
tokens and dilutes results (see [ADR-0001](0001-progressive-disclosure.md)). We need a cheap way to
decide *which* reviewers see *which* parts of a diff.

## Decision (Revised)

**Superseded by a single judgment router (ADR-0003.2 below).** The original tagger + confirm-gate
structure has been replaced with a single **Router** subagent (prompt in `prompts/router.md`) that
runs after the summarizer and before the reviewers. The router is a **judgment call**, not mechanical
keyword matching: it decides which reviewers would find something worth fixing in this diff, given
the code, the summary, and each reviewer's declared interests.

Reviewers then receive only their tagged sections. Four deliberate exceptions bypass routing and always
receive the full diff because their value is cross-cutting:

- **Sam System** — traces composition/data-flow across files.
- **Code Rot Cody** — greps the whole repo for orphaned/uncalled symbols.
- **Consistency Checker** — patterns across the whole diff.
- **Contrarian Carl** — runs last, sees all findings to find what was missed.

(Note: see the 2026-09-17 amendment below for the current always-run set and Sam System's routing status.)

`--all` overrides routing to force every reviewer. Naming reviewers explicitly
(`/expert-review rachel,security-sage`) *is* the routing decision
for judgment reviewers: it bypasses the router and skips its judgment call entirely. The four
always-run reviewers above still participate — their domain is the whole diff by definition,
independent of routing, so naming reviewers narrows the judgment panel, not the always-run set.

## Amendment (Superseded)

The original routing was keyword-based ("tagger") with a haiku confirm-gate for un-routed reviewers
as a safety net. This structure collapsed under high-trigger-mismatch conditions: measured on a
typical run, ~74% of reviewers fell through the tagger to the gate, which then escalated ~14 of 19
(74%), meaning the gate was the actual routing layer, not the tagger. Gate agents re-read the full
diff doing pure routing (not review), which was expensive and added little value over a single
judgment call at routing time.

## Amendment — judgment router (ADR-0003.2)

The confirm-gate is removed. A single **judgment router** (pinned to Sonnet via an explicit
`model: "sonnet"` parameter when the command spawns the router subagent, not the panel model —
narrow judgment, not deep expertise) runs once after the summarizer:

```
Reads: full diff, summary + business context, reviewers/index.yaml (ONLY)
Outputs: tagged-sections.md with a Panel Decision table (included/excluded, rationale each)
         and line ranges for every included reviewer
```

The router does not mechanically match keywords. Instead:
- It reads the diff and summary to understand what changed and why
- For each reviewer, it checks their `useWhen` and `triggers` as *signals of interest*, not rules
- It asks: "Would a domain expert, reading this diff, think this touches their work?"
- Threshold: inclusion leans toward "yes" when uncertain (missing a reviewer costs more than
  including an unneeded one)

**Why judgment?** Keyword-tagger runs on earlier branches showed 67% of reviewers unrouted, then
74% escalated by the gate — the system discovered that keyword-matching was not calibrated to the
repo's diffs. A judgment call, made once at routing time with full context, is simpler and cheaper
than routing-then-gate-then-escalation.

**Why Sonnet?** Routing requires understanding English prose (the diff, the summary) and applying
judgment, so it is not mechanical work (Haiku tier). But it is also not deep expertise (panel model).
Sonnet is the middle tier: capable of judgment, economical enough for every review.

Amended 2026-09-17 (#193): the always-run set is three — Code Rot Cody, Consistency Checker, and Contrarian Carl. Sam System is not always-run; since #148 he is gated by the deterministic diff-shape precondition in `prompts/expert-review-panel.md` and runs only when routed in, still receiving the full diff on those runs — superseding the four-reviewer list in `Decision (Revised)` above.

## Consequences

- **Good:** Single routing decision per review, made with full context (diff + summary + business).
  Sonnet at $0.003/$0.015 per 1M tokens is 3× cheaper than Opus but fully capable of judgment. No
  separate gate → no gate-collapse phenomenon → predictable cost and better routing accuracy.
- **Cost:** Judgment is harder to predict or tune than keyword matching. If the router consistently
  includes reviewers who find nothing, tuning means adjusting `useWhen` guidance in `index.yaml` or
  reframing the router prompt — but the data (`/review-stats` on router routing accuracy) is available
  to drive the tuning.
- **Constraint on contributors:** New personas must declare `triggers` and `useWhen` in `index.yaml`,
  which serve as signals the router consults. A persona with no triggers is never routed — the router
  makes judgment calls, so it is possible to exclude a reviewer even if they have triggers, but it is
  harder for reviewers to opt in without declaring their domain.

## Amendment (ADR-0003.2.1) — Structural Pre-Router Exclusion

### Context

The router selects reviewers based on judgment of the code, summary, and declared interests (`triggers`,
`useWhen`). For most reviewers, judgment is the right mechanism — their domain is broad enough that
diff shape cannot rule them out. Sam System is a measured exception: he reads the *full* diff
regardless of relevance, which is costly when the diff touches only a small scope. Analysis showed the
router still includes him on ~94% of runs despite only ~10% of diffs meeting his cross-file-composition
domain — the threshold is simply too high when tuning through `useWhen` alone.

### Decision

Introduce **structural pre-Router exclusion** as a third reviewer-participation category, alongside the
existing "always-run" (Code Rot Cody, Consistency Checker, Contrarian Carl) and "Router-judged"
categories. When the conditions are met, the candidate is excluded before the Router runs, not by the
Router's own judgment.

Currently, **Sam System is the only member** of this category. His exclusion signal is deterministic: if
the diff touches fewer than 3 files, fewer than 2 top-level directories, and contains no cross-file-composition
triggers (createSession, factory, bus, eventBus, config, options, inject, provider, compose), he is
structurally excluded (his gate reason is recorded, but he is still a normal Router candidate if the
precondition passes).

### Three required properties of any structural pre-Router gate

Any future structural exclusion gate must satisfy all three of these conditions, to avoid creating
shape-gaming incentives and to fail safe when infrastructure breaks:

1. **Structural signal from `diff-index.md`:** The exclusion signal must be deterministically computable
   from `diff-index.md` (file paths, line counts, keyword matches). It must not depend on model judgment,
   semantic analysis, or subjective thresholds. The signal is unreadable by definition — it cannot
   improve judgment; it can only reduce over-inclusion.

2. **One-way gate (Yes → No, never No → Yes):** The signal can only turn a candidate from "Yes" to "No,"
   never the reverse. False negatives (wrongly excluding a reviewer) are acceptable; false positives
   (wrongly including one) are not. This constraint keeps the gate from corrupting routing judgment.

3. **Fail-open on missing or unreadable inputs:** If `diff-index.md` is absent, empty, or unreadable,
   the gate must not fire — the candidate remains a normal Router candidate. Exclusion on an input error
   is indistinguishable from genuine exclusion and silently breaks the review for a broken file. Marker
   the skip in `tagged-sections.md` so a harness can audit gate behavior later.

### The structural_pre_gate_ineligible denylist

`reviewers/index.yaml` maintains a `structural_pre_gate_ineligible` list, currently containing:
`security-sage`, `rachel`, `tara-typesafe`, `contract-chris`. These reviewers are permanently
ineligible for structural pre-Router gates because their domain can be fully expressed in a single file
or single line — a hardcoded secret, a shell injection, an unescaped path, a race condition in a mutex,
an off-by-one in a bounds check. No structural diff-shape precondition can ever prove such vulnerabilities
do not apply, so excluding them structurally is not a tuning choice; it would be incorrect. Any new
structural gate must verify that its gated reviewer is not in this list.

### Tuning vs. architecture

A separate offline audit harness will analyze `useWhen`/`triggers` tuning for reviewers selected with
historically low yield (per `/review-stats`). Tuning is operational, not architectural — it remains
human-authored edits to `index.yaml`, requiring no new ADR. **Only if an audit harness ever participates
in the live review-time decision** (e.g., by dynamically excluding reviewers based on measured yield data)
would that require its own architectural amendment. As long as the harness output is human-curated `index.yaml`
edits, it is tuning, not architecture.

### Reconciliation with prior decisions

This amendment does not contradict prior recorded findings in ADR-0003.2:

- **Extend existing patterns over inventing new arbiters.** The router already handles judgment; this
  gate is an exception for a specific over-selection problem, not a new arbitration layer.
- **Prefer Sonnet for narrow judgment.** This gate is not judgment — it is mechanical — so Sonnet does
  not run it; it fires during Step 5's setup before the Router runs.
- **No numeric Router cap.** The gate uses numeric thresholds (3 files, 2 dirs), but these are
  preconditions on *exclusion*, not caps on Router output — they may stop Sam System, but they cannot
  reduce any other reviewer's selection.
- **Fail-closed-but-input-guarded gates.** This gate fails *open*, not closed — if `diff-index.md` is
  missing, Sam System is *not* excluded. This is correct: on a missing file, the safe default is to run
  the reviewer, not to guess.
- **Measure before building.** The gate was tuned on corpus data (`/review-stats` showed 94% Sam System
  inclusion vs. ~10% domain relevance). The threshold (3 files, 2 dirs) is measured from actual diffs,
  not arbitrary.

## Amendment (ADR-0003.2.2) — Reviewer contexts and precedence

### Context

As the system evolved (ADR-0012, Phase 1 of Routing v2), reviewers gained tagged roles: `review` for code
review participation, `plan` for planning-command participation, and `write` for writing/editing commands.
Multiple participation-control mechanisms now coexist — the pre-router exclusion gate, the router's judgment,
named-selection override, effort levels, and the new context tags — and their interaction order must be
stated once, in one place, to prevent drift.

### Decision

The Router now honors **`contexts`** as a hard eligibility filter applied *before* the router runs, on the
resolved candidate pool. The tag schema is defined in `reviewers/index.yaml` (header) and `reviewers/README.md`
(**Contexts and resolution precedence** section).

The **precedence order** for all review-participation mechanisms is defined once in `reviewers/README.md`'s **Contexts and resolution precedence** section (Named selection → `contexts` hard filter → Existing structural gates → `useWhen`/triggers ranking) — this ADR records the decision to adopt it, not a second copy of the steps. Consult that section for the canonical ordering and rationale.

**Structural gates stay sibling:** `structural_pre_gate_ineligible` remains a top-level key in
`reviewers/index.yaml` in Phase 1 and is not folded into `contexts` (Phase 2 may revisit this).

**No `gate` field:** gating behavior (always-run, shape-gated, path-gated) stays where it is today
(router/panel prose, structural_pre_gate_ineligible list, trigger conditions), pointed at from the entry's
`note:` if needed. The `contexts` key carries only the strength enum (`primary|secondary|named-only`).

**Missing `contexts` is a hard error:** every reviewer entry in `reviewers/index.yaml` must carry an
explicit `contexts` map with valid keys and values; there are no defaults, and a missing map is not
silently skipped.

**Fail-closed on empty resolution:** if the resolved set for a context is empty after all four precedence steps,
the command stops and reports which context resolved empty; the review or plan does not run with zero reviewers.

### Consequences

- **Good:** one rule for all mechanisms, stated once, cited not restated, preventing the tuning/architecture
  drift that plagued the two planning tables.
- **Good:** phase separation is clear — Phase 1 establishes the schema and four-stage precedence;
  Phase 2 can operationalize `secondary` promotion without changing the precedence rule.
- **Cost:** Phase 1 reviewers now have an additional tagged field to maintain; missing or wrong tags are
  caught by `tests/test_reviewer_contexts.py` and by the audit harness (`scripts/reviewer-selection-audit.py`).
- **Constraint:** all four participation mechanisms must yield a non-empty set; an `empty` result for any
  context signals misconfiguration and halts the run.

## Amendment (ADR-0003.3) — Deterministic shadow scorer (observe-only, #195)

Amended 2026-09-23 (#195): A deterministic, no-LLM scorer (`scripts/route-score.py`) runs in shadow mode
during Step 5 after the Router's Panel Decision is final, writing `route-scores.json` as an audit copy.
The shadow scorer has zero effect on seating — the Router (ADR-0003.2) remains the sole seating authority
until Phase 3 (#196). The shadow scorer measures whether a deterministic-only routing would have matched
the Router's judgment over a collection window, enabling Phase 3 to decide whether to flip seating authority.
Any flip in Phase 3 requires a real ADR-0003 amendment (not a shadow mode), per ADR-0016 (usage data must
not silently change routing). See `reviewers/README.md` (route: schema), `commands/review-stats.md` (shadow
section), and `prompts/expert-review-panel.md` Step 5 for implementation details.
