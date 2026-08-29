# Telemetry: Usage & Yield Measurement

## Purpose

This repo ships optional **local, observational telemetry** to answer real cost and yield questions with evidence instead of overlapping percentage claims. All telemetry is recorded to a single-machine JSONL log (`~/.claude/telemetry/events.jsonl`); **it never leaves your machine and is never synced**.

The privacy boundary is hard: telemetry records only metadata (repo name, command name, timestamps, token counts, outcomes, correlation IDs). It never records prompts, source code, diffs, issue bodies, or credentials. Repo and command names are recorded in plaintext since this data never leaves your machine.

## Event Model

Telemetry consists of 8 event types, forming a 4-level hierarchy via correlation IDs:

### Event Types

1. **`session.begin` / `session.end`** — lifecycle of a Claude Code session
2. **`command.begin` / `command.end`** — lifecycle of a custom slash command (e.g., `/expert-review`)
3. **`stage.begin` / `stage.end`** — lifecycle of an internal phase within a command (e.g., "router", "pass1", "implementation")
4. **`agent.begin` / `agent.end`** — lifecycle of a spawned subagent (e.g., expert-reviewer, plan-implementer)

### Correlation IDs (Foreign Keys)

- `session_id` (always required) — UUID identifying the session
- `command_id` (FK `session_id`, optional) — UUID identifying a command; `command.begin/end` and their stages/agents all carry this
- `stage_id` (FK `command_id`, optional) — UUID identifying a stage; `stage.begin/end` carry this
- `agent_id` (FK `session_id`, optional) — UUID identifying a spawned agent; `agent.begin/end` carry this

### Outcome Tagged Union

Every `*.end` event carries an `outcome` field with one of three shapes:

```json
{"status": "success"}
{"status": "failure", "class": "timeout|api_error|test_failure|guard_block|other"}
{"status": "interrupted"}
```

**Important:** an event whose matching `*.end` never arrives is never silently dropped and never
inferred as a zero-cost success. Today, `run-metrics.py diagnose` surfaces this as an unmatched
begin lowering the reported match rate (see "Telemetry Health Check" below) — it does not (yet)
synthesize an `interrupted` outcome record for the orphaned begin. `outcome: {"status":
"interrupted"}` is a value a writer can emit explicitly (e.g. a future crash-detection pass); a
reader-side reconciliation step that retroactively marks stale begins as `interrupted` is
deferred to a future phase.

## Field Reference

Every event emitted by `run-metrics.py` carries these fields:

### Always Present

- `schema_version` (integer) — currently `1`
- `event_type` (string) — one of the 8 types above
- `timestamp` (ISO 8601 string) — UTC time the event was recorded
- `session_id` (string) — session UUID or `"unknown"` (defaults from `CLAUDE_CODE_SESSION_ID` environment variable when not explicitly passed via CLI flag)
- `turns` (integer or string) — number of conversation turns; defaults to literal string `"unknown"` when not captured
- `elapsed_seconds` (integer or string) — wall-clock elapsed time; defaults to literal string `"unknown"` when not captured
- `retries` (integer or string) — number of retries; defaults to literal string `"unknown"` when not captured
- `peak_concurrency` (integer or string) — peak number of concurrent tasks; defaults to literal string `"unknown"` when not captured
- `transcript_size` (integer or string) — size of transcript output artifact in bytes; defaults to literal string `"unknown"` when not captured
- `output_artifact_size` (integer or string) — size of output artifact(s) in bytes; defaults to literal string `"unknown"` when not captured

**Critical:** metric fields are the literal string `"unknown"` when not captured — **never a fabricated zero**. This makes missing data visible to analysis tools.

### Optional (Included Only if Present)

- `command_id` (string) — command UUID (FK `session_id`)
- `stage_id` (string) — stage UUID (FK `command_id`)
- `agent_id` (string) — agent UUID (FK `session_id`)
- `repo` (string) — repository name (basename of `cwd`), or `"unknown"`
- `cwd` (string) — working directory path, or `"unknown"`
- `command` (string) — command name (e.g., `"expert-review"`)
- `stage` (string) — stage name (e.g., `"router"`)
- `agent_type` (string) — agent type (e.g., `"expert-reviewer"`)
- `parent` (string) — parent agent type if nested
- `model` (string) — model name (e.g., `"claude-sonnet-4-20250514"`)
- `tokens` (object) — token counts with keys:
  - `input` (integer or `"unknown"`)
  - `output` (integer or `"unknown"`)
  - `cache_read` (integer or `"unknown"`)
  - `cache_creation` (integer or `"unknown"`)
