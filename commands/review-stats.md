---
name: review-stats
description: Use when the user says "/review-stats" or asks which reviewers are earning their keep. Scans past /expert-review checkpoints and reports per-reviewer confirmed-vs-rejected finding rates, so low-signal personas can be pruned or fixed.
model: haiku
---

# Review Stats — Reviewer Eval Loop

`/expert-review` leaves a full audit trail under `~/.claude/reviews/{project}/{branch}-{hash}/`.
This command aggregates it into per-reviewer signal rates: how often each persona runs, finds
anything, and how its findings survive Pass 2 (CONFIRMED vs RESOLVED/DOWNGRADED). Use it to tune
the panel — a persona that never produces CONFIRMED findings is either mis-triggered, redundant,
or needs a sharper prompt.

## Arguments

- No args: aggregate across ALL projects in `~/.claude/reviews/`
- `$1`: limit to one project (directory name under `~/.claude/reviews/`)

## Instructions

### 1. Enumerate reviews

```bash
ls -d ~/.claude/reviews/*/*/ 2>/dev/null
```

If none exist, say so and stop ("run /expert-review first"). If `$1` was given, filter to
`~/.claude/reviews/$1/*/`.

### 2. Collect per-reviewer data

For each review folder, for each `{reviewer}-pass1.md`:
- **Decision** (`QUICK-SCAN` / `DEEP-DIVE`) from the `## Decision` section
- **Finding counts** from the `## Summary` section (Critical/High/Medium/Low)

For each `{reviewer}-pass2.md`, the `## Summary` counts of CONFIRMED / RESOLVED / DOWNGRADED.

**Routing data** (from the new router-based pipeline):
- `final-report.md` → `## Routing Accuracy` — the router's Panel Decision table.
  Each row is a reviewer: Selected/Not Selected, plus the router's rationale.
  - Reviewers Selected=Yes who ended up with findings → router was right
  - Reviewers Selected=Yes who found nothing → router over-inclusive (minor, costs one agent)
  - Reviewers Selected=No who do not appear in any findings → router was right
  - The Amalgamator attributes a finding to a domain whose reviewer was Selected=No → potential
    router miss (rare; check whether that domain's `useWhen` in `index.yaml` needs broadening)

**Format carve-outs** (ADR-0006 — these do NOT use the canonical `## Summary` block; parse them
explicitly rather than dropping them into `unparsed`):
- **Code Rot Cody** — symbol-inventory table; count rows flagged `DEAD` / `ORPHANED` as findings.
- **Consistency Checker** — its own inconsistency list; count each listed inconsistency as a finding.
- **Contrarian Carl** — contrastive format, and no pass2 by design: report his finding volume with
  the confirmation column `n/a`.

Anything still unparseable (genuinely older format) counts as `unparsed` rather than a guess.

### 3. Aggregate and report

Per reviewer, across all scanned reviews:

| Column | Meaning |
|--------|---------|
| Router selections | how many times the router selected this reviewer |
| Router skips | how many times the router did not select this reviewer |
| Runs | pass1 files present (reviews where selected) |
| Findings | total across runs |
| Confirmed / Resolved / Downgraded | from pass2 summaries |
| **Signal rate** | Confirmed ÷ Findings (blank if no findings) |
| **Unparsed** | files whose format couldn't be read (should be 0) |

Sort by signal rate descending, then findings descending. After the table, add a short **Tuning
suggestions** section:

- **Prune/re-trigger candidates**: ≥5 router selections, zero findings ever — the router's `useWhen`
  guidance may be too generous, or this domain truly doesn't appear in this codebase's diffs.
  Update `useWhen` in `index.yaml` to narrow the router's interest.
- **Noise candidates**: ≥5 findings with signal rate < 25% — their prompt likely needs the
  framework's "When NOT to Flag" guards applied more aggressively, or their severity calibration
  is off.
- **Keepers**: signal rate ≥ 60% with ≥3 confirmed findings.
- **Router-too-narrow**: routed <20% of diffs but found CONFIRMED findings when selected — the
  router may be under-including this reviewer. Check their `useWhen` in `index.yaml` and consider
  broadening it; the router is being conservative.
- **Router-too-generous**: routed >80% of diffs, mostly finding nothing — the router's `useWhen`
  guidance is too broad. Tighten it in `index.yaml`.
- **Unparsed > 0**: a format changed and this report is now blind to it. Fix the parser before
  reading anything else here — silent exclusion is worse than a wrong number.
- **Router accuracy check**: spot-check a few reviews' `## Routing Accuracy` tables. Does the
  router's rationale make sense? Are there obvious misses (e.g., a domain that clearly applies but
  the router skipped it)? Poor routing accuracy should drive refinement of `useWhen` guidance.

State the sample size prominently — with fewer than ~5 reviews, say the data is too thin to act
on and skip the suggestions.

## Output

A single table + suggestions in the conversation (no file written — this is a read-only report).
End with: "Tune triggers in `~/.claude/reviewers/index.yaml`; tune prompts in the persona YAMLs."
