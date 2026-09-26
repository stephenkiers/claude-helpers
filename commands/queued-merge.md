---
name: queued-merge
description: Merge a PR through the local merge queue, testing it against the exact base it lands on. The queue serializes PRs and merges them one at a time in arrival order. Run from the worktree you want to merge, or from the main worktree with a PR number.
argument-hint: [PR number]
allowed-tools: Read, Bash(git worktree:*), Bash(git rev-parse:*), Bash(git status:*), Bash(gh pr view:*), Bash(printf:*), Bash(jq:*), Bash(grep:*), Bash(tail:*), Bash(cat:*), Bash(test:*), Bash(ls:*), Bash(rm:*)
model: haiku
---

# Queued Merge

Merge a PR through the local merge queue when the repo has a merge-queue configuration. The queue
serializes all PRs and merges them strictly one at a time, in arrival order, testing each against
the exact base it will land on. Several PRs can be queued at once and they remain serialized even
if one fails — failures are kicked back without blocking the rest.

Run from the worktree you want to merge (auto-detects PR), or from the main worktree with an explicit
PR number.

The merge queue handles:
- **Serialization**: one PR merges at a time, in arrival order, via a kernel `flock` and ticket files.
- **Full gate testing**: every PR runs the configured merge gate (e.g., `just merge` or a test suite) against the exact `origin/<base>` it will land on.
- **Unverified main detection**: if the base branch has untrailered commits (showing it may not be in a trusted state), the queue runs the main gate in a scratch worktree before proceeding.
- **Failure handling**: conflicts, gate failures, and push rejections kick back the PR without blocking the queue.
- **Exit codes**: 0 = MERGED, 2 = KICKBACK (conflict/gate failure/push failure), 3 = REFUSED (config/stacked PR/dirty tree), 1 = INTERNAL_ERROR.

## Workflow

### Phase 1 — Resolve PR and worktree

Auto-detect the PR from the current worktree (when no argument given), or accept a PR number from `$ARGUMENTS`. Validate the PR is OPEN and is targeted at the default branch (stacked PRs are refused).

```bash
# Resolve $ARGUMENTS to a PR number, or use auto-detection
if [ -z "$ARGUMENTS" ] || [ "$ARGUMENTS" = "" ]; then
  # Auto-detection: resolve from current worktree
  CURRENT_BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null) || CURRENT_BRANCH=""
  if [ -z "$CURRENT_BRANCH" ]; then
    echo "ERROR: auto-detection requires a symbolic ref (attached branch); detached HEAD found"
    exit 3
  fi

  # Use gh to find the PR by branch name
  PR_NUM=$(gh pr view "$CURRENT_BRANCH" --json number -q '.number' 2>/dev/null)
  if [ -z "$PR_NUM" ] || [ "$PR_NUM" = "" ]; then
    echo "ERROR: no PR found for branch '$CURRENT_BRANCH' — run from a worktree with a PR, or specify PR number explicitly"
    exit 3
  fi
else
  # Explicit PR number from arguments
  PR_NUM="$ARGUMENTS"
fi

# Validate PR is OPEN and targeted at default branch
PR_INFO=$(gh pr view "$PR_NUM" --json state,baseRefName,headRefName,number,title 2>/dev/null)
if [ -z "$PR_INFO" ]; then
  echo "ERROR: PR #$PR_NUM not found or inaccessible"
  exit 3
fi

PR_STATE=$(printf '%s' "$PR_INFO" | jq -r '.state' 2>/dev/null)
PR_BASE=$(printf '%s' "$PR_INFO" | jq -r '.baseRefName' 2>/dev/null)
PR_HEAD=$(printf '%s' "$PR_INFO" | jq -r '.headRefName' 2>/dev/null)
PR_TITLE=$(printf '%s' "$PR_INFO" | jq -r '.title' 2>/dev/null)

if [ "$PR_STATE" != "OPEN" ]; then
  echo "ERROR: PR #$PR_NUM is not OPEN (state: $PR_STATE)"
  exit 3
fi

# Check if base equals default branch
DEFAULT_BRANCH=$(gh repo view --json defaultBranchRef --jq '.defaultBranchRef.name' 2>/dev/null)
if [ "$PR_BASE" != "$DEFAULT_BRANCH" ]; then
  echo "ERROR: PR #$PR_NUM is targeted at '$PR_BASE', not the default branch '$DEFAULT_BRANCH' (stacked PRs are not supported by the merge queue)"
  exit 3
fi

echo "PR #$PR_NUM: $PR_TITLE"
echo "Branch: $PR_HEAD (base: $PR_BASE)"
```

### Phase 2 — Find the worktree

Resolve the worktree path. If run from the main worktree, the PR worktree must exist and be locatable.

