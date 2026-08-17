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
elif [ "$(basename "$(dirname "$MAIN_WORKTREE")")" = "worktrees" ]; then
  # Fresh /setup-repo clone: main worktree already sits under a worktrees/ dir, so siblings go there.
  WORKTREE_PARENT=$(dirname "$MAIN_WORKTREE")
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
`gh stack link`, `gh stack unstack`, and graphql reads. `gh stack init`, `gh stack checkout` are
**fatal under per-branch layout** (branches are permanently checked out in sibling worktrees), but
under **single-driver layout** (one working copy driving the whole stack via `gh stack checkout`)
`gh stack sync` is the intended one-command path — use the "Detect layout" block to resolve
`STACK_LAYOUT` before choosing.

### Is-stacked (this branch)

Detect whether the current branch's parent is another worktree's branch (not the default branch).
Cache-first from `.claude/github-cache.json` `.stack.isStacked`; if absent/null, detect the parent
as the nearest ancestor among other worktree branches. Emits `STACK_IS_STACKED` (true/false),
`STACK_PARENT_BRANCH`, and `STACK_PARENT_PR`.

```bash
# Check cache first (most specific)
GITHUB_CACHE=$(cat .claude/github-cache.json 2>/dev/null || echo '{}')
CACHED_STACKED=$(echo "$GITHUB_CACHE" | jq -r '.stack.isStacked // "unset"' 2>/dev/null)

STACK_IS_STACKED=false
STACK_PARENT_BRANCH=""
STACK_PARENT_PR=""

if [ "$CACHED_STACKED" = "true" ]; then
  # Cache says stacked — use cached parent info
  STACK_IS_STACKED=true
  STACK_PARENT_BRANCH=$(echo "$GITHUB_CACHE" | jq -r '.stack.parentBranch // ""')
  STACK_PARENT_PR=$(echo "$GITHUB_CACHE" | jq -r '.stack.parentPr // ""')
elif [ "$CACHED_STACKED" != "false" ] && [ "$CACHED_STACKED" != "unset" ]; then
  # Cache explicitly says false — not stacked
  STACK_IS_STACKED=false
else
  # Cache says not stacked or missing — detect by looking at worktree branches
  # For each branch B from git worktree list (excluding main and current), test if B is an ancestor
  CURRENT_BRANCH=$(git branch --show-current)
  DEFAULT_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|^refs/remotes/origin/||')
  [ -z "$DEFAULT_BRANCH" ] && DEFAULT_BRANCH="main"

  # Get all worktree branches (handle paths with spaces by matching the key, not field $2)
  WORKTREE_BRANCHES=$(git worktree list --porcelain | awk '
    /^worktree / { wt=substr($0, 10); next }  # capture from "worktree " onwards
    /^branch / && wt != "" { 
      branch=substr($0, 8)  # capture from "branch " onwards
      sub(/^refs\/heads\//, "", branch)
      print branch
      wt=""
    }
  ')

  BEST_ANCESTOR=""
  BEST_DISTANCE=""

  while IFS= read -r branch; do
    # Skip main, current branch, and empty entries
    [ -z "$branch" ] && continue
    [ "$branch" = "$DEFAULT_BRANCH" ] && continue
    [ "$branch" = "$CURRENT_BRANCH" ] && continue

    # Test if this branch is an ancestor of HEAD
    if git merge-base --is-ancestor "$branch" HEAD 2>/dev/null; then
      # Count commits between ancestor and HEAD (tightest = fewest commits)
      if DISTANCE=$(git rev-list --count "$branch..HEAD" 2>/dev/null); then
        if [ -z "$BEST_DISTANCE" ] || [ "$DISTANCE" -lt "$BEST_DISTANCE" ]; then
          BEST_ANCESTOR="$branch"
          BEST_DISTANCE="$DISTANCE"
        fi
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

### Detect layout

Detect the worktree layout so stacked pushes route correctly. Self-contained — re-runs the worktree list unconditionally (cheap; ~5ms). Precondition: `STACK_IS_STACKED=true`, `STACK_PARENT_BRANCH` set. Emits `STACK_LAYOUT` (`single-driver` | `per-branch` | `unknown`). Value is re-derived each run (not cached).

```bash
# Detect worktree layout for stacked-push routing — STRUCTURAL, not commit-ancestry.
# Precondition: STACK_IS_STACKED=true, STACK_PARENT_BRANCH set (run Is-stacked first).
# Emits: STACK_LAYOUT="single-driver"|"per-branch"|"unknown"
#
# Layout turns on one structural fact: is any OTHER member of this stack checked out in a
# sibling worktree? If yes, `gh stack sync`/`checkout` would try to check out an
# already-checked-out branch -> fatal -> per-branch. If no member other than the current
# branch is checked out anywhere, one working copy drives the stack -> single-driver.
# Read from metadata (worktree branch list + each worktree's cached .stack.parentBranch),
# never from `git merge-base --is-ancestor`.

