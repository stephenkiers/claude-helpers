# ADR-0021: Local merge queue

**Status:** Accepted (partial)

## Context

The core problem: when a solo maintainer merges a PR, the test gate runs against the PR's tip-tested
state (the result of rebasing onto `main` at enqueue time), not against the exact base the PR will
actually land on. By the time the tests finish, another PR may have merged and `main` may have moved,
so the "passing" PR could land on an untested base — introducing a hidden integration risk.

A prior integration-gap incident caught this exact problem: a test passed on one base, but a concurrent PR merged
in the meantime, and the first PR's merge landed on an untested state. Detecting this required manual
analysis; the queue should catch it automatically.

## Decision

**Implement a local, deterministic merge queue:**

1. **Serialization:** All PRs queue through `merge.lock` (kernel flock on a never-unlinked file in the
   git state directory). Only one PR holds the lock at a time; the others wait.

2. **Ordering:** Arrival order is proven via ticket files (`tickets/<n:012d>`) created with `O_EXCL`
   under `alloc.lock` (a short-held allocator lock). Tickets are locked, probed, and unlinked as each
   waiter's turn arrives. This is fairer than bare flock (which makes no ordering promise) and avoids
   a counter file (which would need atomic compare-and-swap).

3. **Testing against the exact base:** Inside the lock, the queue fetches `origin/<base>`, runs the
   merge gate against `origin/<base>`, and verifies the base has not moved since the fetch. Only then
   does it push and merge. This closes the landing-base integration gap.

4. **Unverified-main detection:** If the base branch has untrailered commits (showing it may not be
   in a trusted state), the queue runs the main gate in a scratch worktree before proceeding. A
   `base-verified/<sha>` record marks successful verifications; `base-failed/<sha>` marks failures.
   The first failure launches Claude; subsequent waiters for the same base are kicked back without
   ceremony. `merge-queue reverify` clears a false positive. A new base sha clears the halt.

5. **Kick-back, not blocking:** On conflict, gate failure, or push rejection, the PR is kicked back
   with the exact issue. The result is written to `result.json`. Subsequent PRs in the queue proceed
   immediately (not blocked by an earlier failure).

6. **Force-push carve-out:** The queue's force-push is `--force-with-lease=<branch>:<lease_sha>`
   (the lease pins the branch's *pre-rebase* tip, so the push is rejected if the remote moved
   underneath it) with an explicit refspec `<tested_sha>:refs/heads/<branch>` for what's actually
   pushed, audited in a single function (`force_push_tested()`) with no other push call site in the
   queue module and no bare `--force` or `+`-prefixed refspecs. This is one of three **authorized
   force-pushes** in this repo's tooling: the merge queue's pinned force-push (after the full gate
   passes), `/stack-sync`'s `--force-with-lease` push (gated behind the project check gate per child),
   and `/expert-rebase`'s `--force-with-lease` push (confirmation-gated for the branch being rebased).
   All three use `--force-with-lease` (never bare `--force`) and never inherit from `git config`.

7. **Parallel mutation seam:** This module's `Runner.run_git`/`Runner.run_gh` are a separate, self-audited
   choke point for merge-queue mutations — they do not go through `scripts/workflow/mutations.py`'s
   `check_mutation_allowed()` allowlist (ADR-0013). This is intentional: the queue's mutating calls
   (rebase, the pinned force-push, `gh pr merge` with queue-specific flags) are shaped differently
   from what that allowlist covers, and `force_push_tested()` already provides an equivalent, narrower
   audit for the one call that matters most. Anyone editing this module should keep both funnels in
   mind rather than assume ADR-0013's allowlist covers this code.

## Configuration

The queue reads a local per-machine config file at one of these locations (in order):
- `--config <path>` command-line flag
- `MERGE_QUEUE_CONFIG` environment variable
- Default: `<container>/merge-queue.json`, where `<container>` is the parent of the `worktrees/`
  directory (resolved via `git rev-parse --path-format=absolute --git-common-dir`)

