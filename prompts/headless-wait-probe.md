# Headless wait probe

A manual procedure, not part of `just check`. It answers one harness question that decides how step
agents wait for their children (#532): when a subagent running inside a headless `claude -p` session
launches Tasks with `run_in_background: false`, does the call block, run them in parallel, and return
their reports? Re-run it whenever the Claude Code harness changes, and add a dated row to the results
table.

The repo has twice recorded that it does not (the ADR-0018 #222 amendment, and the
`tests/test_expert_flow.py` regression note "nested Agent calls always run in the background"). This
probe checks that again, on the current harness, inside `claude -p`.

## Setup

Run from any directory. Every artifact goes under one throwaway directory.

```bash
PROBE_DIR="$(mktemp -d)/headless-wait-probe"
mkdir -p "$PROBE_DIR/a" "$PROBE_DIR/b"
echo "$PROBE_DIR"
```

Both probes use the same flags. `--allowedTools` grants only what the probe needs, and
`--permission-mode dontAsk` makes anything else fail instead of hang. `Bash(mkdir:*)` is on the list
because a step agent sometimes prefixes its first command with `mkdir -p … &&`; without it that
call is denied and the probe measures nothing (seen twice on 2026-11-23). `stream-json` with
`--verbose` keeps the raw tool results (including any "Async agent launched" acknowledgement) as
evidence. If the report says a Bash call was denied, the run measured nothing: re-run it.

```bash
PROBE_FLAGS=(--model sonnet --output-format stream-json --verbose --permission-mode dontAsk
  --add-dir "$PROBE_DIR"
  --allowedTools "Agent" "Task" "Bash(python3:*)" "Bash(date:*)" "Bash(ls:*)" "Bash(mkdir:*)"
  "Write" "Read")
```

The children sleep with `python3 -c 'import time; time.sleep(N)'`, not `sleep`, because some
harness builds block a foreground `sleep`. A Bash call is capped at 600 s, so Probe B sleeps in two
480 s calls.

## Probe A: three foreground children from a subagent

The top-level session stands in for the orchestrator. It launches one foreground `general-purpose`
subagent that stands in for a step agent, and that subagent launches the three children.

```bash
OUT="$PROBE_DIR/a"
claude -p "${PROBE_FLAGS[@]}" -- "$(cat <<EOF
You are the top level of a harness probe. Do exactly this and nothing else.

Launch ONE Agent call with subagent_type "general-purpose", run_in_background false, and this prompt
(copy it verbatim between the markers):

---BEGIN STEP PROMPT---
You are a step agent in a harness probe. Do exactly these steps, in order. Run each Bash command exactly as written, with nothing added before or after it.
1. Run Bash: date +%s > $OUT/step-before
2. In ONE message, launch THREE Agent calls, each with subagent_type "general-purpose", model "haiku"
   and run_in_background false (if your Agent tool has no run_in_background parameter, launch them
   anyway and note that). Child N (N = 1, 2, 3) gets this prompt, with N filled in:
   "Run this one Bash command with timeout 120000, then reply exactly CHILD N DONE:
    python3 -c 'import time,pathlib; p=pathlib.Path(\"$OUT\"); (p/\"child-N.start\").write_text(str(int(time.time()))); time.sleep(90); (p/\"child-N.end\").write_text(str(int(time.time())))'"
3. As soon as the three Agent calls return, run Bash: date +%s > $OUT/step-after ; ls $OUT
4. Write $OUT/step-report.md with:
   - TOOL_SCHEMA: whether your Agent tool's schema had a run_in_background parameter (yes/no), and
     its exact description text if present.
   - For each child: the first 300 characters of the Agent tool result exactly as you received it.
   - The ls output from step 3.
5. Reply with the contents of step-report.md.
---END STEP PROMPT---

When the Agent call returns, run Bash: date +%s > $OUT/top-after
Then reply with the subagent's report verbatim, followed by the first 300 characters of the Agent
tool result exactly as you received it.
EOF
)" > "$OUT/stream.jsonl" 2> "$OUT/stderr.log"
echo "exit=$?" > "$OUT/exit"
```

Read the result:

```bash
OUT="$PROBE_DIR/a"
for f in step-before step-after top-after child-{1,2,3}.{start,end}; do
  printf '%-14s %s\n' "$f" "$(cat "$OUT/$f" 2>/dev/null || echo MISSING)"; done
cat "$OUT/step-report.md"
grep -o 'Async agent launched[^"]*' "$OUT/stream.jsonl" | head
```

Record:

- (a) **Blocks**: `step-after` is at or after the latest `child-N.end`. If `step-after` comes before
  the children finish, or a `child-N.end` is missing when `step-after` was written, the call did not
  block.
- (b) **Parallel**: the latest `child-N.end` minus `step-before` is about 90 s, not about 270 s.
- (c) **Synchronous report**: the child results in `step-report.md` contain `CHILD N DONE`, not an
  "Async agent launched" acknowledgement.
- (d) **Foreground option offered**: `TOOL_SCHEMA` in `step-report.md`.

## Probe A-flow: the same check with a real flow step agent

Probe A uses a `general-purpose` step agent, which has no flow guard hooks. This variant uses
`flow-implement`, so its PreToolUse hooks (Agent allowlist, Bash guard) are in play. That agent may
only spawn `plan-implementer`, which has no `python3`, so each child stamps the time with `date`
around a few seconds of reading instead of a 90 s sleep. It checks blocking and the synchronous
report; parallelism was already measured by Probe A.

```bash
OUT="$PROBE_DIR/a-flow"; mkdir -p "$OUT"
claude -p "${PROBE_FLAGS[@]}" -- "$(cat <<EOF
You are the top level of a harness probe. Do exactly this and nothing else.

Launch ONE Agent call with subagent_type "flow-implement", run_in_background false, and this prompt
(copy it verbatim between the markers):

---BEGIN STEP PROMPT---
This is a harness probe, not a flow step: do not invoke any Skill and write no receipt.
Do exactly these steps, in order. Run each Bash command exactly as written, with nothing added before or after it.
1. Run Bash: date +%s > $OUT/step-before
2. In ONE message, launch THREE Agent calls, each with subagent_type "plan-implementer", model
   "haiku" and run_in_background false. Child N (N = 1, 2, 3) gets this prompt, with N filled in:
   "This is a harness probe; change no code and commit nothing. Run Bash: date +%s > $OUT/child-N.start
    Then run Bash: ls -R $HOME/.claude/agents , then Bash: date +%s > $OUT/child-N.end
    Then reply exactly CHILD N DONE"
3. As soon as the three Agent calls return, run Bash: date +%s > $OUT/step-after
4. Run Bash: ls $OUT
5. Write $OUT/step-report.md with, for each child, the first 300 characters of the Agent tool
   result exactly as you received it, and the ls output.
6. Reply with the contents of step-report.md.
---END STEP PROMPT---

When the Agent call returns, run Bash: date +%s > $OUT/top-after
Then reply with the subagent's report verbatim.
EOF
)" > "$OUT/stream.jsonl" 2> "$OUT/stderr.log"
echo "exit=$?" > "$OUT/exit"
```

Record: `step-after` is at or after every `child-N.end`, each child result carries `CHILD N DONE`,
the stream has no "Async agent launched", and no hook denial appears in the stream.

## Probe B: one foreground child longer than 15 minutes

```bash
OUT="$PROBE_DIR/b"
claude -p "${PROBE_FLAGS[@]}" -- "$(cat <<EOF
You are the top level of a harness probe. Do exactly this and nothing else.

Launch ONE Agent call with subagent_type "general-purpose", run_in_background false, and this prompt
(copy it verbatim between the markers):

---BEGIN STEP PROMPT---
You are a step agent in a harness probe. Do exactly these steps, in order. Run each Bash command exactly as written, with nothing added before or after it.
1. Run Bash: date +%s > $OUT/step-before
2. Launch ONE Agent call with subagent_type "general-purpose", model "haiku" and
   run_in_background false, with this prompt:
   "Run these two Bash commands one after the other, each with timeout 600000, then reply exactly
    LONG CHILD DONE:
    python3 -c 'import time,pathlib; p=pathlib.Path(\"$OUT\"); (p/\"child.start\").write_text(str(int(time.time()))); time.sleep(480)'
    python3 -c 'import time,pathlib; p=pathlib.Path(\"$OUT\"); time.sleep(480); (p/\"child.end\").write_text(str(int(time.time())))'"
3. As soon as the Agent call returns, run Bash: date +%s > $OUT/step-after ; ls $OUT
4. Write $OUT/step-report.md with the first 300 characters of the Agent tool result exactly as you
   received it, and the ls output.
5. Reply with the contents of step-report.md.
---END STEP PROMPT---

When the Agent call returns, run Bash: date +%s > $OUT/top-after
Then reply with the subagent's report verbatim.
EOF
)" > "$OUT/stream.jsonl" 2> "$OUT/stderr.log"
echo "exit=$?" > "$OUT/exit"
```

Record whether `child.end` exists, `step-after` is at or after it (about 960 s after `step-before`),
and the child result in `step-report.md` is `LONG CHILD DONE`.

## Choosing the branch

- **Branch F** if Probe A shows (a) blocking, (b) parallel and (c) a synchronous report.
- **Branch W** otherwise.
- **Units foreground** only if the result is Branch F and Probe B survived. Otherwise units use a
  background launch plus `wait-files.py`.

The human runs the probe. Inside a flow, the implementer asks for the result through the
flow-reference Ask block and writes it into the table below. It never picks a branch without a
recorded result.

## Recorded decision

**2026-11-23, human-approved: Branch F, units background.** This is the decision later plan steps follow.

- **Branch F** for bounded sites: panel reviewers, scouts, Router, Q&A and other short dispatches
  launch with `run_in_background: false`, in one message, and the step reads their reports from the
  returned results.
- **Units stay background plus `wait-files.py`**: implementer units, the single-unit fallback, the
  long-running sweeps and the background check run. The probe would allow units in the foreground
  (Probe B survived), and this decision declines that on purpose. A foreground call has no
  deadline, so a hung unit would block the step forever and skip the Q4 deadline hard stop, its
  telemetry and the persisted attempt counter. Units are the longest-running and likeliest to hang.
- **Confirmed with a flow step agent (Probe A-flow, 2026-11-23):** a `flow-implement` step agent
  blocked on three foreground `plan-implementer` children and got their reports back, so Branch F
  holds with the flow guard hooks in play. If a later re-run of Probe A-flow does not block, or does
  not return the reports, the decision falls back to Branch W.

## Acceptance

Pending (2026-11-23). The wait primitive and the dispatch-site changes have landed, but the end-to-end
acceptance run has not been done yet. Acceptance is a headless `/expert-flow` (or fleet) run on a real
ticket in which the plan, implement and review steps each return a receipt without ending their turn
to wait, and `child-wait.jsonl` in the flow dir shows `closed` rows for the file-wait phases. Record
it here with the date, the Claude Code version, and the flow dir's `child-wait.jsonl` summary.

## Results

| Date | Claude Code | (a) blocks | (b) parallel | (c) sync report | (d) foreground option | Probe B survives | Branch | Units |
|------|-------------|------------|--------------|-----------------|-----------------------|------------------|--------|-------|
| 2026-11-23 | 2.1.331 | yes: `step-after` 3 s after the last `child-N.end` | yes: children started within 3 s; last end 97 s after `step-before` | yes: 3 × `CHILD N DONE`, 0 "Async agent launched" in the stream | yes: `run_in_background` in the schema; all three launched `false` | yes: child ran 967 s, `step-after` 6 s after `child.end`, `LONG CHILD DONE` returned | F | foreground allowed; background chosen (see Recorded decision) |

Notes for 2026-11-23: top level and step agent on Sonnet, children on Haiku, step agent `general-purpose`
(not a `flow-*` agent, so no flow guard hooks were in play). Probe A's first two runs measured nothing:
the step agent's first Bash call (`mkdir -p … && date …`) was denied, which led to adding
`Bash(mkdir:*)` and the "exactly as written" line. Probe B ran with the earlier flags (without `Bash(mkdir:*)`); its step agent
did not add `mkdir`, so its result stands. Probe B cost about $0.28 and took 996 s wall clock.

This contradicts the earlier repo notes (ADR-0018 #222 amendment; the `tests/test_expert_flow.py`
note "nested Agent calls always run in the background"). One possible reconciliation, not tested here:
those notes saw the default with `run_in_background` omitted, which the schema says is background.
On this harness an explicit `false` blocks.

Probe A-flow, 2026-11-23 (same harness): step agent `flow-implement`, children `plan-implementer` on
Haiku, all three launched with `run_in_background: false`. Blocks: yes, `step-after` 3 s after the last
`child-N.end` (children ran 7–12 s after `step-before`, overlapping). Sync report: yes, each result
carries `CHILD N DONE`, 0 "Async agent launched" in the stream. No hook denial and no tool error in the
stream. The stream does not show whether a hook ran and allowed the call, only that none denied it.
