# ADR-0011: Stacked-PR layout model (single-driver vs per-branch)

**Status:** Accepted

## Context

Stacked-PR commands — `/shipit`, `/expert-rebase`, and the shared primitives in `prompts/worktree-reference.md` — need to distinguish between two fundamentally different working layouts when deciding whether certain git operations are safe or fatal:

1. **Single-driver layout:** one working copy drives the entire stack (other stack branches are NOT checked out in sibling worktrees). Under this layout, `gh stack sync` is safe — it fetches, cascade-rebases the whole stack onto the updated trunk, and pushes all branches atomically in one command.

2. **Per-branch layout:** each stack branch is checked out in its OWN sibling worktree. Under this layout, `gh stack sync` / `checkout` / `init` are fatal — they attempt to check out branches that are already permanently checked out in sibling worktrees, causing conflicts and potential corruption.

This ADR records a reversal of PR #55, which blanket-forbade `gh stack sync` / `checkout` / `init` in the repo's worktree tooling. That blanket ban was too broad because it did not distinguish between these two layouts. The ban correctly protected the per-branch case but unnecessarily prohibited the safe single-driver case.

The distinction and the routing logic currently live only in prose scattered across four files: `commands/shipit.md`, `commands/expert-rebase.md`, `prompts/worktree-reference.md`, and `docs/adr/0010-worktree-clone-layout.md`. With no reference of record, new stacked-push commands risk re-encoding the layout detection logic independently, violating the same "layout as implicit convention" gap that ADR-0010 was created to close (now reappearing one level up).

## Decision

**Layout determines whether `gh stack sync` is safe or fatal.** Commands must detect the layout structurally, fail closed to `unknown` (which triggers a stop-and-ask prompt), and delegate all push and rebase routing to the shared primitives in `prompts/worktree-reference.md`.

**Layout detection:**
- **Single-driver:** one working copy has all stack branches' state locally. No sibling worktrees are checked out for other stack branches. Safe for `gh stack sync`.
- **Per-branch:** other stack branches are already checked out in sibling worktrees. Fatal for `gh stack sync` / `checkout` / `init`.
- **Unknown:** the layout cannot be proven from worktree metadata or sibling caches (`.stack.parentBranch` absent or ambiguous). Must prompt the user rather than assuming.

Layout is determined by examining Git worktree metadata and the `.stack.parentBranch` cache files in sibling worktrees — this is structural detection, not user configuration.

**Single source of truth:** `prompts/worktree-reference.md` contains the layout-detection logic and the push/rebase routing primitives. All commands that push or rebase stacked branches must consult and delegate to this shared block rather than re-encoding the logic.

## Consequences

- **Reverses PR #55's blanket ban:** `/shipit`, `/expert-rebase`, and any new stacked-push commands may safely use `gh stack sync` when the layout is proven to be single-driver. The per-branch case is still forbidden — the routing logic enforces this.
- **New stacked commands must consult this ADR:** Any command that manages a stacked-PR workflow — rebasing, pushing, or syncing a stack — must:
  1. Read this ADR to understand the layout distinction.
  2. Use the layout-detection and push-routing primitives from `prompts/worktree-reference.md`.
  3. Never re-encode the layout detection or push routing logic in the command itself.
- **Safety cost of wrong single-driver force-push:** If layout detection incorrectly identifies a per-branch setup as single-driver, a `gh stack sync` or force-push command could corrupt worktree state. This is why detection fails closed to `unknown` — when in doubt, prompt rather than assume single-driver.
- **Complementary to ADR-0010:** ADR-0010 owns the *clone and worktree* layout shape (the `worktrees/` directory structure). This ADR owns the *stacked-push* layout model (which branches are checked out where, and when `gh stack sync` is safe). The two are orthogonal concerns: a repo can follow ADR-0010's clone layout while using either the single-driver or per-branch stacked-push model.
- **Cross-reference PR #55:** See the commit history of PR #55 for context on the original blanket ban and why this reversal is deliberate, not a bugfix.
