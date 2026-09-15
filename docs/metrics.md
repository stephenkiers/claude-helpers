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
- `elapsed_seconds` (integer or string) — wall-clock seconds between a lifecycle's begin and end event, computed automatically by `run-metrics.py` from the begin timestamp stashed in the session state file (`session.begin`/`session.end` and `agent.begin`/`agent.end` use a separate `<session_id>.session.json` meta file so agent/session timing survives `command-end`'s state-file deletion; `command.begin`/`command.end` and `stage.begin`/`stage.end` use the regular per-session state file). Always `"unknown"` on `*.begin` events (nothing to measure yet) and on any `*.end` event whose begin never resolved via state (explicit `--command-id`/`--stage-id` that doesn't match what's in state, missing/stale state file, or clock skew producing a negative delta) — never a fabricated number; note this does not cap an unusually large forward delta from a mid-lifecycle clock jump (laptop sleep, NTP step) — such a delta is surfaced as a large-but-plausible `elapsed_seconds` rather than `"unknown"`, since capping risks masking a genuinely long-running lifecycle as unknown.
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
- `effort` (string) — expert-review panel effort level, one of `"1"`..`"5"`. Set via `--effort` on
  `command-begin`/`stage-end`.
- `mode` (string) — expert-review run mode, one of `"local"`, `"pr"`, `"coworker"`. Set via `--mode`
  on `command-begin`/`stage-end`.
- `reviewer_count` (integer) — number of reviewers selected for the panel. Set via
  `--reviewer-count` on `command-begin`/`stage-end`.
- `resumed_from` (string) — the `command_id` of the run this run continues after a `/clear`-based resume. Present only when a `--resumed-from` flag was passed to `command-begin`.
- `tokens` (object) — token counts with keys:
  - `input` (integer or `"unknown"`)
  - `output` (integer or `"unknown"`)
  - `cache_read` (integer or `"unknown"`)
  - `cache_creation` (integer or `"unknown"`)
- `outcome` (object) — success/failure/interrupted shape (see above)
- `findings` (object) — findings yield for a `command.end`/`stage.end`, "where applicable" (a
  stage with nothing finding-shaped to report simply omits this field). Keys are a subset of
  `produced`, `accepted`, `unique`, `rejected`, `acted_upon`, each a non-negative integer. Set via
  `--findings-produced`/`--findings-accepted`/`--findings-unique`/`--findings-rejected`/
  `--findings-acted-upon` on `command-end`/`stage-end`.
- `checks` (object) — checks/tests executed and outcome for a `command.end`/`stage.end`. Keys are
  a subset of `executed`, `passed`, each a non-negative integer. Set via `--checks-executed`/
  `--checks-passed` on `command-end`/`stage-end`.
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

The file is a dict of command-lifecycle entries keyed by `command_id`, so nested/sibling commands
within the same session each get their own entry instead of clobbering a single flat slot. A
reserved `"unknown"` key holds stage activity that has no owning command (a `stage-begin` with no
prior `command-begin` — legal, and must stay legal):

```json
{
  "commands": {
    "<command_id>": {
      "session_id": "session-id",
      "command": "command-name or null",
      "command_began_at": "ISO timestamp or null",
      "stage_id": "hex-uuid or null",
      "stage": "stage-name or null",
      "stage_began_at": "ISO timestamp or null"
    },
    "unknown": {
      "session_id": "session-id",
      "command": null,
      "stage_id": "hex-uuid",
      "stage": "stage-name",
      "stage_began_at": "ISO timestamp"
    }
  }
}
```

Each entry also stores `session_id` (not just a top-level field) so a mismatch guard can verify the
entry belongs to the current session before it's read, cleared, or resolved.

### Behavior