- `outcome` (object) — success/failure/interrupted shape (see above)
- `findings` (integer) — count of findings in a review
- `checks` (object) — executed/outcome summary for checks (structure defined per use case)
- `token_confidence` (string) — confidence level of token counts (e.g., `"low"`, `"high"`)

## Hook Wiring

Telemetry ships as **user-level hooks** in `~/.claude/settings.json` (not per-project). This observes usage across every repo, not just this one.

When `./install.sh` runs (the bash installer), it automatically registers four hooks:

- `SessionStart` hook → `run-metrics.py session-begin` (payload piped via stdin)
- `SessionEnd` hook → `run-metrics.py session-end` (payload piped via stdin)
- `SubagentStart` hook → `run-metrics.py agent-begin` (payload piped via stdin)
- `SubagentStop` hook → `run-metrics.py agent-end` (payload piped via stdin)

### Hook Configuration (JSON Snippet)

Add this to `~/.claude/settings.json` (top-level `hooks` key — each event maps to an array of
matcher groups, each with its own nested `hooks` array; this two-level nesting is Claude Code's
actual hook config schema, confirmed by live-capturing a real `SubagentStart`/`SubagentStop`
payload against this exact config shape during this implementation pass):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 $HOME/.claude/scripts/run-metrics.py session-begin"
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 $HOME/.claude/scripts/run-metrics.py session-end"
          }
        ]
      }
    ],
    "SubagentStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 $HOME/.claude/scripts/run-metrics.py agent-begin"
          }
        ]
      }
    ],
    "SubagentStop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 $HOME/.claude/scripts/run-metrics.py agent-end"
          }
        ]
      }
    ]
  }
}
```

(Note: `$HOME` is shell-expanded by Claude Code at hook execution time.)

### Important Caveat

`SessionStart`/`SessionEnd` hook payload field names (`session_id`, `cwd`, `session_start_type`, `session_end_reason`, etc.) are documented by Claude Code but were **not independently re-verified in this implementation pass** — only `SubagentStart`/`SubagentStop` payloads were empirically captured and confirmed. The `run-metrics.py` handlers for `agent-begin`/`agent-end` are defensive (using `.get()` with `UNKNOWN` fallback) precisely to handle uncertainty in payload schema.

## Command/Stage Boundaries

Claude Code has no hook that observes inside a custom slash command's internal phases, so `command-begin`/`command-end`/`stage-begin`/`stage-end` are explicit markers embedded in command docs themselves. A separate implementation pass is adding these telemetry markers to `/shipit`, `/track-and-start`, `/cleanup`, and `/expert-plan`.

Previous approach (shell-variable capture — DO NOT USE):

```bash
# OLD PATTERN (broken — Bash tool calls don't share shell state)
TELEMETRY_COMMAND_ID=$(python3 $HOME/.claude/scripts/run-metrics.py \
  command-begin --command expert-review)
python3 $HOME/.claude/scripts/run-metrics.py \
  command-end --command-id "$TELEMETRY_COMMAND_ID" --command expert-review --outcome success
```

The problem: Bash tool invocations in Claude Code are isolated subprocesses. A shell variable set in one call (`TELEMETRY_COMMAND_ID=$(...)`) is not visible in the next call — the ID is blank by the time `command-end` runs. This produced a 39% begin/end match rate instead of the required 95%.

**New approach (session-scoped state file — use this):**

Telemetry now maintains a per-session state file at `~/.claude/telemetry/state/<session_id>.json` that tracks the current `command_id` and `stage_id`. Call sites no longer need to capture or pass IDs across bash invocations:

```bash
# NEW PATTERN (works correctly with independent bash calls)
python3 "$HOME/.claude/scripts/run-metrics.py" command-begin --command shipit >/dev/null 2>&1 || true

python3 "$HOME/.claude/scripts/run-metrics.py" stage-begin --stage run-checks >/dev/null 2>&1 || true

python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage run-checks --outcome success 2>/dev/null || true

