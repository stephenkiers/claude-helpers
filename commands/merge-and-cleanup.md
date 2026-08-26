---
name: merge-and-cleanup
description: Merge a PR through the repo's real merge gate, then remove its worktree and update main. Run from the main worktree with a PR number, e.g. /merge-and-cleanup 1022.
argument-hint: <PR number>
allowed-tools: Read, Skill, AskUserQuestion, Bash(git worktree:*), Bash(git status:*), Bash(git rev-parse:*), Bash(git symbolic-ref:*), Bash(git rev-list:*), Bash(git branch:*), Bash(git log:*), Bash(git fetch:*), Bash(gh pr view:*), Bash(gh pr merge:*), Bash(just:*), Bash(jq:*), Bash(ls:*), Bash(grep:*), Bash(head:*), Bash(awk:*), Bash(cut:*), Bash(mv:*), Bash(printf:*), Bash(test:*)
model: haiku
---

# Merge and Cleanup

Merge a PR through the repo's merge gate (discovered automatically), then clean up its worktree and branch.

## Permission & Safety Philosophy

**Goal: merge safely, then clean up automatically without losing work.**

- The push gate (Phase 2) is the "one hard-fail" stop — if everything is pushed, `/cleanup` cannot lose work.
- Merge gate failures stop the command (non-zero exit halts Phase 3, changes nothing).
- Cleanup failures after a successful merge are reported loudly but do not reverse the merge (it is already irreversible on GitHub).
- This command deliberately lacks `Bash(rm:*)` and `Bash(git push:*)` in its `allowed-tools` — push and worktree removal are delegated to other phases or `/cleanup`, never done here.

## Workflow

### Phase 0 — Resolve the PR

Parse a bare integer from `$ARGUMENTS` (tolerate `1022`, `#1022`, `PR 1022`, or a URL). Then verify the PR exists and is open.

```bash
# Extract PR number: prefer a URL's /pull/<n> segment (a repo or org name earlier
# in the URL could itself be numeric, so the naive "first digit run" is not safe
# for URLs); fall back to the first digit run for the bare/#/PR-prefixed forms.
PR_NUM=$(echo "$ARGUMENTS" | grep -oE 'pull/[0-9]+' | grep -oE '[0-9]+' | head -1)
if [ -z "$PR_NUM" ]; then
  PR_NUM=$(echo "$ARGUMENTS" | grep -oE '[0-9]+' | head -1)
fi

if [ -z "$PR_NUM" ]; then
  echo "ERROR: No PR number found in '$ARGUMENTS'"
  echo "Usage: /merge-and-cleanup 1022  (or #1022, PR 1022, or a URL)"
  exit 1
fi

# Fetch PR details
PR_DATA=$(gh pr view "$PR_NUM" --json headRefName,state,title,url,baseRefName 2>/dev/null)

if [ -z "$PR_DATA" ]; then
  echo "ERROR: PR #$PR_NUM not found or not accessible"
  exit 1
fi

# Extract fields
HEAD_REF=$(echo "$PR_DATA" | jq -r '.headRefName')
PR_STATE=$(echo "$PR_DATA" | jq -r '.state')
PR_TITLE=$(echo "$PR_DATA" | jq -r '.title')
PR_URL=$(echo "$PR_DATA" | jq -r '.url')

if [ "$PR_STATE" != "OPEN" ]; then
  echo "ERROR: PR #$PR_NUM is not open (state: $PR_STATE)"
  exit 1
fi

echo "PR #$PR_NUM: $PR_TITLE"
echo "Branch: $HEAD_REF"
```

### Phase 1 — Resolve branch → worktree

Directory names lie and must never be matched. Example: a PR's branch might be `chore/1020-something` living in a worktree directory literally named `1020-something`, or `feature/coach-drop-sourcesdir-persistence` living in a directory named `coach-sources-dir-removal`. Match only the exact porcelain line from `git worktree list`.

```bash
# Resolve worktree from git worktree list using exact branch match
WT=$(git worktree list --porcelain | awk -v b="refs/heads/$HEAD_REF" '
  /^worktree /{w=$2} $0=="branch "b{print w; exit}')

if [ -z "$WT" ]; then
  echo "ERROR: No local worktree found for branch $HEAD_REF"
  exit 1
fi

# Verify the worktree directory exists
if ! test -d "$WT"; then
  echo "ERROR: Worktree path $WT does not exist (may have been pruned)"
  exit 1
fi

# Ensure it's not the main worktree
MAIN_WT=$(git worktree list --porcelain | grep '^worktree ' | head -1 | cut -d' ' -f2)
if [ "$WT" = "$MAIN_WT" ]; then
  echo "ERROR: Target worktree resolves to main. Cannot merge from main."
  exit 1
fi

echo "Resolved worktree: $WT"
```

