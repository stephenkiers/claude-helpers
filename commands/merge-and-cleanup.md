---
name: merge-and-cleanup
description: Merge a PR through the repo's real merge gate, then remove its worktree and update main. Run from the worktree you want to merge (auto-detects PR), or from the main worktree with a PR number or worktree path, e.g. /merge-and-cleanup or /merge-and-cleanup 1022 or /merge-and-cleanup ../1020-some-worktree.
argument-hint: [PR number | worktree path]
allowed-tools: Read, Skill, Bash(git worktree:*), Bash(git status:*), Bash(git rev-parse:*), Bash(git symbolic-ref:*), Bash(git rev-list:*), Bash(git log:*), Bash(git fetch:*), Bash(gh pr view:*), Bash(gh pr merge:*), Bash(just:*), Bash(jq:*), Bash(ls:*), Bash(grep:*), Bash(head:*), Bash(awk:*), Bash(cut:*), Bash(tr:*), Bash(mv:*), Bash(printf:*), Bash(test:*), Bash(python3 -m scripts.workflow.cli:*), Bash(dirname:*), Bash(readlink:*), Bash(mkdir:*), Bash(rm:*)
model: haiku
---

# Merge and Cleanup

Merge a PR through the repo's merge gate (discovered automatically), then clean up its worktree and branch. Auto-detect the PR when run from the worktree you want to merge, or accept a PR number or worktree path explicitly when run from the main worktree.

**Why `model: haiku`:** every conditional branch here is a literal check against command
output (file exists, JSON field present, exit code, byte-for-byte string match) — the same
mechanical-judgment shape as this repo's other Haiku-pinned roles (ADR-0004) — and the one
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

# Call plan_merge to resolve PR/worktree and run push gate
# CLAUDE_HELPERS_DIR: run-metrics.py lives at <repo>/scripts/run-metrics.py, so two dirname
# calls from its resolved (symlink-following) path yields the claude-helpers repo root.
# Intentionally resolves to whichever checkout /setup-local last symlinked (conventionally
# main), not this worktree — /cleanup and /merge-and-cleanup run the installed, canonical
# CLI by design, not an in-progress feature-branch copy.
RUN_METRICS_RESOLVED="$(readlink -f "$HOME/.claude/scripts/run-metrics.py")"
if [ -z "$RUN_METRICS_RESOLVED" ]; then
  echo "ERROR: could not resolve ~/.claude/scripts/run-metrics.py — run /setup-local to (re)install claude-helpers symlinks" >&2
  exit 1
fi
CLAUDE_HELPERS_DIR="$(dirname "$(dirname "$RUN_METRICS_RESOLVED")")"
PLAN_JSON=$(PYTHONPATH="$CLAUDE_HELPERS_DIR" python3 -m scripts.workflow.cli merge plan "$ARGUMENTS")
PLAN_RESULT=$?

if [ $PLAN_RESULT -ne 0 ]; then
  echo "ERROR: Failed to plan merge for '$ARGUMENTS'"
  exit 1
fi

# Extract resolved values
PR_NUM=$(echo "$PLAN_JSON" | jq -r '.pr_number')
HEAD_REF=$(echo "$PLAN_JSON" | jq -r '.head_ref')
WT=$(echo "$PLAN_JSON" | jq -r '.target_worktree')

# Check for push gate failures (blocking_failures is a list)
BLOCKING=$(echo "$PLAN_JSON" | jq -r '.blocking_failures[]' 2>/dev/null)
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
rm -f "$MC_STATE_DIR/apply_result.json" "$MC_STATE_DIR/apply_result.stderr" "$MC_STATE_DIR/apply_exit_code"
echo "$PLAN_JSON" > "$MC_STATE_DIR/plan.json"
echo "$PR_NUM" > "$MC_STATE_DIR/pr_num"
echo "$WT" > "$MC_STATE_DIR/wt"
echo "State dir: $MC_STATE_DIR"
```

### Phase 3 — Run the merge gate

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

**Before running this block, substitute the literal PR number** (from the `PR #$PR_NUM` output above) in the assignment below.