python3 "$HOME/.claude/scripts/run-metrics.py" command-end --command shipit --outcome success 2>/dev/null || true
```

Notice: no `--command-id`, `--stage-id` flags, and no shell variable capture. The CLI resolves IDs from the session state file automatically.

## Session-Scoped State File

When a `CLAUDE_CODE_SESSION_ID` environment variable is set (always true in Claude Code), `run-metrics.py` maintains a lightweight JSON state file per session to track the current command_id and stage_id. This file is at:

```
~/.claude/telemetry/state/<session_id>.json
```

### State File Schema

```json
{
  "command_id": "hex-uuid or null",
  "command": "command-name or null",
  "stage_id": "hex-uuid or null",
  "stage": "stage-name or null"
}
```

### Behavior

- **`command-begin`:** Writes `{"command_id": ..., "command": ..., "stage_id": null, "stage": null}`, replacing any prior state. Opportunistically prunes state files older than 24 hours as a side effect (best-effort; failures are silently ignored).
- **`stage-begin`:** Updates `stage_id` and `stage` fields, leaving `command_id` and `command` intact. Reads `command_id` from state file if no explicit `--command-id` flag is given.
- **`stage-end`:** Clears the `stage_id` and `stage` fields back to null (command may still be in flight). Reads `stage_id` and `command_id` from state if not explicitly provided.
- **`command-end`:** Deletes the session's state file entirely (command lifecycle is complete). Reads `command_id` from state if not explicitly provided.

### ID Resolution (Precedence)

When an ID-bearing call site (e.g., `stage-end`) omits an explicit `--command-id` flag:

1. **Explicit flag wins:** If `--command-id` is passed, use it.
2. **State file fallback:** If session_id is known (not "unknown"), read the state file and use the recorded command_id.
3. **Graceful degradation:** If both (1) and (2) fail, use the literal string `"unknown"`.

This ensures that calls with an explicit ID always override the state file (preserving intra-block failure-exit branches in `cleanup.md` that set and read IDs within the same Bash call).

### State Mismatch Detection

If `stage-end` or `command-end` is called with a stage/command name that does not match what the state file says is currently active, the emitted event includes a `state_mismatch: true` field. This is a data-quality signal (currently not specially surfaced by `diagnose`, but available for future analysis). Examples:

- `stage-begin --stage foo` followed by `stage-end --stage bar` (without explicit `--stage-id`) → `state_mismatch: true`
- `command-begin --command shipit` followed by `command-end --command expert-review` (without explicit `--command-id`) → `state_mismatch: true`

### Degradation When Session Unknown

When `CLAUDE_CODE_SESSION_ID` is unset or empty (session_id resolves to `"unknown"`), no state file is written or read — the behavior reverts to the old pattern (all calls must provide explicit IDs, or they degrade to `"unknown"`). This is safe and maintains backward compatibility.

### Graceful Degradation on Concurrent Lifecycles

Session-scoped state assumes only one command and one stage can be active per session at a time, and this assumption is backed by Compare-And-Swap (CAS) guards. When concurrent same-session lifecycles do occur, the state file is protected from corruption: a `*-end` call whose resolved ID no longer matches the state's current ID will silently skip its destructive clear/delete operation and log a stderr warning. This ensures that the concurrent lifecycle's state survives untouched, though the skipped `*-end` call will have emitted an event with resolved (often `"unknown"`) correlation IDs rather than the mismatched state's IDs.

The CLI subcommands are:

```
python3 scripts/run-metrics.py [--log PATH] command-begin --command NAME
  # prints a bare command_id (hex uuid) to stdout

python3 scripts/run-metrics.py [--log PATH] command-end \
  --command-id ID --command NAME --outcome success|failure|interrupted \
  [--failure-class timeout|api_error|test_failure|guard_block|other]

python3 scripts/run-metrics.py [--log PATH] stage-begin --command-id ID --stage NAME
  # prints a bare stage_id (hex uuid) to stdout

python3 scripts/run-metrics.py [--log PATH] stage-end \
  --stage-id ID --command-id ID --stage NAME --outcome success|failure|interrupted \
  [--failure-class ...]
