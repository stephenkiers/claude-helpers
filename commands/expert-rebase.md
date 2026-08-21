---
description: Rebase the current branch on origin/main. If the rebase is clean, reports success and offers to force-push. If there are conflicts, engages expert reviewers to analyze each conflicting hunk and recommend a resolution strategy before applying.
allowed-tools: Bash(git fetch:*), Bash(git rebase:*), Bash(git status:*), Bash(git diff:*), Bash(git log:*), Bash(git branch:*), Bash(git push:*), Bash(git add:*), Bash(git worktree:*), Bash(git ls-remote:*), Bash(cat:*), Bash(grep:*), Bash(gh stack:*), Read, Glob, Grep, AskUserQuestion
---

# Expert Rebase

Rebase the current branch on `origin/main`. If the rebase succeeds cleanly, offer to force-push. If conflicts arise, engage expert reviewers to analyze each conflicting hunk and recommend a resolution strategy — then apply with approval.

## Step 0.5: Stack Detection

**Stack Detection.** Run the **"Is-stacked (this branch)"** block, then the **"Detect layout"** block, from `~/.claude/prompts/worktree-reference.md`. This resolves `STACK_IS_STACKED`, `STACK_PARENT_BRANCH`, and `STACK_LAYOUT`. If `STACK_IS_STACKED` is false, skip layout detection and treat everything below as the ordinary (non-stacked) path. Also set `BRANCH=$(git branch --show-current)` for the push steps below.

## Step 1: Safety Check

Before touching anything, gather state:

```bash
git status --short
git branch --show-current
git log --oneline origin/main..HEAD
git log --oneline HEAD..origin/main 2>/dev/null | head -10
```

Report:
- Current branch name
- Number of local commits ahead of origin/main
- Number of commits on origin/main the branch doesn't have
- Any uncommitted changes (if dirty, stop and tell the user to stash first)

If the working tree is dirty, stop:
> "Your working tree has uncommitted changes. Stash or commit them first, then re-run `/expert-rebase`."

If already up to date with origin/main (0 commits behind), report "Already up to date." and stop.

## Step 2: Fetch

```bash
git fetch origin
```

## Step 3: Attempt the Rebase

```bash
if [ "$STACK_IS_STACKED" != "true" ]; then
  git rebase origin/main
elif [ "$STACK_LAYOUT" = "single-driver" ]; then
  # Do NOT rebase locally — gh stack sync (in the push step) both rebases and pushes.
  echo "single-driver stack: skipping local rebase; gh stack sync will rebase+push in the push step."
elif [ "$STACK_LAYOUT" = "per-branch" ]; then
  [ -n "$STACK_PARENT_BRANCH" ] || { echo "ERROR: per-branch stack but STACK_PARENT_BRANCH is empty — cannot rebase." >&2; exit 1; }
  git rebase origin/"$STACK_PARENT_BRANCH"
else
  echo "ERROR: STACK_LAYOUT is '$STACK_LAYOUT' (unknown) — cannot determine rebase strategy. Resolve layout manually before continuing." >&2
  exit 1
fi
```

Capture exit code. Two paths:

### Path A: Clean rebase (exit 0)

Report success briefly — branch name, how many commits were rebased.

