---
name: track-and-start
description: Use when user says "/track-and-start" to create a GitHub issue (or local plan), branch, and worktree in one step.
---

# Track and Start - Combined Issue, Branch, and Worktree Workflow

Creates a GitHub issue (or local plan file) from the plan, generates a branch name, and sets up a worktree in one step. When the project root has a `plans/` directory and an array-format `issues.json`, uses local plan tracking instead of GitHub issues.

## Requirements

- Plan mode **or** a resolvable plan-file path (`$ARG1`, `$ARG2` alongside a tracker ticket ID, or the explicit `--plan-file <path>` flag); tracker ticket mode works with `[A-Z]+-\d+` patterns
- Current directory must be within a git repository (GitHub remote required for non-tracker modes)
- If called with a tracker ticket ID argument (`[A-Z]+-\d+`), a GitHub remote is **not** required

## Flags

- `--plan-file <path>`: Explicitly names the plan-file path, instead of relying on positional-argument inference. Use this whenever you also want to pass other hint text (e.g. `--issue`), since a bare path in `$ARG1` alongside any other token hits the "Ambiguous arguments" error (see Entry Mode Dispatch below) — `--plan-file` sidesteps that entirely.
- `--issue <number|TICKET-ID>`: Explicitly names the issue (or tracker ticket) to attach/pivot this plan to, skipping the auto-detected Pivot Detection and the interactive Duplicate Detection prompt. The target issue must exist and be open (validated via `gh issue view` before any mutation) — an invalid or closed target is a hard error, not a silent fallback. This is the recommended way to say "pivot to issue N" up front, including when no worktree exists yet for that issue (the "Pivot to existing" flow creates one).

Example: `/track-and-start --plan-file ~/.claude/plans/floating-skipping-hippo.md --issue 59` resolves the plan file from the flag, skips the candidate-matching guesswork, and pivots straight to issue #59 — creating its worktree if one doesn't already exist.

## Behavior

1. Parse arguments and determine entry mode (plan mode, plan-file path, tracker ticket, or combination)
2. Resolve and validate plan file if in a path-based mode
3. Detect project from git remote and worktree layout (skipped only if tracker mode has no GitHub remote)
4. Check for local plan mode
5. Pivot detection
6. Duplicate detection
7. Create GitHub issue with original plan as body
8. Generate branch name from issue type and title
9. Create worktree in correct location
10. Output handoff commands for user to start implementation

**Note:** This skill does NOT call ExitPlanMode or continue implementation, **except** in the pivot flow, and only when the session is still in plan mode (`IN_PLAN_MODE=1`) — in that case the user is already in the correct worktree, so ExitPlanMode is called so they can approve and begin implementing immediately. `IN_PLAN_MODE` tracks whether the session is *currently* in plan mode, independently of where the plan content came from — a live plan-mode session that also passes a plan-file path argument still has `IN_PLAN_MODE=1` and still gets `ExitPlanMode` called. The pivot flow prints the standard handoff block instead only when `IN_PLAN_MODE=0` (a path-arg or tracker-with-path invocation made outside of a live plan-mode session).

## Telemetry: mark command start