```

Default log path (no `--log` given): `~/.claude/telemetry/events.jsonl`.

## Telemetry Call-Site Conventions

Every telemetry call in command docs is non-fatal (stderr redirected, fallback provided) to ensure `run-metrics.py` failures never break the command itself.

**Simplified pattern (recommended for new call sites):**

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" command-begin --command shipit >/dev/null 2>&1 || true
python3 "$HOME/.claude/scripts/run-metrics.py" stage-begin --stage run-checks >/dev/null 2>&1 || true
python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage run-checks --outcome success 2>/dev/null || true
python3 "$HOME/.claude/scripts/run-metrics.py" command-end --command shipit --outcome success 2>/dev/null || true
```

Key points:

1. **No shell variable capture:** IDs are resolved from the session state file, not threaded through shell variables across separate bash calls.
2. **No explicit `--command-id`/`--stage-id` flags:** These are optional and resolved from the state file when omitted.
3. **Redirect stderr to `/dev/null`:** Suppresses error messages from `run-metrics.py` if it fails or is missing.
4. **Use `|| true`:** Suppresses the non-zero exit status so telemetry failures don't abort the command.

**Why this works where the old pattern failed:**

The old pattern (capturing IDs in shell variables across separate bash calls) relied on shell state persisting between Bash tool invocations. Claude Code's Bash tool is isolated per invocation — a variable set in one call is not visible in the next. The session state file solves this by persisting state to disk, which **is** visible across isolated subprocess calls.

**Backward compatibility:**

Existing call sites that explicitly pass `--command-id` and `--stage-id` (like intra-block failure handlers in `cleanup.md`) continue to work unchanged — explicit flags always win over the state file. This ensures no existing behavior breaks.

## Transcript Parsing (`claude-transcript-metrics.py`)

`claude-transcript-metrics.py` is a separate, **read-only, post-hoc** tool (not hook-driven) that extracts token and turn counts unavailable from hook payloads. It joins to writer events by `session_id`/`agent_id` at read time — the writer and parser never touch the same log file simultaneously.

### Usage

```
python3 scripts/claude-transcript-metrics.py parse --transcript PATH \
  [--session-id ID] [--agent-id ID]
  # prints one JSON object to stdout
```

### Output

The tool emits a JSON object to stdout with:
- `transcript` — path to the transcript file
- `session_id`, `agent_id` — correlation IDs (from args or event data)
- `lines_parsed`, `lines_skipped` — sanity check
- `turns` — unique turn count
- `tokens` — dict with `input`, `output`, `cache_read`, `cache_creation` (each a number or `"unknown"`)
- `token_confidence` — confidence level for token counts (`"low"` if per-message counts are unreliable)
- `cost_state` — (if present in transcript) the verbatim `cost-state` line from transcript JSONL

### Known Caveat

Per-message `output_tokens` in Claude Code transcript JSONL may reflect mid-stream placeholders and is unreliable. The tool flags this via `"token_confidence": "low"`. When a session-level `cost-state` line is present in the transcript, it is the more trustworthy aggregate and is included verbatim under `cost_state` in the output.

## Telemetry Health Check

The `diagnose` subcommand reads the telemetry log, reconciles `*.begin` / `*.end` pairs, and checks data quality:

```
python3 scripts/run-metrics.py [--log PATH] diagnose [--window-days N]
  # prints a match-rate / unknown-field report
  # exits 0 if match_rate >= 95% AND unknown_pct < 15%, else 1
```

### Thresholds

These thresholds were defined **before any baseline data exists**, per the issue plan:

- **≥95% of `*.begin` events have a matching `*.end`** — ensures most activity is observed completeness
- **<15% of token/cache fields are `"unknown"` across successful runs** — ensures reasonable token capture rate

Run `diagnose` regularly to track telemetry data quality over time.

## Privacy Allowlist

Telemetry records (and **only**):

- Timestamps (UTC ISO 8601)
- Correlation IDs (session_id, command_id, stage_id, agent_id — all UUIDs)
- Repository name (basename of working directory)
- Command, stage, agent type, and model names
- Token counts (input, output, cache_read, cache_creation)
- Turn count
- Elapsed time
- Retry count
- Peak concurrency
- Transcript size
- Output artifact size
- Findings count
- Checks executed / outcome summary
- Outcome status and failure class

**Never recorded:** prompts, source code, diffs, issue bodies, or credentials.
