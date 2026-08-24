---
name: stack-sync
description: Layout-routed stack sync — rebases stacked descendants onto their updated parent (or onto the default branch after a parent merges) inside each child's own worktree via git -C. Routes on STACK_LAYOUT: single-driver delegates to gh stack sync; per-branch walks descendants bottom-up and rebases each via the canonical Restack-a-child block. Never gh stack init/checkout. Auto-detects ongoing vs post-merge, runs the project check gate before any force-push, pauses once before the first push. Use when user says "/stack-sync", "sync the stack", "restack children", or after /shipit on a per-branch stacked PR.
allowed-tools: Bash(git -C:*), Bash(git worktree:*), Bash(git fetch:*), Bash(git rebase:*), Bash(git merge-base:*), Bash(git rev-parse:*), Bash(git rev-list:*), Bash(git branch:*), Bash(git reset:*), Bash(git status:*), Bash(git diff:*), Bash(git log:*), Bash(git push:*), Bash(git symbolic-ref:*), Bash(git ls-remote:*), Bash(gh pr view:*), Bash(gh pr list:*), Bash(gh api:*), Bash(gh repo view:*), Bash(gh-stack:*), Bash(jq:*), Bash(find:*), Bash(cat:*), Bash(echo:*), Bash(eval:*), Bash(bash:*), Read, AskUserQuestion
argument-hint: [--dry-run] [--yes] [pivot-branch]
---

# Stack Sync

Layout-routed stack sync. The **pivot** (the branch whose children need syncing) is **not** re-pushed
here — `/shipit` or `/expert-rebase` already pushed it. This command syncs the pivot's **descendants**:
each child is rebased onto its updated parent (ongoing) or onto the default branch (post-merge), inside
the child's own worktree via `git -C`, then force-pushed only after the project check gate passes.

Routing follows the same layout model PR #64 shipped for push: `single-driver` delegates to the
`gh stack sync` cascade (one working copy drives the whole stack); `per-branch` walks descendants
bottom-up and rebases each via the canonical **Restack-a-child** block from
`~/.claude/prompts/worktree-reference.md`. Never `gh stack init` or `gh stack checkout` — both are
fatal under a worktree-per-branch layout.

## Step 1: Resolve pivot + environment

Run the **Project Detection** block from `~/.claude/prompts/worktree-reference.md` to resolve
`MAIN_WORKTREE`, `WORKTREE_PARENT`, `REPO`. Also resolve the default branch the same way the
**Detect layout** block does (it is the one shared value Project Detection does not emit directly):

```bash
DEFAULT_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|^refs/remotes/origin/||')
[ -z "$DEFAULT_BRANCH" ] && DEFAULT_BRANCH="main"
```

Parse `PIVOT_BRANCH` from args: strip the `--dry-run` and `--yes` flags; the remaining positional
argument is the pivot branch. If no positional arg was given, fall back to the current branch.

```bash
DRY_RUN=false
ASSUME_YES=false
PIVOT_BRANCH=""
ARG_POSITIONAL=""
for _a in $ARGUMENTS; do
  case "$_a" in
    --dry-run) DRY_RUN=true ;;
    --yes|-y)  ASSUME_YES=true ;;
    *)         PIVOT_BRANCH="$_a"; ARG_POSITIONAL="$_a" ;;
  esac
done
[ -z "$PIVOT_BRANCH" ] && PIVOT_BRANCH=$(git branch --show-current)
echo "PIVOT_BRANCH=$PIVOT_BRANCH  DRY_RUN=$DRY_RUN  ASSUME_YES=$ASSUME_YES"
```

Default-branch guards:

- If `PIVOT_BRANCH` equals `DEFAULT_BRANCH` → **post-merge mode**: the pivot is the trunk itself, so
  sync every branch stacked directly on the default branch. This is a legitimate request only when the
  user passed the default branch explicitly as the pivot.
- If you are sitting on the default branch **and no pivot arg was given** → error and stop. Running
  from the default branch with no pivot is ambiguous (there is no single parent to sync from).

```bash
if [ "$PIVOT_BRANCH" = "$DEFAULT_BRANCH" ] && [ -z "$ARG_POSITIONAL" ]; then
  echo "ERROR: no pivot branch given and the current branch is the default branch ('$DEFAULT_BRANCH')."
  echo "Pass a pivot branch: /stack-sync <pivot-branch>"
  exit 1
fi
```