Use `AskUserQuestion` to ask:
```
Do you want to force-push to origin?
```
Options:
- **Yes, push** — if `STACK_IS_STACKED` is false, run `git push --force-with-lease origin "$BRANCH"`. If stacked: single-driver → run the **"Push a stacked branch (new local work)"** block from `~/.claude/prompts/worktree-reference.md` (gh stack sync does both rebase and push; do not also run the local rebase from Step 3); per-branch → Step 3 already rebased, so force-push only: `git push --force-with-lease --force-if-includes origin "$BRANCH"` (do NOT re-run the push block's rebase); unknown → stop and ask the user before pushing.
- **No, I'll push later** — stop here

If push chosen, run it and report the result.

**Done.**

### Path B: Conflicts (exit non-zero)

Proceed to Step 4.

## Step 4: Identify Conflicts

Collect every conflicted file:

```bash
git status --short | grep '^UU\|^AA\|^DD\|^AU\|^UA\|^DU\|^UD' | awk '{print $2}'
```

For each conflicted file, read the full conflict markers — show content between `<<<<<<< HEAD`, `=======`, and `>>>>>>> <commit>`. Capture:

- **Ours** (HEAD — what's on the current branch)
- **Theirs** (origin/main — the incoming commit)
- **Context** — the surrounding code (5 lines above/below the conflict)

Also capture the commit message of the commit that introduced the conflicting change on origin/main:
```bash
git log --oneline -1 REBASE_HEAD 2>/dev/null || git log --oneline -1 origin/main
```

Summarize the conflict landscape before calling experts:
- N conflicts in M files
- Brief description of what each conflict is (e.g., "same function modified in different directions", "deleted in ours vs. modified in theirs")

## Step 5: Select Experts

Choose 3–5 experts based on what the conflicts involve. Use this guide:

| Expert | Trigger |
|--------|---------|
| Sam System (`sam-system.yaml`) | Conflicts span multiple files; factory wiring, config, or data flow affected |
| Uncle Bob (`uncle-bob.yaml`) | Architecture, structure, class/function design conflicts |
| Tara TypeSafe (`tara-typesafe.yaml`) | Type definitions, interfaces, or schema conflicts |
| Eric Evans (`eric-evans.yaml`) | Naming, domain model, bounded context conflicts |
| Security Sage (`security-sage.yaml`) | Auth, secrets, input validation, or error handling conflicts |
| Rachel (`rachel.yaml`) | Concurrency, async, shared state conflicts |
| Know-It-All Nigel (`know-it-all-nigel.yaml`) | Language-idiom conflicts — which style is more idiomatic |
| Contrarian Carl (`contrarian-carl.yaml`) | **Always runs last** — finds what the others missed |

Load each persona from `~/.claude/reviewers/<file>`. Use `summary.character` and `summary.voice` for persona, `principles` for their lens.

Tell the user which experts you selected and why (one line each).

## Step 6: Expert Analysis (Sequential)

For each selected expert (except Contrarian Carl), iterate and analyze **every conflict**:

```markdown
### [Expert Name]'s Analysis

**Conflict in `<file>`**
- **Ours**: [brief description of the HEAD version]
- **Theirs**: [brief description of the origin/main version]
- **My read**: [what this conflict represents from their domain perspective]
- **Recommendation**: [keep ours / keep theirs / merge both / needs discussion]
- **Rationale**: [why — grounded in their domain expertise]
- **Risk if wrong**: [what breaks if we pick the wrong side]
```

If multiple conflicts exist in the same file, group them under the file heading.

### Contrarian Carl (Last)

After all other experts, Carl reviews their collective recommendations:

```markdown
### Contrarian Carl

**What others agreed on**: [brief]
**Where I disagree**: [at least one recommendation Carl thinks is wrong, or a risk everyone missed]
**What nobody mentioned**: [a consequence, edge case, or assumption baked into the conflict that no one surfaced]
```

## Step 7: Synthesize and Checkpoint

Compile all expert recommendations into a resolution plan:

```markdown
## Conflict Resolution Plan

### `<file>` — Conflict 1
- **Resolution**: Keep [ours/theirs/combined]
- **Rationale**: [synthesized from expert input]
- **Dissent**: [note any expert who disagreed and why]

### `<file>` — Conflict 2
...

### Conflicts Needing Your Decision
[Any conflict where experts disagreed or where Carl raised a serious concern]
```

Use `AskUserQuestion` for any conflict where:
- Experts disagreed
- Carl flagged a serious concern
- The right answer depends on intent the experts can't know

For clear-consensus conflicts, just list them in the plan — no question needed.

**Wait for user approval before touching any files.**

## Step 8: Apply Resolutions

For each conflict, apply the approved resolution:

1. Edit the file to remove conflict markers and keep the correct content
2. `git add <file>` after resolving each file
3. After all files resolved: `git rebase --continue`

If `git rebase --continue` produces another conflict (multi-commit rebase), loop back to Step 4 for the new conflict set — noting which commit we're now applying.

## Step 9: Rebase Complete

Report final state:
- Branch name
- Commits rebased
- Conflicts resolved (count)

Use `AskUserQuestion`:
```
Rebase complete. Force-push to origin?
```
Options:
- **Yes, force-push** — if `STACK_IS_STACKED` is false, run `git push --force-with-lease origin "$BRANCH"`. If stacked: single-driver → run the **"Push a stacked branch (new local work)"** block from `~/.claude/prompts/worktree-reference.md` (gh stack sync does both rebase and push; do not also run the local rebase from Step 3); per-branch → Step 3 already rebased, so force-push only: `git push --force-with-lease --force-if-includes origin "$BRANCH"` (do NOT re-run the push block's rebase); unknown → stop and ask the user before pushing.
- **No, I'll push later**

If force-push chosen, run it and report.

Under single-driver layout, `gh stack sync` cascades to child branches automatically. Under per-branch layout, child branches are not updated — use the **"Restack a child (after its parent merged)"** runbook in `~/.claude/prompts/worktree-reference.md` for each child.

---

## Abort Protocol

If at any point the user wants to abort:

```bash
git rebase --abort
```

Report that the branch has been restored to its pre-rebase state.

## Notes

- Always use `--force-with-lease` (not `--force`) for pushes — it prevents overwriting upstream work you haven't seen.
- If the rebase involves many commits and conflicts recur across multiple commits, number each conflict round clearly so the user knows where they are.
- Never silently pick a side — every resolution should be explained.
