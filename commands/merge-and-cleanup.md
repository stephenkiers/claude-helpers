---
name: merge-and-cleanup
description: Merge a PR through the repo's real merge gate, then remove its worktree and update main. Run from the worktree you want to merge (auto-detects PR), or from the main worktree with a PR number or worktree path, e.g. /merge-and-cleanup or /merge-and-cleanup 1022 or /merge-and-cleanup ../1020-some-worktree.
argument-hint: [PR number | worktree path]
allowed-tools: Read, Skill, AskUserQuestion, Bash(cd:*), Bash(git rev-parse:*), Bash(gh pr view:*), Bash(gh repo view:*), Bash(jq:*), Bash(ls:*), Bash(grep:*), Bash(printf:*), Bash(test:*), Bash(python3 -m scripts.workflow.cli:*), Bash(cat:*), Bash(python3:*), Bash(command:*), Bash(*merge-queue*), Bash(dirname:*), Bash(readlink:*), Bash(mkdir:*), Bash(rm:*)
model: haiku
---

# Merge and Cleanup

Merge a PR through the repo's merge gate (discovered automatically), then clean up its worktree and branch. Auto-detect the PR when run from the worktree you want to merge, or accept a PR number or worktree path explicitly when run from the main worktree.

**Note:** When a merge-queue configuration exists and the PR targets the queue's configured base branch, this command routes through `/queued-merge` automatically (after running Phase 1's push gate), then invokes `/cleanup` on successful merge. PRs targeting any other base (including stacked children and release branches) merge normally via the direct route (Phases 3–4). The merge queue provides ordered serialization and unverified-main detection, testing each PR against the exact base it will land on. If the queue configuration is unknown, undetermined, or broken, the command refuses with exit code 3; there is no bypass flag.

**Why `model: haiku`:** every conditional branch here is a literal check against command
output (file exists, JSON field present, exit code, byte-for-byte string match) — the same
mechanical-judgment shape as this repo's other Haiku-pinned roles (ADR-0004). Phase 1 returns exit code 3 when
two conditions hold: `plan_merge` signals a queue-decision (third return value) AND the Q1 routing predicate
(`.queue.decision=="refuse" and .queue.detection.state=="configured" and .queue.pr_base!=null and .queue.pr_base==.queue.queue_base`) evaluates to true — the latter means "route to queue". When the predicate is false (any other refuse cause), the command refuses without routing. The one
irreversible action (the actual merge) sits behind the push gate's single hard-fail stop, which
bounds the blast radius of a misjudgment to "the command halts," not "the wrong thing merges."

## Permission & Safety Philosophy

**Goal: merge safely, then clean up automatically without losing work.**

- The push gate (Phase 2) is the "one hard-fail" stop — if everything is pushed, `/cleanup` cannot lose work.
- Merge gate failures stop the command (non-zero exit halts Phase 3, changes nothing).
- Cleanup failures after a successful merge are reported loudly but do not reverse the merge (it is already irreversible on GitHub).
- `Bash(rm:*)` is granted but scoped only to the command's own `/tmp/merge-and-cleanup.pr-*` state directories (never to worktree or branch removal, which remain delegated to `/cleanup`). `Bash(git push:*)` is deliberately absent — push gates are handled elsewhere.

## Workflow

### Phase 0 & 1 — Resolve PR, worktree, and run push gate

Auto-detect the PR from the current worktree (when no argument given), or accept a worktree path or PR number from `$ARGUMENTS`. The plan resolves the PR/worktree and validates the push gate:
- Auto-detection (when run from a linked worktree with no argument) or explicit PR/worktree resolution (cache-first for path mode)
- 4-check push gate: detached HEAD, uncommitted changes, no upstream, unpushed commits
- If run from the main worktree with no argument, returns an error (auto-detection only works in a linked worktree)

