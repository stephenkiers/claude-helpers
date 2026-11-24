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
file so downstream glob patterns find something (the barrier never hangs silently — a non-returning
agent escalates to the user).

## Waiting for the barrier (harness-agnostic)

This section defines when a barrier closes and how a subagent or skill waits for it. It binds every
subagent and every skill, interactive or run by an `/expert-flow` step agent: there is one mechanism
for both modes (ADR-0023 amendment 2026-11-23, #532). The headless probe that chose it is recorded in
`~/.claude/prompts/headless-wait-probe.md` (Branch F: bounded sites collect by a foreground return;
units, sweeps, check runs and merge applies collect by a file wait).

1. **Launch** the whole batch in one message. A site designated *foreground* launches every call with
   `run_in_background: false`. A site designated a *file wait* runs `wait-files.py mark` immediately
   before the launch (rule 6) and launches with `run_in_background: true`.

2. **The launched set** is the list of ids you just dispatched, taken from existing on-disk artifacts: the Router's selection in the review dir, `selected-experts.md` in the plan session dir, the unit ids in implement-with-haiku, or the single background Bash check run in implement-with-haiku's Final check. It is re-derivable after context compaction. There is no new manifest file.

3. **Per-id state** is `launched → returned → ok | bad`, with a branch to `launched (retry)` on first failure. An id becomes *returned* when its final report arrives: a synchronous result containing the report, an `<agent-message from="{id}">` hand-back, or a task notification whose `<result>` contains the report. A synchronous result that only says the agent was launched ("Async agent launched", "working in the background") is a launch acknowledgement: the id stays `launched`. A notification whose `<result>` only points at a hand-back marks the id *returned*, but the receipt is read from the hand-back message. The hand-back and the notification for one id may arrive in separate turns: that is two partial turns and one state change, never a retry. A second or late notification for an id that has already returned, or that a file wait already closed, is a no-op, including one that arrives after that id's stand-in was written: it never overwrites the stand-in or triggers another retry. Never keep a running count of notifications.

4. **On return**, check that id's checkpoint file and sentinel and mark it `ok` or `bad`. This applies at **foreground sites**, where a `bad` id is retried once per rule 5. At **file-wait sites** the same file check decides `ok` or not-yet-`ok`, but a `bad` file never triggers a retry or stand-in; rule 5 governs that case. For agents that write no checkpoint file and return their result inline (Router, Q&A), the report carried by the hand-back (or synchronous result or notification, whichever contains it) is the whole receipt. **After context compaction, or whenever remembered state is uncertain,** an id whose checkpoint file exists with its sentinel is `ok` regardless of remembered receipt state; only ids without a valid file remain outstanding, and a missing file still never triggers a retry. A present-but-invalid file (exists, but lacks a sentinel or is unreadable) is `bad`.

5. **Retry once** only at a foreground site, when an id has *returned* and is `bad` — state becomes `launched (retry)`. Evaluate the next return once: if still `bad`, write the stand-in file per the existing pattern. **File-wait sites have no "returned bad" state, no retry and no stand-in:** a child is `ok`, or the phase ends at its deadline with the hard stop of rule 8. (A per-id retry there needs per-id freshness and a defined `bad` marker; that belongs to the self-heal follow-up.)

6. **Never end your turn while any launched id is outstanding.** Collect the children in the same turn:
   - **Foreground sites** collect by the foreground return: the dispatch call blocks until every child returns and carries its report. The bound here is the harness and each child's own turn limits; a foreground return has no `wait-files.py` deadline. An outer step deadline, where one is named, still applies.
   - **File-wait sites** collect with `~/.claude/scripts/wait-files.py`. Run `mark` immediately before the launch, then repeat `wait` in the foreground until it exits 0 (closed) or 4 (deadline). Exit 3 (`pending: <ids>`) means the slice ended: re-issue the same `wait` call at once. Each call blocks for up to `--timeout` seconds (default 540, never below 300) and polls the files every 5 s, so it costs no model tokens while it waits.
     - **Bash tool timeout.** Every Bash tool call that runs `wait-files.py wait` must pass an explicit `timeout: 600000` (milliseconds) parameter on the tool call. The default Bash tool timeout is shorter than the 540 s slice, so without it the call is killed mid-slice.
     - **Killed or exit 2.** If a `wait` Bash call is killed or returns exit 2, re-issue the same `wait` exactly once. If that re-issue is also killed or returns exit 2, write the FAILED receipt (rule 8 wording, `Reason: child-wait call failed: phase <P>`) and stop. Do not relaunch the children.
     - **Setup.** This requires `/setup-local` to have been run after pulling, so that `~/.claude/scripts/wait-files.py` exists.
     - **Marks.** `mark` refuses to overwrite an open phase without `--force`. There is one phase per `STATE_DIR`. After context compaction, pass an explicit `--since <EPOCH>` to `wait` (the epoch recorded at the mark) rather than relying on the recorded mark.

     ```bash
     python3 "$HOME/.claude/scripts/wait-files.py" mark --phase <STEP or command>:<phase> --state-dir <STATE_DIR>
     # ... launch the batch ...
     # Bash tool call, with timeout: 600000 (milliseconds)
     python3 "$HOME/.claude/scripts/wait-files.py" wait --phase <STEP or command>:<phase> --state-dir <STATE_DIR> \
       --deadline <SECS> --sentinel '<sentinel>' <id>=<path> [<id>=<path> ...]   # or --exists for exit-status files
     ```
   - `<STATE_DIR>` is `FLOW_DIR` when set, otherwise the run's own directory (the plan session dir, the review dir, or the implement scratch dir). The phase name is `<STEP or command>:<phase>`, for example `implement:round1`.
   - Per-phase deadlines (initial values, to be calibrated from the `closed` rows in `<STATE_DIR>/child-wait.jsonl`): panel, scout, Q&A and Pass 2 phases 1800 s; Round 2 and Round 3 sweeps 3600 s; implementer units 5400 s; the check run `timeout_secs` + 300; merge apply `MERGE_APPLY_TIMEOUT_SECS` (default 1800) + 300; queued merge 3600 s.
   - `ScheduleWakeup`, where granted, is never used to wait for children. Never sleep or poll with a short interval of your own: `wait-files.py` is the only polling, and its floor is what keeps it from becoming #216-style polling.

7. **A launch acknowledgement at a foreground site is a hard failure.** If a dispatch at a site designated foreground comes back with only a launch acknowledgement, stop the phase with reason `foreground dispatch returned a launch acknowledgement: <id>`. Do not wait for that child in a later turn.

8. **Deadline hard stop.** On `wait-files.py` exit 4, stop the phase:
   - emit `run-metrics.py stage-end --outcome failure --failure-class timeout`;
   - inside a flow, write the FAILED receipt with `Reason: child-wait deadline: phase <P>, outstanding <ids>, attempt <n>`, prefixed `repeated ` when the script printed its `loop:` line;
   - never relaunch and never write a stand-in for an outstanding id. The flow's existing Retry / Take over / Abort stop handles it; a repeated deadline is visible in the receipt and in `child-wait-attempts.json`.

9. **The barrier closes** when every id is `ok` (or stood-in, at a foreground site). A launch acknowledgement alone never closes it.

**Known tradeoff.** At a file-wait site, a dead child is invisible until the full phase deadline expires; nothing reports it earlier. This is accepted for now (plan Q4: hard stop, no relaunch). A self-heal follow-up issue is to be filed to address per-id freshness and a defined `bad` marker.

**Carve-out: the top-level orchestrator.** `commands/expert-flow.md` is not a step agent. It waits on its one step subagent at a time (N=1) and may still end its turn to await that single step's completion, with at most one `ScheduleWakeup` of 1800 seconds per step as a heartbeat that writes a status report only. Rules 6 to 8 bind every subagent and skill it runs, and every other caller.

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
| Inline-return children at a file-wait site (Router, Q&A, effort-1 scouts) | the report file named in the dispatch prompt | `<!-- report-end -->` | Only when such a child is collected by `wait-files.py`; at a foreground site its report is the returned result |
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

- If the barrier timed out (`wait-files.py` exited 4, children still outstanding at the phase deadline): emit failure telemetry for the stage, then `command-end failure`, then stop with the deadline hard stop of § Waiting for the barrier rule 8.
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
