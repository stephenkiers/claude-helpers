---
name: usage-report
description: Use when user says "/usage-report" or wants a summary of local Claude Code usage/yield telemetry (command and stage match rates, data quality) over the last 7 and 30 days.
model: haiku
---

# Usage Report

Report on the local, observational telemetry log (`~/.claude/telemetry/events.jsonl`) — a 7-day
and a 30-day view. This command is a **thin wrapper**: it only runs the deterministic telemetry
CLI and shows its literal output. It never computes, sums, estimates, or reformats numbers itself
— all arithmetic already happened inside `run-metrics.py`.

## 1. Run diagnose for both windows

Run both windows and show the user **both outputs verbatim**, labeled by window:

```bash
echo "=== Last 7 days ==="
python3 "$HOME/.claude/scripts/run-metrics.py" diagnose --window-days 7
echo ""
echo "=== Last 30 days ==="
python3 "$HOME/.claude/scripts/run-metrics.py" diagnose --window-days 30
```

Present these two blocks to the user as-is (you may add a short heading above each, but do not
alter, recompute, or summarize the numbers inside them).

## 2. Instruction to future-Claude: deeper breakdowns are ad hoc, not built-in

If the user asks for something `diagnose` doesn't print — a parent-vs-subagent cost split, cost
per accepted finding, retry/zero-yield stage detection, or a concurrency-band breakdown — this is
a deliberate design choice (ADR-0016), not a gap to fill in with a new subcommand. Read
`docs/metrics.md`'s "Querying the Log Directly" section for starting `jq`/`python` patterns
against `~/.claude/telemetry/events.jsonl`, adapt them to the specific question, and answer it
directly in conversation. Do not build a new deterministic report or aggregation command for this
— the log stays a flat file meant to be queried fresh per question, since a fixed report can't
anticipate every value-vs-cost question worth asking (see ADR-0016's rationale).

## 3. Never fabricate numbers

If either `diagnose` invocation reports that the log doesn't exist yet, is empty, or has a 0%
match rate because nothing has been recorded, say so plainly — for example:

> No telemetry recorded yet — hooks may not be registered. See `docs/metrics.md` for setup.

Do not invent example token counts, costs, or match-rate numbers to fill the gap. If `diagnose`
exits non-zero (FAIL), report that plainly too — it means match rate or unknown-token thresholds
weren't met, not that the command itself failed.
