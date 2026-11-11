# Walking Skeleton Runbook: The Measurement Procedure

This runbook describes how to run the v2 driver walking skeleton in interactive and headless modes,
collect evidence, and record the results that gate Layer 1 (#485). The runs produce
`skeleton-findings.md`.

## Setup (S-W3)

The human runs these from a scratch directory that has no `.claude/flow-run` marker.

1. Create two temporary file-level symlinks into this worktree:
   - `~/.claude/commands/expert-flow-v2.md` → `<worktree>/commands/expert-flow-v2.md`
   - `~/.claude/scripts/flow_driver` → `<worktree>/scripts/flow_driver`

2. Record when the links were created.

3. Do **not** use `./install.sh --from-worktree`. Create the links manually, as above.

4. After all runs complete, remove both symlinks and record when they were removed. The removal must
   be verified before any merge.

## Run Matrix (S-Q3 B)

Each cell runs a fixed N (default 3 per S-Q3 B: "small fixed N"). The actual N is recorded per row,
so a smaller run is labelled as such rather than padded.

### R1: Interactive Happy Path

- **N = 3** (or actual count if fewer)
- **Step duration: short** (60 seconds recommended)
- **Purpose**: Covers exactly-once execution, one park/answer cycle, and RESUME in the same session

**Pre-check**: Before running the main R1 loop, verify that heartbeats arrive about every 60 seconds
in interactive mode. This baseline is needed to detect if later stalls are due to a kill vs. agent
or harness tooling.

**Procedure**:
1. Start a fresh interactive session with `start --repo <OWNER/NAME> --mode interactive --wait-mode end-turn --step-minutes 1`
2. On the SPAWN directive, wait for the agent to start and the `spawns.jsonl` line to appear
3. When a question appears, record that a question was asked (R1 evidence: ASK or PARK)
4. Supply the answer using the `answer_command` in the same session
5. When RESUME occurs, record the result (R1 evidence: RESUME provenance, row a: same-session end-turn)
6. Verify the heartbeat file reaches its final `done` line
7. When DONE is printed, record the flow dir path
8. Repeat for N = 3 runs

**Evidence to collect**: flow dir path, `driver.log`, `spawns.jsonl`, `heartbeat-a*`, heartbeat count,
model version, date, mode.

### R2: Kill Cells

- **N = 3 per cell** (or actual count if fewer)
- **Four cells**: interrupt or process kill × K1 or K2
- **Step duration: short** (60 seconds recommended)
- **K1**: interrupt or kill as soon as `NEXT: SPAWN` appears, before any `spawns.jsonl` line
- **K2**: interrupt or kill after the `spawns.jsonl` line, before the receipt
- **Purpose**: Measure double-spawn from kill re-runs and identify phantom-live cases

**For each of the four cells (R2.1, R2.2, R2.3, R2.4)**:
1. Start an interactive session and let the agent begin
2. At the configured kill point (K1 or K2), interrupt the session (Ctrl+C) or kill the process
3. Record which session re-ran it (same session via `start --flow` or a new session)
4. Check for `phantom-live`: if `next` emits WAIT with no receipt, run `clear-live` to recover
5. Record whether `clear-live` was needed (phantom-live is present if yes)
6. Verify the receipt's fate: did a late receipt from the killed attempt overwrite the per-step
   receipt?
7. Record the re-run session's ID

**Evidence to collect per cell**: flow dir, kill kind (interrupt/kill) and point (K1/K2), re-run
session ID, whether `clear-live` was needed, late-receipt overwrite result, `driver.log` entries at
the kill point and re-run point.

### R3: Headless with End-Turn WAIT

- **N = 3** (or actual count if fewer)
- **Flags**: `--mode headless --wait-mode end-turn --step-minutes 15`
- **Purpose**: Survival run with background heartbeating, without the foreground WAIT call

**Procedure**:
1. Start a headless session: `claude -p --allowedTools <grant-set>` (see Headless permissions below)
2. Run the stub with the flags above
3. Supply the answer via the `answer_command` in a separate message
4. Observe the heartbeat file during the 15+ minute wait
5. Record the final heartbeat timestamp and the session end time
6. Calculate survival as the interval between the last heartbeat and session end
7. If the heartbeat reaches the final `done` line, record as `>= step_minutes (censored)`

**Evidence to collect**: flow dir, heartbeat count, survival time (censored or interval),
`allowedTools` grant used or permission mode, model version, date.

### R4: Headless with Foreground WAIT

- **N = 3** (or actual count if fewer)
- **Flags**: `--mode headless --wait-mode foreground --step-minutes 15`
- **Purpose**: Survival run using `fleet.py wait` explicitly; record the event that ended each WAIT

**Procedure**:
1. Start a headless session with the same permission setup as R3
2. Run the stub with the flags above
3. Supply the answer via the `answer_command` in a separate message
4. Record which event ended each WAIT: `wait-settled`, `wait-timeout`, or other (noted in log)
5. Observe the heartbeat file during the wait
6. Record the final heartbeat timestamp and the session end time
7. If the heartbeat reaches the final `done` line, record as `>= step_minutes (censored)`

**Evidence to collect**: flow dir, heartbeat count, survival time (censored or interval), which event
ended the WAIT, `allowedTools` grant used or permission mode, model version, date.

### R5: RESUME After a Park in Headless Mode

- **Purpose**: Measure RESUME provenance after a headless park (row b of RESUME evidence)

**Procedure**:
1. Start a headless session (same permission setup as R3 and R4)
2. Run the stub, supply the answer, and let it park (PARK verb, not ASK)
3. In a new session or the same one, call `start --flow <flow dir>` to resume
4. The stub will RESUME the agent
5. Record the resuming session's ID

**Evidence to collect**: flow dir, original session ID, resuming session ID, RESUME provenance.

### R6: Compaction

- **Status: NOT MEASURED unless compaction happens**
- **Optional**: Running `/compact` during an R3, R4, or R5 park is optional (out of scope, Layer 1)
- **If run**: Record the interval between `NEXT:` line and receipt, and timestamp the compaction

**Evidence to collect** (if applicable): flow dir, interval, compaction timestamp.

## Headless Permissions (R3–R5)

The `claude -p` command cannot answer permission prompts, so an unmatched tool call is denied and
looks like an agent death. For R3, R4, and R5:

**Option 1: Explicit `--allowedTools` set**

Pass `--allowedTools` covering the stub's Bash grant and the `general-purpose` agent's `Read`,
`Write` and `Bash` use under `~/.claude/flows-v2/`:

```
--allowedTools \
  'Bash(python3 "$HOME/.claude/scripts/flow_driver/__main__.py":*)', \
  'Bash(python3 "$HOME/.claude/scripts/fleet.py" wait:*)', \
  'Bash(cd ~ && mkdir -p):*' \
  'Read' \
  'Write(~/.claude/flows-v2/*:*)' \
  'Task'
```

**Option 2: Permission mode**

Run with `--permissionMode no-prompt` or equivalent, and handle denials as appropriate.

**Void condition**: If any tool call is denied (appears in the transcript), mark that run as
`NOT MEASURED - permission denied`.

Record the exact `--allowedTools` flags or permission mode used in each row's provenance.

## Evidence to Collect Per Run

For every run (R1–R5), collect these files and facts:

1. **Flow directory**: absolute path (will be scrubbed to `<worktree>` or `~/.claude/flows-v2/…`)
2. **`driver.log`**: append-only JSONL, contains every transition and verb
3. **`spawns.jsonl`**: agent's evidence line: `{nonce, step, attempt, utc, prompt_file}`
4. **`heartbeat-a*` file**: one per attempt, timestamped lines with final `done`
5. **Subagent transcript count**: number of transcript files for this session under the harness's
   project transcript directory
6. **CLI version**: output of `claude --version`
7. **Model name**: recorded in session info
8. **Date and time**: UTC timestamps of run start and end
9. **Mode**: `interactive` or `headless`
10. **Human notes**: recorded in `human-notes.md` within the flow dir (kill kind, kill point for R2,
    observed stall, permission mode)

Cross-check: compare the agent's `spawns.jsonl` line with the harness transcript (via CLI version
stamp) for evidence of a spawn.

