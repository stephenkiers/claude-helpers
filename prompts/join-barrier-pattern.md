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
for both modes (ADR-0023 amendments 2026-11-23, #532, and 2026-11-24, #553). Every child is launched in
the background and collected by a file wait. There is no foreground site: an interactive session's
Agent tool has no foreground mode (its schema has no `run_in_background` parameter, a `false` value is
silently dropped, and the call returns a launch acknowledgement), so a wait that relies on a blocking
return works only under `claude -p`. The probe history is in `~/.claude/prompts/headless-wait-probe.md`.

1. **Launch** the whole batch in one message. Run `wait-files.py mark` immediately before the launch
   (rule 6), and launch every call with `run_in_background: true`.

2. **The launched set** is the list of ids you just dispatched, taken from existing on-disk artifacts: the Router's selection in the review dir, `selected-experts.md` in the plan session dir, the unit ids in implement-with-haiku, or the single background Bash check run in implement-with-haiku's Final check. It is re-derivable after context compaction. There is no new manifest file.

3. **Receipt files.** Every dispatch prompt names the child's receipt file with a literal line
   `Receipt file: <RUN_DIR>/receipts/<phase>-<id>.md` (absolute path; `<phase>` is the part of the phase name after the colon, so a reviewer's Pass 1, Q&A and Pass 2 receipts never collide; `<RUN_DIR>` is the review dir, the plan
   session dir, or the implement scratch dir, never `FLOW_DIR`) and ends with this instruction: "Your
   last action: `Write` that file with your receipt line, then a final line `<!-- report-end -->`. A
   role that returns its result inline (Router, judges, effort-1 scouts) writes its whole report there
   instead of the receipt line." The receipt file is what the parent waits on and what it reads; the
   child's reply is never needed. A site that already names a report file ending with
   `<!-- report-end -->` (implement-with-haiku's units and sweeps) uses that file as the receipt file.
   Outputs keep their own formats and sentinels; the receipt file is the only new file.

4. **Per-id state** is `launched → returned → ok | bad`, with a branch to `launched (retry)` on first failure. An id is `ok` when its receipt file is fresh (rule 6's mark), ends with `<!-- report-end -->`, and, where the site names an output file and sentinel, that file exists and ends with its sentinel. An id becomes *returned* when its completion arrives: an `<agent-message from="{id}">` hand-back or a task notification. A launch acknowledgement ("Async agent launched", "working in the background") is never a return. A second or late notification for an id that is already `ok`, or that was stood in, is a no-op: it never overwrites the stand-in or triggers another retry. Never keep a running count of notifications. **After context compaction, or whenever remembered state is uncertain,** an id whose files pass the `ok` check is `ok` regardless of remembered state.

5. **Retry once** only where the site has a stand-in (Pass 1 reviewers, plan contributions, pods, alignment, reconciliation): when an id has *returned* and is not `ok`, it is `bad`. Re-launch that one child (same receipt path; no new mark, since the retry's files are newer than the mark) and keep waiting. If it returns `bad` again, write the stand-in file per the existing pattern and treat its receipt as `ok`. A site with no stand-in never retries: a `bad` id there fails the phase with reason `child returned without a valid receipt: <id>`. An id that never returns is handled by the deadline (rule 8), never by a retry.

6. **Never end your turn while any launched id is outstanding.** Collect the children in the same turn with `~/.claude/scripts/wait-files.py`. Run `mark` immediately before the launch, then repeat `wait` on the receipt files in the foreground until it exits 0 (closed) or 4 (deadline). Exit 3 (`pending: <ids>`) means the slice ended: before re-issuing the same `wait`, apply rule 5 to any pending id that has *returned* since the last slice. On exit 0, check each id's output file per rule 4 before continuing; an id that fails it has returned `bad` (rule 5), and the wait for its retry lists that id alone. Each call blocks for up to `--timeout` seconds (default 540, never below 300) and polls the files every 5 s, so it costs no model tokens while it waits.
   - **Bash tool timeout.** Every Bash tool call that runs `wait-files.py wait` must pass an explicit `timeout: 600000` (milliseconds) parameter on the tool call. The default Bash tool timeout is shorter than the 540 s slice, so without it the call is killed mid-slice.
   - **Killed or exit 2.** If a `wait` Bash call is killed or returns exit 2, re-issue the same `wait` exactly once. If that re-issue is also killed or returns exit 2, write the FAILED receipt (rule 8 wording, `Reason: child-wait call failed: phase <P>`) and stop. Do not relaunch the children.
   - **Setup.** This requires `/setup-local` to have been run after pulling, so that `~/.claude/scripts/wait-files.py` exists.
   - **Marks.** `mark` refuses to overwrite an open phase without `--force`. Phases are keyed by name, so batches that overlap (North Star scouts beside Pass 1) each take their own phase name. A wait that exits 4 (deadline) leaves the phase open, so re-running the same phase requires `mark --force`. A phase that repeats per round (plan reaction rounds) is marked again after its previous wait closed. After context compaction, pass an explicit `--since <EPOCH>` to `wait` (the epoch recorded at the mark) rather than relying on the recorded mark.

     ```bash
     python3 "$HOME/.claude/scripts/wait-files.py" mark --phase <STEP or command>:<phase> --state-dir <STATE_DIR>
     # ... launch the batch, every call with run_in_background: true ...
     # Bash tool call, with timeout: 600000 (milliseconds)
     python3 "$HOME/.claude/scripts/wait-files.py" wait --phase <STEP or command>:<phase> --state-dir <STATE_DIR> \
       --deadline <SECS> --sentinel '<!-- report-end -->' <id>=<RUN_DIR>/receipts/<phase>-<id>.md [<id>=<path> ...]
     ```
   - `<STATE_DIR>` is `FLOW_DIR` when set, otherwise the run's own directory (the plan session dir, the review dir, or the implement scratch dir). The phase name is `<STEP or command>:<phase>`, for example `implement:round1` or `review:summarize`. Outside a flow `<STEP or command>` is the command name (`expert-review`, `expert-plan`).
   - Per-phase deadlines (initial values, to be calibrated from the `closed` rows in `<STATE_DIR>/child-wait.jsonl`): single bounded children (Summarizer, Router, Carl, Amalgamator, Triage Chief, synthesis, audit, judges, single fix agents) and panel, scout, Q&A, reaction and Pass 2 phases 1800 s; Precedent Pat 300 s (on deadline it fails soft, see its site); Round 2 and Round 3 sweeps 3600 s; implementer units 5400 s; the check run `timeout_secs` + 300; merge apply `MERGE_APPLY_TIMEOUT_SECS` (default 1800) + 300; queued merge 3600 s.
   - `ScheduleWakeup`, where granted, is never used to wait for children. Never sleep or poll with a short interval of your own: `wait-files.py` is the only polling, and its floor is what keeps it from becoming #216-style polling.

7. **Never rely on a blocking return.** Do not pass `run_in_background: false` and do not read a child's result from the Agent call's return. If an Agent call does return a full report (a harness that still blocks), ignore it and collect the receipt file as usual; the wait closes at once.

8. **Deadline hard stop.** On `wait-files.py` exit 4, stop the phase:
   - emit `run-metrics.py stage-end --outcome failure --failure-class timeout`;
   - inside a flow, write the FAILED receipt with `Reason: child-wait deadline: phase <P>, outstanding <ids>, attempt <n>`, prefixed `repeated ` when the script printed its `loop:` line;
   - never relaunch and never write a stand-in for an outstanding id. The flow's existing Retry / Take over / Abort stop handles it; a repeated deadline is visible in the receipt and in `child-wait-attempts.json`. A site that names a soft deadline (Precedent Pat) continues as that site says instead.

9. **The barrier closes** when every id is `ok` (or stood in). A launch acknowledgement alone never closes it.

**Known tradeoff.** A child that dies without returning is invisible until the full phase deadline expires; a child that returns without a valid receipt is caught at the next slice boundary (at most one `--timeout` slice later). This is accepted for now. A self-heal follow-up issue is to address per-id freshness for children that never return.

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
| Every child's receipt file | `<RUN_DIR>/receipts/<phase>-<id>.md` (the `Receipt file:` line in the dispatch prompt) | `<!-- report-end -->` | Written last; holds the receipt line, or the whole report for an inline-return role (Router, judges, effort-1 scouts). The barrier waits on it; outputs keep their own sentinels |
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
