---
description: "Experimental, not for real tickets: v2 /expert-flow stub that runs one ticket through the Python driver (scripts/flow_driver, ADR-0026) from worktree to ship. Merge is a human gate; --auto-merge is refused."
allowed-tools: Bash(python3 "$HOME/.claude/scripts/flow_driver/__main__.py":*), Bash(python3 "$HOME/.claude/scripts/fleet.py" wait:*), Read, Task, SendMessage
---

experimental, not for real tickets

# Expert Flow v2 (one ticket, worktree through ship)

This stub drives one ticket through the v2 driver at `scripts/flow_driver/`. The steps run in order: worktree, plan,
track, implement, review, fix, verify, ship, then the merge gate. The merge gate always asks the human, healed or not,
and `start` refuses `--auto-merge`. Flows live under
`~/.claude/flows-v2/<repo>/`. v2 never continues a v1 flow and never edits v1 files.

The stub is a dumb executor. The driver decides. The stub runs the command the driver names, reads the directive file it
points to, and obeys it. It never guesses a verb or a path, and it never writes a receipt.

## Rules

- **Run one `driver next` at a time**, never two in parallel. A `locked` refusal means another call holds the flow lock.
  Print the refusal and STOP. Do not retry.
- Only the driver creates or changes `state.json`, `answers/` and `fleet-ask.json`. The stub does not touch them, and
  the stub does not run any command that would.
- A driver call that yields a directive must print exactly one stdout line, `NEXT: <VERB> <absolute directive file>`,
  and exit 0. Accept it only when the line matches that form, the directive file is readable, and the verb is one of
  SPAWN, MERGE, RESUME, WAIT, ASK, PARK, STOP or DONE. Anything else is STOP.
- Every command below begins with `python3 "$HOME/.claude/scripts/flow_driver/__main__.py"`, the prefix the Bash grant
  matches. Run them verbatim, with the flow dir from the invocation.

## Driver subcommands

| Subcommand | Use |
|---|---|
| `init --flow <dir> --worktree <abs path> [--repo OWNER/NAME]` | Create a new v2 flow. Prints `INIT flow=... run_id=...`. `--repo` is needed when the repo name has an underscore. |
| `start --flow <dir> [--gates]` | The first `next` of an initialized flow. `--gates` turns on the step gates (an ASK after plan, implement, review, fix and verify). Also sweeps expired flows of this repo (see Retention). |
| `next --flow <dir> [--after <label>]` | One loop step. With no `--after` it is a re-entry, which is how a later session resumes a parked flow. |
| `answer --flow <dir> --text <answer> [--note <text>]` | Record the human's answer for a parked attempt. Prints to stderr. One answer per attempt. `--note` only for an option that takes a note (Redo, Drop). |
| `recover --flow <dir> --choice retry\|take-over\|keep-previous\|resume` | Answer a stopped step. `retry` works only after a FAILED receipt or an audit rejection and at most twice per step. `resume` re-asks a step gate the human paused or a merge they held. `keep-previous` is refused in Layer 2. |
| `clear-live --flow <dir> --reason <text>` | Clear a live record, logged with its nonce. Only for a `wait-timeout` or `resume-failed` STOP. |
| `abandon --flow <dir>` | Mark the flow abandoned. Refused while an attempt is `spawned` or `resumed`, and the refusal names `clear-live`. Deletes nothing. |
| `retro --flow <dir>` | Not built for v2: it exits 3 (`not-yet-built`). Refused for v1 flows. |
| `status --json --flow <dir>` | Read-only JSON of the flow. |
| `list [--repo OWNER/NAME]` | Read-only. One row per flow: `owner<TAB>path<TAB>step<TAB>status`. v1 rows show `v1`. |

Exit codes: 0 is success. 1 is a refusal, with the reason on stderr. 2 is a usage error, such as a flow path outside
both roots. 3 is not-yet-built. Any non-zero exit is STOP: print stderr and end.