```bash
# Determine which worktree to use
# If running from the main worktree, we need to find the PR worktree
# If running from a linked worktree, use the current one

MAIN_WORKTREE=$(git rev-parse --git-common-dir 2>/dev/null | xargs dirname)
CURRENT_WORKTREE=$(pwd)

if [ "$CURRENT_WORKTREE" = "$MAIN_WORKTREE" ]; then
  # Running from main worktree; find the PR worktree
  # Try to find a worktree with the PR branch name
  PR_WORKTREE=$(git worktree list --porcelain 2>/dev/null | grep "branch $PR_HEAD" | awk '{print $1}' | head -1)
  
  if [ -z "$PR_WORKTREE" ]; then
    echo "ERROR: PR #$PR_NUM worktree not found. Create a worktree for branch '$PR_HEAD' first."
    exit 3
  fi
else
  # Running from a linked worktree; use the current one
  PR_WORKTREE="$CURRENT_WORKTREE"
fi

echo "Worktree: $PR_WORKTREE"
```

### Phase 3 — Run the merge queue (background)

Launch the merge queue as a background process so the session can be re-invoked on exit. The queue
will handle serialization, testing, and retry internally.

**Start this in the background** so the command doesn't block:

```bash
echo "Enqueuing PR #$PR_NUM in the local merge queue..."

# Locate the merge-queue script
if ! command -v merge-queue &>/dev/null; then
  if [ -x "$HOME/.claude/scripts/merge-queue" ]; then
    MERGE_QUEUE_SCRIPT="$HOME/.claude/scripts/merge-queue"
  else
    echo "ERROR: merge-queue script not found in ~/.claude/scripts/ — run /setup-local"
    exit 1
  fi
else
  MERGE_QUEUE_SCRIPT=$(command -v merge-queue)
fi

# Run the merge queue in the PR worktree (background)
cd "$PR_WORKTREE"
"$MERGE_QUEUE_SCRIPT" enqueue --no-claude 2>&1
```

**When the background process completes**, move to Phase 4.

### Phase 4 — Read result and report

After the merge queue finishes (when you see the completion notification), read `result.json` from
the git state directory and report the outcome.

```bash
# The result.json lives in the git state dir
STATE_DIR="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)/merge-queue"

if [ ! -f "$STATE_DIR/result.json" ]; then
  echo "ERROR: result.json not found at $STATE_DIR/result.json"
  exit 1
fi

# Extract outcome and details
RESULT_JSON=$(cat "$STATE_DIR/result.json" 2>/dev/null)
OUTCOME=$(printf '%s' "$RESULT_JSON" | jq -r '.outcome // "UNKNOWN"' 2>/dev/null)
REASON=$(printf '%s' "$RESULT_JSON" | jq -r '.reason // ""' 2>/dev/null)
DETAILS=$(printf '%s' "$RESULT_JSON" | jq -r '.details // ""' 2>/dev/null)

# Report the outcome
case "$OUTCOME" in
  MERGED)
    echo "✓ PR #$PR_NUM merged successfully via the local merge queue"
    exit 0
    ;;
  KICKBACK)
    echo "⚠ PR #$PR_NUM was kicked back:"
    echo "  $REASON"
    if [ -n "$DETAILS" ]; then
      echo "  Details: $DETAILS"
    fi
    exit 2
    ;;
  PUSHED_NOT_MERGED)
    echo "⚠ PR #$PR_NUM was pushed but merge failed:"
    echo "  $REASON"
    exit 2
    ;;
  REFUSED)
    echo "✗ PR #$PR_NUM was refused at enqueue:"
    echo "  $REASON"
    if [ -n "$DETAILS" ]; then
      echo "  Details: $DETAILS"
    fi
    exit 3
    ;;
  INTERNAL_ERROR)
    echo "✗ Internal error in the merge queue:"
    echo "  $REASON"
    if [ -n "$DETAILS" ]; then
      echo "  Details: $DETAILS"
    fi
    exit 1
    ;;
  *)
    echo "ERROR: unknown outcome '$OUTCOME'"
    exit 1
    ;;
esac
```

## Exit Codes

- **0** — `MERGED`: the PR merged successfully
- **2** — `KICKBACK` or `PUSHED_NOT_MERGED`: the PR failed a gate step, had a conflict, or could not be merged (already queued, handle it first)
- **3** — `REFUSED`: the PR was refused at enqueue (config missing, stacked PR, dirty tree, duplicate enqueue)
- **1** — `INTERNAL_ERROR`: something went wrong in the queue itself

## Files

- `commands/queued-merge.md` — this command
- `~/.claude/scripts/merge-queue` — the merge queue script (installed by `/setup-local`)
- `<repo-container>/merge-queue.json` — local per-machine config (must exist for this command to work)
