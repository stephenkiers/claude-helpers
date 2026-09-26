# ADR-0021: Local merge queue

**Status:** Accepted

## Context

The core problem: when a solo maintainer merges a PR, the test gate runs against the PR's tip-tested
state (the result of rebasing onto `main` at enqueue time), not against the exact base the PR will
actually land on. By the time the tests finish, another PR may have merged and `main` may have moved,
so the "passing" PR could land on an untested base — introducing a hidden integration risk.

The previous incident (lotl #1431) caught this: a test passed on one base, but a concurrent PR merged
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

6. **Force-push carve-out:** The queue's only force-push is `--force-with-lease=<branch>:<tested_sha>`
   with an explicit refspec `<tested_sha>:refs/heads/<branch>`, audited in a single function with no
   other push call site in the queue module and no bare `--force` or `+`-prefixed refspecs. This is
   the **only authorized force-push** — it happens only after the full gate passes, and it is never a
   bare `--force` or inherited from `git config`.

## Configuration

The queue reads a local per-machine config file at one of these locations (in order):
- `--config <path>` command-line flag
- `MERGE_QUEUE_CONFIG` environment variable
- Default: `<container>/merge-queue.json`, where `<container>` is the parent of the `worktrees/`
  directory (resolved via `git rev-parse --path-format=absolute --git-common-dir`)

If the layout doesn't match a `worktrees/` ancestor, the queue fails closed with a message naming
both override mechanisms. The config is **never** read from `origin/<base>` or the PR worktree —
it is local, per-machine state only.

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
  ]
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
  worktree is created (e.g., `git fetch origin main:main`).
- **`inherit_lock_fd`** (optional, boolean, default `false`): If `true`, pass the `merge.lock` fd
  to step child processes (for repos where real-app E2E needs to hold the lock across steps).
- **`allow_unverified`** (optional, list of 40-hex SHAs): Shas of commits on the base branch that
  are excused from the trailerization check individually (not anchors). Allows a hotfix to land
  without first poisoning the base; the scan still continues below it.

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

The trailer defends against forgetting to retest after a force-rewrite or stale CI state, not against
a committer who deliberately forges one.

## Residuals

These gaps cannot be closed locally and are documented as follows:

1. **Step 8/9 TOCTOU window:** Between the base-unmoved check (step 8) and `gh pr merge` (step 9),
   another clone can push to the base, making the tested base stale before the merge lands. The next
   queue run's poison scan detects the untrailered commit and runs the main gate. A GitHub
   "require up to date" ruleset is a follow-up.

2. **SIGKILL orphan:** If the queue process receives `SIGKILL` alone (not SIGINT/SIGTERM), and
   `inherit_lock_fd` is `false` (the default), the `merge.lock` fd is released but any step process
   group may still be running, allowing the next PR's gate to start before the previous step finishes.
   The `finally` block in the queue covers SIGINT/SIGTERM and exceptions. `inherit_lock_fd: true`
   keeps the lock open across steps (for real-app E2E) and prevents overlap on SIGKILL. A machine-wide
   E2E lock is a follow-up.

3. **Cleanup concurrency:** Cleanup (deleting the PR worktree and syncing the base branch) runs
   after the lock is released and races with the next PR's fetch/rebase. A `git fetch` ref-lock
   error (`cannot lock ref`) triggers a bounded retry (3 attempts with backoff); this is the only
   retry (push and merge are never retried).

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

**Bootstrap:** if the base has no anchor (no trailered commit, no verification record, no allowlist
match going back through all reachable history), the queue fails closed with "base is unbootstrapped —
run `merge-queue bootstrap`". The bootstrap command:

- Asks for explicit confirmation (or accepts `--yes`).
- Records the current `origin/<base>` sha as `base-verified`.
- Allows subsequent enqueues to proceed (the bootstrap sha is an anchor).

## Follow-ups

- **GitHub ruleset:** after two clean weeks of local queuing, add a GitHub ruleset requiring the
  `Merge-Gate:` trailer and `tested-base` ancestry to prevent out-of-order merges via the GitHub UI.
- **Retire or shrink merge.py:** the existing `merge.py` (O_EXCL lock, 1800s timeout, `capture_output`)
  can be retired or simplified once the queue is stable; this PR deliberately does not modify it
  (ADR-0021 Step 3, Out of Scope).
- **Machine-wide E2E lock:** lotl may need a machine-wide lock order (queue lock → E2E lock) to prevent
  step orphans when `inherit_lock_fd` is false. Set `inherit_lock_fd: true` per repo as a temporary
  measure.

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
