---
name: merge-and-cleanup
description: Merge a PR through the repo's real merge gate, then remove its worktree and update main. Run from the main worktree with a PR number or worktree path, e.g. /merge-and-cleanup 1022 or /merge-and-cleanup ../1020-some-worktree.
argument-hint: <PR number | worktree path>
allowed-tools: Read, Skill, Bash(git worktree:*), Bash(git status:*), Bash(git rev-parse:*), Bash(git symbolic-ref:*), Bash(git rev-list:*), Bash(git log:*), Bash(git fetch:*), Bash(gh pr view:*), Bash(gh pr merge:*), Bash(just:*), Bash(jq:*), Bash(ls:*), Bash(grep:*), Bash(head:*), Bash(awk:*), Bash(cut:*), Bash(tr:*), Bash(mv:*), Bash(printf:*), Bash(test:*)
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

### Phase 0 — Resolve PR and worktree (path or PR-number input)

Accept either a worktree path or a PR number from `$ARGUMENTS`. Detect which via directory existence check: if `$ARGUMENTS` resolves to a directory, treat as path mode; otherwise parse as PR number. Both branches converge on identical `PR_NUM`, `HEAD_REF`, and `WT` values before Phase 2.

```bash
# DETECTION: Is $ARGUMENTS a directory (path mode) or a PR number (PR-number mode)?
WT_CANDIDATE=$(cd "$ARGUMENTS" 2>/dev/null && pwd || echo "")

if [ -n "$WT_CANDIDATE" ]; then
  # PATH MODE: $ARGUMENTS resolves to an existing directory
  WT="$WT_CANDIDATE"
  
  # Verify it's a real git worktree
  if ! git -C "$WT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "ERROR: '$ARGUMENTS' is not a git worktree"
    exit 1
  fi
  
  # Ensure it's not the main worktree
  MAIN_WT=$(git worktree list --porcelain | grep '^worktree ' | head -1 | cut -d' ' -f2)
  if [ "$WT" = "$MAIN_WT" ]; then
    echo "ERROR: Target worktree resolves to main. Cannot merge from main."
    exit 1
  fi
  
  # Resolve HEAD_REF from the current branch in the worktree
  HEAD_REF=$(git -C "$WT" symbolic-ref --short -q HEAD 2>/dev/null || echo "")
  # If detached, leave HEAD_REF empty — Phase 2 will catch it with its own error
  
  # Resolve PR_NUM via cache-first + gh fallback
  CACHE_FILE="$WT/.claude/github-cache.json"
  GITHUB_CACHE=$(cat "$CACHE_FILE" 2>/dev/null || echo '{}')
  PR_NUM=$(printf '%s' "$GITHUB_CACHE" | jq -r '.pr.number // "unset"' 2>/dev/null)
  
  if [ "$PR_NUM" = "unset" ] || [ -z "$PR_NUM" ]; then
    # Cache miss — query GitHub
    PR_DATA=$(cd "$WT" && gh pr view --json number,state,headRefName,title,baseRefName,url 2>/dev/null || echo "")
    if [ -z "$PR_DATA" ]; then
      echo "ERROR: No PR found for worktree $WT (no cached PR and none open on GitHub)"
      exit 1
    fi
    
    PR_NUM=$(printf '%s' "$PR_DATA" | jq -r '.number')
    PR_STATE=$(printf '%s' "$PR_DATA" | jq -r '.state')
    PR_TITLE=$(printf '%s' "$PR_DATA" | jq -r '.title')
    PR_URL=$(printf '%s' "$PR_DATA" | jq -r '.url')
    
    # Backfill the cache with PR data (temp-file-then-mv pattern to avoid truncation)
    if [ ! -f "$CACHE_FILE" ]; then
      printf '{}' > "$CACHE_FILE"
    fi
    EXISTING=$(cat "$CACHE_FILE" 2>/dev/null || echo '{}')
    TMP=$(mktemp "$WT/.claude/github-cache.json.XXXXXX")
    if printf '%s' "$EXISTING" | jq \
        --argjson number "$PR_NUM" \
        --arg url "$PR_URL" \
        --arg state "$PR_STATE" \
        '. + {pr: {number: $number, url: $url, state: $state}}' > "$TMP"; then
      mv "$TMP" "$CACHE_FILE"
    else
      rm -f "$TMP"
      echo "WARNING: failed to update $CACHE_FILE (jq error); continuing without cache" >&2
    fi
  else
    # Cache hit — fetch full PR details to verify state
    PR_DATA=$(cd "$WT" && gh pr view "$PR_NUM" --json number,state,title,headRefName,baseRefName 2>/dev/null || echo "")
    if [ -z "$PR_DATA" ]; then
      echo "ERROR: PR #$PR_NUM not found or not accessible"
      exit 1
    fi
    PR_STATE=$(printf '%s' "$PR_DATA" | jq -r '.state')
    PR_TITLE=$(printf '%s' "$PR_DATA" | jq -r '.title')
  fi
  
  # Validate PR is open
  if [ "$PR_STATE" != "OPEN" ]; then
    echo "ERROR: PR #$PR_NUM is not open (state: $PR_STATE)"
    exit 1
  fi
  
  echo "PR #$PR_NUM: $PR_TITLE"
  echo "Branch: $HEAD_REF"
  echo "Resolved worktree: $WT"
  echo "PR_NUM=$PR_NUM"
  echo "WT=$WT"

else
  # PR-NUMBER MODE: $ARGUMENTS is not a directory; parse as PR number
  
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
  PR_DATA=$(gh pr view "$PR_NUM" --json headRefName,state,title,baseRefName)
  
  if [ -z "$PR_DATA" ]; then
    echo "ERROR: PR #$PR_NUM not found or not accessible"
    exit 1
  fi
  
  # Extract fields (printf, not echo — zsh echo can mangle backslash escapes in
  # piped JSON; see commit a96b06b's fix to shipit.md for the same bug class)
  HEAD_REF=$(printf '%s' "$PR_DATA" | jq -r '.headRefName')
  PR_STATE=$(printf '%s' "$PR_DATA" | jq -r '.state')
  PR_TITLE=$(printf '%s' "$PR_DATA" | jq -r '.title')
  
  if [ "$PR_STATE" != "OPEN" ]; then
    echo "ERROR: PR #$PR_NUM is not open (state: $PR_STATE)"
    exit 1
  fi
  
  echo "PR #$PR_NUM: $PR_TITLE"
  echo "Branch: $HEAD_REF"
  
  # Resolve worktree from git worktree list using exact branch match.
  # Directory names lie and must never be matched. Example: a PR's branch might be
  # `chore/1020-something` living in a worktree directory literally named
  # `1020-something`, or `feature/coach-drop-sourcesdir-persistence` living in a
  # directory named `coach-sources-dir-removal`. Match only the exact porcelain line
  # from `git worktree list`.
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
  echo "PR_NUM=$PR_NUM"
  echo "WT=$WT"
fi
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

# Fetch first so the @{u} comparisons below reflect the actual remote state,
# not a stale local view of origin from before this session started.
git -C "$WT" fetch origin "$HEAD_REF" 2>/dev/null || true

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

# Check 4: Unpushed commits — a failed rev-list must not be mistaken for "0 unpushed"
if ! UNPUSHED_COUNT=$(git -C "$WT" rev-list "@{u}.." --count 2>&1); then
  echo "ERROR: Could not determine unpushed commit count in $WT: $UNPUSHED_COUNT"
  exit 1
fi
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

1. `just -f "$WT/justfile" --summary` lists a `merge` recipe → run `just merge`
2. Else read `.commands.check` from `$WT/.claude/repo-cache.json` via `jq`, then run `gh pr merge --squash`
3. Else run `gh pr merge --squash` alone, with no gate — this is silent unless flagged, so every reach of this path prints a loud, distinct marker (`⚠️ merged with NO GATE`) rather than looking like a gated merge

Run the actual merge-gate command in a **subshell**, never a bare `cd`, since `/cleanup` deletes this worktree directory shortly after and a lingering shell cwd inside it makes every later command fail with "Path does not exist":

```bash
echo "=== Phase 3: Merge Gate ==="

