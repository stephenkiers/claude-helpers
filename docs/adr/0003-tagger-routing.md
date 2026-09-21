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