- **`command-begin`:** Adds (or replaces) only the entry under its own `command_id` — a new
  `command-begin` never touches sibling entries; last-write-wins applies only within the same
  `command_id`. Opportunistically prunes stale state files as a side effect (best-effort; failures
  are silently ignored; the current session's own files are skipped — see "Per-Entry Eviction and
  Pruner Self-Skip" below).
- **`stage-begin`:** Resolves the target entry (LIFO innermost-open by session_id if no explicit
  `--command-id`, else falling back to the reserved `"unknown"` entry) and updates that entry's
  `stage_id`, `stage`, and `stage_began_at`, leaving its `command_id`/`command`/`command_began_at`
  intact.
- **`stage-end`:** Clears the resolved entry's `stage_id`/`stage`/`stage_began_at` fields back to
  null (its command may still be in flight; sibling entries are never touched). Reads `stage_id` and
  `command_id` via the resolution rule below if not explicitly provided, and — only when the
  resolution is a genuine name match (not a mismatch) — uses `stage_began_at` to compute
  `elapsed_seconds`.
- **`command-end`:** Pops only the resolved entry from the dict (the state file itself survives with
  any sibling entries intact — command lifecycle is complete only for that one entry). Reads
  `command_id` via the resolution rule below if not explicitly provided, and — only on a genuine
  match — uses `command_began_at` to compute `elapsed_seconds`.

### Per-Entry Eviction and Pruner Self-Skip

The dict shape has no self-healing the way the old single-slot file did (a stray leftover entry
just sits there instead of being clobbered by the next `command-begin`), so it needs its own bound:

- **Per-entry eviction:** every mutation (`command-begin`, `command-end`, `stage-begin`, `stage-end`)
  resolves its own target entry first, then calls an internal `_evict_expired(entries, keep_id, now)`
  helper that drops any *other* entry whose `command_began_at` is older than 12h (matching
  `diagnose`'s stale/recent split, so both agree on what "abandoned" means), then enforces a hard cap
  of 16 entries by evicting the oldest-begun entries first. `keep_id` is always the entry the call
  just resolved, so a call can never evict the entry it is about to read, set, or clear. A **missing**
  `command_began_at` is exempt from the 12h age check (there's no timestamp to compare), but still
  counts toward the 16-entry cap and sorts as the oldest entry when the cap is enforced. A
  **malformed/unparseable** `command_began_at` (present but not valid ISO) is treated as infinitely
  old and evicted immediately by the age check, not merely deprioritized under the cap.
- **Pruner self-skip:** `prune_stale_state` (called opportunistically from `command-begin`, before
  its own write) now skips the current session's own files — both `{safe_id}.json` and
  `{safe_id}.session.json` — so an active session's file is never reaped mid-run purely because
  every write bumps its mtime past whatever staleness window the pruner uses; that mtime-based file
  prune is a coarser, cross-session mechanism than the per-entry eviction above and does not
  substitute for it.

### Session-Meta File (separate from the state file above)

`session-begin`/`session-end` and `agent-begin`/`agent-end` track their own begin timestamps in a **separate** file, `~/.claude/telemetry/state/<session_id>.session.json`, rather than the state file above:

```json
{
  "session_id": "session-id or null",
  "session_began_at": "ISO timestamp or null",
  "agents": {"<agent_id>": {"session_id": "session-id", "began_at": "ISO timestamp"}, "...": "..."}
}
```

This is deliberately not the same file: state entries and session/agent timing have **different
lifetimes**. A command-state entry is popped when its own `command-end` resolves, but the session and
its agents must survive that — an agent can outlive the command that spawned it, so wiping session/
agent timing alongside a command entry would lose an in-flight agent's `began_at` the moment its
parent command finished. `agents` is a dict keyed by `agent_id`, mirroring the state file's own
`commands` dict keyed by `command_id` — both let concurrent, same-session lifecycles coexist without
clobbering each other. Each stored value (both the top-level `session_id` and each `agents` entry's `session_id`) exists to back a genuine-match CAS guard, mirroring the command/stage path below: `session-end` only returns `session_began_at` and deletes this file when the file's recorded `session_id` equals the session_id it was called with, and `agent-end` only returns an agent's `began_at` when that agent's stored `session_id` matches — otherwise the read is refused (returns nothing usable) and a warning is logged to stderr, and the file/entry is left as-is (session) or popped without being trusted (agent). This guards against session IDs containing characters outside `session_meta_path`'s filename-safe set, which all collide onto the same `unknown.session.json` file. `session-end`'s file deletion on a genuine match also sweeps any orphaned `agents` entries left behind by an agent whose `agent-end` never fired — a session ending is that file's natural end of life.

### Resumed Runs: Split Lifecycles Linked via `resumed_from`

When a run pauses at a round1-join checkpoint, gets `/clear`ed, and is resumed later, it appears in
`events.jsonl` as **two** separate `command.begin`/`command.end` pairs — not one. The first pair
represents the initial run (which was interrupted), and the second pair represents the resumed run.
These two pairs are linked together via the second pair's `resumed_from` field, which points to the
first pair's `command_id`. Analysis tools can use this field to reconstruct the full logical run by
following the chain from the resumed pair back to its predecessor.

### ID Resolution (Precedence)

No call site passes an explicit `--command-id`/`--stage-id` today — every resolution is ambient,
against the `commands` dict, under the one lock held for the whole read-modify-write:

| Call | Candidate set | Resolution |
|---|---|---|
| `--command-id` given | entry under that id | session_id matches → that entry, `state_mismatch = None`; entry absent → `cleared = False`, `began_at = None`, `state_mismatch = None`; entry present but foreign `session_id` → `cleared = False`, `state_mismatch = True`, entry left untouched, stderr warning logged |
| `command-end`, no id, exactly 1 entry with `command == name` | — | that entry; `state_mismatch = None` |
| `command-end`, no id, 2+ entries with `command == name` | — | LIFO: the most recently begun (innermost open); `state_mismatch = None` |
| `command-end`, no id, 0 name matches (whether or not ≥1 entry exists for this session) | — | `command_id = UNKNOWN`, `cleared = False`, `state_mismatch = None` |
| `stage-begin`, no id | — | LIFO innermost-open entry; if none, the reserved `"unknown"` entry |
| `stage-end`, `--stage-id` given | all entries | scan for that `stage_id` (uuid4 — globally unique, so positional ambiguity does not arise) |
| `stage-end`, no id | entries with an open stage whose `stage == name` | 1 → it; 2+ → LIFO by `stage_began_at`; 0 name matches → `UNKNOWN`, `state_mismatch = None` |

A resolution never adopts an entry whose `session_id` doesn't match the caller's — that guard exists
so session IDs colliding onto the same filename-safe slot can't cross-contaminate each other's state.

### State Mismatch Detection

`state_mismatch: true` fires only for an explicit `--command-id`/`--stage-id` collision: the id
resolves to an entry that exists but belongs to a different `session_id`. That entry is a
different, concurrently in-flight session's data, so it is left untouched (`cleared = False`) and
a warning is logged to stderr — this is a cross-session collision signal, not a name-mismatch
signal. No caller passes an explicit `--command-id`/`--stage-id` today, so this path is currently
unreachable in production; it exists for when a caller starts doing so.

The ambient, no-id resolution path (every call today) never sets `state_mismatch` — per the ID
Resolution table above, 0 name matches resolves to `UNKNOWN` with `state_mismatch = None` and the
existing entries left untouched, regardless of whether other entries exist for this session. A
name/id mismatch under the ambient path is silent by design: it preserves the pre-this-PR behavior
of leaving unrelated sibling entries alone rather than adopting one ambiently.

Example: `stage-begin --stage foo` followed by `stage-end --stage bar` (without explicit
`--stage-id`) resolves `bar` to `UNKNOWN`, `state_mismatch = None`, `cleared = False` — the `foo`
entry is left open and unaffected.

### Degradation When Session Unknown

When `CLAUDE_CODE_SESSION_ID` is unset or empty (session_id resolves to `"unknown"`), no state file is written or read — the behavior reverts to the old pattern (all calls must provide explicit IDs, or they degrade to `"unknown"`). This is safe and maintains backward compatibility.

### Graceful Degradation on Concurrent Lifecycles

Nested and sibling commands within one session are a supported, first-class case: each gets its own
entry in the `commands` dict, keyed by `command_id`, so they don't clobber each other. Only one stage
is assumed active *per command entry* at a time, and that narrower assumption is backed by
Compare-And-Swap (CAS) guards: a `*-end` call whose resolved id no longer matches the target entry's
recorded id (a concurrent write already changed it) will silently skip its destructive clear/pop
operation and log a stderr warning. This ensures that a genuinely concurrent write's state survives
untouched, though the skipped `*-end` call will have emitted an event with resolved (often
`"unknown"`) correlation IDs rather than the entry's actual IDs.

The CLI subcommands are:

```
python3 scripts/run-metrics.py [--log PATH] command-begin --command NAME
  # prints a bare command_id (hex uuid) to stdout

python3 scripts/run-metrics.py [--log PATH] command-end \
  --command-id ID --command NAME --outcome success|failure|interrupted \
  [--failure-class timeout|api_error|test_failure|guard_block|other]

python3 scripts/run-metrics.py [--log PATH] command-id --command NAME
  # prints the most recently begun command_id for the given command name, or empty output if none found (read-only)

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

### Interpreting a low match rate

`diagnose` also splits unmatched `*.begin` events into **stale** (≥12h old — likely a session
that was interrupted or abandoned before it closed out, or a genuine correlation bug) vs.
**recent** (<12h old — plausibly still in progress; not evidence of a defect on its own).

This split exists because a low match rate has two very different causes with different fixes:

1. **A correlation bug** — an `*.end` call site not firing on some code path (e.g. an early-exit
   or error branch), or IDs failing to resolve across process boundaries.
2. **Genuine session interruption** — a user closes the terminal, denies a permission and abandons
   the flow, or `/clear`s mid-command. Historically no `*.end` event was possible for these, and no
   amount of code fixing raised the match rate further. This is now **partly** addressed: `session-end`
   sweeps every still-open command/stage entry for the session and closes each with an `--outcome
   interrupted` `*.end` event before it clears the session-meta file, innermost-first. The sweep, the
   resolution of each swept entry, and the `session_began_at` clear are one coordinated critical
   section: if persisting the sweep's changes fails partway through, the sweep emits no events at
   all rather than emitting events for a state change that was never actually saved, and a later
   `command-end`/`stage-end` call treats an entry the sweep *did* successfully clear as a no-op
   (it does not re-emit a second terminal event for it). This only
   fires when `session-end` itself runs (e.g. a hook-driven `SessionEnd`) — a hard process kill or a
   dropped terminal with no `SessionEnd` hook still leaves the gap this section describes. The 12h
   per-entry eviction TTL (see "Per-Entry Eviction and Pruner Self-Skip" above) is deliberately
   aligned with this section's 12h stale threshold, so both agree on what "abandoned" means.

Investigating the pre-existing gap (repo history before the session-scoped state-file fix,
PR #107) found no evidence of case 2's sibling failure mode — an `*.end` arriving with the
*wrong* correlating ID (which would show up as an "orphan" end with no matching begin anywhere in
the log). Every unmatched begin in the log had no corresponding end at all. Comparing before/after
that fix's merge timestamp showed match rate jump from ~37% to ~90% (stage-level) — most of the
original gap was case 1, and PR #107 fixed the dominant instance of it. The residual gap is spread
thin across many stages/commands with no single dominant offender, consistent with a mix of
case 2 and normal measurement noise (a command still running when `diagnose` samples the log).
Do not assume a new low match rate is automatically a bug — use the stale/recent split and check
whether unmatched begins cluster in one stage/command (a fixable wiring gap) or spread evenly
(more likely case 2).

## Querying the Log Directly

There is deliberately no built-in report generator beyond `diagnose` (see ADR-0016). The log is a
flat JSONL file meant to be read directly — by a human with `jq`, or by a Claude session asked an
ad hoc question ("is `/implement-with-haiku` worth its token cost?", "which stage fails most
often?"). These are starting query patterns, not an exhaustive API — adapt the `jq` filter to the
actual question being asked.

**Caveat before anything else:** `run-metrics.py`'s events do **not** carry token/cost data — the
writer only sees hook payload metadata, which doesn't include token counts. Token and turn counts
come from `claude-transcript-metrics.py parse --transcript PATH`, run separately against a
session's transcript file and joined by `session_id`/`agent_id` at read time (see "Transcript
Parsing" above). Any cost question requires that join; the `events.jsonl` log alone answers
frequency, outcome, and timing questions, not token cost.

**Counts by command and outcome:**

```bash
jq -r 'select(.event_type == "command.end") | [.command, .outcome.status] | @tsv' \
  ~/.claude/telemetry/events.jsonl | sort | uniq -c | sort -rn
```

**Which stages fail or get interrupted most often:**

```bash
jq -r 'select(.event_type == "stage.end" and .outcome.status != "success") |
  [.stage, .outcome.status, (.outcome.class // "-")] | @tsv' \
  ~/.claude/telemetry/events.jsonl | sort | uniq -c | sort -rn
```

**Elapsed time by command (where known — events logged before `elapsed_seconds` was wired up have
no begin timestamp to compute from, so filter out `"unknown"`):**

```bash
jq -r 'select(.event_type == "command.end" and .elapsed_seconds != "unknown") |
  [.command, .elapsed_seconds] | @tsv' \
  ~/.claude/telemetry/events.jsonl
```

**Token/cost by command (requires the transcript-parser join described above):** find the
session's `command.begin`/`command.end` pair for its `session_id`, locate that session's
transcript file, run `claude-transcript-metrics.py parse --transcript PATH --session-id ID`, and
combine the resulting `tokens` dict with the command/stage records sharing that `session_id`. This
is inherently a small ad hoc script per question, not a fixed report — write it fresh each time
against the specific question being asked.

**Findings/yield vs. cost:** filter `*.end` events for findings/checks fields alongside `elapsed_seconds`/token data
from the join above, to weigh output against cost per the self-measurement goal in ADR-0016 —
e.g., "does `/implement-with-haiku` produce enough accepted findings/successful outcomes per token
to justify its cost, relative to other commands?" is exactly the kind of question this log exists
to answer with yield fields recorded.

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

## Usage Gate: Per-Subagent Token Accounting

The `usage-check` subcommand reads per-subagent telemetry data and returns a decision to proceed or ask
the user whether to continue, based on cumulative token usage across all agents launched in the current session.

### CLI

```
python3 scripts/run-metrics.py usage-check --seam <id> [--threshold <n>] [--session-id <id>] [--mark-reported]
```

**Flags:**

- `--seam` (required) — one of: `round1-join`, `gate-fix-loop`, `pre-fanout`, `post-fanout`, `pre-round4`, `final`
  - The first five are the five gated decision seams inside `/implement-with-haiku`'s flow (never invented new ones)
  - `final` is a read-only reporting seam used once at the end for the final summary
- `--threshold` (optional, defaults to `130000`) — token count threshold; exit code and `DECISION:` field branch on this
- `--session-id` (optional, defaults to `$CLAUDE_CODE_SESSION_ID`) — session UUID for correlation
- `--mark-reported` (optional flag) — latches "already told the human about this crossing" so the next seam doesn't
  re-ask until usage crosses a further full threshold increment above what was last reported

**Output:**

Always prints two lines:

1. **Human-readable summary line:** e.g., `USAGE-GATE: seam=pre-fanout counted=84,302 threshold=130,000 (65%) agents=7/7 accounted state=under-threshold DECISION: proceed`
   - At `round1-join` and `final` seams only, the line additionally ends with: ` (counts input+output+cache_creation; excludes cache_read; upstream token_confidence is always reported as "low")`
   - When any subagent is unaccounted for (unparseable, launched-but-never-finished, or missing an agent id), prefix `counted` as `counted>=` instead (the number is a floor, not exact)
   - Possible `state:` values: `under-threshold`, `over-threshold`, `unavailable` (telemetry not installed or no agents launched yet), `session-mismatch` (session id in state doesn't match current session)

2. **JSON object:** same data as JSON (internal use; the command doc only needs to parse the `DECISION:` field from the human-readable line)

**Decision semantics:**

- `DECISION: proceed` — usage is under threshold (or `state=unavailable`, which defaults to proceed with notice)
- `DECISION: ask` — fires when: usage is over threshold AND hasn't been reported yet OR has crossed a further full increment since the last report, OR a *new* unparseable/mismatched/leftover agent has appeared since the last `--mark-reported` (latched — see below), OR `state=session-mismatch`

**Latching (non-threshold asks):** an unparseable agent or a `state=unavailable`/`session-mismatch` condition does not ask at every seam for the rest of the run. `state=unavailable` never asks at all (see above). An unparseable/mismatched/leftover agent asks once; `--mark-reported` latches the current floor-agent count (`usage.last_reported_floor_count`), and the seam only asks again once a *new* such agent appears (floor-agent count grows past the latched value).

**Fresh budget per run:** `usage.counted_tokens` (and `unaccounted_no_agent_id_tokens`) are session-cumulative — the write path never resets them, since the same `SubagentStop` handler also feeds `/expert-review` and `/expert-plan-v2`. To make "a re-invocation starts a fresh budget" true, the *reader* mints `usage.active_run = {run_id, started_at, baseline_counted_tokens}` every time `--seam round1-join` is checked (this command's own first seam), and every seam's `counted=`/`DECISION:` is `counted_tokens - baseline_counted_tokens`, not the raw session-cumulative total. Round 1's own spend becomes part of the baseline the moment `round1-join` fires (it already happened — sunk cost); from `round1-join` onward, the gate measures only what rounds 2–4 add on top of that baseline. Re-invoking `/implement-with-haiku` in the same Claude Code session re-mints a fresh baseline (and resets `seams_checked`, `last_reported_crossing_at_tokens`, `last_reported_floor_count`) at that invocation's own `round1-join` call.

**Exit code:** secondary signal (0 for proceed, 10 for ask). The command doc must act on the printed `DECISION:` field text, not the exit code.

### Usage State Shape (on disk)

Stored at `~/.claude/telemetry/state/<session_id>.json` (same session state file used by `command-begin/end` and `stage-begin/end`). The `usage` key is a sibling of existing `command_id`, `stage_id` keys:

```json
{
  "usage": {
    "session_id": "abc123",
    "counted_tokens": 149302,
    "unaccounted_no_agent_id_tokens": 0,
    "last_reported_crossing_at_tokens": 65000,
    "last_reported_floor_count": 0,
    "seams_checked": ["round1-join", "gate-fix-loop"],
    "active_run": {
      "run_id": "9f2a...",
      "started_at": "2026-09-10T12:00:00+00:00",
      "baseline_counted_tokens": 65000
    },
    "agents": {
      "<agent_id>": {"session_id": "abc123", "status": "counted", "counted_tokens": 42151},
      "<agent_id>": {"session_id": "abc123", "status": "unparseable", "counted_tokens": null}
    }
  }
}
```

- `counted_tokens` — cumulative sum of input + output + cache_creation tokens across all agents, session-wide (not reset between `/implement-with-haiku` invocations — see "Fresh budget per run" above)
- `unaccounted_no_agent_id_tokens` — tokens from agents whose `SubagentStop` payload had no usable `agent_id` (never folded into a per-agent entry, since there's no key to fold them under)
- `last_reported_crossing_at_tokens` — the (session-cumulative) token count at which the gate last returned `DECISION: ask` for a threshold crossing
- `last_reported_floor_count` — the unparseable/mismatched/leftover agent count last acknowledged via `--mark-reported`; the floor-agent latch described above
- `seams_checked` — list of seam IDs checked during the *current run* (reset at each `round1-join`)
- `active_run` — the current run's baseline, minted at `round1-join`; `counted=`/`DECISION:` at every seam are `counted_tokens - active_run.baseline_counted_tokens`
- `agents` — map of per-agent data (keyed by `agent_id`); `status` is one of `counted` (transcript
  parsed successfully), `session-mismatch`, or one of four values that distinguish *why* a transcript
  wasn't counted (all four, plus the legacy `unparseable` below, are members of
  `telemetry_schema.UNPARSEABLE_STATUSES` for the purpose of the usage-gate's floor count):
  - `no_transcript_path` — the `SubagentStop` payload had no `agent_transcript_path` at all
  - `path_not_a_file` — a path was given but nothing exists there
  - `parse_raised` — the transcript file exists but parsing it raised an exception (the exception
    class and message are logged to stderr, never written to `events.jsonl`)
  - `parsed_empty` — the transcript parsed without error but yielded zero countable tokens
  - `unparseable` — a legacy value: state files written before this four-way split still carry this
    single catch-all bucket; new writes never use it

### Query Pattern: Token Totals by Session

To sum input + output + cache_creation tokens across all agents for a session (the metric used by the usage gate):

```bash
jq -r 'select(.event_type == "agent.end" and .tokens != null) |
  [(.tokens.input // 0) + (.tokens.output // 0) + (.tokens.cache_creation // 0)] |
  add' \
  ~/.claude/telemetry/events.jsonl | awk '{sum += $1} END {print "Total tokens (input+output+cache_creation): " sum}'
```

This pattern excludes cache_read (which can be large and is not counted by the gate) and sums only agent.end events (for which token data is available). Adapt `$event_type` or `.event_type` filters to answer other questions (e.g., per-command, per-model, per-outcome).
