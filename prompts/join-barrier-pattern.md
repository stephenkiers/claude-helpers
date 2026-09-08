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

The subagent writes to a file path and sentinel pattern determined by its context:

| Context | File path | Sentinel | Notes |
|---------|-----------|----------|-------|
| Step 3 (per-expert contributions, effort 3–5) | `{PLAN_SESSION_DIR}/{expert}-contribution.md` | `<!-- contribution-end -->` | Effort 3–5 standard path |
| Step 3 (swarm merge, effort 1) | `{PLAN_SESSION_DIR}/swarm-contribution.md` | `<!-- contribution-end -->` | Effort 1 swarm path — reuses contribution sentinel |
| Step 3 (pod contributions, effort 2) | `{PLAN_SESSION_DIR}/{pod-id}-pod.md` | `<!-- pod-end -->` | Effort 2 pod path — separate sentinel |
| Step 4 (Carl) | `{PLAN_SESSION_DIR}/contrarian-carl-contribution.md` | `<!-- contribution-end -->` | After per-expert barrier |
| Step 8 (alignment) | `{PLAN_SESSION_DIR}/{expert}-alignment.md` | `<!-- alignment-end -->` | Per-expert alignment pass |

The file **must end with its designated sentinel on a new line**.

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

## Pod Barriers (Effort 2)

A **pod is one atomic unit** for the join barrier. The three join conditions (receipt, file-exists, sentinel) apply to the WHOLE pod file, not per-persona/per-lens. A pod that fails twice gets ONE stand-in `Decision: FAILED` file for that entire pod (covering all 4 personas in that pod), not per-persona stand-ins.

This is pod-atomic granularity, matching `~/.claude/prompts/reviewer-pod.md`'s existing review-mode precedent (effort 2 on the review side). The stand-in file follows the same template as any other stand-in (see above), with `{expert}` replaced by `{pod-id}` and `{suffix}` = `pod`: one `Decision: FAILED` file covers the entire pod, not a separate stand-in per persona.

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
