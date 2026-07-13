# Worktree Reference — Shared Detection Blocks

<!-- Lives in prompts/, not commands/: every .md file in ~/.claude/commands/ is registered as an
     invocable slash command regardless of frontmatter, and this is a reference doc, not a command. -->

Single source of truth for the detection logic shared by `/track`, `/track-and-start`, and
`/cleanup`. Those commands say "run Project Detection (`~/.claude/prompts/worktree-reference.md`)"
instead of restating these blocks.

## Project Detection

Resolves the repo, main worktree, worktree parent, issue cache, and project root.

```bash
# 1. Repo identity (empty in a repo with no GitHub remote — local plan mode relies on this)
REPO=$(gh repo view --json nameWithOwner -q '.nameWithOwner' 2>/dev/null || echo "")

# 2. Main worktree (first entry is always main)
MAIN_WORKTREE=$(git worktree list --porcelain | grep '^worktree ' | head -1 | cut -d' ' -f2)

# 3. Detect worktree parent from existing worktrees
SECOND_WORKTREE=$(git worktree list --porcelain | grep '^worktree ' | sed -n '2p' | cut -d' ' -f2)
if [ -n "$SECOND_WORKTREE" ]; then
  WORKTREE_PARENT=$(dirname "$SECOND_WORKTREE")
else
  WORKTREE_PARENT="${MAIN_WORKTREE}/.claude/worktrees"
fi

# 4. Issue cache at worktree parent level
CACHE_FILE="${WORKTREE_PARENT}/issues.json"

# 5. Current GitHub user for assignment (issue-creating commands only)
ASSIGNEE=$(gh api user -q '.login' 2>/dev/null || echo "")

# 6. Project root (parent of worktrees/ structure, or main worktree itself)
PARENT_BASENAME=$(basename "$(dirname "$MAIN_WORKTREE")")
if [ "$PARENT_BASENAME" = "worktrees" ]; then
  PROJECT_ROOT=$(dirname "$(dirname "$MAIN_WORKTREE")")
else
  PROJECT_ROOT="$MAIN_WORKTREE"
fi
```

**If not in a git repo:** error — every command here needs one.

**If `REPO` is empty** (no GitHub remote): GitHub-mode commands error with a message about needing a
GitHub remote. Local plan mode does not — it runs on git alone, so steps 2–3 and 6 must not depend
on `REPO` or `ASSIGNEE`.

## Graft Detection

Checks whether `graft` (the worktree manager, if installed) manages this repo's worktrees — detected
by presence on `PATH` plus an entry in its config. Sets `USE_GRAFT` and
`GRAFT_REPO_NAME` for commands that create worktrees; commands that *remove* worktrees should also
verify graft tracks the specific worktree (see the cleanup variant below).

```bash
USE_GRAFT=false
GRAFT_REPO_NAME=""
if command -v graft >/dev/null 2>&1; then
  GRAFT_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/graft/config.json"
  if [ -f "$GRAFT_CONFIG" ]; then
    GRAFT_REPO_NAME=$(jq -r --arg path "$MAIN_WORKTREE" '
      .repos // {} | to_entries[] |
      select(.value.path == $path) | .key
    ' "$GRAFT_CONFIG" 2>/dev/null | head -1)
    if [ -n "$GRAFT_REPO_NAME" ]; then
      USE_GRAFT=true
    fi
  fi
fi
```

**Cleanup variant** — additionally verify graft tracks the target worktree (it might have been
created manually). Requires `CURRENT_WORKTREE`, set by the caller (`/cleanup`) before this block:

```bash
if [ "$USE_GRAFT" = "true" ]; then
  GRAFT_WORKTREE_NAME=$(basename "$CURRENT_WORKTREE")
  if ! graft ls -r "$GRAFT_REPO_NAME" 2>/dev/null | grep -q "$GRAFT_WORKTREE_NAME"; then
    USE_GRAFT=false
  fi
fi
```

## Local Plan Mode Detection

When the project root has both a `plans/` directory and an **array-format** `issues.json`, commands
use local plan tracking instead of GitHub issues. Requires `PROJECT_ROOT` from Project Detection.

```bash
PLANS_DIR="${PROJECT_ROOT}/plans"
PROJECT_ISSUES="${PROJECT_ROOT}/issues.json"

LOCAL_MODE=false
if [ -d "$PLANS_DIR" ] && [ -f "$PROJECT_ISSUES" ]; then
  IS_ARRAY=$(jq 'type == "array"' "$PROJECT_ISSUES" 2>/dev/null)
  if [ "$IS_ARRAY" = "true" ]; then
    LOCAL_MODE=true
  fi
fi
```

If `LOCAL_MODE` is false, fall through to the command's normal GitHub flow.

## In-Worktree Check

Detects whether the current directory is inside a non-main worktree (used by pivot detection and
cleanup's no-argument mode):

```bash
COMMON_DIR=$(git rev-parse --git-common-dir 2>/dev/null)
GIT_DIR=$(git rev-parse --git-dir 2>/dev/null)
if [ "$COMMON_DIR" = "$GIT_DIR" ]; then
  IN_WORKTREE=false   # main worktree (or plain repo)
else
  IN_WORKTREE=true
fi
```