```bash
# Resolve $ARGUMENTS to an absolute path if it's an existing filesystem path; if it's a PR number or empty, pass through unchanged
# An empty $ARGUMENTS (or one that's just whitespace) triggers auto-detection from the current worktree
if [ -e "$ARGUMENTS" ]; then
  ARGUMENTS="$(readlink -f "$ARGUMENTS")"
fi

# Capture the caller's directory: the CLI below runs with the claude-helpers checkout as cwd (so a
# PR worktree's own scripts/ package cannot shadow it), and --cwd hands the real caller dir back.
CALLER_DIR="$(pwd -P)"

# Call plan_merge to resolve PR/worktree and run push gate
source "$HOME/.claude/scripts/resolve-claude-helpers-dir.sh" || { echo "ERROR: could not resolve claude-helpers scripts directory — run /setup-local to (re)install claude-helpers symlinks" >&2; exit 1; }

PLAN_JSON=$(cd "$CLAUDE_HELPERS_DIR" && PYTHONPATH="$CLAUDE_HELPERS_DIR" python3 -m scripts.workflow.cli merge plan --cwd "$CALLER_DIR" "$ARGUMENTS")
PLAN_RESULT=$?

if [ $PLAN_RESULT -eq 3 ]; then
  # Exit code 3 can mean: (a) refuse + check predicate to route, or (b) refuse without routing
  # Q1 Predicate: .queue.decision=="refuse" and .queue.detection.state=="configured" and .queue.pr_base!=null and .queue.pr_base==.queue.queue_base
  if printf '%s' "$PLAN_JSON" | jq -e '.queue.decision=="refuse" and .queue.detection.state=="configured" and .queue.pr_base!=null and .queue.pr_base==.queue.queue_base' 2>/dev/null >/dev/null; then
    # Predicate is true — route to queue
    ROUTE="queue"
    # Fall through to extract PR_NUM/HEAD_REF/WT (already populated by plan_merge on refuse)
  else
    # Predicate is false — refuse without routing
    printf '%s' "$PLAN_JSON" | jq -r '.queue.message'
    exit 3
  fi
elif [ $PLAN_RESULT -ne 0 ]; then
  echo "ERROR: Failed to plan merge for '$ARGUMENTS'"
  printf '%s' "$PLAN_JSON" | jq -r '.error // empty'
  exit 1
fi

# Extract resolved values
PR_NUM=$(printf '%s' "$PLAN_JSON" | jq -r '.pr_number')
HEAD_REF=$(printf '%s' "$PLAN_JSON" | jq -r '.head_ref')
WT=$(printf '%s' "$PLAN_JSON" | jq -r '.target_worktree')

# Validate PR_NUM as integer
case "$PR_NUM" in
  ''|*[!0-9]*) echo "ERROR: PR number must be an integer"; exit 1 ;;
esac

# Check for push gate failures (blocking_failures is a list)
BLOCKING=$(printf '%s' "$PLAN_JSON" | jq -r '.blocking_failures[]' 2>/dev/null)
if [ -n "$BLOCKING" ]; then
  echo "ERROR: Push gate failed:"
  echo "$BLOCKING" | sed 's/^/  - /'
  echo ""
  echo "Recommendations:"
  if echo "$BLOCKING" | grep -q "Detached HEAD"; then
    echo "  - Detached HEAD: git -C $WT checkout $HEAD_REF"
  fi
  if echo "$BLOCKING" | grep -q "changes"; then
    echo "  - Uncommitted changes: commit or discard, then re-invoke"
  fi
  if echo "$BLOCKING" | grep -q "upstream"; then
    echo "  - No upstream: git -C $WT push -u origin $HEAD_REF"
  fi
  if echo "$BLOCKING" | grep -q "unpushed"; then
    echo "  - Unpushed commits: git -C $WT push"
  fi
  exit 1
fi

echo "PR #$PR_NUM: $HEAD_REF"
echo "Resolved worktree: $WT"
echo "✓ Push gate passed: branch is clean and fully pushed"

# Persist state to disk — Phase 3 runs backgrounded (see below) and Phase 4 runs as a
# separate Bash call, so neither can rely on these shell variables surviving in-memory.
# Use a PR-scoped state directory with no pointer indirection to prevent concurrent invocations
# from cross-wiring state.
MC_STATE_DIR="/tmp/merge-and-cleanup.pr-${PR_NUM}"

# The path is predictable by design (that is what makes it re-derivable in Phase 3/4), which
# the random `mktemp -d` it replaced was not. So verify we own a real directory before writing
# anything into it: `mkdir -p` follows a pre-existing symlink silently, and every subsequent
# write here — including `wt`, which Phase 4 hands to /cleanup — would land wherever it points.
if [ -L "$MC_STATE_DIR" ]; then
  echo "ERROR: $MC_STATE_DIR is a symlink — refusing to use it as a state directory" >&2
  exit 1
fi
mkdir -p -m 700 "$MC_STATE_DIR"
if [ ! -O "$MC_STATE_DIR" ]; then
  echo "ERROR: $MC_STATE_DIR is not owned by the current user — refusing to use it" >&2
  exit 1
fi

# Clear stale result files from any previous incomplete run for this PR.
# But first, check if another invocation is still running (PID-liveness guard).
PID_LOCK_FILE="$MC_STATE_DIR/pid_lock"
if [ -f "$PID_LOCK_FILE" ]; then
  PRIOR_PID=$(cat "$PID_LOCK_FILE" 2>/dev/null)
  if [ -n "$PRIOR_PID" ] && kill -0 "$PRIOR_PID" 2>/dev/null; then
    echo "ERROR: Another merge-and-cleanup invocation (PID $PRIOR_PID) is still running for PR #$PR_NUM"
    echo "Wait for it to complete, or if it is hung, terminate it first with: kill $PRIOR_PID"
    exit 1
  fi
fi
# Write this invocation's PID for future exclusivity checks
printf '%s\n' "$$" > "$PID_LOCK_FILE"

rm -f "$MC_STATE_DIR/apply_result.json" "$MC_STATE_DIR/apply_result.stderr" "$MC_STATE_DIR/apply_exit_code" "$MC_STATE_DIR/route" "$MC_STATE_DIR/queue_started_at"
printf '%s' "$PLAN_JSON" > "$MC_STATE_DIR/plan.json"
echo "$PR_NUM" > "$MC_STATE_DIR/pr_num"
echo "$WT" > "$MC_STATE_DIR/wt"
echo "State dir: $MC_STATE_DIR"

# If routing to queue, write markers and print notice
if [ "${ROUTE:-}" = "queue" ]; then
  printf 'queue\n' > "$MC_STATE_DIR/route"
  jq -n now > "$MC_STATE_DIR/queue_started_at"
  echo "QUEUE_ROUTE: PR #$PR_NUM targets the queue base ($(printf '%s' "$PLAN_JSON" | jq -r '.queue.queue_base')) — routing through /queued-merge. This may wait behind other PRs already in the queue."
fi

# No queue configured but the layout is queue-capable → Phase 2b offers to set one up.
if [ "$(printf '%s' "$PLAN_JSON" | jq -r '.queue.detection.state')" = "absent" ] \
   && [ "$(printf '%s' "$PLAN_JSON" | jq -r '.queue.detection.path // empty')" != "" ]; then
  echo "QUEUE_SETUP_OFFER: no merge queue configured (would live at $(printf '%s' "$PLAN_JSON" | jq -r '.queue.detection.path'))"
fi
```

