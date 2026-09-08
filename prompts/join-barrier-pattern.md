# Join-Barrier Pattern for Parallel Expert Contributions

This pattern is used by `/expert-plan-v2` in Steps 3 (parallel contributions), 4 (contrarian Carl),
and 8 (alignment pass) to coordinate multiple subagents and ensure all checkpoints are written
before proceeding.

## Overview

A **join barrier** coordinates N parallel subagents by checking three conditions per expert:

1. **Receipt returned** — the subagent returned a one-line receipt in its final message
2. **File exists on disk** — `{PLAN_SESSION_DIR}/{expert}-*.md` exists and is readable
3. **Sentinel present** — the file ends with `<!-- {type}-end -->` sentinel line

If any condition fails for an expert, **retry once**. If the retry also fails, write a stand-in
file so downstream glob patterns find something (the barrier never hangs).

## Receipt Format

The subagent's one-line receipt must be **parseable and unambiguous**. Expected format:

```
{expert}-*.md written — {n} requirements, {n} risks, {n} open questions
```

The filename in the receipt must match the actual file written, and counts must be accurate.

## File Format and Sentinel

The subagent writes to:
```
{PLAN_SESSION_DIR}/{expert}-{suffix}.md
```

Where `{suffix}` is context-dependent:
- Step 3 (contributions): `{expert}-contribution.md`
- Step 4 (Carl): `contrarian-carl-contribution.md`
- Step 8 (alignment): `{expert}-alignment.md`

The file **must end with the sentinel on a new line**:
```
<!-- {type}-end -->
```

Where `{type}` matches context:
- Step 3: `contribution-end`
- Step 4: `contribution-end`
- Step 8: `alignment-end`

## Stand-In File on Repeated Failure

If the subagent fails twice (returned no output, crashed, or partial write), create a stand-in:

```bash
cat > "{PLAN_SESSION_DIR}/{expert}-{suffix}.md" <<'EOF'
# {Type}: {expert}

## Decision
FAILED

## Reason
Agent returned no output or a truncated write after two attempts.

<!-- {type}-end -->
EOF
```

This ensures downstream globbing operations find a file for every selected expert, even if some
subagents failed. The file is marked with `Decision: FAILED` so diagnostic/summary steps can count
failures separately from successes.

## Failure Outcomes

After the join barrier completes (either all experts succeeded or stand-ins filled failed slots):

- If the barrier timed out (subagents still running after timeout): emit failure telemetry for the stage, then `command-end failure`, then stop.
- If multiple experts failed: emit failure telemetry, then stop.
- If all experts succeeded (or are stand-in): continue to the next step.

Failures at the barrier are stage-level, not expert-level — a single failed expert fails the entire
stage (e.g. if 1 of 6 experts crashes, the entire "expert-contributions" stage fails).

## Telemetry for Join Barriers

Each join barrier stage should emit:
```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage <stage-name> --outcome success 2>/dev/null || true
```

On successful barrier completion (all experts returned, receipts verified, files exist with sentinels).

On failure:
```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage <stage-name> --outcome failure --failure-class <class> 2>/dev/null || true
```

Where `<class>` is one of: `contribution-barrier-failed`, `alignment-barrier-failed`, etc.
