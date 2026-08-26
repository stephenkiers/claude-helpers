---
name: stack-sync
description: Layout-routed stack sync — rebases stacked descendants onto their updated parent (or onto the default branch after a parent merges) inside each child's own worktree via git -C. Routes on STACK_LAYOUT: single-driver delegates to gh stack sync; per-branch walks descendants bottom-up and rebases each via the canonical Restack-a-child block. Never gh stack init/checkout. Auto-detects ongoing vs post-merge, runs the project check gate before any force-push, pauses once before the first push. Use when user says "/stack-sync", "sync the stack", "restack children", or after /shipit on a per-branch stacked PR.
allowed-tools: Bash(git -C:*), Bash(git worktree:*), Bash(git fetch:*), Bash(git rebase:*), Bash(git merge-base:*), Bash(git rev-parse:*), Bash(git rev-list:*), Bash(git branch:*), Bash(git reset:*), Bash(git status:*), Bash(git diff:*), Bash(git log:*), Bash(git push:*), Bash(git symbolic-ref:*), Bash(git ls-remote:*), Bash(gh pr view:*), Bash(gh pr list:*), Bash(gh stack:*), Bash(gh api:*), Bash(gh repo view:*), Bash(jq:*), Bash(find:*), Bash(cat:*), Bash(echo:*), Bash(eval:*), Bash(sed:*), Bash(awk:*), Bash(cut:*), Bash(basename:*), Bash(dirname:*), Bash(printf:*), Bash(mktemp:*), Read, AskUserQuestion
argument-hint: [--dry-run] [--yes|-y] [pivot-branch]
---

# Stack Sync

Layout-routed stack sync. The **pivot** (the branch whose children need syncing) is **not** re-pushed
here — `/shipit` or `/expert-rebase` already pushed it. This command syncs the pivot's **descendants**:
each child is rebased onto its updated parent (ongoing) or onto the default branch (post-merge), inside
the child's own worktree via `git -C`, then force-pushed only after the **project check gate** (the
repo's own check/test command, read from `.claude/repo-cache.json` and run inside the child's worktree
before any push) passes.

Routing follows the layout model recorded in `docs/adr/0012-stack-sync.md` (ADR-0012):
`single-driver` delegates to the `gh stack sync` cascade (one working copy drives the whole stack);
`per-branch` walks descendants bottom-up and rebases each via the canonical **Restack-a-child** block
from `~/.claude/prompts/worktree-reference.md`. Never `gh stack init` or `gh stack checkout` — both are
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
set -f   # $ARGUMENTS is word-split below; keep glob metacharacters in args from expanding
DRY_RUN=false
ASSUME_YES=false
PIVOT_BRANCH=""
ARG_POSITIONAL=""
for _a in $ARGUMENTS; do
  case "$_a" in
    --dry-run) DRY_RUN=true ;;
    --yes|-y)  ASSUME_YES=true ;;   # -y is the documented short alias for --yes
    --*)       echo "ERROR: unknown flag '$_a' (expected --dry-run or --yes)." >&2; exit 1 ;;
    -*)        echo "ERROR: unknown flag '$_a' (a pivot branch may not start with '-')." >&2; exit 1 ;;
    *)         if [ -n "$ARG_POSITIONAL" ]; then
                 echo "ERROR: unexpected second positional '$_a' (pivot is already '$ARG_POSITIONAL')." >&2
                 exit 1
               fi
               PIVOT_BRANCH="$_a"; ARG_POSITIONAL="$_a" ;;
  esac