### Phase 1b — Pivot to the queue (only when Phase 1 printed `QUEUE_PIVOT`)

If Phase 1 exited 3 with a `QUEUE_PIVOT:` line, a merge queue owns this PR's base branch. Do not
treat it as an error and do not run Phase 2b/3/4: invoke the `queued-merge` skill via the `Skill`
tool with the PR number (or the original argument, if it was a PR number) as its argument, and stop.
No state directory was written, so there is nothing to clean up. Any other non-zero exit is a real failure.

### Phase 2b — Offer merge-queue setup (only when Phase 1 printed `QUEUE_SETUP_OFFER`)

Skip this phase entirely unless Phase 1 printed a `QUEUE_SETUP_OFFER:` line. An `absent` detection
with no path (a flat clone, reason `layout-not-queue-capable`) cannot host the default config, so no
offer is made there.

First get the proposed config — a dry run, writes nothing. Substitute the literal worktree path from
Phase 1's `Resolved worktree:` line:

```bash
WT=<worktree path resolved in Phase 1>   # substitute the literal path; this is a new Bash call
source "$HOME/.claude/scripts/resolve-claude-helpers-dir.sh" || { echo "ERROR: could not resolve claude-helpers scripts directory — run /setup-local to (re)install claude-helpers symlinks" >&2; exit 1; }
(cd "$CLAUDE_HELPERS_DIR" && PYTHONPATH="$CLAUDE_HELPERS_DIR" python3 -m scripts.workflow.cli merge queue-init --cwd "$WT")
```

Then ask with `AskUserQuestion` (header `Merge queue`), showing the proposed `config` JSON and
`path` from that output in the first option's `preview`:

1. **Set up merge queue, then use /queued-merge** — writes the config shown, then merges this PR through the queue (the first enqueue also verifies the current base by running the gate once — expect it to take about twice as long).
2. **Merge without a queue this time** — continue to Phase 3 unchanged.
3. **Stop** — merge nothing.

If the dry run's `steps` is empty (no repo-cache `check` command and no justfile `check` recipe),
say so in the question and ask the user for the gate command via the "Other" answer; never invent one.
If the user answers "Other" with a different gate command, pass it as `--step` (repeatable) below.

On **Set up**, write the config. `queue-init --write` validates with the queue's own validator and
is create-only — it never overwrites an existing config:

