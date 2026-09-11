# Spike: subagent token telemetry + step/compact pausing (2026-09-09)

Interactive spike, run directly in-conversation per the plan (not via `/implement-with-haiku`).
Goal: characterize whether real-time cumulative subagent token tracking and hook-based
pause/halt behavior are actually achievable, to unblock two follow-ups requested by the user —
(a) a token-usage gate for long commands, (b) pause-for-confirmation at named steps.

Method: a scratch `--settings` override (never touching `~/.claude/settings.json`) wired
`SubagentStart`/`SubagentStop`/`PreCompact` to a logging shim that dumps raw hook stdin JSON to
the session scratchpad, then a throwaway `token-gate-hook.py` prototype. Both were run against
nested, non-interactive `claude -p` invocations (`--permission-mode bypassPermissions`) in a
throwaway scratch project directory, spawning trivial synthetic subagents (2-3 `general-purpose`
agents doing one-line tasks) rather than a real `/implement-with-haiku` run.

## 1. `transcript_path` is shared; `agent_transcript_path` is per-agent (and it's real)

`SubagentStart` and the top-level `transcript_path` field on `SubagentStop` are the **parent
session's** transcript — identical across all concurrent subagents in a run (confirmed with two
subagents spawned in parallel: same `session_id`, same `transcript_path`, only `agent_id`
differed). This matches the plan's structural concern.

But `SubagentStop` carries a **second, distinct field** the plan's research hadn't surfaced:
`agent_transcript_path`, e.g.
`.../<session_id>/subagents/agent-<agent_id>.jsonl`. This file:

- exists on disk (verified), is per-agent, and is **not shared** across concurrent subagents.
- parses cleanly with the existing `scripts/claude-transcript-metrics.py parse --transcript
  <agent_transcript_path> --session-id <sid> --agent-id <aid>` CLI, unmodified, e.g.:
  `{"tokens": {"cache_creation": 21111, "cache_read": 19540, "input": 4, "output": 65}, "turns": 2, ...}`

**This resolves the plan's central open question in the favorable direction**: real per-subagent
token accounting is possible without reimplementing transcript parsing — reuse
`claude-transcript-metrics.py` as designed, keyed off `agent_transcript_path` from the
`SubagentStop` payload, not the shared `transcript_path`.

Caveat: `agent_transcript_path` is only present at `SubagentStop`, not `SubagentStart` — so
per-subagent accounting is necessarily post-hoc (after that subagent finishes), not visible
mid-flight while it's still running.

## 2. `PreCompact` blocking: confirmed real, verbatim message

No prior art existed for this in the repo. Confirmed directly: a `PreCompact` hook exiting 2
produces `Compaction blocked by PreCompact hook: [<command>]: <stderr text>` and compaction does
not proceed (exit code 0 at the CLI level; the block is reported as output, not a crash). A
control run (hook exits 0) proceeds to the normal "not enough messages to compact" path. This was
tested against **manual** compaction (`/compact`, `"trigger": "manual"` in the payload) only —
auto-compaction wasn't tested because `--autocompact` only accepts a 100k–1M-token floor, too
expensive to force deliberately for this spike.

## 3. `SubagentStop` exit 2 does **not** halt execution — it's advisory, not a gate

This is the most consequential finding, and it's a negative result for the token-gate design in
the plan's step 4. Wired a `token-gate-hook.py` prototype (reusing `claude-transcript-metrics.py`
per finding 1, cumulative per-`session_id` state, exits 2 once the running total crosses a 5,000
token test threshold) to `SubagentStop` and ran it live against a synthetic 3-subagent task.

Result: the gate correctly fired (state file shows cumulative 55,180 tokens against the 5,000
threshold, well past it) — but **the orchestrator's run was not halted**. All three subagents
completed, results were reported normally, and the orchestrator model merely noted in its final
reply that "two of the agents flagged a local prototype... it didn't block their answers." Exit 2
on `SubagentStop` blocks the *stop transition for that already-finished subagent's turn* and
surfaces the stderr as context to the model — it does not abort the orchestrator, prevent further
subagent spawning, or halt anything user-visibly. This is consistent with (previously
undocumented in this repo) Stop-family hook semantics: exit 2 says "don't stop, here's why,"
which reads as a nudge to keep working, not a kill switch.

**Implication**: a `SubagentStop`-based token gate cannot implement "halt above threshold" as the
user described it. `PreCompact`'s block is real because compaction is a discrete operation the
hook gates; `SubagentStop`'s block is not, because "stopping" here means the subagent's own turn
ending, not the orchestrator's overall execution.

## 4. Side effect: exit-2 in one hook re-fires the *entire* event's hook chain

