---
description: "Experimental, not for real tickets: walking-skeleton stub of the v2 /expert-flow driver boundary (ADR-0026, Sub-issue S). Runs one trivial step through scripts/flow_driver so a human can measure the park and answer cycle. Has no ship, merge, push or PR step."
allowed-tools: Bash(python3 "$HOME/.claude/scripts/flow_driver/__main__.py":*), Bash(python3 "$HOME/.claude/scripts/fleet.py" wait:*), Read, Task, SendMessage
---

experimental, not for real tickets

# Expert Flow v2 (walking skeleton)

This stub runs the v2 driver boundary for one trivial step. It is a dumb executor: the driver decides, the stub
obeys the directive file the driver names, and the stub never decides anything itself. The driver lives at
`scripts/flow_driver/`. Every flow it writes goes under `~/.claude/flows-v2/`. v2 never reads or writes the v1
`~/.claude/flows/` tree.

The stub has no ship, merge, push or PR step. It does not use the Ask block in `prompts/flow-reference.md`,
because that block resolves `FLOW_DIR` empty under `flows-v2`. ASK and PARK instead point at driver-written question
and answer file paths. Do not follow any Ask block from that reference.

## Accept rule

Every driver call prints exactly one stdout line, `NEXT: <VERB> <absolute directive file>`, and exits 0. Accept the
result only when all of these hold: exactly one stdout line, the line matches `NEXT: <VERB> <absolute path>`, the
exit code is 0, and the directive file is readable. Anything else is STOP. An unknown verb is STOP. Never guess a
verb or a path.

## Start

Run `start` with the invocation's arguments. The driver validates every argument:

    python3 "$HOME/.claude/scripts/flow_driver/__main__.py" start $ARGUMENTS

Accept the result, read the directive file it names, and take the action for its verb below. If the result is STOP,
print the reason from the directive file and end.

## Loop

After each event, run the `next_command` read from the current directive file, with ` --after <label>` appended.
The label is named in each action below. Every directive's `next_command` already holds the absolute flow dir.

## Verbs

- **SPAWN**: launch `Task` in the background with `subagent_type` set to the directive's `agent_type` and the
  prompt "Read and follow <prompt_file>". Keep the agent id this session gets for this step and attempt. Then run
  `next --after spawn-launched`.
- **RESUME**: `SendMessage` to the agent this session spawned for this step and attempt, saying the answer is at the
  directive's `answer_file`. On success, run `next --after resume-sent`, which yields WAIT. If this session does not
  hold that agent id, do not guess: run `next --after resume-failed`, then print
  `RESUME FAILED: no agent id in this session` and end.
- **WAIT, end-turn**: end the turn. The completion notification wakes the stub, and it then runs
  `next --after notification`.
- **WAIT, foreground**: run `python3 "$HOME/.claude/scripts/fleet.py" wait <receipt_file>`. It prints `settled` when
  the receipt holds a Decision line or the `<!-- awaiting-answers:` marker, and `timeout` otherwise. Run
  `next --after wait-settled` on `settled` and `next --after wait-timeout` on `timeout`. A timeout never means
  success: the driver decides from disk.
- **ASK**: print the question file contents, the `answer_file` path and the `answer_command`, then end the turn. On
  the human's next message, run `next --after human-continue`.
- **PARK**: print the same three things and end the session. A later session re-enters with the same invocation
  plus `--flow <flow dir>`.
- **STOP**: print the reason from the directive file and end.
- **DONE**: print a done line that names the flow dir, and end.

## Receipt

The step prompt the driver writes tells the step to write its receipt and the
`<!-- awaiting-answers: skeleton-a<N> -->` marker, then end its turn. The stub only waits on that marker through
the driver. The stub never writes a receipt itself.

## Matching form

Every `next_command` and `answer_command` begins with `python3 "$HOME/.claude/scripts/flow_driver/__main__.py"`,
which is the prefix the Bash grant above matches. Run them verbatim.
