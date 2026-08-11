# Worktree Reference — Shared Detection Blocks

<!-- Lives in prompts/, not commands/: every .md file in ~/.claude/commands/ is registered as an
     invocable slash command regardless of frontmatter, and this is a reference doc, not a command. -->

Single source of truth for the detection logic shared by `/track`, `/track-and-start`,
`/cleanup`, and `/shipit`. Those commands say "run Project Detection (`~/.claude/prompts/worktree-reference.md`)"
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
  WORKTREE_PARENT="${MAIN_WORKTREE}/worktrees"
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

## Stack Detection

Detect and manage stacked PRs (branches whose parent is another worktree's branch, not the default
branch). Used by `/shipit` to set the correct PR base and link parent PRs, and by `/cleanup` to
detect children and emit a restack runbook. **IMPORTANT:** Only worktree-safe verbs are emitted here:
`gh stack link`, `gh stack unstack`, and graphql reads. Never use `gh stack init`, `gh stack sync`,
or `gh stack checkout` — those check out branches and are fatal when branches are held in sibling
worktrees.

### Is-stacked (this branch)

Detect whether the current branch's parent is another worktree's branch (not the default branch).
Cache-first from `.claude/github-cache.json` `.stack.isStacked`; if absent/null, detect the parent
as the nearest ancestor among other worktree branches. Emits `STACK_IS_STACKED` (true/false),
`STACK_PARENT_BRANCH`, and `STACK_PARENT_PR`.

```bash
# Check cache first (most specific)
GITHUB_CACHE=$(cat .claude/github-cache.json 2>/dev/null || echo '{}')
CACHED_STACKED=$(echo "$GITHUB_CACHE" | jq -r '.stack.isStacked // empty' 2>/dev/null)

STACK_IS_STACKED=false
STACK_PARENT_BRANCH=""
STACK_PARENT_PR=""

if [ "$CACHED_STACKED" != "false" ] && [ -n "$CACHED_STACKED" ]; then
  # Cache says stacked — use cached parent info
  STACK_IS_STACKED=$(echo "$GITHUB_CACHE" | jq -r '.stack.isStacked // false')
  STACK_PARENT_BRANCH=$(echo "$GITHUB_CACHE" | jq -r '.stack.parentBranch // ""')
  STACK_PARENT_PR=$(echo "$GITHUB_CACHE" | jq -r '.stack.parentPr // ""')
else
  # Cache says not stacked or missing — detect by looking at worktree branches
  # For each branch B from git worktree list (excluding main and current), test if B is an ancestor
  CURRENT_BRANCH=$(git branch --show-current)
  DEFAULT_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|^refs/remotes/origin/||')
  [ -z "$DEFAULT_BRANCH" ] && DEFAULT_BRANCH="main"

  # Get all worktree branches
  WORKTREE_BRANCHES=$(git worktree list --porcelain | awk '
    $1 == "worktree" { wt=$2; next }
    $1 == "branch" && wt != "" { 
      sub(/^refs\/heads\//, "", $2)
      print $2
      wt=""
    }
  ')

  BEST_ANCESTOR=""
  BEST_DISTANCE=999999

  while IFS= read -r branch; do
    # Skip main, current branch, and empty entries
    [ -z "$branch" ] && continue
    [ "$branch" = "$DEFAULT_BRANCH" ] && continue
    [ "$branch" = "$CURRENT_BRANCH" ] && continue

    # Test if this branch is an ancestor of HEAD
    if git merge-base --is-ancestor "$branch" HEAD 2>/dev/null; then
      # Count commits between ancestor and HEAD (tightest = fewest commits)
      DISTANCE=$(git rev-list --count "$branch..HEAD" 2>/dev/null || echo 999999)
      if [ "$DISTANCE" -lt "$BEST_DISTANCE" ]; then
        BEST_ANCESTOR="$branch"
        BEST_DISTANCE="$DISTANCE"
      fi
    fi
  done <<< "$WORKTREE_BRANCHES"

  if [ -n "$BEST_ANCESTOR" ]; then
    STACK_IS_STACKED=true
    STACK_PARENT_BRANCH="$BEST_ANCESTOR"
    # Try to look up the parent's PR number
    STACK_PARENT_PR=$(gh pr view "$STACK_PARENT_BRANCH" --json number -q '.number' 2>/dev/null || echo "")
  fi
fi
```

### Find children (of a branch)

Given a branch name, find all branches whose parent is that branch. Scan sibling worktree caches
first: `"$WORKTREE_PARENT"/*/.claude/github-cache.json` for entries whose `.stack.parentBranch`
matches. If no caches match, fall back to `gh pr list --base <branch> --state open --json number,headRefName`.
Emits the list of child branches and PR numbers.

```bash
# Requires: WORKTREE_PARENT and PARENT_BRANCH as inputs
# Emits: CHILD_BRANCHES (space-separated list of branch names and PR#s)

PARENT_BRANCH="$1"
CHILD_BRANCHES=""

# Scan worktree caches for children whose parent matches
if [ -d "$WORKTREE_PARENT" ]; then
  while IFS= read -r cache_file; do
    [ ! -f "$cache_file" ] && continue
    CACHED_PARENT=$(jq -r '.stack.parentBranch // empty' "$cache_file" 2>/dev/null)
    if [ "$CACHED_PARENT" = "$PARENT_BRANCH" ]; then
      CHILD_BRANCH=$(dirname "$cache_file" | xargs basename)
      CHILD_PR=$(jq -r '.stack.parentPr // ""' "$cache_file" 2>/dev/null)
      CHILD_BRANCHES="${CHILD_BRANCHES}${CHILD_BRANCH}:${CHILD_PR} "
    fi
  done < <(find "$WORKTREE_PARENT" -maxdepth 3 -name "github-cache.json" 2>/dev/null)
fi

# Fallback: scan open PRs against this branch
if [ -z "$CHILD_BRANCHES" ]; then
  OPEN_PRS=$(gh pr list --base "$PARENT_BRANCH" --state open --json number,headRefName -q '.[] | "\(.headRefName):\(.number)"' 2>/dev/null || true)
  CHILD_BRANCHES=$(echo "$OPEN_PRS" | tr '\n' ' ' | xargs)
fi

echo "Child branches of '$PARENT_BRANCH': $CHILD_BRANCHES"
```

### Stack number (remote)

Query GitHub for the remote stack number of a PR. This is needed to run `gh stack unstack`.

```bash
# Requires: REPO (owner/name) and PR_NUM as inputs
# Emits: STACK_NUMBER

REPO="$1"
PR_NUM="$2"

STACK_NUMBER=$(gh api graphql -f query='{repository(owner:"'"$(echo $REPO | cut -d/ -f1)"'",name:"'"$(echo $REPO | cut -d/ -f2)"'"){pullRequest(number:'"$PR_NUM"'){stack{number}}}}' -q '.data.repository.pullRequest.stack.number' 2>/dev/null || echo "")

if [ -n "$STACK_NUMBER" ]; then
  echo "Stack number: $STACK_NUMBER"
else
  echo "WARNING: Could not resolve stack number for PR #$PR_NUM"
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