```bash
WT=<worktree path resolved in Phase 1>   # substitute the literal path
PR_NUM=<PR number resolved in Phase 1>   # substitute the literal number
source "$HOME/.claude/scripts/resolve-claude-helpers-dir.sh" || { echo "ERROR: could not resolve claude-helpers scripts directory — run /setup-local to (re)install claude-helpers symlinks" >&2; exit 1; }
# Append `--step "<cmd>"` per user-supplied gate command, if any.
INIT_JSON=$(cd "$CLAUDE_HELPERS_DIR" && PYTHONPATH="$CLAUDE_HELPERS_DIR" python3 -m scripts.workflow.cli merge queue-init --cwd "$WT" --write)
if [ "$(printf '%s' "$INIT_JSON" | jq -r '.written // false')" != "true" ]; then
  echo "ERROR: merge-queue setup failed: $(printf '%s' "$INIT_JSON" | jq -r '.error // "unknown"')"
  exit 1
fi
echo "✓ Merge queue configured at $(printf '%s' "$INIT_JSON" | jq -r '.path')"
MC_STATE_DIR="/tmp/merge-and-cleanup.pr-${PR_NUM}"
printf 'queue\n' > "$MC_STATE_DIR/route"
jq -n now > "$MC_STATE_DIR/queue_started_at"
echo "QUEUE_ROUTE: PR #$PR_NUM targets the queue base — routing through /queued-merge. This may wait behind other PRs already in the queue."
```

Then invoke the `queued-merge` skill via the `Skill` tool with the PR number as its argument, and
continue to Phase 2Q.

On **Stop**, remove the state directory with the same two-line `MC_STATE_DIR=…; rm -rf "$MC_STATE_DIR"` used above, and end with
the halted summary. On **Merge without a queue**, continue to Phase 3 (set `ROUTE=""` explicitly and continue).

### Phase 2Q — Route through the queue

**Only run this phase if Phase 1 printed `QUEUE_ROUTE:` or Phase 2b printed `QUEUE_ROUTE:`.**

When either Phase prints that marker, the PR has been enqueued and ownership passes to `/queued-merge`. Invoke it via the `Skill` tool with the PR number as its argument:

> `/queued-merge` ends with "report the outcome and exit" — that is the end of *its* instructions, not this command's. After it prints its outcome (whatever it is, including an error or no result), **return here and run Phase 4-Q**. The resume marker at `/tmp/merge-and-cleanup.pr-<N>/route` is how you know you're mid-route.

After `/queued-merge` completes and prints its outcome, proceed to **Phase 4-Q** (do NOT run Phase 3; skip directly to Phase 4-Q).

### Phase 3 — Run the merge gate

**Skip this phase entirely if Phase 1 wrote `QUEUE_ROUTE:` marker** (check `/tmp/merge-and-cleanup.pr-<N>/route` for the string `queue`).

Auto-detected, no config key (repo-cache.json is gitignored and per-worktree, so a `commands.merge` key would not persist to new worktrees — this mirrors the design in `prompts/shipit-reference.md`). Resolution order:

1. `just -f "$WT/justfile" --summary` lists a `merge` recipe → run `just merge`
2. Else read `.commands.check` from `$WT/.claude/repo-cache.json` via `jq`, then run `gh pr merge --squash`
3. Else run `gh pr merge --squash` alone, with no gate — this is silent unless flagged, so every reach of this path prints a loud, distinct marker (`⚠️ merged with NO GATE`) rather than looking like a gated merge

**Invoke the block below with the Bash tool's `run_in_background: true`.** The `just merge` path
commonly runs a full build + E2E boot, which routinely takes several minutes — long enough to hit a
foreground Bash call's timeout ceiling even though the merge itself is still proceeding fine. A
backgrounded call has no such ceiling; wait for its completion notification, then move on to Phase 4,
which reads the result from disk (`$MC_STATE_DIR/apply_result.json` and `$MC_STATE_DIR/apply_exit_code`) rather than from captured stdout.

The merge gate's own subprocess timeout defaults to 1800s and is configurable per-repo by exporting
`MERGE_APPLY_TIMEOUT_SECS` (a positive integer, in seconds; anything else falls back to the default).

**Queue re-check:** If a merge-queue configuration is active, `merge apply` performs a live check before
acquiring the merge lock. If the PR's target branch has moved to the queue's base between Phase 1 and Phase 3,
or if the queue config has changed, the merge is refused before any lock or gate runs: a non-zero apply exit,
with the queue message in `apply_result.json`'s `.error` (printed by the failure branch below).

**Before running this block, substitute the literal PR number** (from the `PR #$PR_NUM` output above) in the assignment below.

