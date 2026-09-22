---
name: save-usage
description: Save a pasted /usage (or /cost) panel block, tagged with a label, to a local usage log for later cost comparison across runs.
model: haiku
---

# Save Usage

Thin wrapper around `scripts/save-usage.py` — a deterministic parser, no LLM judgment. It never
computes, estimates, or reformats numbers itself; all parsing and arithmetic happen in the script.

## Usage

Run `/usage` (or `/cost`) yourself, copy its output, then invoke this command with optional
label as the first line and the pasted panel text after it:

```
/save-usage expert-plan-v3-effort2-issue184
Settings  Status   Config   Usage   Stats
Session
Total cost:            $1.06
Total duration (API):  3m 27s
Total duration (wall): 1h 19m 46s
Total code changes:    0 lines added, 0 lines removed
Usage by model:
    claude-sonnet-5:  4.2k input, 14.1k output, 2.8m cache read, 102.3k cache write ($1.05)
    claude-haiku-4-5:  925 input, 18 output, 0 cache read, 0 cache write (0.10¢)
```

The label is optional and should identify what this session's usage was for (command, effort
level, ticket) — it makes the log queryable later. If you omit it, the script generates a label
automatically by joining every distinct slash command run in the session (when detectable via
telemetry, e.g. `expert-plan+expert-review`) with the branch and timestamp, falling back to
repo/branch/timestamp if no command was detected, or just timestamp if neither is available.

**Caveat, always worth restating to the user once:** the `/usage` panel is scoped to the whole
session, not to a single command. If other work happened in the same session before you paste,
the saved numbers include that too. For clean per-command data, run the command to measure in a
fresh session, then paste immediately.

The label is a best-effort guess, not the source of truth — a session that ran several commands
still produces one label built from all of them, but the label alone can't tell you which command
actually drove the cost. For that, read the record's structured fields directly: every record
captures `session_id` (from `CLAUDE_CODE_SESSION_ID`) and `commands` — the full list of
command.begin events from this session, each with its `model`/`effort` override when one was
recorded — plus `worktree` (the repository's directory basename), independent of the label. Note
that `session_id` is `null` and `commands` is `[]` when telemetry was not available; callers
should check for these before relying on these fields.

## Steps

Pass `$ARGUMENTS` straight through via `printf '%s'` (never `echo` — see CLAUDE.md's
printf-not-echo convention, since pasted panel text can contain characters `echo` would
reinterpret):

```bash
printf '%s' "$ARGUMENTS" | python3 "$HOME/.claude/scripts/save-usage.py"
```

Show the script's own output verbatim — it already prints the parsed label, total cost, and
per-model costs, and confirms the write path (`~/.claude/telemetry/usage-log.jsonl`). Do not
recompute or restate the numbers yourself.

If the script errors (no `Total cost:` line found — i.e. the paste doesn't look like a usage
panel), show its stderr message as-is and stop; do not retry or guess at the missing fields.

## Notes

- **Opt-in and manual only** — never invoked automatically by another command.
- **Read-only with respect to everything except its own log file** — appends one JSON line to
  `~/.claude/telemetry/usage-log.jsonl`, touches nothing else.
- **Reads but never writes `run-metrics.py`'s `events.jsonl`** — save-usage reads it only to detect
  the slash commands run in the session for the auto-generated fallback label (see Usage section above); it still maintains its own separate,
  human-curated log (`usage-log.jsonl`) and never writes to `events.jsonl`. Query `usage-log.jsonl`
  directly with `jq`/`python` (e.g. `jq -s 'group_by(.label)' ~/.claude/telemetry/usage-log.jsonl`)
  the same way `usage-report.md` documents querying `events.jsonl` — no built-in aggregation command
  exists yet, and one shouldn't be built speculatively before there's enough logged data to need it.