done
set +f
[ -z "$PIVOT_BRANCH" ] && PIVOT_BRANCH=$(git branch --show-current)
# Empty pivot (no arg, detached HEAD) must fail here, not opaquely downstream.
[ -n "$PIVOT_BRANCH" ] || { echo "ERROR: no pivot branch given and HEAD is detached — pass a pivot: /stack-sync <pivot-branch>" >&2; exit 1; }
echo "PIVOT_BRANCH=$PIVOT_BRANCH  DRY_RUN=$DRY_RUN  ASSUME_YES=$ASSUME_YES"
```

Default-branch guards:

- If `PIVOT_BRANCH` equals `DEFAULT_BRANCH` → the pivot is the trunk itself, so sync every branch
  stacked directly on the default branch. Mode detection resolves **ongoing** here (the default branch
  always exists on the remote, so the `ls-remote` check takes the "still on remote" arm), which is
  correct: children rebase onto `origin/$DEFAULT_BRANCH`'s current tip. This is a legitimate request
  only when the user passed the default branch explicitly as the pivot.
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
PR_MERGE_OID=""
if PR_JSON=$(gh pr view "$PIVOT_BRANCH" --json state,mergedAt,mergeCommit 2>/dev/null) && [ -n "$PR_JSON" ]; then
  PR_STATE=$(printf '%s' "$PR_JSON" | jq -r '.state')
  PR_MERGED_AT=$(printf '%s' "$PR_JSON" | jq -r '.mergedAt // ""')
  PR_MERGE_OID=$(printf '%s' "$PR_JSON" | jq -r '.mergeCommit.oid // ""')

  if [ -n "$PR_MERGED_AT" ]; then
    SYNC_MODE="post-merge"     # mergedAt is the authoritative signal (state alone can mislead)
  elif [ "$PR_STATE" = "OPEN" ]; then
    SYNC_MODE="ongoing"
  else
    # CLOSED with null mergedAt — the PR was closed unmerged (abandoned), not merged. Neither
    # recipe applies: there is no updated parent to sync onto. Report and stop; do not guess.
    if git ls-remote --exit-code --heads origin "$PIVOT_BRANCH" >/dev/null 2>&1; then
      REMOTE_NOTE="origin/$PIVOT_BRANCH still exists"
    else
      REMOTE_NOTE="origin/$PIVOT_BRANCH is gone"
    fi
    echo "ERROR: pivot '$PIVOT_BRANCH' has a CLOSED, unmerged PR ($REMOTE_NOTE). Its children have"
    echo "no updated parent to sync onto. Reopen/merge the PR or retarget the children, then re-run."
    exit 1
  fi
else
  # gh could not report a PR. Disambiguate with the remote ref, distinguishing "lookup says absent"
  # (ls-remote exit 2) from "lookup failed" (any other non-zero — transient network/auth). Fail
  # closed on the errored case rather than falling toward either arm on a transient error.
  git ls-remote --exit-code --heads origin "$PIVOT_BRANCH" >/dev/null 2>&1
  case $? in
    0) SYNC_MODE="ongoing" ;;      # branch still on remote → parent advanced, not merged
    2) SYNC_MODE="post-merge" ;;   # branch gone from remote → merged (or deleted); sync onto default
    *) echo "ERROR: could not determine pivot state — 'gh pr view' failed and 'git ls-remote' errored"
       echo "(network/auth?). Resolve the lookup failure and re-run; refusing to guess a sync mode"
       echo "for '$PIVOT_BRANCH'."
       exit 1 ;;
  esac
fi

if [ "$SYNC_MODE" = "post-merge" ]; then
  # Capture the pivot's tip BEFORE any descendant work — post-merge children rebase away from this SHA.
  # (A default-branch pivot never reaches this arm: the default branch always exists on the remote,
  # so mode detection resolves ongoing for it.)
  # Prefer the PR's merge commit — it resolves even when the local branch was already deleted.
  # PR_MERGE_OID came from the routing fetch above; when that fetch never ran (ls-remote
  # fallback path), ask again here — a transient gh failure may have cleared.
  if [ -z "$PR_MERGE_OID" ] || [ "$PR_MERGE_OID" = "null" ]; then
    PR_MERGE_OID=$(gh pr view "$PIVOT_BRANCH" --json mergeCommit -q '.mergeCommit.oid // ""' 2>/dev/null || echo "")
  fi
  MERGED_TIP="$PR_MERGE_OID"
  if [ -z "$MERGED_TIP" ] || [ "$MERGED_TIP" = "null" ]; then
    # Local-ref fallback: the branch tip as recorded locally before deletion.
    MERGED_TIP=$(git rev-parse "$PIVOT_BRANCH" 2>/dev/null || echo "")
  fi
  [ -z "$MERGED_TIP" ] && { echo "ERROR: post-merge mode but could not resolve '$PIVOT_BRANCH' tip SHA."; exit 1; }
  echo "SYNC_MODE=post-merge  MERGED_TIP=$MERGED_TIP"
else
  echo "SYNC_MODE=ongoing  (pivot '$PIVOT_BRANCH' advanced; children rebase onto origin/$PIVOT_BRANCH)"
fi
```