```bash
echo "=== Phase 3: Merge Gate (backgrounded — may take several minutes) ==="

PR_NUM=<PR number resolved in Phase 1>   # substitute the literal number; this is a new Bash call
MC_STATE_DIR="/tmp/merge-and-cleanup.pr-${PR_NUM}"

# Re-verify we own the state directory (same guard as Phase 1, in case ownership changed)
if [ -L "$MC_STATE_DIR" ]; then
  echo "ERROR: $MC_STATE_DIR is a symlink — refusing to use it as a state directory" >&2
  exit 1
fi
if [ ! -O "$MC_STATE_DIR" ]; then
  echo "ERROR: $MC_STATE_DIR is not owned by the current user — refusing to use it" >&2
  exit 1
fi

# Verify we're NOT on the queue route (Phase 3 is only for direct merges)
ROUTE_FILE="$MC_STATE_DIR/route"
if [ -f "$ROUTE_FILE" ] && [ "$(cat "$ROUTE_FILE")" = "queue" ]; then
  echo "ERROR: route file indicates queue routing — this phase should not run (skip to Phase 2Q/4-Q)"
  exit 1
fi

# Cross-check: verify the state dir exists and pr_num matches
if [ ! -d "$MC_STATE_DIR" ]; then
  echo "ERROR: State directory $MC_STATE_DIR not found — Phase 1 may not have run" >&2
  exit 1
fi
if [ "$(cat "$MC_STATE_DIR/pr_num" 2>/dev/null)" != "$PR_NUM" ]; then
  echo "ERROR: PR number mismatch in state directory (expected $PR_NUM, found $(cat "$MC_STATE_DIR/pr_num" 2>/dev/null))" >&2
  exit 1
fi

PLAN_JSON="$(cat "$MC_STATE_DIR/plan.json")"

# Apply the merge plan (executes 3-path merge gate, writes cache on success)
source "$HOME/.claude/scripts/resolve-claude-helpers-dir.sh" || { echo "ERROR: could not resolve claude-helpers scripts directory — run /setup-local to (re)install claude-helpers symlinks" >&2; exit 1; }

# Write the result to disk instead of only holding it in this call's stdout — Phase 4 is a
# separate (foreground) Bash call made after this backgrounded one completes, so it reads
# this file rather than depending on variables from this shell.
printf '%s' "$PLAN_JSON" | (cd "$CLAUDE_HELPERS_DIR" && PYTHONPATH="$CLAUDE_HELPERS_DIR" python3 -m scripts.workflow.cli merge apply -) \
  > "$MC_STATE_DIR/apply_result.json" 2> "$MC_STATE_DIR/apply_result.stderr"
echo $? > "$MC_STATE_DIR/apply_exit_code"

# Guard: ensure exit code file exists, is non-empty, contains numeric value, and equals 0
APPLY_RESULT_CODE="$(cat "$MC_STATE_DIR/apply_exit_code" 2>/dev/null)"
case "$APPLY_RESULT_CODE" in
  ''|*[!0-9]*) APPLY_RESULT_CODE="missing-or-malformed" ;;
esac
if [ "$APPLY_RESULT_CODE" != "0" ]; then
  echo "ERROR: Merge apply failed (exit code: $APPLY_RESULT_CODE)"
  jq -r '.error // empty' "$MC_STATE_DIR/apply_result.json" 2>/dev/null
  cat "$MC_STATE_DIR/apply_result.stderr" >&2
  exit 1
fi

# Extract results
PR_MERGED=$(jq -r '.pr_merged // false' "$MC_STATE_DIR/apply_result.json")
MERGE_GATE_USED=$(jq -r '.merge_gate_used // "unknown"' "$MC_STATE_DIR/apply_result.json")

if [ "$PR_MERGED" = "true" ]; then
  echo "✓ PR #$PR_NUM merged successfully via $MERGE_GATE_USED"
else
  echo "ERROR: PR merge result indicated failure"
  exit 1
fi
```

### Phase 4 — Hand off to `/cleanup`

