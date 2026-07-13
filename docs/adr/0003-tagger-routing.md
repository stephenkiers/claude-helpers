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

`--all` overrides routing to force every reviewer; `--full` reviews the whole codebase, not just the
diff. Naming reviewers explicitly (`/expert-review rachel,security-sage`) *is* the routing decision:
bypasses the router and runs only the named reviewers.

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