Because `--settings` merges with (doesn't replace) the real `~/.claude/settings.json`, both the
real `run-metrics.py agent-end` hook and the scratch `token-gate-hook.py` were registered for
`SubagentStop` simultaneously. The real telemetry log (`~/.claude/telemetry/events.jsonl`) shows
**duplicate `agent.end` events per agent** for the test session — one with a real
`elapsed_seconds`, one with `elapsed_seconds: "unknown"` (because the second pass found the
state-machine's `began_at` already popped by the first).

Root cause: when `token-gate-hook.py` exits 2 (blocking the stop), Claude Code appears to retry
the entire `SubagentStop` hook chain for that event, not just the blocking hook — so
`run-metrics.py agent-end` ran twice per agent. The prototype's own `agents_seen` idempotency
guard (dedup by `agent_id` in its own state) absorbed this correctly on its own side, but this is
a real, reproducible hazard for **any** future hook added to an event that already has a blocking
hook attached to it: it will silently double-fire and must dedupe defensively. Worth flagging
back into `run-metrics.py`/ADR-0016 even though a token gate isn't being productionized from this
spike — the duplication happens regardless of what the blocking hook's own purpose is.

## 5. Concurrency-safety gap (ADR-0016's deferred item): narrower than the plan's research suggested

The plan's opening research (accurate as written) says the state file is "keyed by session_id
only... a single slot per session," race-prone under concurrent workers. Reading
`telemetry_schema.py`'s `load_and_update_state` shows this needs a correction: the state file
**is** a single file per session, but reads/writes go through `fcntl.flock(fd, LOCK_EX)` for a
genuine atomic read-modify-write, and the in-file structure is already `agents[agent_id] = {...}`
— a dict keyed by `agent_id`, not a single overwritten slot. The two-subagent live run's
`events.jsonl` shows both agents' `agent.begin`/`agent.end` pairs recorded distinctly and
correctly (setting aside the unrelated duplication from finding 4). So concurrent-subagent
correlation **is** already lock-protected at the state-file layer — ADR-0016's deferred
verification item can be marked resolved for the specific race it worried about (state-file
clobbering). The duplicate-event hazard in finding 4 is a *different*, previously-undiscovered
issue, orthogonal to the one ADR-0016 flagged.

## 6. `token-gate-hook.py` prototype status

Working as a *cumulative token counter* (correctly reuses `claude-transcript-metrics.py`,
correctly sums across concurrent agents via a locked-equivalent... actually unlocked — see below —
per-session JSON state file, correctly dedupes by `agent_id`). **Not working** as a *halt
mechanism*, per finding 3 — it cannot stop an in-progress `/implement-with-haiku`-style run. Left
at `scratch/token-gate-hook.py` in the session scratchpad only; not installed anywhere, not
committed.

One additional note on the prototype itself: unlike `telemetry_schema.py`'s `load_and_update_state`,
this throwaway script does a plain (unlocked) read-modify-write of its own JSON state file — fine
for a spike with hooks firing sequentially in this test, but would need the same `fcntl.flock`
treatment before any real use to avoid the exact race ADR-0016 originally worried about.

## Go/no-go recommendations

**(a) Real per-subagent token accounting in `/implement-with-haiku`**: **go**, with a scoped
design change from what the plan assumed — instrument `SubagentStop` (not `SubagentStart`), read
`agent_transcript_path` (not `transcript_path`), reuse `claude-transcript-metrics.py` unmodified,
and accept that any total is necessarily reported *after* the fact per-subagent, not live
mid-flight. This is a straightforward extension of the existing telemetry system. If the intent is
still literally "halt execution once cumulative usage crosses N tokens," that requires a mechanism
other than a `SubagentStop` hook exit code — e.g. the orchestrator command's own prompt checking a
running total after each round and choosing not to launch the next round (an in-band check, not a
hook-based one), since no tested hook here can reach in and stop an already-dispatched subagent
fan-out.

**(b) Prompt-level pause-for-confirmation at named steps**: **go**, unchanged from the plan's own
prior conclusion — this needs no hook at all, since hooks can't see custom-command step semantics.
It's `AskUserQuestion`/explicit stop-and-report calls added directly to
`commands/implement-with-haiku.md` (or any other step-based command) at the desired checkpoints.

## Non-goals honored

No changes to `~/.claude/settings.json`, `commands/implement-with-haiku.md`, or
`scripts/run-metrics.py`. All scratch artifacts (settings overrides, hook shim, token-gate
prototype, synthetic scratch project) live under the session scratchpad, not committed. This
document is the only committed artifact from the spike.