**Skip this phase entirely if Phase 1 or 2b wrote `QUEUE_ROUTE:` marker** (this means Phase 2Q ran and you're waiting for Phase 4-Q instead). Check `/tmp/merge-and-cleanup.pr-<N>/route` for the string `queue`.

Confirm the merge actually landed, then invoke `/cleanup` via the Skill tool. Pre-verify the path expands to exactly one directory. This phase is used only for the direct (non-queue) merge route.

**Only start this phase after the Phase 3 background call's completion notification arrives.** This is
a separate Bash call from Phase 3, so substitute the literal PR number (from the `PR #$PR_NUM` output in Phase 1)
in the assignment below, then re-derive `WT` from the state directory rather than assuming it's still set in this shell.

```bash
echo "=== Phase 4: Cleanup ==="

PR_NUM=<PR number resolved in Phase 1>   # substitute the literal number; this is a new Bash call
MC_STATE_DIR="/tmp/merge-and-cleanup.pr-${PR_NUM}"

# Re-verify we own the state directory (same guard as Phase 1, in case ownership changed)
if [ -L "$MC_STATE_DIR" ]; then
  echo "ERROR: $MC_STATE_DIR is a symlink — refusing to use it as a state directory" >&2
  exit 1
fi
if [ ! -O "$MC_STATE_DIR" ]; then
  echo "ERROR: $MC_STATE_DIR is not owned by the current user — refusing to use it" >&2
  exit 1
fi

# Verify we're NOT on the queue route (Phase 4 is only for direct merges; Phase 4-Q handles queue)
ROUTE_FILE="$MC_STATE_DIR/route"
if [ -f "$ROUTE_FILE" ] && [ "$(cat "$ROUTE_FILE")" = "queue" ]; then
  echo "ERROR: route file indicates queue routing — this phase should not run (skip to Phase 4-Q)"
  exit 1
fi

# Cross-check: verify the state dir exists and pr_num matches
if [ ! -d "$MC_STATE_DIR" ]; then
  echo "ERROR: State directory $MC_STATE_DIR not found — Phase 1 may not have run or cleanup may have already occurred" >&2
  exit 1
fi
if [ "$(cat "$MC_STATE_DIR/pr_num" 2>/dev/null)" != "$PR_NUM" ]; then
  echo "ERROR: PR number mismatch in state directory (expected $PR_NUM, found $(cat "$MC_STATE_DIR/pr_num" 2>/dev/null))" >&2
  exit 1
fi

WT="$(cat "$MC_STATE_DIR/wt")"

# Sanity-check the backgrounded Phase 3 call actually finished and succeeded before trusting
# GitHub's state below — an apply that's still running or that errored should not fall through here.
# Guard: ensure exit code file exists, is non-empty, contains numeric value, and equals 0
APPLY_RESULT_CODE="$(cat "$MC_STATE_DIR/apply_exit_code" 2>/dev/null)"
case "$APPLY_RESULT_CODE" in
  ''|*[!0-9]*) APPLY_RESULT_CODE="missing-or-malformed" ;;
esac
if [ "$APPLY_RESULT_CODE" != "0" ]; then
  echo "ERROR: Phase 3 merge apply has not completed successfully yet — wait for its notification first"
  exit 1
fi

# Verify merge landed
FINAL_STATE=$(gh pr view "$PR_NUM" --json state -q '.state' 2>/dev/null)
if [ "$FINAL_STATE" != "MERGED" ]; then
  echo "ERROR: PR #$PR_NUM merge did not land (state: $FINAL_STATE)"
  exit 1
fi

# Pre-verify the path expands to exactly one directory using the SAME glob
# /cleanup itself will build (it appends "*" to a pattern with no trailing slash,
# per its own resolution logic) — a sibling worktree whose name is a strict
# prefix of $WT would otherwise make /cleanup's own match ambiguous.
MATCH_LIST=$(ls -d "${WT}"*/ 2>/dev/null)
MATCH_COUNT=$(echo "$MATCH_LIST" | grep -c .)

if [ "$MATCH_COUNT" -ne 1 ]; then
  echo "ERROR: Worktree path '$WT' is ambiguous for /cleanup's glob resolution (matches: $MATCH_LIST)"
  exit 1
fi

echo "Path verified unambiguous — invoking /cleanup with: $WT"
```

**Now actually invoke the `cleanup` skill via the `Skill` tool**, passing `$WT` (the absolute worktree path, no trailing slash) as its argument — this is a tool call the agent running this command makes directly, not a bash command, so it isn't inside the block above. Only proceed to this call after the bash block above exits 0.

#### Non-duplication rules

- **Stacked-PR handling** (detecting children, restack runbooks) lives entirely in `/cleanup` and `prompts/worktree-reference.md`. This command must not detect stack layout, find children, or restack anything itself (per ADR-0011).
- This command must not compute `WORKTREE_PARENT` or `PROJECT_ROOT` itself — that is owned by Project Detection elsewhere.
- This command must not reimplement worktree removal or branch deletion — `/cleanup` owns that.

**If `/cleanup` fails after a successful merge, the merge is irreversible but cleanup is idempotent.** Print the exact recovery command and stop:
```
/cleanup <abs-path>
```

**If `/cleanup` succeeds, run this cleanup block to remove the state directory.** This final step only runs on success; a failed run leaves the state dir intact for debugging.

```bash
# Clean up state directory now that /cleanup has succeeded
PR_NUM=<PR number resolved in Phase 1>   # substitute the literal number; this is a new Bash call
MC_STATE_DIR="/tmp/merge-and-cleanup.pr-${PR_NUM}"
rm -rf "$MC_STATE_DIR"
echo "✓ State directory removed"
```

### Phase 4-Q — Verify the queued merge, then hand off to `/cleanup`

**Only run this phase if Phase 2Q ran** (i.e., `/queued-merge` was invoked and completed). Re-derive state from disk, verify the merge landed, then invoke `/cleanup`.

```bash
PR_NUM=<PR number from Phase 1>   # substitute the literal number; this is a new Bash call
MC_STATE_DIR="/tmp/merge-and-cleanup.pr-${PR_NUM}"

# Validate PR_NUM as integer
case "$PR_NUM" in
  ''|*[!0-9]*) echo "ERROR: PR number must be an integer"; exit 1 ;;
esac

# Cross-check: verify the state dir exists and pr_num matches (same as Phase 4)
if [ ! -d "$MC_STATE_DIR" ]; then
  echo "ERROR: State directory $MC_STATE_DIR not found — Phase 1 may not have run" >&2
  exit 1
fi
if [ "$(cat "$MC_STATE_DIR/pr_num" 2>/dev/null)" != "$PR_NUM" ]; then
  echo "ERROR: PR number mismatch in state directory (expected $PR_NUM, found $(cat "$MC_STATE_DIR/pr_num" 2>/dev/null))" >&2
  exit 1
fi

# Verify we're on the queue route (this distinguishes Phase 4-Q from Phase 4)
ROUTE_FILE="$MC_STATE_DIR/route"
if [ ! -f "$ROUTE_FILE" ] || [ "$(cat "$ROUTE_FILE")" != "queue" ]; then
  echo "ERROR: route file does not indicate queue routing — this phase should not have run"
  exit 1
fi

WT="$(cat "$MC_STATE_DIR/wt")"

# Read result for reporting only; check timestamps for freshness against the start time
# Phase 1/2b recorded, so a leftover result from a prior run for this PR isn't misreported
# as this run's outcome.
STATE_DIR="$(cd "$WT" && git rev-parse --path-format=absolute --git-common-dir)/merge-queue"
RESULT_FILE="$STATE_DIR/result-$PR_NUM.json"
QUEUE_STARTED_AT="$(cat "$MC_STATE_DIR/queue_started_at" 2>/dev/null)"
RESULT_JSON=""
RESULT_OUTCOME=""
RESULT_REASON=""

if [ -f "$RESULT_FILE" ]; then
  RESULT_JSON=$(cat "$RESULT_FILE" 2>/dev/null)
  RESULT_TIMESTAMP=$(printf '%s' "$RESULT_JSON" | jq -r '.timestamp // .enqueued_at // empty' 2>/dev/null)
  FRESH="missing"
  if [ -n "$RESULT_TIMESTAMP" ] && [ -n "$QUEUE_STARTED_AT" ]; then
    FRESH=$(jq -rn --argjson result_ts "$RESULT_TIMESTAMP" --argjson start_time "$QUEUE_STARTED_AT" \
      'if ($result_ts | tonumber) < ($start_time | tonumber) then "stale" else "fresh" end' 2>/dev/null)
    [ -n "$FRESH" ] || FRESH="missing"
  fi
  if [ "$FRESH" = "fresh" ]; then
    RESULT_OUTCOME=$(printf '%s' "$RESULT_JSON" | jq -r '.outcome // empty' 2>/dev/null)
    RESULT_REASON=$(printf '%s' "$RESULT_JSON" | jq -r '.reason // empty' 2>/dev/null)
  fi
fi

# Live gate: MERGED is the sole authority
FINAL_STATE=$(gh pr view "$PR_NUM" --json state -q '.state' 2>/dev/null)
GH_EXIT=$?

if [ $GH_EXIT -ne 0 ]; then
  # gh command failed — likely a network issue or PR inaccessible
  echo "ERROR: could not verify PR state (gh pr view failed with exit $GH_EXIT)"
  if [ -n "$RESULT_OUTCOME" ]; then
    echo "Queue reported: $RESULT_OUTCOME"
    if [ -n "$RESULT_REASON" ]; then
      echo "Reason: $RESULT_REASON"
    fi
  fi
  exit 1
fi

if [ "$FINAL_STATE" = "MERGED" ]; then
  # Merge landed successfully — note outcome if different from expected
  if [ -n "$RESULT_OUTCOME" ] && [ "$RESULT_OUTCOME" != "merged" ]; then
    echo "Note: GitHub reports PR #$PR_NUM as MERGED, but queue result was: $RESULT_OUTCOME"
  fi
else
  # Merge did not land — report result and exit non-zero
  if [ -n "$RESULT_OUTCOME" ]; then
    echo "Result from queue:"
    echo "  Outcome: $RESULT_OUTCOME"
    if [ -n "$RESULT_REASON" ]; then
      echo "  Reason: $RESULT_REASON"
    fi
  else
    echo "No fresh queue result for this run (result file missing or unparsable)"
  fi
  echo ""
  echo "Worktree and branch left intact: $WT"
  if [ "$RESULT_OUTCOME" = "kickback" ]; then
    echo ""
    echo "RECOMMENDATION: fix the issues, push, then re-run /merge-and-cleanup $PR_NUM"
    exit 2
  elif [ "$RESULT_OUTCOME" = "pushed_not_merged" ]; then
    if [ -n "$RESULT_REASON" ]; then
      echo ""
      echo "RECOMMENDATION: $RESULT_REASON"
    fi
    exit 2
  elif [ "$RESULT_OUTCOME" = "refused" ]; then
    exit 3
  else
    # missing/unparsable or unknown outcome
    exit 1
  fi
fi

# Merge landed on GitHub — verify worktree path is unambiguous, then invoke /cleanup
MATCH_LIST=$(ls -d "${WT}"*/ 2>/dev/null)
MATCH_COUNT=$(echo "$MATCH_LIST" | grep -c .)

if [ "$MATCH_COUNT" -ne 1 ]; then
  echo "ERROR: Worktree path '$WT' is ambiguous for /cleanup's glob resolution (matches: $MATCH_LIST)"
  exit 1
fi

echo "✓ PR #$PR_NUM is merged; invoking /cleanup with: $WT"

# Validate QUEUE_STARTED_AT if present and prepare cleanup arguments
CLEANUP_ARG="$WT"
if [ -n "$QUEUE_STARTED_AT" ]; then
  if python3 -c "import sys; f=float('$QUEUE_STARTED_AT'); sys.exit(0 if f >= 0 and f == f and f != float('inf') and f != float('-inf') else 1)" 2>/dev/null; then
    CLEANUP_ARG="--queue-started-at=$QUEUE_STARTED_AT $WT"
  else
    echo "WARNING: Invalid QUEUE_STARTED_AT='$QUEUE_STARTED_AT' (must be a finite non-negative number); passing $WT alone to /cleanup (no skip)"
  fi
fi
```

**Now actually invoke the `cleanup` skill via the `Skill` tool**, passing the arguments prepared above (either `$WT` alone, or `--queue-started-at=<n> $WT`). Only proceed after the bash block above exits 0. **If `/cleanup` succeeds**, run this cleanup block:

```bash
# Clean up state directory now that /cleanup has succeeded
PR_NUM=<PR number from Phase 1>   # substitute the literal number; this is a new Bash call
MC_STATE_DIR="/tmp/merge-and-cleanup.pr-${PR_NUM}"
rm -rf "$MC_STATE_DIR"
echo "✓ State directory removed"
```

**If `/cleanup` fails** after the queue route, the merge is already irreversible on GitHub but cleanup is idempotent. Print the recovery command and stop:
```
/cleanup <abs-path>
```

### Phase 5 — Summary

Example output for a merged PR via the direct route (PR 1020):

```
PR #1020    ✓ merged via `just merge` (E2E gate passed)
WORKTREE    ✓ removed  /path/to/1020-…
BRANCH      ✓ deleted  chore/1020-…
MAIN        ✓ fast-forwarded to <sha>  |  checks: pass
```

Example of a queue-routed PR (PR 1022) that merged successfully:

```
PR #1022    ✓ routed to merge queue (base: main)
QUEUE       ✓ merged (tested against <sha>)
WORKTREE    ✓ removed  /path/to/1022-…
BRANCH      ✓ deleted  feature/1022-…
MAIN        ✓ fast-forwarded to <sha>  |  checks: pass
```

Example of a queue-routed PR (PR 1023) that was kicked back:

```
PR #1023    ✓ routed to merge queue (base: main)
QUEUE       ⛔ kickback — conflict with main
WORKTREE    — left intact at /path/to/1023-something (tested commit: <sha>)
            RECOMMENDATION: fix, push, re-run /merge-and-cleanup 1023
```

Use ⛔ for a halted phase; omit phases that never ran. Example of a halted run (push gate failure on PR 1024 — nothing past Phase 1 ran):

```
PR #1024    ✓ resolved to branch chore/1024-something
WORKTREE    ✓ resolved  /path/to/1024-something
PUSH GATE   ⛔ halted — 2 unpushed commits in /path/to/1024-something
            RECOMMENDATION: git -C /path/to/1024-something push
```

## Files

- `commands/merge-and-cleanup.md` — this command
- `commands/queued-merge.md` — invoked via Skill on the queue route (modified in #232 for main-worktree resolution)
- `tests/test_merge_and_cleanup.py` — its test file (written by a separate pass)
- Reused, not modified: `commands/cleanup.md`, `prompts/worktree-reference.md`