Telemetry is local, observational, and best-effort — it must never block or fail
`/track-and-start`. Every call below is non-fatal (see docs/metrics.md's telemetry call-site conventions for why `*-begin` uses `|| true` for non-fatal best-effort):

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" command-begin --command track-and-start >/dev/null 2>&1 || true
```

See `docs/metrics.md`'s "Telemetry Call-Site Conventions" section for the full mechanism.

## Argument Parsing and Entry Mode Dispatch

Parse arguments and determine the entry mode (plan mode, path argument, tracker ticket, or combination). This is the **only** place `ENTRY_MODE` and `IN_PLAN_MODE` are assigned.

**First, extract flags and parse remaining positional arguments:**

```bash
CONFIRM_ECHO=1
ARGUMENTS_CLEAN="$ARGUMENTS"

# Strip --yes / --no-confirm flags and set CONFIRM_ECHO
if printf '%s' "$ARGUMENTS_CLEAN" | grep -qE '\s*--no-confirm\s*|\s*--yes\s*'; then
  CONFIRM_ECHO=0
  ARGUMENTS_CLEAN=$(printf '%s' "$ARGUMENTS_CLEAN" | sed -E 's/[[:space:]]*(--no-confirm|--yes)[[:space:]]*/ /g; s/[[:space:]]+/ /g; s/^[[:space:]]*//; s/[[:space:]]*$//')
fi

# Extract --plan-file <path>, if present, into PLAN_FILE_FLAG and strip it out
PLAN_FILE_FLAG=""
if printf '%s' "$ARGUMENTS_CLEAN" | grep -qE '(^| )--plan-file( |$)'; then
  PLAN_FILE_FLAG=$(printf '%s' "$ARGUMENTS_CLEAN" | sed -E 's/^.*--plan-file[[:space:]]+([^[:space:]]+).*$/\1/')
  ARGUMENTS_CLEAN=$(printf '%s' "$ARGUMENTS_CLEAN" | sed -E "s|--plan-file[[:space:]]+$(printf '%s' "$PLAN_FILE_FLAG" | sed 's/[.[\*^$/]/\\&/g')||; s/[[:space:]]+/ /g; s/^[[:space:]]*//; s/[[:space:]]*$//")
fi

# Extract --issue <number|TICKET-ID>, if present, into ISSUE_FLAG and strip it out
ISSUE_FLAG=""
if printf '%s' "$ARGUMENTS_CLEAN" | grep -qE '(^| )--issue( |$)'; then
  ISSUE_FLAG=$(printf '%s' "$ARGUMENTS_CLEAN" | sed -E 's/^.*--issue[[:space:]]+([^[:space:]]+).*$/\1/')
  ARGUMENTS_CLEAN=$(printf '%s' "$ARGUMENTS_CLEAN" | sed -E "s|--issue[[:space:]]+$(printf '%s' "$ISSUE_FLAG" | sed 's/[.[\*^$/]/\\&/g')||; s/[[:space:]]+/ /g; s/^[[:space:]]*//; s/[[:space:]]*$//")
fi

# Parse remaining positional arguments (whatever --plan-file/--issue didn't consume)
set -- $ARGUMENTS_CLEAN
ARG1="${1:-}"
ARG2="${2:-}"

echo "PLAN_FILE_FLAG=$PLAN_FILE_FLAG ISSUE_FLAG=$ISSUE_FLAG ARG1=$ARG1 ARG2=$ARG2"
```

**Known limitation:** `set -- $ARGUMENTS_CLEAN` word-splits on whitespace — a plan-file path containing
a space (e.g. `/track-and-start "~/My Plans/foo.md"`) will be split across `$ARG1`/`$ARG2` incorrectly,
and the same applies to a `--plan-file`/`--issue` value with a space. This is a pre-existing constraint
of how this doc's slash-command arguments are parsed (only a single `$ARGUMENTS` string is available,
with no quoting/array support), not something this ticket introduces or attempts to solve; if a path
with spaces is passed, expect Stage A's "does not exist" error rather than a silent misparse into the
wrong entry mode. Use a path without spaces, or rename/symlink the file, as a workaround.

**`--issue` validation:** If `$ISSUE_FLAG` is set, it must match either a bare integer (`^[0-9]+$`,
a GitHub issue number) or the tracker ticket pattern (`^[A-Z]+-[0-9]+$`). Anything else is a hard
error: `"ERROR: --issue must be a GitHub issue number or a tracker ticket ID (e.g. 59 or PPS-166), got: $ISSUE_FLAG"`.

**Entry mode dispatch:** One ordered `if/elif/elif/elif/else` chain (never independent `if`s). If `$PLAN_FILE_FLAG` is set (the `--plan-file` flag was passed), it is handled **first**, entirely separately from branches 1–6 below — see "`--plan-file` dispatch" immediately after this table.

| # | Condition | `ENTRY_MODE` | `IN_PLAN_MODE` | Plan file source |
|---|-----------|--------------|-----------------|-------------------|
| 0 | `$PLAN_FILE_FLAG` is set | see "`--plan-file` dispatch" below | see below | `$PLAN_FILE_FLAG` |
| 1 | `$ARG1` matches `^[A-Z]+-[0-9]+$` **and** NOT an existing regular file (`-f`) **and** no `$ARG2` | `tracker` | 1 (plan mode still required) | none |
| 2 | `$ARG1` matches the regex **and** NOT an existing regular file **and** `$ARG2` present (non-empty) | `tracker-with-path` | 0 | `$ARG2` |
| 3 | `$ARG1` present (non-matching regex, OR matching-but-`-f`-true — a real file on disk wins) **and no `$ARG2`** | `path-arg` | model-determined | `$ARG1` |
| 3b | `$ARG1` present (not consumed by branch 2) **and** `$ARG2` present and matches a bare issue number `^[0-9]+$` | `path-arg` | model-determined | `$ARG1` (implicit `ISSUE_FLAG=$ARG2`) |
| 4 | `$ARG1` present **and** `$ARG2` present, not matching branch 2 or 3b | — (error) | — | error: "Ambiguous arguments" |
| 5 | no `$ARG1`, plan mode currently active | `plan-mode` | 1 | written to disk |
| 6 | otherwise | — (error) | — | error: "Neither plan mode nor a plan-file path" |

**Critical notes:**
- Branches 1/2/3/3b/4 are shell-testable (grep/`-f`/string equality).
- "Plan mode currently active" (branches 3, 3b, and 5) is **NOT shell-testable** — pure model judgment.
- **Tiebreaker** (only when `$ARG2` is absent): If `$ARG1` matches tracker-ticket pattern AND exists as a file, the file wins (branch 3).
- **Critical:** `IN_PLAN_MODE` gates `ExitPlanMode` in Pivot Detection (Step 6), never `ENTRY_MODE`. Branch 3 can have `ENTRY_MODE=path-arg` but `IN_PLAN_MODE=1` (live session + path arg).
- **Branch 3b** is the bare positional shorthand for `--issue`: `/track-and-start <path> 59` means the same as `/track-and-start --plan-file <path> --issue 59`, without requiring the flags. It exists specifically so a plan-file path can be followed by a bare pivot-target number without hitting branch 4's "Ambiguous arguments" error. It does NOT accept a tracker ticket ID in the `$ARG2` slot (that would be ambiguous with `$ARG1` also being a possible ticket in some other reading) — only a bare integer GitHub issue number. For a tracker ticket target, use `--plan-file`/`--issue` explicitly.

### `--plan-file` dispatch

This is what makes "plan file + other hints" (e.g. `--issue`) possible without hitting branch 4's
"Ambiguous arguments" error: the path no longer occupies a positional slot at all, so `$ARG1` is free
for a tracker ticket ID (or nothing), and `$ISSUE_FLAG` carries the pivot-target hint out-of-band.

When `$PLAN_FILE_FLAG` is set, `$ARG2` MUST be empty (a `--plan-file` flag alongside a second
positional argument is itself ambiguous — two candidate path sources). Then:

1. If `$ARG2` is present → **error**: `"Ambiguous arguments — --plan-file was given but a second positional argument ($ARG2) was also present. Pass at most a tracker ticket ID as the one remaining positional argument alongside --plan-file."`
2. Else if `$ARG1` matches `^[A-Z]+-[0-9]+$` → `ENTRY_MODE=tracker-with-path`, `IN_PLAN_MODE=0`, plan file source is `$PLAN_FILE_FLAG` (used exactly where `$ARG2` would otherwise be used downstream).
3. Else if `$ARG1` is present but does NOT match the ticket pattern → **error**: `"Unrecognized argument alongside --plan-file: '$ARG1'. Expected a tracker ticket ID (e.g. PPS-166) or nothing; use --issue to target an existing issue."` (This is the one case that still rejects free text — an unrecognized bare token is far more likely to be a typo than a meaningful hint, and there is no field to route it to. `--issue` is the supported channel for a pivot-target hint.)
4. Else (`$ARG1` absent) → `ENTRY_MODE=path-arg`, plan file source is `$PLAN_FILE_FLAG`. For `IN_PLAN_MODE`, use the same live-session judgment as branch 3 above.

```bash
if [ -n "$PLAN_FILE_FLAG" ]; then
  if [ -n "$ARG2" ]; then
    echo "ERROR: Ambiguous arguments — --plan-file was given but a second positional argument ($ARG2) was also present." >&2
    exit 1
  fi
  ARG1_IS_TICKET=0
  printf '%s' "$ARG1" | grep -qE '^[A-Z]+-[0-9]+$' && ARG1_IS_TICKET=1
  if [ -n "$ARG1" ] && [ "$ARG1_IS_TICKET" = "0" ]; then
    echo "ERROR: Unrecognized argument alongside --plan-file: '$ARG1'. Expected a tracker ticket ID (e.g. PPS-166) or nothing; use --issue to target an existing issue." >&2
    exit 1
  fi
fi
```

Downstream, wherever this doc reads `$ARG2` as "the path for `tracker-with-path` mode" or `$ARG1` as
"the path for `path-arg` mode," substitute `$PLAN_FILE_FLAG` when it is set — the two are mutually
exclusive by construction (branch 0 vs. branches 1–6), so there is never a conflict about which one
to read.

First, compute the shell-testable classification (this part IS deterministic bash — run it and read back the four booleans):

```bash
ARG1_IS_TICKET=0
printf '%s' "$ARG1" | grep -qE '^[A-Z]+-[0-9]+$' && ARG1_IS_TICKET=1

ARG1_IS_FILE=0
[ -n "$ARG1" ] && [ -f "$ARG1" ] && ARG1_IS_FILE=1

ARG1_PRESENT=0
[ -n "$ARG1" ] && ARG1_PRESENT=1

ARG2_PRESENT=0
[ -n "$ARG2" ] && ARG2_PRESENT=1

ARG2_IS_BARE_ISSUE=0
printf '%s' "$ARG2" | grep -qE '^[0-9]+$' && ARG2_IS_BARE_ISSUE=1

echo "ARG1_IS_TICKET=$ARG1_IS_TICKET ARG1_IS_FILE=$ARG1_IS_FILE ARG1_PRESENT=$ARG1_PRESENT ARG2_PRESENT=$ARG2_PRESENT ARG2_IS_BARE_ISSUE=$ARG2_IS_BARE_ISSUE"
```

Then assign `ENTRY_MODE` and `IN_PLAN_MODE` yourself (the model), by walking this **ordered** chain — never as independent `if`s, and never write these two branches as unconditioned shell with a "change this if needed" placeholder, since "is plan mode currently active" is not something a bash predicate in this environment can answer; it is a judgment you already know from the conversation you are in:

0. If `$PLAN_FILE_FLAG` is set → follow "`--plan-file` dispatch" above instead of branches 1–6 below; do not evaluate branches 1–6 at all in this case.
1. If `ARG1_IS_TICKET=1` and `ARG1_IS_FILE=0` and `ARG2_PRESENT=0` → `ENTRY_MODE=tracker`, `IN_PLAN_MODE=1` (plan mode is still required for this branch; if you are not actually in plan mode, stop and emit the "Neither plan mode nor a plan-file path argument" error instead of forcing this branch).
2. Else if `ARG1_IS_TICKET=1` and `ARG1_IS_FILE=0` and `ARG2_PRESENT=1` → `ENTRY_MODE=tracker-with-path`, `IN_PLAN_MODE=0`.
3. Else if `ARG1_PRESENT=1` and `ARG2_PRESENT=0` (this covers both a non-matching `$ARG1` and the matching-but-`ARG1_IS_FILE=1` tiebreaker case) → `ENTRY_MODE=path-arg`. For `IN_PLAN_MODE`, assess whether this session is *currently* in plan mode (the same judgment the old "Validate plan mode" step already required) — set `IN_PLAN_MODE=1` if so, `IN_PLAN_MODE=0` otherwise. **This is the branch the ticket exists to fix: a live plan-mode session that also passes a path must still get `IN_PLAN_MODE=1`, even though the path — not the live session — is the plan content source.** This branch 3 assessment is not shell-testable; it requires model judgment about whether plan mode is currently active.

3b. Else if `ARG1_PRESENT=1` and `ARG2_PRESENT=1` and `ARG2_IS_BARE_ISSUE=1` (this branch is only reached when branch 2 didn't already match, i.e. `$ARG1` is not a bare tracker ticket — it's a path) → `ENTRY_MODE=path-arg`, plan file source `$ARG1`, and set `ISSUE_FLAG="$ARG2"` **only if `$ISSUE_FLAG` is not already set** (an explicit `--issue` flag always wins over this implicit form; they should never both be set in practice since `$ARG2` and `--issue` are different tokens, but the precedence is explicit here for clarity). `IN_PLAN_MODE` follows the same live-session judgment as branch 3. This is the bare-positional shorthand described in the Entry Mode Dispatch table's branch 3b note above — it exists so `/track-and-start <path> 59` works without requiring `--plan-file`/`--issue` flags.

4. Else if `ARG1_PRESENT=1` and `ARG2_PRESENT=1` (and neither branch 2 nor branch 3b already matched) → **error**, stop here and do not proceed: "Ambiguous arguments — cannot handle both a first and second argument in this context."
5. Else if `ARG1_PRESENT=0` and you are currently in plan mode → `ENTRY_MODE=plan-mode`, `IN_PLAN_MODE=1`. The plan file will be written to disk in Stage C below.
6. Else (`ARG1_PRESENT=0` and you are not in plan mode) → **error**, stop here and do not proceed: "Neither plan mode nor a plan-file path argument was provided. Use `/plan` first, or pass a plan file path."

```bash
# After you've made the judgment above, record it so later bash blocks in this doc can read it:
# ENTRY_MODE="<tracker|tracker-with-path|path-arg|plan-mode>"
# IN_PLAN_MODE=<0|1>
echo "ENTRY_MODE=$ENTRY_MODE IN_PLAN_MODE=$IN_PLAN_MODE"
# ISSUE_FLAG may have just been set implicitly by branch 3b above — leave it untouched otherwise.
echo "ISSUE_FLAG=$ISSUE_FLAG"
```

`ENTRY_MODE` and `IN_PLAN_MODE` are each assigned exactly once, by this section, and never reassigned later in this doc.

## Plan File Resolution

This section resolves and validates the plan file for modes that require it. It runs for `ENTRY_MODE ∈ {path-arg, tracker-with-path, plan-mode}` only; NOT for plain `tracker` mode. It produces `PLAN_FILE`, `PLAN_CONTENT`, and (for path-arg and plan-mode only) `TITLE`.

**Note:** `TITLE` is an unstated partial function — it is never assigned in either tracker mode (tracker and tracker-with-path), only in path-arg and plan-mode. In tracker modes, `BRANCH` and `TICKET_TITLE` come from the tracker and must not be overridden.

### Stage 0: Plan-Mode Disk Write (plan-mode only)

For every other mode, `$PLAN_FILE` already points at an on-disk file (`$ARG1`/`$ARG2`). Plan mode
is the one case with no file yet — this stage writes one, **before** Stage A runs, so plan-mode falls
through Stage A/B exactly like every other mode instead of duplicating their logic.

**Before running the bash block below:** Bind `PLAN_CONTENT` to the live plan text from this plan-mode session. This is the same judgment the Entry Mode Dispatch section already required — if you assigned `ENTRY_MODE=plan-mode`, you already determined that plan mode is active and this variable holds the user's plan content. Record it now so the bash block can write it to disk.

```bash
if [ "$ENTRY_MODE" = "plan-mode" ]; then
  mkdir -p ~/.claude/plans

  # Derive a best-effort slug from that content (lightweight, separate from Stage B's
  # authoritative extraction below — this one runs against content that isn't on disk yet).
  BEST_EFFORT_SLUG=$(printf '%s' "$PLAN_CONTENT" | grep -m 1 -E '^# ' 2>/dev/null | sed 's/^#[[:space:]]*//' | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g; s/--*/-/g; s/^-//; s/-$//' | cut -c1-30)

  UTC_TIMESTAMP=$(date -u +%Y%m%d-%H%M%S)
  if [ -n "$BEST_EFFORT_SLUG" ]; then
    PLAN_FILENAME="${UTC_TIMESTAMP}-${BEST_EFFORT_SLUG}.md"
  else
    PLAN_FILENAME="${UTC_TIMESTAMP}.md"
  fi
  PLAN_FILE="$HOME/.claude/plans/${PLAN_FILENAME}"

  printf '%s' "$PLAN_CONTENT" > "$PLAN_FILE"
  if [ $? -ne 0 ]; then
    rm -f "$PLAN_FILE"
    echo "ERROR: Failed to write plan file to $PLAN_FILE" >&2
    exit 1
  fi
fi
```

`PLAN_FILE` is now set for plan-mode exactly as it is for `path-arg`/`tracker-with-path` — Stage A
below validates it (a fresh write should always pass, but Stage A is what actually confirms that,
not a second, duplicated set of the same five checks) and Stage B derives `TITLE` from it.

### Stage A: Plan File Validation

Runs for `ENTRY_MODE ∈ {path-arg, tracker-with-path, plan-mode}` — the same check sequence for all
three, since by this point `PLAN_FILE` is set one way or another (from `$ARG1`, `$ARG2`, or Stage 0's
write). Resolve to an absolute path (expand a leading `~`; a relative path resolves against the
caller's cwd; plan-mode's `$PLAN_FILE` from Stage 0 is already absolute), then check in order:

```bash
if [ "$ENTRY_MODE" = "path-arg" ] || [ "$ENTRY_MODE" = "tracker-with-path" ] || [ "$ENTRY_MODE" = "plan-mode" ]; then
  if [ "$ENTRY_MODE" != "plan-mode" ]; then
    # path-arg or tracker-with-path: resolve $ARG1 or $ARG2 into an absolute PLAN_FILE.
    # plan-mode already set PLAN_FILE to an absolute path in Stage 0 above.
    if [ -n "$PLAN_FILE_FLAG" ]; then
      # --plan-file was given; it supersedes $ARG1/$ARG2 as the path source
      # regardless of entry mode (see "--plan-file dispatch" above).
      PLAN_FILE_ARG="$PLAN_FILE_FLAG"
    elif [ "$ENTRY_MODE" = "path-arg" ]; then
      PLAN_FILE_ARG="$ARG1"
    else
      # tracker-with-path
      PLAN_FILE_ARG="$ARG2"
    fi

    # Expand tilde if present
    if [[ "$PLAN_FILE_ARG" =~ ^~ ]]; then
      PLAN_FILE="${PLAN_FILE_ARG/#\~/$HOME}"
    else
      # Relative path resolves against caller's cwd
      if [[ "$PLAN_FILE_ARG" == /* ]]; then
        PLAN_FILE="$PLAN_FILE_ARG"
      else
        PLAN_FILE="$(pwd)/$PLAN_FILE_ARG"
      fi
    fi
  fi

  # Check 1: path exists
  if [ ! -e "$PLAN_FILE" ]; then
    echo "ERROR: Plan-file argument does not exist: $PLAN_FILE" >&2
    echo "  If you meant a tracker ticket, ticket IDs are uppercase (e.g. \`PPS-166\`)" >&2
    exit 1
  fi

  # Check 2: path is a regular file (not a directory, not a broken symlink)
  if [ ! -f "$PLAN_FILE" ]; then
    echo "ERROR: Plan-file argument is not a regular file: $PLAN_FILE" >&2
    exit 1
  fi

  # Check 3: path is readable
  if [ ! -r "$PLAN_FILE" ]; then
    echo "ERROR: Plan-file argument is not readable: $PLAN_FILE" >&2
    exit 1
  fi

  # Check 4: file is non-empty (has at least one non-whitespace byte)
  if ! grep -q '[^[:space:]]' "$PLAN_FILE"; then
    echo "ERROR: Plan-file argument is empty: $PLAN_FILE" >&2
    exit 1
  fi

  # Check 5: file size is at or under 1 MiB
  FILE_SIZE=$(stat -f%z "$PLAN_FILE" 2>/dev/null || stat -c%s "$PLAN_FILE" 2>/dev/null || wc -c < "$PLAN_FILE" 2>/dev/null)
  if [ -z "$FILE_SIZE" ]; then
    echo "ERROR: Could not determine plan file size: $PLAN_FILE" >&2
    exit 1
  fi
  if [ "$FILE_SIZE" -gt 1048576 ]; then
    echo "ERROR: Plan-file argument is too large (${FILE_SIZE} bytes > 1 MiB limit): $PLAN_FILE" >&2
    exit 1
  fi

  # Read PLAN_CONTENT from validated path (for plan-mode this re-reads the file Stage 0 just
  # wrote, confirming the write round-tripped correctly)
  PLAN_CONTENT=$(cat "$PLAN_FILE")
  if [ $? -ne 0 ]; then
    echo "ERROR: Failed to read plan file: $PLAN_FILE" >&2
    exit 1
  fi
  
  # For plan-mode, verify content round-trip: written content length must match read length
  if [ "$ENTRY_MODE" = "plan-mode" ]; then
    WRITTEN_LEN=$(printf '%s' "$PLAN_CONTENT" | wc -c)
    READ_LEN=$(printf '%s' "$(cat "$PLAN_FILE")" | wc -c)
    if [ "$WRITTEN_LEN" != "$READ_LEN" ]; then
      echo "ERROR: Plan file write/read mismatch: wrote $WRITTEN_LEN bytes, read $READ_LEN bytes" >&2
      exit 1
    fi
  fi
fi
```

### Stage B: Title Derivation (path-arg and plan-mode only)

This stage runs only for `ENTRY_MODE ∈ {path-arg, plan-mode}` — the single, shared, authoritative
title-derivation implementation for both. In tracker modes, `TICKET_TITLE` comes from the tracker
instead and this stage does not run.

**Stage B1: Extract first H1 from file:**

```bash
if [ "$ENTRY_MODE" = "path-arg" ] || [ "$ENTRY_MODE" = "plan-mode" ]; then
  # First `# ` H1 anywhere in file wins (not necessarily first line)
  TITLE=$(grep -m 1 -E '^# ' "$PLAN_FILE" 2>/dev/null | sed 's/^#[[:space:]]*//')

  # If extracted H1 strips to empty/whitespace-only, treat as "no H1" and fall through to Step 2
  TITLE=$(printf '%s' "$TITLE" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')

  if [ -z "$TITLE" ]; then
    # No usable H1 — proceed to Step 2 (slugify filename)
    TITLE=""
  fi
fi
```

**Stage B2: Fallback to slugified filename if no H1:**

```bash
if [ "$ENTRY_MODE" = "path-arg" ] || [ "$ENTRY_MODE" = "plan-mode" ]; then
  if [ -z "$TITLE" ]; then
    # Slugify the filename stem (lowercase, non-alnum → `-`, collapse/trim dashes)
    FILENAME=$(basename "$PLAN_FILE" .md)
    TITLE=$(printf '%s' "$FILENAME" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g; s/--*/-/g; s/^-//; s/-$//')

    TITLE_LOWER=$(printf '%s' "$TITLE" | tr '[:upper:]' '[:lower:]')
    # Strip trailing -<digits> (OS dedup-style suffix only) for denylist check
    TITLE_FOR_DENY=$(printf '%s' "$TITLE_LOWER" | sed 's/-[0-9]*$//')

    if printf '%s' "$TITLE_FOR_DENY" | grep -qE '^(plan|untitled|draft|new|readme)$'; then
      echo "ERROR: Could not derive a title — add a \`# \` heading or rename the file" >&2
      exit 1
    fi
  fi
  
  # Check that TITLE is non-empty after all derivation attempts
  if [ -z "$TITLE" ]; then
    echo "ERROR: Could not derive a title — add a \`# \` heading or rename the file" >&2
    exit 1
  fi
fi
```

**When Stage B completes:** `$TITLE` is guaranteed non-empty.

## Project Detection

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-begin --stage detect-project >/dev/null 2>&1 || true
```

Run the **Project Detection** block from
`~/.claude/prompts/worktree-reference.md` (read that file for the bash). This sets `REPO`,
`MAIN_WORKTREE`, `WORKTREE_PARENT`, `CACHE_FILE`, `ASSIGNEE`, and `PROJECT_ROOT`.

**If not in a git repo or no GitHub remote:** Error with message about needing to be in a git repository with a GitHub remote.

**Exception:** If Tracker Ticket mode was activated in step 3, a missing GitHub remote is not an error — `REPO` will be empty and that is expected. Steps that follow must not call `gh` commands in this mode.

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage detect-project --outcome success 2>/dev/null || true
```

## Local Plan Mode

When the project root has both a `plans/` directory and an array-format `issues.json`, skip GitHub issue creation and use local plan tracking instead. This replaces steps 5-7 (pivot detection, duplicate detection, issue creation) with a local workflow.

### Detection

Run the **Local Plan Mode Detection** block from `~/.claude/prompts/worktree-reference.md`.
If `LOCAL_MODE` is false, fall through to the normal GitHub flow ([Pivot Detection](#pivot-detection) → [Duplicate Detection](#duplicate-detection) → [Creating the Issue](#creating-the-issue)).

### Local Duplicate Detection

Before generating an ID, scan `issues.json` for existing entries with overlapping titles (same semantic comparison as [Duplicate Detection](#duplicate-detection)). If a match is found with status `"todo"` or `"planned"`, present via `AskUserQuestion`:

| Option | Description |
|--------|-------------|
| **Start this entry** | Use the existing entry's ID, update its status to `"in_progress"`, save the plan file |
| **Create new entry** | Generate a new ID, add a new entry to issues.json |

### Plan and Apply (Local Mode)

**Print metadata confirmation** (for `ENTRY_MODE ∈ {path-arg, tracker-with-path}`):

*Note: This block also appears in Tracker Ticket Mode (Worktree Creation), Pivot Detection, and Creating the Issue sections. Keep these blocks in sync.*

```bash
if [ "$ENTRY_MODE" = "path-arg" ] || [ "$ENTRY_MODE" = "tracker-with-path" ]; then
  # Always print path + mtime line (even if CONFIRM_ECHO=0)
  PLAN_MTIME=$(stat -f '%Sm' -t '%Y-%m-%d %H:%M:%S' "$PLAN_FILE" 2>/dev/null || stat -c '%y' "$PLAN_FILE" 2>/dev/null | cut -d' ' -f1,2)
  echo "**Resolved plan file:** \`$PLAN_FILE\` (modified: $PLAN_MTIME)"
  
  # Print other details only if CONFIRM_ECHO != 0
  if [ "$CONFIRM_ECHO" != "0" ]; then
    echo "**Title:** $TITLE"
    echo "**Branch:** feature/{issue_number}-<slug>"
    echo "**Worktree:** $WORKTREE_PARENT/{issue_number}-<slug>"
    if [ -n "$REPO" ]; then
      echo "**Target repo:** $REPO"
    else
      echo "**Target repo:** (no GitHub remote — local mode)"
    fi
  fi
  echo
fi
```

Use the CLI to plan the local track operation. The CLI infers all required state (slug, branch naming, collision detection):

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-begin --stage create-worktree >/dev/null 2>&1 || true

source "$HOME/.claude/scripts/resolve-claude-helpers-dir.sh" || { echo "ERROR: could not resolve claude-helpers scripts directory — run /setup-local to (re)install claude-helpers symlinks" >&2; exit 1; }

TRACK_PLAN=$(PYTHONPATH="$CLAUDE_HELPERS_DIR" python3 -m scripts.workflow.cli track plan --mode local --plan-file "$PLAN_FILE" --title "$TITLE" \
  --tracker-path "$PROJECT_ISSUES" --plans-dir "$PLANS_DIR")
PLAN_OK=$(printf '%s' "$TRACK_PLAN" | jq -r '.plan_hash // empty')
if [ -z "$PLAN_OK" ]; then
  echo "ERROR: Failed to plan track (local mode)" >&2
  python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage create-worktree --outcome failure --failure-class other 2>/dev/null || true
  python3 "$HOME/.claude/scripts/run-metrics.py" command-end --command track-and-start --outcome failure --failure-class other 2>/dev/null || true
  exit 1
fi
```

Now check if the plan signals a duplicate. The plan's `candidate_issues` array lists existing open entries that may overlap:

```bash
if [ -n "$ISSUE_FLAG" ]; then
  # --issue was given: skip the candidate-matching guesswork entirely and use
  # the named entry directly, same intent as picking "Start this entry" below.
  echo "Using --issue $ISSUE_FLAG: starting that entry directly (skipping duplicate detection)."
else
  CANDIDATES=$(printf '%s' "$TRACK_PLAN" | jq '.candidate_issues | length')
  if [ "$CANDIDATES" -gt 0 ]; then
    # Present the matched issues to the user
    # TODO: AskUserQuestion with options: "Start this entry", "Create new entry"
    # For now, create new entry (user can handle duplicates manually)
    echo "Note: $CANDIDATES existing entries may overlap. Review before proceeding."
  fi
fi
```

Apply the plan to create the local issue entry and worktree:

```bash
source "$HOME/.claude/scripts/resolve-claude-helpers-dir.sh" || { echo "ERROR: could not resolve claude-helpers scripts directory — run /setup-local to (re)install claude-helpers symlinks" >&2; exit 1; }

TRACK_RESULT=$(printf '%s' "$TRACK_PLAN" | PYTHONPATH="$CLAUDE_HELPERS_DIR" python3 -m scripts.workflow.cli track apply -)
TRACK_OK=$(printf '%s' "$TRACK_RESULT" | jq -r '.success // false')
if [ "$TRACK_OK" != "true" ]; then
  ERROR=$(printf '%s' "$TRACK_RESULT" | jq -r '.error // "Unknown error"')
  STEPS_COMPLETED=$(printf '%s' "$TRACK_RESULT" | jq -r '.steps_completed | join(", ")')
  STEPS_FAILED=$(printf '%s' "$TRACK_RESULT" | jq -r '.steps_failed | join(", ")')
  
  echo "ERROR: track apply failed: $ERROR" >&2
  if [ -n "$STEPS_COMPLETED" ]; then
    echo "  Completed: $STEPS_COMPLETED" >&2
  fi
  if [ -n "$STEPS_FAILED" ]; then
    echo "  Failed: $STEPS_FAILED" >&2
  fi
  
  # Handle partial success (e.g., issue created but worktree failed)
  ISSUE_NUM=$(printf '%s' "$TRACK_RESULT" | jq -r '.issue_number // empty')
  if [ -n "$ISSUE_NUM" ] && [ -n "$STEPS_FAILED" ]; then
    echo "  Issue #$ISSUE_NUM was created but is orphaned (no worktree)." >&2
    echo "  Manual cleanup or retry may be needed." >&2
  fi
  
  python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage create-worktree --outcome failure --failure-class other 2>/dev/null || true
  python3 "$HOME/.claude/scripts/run-metrics.py" command-end --command track-and-start --outcome failure --failure-class other 2>/dev/null || true
  exit 1
fi
```

Extract the real branch and worktree path from the result (NOT from the plan, which contains placeholder `{issue_number}`):

```bash
ISSUE_NUM=$(printf '%s' "$TRACK_RESULT" | jq -r '.issue_number // empty')
BRANCH=$(printf '%s' "$TRACK_RESULT" | jq -r '.branch // empty')
WORKTREE_PATH=$(printf '%s' "$TRACK_RESULT" | jq -r '.worktree_path // empty')
# main_worktree/worktree_parent are already available as $MAIN_WORKTREE/$WORKTREE_PARENT
# from Project Detection above — do not reassign $PLAN_FILE/$PLANS_DIR here, those names
# are reused for the plan-mode markdown file and the plans/ directory elsewhere in this doc.

if [ -z "$ISSUE_NUM" ] || [ -z "$BRANCH" ] || [ -z "$WORKTREE_PATH" ]; then
  echo "ERROR: Invalid result from track apply (missing required fields)" >&2
  python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage create-worktree --outcome failure --failure-class other 2>/dev/null || true
  exit 1
fi
```

### Handoff (Local Mode)

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage create-worktree --outcome success 2>/dev/null || true
python3 "$HOME/.claude/scripts/run-metrics.py" command-end --command track-and-start --outcome success 2>/dev/null || true
```

```
## Ready to implement!

**Plan:** `<plan-file-path>`
**Branch:** `<branch-name>`
**Worktree:** `<worktree-path>`

### Start implementation:

cd <worktree-path> && claude "/implement-with-haiku"
```

**Do NOT** call ExitPlanMode, continue implementation, or create a GitHub issue.

---

## Tracker Ticket Mode

Activates when `/track-and-start` is called with a ticket ID argument matching `[A-Z]+-\d+` (e.g., `PPS-166`). It looks up the ticket via MCP, uses the tracker's branch name, creates a worktree named from the lowercase ticket ID, writes the standard `github-cache.json` shape, and does not require a GitHub remote. For `ENTRY_MODE=tracker`, plan mode is still required and the plan content becomes the cache `body`. For `ENTRY_MODE=tracker-with-path`, the plan content comes from the resolved `$ARG2` path instead of the live session. This mode is self-contained (like Local Plan Mode): its own resolution, cache write, and handoff, merging back into the shared worktree creation block. The branch name comes from the tracker and must not be renamed.

#### Entry Mode Detection

Entry into this mode (`ENTRY_MODE=tracker` or `ENTRY_MODE=tracker-with-path`) is determined by the shared Entry Mode Dispatch section above.

```bash
TICKET_ID="$ARG1"
```

**For `ENTRY_MODE=tracker-with-path`:** Plan File Resolution's Stage A (Plan File Validation) has already resolved and validated `$ARG2` into `PLAN_FILE` and `PLAN_CONTENT`. Stage B (Title Derivation) is skipped because `TICKET_TITLE` comes from the tracker and must not be overridden.

**For `ENTRY_MODE=tracker`:** Plan mode is still required as in today's flow. `PLAN_CONTENT` is read from the live session (the model's assessment).

#### Tracker Resolution

Try Linear first; if not found, try Jira.

**Linear:**
Call `mcp__linearv3__get_issue` with the ticket ID (e.g., `PPS-166`).

On success, extract:
- `BRANCH` ← `gitBranchName` field (see fallback below)
- `TICKET_URL` ← `url` field
- `TICKET_TITLE` ← `title` field
- `TICKET_BODY` ← `description` field (used only if needed; plan content is the primary body)

**Jira fallback:**
If Linear returns an error or "not found", call `mcp__atlassian__getJiraIssue` with `issueIdOrKey: "$TICKET_ID"`.

On success, extract:
- `TICKET_TITLE` ← `fields.summary`
- `TICKET_URL` ← constructed from base URL + ticket ID (e.g., `https://<workspace>.atlassian.net/browse/$TICKET_ID`)
- No native `gitBranchName` in Jira — always use slug fallback (see below)

**If both fail:** Error: `"Ticket $TICKET_ID not found in Linear or Jira. Check the ID and try again."`

#### Branch Name

Linear provides `gitBranchName` natively. Use it directly if non-empty:

```bash
BRANCH="${LINEAR_GIT_BRANCH_NAME}"
```

**Fallback** (Linear with empty `gitBranchName`, or Jira):
```bash
TICKET_ID_LOWER=$(echo "$TICKET_ID" | tr '[:upper:]' '[:lower:]')
SLUG=$(echo "$TICKET_TITLE" | \
  tr '[:upper:]' '[:lower:]' | \
  sed 's/[^a-z0-9]/-/g' | \
  sed 's/--*/-/g' | \
  sed 's/^-//' | \
  sed 's/-$//' | \
  cut -c1-40)
BRANCH="${TICKET_ID_LOWER}-${SLUG}"
```

#### Base Branch

Ask the user what branch to base the worktree on. Detect the default:

```bash
DEFAULT_BASE=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||' || echo "main")
```

Present via `AskUserQuestion`:
> What branch should this worktree be based on?
> Default: `<DEFAULT_BASE>` — press Enter to accept, or type a branch name (e.g., `pps-165-some-feature` to stack on a predecessor).

Set `BASE_BRANCH` to the user's answer, defaulting to `$DEFAULT_BASE` if blank.

#### Worktree Dir

Named from the lowercase ticket ID only — not the full branch slug:

```bash
WORKTREE_DIR=$(echo "$TICKET_ID" | tr '[:upper:]' '[:lower:]')
# e.g., "pps-166"
```

#### Worktree Creation

Run Project Detection from `~/.claude/prompts/worktree-reference.md` to get `MAIN_WORKTREE` and `WORKTREE_PARENT`. Then create the worktree directly (the branch name comes from the tracker and must not be renamed):

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-begin --stage create-worktree >/dev/null 2>&1 || true

cd "$MAIN_WORKTREE"
WORKTREE_PATH="${WORKTREE_PARENT}/${WORKTREE_DIR}"
```

**Print metadata confirmation** (for `ENTRY_MODE=tracker-with-path`):

*Note: This block also appears in Local Plan Mode, Pivot Detection, and Creating the Issue sections. Keep these blocks in sync.*

```bash
if [ "$ENTRY_MODE" = "tracker-with-path" ]; then
  # Always print path + mtime line (even if CONFIRM_ECHO=0)
  PLAN_MTIME=$(stat -f '%Sm' -t '%Y-%m-%d %H:%M:%S' "$PLAN_FILE" 2>/dev/null || stat -c '%y' "$PLAN_FILE" 2>/dev/null | cut -d' ' -f1,2)
  echo "**Resolved plan file:** \`$PLAN_FILE\` (modified: $PLAN_MTIME)"
  
  # Print other details only if CONFIRM_ECHO != 0
  if [ "$CONFIRM_ECHO" != "0" ]; then
    echo "**Ticket title:** $TICKET_TITLE"
    echo "**Branch:** $BRANCH"
    echo "**Worktree:** $WORKTREE_PATH"
  fi
  echo
fi
```

```bash
git worktree add "$WORKTREE_PATH" -b "${BRANCH}" "${BASE_BRANCH}"
```

#### Cache Write

Write `.claude/github-cache.json` in the new worktree. `issue.number` is the ticket ID string (e.g., `"PPS-166"`). Downstream commands that use `issue.number` for GitHub issue closing (`Closes #N`) will produce `Closes #PPS-166` in PR bodies — GitHub will not recognize this as a closing reference, which is expected and acceptable (there is no GitHub issue to auto-close in this mode).

```bash
mkdir -p "${WORKTREE_PATH}/.claude"
jq -n \
  --arg branch    "${BRANCH}" \
  --arg number    "${TICKET_ID}" \
  --arg url       "${TICKET_URL}" \
  --arg title     "${TICKET_TITLE}" \
  --arg body      "${PLAN_CONTENT}" \
  '{branch: $branch, issue: {number: $number, url: $url, title: $title, body: $body, state: "open"}}' \
  > "${WORKTREE_PATH}/.claude/github-cache.json"
```

`PLAN_CONTENT` is the original plan content read in step 2 — same as GitHub mode.

#### Handoff Output

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage create-worktree --outcome success 2>/dev/null || true
python3 "$HOME/.claude/scripts/run-metrics.py" command-end --command track-and-start --outcome success 2>/dev/null || true
```

Same format as today:

```
## Ready to implement!

**Ticket:** <ticket-url>
**Branch:** `<branch-name>`
**Worktree:** `<worktree-path>`

### Start implementation:

cd <worktree-path> && claude "/implement-with-haiku"
```

**Do NOT** call `ExitPlanMode`, create a GitHub issue, or call any `gh` command.

---

## Pivot Detection

When `/track-and-start` is called from a worktree that's already linked to an issue, and the new plan overlaps with that issue, offer to **pivot** — replace the issue's scope with the new plan instead of creating a new issue and worktree.

### Step 4-pre: Explicit `--issue` Override

If `$ISSUE_FLAG` is set, skip auto-detection (Steps 4a–4c) and the interactive Duplicate Detection
prompt entirely — the user already told you the target. This also covers the case where **no
worktree exists yet** for the target issue (auto-detected Pivot Detection only ever looks at the
*current* worktree's linked issue, which doesn't help when the target issue has no worktree at all).

1. **Validate the target exists and is open:**

   ```bash
   if [ -n "$ISSUE_FLAG" ]; then
     if printf '%s' "$ISSUE_FLAG" | grep -qE '^[A-Z]+-[0-9]+$'; then
       echo "ERROR: --issue with a tracker ticket ID ($ISSUE_FLAG) is not yet supported for pivoting — tracker tickets don't have a GitHub issue to pivot into. Use a bare GitHub issue number instead." >&2
       exit 1
     fi
     ISSUE_STATE=$(gh issue view "$ISSUE_FLAG" --repo "$REPO" --json state -q '.state' 2>/dev/null)
     if [ -z "$ISSUE_STATE" ]; then
       echo "ERROR: --issue $ISSUE_FLAG not found in $REPO." >&2
       exit 1
     fi
     if [ "$ISSUE_STATE" != "OPEN" ]; then
       echo "ERROR: --issue $ISSUE_FLAG is not open (state: $ISSUE_STATE) — cannot pivot into a closed issue." >&2
       exit 1
     fi
   fi
   ```

2. **Execute the pivot:** set `EXISTING_ISSUE_NUM="$ISSUE_FLAG"` and jump directly to
   [Duplicate Detection → "Pivot to existing" Flow](#pivot-to-existing-flow) — the same flow used
   when the user picks "Pivot to existing" from the interactive prompt, including its worktree
   creation (so a target issue with no existing worktree gets one). Do not run Steps 4a–4c, and do
   not run the normal Duplicate Detection candidate-matching/`AskUserQuestion` step — `$ISSUE_FLAG`
   already resolved that decision.

3. **If `$ISSUE_FLAG` is not set**, proceed to Step 4a as normal (unchanged).

### Step 4a: Detect Current Worktree's Linked Issue

First, run the **In-Worktree Check** from `~/.claude/prompts/worktree-reference.md`.

**If in main worktree** (`IN_WORKTREE=false`): Skip pivot detection entirely, fall through to Step 5 (Duplicate Detection).

If in a non-main worktree, look for a linked issue:

1. **Primary**: Read `.claude/github-cache.json` for `issue.number`, `issue.title`, `issue.body`, `issue.state`
2. **Fallback**: Parse issue number from branch name (`git branch --show-current`), then look up in `$CACHE_FILE` or via `gh issue view`

**Skip pivot if:**
- No linked issue found
- Linked issue is closed (`issue.state` is not `"open"`)

### Step 4b: Compare New Plan Against Existing Issue

Use the same semantic comparison as [Duplicate Detection](#duplicate-detection), but against the single linked issue only:

- **Title similarity**: Keywords in common, same feature area, same component
- **Scope overlap**: The plan addresses something the existing issue already covers (fully or partially)
- **Subset/superset**: The plan is a narrower or broader version of the existing issue

**If no overlap:** Skip pivot, fall through to Step 5 (Duplicate Detection).

### Step 4c: Present Pivot Option

If the plan overlaps with the linked issue, present the choice to the user via `AskUserQuestion`:

```
## Pivot Detected

You're in worktree `{worktree-dir}` which is linked to:
- **Issue #{number}**: {title}
- **URL**: {issue-url}

The new plan overlaps with this existing issue:
- {brief explanation of overlap}
```

| Option | Description |
|--------|-------------|
| **Pivot** | Update this issue with the new plan. The old plan is preserved as a comment. Continue working in this worktree. |
| **New issue + worktree** | Create a separate issue and worktree for the new plan. Existing issue is untouched. |

**If user chooses "New issue + worktree":** Fall through to Step 5 and the normal flow.

### Step 4d: Execute Pivot

**CRITICAL: Operations must execute in this exact order.** The comment (archiving old body) MUST succeed before the edit (replacing body). This ensures no data loss — if commenting fails, the old body is still on the issue.

**1. Archive old body as a comment:**

```bash
gh issue comment "$ISSUE_NUM" --repo "$REPO" --body "$(cat <<'EOF'
## Superseded Plan

_This was the original plan for this issue before it was updated on YYYY-MM-DD._

---

<original issue body, verbatim>
EOF
)"
```

**2. Print metadata confirmation** (for `ENTRY_MODE ∈ {path-arg, tracker-with-path}` and `IN_PLAN_MODE=0`):

*Note: This block also appears in Local Plan Mode, Tracker Ticket Mode, and Creating the Issue sections. Keep these blocks in sync.*

```bash
if ([ "$ENTRY_MODE" = "path-arg" ] || [ "$ENTRY_MODE" = "tracker-with-path" ]) && [ "$IN_PLAN_MODE" = "0" ]; then
  # Always print path + mtime line (even if CONFIRM_ECHO=0)
  PLAN_MTIME=$(stat -f '%Sm' -t '%Y-%m-%d %H:%M:%S' "$PLAN_FILE" 2>/dev/null || stat -c '%y' "$PLAN_FILE" 2>/dev/null | cut -d' ' -f1,2)
  echo "**Resolved plan file:** \`$PLAN_FILE\` (modified: $PLAN_MTIME)"
  
  # Print other details only if CONFIRM_ECHO != 0
  if [ "$CONFIRM_ECHO" != "0" ]; then
    echo "**Title:** $TITLE"
    echo "**Branch:** feature/{issue_number}-<slug>"
    echo "**Worktree:** $WORKTREE_PARENT/{issue_number}-<slug>"
  fi
  echo
fi
```

**3. Replace issue body with the new plan:**

```bash
gh issue edit "$ISSUE_NUM" --repo "$REPO" --body "$(cat <<'EOF'
<new plan content - unmodified>
EOF
)"
```

**4. Update `.claude/github-cache.json` in the current worktree:**

Only update `issue.body` — preserve everything else (`branch`, `issue.number`, `issue.url`, `issue.title`, `issue.state`, and any `pr` section).

```bash
EXISTING=$(cat .claude/github-cache.json 2>/dev/null || echo '{}')
# Write to a temp file and mv on success so a jq failure never truncates the existing cache
# (a bare `> github-cache.json` redirect truncates the file before jq runs).
TMP=$(mktemp .claude/github-cache.json.XXXXXX)
printf '%s' "$EXISTING" | jq --arg body "<new plan content>" \
  '.issue.body = $body' > "$TMP" && mv "$TMP" .claude/github-cache.json || rm -f "$TMP"
```

**5. Update project-level `issues.json` cache:**

Update the issue's body in `$CACHE_FILE` so future duplicate detection runs against the current plan.

**6. Output pivot confirmation:**

```
## Pivot Complete!

**Issue #{number}**: {title}
**URL**: {issue-url}

- Old plan archived as comment on the issue
- Issue body updated with new plan
- Local caches updated
```

**7. Close telemetry and conditionally call `ExitPlanMode`:**

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" command-end --command track-and-start --outcome success 2>/dev/null || true
```

**If `IN_PLAN_MODE=1`** (user is in a live plan-mode session):
Call `ExitPlanMode` — since the user is already in the correct worktree, they can approve the plan and begin implementing immediately. The plan file content is the new plan (it triggered `/track-and-start`).

**If `IN_PLAN_MODE=0`** (user passed a path-arg or tracker-with-path, not in live plan mode):
Skip `ExitPlanMode` and print the standard handoff block instead:

```
## Ready to implement!

**Plan:** `<plan-file-path>`
**Branch:** `<branch-name>`
**Worktree:** `<worktree-path>`

### Start implementation:

cd <worktree-path> && claude "/implement-with-haiku"
```

**Do NOT** proceed to Steps 5-9 after a successful pivot.

### Known Trade-offs

- **Branch name may drift**: After a pivot, the branch name (e.g., `feature/42-old-slug`) may no longer match the new plan. This is acceptable — the issue is the source of truth, and renaming branches in worktrees is disruptive.
- **Multiple pivots**: Each pivot adds a "Superseded Plan" comment, creating an audit trail. This is intentional.

## Duplicate Detection

**Note:** If pivot detection (Step 4) already resolved the overlap by updating the current issue, Steps 5-9 are skipped entirely and this section does not apply. **Likewise, if `$ISSUE_FLAG` is set, Step 4-pre already jumped straight to the ["Pivot to existing" Flow](#pivot-to-existing-flow) below — the candidate-matching logic and `AskUserQuestion` in this section never run.**

The `track plan` CLI step has already performed duplicate detection and populated the plan's `candidate_issues` array with open issues that may overlap. This section documents how to present them to the user.

### How to Check

The plan JSON's `candidate_issues` field is already populated:

```bash
CANDIDATES=$(printf '%s' "$TRACK_PLAN" | jq '.candidate_issues')
CANDIDATE_COUNT=$(printf '%s' "$CANDIDATES" | jq 'length')
```

Each candidate issue includes:
- `number`: GitHub issue number
- `title`: Issue title
- `url`: GitHub issue URL
- `state`: `"open"` (only open issues are returned)
- `labels`: Array of label strings
- `assignee`: Assignee if set, or null

The CLI compares the plan title and content against each open issue's title and labels, looking for:
- **Title similarity**: Keywords in common, same feature area, same component
- **Scope overlap**: The plan addresses something an existing issue already covers (fully or partially)
- **Subset/superset**: The plan is a narrower or broader version of an existing issue

### When Matches Are Found

If one or more open issues look related, **stop and present them** to the user using `AskUserQuestion` before creating anything. Show:

- The issue number, title, and URL for each match
- A brief note on why it looks related (e.g., "both address transcript display")

Then offer these options:

| Option | Description |
|--------|-------------|
| **Pivot to existing** | Archive the existing issue's body as a comment, replace it with the new plan, then create branch/worktree linked to that issue. Use when the plan supersedes or refines the existing issue. |
| **Create new and reference** | Create the new issue but add a "Related: #N" line. Useful when the work is distinct but connected. |
| **Create new (no overlap)** | The match was a false positive. Proceed normally with no references. |

If multiple issues match, list them all and let the user pick which (if any) to link or reference.

### When No Matches Are Found

Proceed directly to issue creation — no user prompt needed.

### "Pivot to existing" Flow

If the user chooses to pivot to an existing issue, execute **steps 1 and 3 of
[Pivot Detection Step 4d](#step-4d-execute-pivot)** (archive-then-replace — step 2 there is the
metadata confirmation print, which does not apply to this flow; same ordering guarantee, same
abort-on-comment-failure rule) against `$EXISTING_ISSUE_NUM` — fetching its body first if not
already available (`gh issue view "$EXISTING_ISSUE_NUM" --repo "$REPO" --json body -q '.body'`).

Then, instead of 4d's steps 4–7 (this pivot targets a duplicate-detection match, not the current
worktree's issue):

1. **Skip issue creation** — use the existing issue number for branch naming: `{type}/{existing-issue#}-{slug}`
2. **Update `$CACHE_FILE`** with the new issue body so future duplicate detection runs against the current plan
3. **Continue with worktree creation** and handoff as normal, using the existing issue's URL and number

## Branch Naming

Format: `{type}/{issue#}-{slug}`

- **Types**: `fix`, `feature`, `chore`
- **Slug**: Kebab-case from issue title, max 50 chars, lowercase
- **Example**: `feature/42-add-transcript-export`

The CLI's `track plan` command handles all branch naming automatically via `infer_type()` and `slugify()` functions — these tables document what the CLI does under the hood.

### Type Inference

Scan the plan title and content for keywords (matched whole-word, case-insensitive; title scanned first):

| Pattern | Type |
|---------|------|
| "fix", "bug", "broken", "error", "crash" | `fix` |
| "add", "new", "feature", "implement", "create" | `feature` |
| "refactor", "cleanup", "update", "chore", "rename", "move" | `chore` |
| Default | `feature` |

(This is what the CLI's `infer_type()` does during `track plan`.)

## Issue Cache

After creating a new issue, append it to the local JSON cache so subsequent commands can avoid API calls.

Cache file location: `${WORKTREE_PARENT}/issues.json` (detected from worktree layout — see [Project Detection](#project-detection))

## Creating the Issue (GitHub Mode)

**Print metadata confirmation** (for `ENTRY_MODE ∈ {path-arg, tracker-with-path}`):

*Note: This block also appears in Local Plan Mode, Tracker Ticket Mode, and Pivot Detection sections. Keep these blocks in sync.*

```bash
if [ "$ENTRY_MODE" = "path-arg" ] || [ "$ENTRY_MODE" = "tracker-with-path" ]; then
  # Always print path + mtime line (even if CONFIRM_ECHO=0)
  PLAN_MTIME=$(stat -f '%Sm' -t '%Y-%m-%d %H:%M:%S' "$PLAN_FILE" 2>/dev/null || stat -c '%y' "$PLAN_FILE" 2>/dev/null | cut -d' ' -f1,2)
  echo "**Resolved plan file:** \`$PLAN_FILE\` (modified: $PLAN_MTIME)"
  
  # Print other details only if CONFIRM_ECHO != 0
  if [ "$CONFIRM_ECHO" != "0" ]; then
    echo "**Title:** $TITLE"
    echo "**Branch:** feature/{issue_number}-<slug>"
    echo "**Worktree:** $WORKTREE_PARENT/{issue_number}-<slug>"
    echo "**Target repo:** $REPO"
  fi
  echo
fi
```

**First, plan the track operation to detect duplicates and infer all required metadata:**

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-begin --stage create-issue >/dev/null 2>&1 || true

source "$HOME/.claude/scripts/resolve-claude-helpers-dir.sh" || { echo "ERROR: could not resolve claude-helpers scripts directory — run /setup-local to (re)install claude-helpers symlinks" >&2; exit 1; }

TRACK_PLAN=$(PYTHONPATH="$CLAUDE_HELPERS_DIR" python3 -m scripts.workflow.cli track plan --mode github --plan-file "$PLAN_FILE" --title "$TITLE" --assignee "$ASSIGNEE")
PLAN_OK=$(printf '%s' "$TRACK_PLAN" | jq -r '.plan_hash // empty')
if [ -z "$PLAN_OK" ]; then
  echo "ERROR: Failed to plan track" >&2
  python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage create-issue --outcome failure --failure-class other 2>/dev/null || true
  exit 1
fi
```

**Second, check for duplicate issues:**

The plan's `candidate_issues` array contains open issues that may overlap. Feed them into duplicate detection:

```bash
# Note: if $ISSUE_FLAG was set, Step 4-pre already jumped to the "Pivot to existing" flow
# and this section is never reached — this candidate check only runs for the un-overridden path.
CANDIDATES=$(printf '%s' "$TRACK_PLAN" | jq '.candidate_issues // []')
CANDIDATE_COUNT=$(printf '%s' "$CANDIDATES" | jq 'length')
if [ "$CANDIDATE_COUNT" -gt 0 ]; then
  # Present matched issues to user via AskUserQuestion
  # (See "When Matches Are Found" in Duplicate Detection section for option table)
  # For now, user must confirm proceed-or-pivot before we apply
  echo "Note: Found $CANDIDATE_COUNT candidate issues that may overlap."
  # TODO: Implement AskUserQuestion to pivot, create new + reference, or create new (no overlap)
fi
```

**Third, apply the plan to create the GitHub issue and all associated state:**

```bash
source "$HOME/.claude/scripts/resolve-claude-helpers-dir.sh" || { echo "ERROR: could not resolve claude-helpers scripts directory — run /setup-local to (re)install claude-helpers symlinks" >&2; exit 1; }

TRACK_RESULT=$(printf '%s' "$TRACK_PLAN" | PYTHONPATH="$CLAUDE_HELPERS_DIR" python3 -m scripts.workflow.cli track apply -)
TRACK_OK=$(printf '%s' "$TRACK_RESULT" | jq -r '.success // false')
if [ "$TRACK_OK" != "true" ]; then
  ERROR=$(printf '%s' "$TRACK_RESULT" | jq -r '.error // "Unknown error"')
  STEPS_COMPLETED=$(printf '%s' "$TRACK_RESULT" | jq -r '.steps_completed | join(", ")')
  STEPS_FAILED=$(printf '%s' "$TRACK_RESULT" | jq -r '.steps_failed | join(", ")')
  
  echo "ERROR: track apply failed: $ERROR" >&2
  if [ -n "$STEPS_COMPLETED" ]; then
    echo "  Completed: $STEPS_COMPLETED" >&2
  fi
  if [ -n "$STEPS_FAILED" ]; then
    echo "  Failed: $STEPS_FAILED" >&2
  fi
  
  # Handle partial success: issue was created but worktree creation failed
  ISSUE_NUM=$(printf '%s' "$TRACK_RESULT" | jq -r '.issue_number // empty')
  if [ -n "$ISSUE_NUM" ] && echo "$STEPS_FAILED" | grep -q "create_worktree"; then
    ISSUE_URL=$(printf '%s' "$TRACK_RESULT" | jq -r '.issue_url // empty')
    echo "  WARNING: GitHub issue #$ISSUE_NUM was created ($ISSUE_URL) but worktree creation failed." >&2
    echo "  The issue is orphaned (no linked worktree). Resolve the error and retry, or" >&2
    echo "  delete the issue manually and start over." >&2
  fi
  
  python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage create-issue --outcome failure --failure-class other 2>/dev/null || true
  exit 1
fi
```

**Extract results (use apply output, NOT the plan, which contains `{issue_number}` placeholders):**

```bash
ISSUE_NUM=$(printf '%s' "$TRACK_RESULT" | jq -r '.issue_number // empty')
ISSUE_URL=$(printf '%s' "$TRACK_RESULT" | jq -r '.issue_url // empty')
if [ -z "$ISSUE_NUM" ] || [ -z "$ISSUE_URL" ]; then
  echo "ERROR: track apply succeeded but missing issue number or URL" >&2
  python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage create-issue --outcome failure --failure-class other 2>/dev/null || true
  exit 1
fi

python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage create-issue --outcome success 2>/dev/null || true
```

**Cache Write-Back:**

The CLI's `track apply` step already writes the GitHub cache (`.claude/github-cache.json` in the new worktree) — no manual step needed here. It includes the issue URL, number, title, body, and state.

## Label Inference

Infer labels from plan content (matched whole-word, case-insensitive; title scanned first):

| Content Pattern | Label |
|-----------------|-------|
| "fix", "bug", "broken" | `bug` |
| "add", "new", "feature" | `enhancement` |
| "doc", "readme", "guide" | `documentation` |
| "refactor", "cleanup" | `chore` |

(This is what the CLI's `infer_labels()` does during `track plan`.)

## Creating the Worktree (GitHub Mode)

**Note:** The CLI's `track apply` step already creates the worktree and writes the cache. This section documents the behavior; **GitHub mode does not manually call `git worktree add` — it's done by the CLI.**

For **Tracker Ticket mode**, which does NOT use the CLI, a plain-git worktree creation path is available:

```bash
# ONLY for Tracker Ticket mode (step 248 in that section):
cd "$MAIN_WORKTREE"
WORKTREE_PATH="${WORKTREE_PARENT}/${WORKTREE_DIR}"
git worktree add "$WORKTREE_PATH" -b "${BRANCH}" "${BASE_BRANCH}"
```

**Worktree location:** The CLI places the worktree at `${WORKTREE_PARENT}/{issue_number}-{slug}` (e.g., `~/Repositories/my-project/worktrees/42-add-feature`).

**GitHub Cache:** The CLI's `track apply` writes `.claude/github-cache.json` in the new worktree, populated with issue number, URL, title, body, and state. This file is read by downstream commands like `/shipit` and `/expert-review`.

## Plan Archival

**In local plan mode:** Already done during [Local Plan Mode](#local-plan-mode) — skip.

**In GitHub mode:** After creating the worktree, check if a `plans/` directory exists at the **project root**. If it does, save a copy of the plan there for permanent reference.

```bash
# $PROJECT_ROOT is already set from project detection above
PLANS_DIR="${PROJECT_ROOT}/plans"
if [ -d "$PLANS_DIR" ]; then
  PLAN_FILE="${PLANS_DIR}/${ISSUE_NUM}-${SLUG}.md"
  cat > "$PLAN_FILE" <<'EOF'
<original plan content>
EOF
  echo "Plan archived to ${PLAN_FILE}"
fi
```

## Project Issues Tracker Update

**In local plan mode:** Already done during [Local Plan Mode](#local-plan-mode) — skip.

**In GitHub mode:** After creating the issue and worktree, check if the project root contains an `issues.json` that is a **JSON array** with objects that have `id`, `title`, and `status` fields. If found, update the matching entry's `status` to `"in_progress"`.

**Matching logic** (in priority order):
1. **By id**: If the GitHub issue number matches an entry's `id` field
2. **By title**: If the GitHub issue title is a close match to an entry's `title` field

```bash
# $PROJECT_ROOT is already set from project detection above
PROJECT_ISSUES="${PROJECT_ROOT}/issues.json"
if [ -f "$PROJECT_ISSUES" ]; then
  IS_ARRAY=$(jq 'type == "array"' "$PROJECT_ISSUES" 2>/dev/null)
  if [ "$IS_ARRAY" = "true" ]; then
    MATCH_IDX=$(jq -r --arg title "$ISSUE_TITLE" \
      'to_entries[] | select(.value.title | ascii_downcase | contains($title | ascii_downcase)) | .key' \
      "$PROJECT_ISSUES" 2>/dev/null | head -1)

    if [ -n "$MATCH_IDX" ]; then
      jq --argjson idx "$MATCH_IDX" '.[$idx].status = "in_progress"' \
        "$PROJECT_ISSUES" > "${PROJECT_ISSUES}.tmp" && mv "${PROJECT_ISSUES}.tmp" "$PROJECT_ISSUES"
      MATCHED_TITLE=$(jq -r --argjson idx "$MATCH_IDX" '.[$idx].title' "$PROJECT_ISSUES")
      echo "Updated issues.json: \"$MATCHED_TITLE\" → in_progress"
    fi
  fi
fi
```

## Final Output - Handoff Commands

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" stage-end --stage create-worktree --outcome success 2>/dev/null || true
python3 "$HOME/.claude/scripts/run-metrics.py" command-end --command track-and-start --outcome success 2>/dev/null || true
```

**CRITICAL:** Extract branch and worktree path from the `track apply` result, NOT from the plan. The plan contains literal placeholders `{issue_number}` — these are only substituted after the issue is created.

```bash
# WRONG - would print literally "feature/{issue_number}-add-feature":
# echo "Branch: $BRANCH"  # (from TRACK_PLAN)

# RIGHT - prints the resolved branch with the real issue number:
BRANCH=$(printf '%s' "$TRACK_RESULT" | jq -r '.branch')
WORKTREE_PATH=$(printf '%s' "$TRACK_RESULT" | jq -r '.worktree_path')
```

Output the following for the user to copy/paste:

```
## Ready to implement!

**Issue:** <issue-url>
**Branch:** `<branch-name>`
**Worktree:** `<worktree-path>`

### Start implementation:

cd <worktree-path> && claude "/implement-with-haiku"
```

The user will:
1. Copy the `cd ... && claude ...` command
2. Run it in their terminal
3. A new Claude session starts in the correct worktree with the issue context

**Do NOT:**
- Call ExitPlanMode
- Try to continue implementation in this session
- Update the local plan file (it stays in the original location)

## Error Handling

On any error-table exit below (before the worktree/handoff step is reached), mark the command as
failed — non-fatal, same as every other telemetry call in this doc:

```bash
python3 "$HOME/.claude/scripts/run-metrics.py" command-end --command track-and-start --outcome failure --failure-class guard_block 2>/dev/null || true
```

| Condition | Action |
|-----------|--------|
| Neither plan mode nor a plan-file path argument nor a tracker ticket ID | Error: "No valid entry point detected. Either use plan mode, pass a plan-file path, or pass a tracker ticket ID (e.g., `PPS-166`)." |
| Plan-file argument does not exist | Error: "Plan-file argument does not exist: {path}" + hint: "If you meant a tracker ticket, ticket IDs are uppercase (e.g. `PPS-166`)" |
| Plan-file argument is not a regular file | Error: "Plan-file argument is not a regular file: {path}" + hint: "Verify the path points to a file, not a directory or symlink" |
| Plan-file argument is not readable | Error: "Plan-file argument is not readable: {path}" + hint: "Check file permissions; run `chmod u+r` if needed" |
| Plan-file argument is empty | Error: "Plan-file argument is empty: {path}" + hint: "Add content to the file, or pass a different plan file" |
| Plan-file argument is too large (>1 MiB) | Error: "Plan-file argument is too large ({size} bytes > 1 MiB limit): {path}" + hint: "Split the plan into smaller files or remove extraneous content" |
| Could not determine plan file size | Error: "Could not determine plan file size: {path}" |
| Could not derive a title from plan file | Error: "Could not derive a title — add a `# ` heading or rename the file" |
| Failed to write plan file (plan-mode) | Error: "Failed to write plan file to {path}" |
| Plan file write/read round-trip failed (plan-mode) | Error: "Plan file write/read mismatch: wrote {bytes1} bytes, read {bytes2} bytes" |
| Ambiguous arguments | Error: "Ambiguous arguments — cannot handle both a first and second argument in this context." |
| `--plan-file` given with a second positional argument | Error: "Ambiguous arguments — --plan-file was given but a second positional argument ($ARG2) was also present. Pass at most a tracker ticket ID as the one remaining positional argument alongside --plan-file." |
| `--plan-file` given with an unrecognized positional argument | Error: "Unrecognized argument alongside --plan-file: '{arg}'. Expected a tracker ticket ID (e.g. PPS-166) or nothing; use --issue to target an existing issue." |
| `--issue` value is not a bare integer or tracker ticket ID | Error: "--issue must be a GitHub issue number or a tracker ticket ID (e.g. 59 or PPS-166), got: {value}" |
| `--issue` given with a tracker ticket ID | Error: "--issue with a tracker ticket ID ({id}) is not yet supported for pivoting — tracker tickets don't have a GitHub issue to pivot into. Use a bare GitHub issue number instead." |
| `--issue` target not found | Error: "--issue {n} not found in {repo}." |
| `--issue` target is closed | Error: "--issue {n} is not open (state: {state}) — cannot pivot into a closed issue." |
| Not in a git repo | Error: "Must be in a git repository with a GitHub remote" |
| No GitHub remote (non-tracker modes) | Error: "No GitHub remote found. Add one with `gh repo create` or `git remote add`" |
| `track plan` fails | Error: Output the `Unknown` reason from plan (e.g., "failed to fetch HEAD SHA", "not in git repo") |
| Plan went stale (HEAD SHA changed) | Error: "Plan went stale — HEAD has advanced since planning. Re-run `/track-and-start` to create a fresh plan." |
| Plan went stale (cache changed) | Error: "Plan went stale — repo cache changed. Re-run `/track-and-start` to create a fresh plan." |
| Mutation not allowed (allowlist) | Error: "This operation is not allowed by the current mutation allowlist. Check your configuration." |
| Overlapping issue found | Ask user: pivot to existing, create new with reference, or create new (no overlap) |
| Pivot-to-existing: `gh issue comment` fails | Error + abort: "Failed to archive old issue body. Aborting pivot to avoid losing the original content." |
| Pivot-to-existing: `gh issue edit` fails | Error: "Failed to update issue body. Old body is preserved as a comment. Try again or update manually." |
| Pivot: `gh issue comment` fails | Error + abort: "Failed to archive old plan. Aborting pivot to avoid losing the original plan." |
| Pivot: `gh issue edit` fails | Error: "Failed to update issue body. Old plan is preserved as a comment. Try again or update manually." |
| Pivot: linked issue is closed | Skip pivot detection, proceed to Step 5 (Duplicate Detection) |
| Issue cache missing/empty | Skip duplicate detection, proceed to create issue |
| Issue creation succeeded, worktree creation failed | Error + warning: "GitHub issue #N was created (URL) but worktree creation failed. The issue is orphaned. Resolve the error, retry, or delete the issue and start over." |
| Worktree already exists | Error: "Worktree already exists at `<path>`. Use it or pick a different branch name." |
| Branch already exists | Error: "Branch `<name>` already exists. Create a new plan with a different title or branch name." |
| gh CLI not authenticated | Error: "GitHub CLI not authenticated. Run `gh auth login`" |
| Ticket ID not found in Linear or Jira | Error: "Ticket $TICKET_ID not found in Linear or Jira. Check the ID and try again." |
| `gitBranchName` empty in Linear response | Fall back to `${ticket-id-lower}-${title-slug}` (same slug generation used in GitHub mode) |
| No GitHub remote (tracker-ticket mode) | Not an error — expected. `REPO` will be empty; no `gh` commands are called. |

(The numbered **Behavior** list at the top is the workflow reference — the sections above are the
detail for each step.)