STACK_LAYOUT="unknown"
CURRENT_BRANCH=$(git branch --show-current)
DEFAULT_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|^refs/remotes/origin/||')
[ -z "$DEFAULT_BRANCH" ] && DEFAULT_BRANCH="main"

# branch<TAB>worktree-path for every worktree (detached HEADs emit no branch line -> skipped)
WORKTREE_LINES=$(git worktree list --porcelain | awk '
  /^worktree / { wt=substr($0,10); next }
  /^branch / && wt != "" { b=substr($0,8); sub(/^refs\/heads\//,"",b); print b "\t" wt; wt="" }')

PER_BRANCH=false
while IFS=$'\t' read -r b wt; do
  [ -z "$b" ] && continue
  [ "$b" = "$CURRENT_BRANCH" ] && continue      # exclude self by name
  [ "$b" = "$DEFAULT_BRANCH" ] && continue      # exclude default branch by name
  # This sibling worktree holds the current branch's parent?
  if [ -n "$STACK_PARENT_BRANCH" ] && [ "$b" = "$STACK_PARENT_BRANCH" ]; then
    PER_BRANCH=true; break
  fi
  # This sibling worktree holds a child of the current branch (per its own cache)?
  SIB_PARENT=$(jq -r '.stack.parentBranch // empty' "$wt/.claude/github-cache.json" 2>/dev/null)
  if [ "$SIB_PARENT" = "$CURRENT_BRANCH" ]; then
    PER_BRANCH=true; break
  fi
done <<< "$WORKTREE_LINES"

if [ "$PER_BRANCH" = "true" ]; then
  STACK_LAYOUT="per-branch"
elif [ -n "$STACK_PARENT_BRANCH" ]; then
  # Stacked, and no stack member other than the current branch is checked out in any
  # sibling worktree -> one working copy can drive the whole stack.
  STACK_LAYOUT="single-driver"
else
  # Stacked flag set but no parent branch known and no sibling members found: cannot
  # prove which layout applies -> fail closed.
  STACK_LAYOUT="unknown"
fi
```

**Edge cases:**
- Parent checked out in a sibling worktree → `per-branch` (`gh stack sync` would try to check out the parent from the current worktree → fatal)
- Child checked out in a sibling worktree → `per-branch` (`gh stack sync` would try to check out the child → fatal)
- No stack member other than the current branch is checked out anywhere → `single-driver` (one working copy can safely drive the whole stack)
- Detached-HEAD worktrees → awk outputs no `branch` line → correctly skipped
- Cannot determine (stacked but no parent or sibling members found) → `unknown` (caller stops and asks)

### Push a stacked branch (new local work)

Preconditions: `STACK_IS_STACKED=true`, `STACK_PARENT_BRANCH` (non-empty), `STACK_LAYOUT` resolved (run Detect layout first), `BRANCH` set to the current branch name.

**Safety guard:** If `STACK_LAYOUT="unknown"`, STOP and report: "cannot determine layout — resolve manually, do not guess an arm". Do not proceed.

1. Check whether the remote ref is present: `git ls-remote --exit-code origin "$BRANCH"`.
   - **If absent** (first push of a brand-new stacked branch): `git push -u origin "$BRANCH"` and you are done. The gotcha (no upstream / tip-behind) only applies to *updates* of an already-pushed stacked branch.
   - **If present** (updating an existing stacked branch): branch on `STACK_LAYOUT`.

   **single-driver:** Precondition: `command -v gh-stack >/dev/null 2>&1` (gh-stack is installed). Run `gh stack sync`. It fetches, cascade-rebases the whole stack onto the updated trunk, and pushes all branches atomically (`--force-with-lease --atomic`) — do NOT rebase locally first and then sync; let sync do both. On non-zero exit: stop and report; do not fall back (the stack may be partially synced, or sync aborted on a divergence it could not resolve non-interactively). Note that in a non-interactive terminal `gh stack sync` aborts without pushing if the local and remote stacks have diverged.

   If `gh-stack` is not installed: emit an install-hint WARNING and stop. Do not silently fall through to per-branch.

   **Recovery** (if `gh stack sync` was interrupted mid-cascade): Run `git rebase --abort` if a rebase is in progress. Verify each branch tip against origin. For each child, use the "### Restack a child (after its parent merged)" runbook.

   **per-branch:** `gh stack sync`/`checkout`/`init` are fatal here (branches are checked out in sibling worktrees), and `gh stack push` also checks out branches — so use the manual git primitive. Baseline (simple case — parent NOT force-rebased). Precondition: `STACK_PARENT_BRANCH` is non-empty; if empty, fail loudly: "cannot determine parent branch — resolve manually".

```bash
# Fail loudly if the parent is unknown — otherwise the rebase below expands to
# `origin/` and dies with an opaque "ambiguous argument" error instead of this hint.
[ -n "$STACK_PARENT_BRANCH" ] || { echo "ERROR: cannot determine parent branch — resolve manually." >&2; exit 1; }
git fetch origin
if git rebase --onto origin/"$STACK_PARENT_BRANCH" \
     "$(git merge-base HEAD "$STACK_PARENT_BRANCH")"; then
  # --force-if-includes: the fetch above just moved the remote-tracking ref, which would
  # otherwise defeat --force-with-lease's implicit lease.
  git push --force-with-lease --force-if-includes origin HEAD
else
  echo "ERROR: rebase onto origin/$STACK_PARENT_BRANCH conflicted — resolve, complete the rebase, then push." >&2
  echo "  git status; after resolving: git rebase --continue && git push --force-with-lease --force-if-includes origin HEAD" >&2
  exit 1
fi
```

   **Rebase ownership** (this per-branch arm OWNS the rebase for callers with new, un-rebased local work — e.g. `/shipit`). A caller that has ALREADY rebased locally (e.g. `/expert-rebase` Step 3) must NOT re-run this block's rebase — it should force-push only (`git push --force-with-lease --force-if-includes origin HEAD`).

   If the rebase fails because the parent was force-rebased, direct the user to the "### Restack a child (after its parent merged)" runbook later in this same file.

**Guardrails:**
- Never `git push -u` to *update* a stacked branch — after a gh-stack rebase there is no local upstream and the tip is behind, so it cannot fast-forward.
- Never `git reset --hard @{u}` when you have unpushed local commits — that is the *stale-local* case (see "Restack a child") and would drop new work.
- Under single-driver, `gh stack sync` cascades to all child branches automatically. Under per-branch, child branches are NOT updated — use the "Restack a child" runbook for each child after the parent moves.

### Find children (of a branch)

Given a branch name, find all branches whose parent is that branch. Scan sibling worktree caches
first: `"$WORKTREE_PARENT"/*/.claude/github-cache.json` for entries whose `.stack.parentBranch`
matches. If no caches match, fall back to `gh pr list --base <branch> --state open --json number,headRefName`.
Emits the list of child branches and PR numbers.

```bash
# Requires: WORKTREE_PARENT and PARENT_BRANCH as inputs
# Emits: CHILD_BRANCHES (space-separated list of "branch:pr:worktree" records)

PARENT_BRANCH="$1"
CHILD_BRANCHES=""

# Scan worktree caches for children whose parent matches
if [ -d "$WORKTREE_PARENT" ]; then
  while IFS= read -r cache_file; do
    [ ! -f "$cache_file" ] && continue
    CACHED_PARENT=$(jq -r '.stack.parentBranch // empty' "$cache_file" 2>/dev/null)
    if [ "$CACHED_PARENT" = "$PARENT_BRANCH" ]; then
      CHILD_WORKTREE=$(basename "$(dirname "$(dirname "$cache_file")")")
      CHILD_BRANCH=$(git worktree list --porcelain 2>/dev/null | awk -v wt="$WORKTREE_PARENT/$CHILD_WORKTREE" '
        $1 == "worktree" && $2 == wt { found=1 }
        found && $1 == "branch" { sub(/^refs\/heads\//, "", $2); print $2; exit }
      ')
      if [ -n "$CHILD_BRANCH" ]; then
        CHILD_PR=$(jq -r '.pr.number // ""' "$cache_file" 2>/dev/null)
        CHILD_BRANCHES="${CHILD_BRANCHES}${CHILD_BRANCH}:${CHILD_PR}:${WORKTREE_PARENT}/${CHILD_WORKTREE} "
      fi
    fi
  done < <(find "$WORKTREE_PARENT" -maxdepth 3 -name "github-cache.json" 2>/dev/null)
fi

# Fallback: scan open PRs against this branch
if [ -z "$CHILD_BRANCHES" ]; then
  OPEN_PRS=$(gh pr list --base "$PARENT_BRANCH" --state open --json number,headRefName -q '.[] | "\(.headRefName):\(.number)"' 2>/dev/null || true)
  CHILD_BRANCHES=$(echo "$OPEN_PRS" | xargs)
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

REPO_OWNER=$(echo "$REPO" | cut -d/ -f1)
REPO_NAME=$(echo "$REPO" | cut -d/ -f2)

STACK_NUMBER=$(gh api graphql -F owner="$REPO_OWNER" -F name="$REPO_NAME" -F number="$PR_NUM" -q '.data.repository.pullRequest.stack.number // empty' 2>/dev/null << 'GRAPHQL'
query ($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      stack {
        number
      }
    }
  }
}
GRAPHQL
)

if [ -n "$STACK_NUMBER" ] && [ "$STACK_NUMBER" != "null" ]; then
  echo "Stack number: $STACK_NUMBER"
else
  echo "WARNING: Could not resolve stack number for PR #$PR_NUM"
fi
```

### Restack a child (after its parent merged)

Canonical, worktree-safe algorithm for reconciling one stacked child after its parent PR merged.
This is the **single source of truth** for the restack primitive; `/cleanup`'s Step 2.6 emits it
verbatim (per child, bottom-up), substituting the four parameters below. It is **detect-then-branch**:
the remote child may already have been rebased for you, so it leads with the reset/prove primitive and
keeps rebase-and-force-push only for the "remote not yet rebased" branch and the non-empty-diff
fallback.

**Why this shape, not rebase-and-replay:** GitHub core retargets a child PR's *base pointer* when its
parent merges, but it never rebases the *commits*. The force-push that actually rewrites the remote
branch comes from a stacking tool (`gh stack sync`, Graphite, or the "Update branch" button) acting as
you — so the remote child is often *already correct* and the **local worktree** is what's stale. In
that case, replaying the pre-rebase local commits (identical messages, new SHAs) re-introduces the
exact duplicate-change conflict you are escaping. The correct primitive is **snapshot →
`reset --hard @{u}` → prove empty diff**. A correct rebase yields an identical final tree, so
`git diff backup HEAD == empty` is a *provable* no-loss check — better than trusting SHAs or counts.
`rebase --onto` is only the fallback, used when the branch has genuinely unique local commits.

Parameterized on `<CHILD_WT>` (child worktree path), `<CHILD_BRANCH>`, `<MERGED_TIP>` (the merged
parent's tip SHA, captured before deletion), and `<DEFAULT_BRANCH>`:

```bash
git -C <CHILD_WT> fetch origin

# Clean tree required — if dirty, stash or bail (never reset over uncommitted work).
[ -z "$(git -C <CHILD_WT> status --porcelain)" ] || { echo "uncommitted changes in <CHILD_WT>; stash first"; exit 1; }

# Verify @{u} resolves (tracking branch exists)
git -C <CHILD_WT> rev-parse '@{u}' >/dev/null 2>&1 || { echo "Branch <CHILD_BRANCH> has no upstream tracking"; exit 1; }

# Detect the force-push signature: histories diverged iff @{u} is NOT an ancestor of HEAD
# AND HEAD is NOT an ancestor of @{u} (both merge-base --is-ancestor checks false).
if ! git -C <CHILD_WT> merge-base --is-ancestor '@{u}' HEAD 2>/dev/null \
   && ! git -C <CHILD_WT> merge-base --is-ancestor HEAD '@{u}' 2>/dev/null; then
  # DIVERGED — a stacking tool already rebased the remote; the local worktree is stale.
  git -C <CHILD_WT> branch -f backup/<CHILD_BRANCH> HEAD   # snapshot — free, lossless
  git -C <CHILD_WT> reset --hard '@{u}'                    # take the already-rebased remote
  # PROOF of no data loss — MUST be empty (identical tree => safe).
  if [ -z "$(git -C <CHILD_WT> diff --stat backup/<CHILD_BRANCH> HEAD)" ]; then
    # Rebased branches can lose upstream tracking; re-link so @{u} keeps resolving.
    git -C <CHILD_WT> branch --set-upstream-to=origin/<CHILD_BRANCH>
    echo "✓ <CHILD_BRANCH>: reset to already-rebased remote, empty diff proves no work lost"
  else
    # NON-empty diff: the branch has genuinely unique local commits. Reset back and replay them.
    git -C <CHILD_WT> reset --hard backup/<CHILD_BRANCH>
    git -C <CHILD_WT> rebase --onto '@{u}' <MERGED_TIP>
    # Re-prove: after replaying only the unique commits, the diff vs backup must be empty.
    if [ -n "$(git -C <CHILD_WT> diff --stat backup/<CHILD_BRANCH> HEAD)" ]; then
      echo "WARNING: <CHILD_BRANCH> diff non-empty after rebase — inspect before pushing"
      exit 1
    fi
    git -C <CHILD_WT> branch --set-upstream-to=origin/<CHILD_BRANCH>   # re-link tracking (symmetric with the reset path above)
    # Gate — force-push ONLY after the project's checks pass. A runnable gate, not a comment:
    # a bare comment here would be silently skipped when the whole block is pasted and run.
    CHECK_CMD=$(jq -r '.commands.check // .commands.test // empty' <CHILD_WT>/.claude/repo-cache.json 2>/dev/null)
    [ -n "$CHECK_CMD" ] || { echo "no check command in <CHILD_WT>/.claude/repo-cache.json — run your checks manually, then re-run this push"; exit 1; }
    ( cd <CHILD_WT> && eval "$CHECK_CMD" ) || { echo "<CHILD_BRANCH>: checks failed — not force-pushing"; exit 1; }
    git -C <CHILD_WT> push --force-with-lease
  fi
else
  # NOT diverged — remote is still stacked on the old, now-merged base. Restack it yourself.
  git -C <CHILD_WT> branch -f backup/<CHILD_BRANCH> HEAD   # snapshot before rewriting history
  git -C <CHILD_WT> rebase --onto origin/<DEFAULT_BRANCH> <MERGED_TIP>
  git -C <CHILD_WT> branch --set-upstream-to=origin/<CHILD_BRANCH>   # re-link tracking (symmetric with the reset path above)
  # Gate — force-push ONLY after the project's checks pass. A runnable gate, not a comment:
  # a bare comment here would be silently skipped when the whole block is pasted and run.
  CHECK_CMD=$(jq -r '.commands.check // .commands.test // empty' <CHILD_WT>/.claude/repo-cache.json 2>/dev/null)
  [ -n "$CHECK_CMD" ] || { echo "no check command in <CHILD_WT>/.claude/repo-cache.json — run your checks manually, then re-run this push"; exit 1; }
  ( cd <CHILD_WT> && eval "$CHECK_CMD" ) || { echo "<CHILD_BRANCH>: checks failed — not force-pushing"; exit 1; }
  git -C <CHILD_WT> push --force-with-lease
fi

# Keep backup/<CHILD_BRANCH> until the child is confirmed correct, then delete it:
#   git -C <CHILD_WT> branch -D backup/<CHILD_BRANCH>
```

**Worktree-safety:** this manual primitive exists precisely because `gh stack init/sync/checkout`
check out each stack branch in one working copy and are **fatal under a worktree-per-branch layout**
(branches are permanently checked out in sibling worktrees). Note too that `gh stack sync` fixes only
the *remote* — it never touches your local worktree, so you reconcile local yourself with the above.

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