## Step 3: Detect layout

Run the **Detect layout** block from `~/.claude/prompts/worktree-reference.md` (the same block
`/shipit`'s stacked push routing uses) with **`STACK_LAYOUT_SUBJECT="$PIVOT_BRANCH"`** so detection
keys off the pivot, not the cwd's branch. This matters for the `/cleanup` handoff: `/cleanup` cd's
to the **main** worktree (its Step 1) before invoking `/stack-sync <pivot>`, so the cwd's branch is
the default branch, not the pivot — keyed off the current branch, detection would deterministically
resolve `unknown` and STOP. When the pivot IS the current branch, run
**Is-stacked (this branch)** first so `STACK_PARENT_BRANCH` is set; for a non-current pivot the layout
block reads the pivot's parent from the pivot's own worktree cache.

If `STACK_LAYOUT="unknown"` → **STOP and ask** (fail closed). Do not guess an arm. Report:

> "Cannot determine stack layout for '$PIVOT_BRANCH'. Resolve the layout manually — is each stack
> member checked out in its own worktree, or does one working copy drive the whole stack? — then re-run."

`single-driver` → Step 4a. `per-branch` → Step 4b.

## Step 4a: single-driver arm

One working copy drives the whole stack, so the `gh stack sync` cascade is the intended one-command
path: it fetches, cascade-rebases every branch in the stack onto the updated trunk, and pushes them all
atomically (`--force-with-lease --atomic`). It owns both the rebase and the push — do NOT pre-rebase
locally; let the cascade do both.

**Ungated by design:** this arm runs with NO Step 5 confirmation prompt and NO repo-cache project
check gate. That is deliberate, not an oversight — the cascade is atomic and lease-protected, and
ADR-0011 (`docs/adr/0011-stacked-pr-layout-model.md`) blesses `gh stack sync` as the safe one-command
path under a proven single-driver layout. ADR-0012's two-gate guarantee (push confirmation + project
checks) scopes to the per-branch arm (Step 4b) only. Do not add a prompt or gate here.

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

Now run the cascade from the stack's driving working copy (the current worktree — the cascade is
cwd-sensitive, and under single-driver the cwd IS the one working copy that drives the stack):

```bash
gh stack sync
```

On **non-zero exit: STOP and report. Do NOT fall back to the per-branch arm.** The
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
# Uses the Find-children primitive (worktree-cache scan UNION gh pr list, deduped by branch) per branch.
SEEN=":"
DESCENDANTS=""          # newline-separated "branch:pr:parent:level:worktree" records, ancestor-first.
                        # The worktree path is LAST so `cut -d: -f5-` stays correct even for paths
                        # containing ':'; branch names and PR numbers are colon-free by git/GitHub rules.
CYCLE_DETECTED=false
# Same flag name as the canonical Find-children block, but sticky here: the canonical snippet runs
# once per caller, while this walk calls find_children_of repeatedly — so the flag is initialized
# once before the walk and OR-accumulated (set, never reset) by each errored call.
GH_CHILD_LOOKUP_FAILED=false