```bash
echo "=== Phase 3: Merge Gate (backgrounded — may take several minutes) ==="

PR_NUM=<PR number resolved in Phase 1>   # substitute the literal number; this is a new Bash call
MC_STATE_DIR="/tmp/merge-and-cleanup.pr-${PR_NUM}"

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
# CLAUDE_HELPERS_DIR: run-metrics.py lives at <repo>/scripts/run-metrics.py, so two dirname
# calls from its resolved (symlink-following) path yields the claude-helpers repo root.
# Intentionally resolves to whichever checkout /setup-local last symlinked (conventionally
# main), not this worktree — /cleanup and /merge-and-cleanup run the installed, canonical
# CLI by design, not an in-progress feature-branch copy.
RUN_METRICS_RESOLVED="$(readlink -f "$HOME/.claude/scripts/run-metrics.py")"
if [ -z "$RUN_METRICS_RESOLVED" ]; then
  echo "ERROR: could not resolve ~/.claude/scripts/run-metrics.py — run /setup-local to (re)install claude-helpers symlinks" >&2
  exit 1
fi
CLAUDE_HELPERS_DIR="$(dirname "$(dirname "$RUN_METRICS_RESOLVED")")"

# Write the result to disk instead of only holding it in this call's stdout — Phase 4 is a
# separate (foreground) Bash call made after this backgrounded one completes, so it reads
# this file rather than depending on variables from this shell.
echo "$PLAN_JSON" | PYTHONPATH="$CLAUDE_HELPERS_DIR" python3 -m scripts.workflow.cli merge apply - \
  > "$MC_STATE_DIR/apply_result.json" 2> "$MC_STATE_DIR/apply_result.stderr"
echo $? > "$MC_STATE_DIR/apply_exit_code"

# Guard: ensure exit code file exists, is non-empty, contains numeric value, and equals 0
APPLY_RESULT_CODE="$(cat "$MC_STATE_DIR/apply_exit_code" 2>/dev/null)"
case "$APPLY_RESULT_CODE" in
  ''|*[!0-9]*) APPLY_RESULT_CODE="missing-or-malformed" ;;
esac
if [ "$APPLY_RESULT_CODE" != "0" ]; then
  echo "ERROR: Merge apply failed (exit code: $APPLY_RESULT_CODE)"
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

Confirm the merge actually landed, then invoke `/cleanup` via the Skill tool. Pre-verify the path expands to exactly one directory.

**Only start this phase after the Phase 3 background call's completion notification arrives.** This is
a separate Bash call from Phase 3, so substitute the literal PR number (from the `PR #$PR_NUM` output in Phase 1)
in the assignment below, then re-derive `WT` from the state directory rather than assuming it's still set in this shell.

```bash
echo "=== Phase 4: Cleanup ==="

PR_NUM=<PR number resolved in Phase 1>   # substitute the literal number; this is a new Bash call
MC_STATE_DIR="/tmp/merge-and-cleanup.pr-${PR_NUM}"

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

### Phase 5 — Summary

Example output (with PR 1022 as the illustrative example):

```
PR #1022    ✓ merged via `just merge` (E2E gate passed)
WORKTREE    ✓ removed  /path/to/1020-…
BRANCH      ✓ deleted  chore/1020-…
MAIN        ✓ fast-forwarded to <sha>  |  checks: pass
```

Use ⛔ for a halted phase; omit phases that never ran. Example of a halted run (push gate failure on PR 1022 — nothing past Phase 2 ran):

```
PR #1022    ✓ resolved to branch chore/1020-something
WORKTREE    ✓ resolved  /path/to/1020-something
PUSH GATE   ⛔ halted — 2 unpushed commits in /path/to/1020-something
            RECOMMENDATION: git -C /path/to/1020-something push
```

## Files

- `commands/merge-and-cleanup.md` — this command
- `tests/test_merge_and_cleanup.py` — its test file (written by a separate pass)
- Reused, not modified: `commands/cleanup.md`, `prompts/worktree-reference.md`