Invocation flags map to subcommands like this. `--list` is `list`. `--abandon` is `abandon --flow`. `--retro` is
`retro --flow`. `--recover <choice>` is `recover --flow --choice`. `--flow <dir>` with no other verb is a re-entry,
`next --flow <dir>`. `--auto-merge` is passed to `start`, which prints `STOP auto-merge-refused: ...`, exits 1, and
creates no state. Print that and end. Any other flag is STOP.

## Start

A new ticket runs `init` with the flow dir and the absolute worktree path from the invocation, then `start`:

    python3 "$HOME/.claude/scripts/flow_driver/__main__.py" init --flow <dir> --worktree <abs worktree>
    python3 "$HOME/.claude/scripts/flow_driver/__main__.py" start --flow <dir>

Accept the `NEXT:` line from `start`, read its directive file, and take the action for its verb below.

## Verbs

After each event, run the directive's `next_command`, with ` --after <label>` appended. Each action names its label.

- **SPAWN**: launch `Task` in the background with `subagent_type` set to the directive's `agent_type` and the prompt
  "Read and follow <prompt_file>". Keep the agent id this session gets for this step and attempt. Then run
  `next --after spawn-launched`.
- **MERGE**: launch `Task` exactly as for SPAWN (`subagent_type` from the directive's `agent_type`, the prompt from its
  prompt file). Never run MERGE without a directive file the driver printed.
- **RESUME**: `SendMessage` to the agent this session spawned for this step and attempt, saying the answer is at the
  directive's `answer_file`. On success run `next --after resume-sent`, which yields WAIT. If this session does not hold
  that agent id, do not guess: run `next --after resume-failed`, then print `RESUME FAILED: no agent id in this session`
  and end.
- **WAIT, end-turn**: end the turn. The completion notification wakes the stub, which runs `next --after notification`.
- **WAIT, foreground**: run `python3 "$HOME/.claude/scripts/fleet.py" wait <receipt_file>`. It prints `settled` when the
  receipt holds a Decision line or the `<!-- awaiting-answers:` marker, and `timeout` otherwise. Run
  `next --after wait-settled` on `settled` and `next --after wait-timeout` on `timeout`. A timeout never means success;
  the driver decides from disk.
- **ASK**: print the question file contents, the `answer_file` path and the `answer_command`, then end the turn. On the
  human's next message, run `answer_command` with the human's text in place of `<your answer>`, then run
  `next --after human-continue`.
- **PARK**: print the same three things and end the session. A later session re-enters with `next --flow <dir>`.
- **STOP**: print the reason from the directive file and end. See Recovery for `step-failed`.
- **DONE**: print a line that names the flow dir, and end.

## Recovery

A FAILED step receipt is STOP `step-failed` and nothing more. Layer 2 does not retry it on its own. The human chooses:

- `recover --flow <dir> --choice retry`, then `next --flow <dir>` (a new attempt, at most two retries per step), or
- `recover --flow <dir> --choice take-over`, which leaves the step to the human.

Stop and print the directive's reason for any other STOP, including `ship-head-differs`,
`ship-pr-closed`, `expected-head-mismatch`, `north-star-preflight`, `hook-preflight`, `uncarried-direction`,
`ledger-error`, `gh-error`, `retry-cap`, `malformed-cap` and `wait-timeout`. Do not work around any of them.

## Receipt and markers

The step prompt the driver writes tells the step to write its receipt and marker, then end its turn. The driver reads
the receipt on the next call. The stub only waits, through the driver. It never writes a receipt.

## Retention

`start` removes finished flows of this repo (status `done` or `abandoned`) whose `updated` stamp is more than 30 days
old, and their whole flow directories. It never touches a running flow, the flow it was asked to start, or any v1 flow.
`list` and `status` never delete anything.

## Known gaps

- The Ask block in `prompts/flow-reference.md` resolves `FLOW_DIR` empty under `flows-v2`. The stub does not use it.
  ASK and PARK instead point at driver-written question and answer paths. Do not follow any Ask block from that file.
- Retry, redo, the base-stale rebase, the flow-healer heal and the Unknown-skill retry are not wired into the loop.
  A failed step stops; see Recovery.
- Fleet behaviour is a later layer (ADR-0026). `--auto-merge` is not supported, by design.
