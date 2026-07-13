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

**Routing data** (present only in reviews run by the gated pipeline):
- `skipped.md` — each row is a reviewer whose haiku confirm-gate agreed with the tagger's SKIP.
  Count as a **gate-skip** (not a run).
- `final-report.md` → `## Routing Accuracy` — reviewers the gate **escalated** past the tagger.
  An escalation whose findings end up CONFIRMED is a caught tagger miss; one that produces no
  confirmed finding is a gate false-positive.
- `final-report.md` → `## Routing Accuracy` header line, if present — a **⚠️ TAGGER COLLAPSE** note
  (routed {n}/{total}, skipped {m}/{total}). Count these per project as **collapse events**: how
  many of this project's reviews had the tagger skip >60% of the panel. A project with frequent
  collapses has triggers mismatched to what its diffs actually look like (e.g. prose-heavy repos
  where code-pattern keywords rarely match).

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
| Runs | pass1 files present (routed + escalated) |
| Gate-skips | rows in `skipped.md` — tagger said skip, gate agreed |
| Escalations | gate overruled the tagger and forced a full review |
| Findings | total across runs |
| Confirmed / Resolved / Downgraded | from pass2 summaries |
| **Signal rate** | Confirmed ÷ Findings (blank if no findings) |
| **Unparsed** | files whose format couldn't be read (should be 0) |

Sort by signal rate descending, then findings descending. If any collapse events were found, add a
one-line note above the table: `⚠️ Tagger collapse in {c}/{total} reviews for this project — see
Tuning suggestions.` After the table, add a short **Tuning suggestions** section:

- **Prune/re-trigger candidates**: ≥5 runs, zero findings ever — triggers may be routing them to
  irrelevant diffs, or their domain never appears in this codebase.
- **Noise candidates**: ≥5 findings with signal rate < 25% — their prompt likely needs the
  framework's "When NOT to Flag" guards applied more aggressively, or their severity calibration
  is off.
- **Keepers**: signal rate ≥ 60% with ≥3 confirmed findings.
- **Trigger-too-narrow**: escalated ≥3 times with confirmed findings — the tagger keeps missing
  this reviewer's domain. Widen their triggers in `index.yaml`; the gate is catching what routing
  should have.
- **Rubber-stamp gate**: ≥8 gate-skips and zero escalations ever. Either the persona is correctly
  narrow, or its gate is agreeing with the tagger by reflex. Spot-check one `skipped.md` reason
  against the diff before trusting it.
- **Unparsed > 0**: a format changed and this report is now blind to it. Fix the parser before
  reading anything else here — silent exclusion is worse than a wrong number.
- **Tagger collapse ≥2 reviews**: this project's `index.yaml` triggers are systematically
  mismatched to its diffs — most reviewers fall through to the confirm-gate on every run, which
  works but costs far more tokens than routing would. Retune triggers toward how this repo's diffs
  actually read (e.g. prose/config keywords, not just code patterns) rather than treating the gate
  as the steady-state path.

State the sample size prominently — with fewer than ~5 reviews, say the data is too thin to act
on and skip the suggestions.

## Output

A single table + suggestions in the conversation (no file written — this is a read-only report).
End with: "Tune triggers in `~/.claude/reviewers/index.yaml`; tune prompts in the persona YAMLs."
