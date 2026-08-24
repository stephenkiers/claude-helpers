# ADR-0012: /stack-sync — layout-routed stack sync

**Status:** Accepted

## Context

PR #64 shipped the layout-routed **push** side of stacked-PR workflows: `/shipit`, `/expert-rebase`, and the
expert/haiku/shipit ecosystem consult the single-driver vs per-branch distinction from
[ADR-0011](0011-stacked-pr-layout-model.md) and route `gh stack sync` / push accordingly. But it left four
gaps on the **sync** side:

1. **No executor for the restack runbook.** `/cleanup` emitted a "Restack-a-child" prose runbook telling the
   user what to run after a parent merged — but nothing actually ran it. The user had to copy-paste git
   commands by hand, and frequently skipped it, leaving children stale against a merged-or-advanced parent.
2. **No ongoing-sync mode.** ADR-0011's routing only covered the push at ship time. When a parent PR is still
   *open* and advances (force-pushes after review feedback), the children had no command to rebase onto the
   new parent tip — only the post-merge case was handled.
3. **No bottom-up walker for 3+ level stacks.** The Restack-a-child block assumed a single parent/child pair.
   A stack of three or more levels (pivot → mid → leaf) had no ordered rebase primitive; running the blocks
   in the wrong order re-stales a freshly rebased child against a not-yet-rebased parent.
4. **No unified "sync the stack" entry point.** Each gap above was a separate manual procedure; there was no
   one command a user (or a lifecycle command) could call to mean "bring this stack up to date."

`/stack-sync` is the sync-side command that closes these gaps. It is the layout-routed **sync** mirror of
#64's layout-routed **push**. It does **not** re-push the pivot branch — that is already pushed by
`/shipit` or `/expert-rebase`; `/stack-sync` only syncs descendants onto the current parent state.

## Decision

**`/stack-sync` inherits ADR-0011's layout routing and re-imposes no ban.** The command detects the layout
structurally (single-driver / per-branch / unknown) using the same primitives as the push side, then routes:

1. **Single-driver layout → delegate to `gh stack sync`.** One working copy drives the stack; `gh stack sync`
   fetches, cascade-rebases, and pushes atomically. This is the safe case ADR-0011 re-legalized.

2. **Per-branch layout → manual `git -C` bottom-up walk.** Each descendant branch lives in its own sibling
   worktree; `gh stack sync` / `checkout` / `init` are fatal (per ADR-0011), so `/stack-sync` walks the
   descendants itself using `git -C <worktree>`.

3. **Unknown layout → fail closed (STOP and ask).** When the layout cannot be proven from worktree metadata
   or the `.stack.parentBranch` cache, `/stack-sync` halts and prompts the user rather than assuming
   single-driver.

**Descendants are collected via a topological walk and verified (not ordered) by
`git merge-base --is-ancestor`, not by the `.stack.parentBranch` cache.** The cache may be empty or stale
(a freshly created sibling worktree has no `.stack.parentBranch` until something writes it). The reliable
ordering is structural: an ancestor is rebased before its descendant. A cycle in the ancestry graph
(A is ancestor of B and B is ancestor of A) is a hard error — `/stack-sync` aborts rather than guessing
a topology.

**The Restack-a-child block is generalized to `<NEW_BASE>` / `<OLD_BASE>`.** Rather than two separate blocks
(one for ongoing mode where the parent advanced, one for post-merge mode where the parent was merged into
trunk), `/stack-sync` uses one primitive parameterized by the old and new base. Ongoing mode sets
`<NEW_BASE>` to the parent's new tip and `<OLD_BASE>` to the parent's previous tip; post-merge mode sets
`<NEW_BASE>` to the trunk the parent merged into and `<OLD_BASE>` to the parent's merged commit. One block
means the two modes cannot drift apart.

**Force-push is gated twice: once on confirmation, once on the repo-cache check.** Before any force-push,
`/stack-sync` asks for a pre-push confirmation (skippable via `--yes`), and additionally consults the
`repo-cache.json` check gate that the push side already enforces. The two gates are independent: the
confirmation gate is a human-in-the-loop safety, the cache gate is a structural guard against re-pushing
branches whose state has moved underneath the local view.

## Consequences

- **Inherits ADR-0011's routing; does not re-ban `gh stack sync`.** The per-branch case is still forbidden
  from calling `gh stack sync` — it walks manually instead. The single-driver case uses `gh stack sync`
  directly. No new blanket ban is introduced.
- **One Restack-a-child primitive, two modes.** Ongoing-sync and post-merge share the `<NEW_BASE>` /
  `<OLD_BASE>` block. A future third mode would extend the same block rather than fork it; two blocks would
  inevitably drift, re-introducing gap #3 at the block level.
- **Ordering correctness depends on the topological walk (verified by `git merge-base --is-ancestor`), not
  on cache freshness.** Commands that skip the structural ancestry check and trust `.stack.parentBranch`
  will mis-order 3+ level stacks when the cache is empty. `/stack-sync`'s cycle-detection also means a
  corrupted `.stack` topology cannot cause a non-terminating rebase loop.
- **Does not re-push the pivot.** `/stack-sync` syncs descendants only. The pivot branch is pushed by
  `/shipit` / `/expert-rebase`. Calling `/stack-sync` before the pivot is pushed is a no-op for the pivot
  and a rebase-onto-trunk for descendants — which is correct, not a bug.
- **Two-gate force-push is the cost of safety.** The pre-push confirmation adds one prompt to every
  force-push; `--yes` exists for scripted/lifecycle callers that have already obtained consent. The
  repo-cache gate adds a read of `repo-cache.json` before each push. Both are intentional and both must
  remain; removing either re-opens the force-push-corrupts-remote class that ADR-0011's fail-closed
  detection only half-closes.
- **Cross-reference ADR-0011.** This ADR is the sync companion to ADR-0011's push routing. Any new command
  that syncs or rebases a stack must consult ADR-0011 for the layout model and this ADR for the sync-side
  invariants.