## Step 2: Mode detection — ongoing vs post-merge

Determine whether the pivot *advanced* (ongoing — its children rebase onto the pivot's new tip) or
*merged* (post-merge — its children rebase onto the default branch, away from the merged tip). The
pivot itself is never touched; its PR state tells you which recipe each child needs.

```bash
PR_STATE=$(gh pr view "$PIVOT_BRANCH" --json state,mergedAt -q '.state' 2>/dev/null || echo "NONE")

if [ "$PR_STATE" = "MERGED" ]; then
  SYNC_MODE="post-merge"
elif [ "$PR_STATE" = "OPEN" ] || [ "$PR_STATE" = "CLOSED" ]; then
  SYNC_MODE="ongoing"
else
  # NONE / empty — disambiguate with the remote ref.
  if git ls-remote --exit-code --heads origin "$PIVOT_BRANCH" >/dev/null 2>&1; then
    SYNC_MODE="ongoing"      # branch still on remote → parent advanced, not merged
  else
    SYNC_MODE="post-merge"   # branch gone from remote → merged (or deleted); sync onto default
  fi
fi

if [ "$SYNC_MODE" = "post-merge" ]; then
  # Capture the pivot's tip BEFORE any descendant work — post-merge children rebase away from this SHA.
  if [ "$PIVOT_BRANCH" = "$DEFAULT_BRANCH" ]; then
    MERGED_TIP=$(git rev-parse "origin/$DEFAULT_BRANCH" 2>/dev/null || echo "")
  else
    MERGED_TIP=$(git rev-parse "$PIVOT_BRANCH" 2>/dev/null || echo "")
  fi
  [ -z "$MERGED_TIP" ] && { echo "ERROR: post-merge mode but could not resolve '$PIVOT_BRANCH' tip SHA."; exit 1; }
  echo "SYNC_MODE=post-merge  MERGED_TIP=$MERGED_TIP"
else
  echo "SYNC_MODE=ongoing  (pivot '$PIVOT_BRANCH' advanced; children rebase onto origin/$PIVOT_BRANCH)"
fi
```

## Step 3: Detect layout

Run the **Detect layout** block from `~/.claude/prompts/worktree-reference.md` (the same block #64's
push routing uses, unchanged) to resolve `STACK_LAYOUT` (`single-driver` | `per-branch` | `unknown`).
Run **Is-stacked (this branch)** first when the pivot is the current branch, so `STACK_PARENT_BRANCH`
is set; for a non-current pivot the layout block re-derives from worktree metadata directly.

If `STACK_LAYOUT="unknown"` → **STOP and ask** (fail closed). Do not guess an arm. Report:

> "Cannot determine stack layout for '$PIVOT_BRANCH'. Resolve the layout manually — is each stack
> member checked out in its own worktree, or does one working copy drive the whole stack? — then re-run."

`single-driver` → Step 4a. `per-branch` → Step 4b.

## Step 4a: single-driver arm

One working copy drives the whole stack, so the `gh stack sync` cascade is the intended one-command
path: it fetches, cascade-rebases every branch in the stack onto the updated trunk, and pushes them all
atomically (`--force-with-lease --atomic`). It owns both the rebase and the push — do NOT pre-rebase
locally; let the cascade do both.

Install guard and handle dry-run first:

```bash
# single-driver: the cascade owns rebase + push. Do not pre-rebase locally.
command -v gh-stack >/dev/null 2>&1 || {
  echo "ERROR: gh-stack is not installed. Under single-driver layout it is required — the manual"
  echo "per-branch primitive is not a substitute here. Install gh-stack and re-run, or switch to a"
  echo "per-branch layout."
  exit 1
}

if [ "$DRY_RUN" = "true" ]; then
  # Dry-run: emit the command, do not execute it.
  echo "[dry-run] would run: gh stack sync"
  exit 0
fi
```

Now run the `gh stack sync` cascade (the agent executes this as a prose directive — it is not a
pasted one-liner). On **non-zero exit: STOP and report. Do NOT fall back to the per-branch arm.** The
stack may be partially synced, or the cascade aborted on a divergence it could not resolve
non-interactively; falling back would double-rebase branches sync already touched.

**Recovery** (if the cascade was interrupted mid-cascade): run `git rebase --abort` if a rebase is
in progress, verify each branch tip against `origin`, then use the **Restack a child** runbook in
`~/.claude/prompts/worktree-reference.md` for each child that did not get pushed.

Report success: the whole stack was cascade-rebased and pushed atomically.

## Step 4b: per-branch arm

Under per-branch layout each stack member is permanently checked out in its own worktree, so the
manual `git -C` primitive is the only safe path. Collect all descendants bottom-up, then rebase each
onto its updated parent inside its own worktree.

### 4b.1 Collect descendants (bottom-up, topologically ordered)

Recursively find children starting from the pivot, using the **Find children (of a branch)** block
from `~/.claude/prompts/worktree-reference.md` as the direct-children primitive. Guard against cycles
with a `SEEN` map; emit descendants ancestor-first (topological order verified via
`git merge-base --is-ancestor`).

```bash
# Recursive, cycle-guarded descendant collection.
# Uses the Find-children primitive (cache scan + gh pr list fallback) per branch.
SEEN=":"
DESCENDANTS=""          # ordered "branch:pr:worktree:parent:level" records, ancestor-first
CYCLE_DETECTED=false

# find_children_of <branch> — re-runs the canonical Find-children block inline (it is a
# snippet, not a shell function). Emits CHILD_BRANCHES as "branch:pr:worktree" records.
find_children_of() {
  local parent="$1"
  CHILD_BRANCHES=""
  if [ -d "$WORKTREE_PARENT" ]; then
    while IFS= read -r cache_file; do
      [ ! -f "$cache_file" ] && continue
      local cached_parent
      cached_parent=$(jq -r '.stack.parentBranch // empty' "$cache_file" 2>/dev/null)
      if [ "$cached_parent" = "$parent" ]; then
        local child_wt_dir child_branch child_pr
        child_wt_dir=$(basename "$(dirname "$(dirname "$cache_file")")")
        child_branch=$(git worktree list --porcelain 2>/dev/null | awk -v wt="$WORKTREE_PARENT/$child_wt_dir" '
          $1 == "worktree" && $2 == wt { found=1 }
          found && $1 == "branch" { sub(/^refs\/heads\//, "", $2); print $2; exit }
        ')
        if [ -n "$child_branch" ]; then
          child_pr=$(jq -r '.pr.number // ""' "$cache_file" 2>/dev/null)
          CHILD_BRANCHES="${CHILD_BRANCHES}${child_branch}:${child_pr}:${WORKTREE_PARENT}/${child_wt_dir} "
        fi
      fi
    done < <(find "$WORKTREE_PARENT" -maxdepth 3 -name "github-cache.json" 2>/dev/null)
  fi
  if [ -z "$CHILD_BRANCHES" ]; then
    local open_prs
    open_prs=$(gh pr list --base "$parent" --state open --json number,headRefName -q '.[] | "\(.headRefName):\(.number):"' 2>/dev/null || true)
    CHILD_BRANCHES=$(echo "$open_prs" | xargs)
  fi
}

# resolve_worktree <branch> <worktree-from-cache> — fall back to scanning git worktree list
# when the cache record has an empty worktree field.
resolve_worktree() {
  local branch="$1" wt_cache="$2"
  if [ -n "$wt_cache" ] && [ -d "$wt_cache" ]; then
    echo "$wt_cache"
    return
  fi
  git worktree list --porcelain | awk -v b="$branch" '
    /^worktree / { wt=substr($0,10); next }
    /^branch / && wt != "" { br=substr($0,8); sub(/^refs\/heads\//,"",br); if(br==b){print wt;exit} }
  '
}

# walk <parent> <level> — DFS post-order so ancestors are emitted before their descendants.
walk() {
  local parent="$1" level="$2"
  case "$SEEN" in *":$parent:"*) CYCLE_DETECTED=true; return ;; esac
  SEEN="$SEEN$parent:"
  find_children_of "$parent"
  local rec
  for rec in $CHILD_BRANCHES; do
    local child_branch child_pr child_wt_cache resolved_wt
    child_branch=$(echo "$rec" | cut -d: -f1)
    child_pr=$(echo "$rec" | cut -d: -f2)
    child_wt_cache=$(echo "$rec" | cut -d: -f3-)
    # Recurse first (post-order) so deeper descendants append after this child.
    walk "$child_branch" $((level + 1))
    resolved_wt=$(resolve_worktree "$child_branch" "$child_wt_cache")
    DESCENDANTS="${DESCENDANTS}${child_branch}:${child_pr}:${resolved_wt}:${parent}:${level} "
  done
}

walk "$PIVOT_BRANCH" 0

if [ "$CYCLE_DETECTED" = "true" ]; then
  echo "ERROR: cycle detected in stack lineage (a branch is its own ancestor). Refusing to sync."
  exit 1
fi

# Topological verification: every parent must be an ancestor of its child. A violation is a hard error.
for rec in $DESCENDANTS; do
  _cb=$(echo "$rec" | cut -d: -f1)
  _par=$(echo "$rec" | cut -d: -f4)
  if [ "$_par" != "$PIVOT_BRANCH" ] && ! git merge-base --is-ancestor "$_par" "$_cb" 2>/dev/null; then
    echo "ERROR: topological order broken — '$_par' is not an ancestor of '$_cb'. Refusing to sync."
    exit 1
  fi
done

if [ -z "$DESCENDANTS" ]; then
  # Clean no-op: a leaf branch has no descendants. Keeps /shipit's per-branch call silent for leaves.
  echo "No stacked descendants of '$PIVOT_BRANCH'. Nothing to sync."
  exit 0
fi

echo "DESCENDANTS (ancestor-first):"
for rec in $DESCENDANTS; do
  echo "  $rec"
done
```

### 4b.2 Compute per-child substitutions

For each descendant, resolve the recipe and its `<NEW_BASE>` / `<OLD_BASE>` substitutions. Level-1
uses the mode result; level-2+ use `origin/<parent>` (the just-synced parent's new remote tip). Fetch
first so every `origin/<parent>` ref is fresh.

```bash
git fetch origin --prune

echo "=== Per-child sync plan (ancestor-first) ==="
for rec in $DESCENDANTS; do
  CHILD_BRANCH=$(echo "$rec" | cut -d: -f1)
  CHILD_PR=$(echo "$rec" | cut -d: -f2)
  CHILD_WT=$(echo "$rec" | cut -d: -f3)
  PARENT=$(echo "$rec" | cut -d: -f4)
  LEVEL=$(echo "$rec" | cut -d: -f5)

  if [ "$LEVEL" = "1" ]; then
    if [ "$SYNC_MODE" = "post-merge" ]; then
      RECIPE="Restack a child"
      NEW_BASE="origin/$DEFAULT_BRANCH"
      OLD_BASE="$MERGED_TIP"
    else
      RECIPE="Sync a child (ongoing)"
      NEW_BASE="origin/$PIVOT_BRANCH"
      OLD_BASE="<merge-base>"   # the ongoing recipe derives this via git merge-base HEAD origin/<parent>
    fi
  else
    # Level 2+: the parent advanced (was just force-pushed by the level below), never merged.
    RECIPE="Sync a child (ongoing)"
    NEW_BASE="origin/$PARENT"
    OLD_BASE="<merge-base>"
  fi

  # Resolve worktree if the cache field was empty.
  [ -z "$CHILD_WT" ] && CHILD_WT=$(resolve_worktree "$CHILD_BRANCH" "")

  # Dirty check — a dirty child is SKIPPED (Step 5), never aborted.
  if [ -n "$CHILD_WT" ] && [ -d "$CHILD_WT" ]; then
    if [ -n "$(git -C "$CHILD_WT" status --porcelain 2>/dev/null)" ]; then
      DIRTY_FLAG="dirty"
    else
      DIRTY_FLAG="clean"
    fi
  else
    DIRTY_FLAG="no-worktree"
  fi

  echo "$CHILD_BRANCH | pr=$CHILD_PR | wt=$CHILD_WT | parent=$PARENT | level=$LEVEL | recipe=$RECIPE | NEW_BASE=$NEW_BASE | OLD_BASE=$OLD_BASE | $DIRTY_FLAG"
done
```

### 4b.3 Run the canonical block per child

For each child in the emitted plan (ancestor-first order), run the mode-appropriate canonical block
from `~/.claude/prompts/worktree-reference.md`, substituting the four parameters:

- **Restack a child** (post-merge, level-1 only): `<CHILD_WT>`, `<CHILD_BRANCH>`,
  `<NEW_BASE>=origin/$DEFAULT_BRANCH`, `<OLD_BASE>=$MERGED_TIP`.
- **Sync a child (ongoing — parent advanced, not merged)** (ongoing level-1, and all level-2+):
  `<CHILD_WT>`, `<CHILD_BRANCH>`, `<NEW_BASE>=origin/<parent>`; the recipe computes `<OLD_BASE>`
  itself via `git merge-base HEAD origin/<parent>`.

The block auto-executes rebase + project check gate; `push --force-with-lease` runs **only after the
gate passes**. Because descendants are processed ancestor-first, each level-2+ child's `origin/<parent>`
ref already reflects the parent's just-synced new tip (the fetch above and the parent's force-push
made it so).

**Before running the FIRST child's block, apply the Push confirmation gate (Step 5).** Then proceed
through the rest. Per-child failure handling (see Step 5): a dirty child worktree is skipped; a rebase
conflict leaves `backup/<child>` in place for rollback and continues with independent siblings.

## Step 5: Push confirmation gate

Before the **first** force-push in the run, pause once and confirm with `AskUserQuestion`:

```
Stack-sync is about to force-push N descendant branch(es) onto their updated parent.
Proceed with the force-pushes?
```

Options:
- **Yes, sync and push** — run the per-child blocks (Step 4b.3). Each force-push is
  `--force-with-lease` and gated behind the project checks inside the canonical block.
- **No, abort** — stop without pushing anything. Descendants are not rebased.

Skip the question (proceed automatically) when `--yes` was passed. Skip all execution when `--dry-run`
was passed — in dry-run the emitted plan from Step 4b.2 (and the single-driver `echo` from Step 4a) is
the entire output; do not rebase or push anything.

Failure policy across the per-child loop:

- **Dirty child worktree** → skip that child, continue its siblings, report it at the end. Never
  rebase or reset over uncommitted work.
- **Rebase conflict** → report the conflicted child, leave `backup/<child>` in place for manual
  rollback, and continue with independent siblings. Do not abort the whole run on one conflict.
- **Project check gate failure** → the canonical block already refuses to force-push; record the
  outcome as `checks-failed` and continue with siblings.

## Step 6: Report

Emit one line per child: layout arm, mode, and outcome (`synced` | `skipped-dirty` | `conflict` |
`checks-failed` | `no-worktree`). Then emit the `backup/<child>` cleanup commands for the user to run
once they have confirmed each synced child is correct.

```bash
echo "=== Stack sync report ==="
echo "Layout: $STACK_LAYOUT  |  Mode: $SYNC_MODE  |  Pivot: $PIVOT_BRANCH"
echo ""
for rec in $DESCENDANTS; do
  CHILD_BRANCH=$(echo "$rec" | cut -d: -f1)
  CHILD_WT=$(echo "$rec" | cut -d: -f3)
  # OUTCOME is captured per-child during Step 4b.3 execution (synced | skipped-dirty | conflict | checks-failed | no-worktree).
  echo "  $CHILD_BRANCH → $OUTCOME"
  if [ -n "$CHILD_WT" ] && [ "$OUTCOME" = "synced" ]; then
    echo "    cleanup: git -C '$CHILD_WT' branch -D backup/$CHILD_BRANCH"
  fi
done
```

Keep `backup/<child>` until the child is confirmed correct, then run the emitted `branch -D` cleanup
command. Conflicted children keep their backup for manual rollback — do not delete those automatically.

## Notes

- The pivot is never re-pushed by this command. `/shipit` and `/expert-rebase` own the pivot; this
  command only reconciles the pivot's descendants.
- Under `single-driver`, the `gh stack sync` cascade handles the whole stack atomically — there is no
  per-child walk. Under `per-branch`, descendants are walked bottom-up and rebased one at a time inside
  each child's own worktree via `git -C`, so sibling worktrees are never disturbed.
- All detection (Project Detection, Stack Detection, Detect layout, Find children) and the restack
  primitives live in `~/.claude/prompts/worktree-reference.md`. This command references those blocks by
  name rather than re-encoding them, so fixes to the canonical blocks propagate here automatically.
- `--force-with-lease` (never bare `--force`) is used for every push, and every push is gated behind
  the project's check command from `.claude/repo-cache.json` inside the canonical block.
