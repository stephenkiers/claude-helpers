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

## Stack Detection

Detect and manage stacked PRs (branches whose parent is another worktree's branch, not the default
branch). Used by `/shipit` to set the correct PR base, and by `/cleanup` to detect children and emit
a restack runbook. **IMPORTANT:** `/shipit` establishes the parent→child chain via `gh pr create
--base <parent>` alone — it does **not** auto-run `gh stack link` (see hazard below). The worktree-safe
verbs referenced here are `gh stack unstack` and graphql reads. `gh stack init`, `gh stack checkout`,
and `gh stack push` are **fatal under per-branch layout** (branches are permanently checked out in
sibling worktrees), but under **single-driver layout** (one working copy driving the whole stack via
`gh stack checkout`) `gh stack sync` is the intended one-command path — use the "Detect layout" block
to resolve `STACK_LAYOUT` before choosing.

**`gh stack link` hazard (read before ever running it):** `gh stack link` is worktree-safe (no
checkout) but **NOT metadata-only** — it repoints the FIRST (bottom) PR's base to the trunk (master),
because it treats its first arg as the stack bottom. Linking a parent+child *subset* when the parent
is itself stacked on a grandparent DETACHES the parent from the grandparent and locks every base under
a new server stack entity (`gh pr edit --base` then fails with "Cannot change the base branch because
the pull request is part of a stack"). Observed 2026-08-25: `gh stack link 67 77` repointed #67 from
`pps-223-…` → `master`, requiring `gh stack unstack <n>` + `gh pr edit 67 --base <pps-223-…>` to
repair. The base-branch pointer set by `gh pr create --base` is the substantive link; the server
"stack" entity is optional cosmetic grouping. If you do want it, link the FULL bottom→top chain
(all members incl. the already-trunk-based real bottom), never just the new pair — see "GitHub stack
entity (optional)" in `~/.claude/prompts/shipit-reference.md`.

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

Detect the worktree layout so stacked pushes route correctly. Self-contained — re-runs the worktree list unconditionally (cheap; ~5ms). Precondition: `STACK_IS_STACKED=true`; `STACK_PARENT_BRANCH` set when the subject is the current branch (run Is-stacked first). Optional input: `STACK_LAYOUT_SUBJECT` — the branch whose layout to detect; defaults to the current branch. Callers acting on a **non-current pivot** (e.g. `/stack-sync <pivot>`, including `/cleanup`'s handoff — `/cleanup` cd's to the main worktree first, so the cwd's branch there is the default branch, not the pivot) MUST set it to the pivot — keyed off the cwd's branch, detection would deterministically misresolve to `unknown`. For a non-current subject the parent is read from the subject's own worktree cache instead of `STACK_PARENT_BRANCH`. Emits `STACK_LAYOUT` (`single-driver` | `per-branch` | `unknown`). Value is re-derived each run (not cached).

```bash
# Detect worktree layout for stacked-push routing — STRUCTURAL, not commit-ancestry.
# Precondition: STACK_IS_STACKED=true; STACK_PARENT_BRANCH set when the subject is the current
#   branch (run Is-stacked first). For a non-current subject the parent comes from the subject's
#   own worktree cache.
# Input (optional): STACK_LAYOUT_SUBJECT — branch whose layout to detect; defaults to current branch.
# Emits: STACK_LAYOUT="single-driver"|"per-branch"|"unknown"
#
# Layout turns on one structural fact: is any OTHER member of this stack checked out in a
# sibling worktree? If yes, `gh stack sync`/`checkout` would try to check out an
# already-checked-out branch -> fatal -> per-branch. If no member other than the subject
# branch is checked out anywhere, one working copy drives the stack -> single-driver.
# Read from metadata (worktree branch list + each worktree's cached .stack.parentBranch),
# never from `git merge-base --is-ancestor`.

STACK_LAYOUT="unknown"
CURRENT_BRANCH=$(git branch --show-current)
STACK_LAYOUT_SUBJECT="${STACK_LAYOUT_SUBJECT:-$CURRENT_BRANCH}"
DEFAULT_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|^refs/remotes/origin/||')
[ -z "$DEFAULT_BRANCH" ] && DEFAULT_BRANCH="main"

# branch<TAB>worktree-path for every worktree (detached HEADs emit no branch line -> skipped)
WORKTREE_LINES=$(git worktree list --porcelain | awk '
  /^worktree / { wt=substr($0,10); next }
  /^branch / && wt != "" { b=substr($0,8); sub(/^refs\/heads\//,"",b); print b "\t" wt; wt="" }')

# The subject's parent: from Is-stacked when the subject IS the current branch; otherwise from the
# subject's own worktree cache (a non-current pivot lives in a sibling worktree).
SUBJECT_PARENT=""
if [ "$STACK_LAYOUT_SUBJECT" = "$CURRENT_BRANCH" ]; then
  SUBJECT_PARENT="$STACK_PARENT_BRANCH"
else
  while IFS=$'\t' read -r b wt; do
    [ "$b" = "$STACK_LAYOUT_SUBJECT" ] || continue
    SUBJECT_PARENT=$(jq -r '.stack.parentBranch // empty' "$wt/.claude/github-cache.json" 2>/dev/null)
    break
  done <<< "$WORKTREE_LINES"
fi

PER_BRANCH=false
while IFS=$'\t' read -r b wt; do
  [ -z "$b" ] && continue
  [ "$b" = "$STACK_LAYOUT_SUBJECT" ] && continue  # exclude the subject itself by name
  [ "$b" = "$DEFAULT_BRANCH" ] && continue        # exclude default branch by name
  # This sibling worktree holds the subject's parent?
  if [ -n "$SUBJECT_PARENT" ] && [ "$b" = "$SUBJECT_PARENT" ]; then
    PER_BRANCH=true; break
  fi
  # This sibling worktree holds a child of the subject (per its own cache)?
  SIB_PARENT=$(jq -r '.stack.parentBranch // empty' "$wt/.claude/github-cache.json" 2>/dev/null)
  if [ "$SIB_PARENT" = "$STACK_LAYOUT_SUBJECT" ]; then
    PER_BRANCH=true; break
  fi
done <<< "$WORKTREE_LINES"

if [ "$PER_BRANCH" = "true" ]; then
  STACK_LAYOUT="per-branch"
elif [ -n "$SUBJECT_PARENT" ]; then
  # Stacked, and no stack member other than the subject is checked out in any
  # sibling worktree -> one working copy can drive the whole stack.
  STACK_LAYOUT="single-driver"
else
  # Stacked flag set but no parent branch known and no sibling members found: cannot
  # prove which layout applies -> fail closed.
  STACK_LAYOUT="unknown"
fi
```

**Edge cases:**
- Parent checked out in a sibling worktree → `per-branch` (`gh stack sync` would try to check out the parent from the driving worktree → fatal)
- Child checked out in a sibling worktree → `per-branch` (`gh stack sync` would try to check out the child → fatal)
- No stack member other than the subject branch is checked out anywhere → `single-driver` (one working copy can safely drive the whole stack)
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

Given a branch name, find all branches whose parent is that branch. Two detectors ALWAYS run and
their results are **unioned** (deduped by branch — a cache-found child keeps its cache record, which
carries the worktree path): the sibling worktree caches
(`"$WORKTREE_PARENT"/*/.claude/github-cache.json` entries whose `.stack.parentBranch` matches) and
`gh pr list --base <branch> --state open --json number,headRefName`. Neither alone is authoritative —
a cache can be stale or missing for worktrees created out-of-band, and `gh pr list` misses children
whose PRs are closed or whose base was retargeted. Emits the list of child branches and PR numbers,
plus a failure flag when the gh detector errors so callers can warn that detection may be incomplete.

```bash
# Requires: WORKTREE_PARENT and PARENT_BRANCH as inputs
# Emits: CHILD_BRANCHES (newline-separated "branch:pr:worktree" records — worktree LAST so
#        `cut -d: -f3-` stays correct for paths containing ':'; branch names and PR numbers are
#        colon-free by git/GitHub rules), GH_CHILD_LOOKUP_FAILED (true when gh pr list errored)
# Note: /stack-sync's 4b.1 re-encodes this block inline as a recursive function — keep the two in sync.

PARENT_BRANCH="$1"
CHILD_BRANCHES=""
GH_CHILD_LOOKUP_FAILED=false

# Detector 1 — scan worktree caches for children whose parent matches
if [ -d "$WORKTREE_PARENT" ]; then
  while IFS= read -r cache_file; do
    [ ! -f "$cache_file" ] && continue
    CACHED_PARENT=$(jq -r '.stack.parentBranch // empty' "$cache_file" 2>/dev/null)
    if [ "$CACHED_PARENT" = "$PARENT_BRANCH" ]; then
      CHILD_WORKTREE=$(basename "$(dirname "$(dirname "$cache_file")")")
      CHILD_BRANCH=$(git worktree list --porcelain 2>/dev/null | awk -v wt="$WORKTREE_PARENT/$CHILD_WORKTREE" '
        /^worktree / { found=(substr($0, 10) == wt); next }
        found && /^branch / { b=substr($0, 8); sub(/^refs\/heads\//, "", b); print b; exit }
      ')
      if [ -n "$CHILD_BRANCH" ]; then
        CHILD_PR=$(jq -r '.pr.number // ""' "$cache_file" 2>/dev/null)
        CHILD_BRANCHES="${CHILD_BRANCHES}${CHILD_BRANCH}:${CHILD_PR}:${WORKTREE_PARENT}/${CHILD_WORKTREE}"$'\n'
        echo "  detected child $CHILD_BRANCH of $PARENT_BRANCH (worktree cache)"
      fi
    fi
  done < <(find "$WORKTREE_PARENT" -maxdepth 3 -name "github-cache.json" 2>/dev/null)
fi

# Detector 2 — open PRs based on this branch. Always runs (union, not fallback); dedupe by branch.
if OPEN_PRS=$(gh pr list --base "$PARENT_BRANCH" --state open --json number,headRefName -q '.[] | "\(.headRefName):\(.number):"' 2>/dev/null); then
  while IFS= read -r pr_rec; do
    [ -z "$pr_rec" ] && continue
    PR_BRANCH=$(printf '%s' "$pr_rec" | cut -d: -f1)
    if printf '%s' "$CHILD_BRANCHES" | awk -F: -v b="$PR_BRANCH" '$1==b {f=1} END {exit !f}'; then
      continue   # already detected via cache
    fi
    CHILD_BRANCHES="${CHILD_BRANCHES}${pr_rec}"$'\n'
    echo "  detected child $PR_BRANCH of $PARENT_BRANCH (gh pr list)"
  done <<< "$OPEN_PRS"
else
  GH_CHILD_LOOKUP_FAILED=true
  echo "  WARNING: 'gh pr list --base $PARENT_BRANCH' failed — child detection may be incomplete"
fi

echo "Child branches of '$PARENT_BRANCH':"
printf '%s' "$CHILD_BRANCHES"
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

Parameterized on `<CHILD_WT>` (child worktree path), `<CHILD_BRANCH>`, `<NEW_BASE>` (the base to
rebase onto), and `<OLD_BASE>` (the fork point / old base to exclude). The block is mode-agnostic:
the caller supplies either **post-merge values** (`<NEW_BASE>=origin/<default-branch>`,
`<OLD_BASE>=<merged parent tip SHA>`, captured before deletion) or **ongoing values**
(`<NEW_BASE>=origin/<parent>`, `<OLD_BASE>=$(git merge-base HEAD origin/<parent>)`). The bash block
below is the post-merge example (`NEW_BASE=origin/<default>`, `OLD_BASE=<merged tip>`); the ongoing
recipe later in this section composes this same block with its own substitutions.

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
    git -C <CHILD_WT> rebase --onto '@{u}' <OLD_BASE>
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
  git -C <CHILD_WT> rebase --onto <NEW_BASE> <OLD_BASE>
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

### Sync a child (ongoing — parent advanced, not merged)

Recipe for the ongoing case: the parent's PR is still **open** but the parent advanced (e.g. via
`/shipit`), so the child needs to rebase onto the parent's new tip. Rather than duplicating the
restack logic, this computes the two mode-specific parameters and then composes the generalized
Restack-a-child block above.

```bash
git -C "$CHILD_WT" fetch origin
PARENT_OLD_TIP=$(git -C "$CHILD_WT" merge-base HEAD "origin/$PARENT_BRANCH")
NEW_BASE="origin/$PARENT_BRANCH"
# then run the generalized Restack-a-child block with
#   <CHILD_WT>=$CHILD_WT  <CHILD_BRANCH>=$CHILD_BRANCH
#   <NEW_BASE>=$NEW_BASE  <OLD_BASE>=$PARENT_OLD_TIP
# Multi-child runs: substitute the parent's captured pre-sync tip for PARENT_OLD_TIP instead —
# see the prose below.
```

`merge-base(HEAD, origin/<parent>)` is the correct `<OLD_BASE>` only while `origin/<parent>` still
contains the history the child forked from — i.e. the standalone, single-child ongoing case this
recipe serves, where the parent advanced but nobody has rewritten the parent's history out from under
the child (the fork point is then a real shared commit, and merge-base finds it). It is **not**
correct in a multi-child sync run (`/stack-sync`): a level-2+ child's parent was just rewritten and
force-pushed by the same run, so the old shared commits are gone from `origin/<parent>` and merge-base
computes the fork against the parent's NEW history — a wrong, too-early base that replays
already-integrated parent commits. There the caller must capture the parent's pre-sync tip BEFORE the
parent's force-push and pass it as `<OLD_BASE>` (see `/stack-sync` Step 4b.2). This recipe is for the
ongoing case (parent's PR still OPEN, parent advanced via `/shipit`); it composes the generalized
Restack block rather than duplicating it.

## Docker Compose Project Isolation

If a repo has a `docker-compose.yml` (or `compose.yml`) at its worktree root, its `worktrees/.envrc`
should export a dynamically-computed `COMPOSE_PROJECT_NAME`. Without this, Compose derives its
project name from the current directory's **basename**, and every repo's primary worktree is
conventionally named `main` — so two unrelated repos' `main` worktrees collide under the same
Compose project. This is not theoretical: `docker compose up -d postgres --remove-orphans` run
from one repo's `main` worktree once deleted a healthy, running container belonging to a
completely different repo's `main` worktree, because Compose believed they were the same project.

**Fix** — append to `worktrees/.envrc` (after any existing `dotenv` block). If your repo's `.envrc` already resolves its own path via `BASH_SOURCE` for another purpose, reuse that existing `ENVRC_DIR` variable (or equivalent) instead of redeclaring it.

```bash
# Unique docker-compose project name per repo+worktree — prevents `docker compose
# up/down --remove-orphans` in one repo's worktree from treating another repo's
# identically-named worktree (e.g. every repo's "main") as part of the same project.
# direnv evaluates this file with $PWD set to its own directory (the worktrees/
# parent), so that gives the repo name; $OLDPWD is the worktree dir that triggered it.
# Lowercased because Compose project names must match [a-z0-9][a-z0-9_-]* — repo
# directory names here are capitalized (AcmeApp, WidgetCo, ...).
# Note: repo/worktree names containing characters outside [a-z0-9_-] (dots, spaces, parens, etc.)
# require an additional strip/replace step beyond the case-fold shown here.
ENVRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_NAME=$(basename "$(dirname "$ENVRC_DIR")" | tr '[:upper:]' '[:lower:]')
WT_NAME=$(basename "${OLDPWD:?OLDPWD not set — open a shell in the worktree directory first, then cd here}" | tr '[:upper:]' '[:lower:]')
export COMPOSE_PROJECT_NAME="${REPO_NAME}-${WT_NAME}"
```

This yields e.g. `acme-app-main` and `widgetco-142-tier-2-invoice-reconciliation` — unique per
repo and per worktree, computed with no hardcoding, so new worktrees and new repos get correct
names automatically. If the repo has no `worktrees/.envrc` yet (no shared secrets to `dotenv`),
create one containing only this block.

**Why `$OLDPWD`, not `$PWD`:** direnv genuinely `cd`s to the `.envrc`'s own directory before
evaluating it (verified — a subprocess `pwd -P` run from inside the script confirms this, not just
the `$PWD` shell variable), so `$PWD` during evaluation is always the `worktrees/` parent, never the
actual worktree you're in. `$OLDPWD` is a side effect of that `cd` and reliably holds the real
invocation directory.

**If migrating an existing repo with real dev data in a named volume:** the project name change
means Compose creates a *new*, empty volume under the new project-scoped name — the old volume
(e.g. `main_pgdata`) is orphaned, not deleted. Migrate before relying on the new name.
For example, if the old project was named `main` (pre-fix, un-namespaced) and the new one is
`acme-app-main`, then `<old-project>` = `main` and `<new-project>` = `acme-app-main`;
`<volume>` is the volume name declared under `volumes:` in your `docker-compose.yml` (e.g. `pgdata`),
so `<old-project>_<volume>` reads as e.g. `main_pgdata` and `<new-project>_<volume>` as `acme-app-main_pgdata`.

This recipe assumes a single-service (database-only) Compose stack. If your stack includes other
services (app, cache, etc.), `docker compose down` with no service argument will stop all of them;
multi-service stacks need additional steps beyond this recipe to avoid silently leaving orphaned
containers under the old project name.

Apply this recipe:

```bash
# Capture baseline count from old project's live database — do this BEFORE stopping it
OLD_COUNT=$(docker compose -p <old-project> exec <service> psql -c 'SELECT count(*) FROM <table>;' 2>/dev/null | grep -oE '[0-9]+' | tail -1) \
  || { echo "failed to read count from live old database"; exit 1; }
echo "Baseline: $OLD_COUNT rows"

docker volume create <new-project>_<volume>

# Stop the old database — gate on success (a bare comment would be silently skipped when pasted)
docker compose -p <old-project> down \
  || { echo "docker compose down failed — aborting to avoid data loss"; exit 1; }
# Verify no lingering containers after down
[ -z "$(docker compose -p <old-project> ps -q)" ] \
  || { echo "containers still running under <old-project> after down — aborting copy"; exit 1; }

# Verify destination volume is empty before copying into it
DEST_FILE_COUNT=$(docker run --rm -v <new-project>_<volume>:/to alpine sh -c 'find /to -type f 2>/dev/null | wc -l') \
  || { echo "failed to inspect destination volume"; exit 1; }
[ "$DEST_FILE_COUNT" -eq 0 ] \
  || { echo "destination volume not empty ($DEST_FILE_COUNT files found) — aborting copy"; exit 1; }

# Copy data
docker run --rm -v <old-project>_<volume>:/from -v <new-project>_<volume>:/to \
  alpine sh -c "cp -a /from/. /to/" \
  || { echo "data copy failed"; exit 1; }

# Bring up database under new project name
docker compose -p <new-project> up -d <service> \
  || { echo "docker compose up failed"; exit 1; }

# Verify data integrity: compare row count to baseline
NEW_COUNT=$(docker compose -p <new-project> exec <service> psql -c 'SELECT count(*) FROM <table>;' 2>/dev/null | grep -oE '[0-9]+' | tail -1) \
  || { echo "failed to verify count in new database"; exit 1; }
echo "Migrated: $NEW_COUNT rows (baseline: $OLD_COUNT)"

# Clean up old volume ONLY after verification passes
test "$NEW_COUNT" = "$OLD_COUNT" \
  || { echo "row count mismatch: expected $OLD_COUNT, got $NEW_COUNT — investigate before cleaning up"; exit 1; }
docker volume rm <old-project>_<volume> || { echo "warning: failed to remove old volume"; }
echo "✓ Migration complete"
```

**CAUTION:** Always pin Compose commands to the project name explicitly (e.g. `docker compose -p
<old-project> down`). If you run this recipe after `.envrc` is already sourced, an unpinned
`docker compose down` will silently target the new, empty project instead, risking data corruption.

Bind-mounted storage (e.g. a host path, not a named Docker volume) is unaffected by project-name
changes and needs no migration.

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
