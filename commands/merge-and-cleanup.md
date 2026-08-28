---
name: merge-and-cleanup
description: Merge a PR through the repo's real merge gate, then remove its worktree and update main. Run from the main worktree with a PR number or worktree path, e.g. /merge-and-cleanup 1022 or /merge-and-cleanup ../1020-some-worktree.
argument-hint: <PR number | worktree path>
allowed-tools: Read, Skill, Bash(git worktree:*), Bash(git status:*), Bash(git rev-parse:*), Bash(git symbolic-ref:*), Bash(git rev-list:*), Bash(git log:*), Bash(git fetch:*), Bash(gh pr view:*), Bash(gh pr merge:*), Bash(just:*), Bash(jq:*), Bash(ls:*), Bash(grep:*), Bash(head:*), Bash(awk:*), Bash(cut:*), Bash(tr:*), Bash(mv:*), Bash(printf:*), Bash(test:*), Bash(python3 -m scripts.workflow.cli:*), Bash(dirname:*), Bash(readlink:*)
model: haiku
---

# Merge and Cleanup

Merge a PR through the repo's merge gate (discovered automatically), then clean up its worktree and branch. Accept either a PR number or a worktree path as input.

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
- This command deliberately lacks `Bash(rm:*)` and `Bash(git push:*)` in its `allowed-tools` — push and worktree removal are delegated to other phases or `/cleanup`, never done here.

## Workflow

### Phase 0 & 1 — Resolve PR, worktree, and run push gate

Accept either a worktree path or a PR number from `$ARGUMENTS`. The plan resolves the PR/worktree and validates the push gate:
- PR/worktree resolution (cache-first for path mode)
- 4-check push gate: detached HEAD, uncommitted changes, no upstream, unpushed commits

```bash
# Resolve $ARGUMENTS to an absolute path if it's an existing filesystem path; if it's a PR number, pass through unchanged
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
```

### Phase 3 — Run the merge gate

Auto-detected, no config key (repo-cache.json is gitignored and per-worktree, so a `commands.merge` key would not persist to new worktrees — this mirrors the design in `prompts/shipit-reference.md`). Resolution order:

1. `just -f "$WT/justfile" --summary` lists a `merge` recipe → run `just merge`
2. Else read `.commands.check` from `$WT/.claude/repo-cache.json` via `jq`, then run `gh pr merge --squash`
3. Else run `gh pr merge --squash` alone, with no gate — this is silent unless flagged, so every reach of this path prints a loud, distinct marker (`⚠️ merged with NO GATE`) rather than looking like a gated merge

```bash
echo "=== Phase 3: Merge Gate ==="

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
APPLY_RESULT=$(echo "$PLAN_JSON" | PYTHONPATH="$CLAUDE_HELPERS_DIR" python3 -m scripts.workflow.cli merge apply -)
APPLY_RESULT_CODE=$?

if [ $APPLY_RESULT_CODE -ne 0 ]; then
  echo "ERROR: Merge apply failed"
  exit 1
fi

# Extract results
PR_MERGED=$(echo "$APPLY_RESULT" | jq -r '.pr_merged // false')
MERGE_GATE_USED=$(echo "$APPLY_RESULT" | jq -r '.merge_gate_used // "unknown"')

if [ "$PR_MERGED" = "true" ]; then
  echo "✓ PR #$PR_NUM merged successfully via $MERGE_GATE_USED"
else
  echo "ERROR: PR merge result indicated failure"
  exit 1
fi
```

### Phase 4 — Hand off to `/cleanup`

Confirm the merge actually landed, then invoke `/cleanup` via the Skill tool. Pre-verify the path expands to exactly one directory.

```bash
echo "=== Phase 4: Cleanup ==="

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