## Definitions Used in Findings

### Double Spawn

More than one spawn-evidence line (agent's `spawns.jsonl` entry) for the same `(step, attempt)` pair.
Counted per verb in row AC1; aggregated rate in row AC3.

### Orphan Overlap

An earlier attempt still heartbeating (append to `heartbeat-a<M>`) after a later attempt's spawn
line (`spawns.jsonl` for `a<N>` where N > M). Indicates a heartbeat loop that did not terminate.

### Phantom-Live

A live record (`pending_spawn.json` shows `spawned` or `resumed` state) with no corresponding spawn
evidence in `spawns.jsonl` for the current attempt. Recovery requires running `clear-live`, which is
logged.

### Survival

The time interval between the last heartbeat line (UTC timestamp) and the end of the session or
turn. A run whose heartbeat file reaches the final `done` line is recorded as `>= step_minutes
(censored)`, never as a point value. A run that does not reach `done` records the actual interval in
seconds.

### Repeat, Per Verb

- **SPAWN**: Double spawn, as above.
- **RESUME**: More than one resume evidence line per attempt (the agent's heartbeat after a
  `SendMessage` indicating answer availability).
- **WAIT**: `driver.log` emissions of WAIT in excess of the `--after` events that called for one.
  (E.g., if `next --after spawn-launched` triggers a WAIT, then re-running `next` with a live
  record and no receipt should not emit another WAIT.)
- **ASK or PARK**: `driver.log` emissions of ASK or PARK in excess of the `--after` events that
  called for one.
- **Any verb with no counted evidence**: `NOT MEASURED`.

## Cleanup (S-D5)

Manual removal is required; no cleanup helper is provided.

1. Remove skeleton flow dirs: `rm -rf ~/.claude/flows-v2/<repo_key>/skeleton-*`
2. Remove the two temporary symlinks: `rm ~/.claude/commands/expert-flow-v2.md` and `rm
   ~/.claude/scripts/flow_driver`
3. Verify removal in `human-notes.md` before merge

## Scrubbing

Before pasting any excerpt from the runbook results into `skeleton-findings.md`, scrub:

- Replace personal absolute paths with `~` (e.g., `/Users/alice/` → `~`)
- Replace worktree paths with `<worktree>` (e.g., `/home/alice/src/claude-helpers/worktrees/…` →
  `<worktree>`)
- Remove private project names and replace them with "a private project" or a neutral placeholder
- Keep issue numbers and dates as written

## References

- Decisions: S-W1, S-W2, S-W3, S-Q1, S-Q2, S-Q3, S-Q4, S-D1, S-D2, S-D3, S-D4, S-D5
- ADR-0026 §13 (compaction item), ADR-0026 §8 (pending state)
- Plan Step 8