If the layout doesn't match a `worktrees/` ancestor, the queue fails closed with a message naming
both override mechanisms. The config is **never** read from `origin/<base>` or the PR worktree —
it is local, per-machine state only. `--config`/`MERGE_QUEUE_CONFIG` overrides are trust-boundary
checked: the resolved path must lie outside the PR worktree (under `git-common-dir`), and the check
itself fails closed — raising rather than silently allowing the override through — if
`git-common-dir` can't be determined.

### Config schema

```json
{
  "base": "main",
  "steps": [
    "just test",
    {"cmd": "just build", "timeout_secs": 300}
  ],
  "cleanup": true,
  "scratch_setup": [
    "git fetch origin main:main"
  ],
  "inherit_lock_fd": false,
  "allow_unverified": [
    "abc123def456...(40-hex SHA)..."
  ],
  "mutation_timeout_secs": 300,
  "pr_merge_poll_secs": 60
}
```

- **`base`** (required, non-empty string): Must equal the repo's default branch (from `gh repo view
  --json defaultBranchRef`).
- **`steps`** (required, non-empty list): Gate steps to run. Each step is either a string (shell
  command) or an object `{cmd: string, timeout_secs: positive int}`. Steps run in order; the first
  failure kicks back the PR.
- **`cleanup`** (required, boolean): If `true`, run cleanup via `cleanup.py` after a successful
  merge (from the base worktree, with the configured `base` branch, not `main`).
- **`scratch_setup`** (optional, list of strings): One-time setup commands to run when the scratch
  worktree is created (e.g., `git fetch origin main:main`). The scratch worktree lives at a fixed,
  repo-keyed path outside any checkout's own directory tree (`~/.claude/merge-queue-scratch/<hash>/scratch`,
  see `get_scratch_dir()`) so ancestor-directory-walking tools (Cargo workspace discovery, `go.work`)
  never escape it into an enclosing repo checkout. That location's depth relative to sibling
  directories will not generally match a real worktree's depth, though, so a project with a relative
  path dependency on a sibling repo (`../../../sibling-repo/crate`) will resolve it incorrectly
  inside scratch even though it resolves fine in a real worktree. Use `scratch_setup` to paper over
  that mismatch with a symlink, e.g. `ln -sfn /abs/path/to/sibling-repo .git/sibling-repo` plus
  adjusting the dependency's path, or a `[patch]`/path override scoped to the scratch checkout.
- **`inherit_lock_fd`** (optional, boolean, default `false`): If `true`, pass the `merge.lock` fd
  to step child processes (for repos where real-app E2E needs to hold the lock across steps).
- **`allow_unverified`** (optional, list of 40-hex SHAs): Shas of commits on the base branch that
  are excused from the trailerization check individually (not anchors). Allows a hotfix to land
  without first poisoning the base; the scan still continues below it.
- **`mutation_timeout_secs`** (optional, positive int, default `300`): Fallback timeout for any gate
  or scratch-setup step that doesn't set its own `timeout_secs`.
- **`pr_merge_poll_secs`** (optional, positive int, default `60`): How long to poll GitHub for
  PR-metadata convergence before merging.

Unknown keys are rejected. Error messages point to this schema.

## Trust Boundary

The config is local, per-machine, and outside the repo — a PR cannot change it. The queue is only as
trustworthy as the PR's own `steps` commands, because:

- Step commands are maintainer-authored shell strings (e.g., `just test`, `cargo test`).
- They run in the PR worktree, where they see the PR's code and can run its recipes.
- Untrusted code in the PR (e.g., a malicious test setup) can do anything the maintainer can do.

The queue's safeguards (serialization, base testing, force-push auditing, unverified-main detection)
protect against *accidents* and *integration gaps*, not against a hostile maintainer or a hostile PR
that already has review approval.

## Decision 5: Post-kickback worktree contract

When a rebase completes but a subsequent operation fails (step failure, base moved, push rejected),
the PR is kicked back. By default, the worktree is left at the tested commit (the rebase's result
SHA) rather than being rolled back to the original pre-rebase tip.

The preflight check (run on the next enqueue of the same PR) accepts this state without requiring a
re-push:
- If `remote/<branch>` == original pre-rebase tip but `local HEAD` == tested commit from the last KICKBACK
  (recorded in `result.json`), preflight allows the enqueue to proceed.
- This avoids a required re-push when the developer hasn't touched the branch since the kickback.

The worktree may remain at this tested SHA until the developer either:
1. Fixes the issue locally and pushes a new commit (satisfying the normal push-completeness check).
2. Enqueues again without changes (using the Decision 5 fallback via `result.json`).

## Trailerization and Provenance

The `Merge-Gate:` trailer (added to the commit message by the queue) records which base and head
shas were tested:

```
Merge-Gate: merge-queue tested-base=<40hex> tested-head=<40hex>
```

This trailer is **provenance against accidents**, not a hostile committer:

- **Valid trailer:** appears only in the final trailer block of the commit message (not the subject
  or mid-body), `tested-base` is an ancestor of the commit, and both shas are exactly 40 hex digits.
- **Scan logic:** when checking if the base has untrailered commits, the queue walks back from
  `origin/<base>` to the first *anchor* (a commit with a valid trailer, a sha with a `base-verified`
  record, or a sha in the `allow_unverified` list). Any commits in between are reported as untrailered.
- **Parsing:** the trailer is extracted via `git interpret-trailers --parse`, not string search or
  regex.
- **Commit body shape:** the merge commit body is `"Tested by merge-queue.\n\n<trailer>"`, not the
  bare trailer line — `git interpret-trailers --parse` only recognizes a trailer block that follows
  a non-trailer paragraph and a blank line, so a body consisting solely of the trailer would be
  silently dropped by the parser that reads it back.

The trailer defends against forgetting to retest after a force-rewrite or stale CI state, not against
a committer who deliberately forges one.

## Residuals

These gaps cannot be closed locally and are documented as follows:

1. **Testing-to-merge TOCTOU window:** Between the base-unmoved verification (in Decision 3's step 9)
   and the final merge operation, another clone can push to the base, making the tested base stale
   before the merge lands. The next queue run's poison scan detects the untrailered commit and runs
   the main gate automatically (Decision 4, auto-anchoring). A GitHub "require up to date" ruleset
   would close this window server-side.

2. **SIGKILL orphan:** If the queue process receives `SIGKILL` alone (not SIGINT/SIGTERM), and
   `inherit_lock_fd` is `false` (the default), the `merge.lock` fd is released but any step process
   group may still be running, allowing the next PR's gate to start before the previous step finishes.
   The `finally` block in the queue covers SIGINT/SIGTERM and exceptions. `inherit_lock_fd: true`
   keeps the lock open across steps (for real-app E2E) and prevents overlap on SIGKILL. A machine-wide
   E2E lock is a follow-up.

3. **Cleanup concurrency:** Cleanup (deleting the PR worktree and syncing the base branch) runs
   after the lock is released and races with the next PR's fetch/rebase. A `git fetch` ref-lock
   error (`cannot lock ref`) is a known outcome; a bounded retry (3 attempts with backoff) is
   documented as a mitigation but not yet implemented (see Built vs. Planned). This is the only
   retry in the system (push and merge are never retried).

## Bypass Limit and Rollout Metric

The queue is opt-in per repo (gated by `merge-queue.json` existence). Before considering server-side
enforcement (e.g., GitHub rulesets), the queue must prove itself locally:

- **Bypass metric:** `git log --grep=Merge-Gate <default-branch>` counts trailered merges; the
  untrailered commit count is the bypass rate.
- **Rollout gate:** Require two clean weeks (14 consecutive days with zero untrailered commits before
  server-side enforcement is considered).

## Per-SHA Base Records and Reverify

Base verification records are content-addressed (keyed by the base sha):

- **`base-verified/<sha>`:** written when the main gate passes on that sha. Contains the step name
  and log path.
- **`base-failed/<sha>`:** written when the main gate fails on that sha. Contains the step name and
  log path.

If a base-failed record exists for the current base, the queue kicks back immediately without
re-running the gate. Only the run that wrote the record launches Claude. A `merge-queue reverify [<sha>]`
command deletes the record, allowing retry (or defaults to the current base sha if no sha is given).

The first PR to enqueue at a new base sha also runs the gate at that sha if no verification record
exists. A successful run clears any `base-failed` record for a previous sha.

**Auto-anchoring:** if the base has no anchor (no trailered commit, no verification record, no allowlist
match going back through all reachable history), the queue runs the verification automatically (as part
of the enqueue flow), acquiring `merge.lock` and running the main gate in the scratch worktree before
proceeding. On success, the base is recorded as `base-verified` and subsequent enqueues can proceed
(the verified sha is an anchor). The `merge-queue bootstrap` command exists as an explicit escape hatch
to pre-verify a base before any PR enqueue, following the same path: acquire `merge.lock`, verify in
scratch, and write `base-verified`.

## Built vs. Planned

The implementation is **partial**: the following features are **documented but not yet built**:

- **Explicit confirmation for bootstrap:** The `merge-queue bootstrap` command is documented as having
  an optional confirmation step (`--yes` flag to skip it), but the current implementation does not prompt
  for confirmation — it silently verifies and records. (needs a tracking issue)
- **Claude hand-off on base verification failure:** When the base fails verification, the ADR describes
  launching Claude; the current implementation writes a `base-failed` record and kicks back immediately
  without triggering Claude. (needs a tracking issue)
- **`resume` subcommand:** Documented in Per-SHA Base Records but not built. The CLI accepts 
  `resume [--pr N]` but returns non-zero (unimplemented). (needs a tracking issue)
- **`--no-claude` flag for enqueue:** Reserved for future use; accepted by the CLI but currently ignored.
  (needs a tracking issue once Claude integration is designed)
- **Cleanup consumption:** The config's `cleanup` field is validated but never read or acted upon; the
  actual cleanup integration is planned but not implemented. (needs a tracking issue)
- **Bounded git-fetch retry on cleanup:** Residual 3 describes a bounded (3-attempt) `git fetch` ref-lock
  retry with backoff during cleanup. The current implementation does not retry on cleanup-phase fetch
  failures. (needs a tracking issue)

**Built features:**
- **Automatic base verification on unanchored base:** Enqueue automatically verifies an unanchored base
  before proceeding, without requiring explicit bootstrap. Bootstrap exists as an optional escape hatch
  to pre-verify ahead of time.
- **Successful verification clears base-failed:** When the base passes verification, any prior
  `base-failed` record for a previous sha is superseded by the new `base-verified` record for the
  current sha.
- **Partial fetch timeout retry (M2):** Timeout handling for fetch and push operations re-queries state
  to verify actual success or failure, avoiding false negatives on slow networks. This is narrower than
  the full 3-attempt retry mentioned in Residual 3.
- **Decision 5 post-kickback contract:** Worktrees are left at the tested commit after a kickback,
  and preflight checks accept this state without requiring re-push if the local HEAD matches the last
  KICKBACK's tested_sha.
- **ADR-0013 mutation-allowlist exception:** The queue's mutations (`Runner.run_git`/`Runner.run_gh`)
  bypass ADR-0013's `check_mutation_allowed()` allowlist per Decision 7. This exception is now
  documented in both ADR-0021 (Decision 7) and ADR-0013 Amendment (as a formal note).
- **`/merge-and-cleanup` enforcement:** The command refuses PRs that target a merge queue's base
  branch, with a live check at plan time (for UX) and again at apply time (authoritative gate).
  Base-scoped refusal (Q2) allows stacked PRs targeting other branches to merge normally. The CLI
  hardens against shadowing by running in the claude-helpers checkout. See Amendment above.

## Amendment: `/merge-and-cleanup` enforcement

The `/merge-and-cleanup` command now refuses PRs that target a merge queue's configured base
branch. When a queue config resolves and the PR's base equals the queue base, the command exits
with code 3 and instructs the user to use `/queued-merge` instead. PRs targeting other branches
(stacked children, release branches) proceed normally through the existing merge gate.

**Enforcement point:** A typed `detect_merge_queue()` function in `merge.py` runs a live check against
the queue resolver (`resolve_config_path()`), re-run inside `apply_merge()` before the lock to catch
PRs retargeted after the plan phase. No cache is stored; detection happens fresh on each run.

**Configuration precedence (Q3):** The order is `--config` > `MERGE_QUEUE_CONFIG` > default `<container>/merge-queue.json`.
- An override (flag or env) that is set but points to a missing file → refuse, naming the override source.
- Any resolver error when an override is active (including trust-boundary rejection) → refuse with the error.
- No override + `LayoutMismatchError` (non-`worktrees/` layout) → proceed (queue absent, repo not queue-capable).
- No override + any other resolver error → refuse.
- No override + default config file present → proceed (queue configured).
- No override + default missing → proceed (queue absent).

This reads as: the guard and `/queued-merge` have identical visibility into the queue state,
ensuring no cross-tool disagreement (Decisions Q1 and Q3).

**Setup offer on absent:** When detection is `absent` with a known default path (queue-capable
layout, no config yet), `/merge-and-cleanup` offers — via one `AskUserQuestion`, never silently — to
create `<container>/merge-queue.json` and hand the PR to `/queued-merge`. The proposal comes from
`cli merge queue-init` (`{base: <default branch>, steps: [<repo-cache check | just check>], cleanup:
true}`; never `just merge`, which may merge on its own), is validated with the queue's own
`validate_config_data()`, and is written create-only so an existing config is never overwritten.
Declining merges as before. A non-`worktrees/` layout gets no offer.

**Base scoping (Q2):** Refusal is conditional on `PR.base == queue.base`. Stacked PRs that target
a parent branch (or any non-base branch) fall through to the normal merge gate, so stacked workflows
are not disrupted. The base is re-read live in `apply_merge()` to catch retargets.

**Threat model:** This enforcement stops *accidental* bypass by our own tooling (a developer
running `/merge-and-cleanup` when they should run `/queued-merge`). It does not stop deliberate
bypass (editing the PR, running raw `gh pr merge`, exporting a dead-path env var, moving the
config file). Those are detected afterwards by ADR-0021's Bypass Limit metric (untrailered commit
count), and server-side rulesets remain the only layer that enforces the queue universally.
Detection sees env + default only, not another command's `--config` flag.

**No bypass flag (Q4):** If the queue is broken or misconfigured, the recovery path is:
1. Fix the config or env var, then retry `/merge-and-cleanup`.
2. If the queue is unrepairable, manually run `gh pr merge` outside the tooling. ADR-0021's
   Bypass Limit metric (`git log --grep=Merge-Gate <default-branch>`) records the untrailered commit.

**CLI hardening (Q5):** Both `/merge-and-cleanup`'s CLI calls run with the claude-helpers checkout
as the process `cwd`, so a PR worktree's `scripts/workflow/cli.py` cannot shadow the guard. The
`merge plan` command takes a `--cwd` flag to specify the caller's directory for auto-detect and
absolute-path resolution.

## Follow-ups

- **GitHub ruleset:** after two clean weeks of local queuing, add a GitHub ruleset requiring the
  `Merge-Gate:` trailer and `tested-base` ancestry to prevent out-of-order merges via the GitHub UI.
- **Retire or shrink merge.py:** the existing `merge.py` (O_EXCL lock, 1800s timeout, `capture_output`)
  provides non-serialized merging for single-developer workflows and testing; the queue is the
  serialized production path. The `/merge-and-cleanup` enforcement amendment adds the queue guard
  (`detect_merge_queue()` / `queue_guard()`) to `merge.py`, so it now refuses PRs on the queue's base;
  its non-queue merge path is otherwise unchanged.
- **Machine-wide E2E lock:** a project may need a machine-wide lock order (queue lock → E2E lock) to
  prevent step orphans when `inherit_lock_fd` is false. Set `inherit_lock_fd: true` per repo as a
  temporary measure.

## Consequences

- **Good:** Every PR is tested against the exact base it lands on. Unverified main is caught before
  re-run. Conflicts, gate failures, and push rejections don't block the queue. The force-push is
  audited and constrained to a single call site. Kick-back and Claude hand-off happen after the lock
  is released.
- **Cost:** An additional local config file must exist (out-of-repo, local state). Cleanup runs
  concurrently with the next PR's early steps (mitigated by ref-lock retry). The step 8/9 TOCTOU
  window persists until server-side enforcement.
- **Error messages:** point to the config schema (this ADR section).
- **Debugging:** `merge-queue status` shows holder, current step, and elapsed time. `result.json`
  captures the outcome and logs. `merge-queue reverify` is the escape hatch for flaky E2E.