# find_children_of <branch> — mirrors the canonical Find-children block from
# ~/.claude/prompts/worktree-reference.md, re-encoded inline as a function for recursion (the
# canonical block is a snippet, not a shell function — keep the two in sync). Emits CHILD_BRANCHES
# as newline-separated "branch:pr:worktree" records; sets GH_CHILD_LOOKUP_FAILED on gh errors.
find_children_of() {
  local parent="$1"
  CHILD_BRANCHES=""
  # Detector 1 — sibling worktree caches.
  if [ -d "$WORKTREE_PARENT" ]; then
    while IFS= read -r cache_file; do
      [ ! -f "$cache_file" ] && continue
      local cached_parent
      cached_parent=$(jq -r '.stack.parentBranch // empty' "$cache_file" 2>/dev/null)
      if [ "$cached_parent" = "$parent" ]; then
        local child_wt_dir child_branch child_pr
        child_wt_dir=$(basename "$(dirname "$(dirname "$cache_file")")")
        child_branch=$(git worktree list --porcelain 2>/dev/null | awk -v wt="$WORKTREE_PARENT/$child_wt_dir" '
          /^worktree / { found=(substr($0, 10) == wt); next }
          found && /^branch / { b=substr($0, 8); sub(/^refs\/heads\//, "", b); print b; exit }
        ')
        if [ -n "$child_branch" ]; then
          child_pr=$(jq -r '.pr.number // ""' "$cache_file" 2>/dev/null)
          CHILD_BRANCHES="${CHILD_BRANCHES}${child_branch}:${child_pr}:${WORKTREE_PARENT}/${child_wt_dir}"$'\n'
          echo "  detected child $child_branch of $parent (worktree cache)"
        fi
      fi
    done < <(find "$WORKTREE_PARENT" -maxdepth 3 -name "github-cache.json" 2>/dev/null)
  fi
  # Detector 2 — open PRs based on this branch. ALWAYS runs (union, not fallback): a cache hit can
  # be stale, and gh can miss children the cache knows about; neither alone is authoritative.
  local open_prs pr_rec pr_branch
  if open_prs=$(gh pr list --base "$parent" --state open --json number,headRefName -q '.[] | "\(.headRefName):\(.number):"' 2>/dev/null); then
    while IFS= read -r pr_rec; do
      [ -z "$pr_rec" ] && continue
      pr_branch=$(printf '%s' "$pr_rec" | cut -d: -f1)
      # Dedupe by branch (field 1) — a child found via cache keeps its cache record (with worktree).
      if printf '%s' "$CHILD_BRANCHES" | awk -F: -v b="$pr_branch" '$1==b {f=1} END {exit !f}'; then
        continue
      fi
      CHILD_BRANCHES="${CHILD_BRANCHES}${pr_rec}"$'\n'
      echo "  detected child $pr_branch of $parent (gh pr list)"
    done <<< "$open_prs"
  else
    GH_CHILD_LOOKUP_FAILED=true   # sticky — see the flag's declaration above
    echo "  WARNING: 'gh pr list --base $parent' failed — child detection may be incomplete"
  fi
}

# resolve_worktree <branch> <worktree-from-cache> — fall back to scanning git worktree list
# when the cache record has an empty worktree field. Hard-errors when the branch is checked out
# in MORE THAN ONE worktree (ambiguous — the plan promised a duplicate guard).
resolve_worktree() {
  local branch="$1" wt_cache="$2"
  if [ -n "$wt_cache" ] && [ -d "$wt_cache" ]; then
    echo "$wt_cache"
    return 0
  fi
  git worktree list --porcelain | awk -v b="$branch" '
    /^worktree / { wt=substr($0,10); next }
    /^branch / && wt != "" {
      br=substr($0,8); sub(/^refs\/heads\//,"",br)
      if (br==b) { matches[++n]=wt; wt="" }
    }
    END {
      if (n > 1) {
        printf "ERROR: branch %s is checked out in %d worktrees (ambiguous):\n", b, n > "/dev/stderr"
        for (i=1; i<=n; i++) printf "  %s\n", matches[i] > "/dev/stderr"
        exit 3
      }
      if (n == 1) print matches[1]
    }
  '
}

# walk <parent> <level> — DFS pre-order so ancestors are emitted before their descendants.
walk() {
  local parent="$1" level="$2"
  case "$SEEN" in *":$parent:"*) CYCLE_DETECTED=true; return ;; esac
  SEEN="$SEEN$parent:"
  find_children_of "$parent"
  local rec
  while IFS= read -r rec; do
    [ -z "$rec" ] && continue
    local child_branch child_pr child_wt_cache resolved_wt
    child_branch=$(printf '%s' "$rec" | cut -d: -f1)
    child_pr=$(printf '%s' "$rec" | cut -d: -f2)
    child_wt_cache=$(printf '%s' "$rec" | cut -d: -f3-)
    # Append this child first (pre-order) so ancestors precede their descendants.
    if ! resolved_wt=$(resolve_worktree "$child_branch" "$child_wt_cache"); then
      echo "ERROR: duplicate worktree for '$child_branch' (paths listed above). Refusing to guess."
      exit 1
    fi
    DESCENDANTS="${DESCENDANTS}${child_branch}:${child_pr}:${parent}:$((level + 1)):${resolved_wt}"$'\n'
    # Recurse after so deeper descendants append after this child.
    walk "$child_branch" $((level + 1))
  done <<< "$CHILD_BRANCHES"
}

walk "$PIVOT_BRANCH" 0

if [ "$CYCLE_DETECTED" = "true" ]; then
  echo "ERROR: cycle detected in stack lineage (a branch is its own ancestor). Refusing to sync."
  exit 1
fi

# Topological verification: every parent must be an ancestor of its child. Only a VERIFIED violation
# is a hard error — pairs whose refs cannot be resolved are skipped with a note (a gh-only fallback
# child may have no local ref; after a partial re-run a parent may already be rewritten). Ancestry is
# accepted from the local ref OR the origin ref.
while IFS= read -r rec; do
  [ -z "$rec" ] && continue
  _cb=$(printf '%s' "$rec" | cut -d: -f1)
  _par=$(printf '%s' "$rec" | cut -d: -f3)
  [ "$_par" = "$PIVOT_BRANCH" ] && continue
  _cb_ref=""; _par_ref=""
  git rev-parse --verify -q "$_cb" >/dev/null && _cb_ref="$_cb"
  [ -z "$_cb_ref" ] && git rev-parse --verify -q "origin/$_cb" >/dev/null && _cb_ref="origin/$_cb"
  git rev-parse --verify -q "$_par" >/dev/null && _par_ref="$_par"
  [ -z "$_par_ref" ] && git rev-parse --verify -q "origin/$_par" >/dev/null && _par_ref="origin/$_par"
  if [ -z "$_cb_ref" ] || [ -z "$_par_ref" ]; then
    echo "  note: skipping ancestry check for $_par -> $_cb (ref not resolvable locally or on origin)"
    continue
  fi
  if ! git merge-base --is-ancestor "$_par_ref" "$_cb_ref" 2>/dev/null; then
    echo "ERROR: topological order broken — '$_par' is not an ancestor of '$_cb' (verified against"
    echo "'$_par_ref' / '$_cb_ref'). Refusing to sync."
    exit 1
  fi
done <<< "$DESCENDANTS"

if [ -z "$DESCENDANTS" ]; then
  if [ "$GH_CHILD_LOOKUP_FAILED" = "true" ]; then
    # gh errored during detection — the child set may be incomplete. Refuse to declare a clean no-op.
    echo "WARNING: no descendants found, but 'gh pr list' failed during detection — the child set"
    echo "may be incomplete. Re-run when gh is healthy; refusing to declare 'nothing to sync'."
    exit 1
  fi
  # Clean no-op: a leaf branch has no descendants. Keeps /shipit's per-branch call silent for leaves.
  echo "No stacked descendants of '$PIVOT_BRANCH'. Nothing to sync."
  exit 0
fi

echo "DESCENDANTS (ancestor-first):"
printf '%s' "$DESCENDANTS"
```

### 4b.2 Compute per-child substitutions

For each descendant, resolve the recipe and its `<NEW_BASE>` / `<OLD_BASE>` substitutions. Level-1
uses the mode result; level-2+ use `origin/<parent>` (the just-synced parent's new remote tip) as
`<NEW_BASE>` and the parent's **pre-sync tip** as `<OLD_BASE>` — captured here, in the planning loop,
before any child is force-pushed. Fetch first so every `origin/<parent>` ref is fresh.

```bash
[ "$DRY_RUN" = "true" ] || git fetch origin --prune

# Outcomes accumulator for the Step 6 report — one '<branch>:<outcome>' line per child. A file (not
# a shell variable) because each child's block runs as its own Bash invocation (Step 4b.3), and shell
# state does not reliably survive across invocations. Later invocations re-read this path from the
# emitted plan output.
OUTCOMES_FILE=$(mktemp "${TMPDIR:-/tmp}/stack-sync-outcomes.XXXXXX")
echo "OUTCOMES_FILE=$OUTCOMES_FILE"

echo "=== Per-child sync plan (ancestor-first) ==="
echo "(This plan IS the runbook — it is emitted in full even if the run is later aborted at the"
echo " Step 5 gate, so it can always be executed by hand.)"
while IFS= read -r rec; do
  [ -z "$rec" ] && continue
  CHILD_BRANCH=$(printf '%s' "$rec" | cut -d: -f1)
  CHILD_PR=$(printf '%s' "$rec" | cut -d: -f2)
  PARENT=$(printf '%s' "$rec" | cut -d: -f3)
  LEVEL=$(printf '%s' "$rec" | cut -d: -f4)
  CHILD_WT=$(printf '%s' "$rec" | cut -d: -f5-)

  if [ "$LEVEL" = "1" ]; then
    if [ "$SYNC_MODE" = "post-merge" ]; then
      RECIPE="Restack a child"
      NEW_BASE="origin/$DEFAULT_BRANCH"
      OLD_BASE="$MERGED_TIP"
    else
      RECIPE="Sync a child (ongoing)"
      NEW_BASE="origin/$PIVOT_BRANCH"
      # Correct here: the pivot was NOT rewritten by this run (/shipit or /expert-rebase already
      # pushed it), so merge-base still computes against history the child shares.
      OLD_BASE="<merge-base>"   # the ongoing recipe derives this via git merge-base HEAD origin/<parent>
    fi
  else
    # Level 2+: the parent advanced (was just force-pushed by the level below), never merged.
    # OLD_BASE is the parent's PRE-SYNC tip, captured NOW — before any child runs. After the parent's
    # force-push, `git merge-base HEAD origin/<parent>` would compute the fork against the parent's
    # rewritten history (the old shared commits are gone) and pick a wrong, too-early base.
    RECIPE="Sync a child (ongoing)"
    NEW_BASE="origin/$PARENT"
    OLD_BASE=$(git rev-parse "origin/$PARENT" 2>/dev/null || echo "")
    if [ -z "$OLD_BASE" ]; then
      if [ "$DRY_RUN" = "true" ]; then
        OLD_BASE="<unresolved origin/$PARENT>"
      else
        echo "ERROR: cannot resolve origin/$PARENT pre-sync tip — needed as OLD_BASE for level-$LEVEL"
        echo "child '$CHILD_BRANCH'. Refusing to plan with a wrong fork point."
        exit 1
      fi
    fi
  fi

  # Resolve worktree if the cache field was empty.
  if [ -z "$CHILD_WT" ]; then
    if ! CHILD_WT=$(resolve_worktree "$CHILD_BRANCH" ""); then
      echo "ERROR: duplicate worktree for '$CHILD_BRANCH' (paths listed above). Refusing to guess."
      exit 1
    fi
  fi

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
done <<< "$DESCENDANTS"

# Dry-run enforcement gate (mirrors Step 4a): the plan above is the entire output — no rebase,
# no push, no Step 5 prompt.
if [ "$DRY_RUN" = "true" ]; then
  echo "[dry-run] plan emitted above; skipping ALL execution."
  exit 0
fi
```

### 4b.3 Run the canonical block per child — one invocation per child

Each child's composed canonical block runs as its **own Bash invocation** — one tool call per child,
in ancestor-first plan order. Never paste multiple children's blocks together and run them wholesale:
a shared invocation makes one child's exit status indistinguishable from another's, and Step 6's
per-child report depends on per-child outcomes.

For each child, run the mode-appropriate canonical block from
`~/.claude/prompts/worktree-reference.md`, substituting the four parameters:

- **Restack a child** (post-merge, level-1 only): `<CHILD_WT>`, `<CHILD_BRANCH>`,
  `<NEW_BASE>=origin/$DEFAULT_BRANCH`, `<OLD_BASE>=$MERGED_TIP`.
- **Sync a child (ongoing — parent advanced, not merged)** (ongoing level-1): `<CHILD_WT>`,
  `<CHILD_BRANCH>`, `<NEW_BASE>=origin/$PIVOT_BRANCH`; the recipe computes `<OLD_BASE>` itself via
  `git merge-base HEAD origin/<parent>` — correct here because the pivot was not rewritten by this
  run (it was already pushed by `/shipit` or `/expert-rebase`), so merge-base still sees the history
  the child forked from.
- **Level-2+ (ongoing)**: `<CHILD_WT>`, `<CHILD_BRANCH>`, `<NEW_BASE>=origin/$PARENT`,
  `<OLD_BASE>=` the parent's **pre-sync tip captured in 4b.2** — NOT merge-base. By the time a
  level-2+ child runs, its parent has already been rewritten and force-pushed by this run, so
  merge-base would compute the fork against the parent's new history and pick a wrong base.

The block auto-executes rebase + project check gate; `push --force-with-lease` runs **only after the
gate passes**. Because descendants are processed ancestor-first and a level-2+ child runs **only when
its parent synced** (skip-parent rule below), its `origin/<parent>` ref already reflects the parent's
just-pushed tip. When a parent's outcome is not `synced`, that freshness guarantee does not hold —
`origin/<parent>` still points at the stale pre-sync tip, and "syncing" the child onto it would
silently rebase onto the old base.

**Skip-parent rule:** a child whose parent is itself a descendant in this run (level ≥ 2) runs only
when its parent's recorded outcome is `synced`. Otherwise mark the child — and recursively its whole
subtree — `skipped-parent-failed` WITHOUT running its block, and continue with independent siblings.

**Capture each child's outcome from its own invocation's exit status.** The canonical block bails
with `exit 1` at seven sites (dirty tree, missing upstream, non-empty post-replay diff, and the
missing-check-command / failed-checks pair on each of its two paths); under the per-child invocation
model each of those exits is just that child's outcome — it does not abort the run. After each
invocation, classify the outcome from the exit status and the block's echoed reason, then record it:

- exit 0 → `synced`
- 4b.2 flagged the worktree dirty → do NOT invoke the block at all; record `skipped-dirty`
- 4b.2 could not resolve a worktree → do NOT invoke; record `no-worktree`
- invocation output shows the check gate refusing to push → `checks-failed`
- invocation shows the block's dirty-tree bail ("uncommitted changes") although 4b.2 flagged the
  worktree clean → the tree went dirty between planning and invocation (TOCTOU) — record
  `skipped-dirty`, not `conflict`
- invocation shows a rebase conflict (or any other non-zero exit) → `conflict`
- parent's outcome was not `synced` → `skipped-parent-failed` (no invocation; see skip-parent rule)

```bash
# After a non-zero invocation, never leave the child stranded mid-rebase (a no-op when no rebase is
# in progress). The backup/<child> snapshot stays in place for manual rollback.
git -C "$CHILD_WT" rebase --abort 2>/dev/null || true

# Record this child's outcome for the Step 6 report — one '<branch>:<outcome>' line per child.
# Branch names are colon-free by git refname rules, so the two-field parse is unambiguous.
printf '%s:%s\n' "$CHILD_BRANCH" "$OUTCOME" >> "$OUTCOMES_FILE"
```

**Before running the FIRST child's block, apply the Push confirmation gate (Step 5).** Then proceed
through the rest. Per-child failure handling (see Step 5): a dirty child worktree is skipped; a
conflicted child is aborted out of its rebase, keeps `backup/<child>` for rollback, and has its
subtree marked `skipped-parent-failed`; independent siblings continue.

## Step 5: Push confirmation gate

Before the **first** force-push in the run, pause once and confirm with `AskUserQuestion`:

```
Stack-sync is about to force-push N descendant branch(es) onto their updated parent.
Proceed with the force-pushes?
```

Options:
- **Yes, sync and push** — run the per-child blocks (Step 4b.3). Each force-push is
  `--force-with-lease` and gated behind the project checks inside the canonical block.
- **No, abort** — stop without pushing anything. Descendants are not rebased. The per-child runbook
  (Step 4b.2's emitted plan) has already been printed in full and stands — point the user at it so
  they can execute it by hand; an abort never leaves them without the runbook.

Skip the question (proceed automatically) when `--yes` (alias `-y`) was passed — `ASSUME_YES=true`.
Skip all execution when `--dry-run` was passed — 4b.2's dry-run gate already exited before this step;
the emitted plan (and the single-driver `echo` from Step 4a) is the entire output.

Failure policy across the per-child loop (each child is its own invocation — see Step 4b.3):

- **Dirty child worktree** → skip that child (`skipped-dirty`), continue its siblings, report it at
  the end. Never rebase or reset over uncommitted work.
- **Rebase conflict** → run `git -C "$CHILD_WT" rebase --abort` so the child is not stranded
  mid-rebase, record `conflict`, leave `backup/<child>` in place for manual rollback, mark the
  child's whole subtree `skipped-parent-failed`, and continue with independent siblings. Do not
  abort the whole run on one conflict.
- **Project check gate failure** → the canonical block already refuses to force-push; record the
  outcome as `checks-failed`, mark the child's whole subtree `skipped-parent-failed`, and continue
  with siblings.
- **Any non-`synced` parent** → every descendant of that parent is recorded `skipped-parent-failed`
  without running its block: its `<NEW_BASE>=origin/<parent>` still points at the parent's stale
  pre-sync tip, so "syncing" it would silently rebase onto the old base and report a false `synced`.

## Step 6: Report

Emit one line per child: layout arm, mode, and outcome (`synced` | `skipped-dirty` | `conflict` |
`checks-failed` | `no-worktree` | `skipped-parent-failed`). Then emit the `backup/<child>` cleanup
commands — gated on **that child's own** outcome, never a run-level or last-child status: a sibling's
success must never direct deletion of the one backup a failed child still needs.

```bash
echo "=== Stack sync report ==="
echo "Layout: $STACK_LAYOUT  |  Mode: $SYNC_MODE  |  Pivot: $PIVOT_BRANCH"
echo ""
while IFS= read -r rec; do
  [ -z "$rec" ] && continue
  CHILD_BRANCH=$(printf '%s' "$rec" | cut -d: -f1)
  CHILD_WT=$(printf '%s' "$rec" | cut -d: -f5-)
  # This child's own outcome — recorded by its 4b.3 invocation, one '<branch>:<outcome>' line each.
  OUTCOME=$(awk -F: -v b="$CHILD_BRANCH" '$1==b {o=$2} END {print o}' "$OUTCOMES_FILE")
  [ -z "$OUTCOME" ] && OUTCOME="unknown"
  echo "  $CHILD_BRANCH → $OUTCOME"
  if [ -n "$CHILD_WT" ] && [ "$OUTCOME" = "synced" ]; then
    echo "    cleanup: git -C '$CHILD_WT' branch -D 'backup/$CHILD_BRANCH'"
  fi
done <<< "$DESCENDANTS"
```

Keep `backup/<child>` until the child is confirmed correct, then run the emitted `branch -D` cleanup
command. Conflicted children keep their backup for manual rollback — do not delete those automatically.
(The cleanup lines assume shell-safe branch and worktree names — no embedded single quotes; for exotic
names, emit the command with `printf '%q'` instead.)

## Notes

- The pivot is never re-pushed by this command. `/shipit` and `/expert-rebase` own the pivot; this
  command only reconciles the pivot's descendants.
- Under `single-driver`, the `gh stack sync` cascade handles the whole stack atomically — there is no
  per-child walk. Under `per-branch`, descendants are walked bottom-up and rebased one at a time inside
  each child's own worktree via `git -C`, so sibling worktrees are never disturbed.
- All detection (Project Detection, Stack Detection, Detect layout) and the restack
  primitives live in `~/.claude/prompts/worktree-reference.md`. This command references those blocks
  by name rather than re-encoding them, so fixes to the canonical blocks propagate here automatically.
  The one exception is **Find children**: 4b.1 re-encodes it inline as a recursive shell function (the
  canonical block is a snippet, not a function). The inline copy mirrors the canonical block — same
  union of both detectors, same dedupe-by-branch, same lookup-failure flag — keep the two in sync when
  editing either.
- `--force-with-lease` (never bare `--force`) is used for every push, and every push is gated behind
  the project's check command from `.claude/repo-cache.json` inside the canonical block.
