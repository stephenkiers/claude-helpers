# Walking Skeleton Findings (Sub-issue S, #484)

These are the results of the measurement runs described in [skeleton-runbook.md](skeleton-runbook.md). They
gate Layer 1 (#485). Every row is either measured evidence (value, N, method, mode, evidence pointer) or the
literal `NOT MEASURED — <reason>`. No number here is estimated or inferred.

## Provenance

- **Who ran it**: the implementing agent, under the human's explicit authorization, for the headless rows
  (R3, R4, R5) only. The interactive rows (R1, R2, R6) were not run; see the rows below.
- **CLI version**: `claude --version` printed `2.1.322 (Claude Code)`.
- **Model**: `claude-sonnet-5-5` (the model named in every stream-json `assistant` message; the step agent
  inherited the default).
- **Dates**: all runs on 2026-11-12 (UTC), between 19:48Z and 20:05Z.
- **Runbook**: [skeleton-runbook.md](skeleton-runbook.md).
- **Setup (S-W3)**: the two temporary symlinks (`~/.claude/commands/expert-flow-v2.md` and
  `~/.claude/scripts/flow_driver`, both pointing into the #484 worktree) were created at 2026-11-12T19:45:32Z
  and removed at 2026-11-12T20:05:40Z. Removal was verified with `ls` (both paths absent), and the
  `~/.claude/flows-v2/` tree created by these runs was removed after the evidence was copied out. `install.sh`
  was never run. Runs started from a scratch directory with no `.claude/flow-run` marker.
- **Headless invocation**: `claude -p "<prompt>" --output-format stream-json --verbose` with the flags below,
  one fresh session per row. Evidence is the stream-json transcript plus the flow dir files (`driver.log`,
  `spawns.jsonl`, `heartbeat-a1`, `receipts/`).
- **Grant set (differs from the runbook, see GRANT-SHAPE)**: `--allowedTools 'Bash(python3 *flow_driver/__main__.py *)'
  --allowedTools 'Bash(python3 *fleet.py wait *)' --allowedTools 'Bash(echo *spawns.jsonl*)'
  --allowedTools 'Bash(printf *spawns.jsonl*)' --allowedTools 'Bash' --allowedTools 'Read'
  --allowedTools 'Write(~/.claude/flows-v2/*)' --allowedTools 'Task'`. The unrestricted `Bash` grant is what made
  the runs measurable; the step prompt is driver-written and was read before the runs.
- **Denied tool calls**: none in the rows that carry values (R3, R4, R5 transcripts each contain zero
  `permission_denied` events). The first R3 attempt, with the runbook's grant set, had two and is recorded as the
  void run in PERMISSION-VOID.
- **Cost**: the per-session `total_cost_usd` values in the transcripts were about 0.11 (R3), 0.13 (R4) and 0.24
  (R3 session, after the R5 resume) US dollars.

## Coverage disclaimer (S-D1)

The step agent in every run was `general-purpose` only. The restricted `/expert-flow` step agents
(`flow-implement`, `flow-review`, `flow-plan`, `flow-ship`, `flow-merge`) are not covered by these
measurements, and their hook and tool-grant behaviour under this driver moves to Layer 2.

## Findings table

N is the number of runs behind a row. N=1 means one run; the runbook's default of 3 was not met, to bound cost.
Flow ids are the flow dir basenames under `~/.claude/flows-v2/skeleton_measure/` (removed after the runs).
Flow A is `skeleton-20261112T195019Z-47a9a794` (R3, then R5 on the same flow). Flow B is
`skeleton-20261112T195020Z-941ddb08` (R4). The void flow is `skeleton-20261112T194821Z-45a776bc`.

| ID | Result (value, N, method, mode) | Evidence or reason |
| --- | --- | --- |
| AC1-SPAWN | n/a | NOT MEASURED — R1 (interactive happy path) was not run: requires an interactive session; not run by the implementing agent. Headless counts are in HEADLESS-VERBS. |
| AC1-RESUME | n/a | NOT MEASURED — R1 (interactive happy path) was not run: requires an interactive session; not run by the implementing agent. Headless counts are in HEADLESS-VERBS. |
| AC1-WAIT | n/a | NOT MEASURED — R1 (interactive happy path) was not run: requires an interactive session; not run by the implementing agent. Headless counts are in HEADLESS-VERBS. |
| AC1-ASK | n/a | NOT MEASURED — R1 (interactive happy path) was not run: requires an interactive session (ASK is the interactive-mode verb; headless mode emits PARK); not run by the implementing agent. |
| AC1-PARK | n/a | NOT MEASURED — the R1 interactive cell was not run: requires an interactive session; not run by the implementing agent. Headless PARK counts are in HEADLESS-VERBS. |
| AC2-K1-interrupt | n/a | NOT MEASURED — requires interactive sessions and a human interrupt (R2 cell 1); not run by the implementing agent. |
| AC2-K1-kill | n/a | NOT MEASURED — requires a human process kill (R2 cell 2); not run by the implementing agent. |
| AC2-K2-interrupt | n/a | NOT MEASURED — requires interactive sessions and a human interrupt (R2 cell 3); not run by the implementing agent. |
| AC2-K2-kill | n/a | NOT MEASURED — requires a human process kill (R2 cell 4); not run by the implementing agent. |
| AC3 | Double spawns: 0 of 2 attempts (N=2 attempts, 1 each in R3 and R4), model-in-the-loop, headless only. Per-cell rates: none (R2 cells not run). The scripted loop's repeat behaviour is covered by the unit test `tests/test_flow_driver.py`, not by this row. | Evidence: Flow A and Flow B `spawns.jsonl` each hold exactly 1 line for (skeleton, attempt 1); `driver.log` of each holds exactly 1 SPAWN. The void flow is excluded (its agent was denied before it could write evidence). Per-cell and kill-induced rate: NOT MEASURED as stated in the AC2 rows. |
| AC4a | Headless survival without the foreground WAIT (R3), N=1, `--mode headless --wait-mode end-turn --step-minutes 15`: heartbeat reached its final `done` line, so survival is `>= 15 minutes (censored)`. The `claude -p` session's first turn ended 19:50:33Z and its last turn (after the R5 resume) ended 20:00:03Z; the heartbeat kept appending after both and ended with `done`. | Evidence: Flow A `heartbeat-a1`: 15 loop lines 19:50:26Z to 20:04:26Z at 60 s spacing, one hand-appended resume line at 20:00:00Z, then `done`. Caveat: the heartbeat is a detached shell loop started by the step agent, so this shows that background process outlived the `claude -p` session, not that an agent stayed live. The agent itself finished in seconds. |
| AC4b | Headless survival with the foreground WAIT (R4), N=1, `--mode headless --wait-mode foreground --step-minutes 15`: heartbeat reached `done`, so survival is `>= 15 minutes (censored)`. The event that ended the WAIT was `wait-timeout`, not `wait-settled`: `fleet.py wait` printed `timeout` at 19:59:28Z after its 540 s default, and the driver then decided PARK from disk at 19:59:29Z. The session ended 19:59:34Z. | Evidence: Flow B `driver.log` (`after: wait-timeout`, verb PARK, 19:59:29Z); R4 transcript tool_result `timeout` for the `fleet.py wait` call started 19:50:27Z; Flow B `heartbeat-a1` 15 loop lines 19:50:26Z to 20:04:27Z then `done`. See FLEET-WAIT-STALE for why it timed out. |
| PHANTOM-LIVE | 0 phantom-live cases in 2 completed flows (N=2, Flow A and Flow B); `clear-live` was never needed. One phantom-live-shaped state occurred in the void flow and is reported separately, not counted: see PERMISSION-VOID. | Evidence: Flow A and Flow B `pending_spawn.json` had a matching `spawns.jsonl` line for the live nonce at every `next`; no `clear-live` line in either `driver.log`. Repeated-`next` premise (ADR-0026 section 3): in Flow A and Flow B, `next --after notification` always followed a spawn line, so the premise was not stressed. |
| ORPHAN-OVERLAP | n/a | NOT MEASURED — every flow ran a single attempt (no attempt 2 spawn line), so an earlier attempt cannot overlap a later one; that needs the R2 kill re-runs, which were not run. |
| SPAWN-XCHECK | Agent spawn line vs harness transcript matches in Flow A and Flow B (N=2): 1 `spawns.jsonl` line and 1 subagent transcript file each. In the void flow they disagree (0 lines, 1 transcript), which is itself a finding: the agent-written line undercounts spawns when the agent's first Bash call is denied. | Evidence: Flow A session `d7929551`: `~/.claude/projects/<scratch cwd slug>/d7929551-63be-4cf8-9e48-2625dec8bf2b/subagents/agent-a88829e62e6cff322.jsonl`, 1 `Task` tool_use in the stream. Flow B session `d187eda3`: `.../d187eda3-4a92-4c12-9f24-49abac355a66/subagents/agent-aa1861d4ad0119104.jsonl`, 1 `Task` tool_use. Void flow session `05fdb3e5`: `.../subagents/agent-aea850aa72d528e62.jsonl`, 1 `Task` tool_use, 0 `spawns.jsonl` lines. Transcript paths are for CLI 2.1.322. |
| RESUME-a | n/a | NOT MEASURED — same-session end-turn RESUME is the R1 interactive case; requires an interactive session; not run by the implementing agent. |
| RESUME-b | RESUME after a headless PARK via `claude -p --resume <session>`, N=1: the resumed session received RESUME, `SendMessage` to the original agent id succeeded, the agent wrote `Decision: done`, and the driver reached DONE. The stub knew the id from the resumed session's own conversation history (the original `Task` result), not from disk. The stub first had to load the `SendMessage` tool through `ToolSearch`. The agent had already completed when it was resumed, and the resume still worked. | Evidence: Flow A `driver.log` 19:59:54Z `start` (`--flow`) observed `parked-awaiting-answer` verb RESUME, 19:59:59Z `resume-sent` verb WAIT, 20:00:02Z `notification` verb DONE; R5 transcript `SendMessage` result `resumedAgentId: a88829e62e6cff322`, session id `d7929551-63be-4cf8-9e48-2625dec8bf2b` (same as the original session). A brand-new session without `--resume` was not run, so whether `RESUME FAILED: no agent id in this session` fires there is NOT MEASURED. |
| RESUME-c | n/a | NOT MEASURED — the compaction row: no compaction was run (R6); requires a long interactive session or a human `/compact`; not run by the implementing agent. |
| COMPACTION | n/a | NOT MEASURED — no `/compact` or automatic compaction occurred during R3, R4 or R5 (each session lasted under 10 minutes and under 0.25 US dollars), so the interval between `NEXT:` and the receipt under compaction was not observed (ADR-0026 section 13). |
| LATE-RECEIPT | n/a | NOT MEASURED — a killed attempt's late receipt needs the R2 kill cells, which were not run. |
| HEADLESS-VERBS | Verb emissions per flow from `driver.log`, headless, N=2 flows plus R5. Flow A: SPAWN 1, WAIT 2 (after `spawn-launched`, after `resume-sent`), PARK 1, RESUME 1, DONE 1; resume heartbeat lines 1. Flow B: SPAWN 1, WAIT 1, PARK 1. No verb was emitted in excess of the `--after` events that called for one, and there was no repeated SPAWN or RESUME. This is the headless counterpart of the AC1 rows, not a substitute for the R1 interactive rows. | Evidence: Flow A and Flow B `driver.log` (all lines; `after` values: `start`, `spawn-launched`, `notification`, then for Flow A `answer`, `start`, `resume-sent`, `notification`). |
| FLEET-WAIT-STALE | A real defect in the foreground WAIT path, observed once (N=1): the step agent wrote the awaiting-answers receipt at 19:50:26Z and the stub started `fleet.py wait` at 19:50:27Z. `fleet.py wait` only reports `settled` for a receipt that changed after the call started, so it could not settle and idled the full 540 s default before printing `timeout`. The driver recovered by deciding from disk. The runbook's snapshot-race note describes a torn read; this is a different mode (a receipt that landed before the wait began). | Evidence: Flow B receipt mtime 19:50:26Z (file contents `ATTEMPT: 1` and the awaiting marker); R4 transcript timestamps 19:50:27Z (call) and 19:59:28Z (result `timeout`); `scripts/fleet.py` `wait()` compares the current read against the read at call start. |
| GRANT-SHAPE | The runbook's `--allowedTools` set does not match the real call shapes in this CLI. Quoted-`$HOME` patterns such as `Bash(python3 "$HOME/.claude/scripts/flow_driver/__main__.py":*)` and `...*)` and `..."*)` were each denied for the exact command `python3 "$HOME/.claude/scripts/flow_driver/__main__.py" --help`, while `Bash(python3 *flow_driver/__main__.py *)` was allowed (N=1 probe each, `claude -p` with a one-line prompt). The step agent's spawn-line command is a compound shell command (variable assignment, `echo`, command substitution, a background loop), which the `echo >> ...spawns.jsonl` grant does not match either, so an unrestricted `Bash` grant was used for the measured runs. | Evidence: void flow transcripts (first R3 attempt: driver `start` call denied under the runbook grants; second attempt: step agent's compound call denied under the narrowed Bash grants); probe results: 1 denied event for each of the two quoted forms, 0 for `Bash(python3 *flow_driver/__main__.py *)`. Layer 1 should fix the runbook grants. |
| PERMISSION-VOID | Two void runs, both recorded `NOT MEASURED - permission denied` for survival. Attempt 1 (runbook grants): the stub's `start` call was denied; nothing ran. Attempt 2 (narrowed Bash grants, void flow): the driver and `Task` launch worked, but the step agent's first Bash call was denied, so it wrote no spawn line, heartbeat, question or receipt, and ended. The stub then ran `next --after notification` and got `WAIT` again with a live `spawned` record, no spawn evidence and no receipt. That state matches the phantom-live definition and would stall without `clear-live`; the stub stopped there. | Evidence: void flow `driver.log` (SPAWN 19:48:21Z, WAIT 19:48:25Z, WAIT 19:48:32Z `after: notification`, `observed: spawned`), `pending_spawn.json` state `spawned`, no `spawns.jsonl`; transcript `permission_denied` events (1 in attempt 2). Excluded from every value row above. |

## Cleanup note (S-D5)

Cleanup is manual; no helper exists. After these runs the two temporary symlinks were removed by hand (`rm` of
`~/.claude/commands/expert-flow-v2.md` and `~/.claude/scripts/flow_driver`) and the `skeleton-*` flow dirs under
`~/.claude/flows-v2/` were removed after the evidence was copied out. The harness's own session transcripts under
`~/.claude/projects/` for the scratch runs were left in place, because they are harness-managed and are the
evidence the SPAWN-XCHECK row points at. Anyone repeating the runs must do the same manual cleanup before merge.