### Phase 2 — Push gate (the one hard stop)

This is the "one hard-fail push gate" — if everything is pushed, later worktree removal in `/cleanup` cannot lose work. All checks run via `git -C "$WT"`. This gate never pushes or discards anything itself (no `git push` or destructive commands are in this command's `allowed-tools` — the user runs the recommended fix themselves and re-invokes).

| Check | Detection | Recommendation |
|---|---|---|
| Detached HEAD | `symbolic-ref -q HEAD` fails | `git -C <wt> checkout <branch>` |
| Uncommitted / untracked | `status --porcelain` non-empty | Show `--short`; commit or discard deliberately. Never auto-discard. |
| No upstream | `rev-parse @{u}` fails | `git -C <wt> push -u origin <branch>` |
| Unpushed commits | `rev-list @{u}.. --count` > 0 | Show `log @{u}.. --oneline`, then `git -C <wt> push` |

```bash
echo "=== Phase 2: Push Gate ==="

# Check 1: Detached HEAD
if ! git -C "$WT" symbolic-ref -q HEAD >/dev/null 2>&1; then
  echo "ERROR: Detached HEAD in $WT"
  echo "RECOMMENDATION: git -C $WT checkout $HEAD_REF"
  exit 1
fi

# Check 2: Uncommitted / untracked changes
DIRTY=$(git -C "$WT" status --porcelain)
if [ -n "$DIRTY" ]; then
  echo "ERROR: Uncommitted or untracked changes in $WT:"
  git -C "$WT" status --short
  echo "RECOMMENDATION: Commit or deliberately discard the changes, then re-invoke"
  exit 1
fi

# Check 3: No upstream tracking branch
if ! git -C "$WT" rev-parse "@{u}" >/dev/null 2>&1; then
  echo "ERROR: Branch $HEAD_REF has no upstream tracking branch"
  echo "RECOMMENDATION: git -C $WT push -u origin $HEAD_REF"
  exit 1
fi

# Check 4: Unpushed commits
UNPUSHED_COUNT=$(git -C "$WT" rev-list "@{u}.." --count 2>/dev/null || echo 0)
if [ "$UNPUSHED_COUNT" -gt 0 ]; then
  echo "ERROR: $UNPUSHED_COUNT unpushed commits in $WT:"
  git -C "$WT" log "@{u}.." --oneline
  echo "RECOMMENDATION: git -C $WT push"
  exit 1
fi

echo "✓ Push gate passed: branch is clean and fully pushed"
```

### Phase 3 — Resolve and run the merge gate

Auto-detected, no config key (repo-cache.json is gitignored and per-worktree, so a `commands.merge` key would not persist to new worktrees — this mirrors the design in `prompts/shipit-reference.md`). Resolution order:

1. `just -f "$WT/justfile" --list` contains a `merge` recipe → run `just merge`
2. Else read `.commands.check` from `$WT/.claude/repo-cache.json` via `jq`, then run `gh pr merge --squash`
3. Else run `gh pr merge --squash` alone

Run the actual merge-gate command in a **subshell**, never a bare `cd`, since `/cleanup` deletes this worktree directory shortly after and a lingering shell cwd inside it makes every later command fail with "Path does not exist":

```bash
echo "=== Phase 3: Merge Gate ==="

MERGE_GATE_USED=""

# Path 1: just merge (if recipe exists in target worktree)
if [ -f "$WT/justfile" ] && just -f "$WT/justfile" --list 2>/dev/null | grep -q "^merge"; then
  echo "Found 'just merge' recipe in $WT/justfile"
  echo "(This may take many minutes if it invokes E2E build/test)"
  if ( cd "$WT" && just merge ); then
    MERGE_GATE_USED="just merge"
    echo "✓ Merge succeeded via 'just merge'"
  else
    echo "ERROR: 'just merge' failed"
    exit 1
  fi
fi

# Path 2: check repo-cache.json for a check gate, then gh pr merge --squash
if [ -z "$MERGE_GATE_USED" ]; then
  REPO_CACHE="$WT/.claude/repo-cache.json"
  if [ -f "$REPO_CACHE" ]; then
    CHECK_CMD=$(jq -r '.commands.check // empty' "$REPO_CACHE" 2>/dev/null)
    if [ -n "$CHECK_CMD" ]; then
      echo "Found merge gate in repo-cache.json: $CHECK_CMD"
      if ( cd "$WT" && eval "$CHECK_CMD" ); then
        MERGE_GATE_USED="repo-cache check"
        echo "✓ Merge gate passed"
      else
        echo "ERROR: Merge gate failed"
        exit 1
      fi
    fi
  fi
fi

# Path 3: plain gh pr merge --squash (no gate)
if [ -z "$MERGE_GATE_USED" ]; then
  echo "No merge gate found (no 'just merge' recipe and no repo-cache.json check)"
  MERGE_GATE_USED="gh pr merge (no gate)"
fi

# Perform the actual merge — but only on paths 2/3. Path 1 ("just merge") already
# IS the merge action; calling gh pr merge --squash again afterward would hit an
# already-merged PR and fail, incorrectly halting a command that just succeeded.
if [ "$MERGE_GATE_USED" = "just merge" ]; then
  echo "✓ PR #$PR_NUM merged successfully via 'just merge'"
else
  echo "Running: gh pr merge --squash $PR_NUM"
  if gh pr merge --squash "$PR_NUM"; then
    echo "✓ PR #$PR_NUM merged successfully"

    # Only write cache on the fallback path (path 3) — path 2's check command
    # is a gate only, not the merge, but its own tooling is expected to own
    # cache updates the same way path 1's does.
    if [ "$MERGE_GATE_USED" = "gh pr merge (no gate)" ]; then
      CACHE_FILE="$WT/.claude/github-cache.json"
      if [ ! -f "$CACHE_FILE" ]; then
        printf '{}' > "$CACHE_FILE"
      fi
      jq --arg state "MERGED" '.pr.state = $state' "$CACHE_FILE" > "$CACHE_FILE.tmp" && mv "$CACHE_FILE.tmp" "$CACHE_FILE"
      echo "(Wrote .pr.state = MERGED to cache)"
    fi
  else
    echo "ERROR: gh pr merge failed"
    exit 1
  fi
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
MATCH_LIST=$(eval ls -d "${WT}*/" 2>/dev/null)
MATCH_COUNT=$(echo "$MATCH_LIST" | grep -c .)

if [ "$MATCH_COUNT" -ne 1 ]; then
  echo "ERROR: Worktree path '$WT' is ambiguous for /cleanup's glob resolution (matches: $MATCH_LIST)"
  exit 1
fi

echo "Path verified unambiguous — invoking /cleanup with: $WT"
```

**Now actually invoke the `cleanup` skill via the `Skill` tool**, passing `$WT` (the absolute worktree path, no trailing slash) as its argument — this is a tool call the agent running this command makes directly, not a bash command, so it isn't inside the block above. Only proceed to this call after the bash block above exits 0.

#### Non-duplication rules

- **Stacked-PR handling** (detecting children, restack runbooks) lives entirely in `/cleanup` and `prompts/worktree-reference.md`. This command must not detect stack layout, find children, or restack anything itself (per ADR-0011, ADR-0010).
- This command must not compute `WORKTREE_PARENT` or `PROJECT_ROOT` itself — that is owned by Project Detection elsewhere.
- This command must not reimplement worktree removal or branch deletion — `/cleanup` owns that.

**If `/cleanup` fails after a successful merge, the merge is irreversible but cleanup is idempotent.** Print the exact recovery command and stop:
```
/cleanup <abs-path>
```

### Phase 5 — Summary

Example output (with PR 1022 as the illustrative example):

```
PR #1022    ✅ merged via `just merge` (E2E gate passed)
WORKTREE    ✅ removed  /path/to/1020-…
BRANCH      ✅ deleted  chore/1020-…
MAIN        ✅ fast-forwarded to <sha>  |  checks: pass
```

Use ⛔ for a halted phase; omit phases that never ran.

## Files

- `commands/merge-and-cleanup.md` — this command
- `tests/test_merge_and_cleanup.py` — its test file (written by a separate pass)
- Reused, not modified: `commands/cleanup.md`, `prompts/worktree-reference.md`