# Reentrancy guard: refuse a second concurrent/repeat invocation against the
# same worktree while a prior run's lock is still present. The lock is not
# removed on success (this worktree is about to be deleted by /cleanup
# anyway) or on failure (a human should inspect before retrying) — clear it
# manually with `mv` if you need to re-run after a failure.
LOCK_FILE="$WT/.claude/.merge-and-cleanup.lock"
if test -f "$LOCK_FILE"; then
  echo "ERROR: Lock file $LOCK_FILE already exists — a merge-and-cleanup run may already be in progress or a prior run failed without cleanup."
  echo "RECOMMENDATION: inspect the worktree, then 'mv $LOCK_FILE ${LOCK_FILE}.stale' to clear it before re-running."
  exit 1
fi
printf 'PR #%s locked by merge-and-cleanup\n' "$PR_NUM" > "$LOCK_FILE"

MERGE_GATE_USED=""

# Path 1: just merge (if recipe exists in target worktree). --summary lists
# recipe names space-separated on one line; --list's output is human-oriented
# columns and a plain "^merge" anchor never matches it.
if [ -f "$WT/justfile" ] && just -f "$WT/justfile" --summary 2>/dev/null | tr ' ' '\n' | grep -qx merge; then
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
    if ! jq -e . "$REPO_CACHE" >/dev/null 2>&1; then
      echo "ERROR: $REPO_CACHE exists but could not be parsed as JSON"
      exit 1
    fi
    CHECK_CMD=$(jq -r '.commands.check // empty' "$REPO_CACHE")
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
  echo "⚠️  merged with NO GATE"
  MERGE_GATE_USED="gh pr merge (no gate)"
fi

# Perform the actual merge — but only on paths 2/3. Path 1 ("just merge") already
# IS the merge action; calling gh pr merge --squash again afterward would hit an
# already-merged PR and fail, incorrectly halting a command that just succeeded.
if [ "$MERGE_GATE_USED" = "just merge" ]; then
  echo "✓ PR #$PR_NUM merged successfully via 'just merge'"
else
  echo "Running: gh pr merge --squash $PR_NUM"
  if MERGE_ERR=$(gh pr merge --squash "$PR_NUM" 2>&1); then
    echo "✓ PR #$PR_NUM merged successfully"

    # Both path 2 (gate-only) and path 3 (no gate) perform the merge here —
    # write the cache on either success, not just the no-gate fallback.
    CACHE_FILE="$WT/.claude/github-cache.json"
    if [ ! -f "$CACHE_FILE" ]; then
      printf '{}' > "$CACHE_FILE"
    fi
    if jq --arg state "MERGED" '.pr.state = $state' "$CACHE_FILE" > "$CACHE_FILE.tmp"; then
      mv "$CACHE_FILE.tmp" "$CACHE_FILE"
      echo "(Wrote .pr.state = MERGED to cache)"
    else
      echo "WARNING: failed to update $CACHE_FILE with merged state"
      mv "$CACHE_FILE.tmp" "${CACHE_FILE}.failed" 2>/dev/null || true
    fi
  else
    echo "ERROR: gh pr merge failed: $MERGE_ERR"
    echo "$MERGE_ERR" | grep -qi "squash" && \
      echo "NAMED ERROR: this repo may not allow squash merges — edit Phase 3's --squash flag to a merge method this repo allows."
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
