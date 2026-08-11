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

Where `<repos-root>` defaults to `~/Repositories` and is user-overridable (e.g., via `REPOS_ROOT` env var in `/setup-repo`).

**Key invariant:** the default-branch checkout is not at the repository root. Instead, it lives under a directory named exactly `worktrees` — the siblings created by `git worktree add` live alongside it, all under the same `worktrees/` parent. **Detection logic keys off this parent directory name: when the parent of the main worktree is named `worktrees`, that parent is the worktree parent; otherwise, the worktree parent is sibling-adjacent to the main worktree (detected from existing siblings).**

## Consequences

- **Positional surprise:** The default-branch checkout lives under a subdirectory (`worktrees/<default-branch>`), not at the repository root. Documented in `/setup-repo` and in this ADR, so the shape is not accidental.
- **Two-place logic:** The detection logic appears in `prompts/worktree-reference.md` (the shared "Project Detection" block) and must remain in sync with how `/setup-repo` creates the structure. This ADR is the reference.
- **Fresh-clone layout awareness:** The empty-siblings fallback in the "Project Detection" block (step 3 of `prompts/worktree-reference.md`) must be layout-aware: when there is no second worktree, check whether the main worktree's parent is already named `worktrees` — if so, that parent IS the worktree parent; otherwise, assume the old single-tree layout and create `worktrees/` at the main worktree level. This ensures fresh `/setup-repo` clones resolve correctly without creating nested `worktrees/worktrees/` directories.
- **Compatibility:** Any new command that manages worktrees must respect this layout invariant. Failure to do so breaks the assumption that sibling worktrees and the issue cache live in a stable, detectable location.
