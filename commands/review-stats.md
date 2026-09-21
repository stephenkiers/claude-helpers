---
name: review-stats
description: Per-reviewer token yield and finding escalation leaderboard — opt-in human reporting, never wired into expert-review.
---

# Review Stats — Per-Reviewer Leaderboard

This command tracks reviewer ROI across `/expert-review` runs: **token cost per reviewer** (input, output, cache), **mention count** (how many times each reviewer's findings appear in the final report), and **escalation count** (findings that made it into the action plan as high-priority).

**This is a leaderboard, not a verdict.** It is opt-in and completely disconnected from expert-review's own decision-making — a bug in this script produces a bad report, never a bad review. Never read by any reviewer prompt or triage logic.

## Usage

- `/review-stats` (no args) — aggregate leaderboard across all logged runs for the current repo.
- `/review-stats <review-dir-or-path>` — log that specific run (idempotent) and print its
  per-reviewer breakdown.

## Steps

Parse `$ARGUMENTS` — empty means aggregate mode, non-empty is treated as a review-dir path or name.

```bash
# Repo identity — same pattern as verify-queue.md
REPO=$(gh repo view --json nameWithOwner -q '.nameWithOwner' 2>/dev/null || echo "")
if [ -z "$REPO" ]; then
  echo "Error: not in a git repo with a GitHub remote (gh repo view failed)." >&2
  exit 1
fi
REPO_KEY=$(printf '%s' "$REPO" | tr '/' '-')

if [ -z "$ARGUMENTS" ]; then
  # No args: aggregate leaderboard for the current repo
  scripts/reviewer-yield.py --aggregate "$REPO_KEY"
else
  # A review-dir path or name was given: log it (idempotent) and print its table
  scripts/reviewer-yield.py "$ARGUMENTS"
fi
```

Idempotent: running the single-review form twice for the same review-dir does nothing the second
time (already logged). Outputs a per-reviewer table to stdout and appends one JSON line per
reviewer to `~/.claude/reviews/{owner-repo}/reviewer-yield.jsonl`.

Reads `~/.claude/reviews/{owner-repo}/reviewer-yield.jsonl` in aggregate mode and prints average
cost (input + output tokens per run), total mentions and escalations, and escalation rate (% of
mentions that made it into the action plan) for each reviewer across all runs.

## How It Works

1. Locates your review directory and finds the subagent transcripts that wrote to it.
2. Parses token usage from each subagent (only "complete turn" messages with `iterations` key to avoid double-counting).
3. Counts mentions of each reviewer in `final-report.md`.
4. Counts escalations in `claude-action-plan.md` (looks for `**Raised by**: <reviewer>` markers).
5. Appends one JSON line per reviewer to `~/.claude/reviews/{owner-repo}/reviewer-yield.jsonl` (creates file and parent dirs if missing, skips if run_id already exists).

## Notes

- **Opt-in only:** Manual run after expert-review, never automatic.
- **Read-only:** No interaction with reviewer logic, rulings, or findings suppression.
- **Per-repo queue:** Stored at `~/.claude/reviews/{owner-repo}/reviewer-yield.jsonl` alongside your review directories.

For the methodology behind tuning `/expert-review`'s reviewer selection based on attendance and yield metrics, see `prompts/reviewer-selection-audit.md`.
