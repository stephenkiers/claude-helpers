# ADR-0010: Worktree clone layout

**Status:** Accepted

## Context

Downstream commands — `/track`, `/track-and-start`, and `/cleanup` — detect the worktree parent directory from the on-disk layout to manage issue caches and create sibling worktrees. Until now, this layout shape has been an implicit convention living in two places:

1. The detection logic in `prompts/worktree-reference.md` (the "Project Detection" block)
2. The clone instruction in `commands/setup-repo.md` (how `/setup-repo` sets up the initial directory structure)

With no ADR anchoring it, a new command re-encoding the layout had no reference to arbitrate the correct shape. A review flagged this gap: the invariant deserves a home.

## Decision

**The default-branch checkout is itself a worktree under a `worktrees/` directory.**

The on-disk layout produced by `/setup-repo` and consumed by the worktree workflow (`/track`, `/track-and-start`, `/cleanup`):

```
<repos-root>/<repo>/worktrees/<default-branch>/   ← the default-branch clone; this IS the main worktree
<repos-root>/<repo>/worktrees/<issue-or-slug>/    ← sibling worktrees created later by /track-and-start
```

Where `<repos-root>` defaults to `~/Repositories` and is user-overridable (via the `CLAUDE_REPOS_ROOT` env var or a `repos_root:` entry in `~/.claude/preferences.yaml`, as `/setup-repo` resolves it).

**Key invariant:** the default-branch checkout is not at the repository root. Instead, it lives under a directory named exactly `worktrees` — the siblings created by `git worktree add` live alongside it, all under the same `worktrees/` parent. **Detection logic keys off this parent directory name: when the parent of the main worktree is named `worktrees`, that parent is the worktree parent; otherwise, the worktree parent is sibling-adjacent to the main worktree (detected from existing siblings).**

## Consequences

- **Positional surprise:** The default-branch checkout lives under a subdirectory (`worktrees/<default-branch>`), not at the repository root. Documented in `/setup-repo` and in this ADR, so the shape is not accidental.
- **Two-place logic:** The detection logic appears in `prompts/worktree-reference.md` (the shared "Project Detection" block) and must remain in sync with how `/setup-repo` creates the structure. This ADR is the reference.
- **Fresh-clone layout awareness:** Detection in the "Project Detection" block (step 3 of `prompts/worktree-reference.md`, which calls `detect_worktree_parent` in `scripts/workflow/worktrees.py` via the CLI; see the 2026-10-13 amendments below) checks the canonical layout first: if the main worktree's parent is already named `worktrees`, that parent IS the worktree parent and no sibling heuristic applies. Only otherwise does it use the parent of the first usable non-main worktree (skipping paths under `~/.claude/`, paths nested under main, and parents named `feature`/`fix`/`chore`); only when no sibling qualifies does it fall back to `<main>/worktrees`, the single-tree layout. This ensures fresh `/setup-repo` clones resolve correctly without creating nested `worktrees/worktrees/` directories, even when a stray sibling worktree exists. See the 2026-10-13 amendment below.
- **The `worktrees` directory name is the detection key.** Both the fallback above and the existing `PROJECT_ROOT` derivation identify the layout solely by the main worktree's parent directory being named exactly `worktrees`. This is intentional and consistent across the shared block, but it means a checkout placed under an unrelated directory that happens to be named `worktrees` is treated as a layout repo. Repos set up outside `/setup-repo` should avoid naming a checkout's parent `worktrees` unless they intend this behavior.
- **Compatibility:** Any new command that manages worktrees must respect this layout invariant. Failure to do so breaks the assumption that sibling worktrees and the issue cache live in a stable, detectable location.
- **Stacked-push layout is a separate concern:** the single-driver vs per-branch distinction that governs stacked-PR push routing is recorded in [ADR-0011](0011-stacked-pr-layout-model.md), not here.

## Amendment (2026-10-13): detection precedence and fail-closed resolution (#163)

Worktree-parent detection used to reach the layout check only as a fallback when no second worktree
existed. A fresh `/setup-repo` clone that had a stray sibling worktree could therefore resolve to that
sibling's parent instead of its own `worktrees/` parent. #163 reorders detection so the canonical
layout check (main's parent named `worktrees`) runs first, before any sibling heuristic, in both the
shared bash block in `prompts/worktree-reference.md` and `detect_worktree_parent` in
`scripts/workflow/worktrees.py`.

Fail-closed behaviour: the Python implementation raises `WorktreeParentError` when the resolved parent
is the main worktree itself or lies under `~/.claude/`; the `worktree detect-parent` CLI exits 1 on the
same conditions. A git failure while listing worktrees also raises rather than returning an empty
result. An empty listing is not a fallback case: detection returns `""` and the `worktree detect-parent`
CLI exits 1 with `ERROR:`, so the caller fails closed. The detection key
remains the parent directory name `worktrees`; the reordering changes precedence, not the invariant.

## Amendment (2026-10-13): detection implemented once, in Python

The bash block in `prompts/worktree-reference.md` used to restate the detection heuristic alongside
`detect_worktree_parent` in `scripts/workflow/worktrees.py`, so the two could drift. Detection is now
implemented once, in `scripts/workflow/worktrees.py::detect_worktree_parent`, and command docs obtain it
through `worktree detect-parent` (`--cwd`; prints the parent path on success, `ERROR: <message>` to stderr
and exit 1 on failure). The bash block no longer contains any sibling-scan loop.

The precedence, as implemented by that function and set by #163, is:

1. Canonical layout: if the main worktree's parent is named `worktrees`, that parent is the worktree parent.
2. Otherwise, the first usable sibling worktree's parent, skipping entries under `~/.claude/`, entries
   nested under the main worktree, and entries whose parent is named `feature`, `fix`, or `chore`.
3. Otherwise, `<main>/worktrees` (single-tree layout).

The result is fail-closed: a resolved parent equal to the main worktree or under `~/.claude/`, or a git
failure, is an error and never a usable parent. The invariant that worktree directories are flat
`{issue_number}-{slug}` siblings and never contain `/` is unchanged.
