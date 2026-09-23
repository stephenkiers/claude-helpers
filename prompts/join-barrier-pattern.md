# Join-Barrier Pattern for Parallel Expert Contributions

This pattern is used by the `/expert-review` panel (Pass 1, Q&A, Pass 2 dispatches), `/implement-with-haiku` 
Step 4d, and `/expert-plan` (Steps 3, 4, 6, 7) to coordinate multiple parallel subagents and ensure all 
checkpoints are written before proceeding.

## Overview

A **join barrier** coordinates N parallel subagents by checking three conditions per expert:

1. **Receipt returned** — the subagent returned a one-line receipt in its final message
2. **File exists on disk** — `{PLAN_SESSION_DIR}/{expert}-*.md` exists and is readable
3. **Sentinel present** — the file ends with `<!-- {type}-end -->` sentinel line

If any condition fails for an expert, **retry once**. If the retry also fails, write a stand-in
file so downstream glob patterns find something (the barrier never hangs).

## Waiting for the barrier (harness-agnostic)

This section defines when a barrier closes and how orchestrators wait for it, independent of whether
the harness returns agent results synchronously (older harness) or through notifications (current harness).

1. **Launch** the whole batch in one message. Do not assume the batch has finished by your next turn.

2. **The launched set** is the list of ids you just dispatched, taken from existing on-disk artifacts: the Router's selection in the review dir, `selected-experts.md` in the plan session dir, or the unit ids in implement-with-haiku. It is re-derivable after context compaction. There is no new manifest file.

3. **Per-id state** is `launched → returned → ok | bad`. An id becomes *returned* when its final report arrives: a synchronous result containing the report, an `<agent-message from="{id}">` hand-back, or a task notification whose `<result>` contains the report. A synchronous result that only says the agent was launched ("Async agent launched", "working in the background") is a launch acknowledgement: the id stays `launched`. A notification whose `<result>` only points at a hand-back marks the id *returned*, but the receipt is read from the hand-back message. The hand-back and the notification for one id may arrive in separate turns: that is two partial turns and one state change, never a retry. A second or late notification for an id that has already returned is a no-op, including one that arrives after that id's stand-in was written: it never overwrites the stand-in or triggers another retry. Never keep a running count of notifications.

4. **On return**, check that id's checkpoint file and sentinel and mark it `ok` or `bad`. For agents that write no checkpoint file and return their result inline (Router, Q&A), the report carried by the hand-back (or synchronous result or notification, whichever contains it) is the whole receipt. **After context compaction, or whenever remembered state is uncertain,** an id whose checkpoint file exists with its sentinel is `ok` regardless of remembered receipt state; only ids without a valid file remain outstanding, and a missing file still never triggers a retry. Late or "duplicate" notifications after compaction are no-ops, not a reason to poll.

5. **Retry once** only when an id has *returned* and is `bad`. A missing file for an id that has not returned means it is still running and is never a retry trigger. If the retry is also `bad`, write the existing stand-in file, per the existing pattern.

6. **If any id is still outstanding**, end your turn. A turn triggered by a partial arrival updates state and ends the turn again: no re-launch, no reading other agents' checkpoints early, no sleeping, no short-interval `ScheduleWakeup`.

7. **The barrier closes** when every id is `ok` or stood-in. If every final report arrived in the launching turn (a harness that returns reports inline), it closes right away with no turn end. A launch acknowledgement alone never closes it.

8. **Fallback.** When ending a turn with ids still outstanding, you may schedule **at most one** `ScheduleWakeup` of 1800s for the current phase (one dispatch batch), and only if none is already scheduled for that phase. When it fires:
   - If the phase is already closed, take no action.
   - Otherwise, write a **status report only**: which ids have returned, which files are missing or lack a sentinel. Tell the user, then end the turn.
   - It never retries, never writes stand-ins, and never schedules another wakeup. A later notification still closes the barrier normally.

## Receipt Format

The subagent's one-line receipt must be **parseable and unambiguous**. Expected format varies by context:

**Multi-expert contexts (Steps 3, 4, 8):**
```
{expert}-*.md written — {n} requirements, {n} risks, {n} open questions
```

**Single-agent contexts (Step 9):**
```
{filename} written — {n} resolved fixes, {n} needs-decision items
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
| Step 9 (reconciliation, single agent) | `{PLAN_SESSION_DIR}/plan-reconciled.md` | `<!-- reconciliation-end -->` | Single-agent step — N=1; on join-barrier failure after retry, this is a hard command failure (no stand-in — plan-reconciled.md IS the deliverable, unlike per-expert alignment notes where a FAILED stand-in is harmless input signal) |

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

## Failed-After
(For pod agents only: the index of the last completed persona before failure, or "none" if no personas completed.)

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
