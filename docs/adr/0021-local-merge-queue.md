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
   queue module and no bare `--force` or `+`-prefixed refspecs. This is one of four **authorized
   force-pushes** in this repo's tooling: the merge queue's pinned force-push (after the full gate
   passes), `/stack-sync`'s `--force-with-lease` push (gated behind the project check gate per child),
   `/shipit`'s `--force-with-lease` push (confirmation-gated via the Ask block in a flow), and
   `/expert-rebase`'s `--force-with-lease` push (confirmation-gated for the branch being rebased).
   All four use `--force-with-lease` (never bare `--force`) and never inherit from `git config`.

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
  directory (resolved via `git rev-parse --path-format=absolute --git-common-dir`). A bare-repo
  layout (`<container>/.bare` + `<container>/worktrees/`) is also recognized: the container is the
  parent of `.bare/`.

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
  "scratch_setup": [
    "git fetch origin main:main"
  ],
  "scratch_env_strip": ["COMPOSE_PROJECT_NAME", "DATABASE_URL"],
  "scratch_env": {},
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
- **`cleanup`** (optional, boolean, **ignored**): Accepted for backward compatibility with configs written before #232; the queue never runs cleanup. Worktree cleanup after a queued merge is owned by `/merge-and-cleanup`'s route (#232).
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
- **`scratch_env_strip`** (optional, list of strings, default `["COMPOSE_PROJECT_NAME", "DATABASE_URL"]`): Environment variables removed — not replaced — from steps run in the scratch checkout (`scratch_setup` and the base-verification gate). These are per-worktree direnv values that would otherwise point the scratch run at the invoking worktree's Compose project or database. Steps run in the PR worktree itself inherit the process environment unchanged. A scratch step that needs a Compose project or database must set its own (`docker compose -p`, a top-level `name:`, an inline `VAR=value cmd`, or `scratch_env`).
- **`scratch_env`** (optional, object of string → string, default `{}`): Variables laid on top of the scratch step environment after `scratch_env_strip` is applied. Opt-in only; the queue never injects a `COMPOSE_PROJECT_NAME` by itself.
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
- **`/merge-and-cleanup` routing:** When a PR targets a merge queue's base branch, the command routes
  it through `/queued-merge` (blocking while the queue drains) and runs `/cleanup` once GitHub reports
  it MERGED. Base-scoped routing allows stacked PRs targeting other branches to merge normally. The CLI
  hardens against shadowing by running in the claude-helpers checkout. See Amendment below.
- **Queue-proof skip:** Post-merge validation skips when the landed tree is provably identical to the
  queue-tested tree AND the queue's steps are a superset of the repo-cache checks. The skip requires
  tree-equality fingerprinting and queue-steps-superset validation. The `--queue-started-at` argument
  hand-offs the queue timestamp to cleanup for freshness checking. Fingerprint covers tracked files only
  (residual blind spot documented in the amendment).

## Amendment: `/merge-and-cleanup` enforcement and routing (#229, superseded by #232)

**Superseded in part by #232 (2026-10-01):** enforcement is now routing, not refusal.

The original `/merge-and-cleanup` command refused PRs that target a merge queue's configured base
branch. When a queue config resolves and the PR's base equals the queue base, the command exits
with code 3 and instructs the user to use `/queued-merge` instead. This refusal was written by #229
to prevent accidental out-of-order merges. #232 advances this to active routing: a queue-owned PR
is now merged via `/queued-merge` and then cleaned up automatically with `/cleanup`, all in one
command invocation, instead of refusing and requiring manual dispatch.

**Enforcement point and routing predicate:** A typed `detect_merge_queue()` function in `merge.py` runs a live check against
the queue resolver (`resolve_config_path()`), re-run inside `apply_merge()` before the lock to catch
PRs retargeted after the plan phase. No cache is stored; detection happens fresh on each run. When
the predicate `.queue.decision=="refuse" and .queue.detection.state=="configured" and .queue.pr_base!=null and .queue.pr_base==.queue.queue_base`
is true, the PR routes through `/queued-merge` and Phase 4-Q cleanup; otherwise, refusal branch defaults (unknown state, base undetermined) are unchanged.

**Refusal cases (unchanged):**
- `unknown` state: queue configuration state cannot be determined. The message names the reason and points to the recovery path (fix the config, retry).
- Base undetermined: PR base or queue base could not be derived. Both error cases fail closed and refuse with a message pointing to the recovery.

**Configured but invalid config (known behavior change):** A configured-but-invalid `merge-queue.json` (bad keys, empty steps, unreadable) surfaces downstream as `merge-queue enqueue` returning a config error (exit 3) with **no result file written** (`merge_queue.py:2474-2479`). Phase 4-Q reports this as "no fresh queue result" rather than the upstream config-error message. This fails closed (no cleanup happens), but the user loses the specific config-error message in favor of a generic "no fresh result" diagnostic. This is a known, accepted behavior change from #229's immediate refusal.

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
`cli merge queue-init` (`{base: <default branch>, steps: [<repo-cache check | just check>]}`; never `just merge`, which may merge on its own), is validated with the queue's own
`validate_config_data()`, and is written create-only so an existing config is never overwritten.
After a successful create, the PR is handed to `/queued-merge` and the same route-and-cleanup tail applies (#232).
Declining merges as before. A non-`worktrees/` layout gets no offer.

**Base scoping (Q2):** Routing (and the original refusal) is conditional on `PR.base == queue.base`. Stacked PRs that target
a parent branch (or any non-base branch) fall through to the normal merge gate, so stacked workflows
are not disrupted. The base is re-read live in `apply_merge()` to catch retargets.

**Threat model:** This enforcement/routing removes the accidental-bypass path entirely, because a queue-owned PR is never merged outside the queue. A queue-owned PR is now merged via the route, not refused. The deliberate-bypass text stays: this enforcement stops *accidental* bypass by our own tooling (a developer
running `/merge-and-cleanup` when they should run `/queued-merge`). It does not stop deliberate
bypass (editing the PR, running raw `gh pr merge`, exporting a dead-path env var, moving the
config file). Those are detected afterwards by ADR-0021's Bypass Limit metric (untrailered commit
count), and server-side rulesets remain the only layer that enforces the queue universally.
Detection sees env + default only, not another command's `--config` flag.

**No bypass flag (Q4):** If the queue is broken or misconfigured, the recovery path is:
1. Fix the config or env var, then retry `/merge-and-cleanup`.
2. If the queue is unrepairable, manually run `gh pr merge` outside the tooling. ADR-0021's
   Bypass Limit metric (`git log --grep=Merge-Gate <default-branch>`) records the untrailered commit.

A queue-owned PR is now merged via the route, not refused, so `/merge-and-cleanup` handles the route invocation.

**CLI hardening (Q5):** Both `/merge-and-cleanup`'s CLI calls run with the claude-helpers checkout
as the process `cwd`, so a PR worktree's `scripts/workflow/cli.py` cannot shadow the guard. The
`merge plan` command takes a `--cwd` flag to specify the caller's directory for auto-detect and
absolute-path resolution.

**Residual blocking (Q7):** A routed invocation blocks for as long as the queue takes to drain. The `/merge-and-cleanup` invocation may wait behind other PRs already in the queue.

## Amendment: Post-merge validation env isolation (#241)

**Status:** Accepted (2026-10-06). The queue-proven skip removes one safety net.

Post-merge validation (`/cleanup` when a PR is merged) isolates the check environment using an
**allowlist** of known variables (`PATH`, `HOME`, `USER`, `SHELL`, `TERM`, `LANG`, `TMPDIR`,
`NVM_DIR`, `VOLTA_HOME`, `LC_*`) plus any variables exported by the main worktree's `.envrc`
(via `direnv export`). All other variables are dropped. This is different from the queue's own
scratch steps, which use a **denylist** (`SCRATCH_ENV_STRIPPED_VARS`): the queue's steps run
under an unscrubbed PR-worktree environment with only a few unsafe vars removed, while post-merge
validation must fail closed on unknown per-worktree vars that might poison the regression gate.

When a queue-proven skip is taken (the landed tree is identical to the queue-tested tree), the
skip inherits the queue gate's unscrubbed PR-worktree environment — validation is not re-run, so
the environment isolation applied to the queue's steps does not carry through to the skip decision.
This is the intended trade-off: the skip removes one safety net (revalidation under the isolated
env) in exchange for avoiding redundant checks after a proved merge.

A `direnv` that cannot be re-derived (blocked, missing, or times out) gives an `inconclusive`
verdict for Compose repos and a plain scrubbed run with a diagnostic note for others. The
allowlist never overrides a configured repo's own `.envrc`: variables it exports are applied on
top of the base allowlist.

## Amendment: Queue-proof skip criteria and --queue-started-at hand-off

**Status:** Accepted (2026-10-08).

Post-merge `/cleanup` can skip validation when the landed tree is provably identical to the
queue-tested tree. The skip decision (returning `VALIDATION=skipped` with a `Proved` verdict)
requires **both** conditions:

1. **Tree equality:** The tree at `origin/<base>` (freshly fetched) is identical to the
   tree that passed the queue gate (compared by git tree SHA, which covers tracked content only).
   This is the primary proof of equivalence.

2. **Queue-steps superset:** The queue gate's configured `steps` must be a superset of the
   checks read from `.claude/repo-cache.json` in the main worktree. That is, every check command
   the PR is validated against in post-merge validation must have already been run (and passed)
   by the queue. This ensures that skipping validation does not dodge a new check that was added
   between enqueue and cleanup.

**`--queue-started-at` hand-off:** The queue records the wall-clock Unix timestamp when the PR
entered the queue and passes it to `/cleanup` via the `--queue-started-at` command-line argument.
Cleanup passes it to the queue-result reader (`read_pr_result`) as a timestamp gate, so only a
queue result recorded for this queue run is accepted as proof. There is no separate check of
`repo-cache.json` file times; the steps-superset condition above is what covers checks added
after the PR was queued.

**Residual blind spot (F16):** The tree-equality proof covers tracked content only. Untracked or
`.gitignore`d files are not part of it, so a PR whose tracked tree matched the queue-tested tree is
skipped even if ignored artifacts differ. This is accepted: ignored files should not affect
correctness, and detecting them would require the queue to snapshot the full working tree. (This is
distinct from the post-merge dirty-main fingerprint in `git.py`, which guards against a validation
run mutating main.) There is currently no opt-out env var.

**Built vs. Planned:** Queue-proof skip is now built and working. The skip compares fingerprints,
checks the steps-superset condition, and gates the queue result on `--queue-started-at`. Cleanup
returns `VALIDATION=skipped` (a passing verdict) when both conditions are met and the skip is
taken; otherwise, it proceeds with validation as usual.

**Status:** Accepted (2026-10-06). The queue-proven skip removes one safety net.

Post-merge validation (`/cleanup` when a PR is merged) isolates the check environment using an
**allowlist** of known variables (`PATH`, `HOME`, `USER`, `SHELL`, `TERM`, `LANG`, `TMPDIR`,
`NVM_DIR`, `VOLTA_HOME`, `LC_*`) plus any variables exported by the main worktree's `.envrc`
(via `direnv export`). All other variables are dropped. This is different from the queue's own
scratch steps, which use a **denylist** (`SCRATCH_ENV_STRIPPED_VARS`): the queue's steps run
under an unscrubbed PR-worktree environment with only a few unsafe vars removed, while post-merge
validation must fail closed on unknown per-worktree vars that might poison the regression gate.

When a queue-proven skip is taken (the landed tree is identical to the queue-tested tree), the
skip inherits the queue gate's unscrubbed PR-worktree environment — validation is not re-run, so
the environment isolation applied to the queue's steps does not carry through to the skip decision.
This is the intended trade-off: the skip removes one safety net (revalidation under the isolated
env) in exchange for avoiding redundant checks after a proved merge.

A `direnv` that cannot be re-derived (blocked, missing, or times out) gives an `inconclusive`
verdict for Compose repos and a plain scrubbed run with a diagnostic note for others. The
allowlist never overrides a configured repo's own `.envrc`: variables it exports are applied on
top of the base allowlist.

## Amendment: Phase 6 rebase skip when base is already an ancestor (#420)

**Status:** Accepted (2026-11-02). Phase 6 skips `git rebase` when `origin/<base>` is already an ancestor of HEAD, logs a stderr line and a `rebase_skipped` queue event, and goes straight to the gate. On the skip path the rebase result and the pre-rebase tip are the same commit, so Decision 5's tested-commit recovery applies unchanged (in code, `_restore_worktree_to_orig_head` resets to `orig_head` after a post-rebase kickback, which on the skip path is the tested commit; on the replaying path it is the pre-rebase tip), and `tested_sha == orig_head == lease_sha` makes the audited `force_push_tested` push in Phase 8 a no-op (the same state an already-current linear branch reaches; it remains the only push site). This matches Decision 3, because the gate still runs on the base plus the PR, and HEAD's tree equals base plus the PR's net diff, the same tree the squash lands. The Phase 9 base-unchanged re-check stays mandatory as the backstop. A branch that merged an older base still takes the replaying rebase (tracked as a follow-up, #431).

## Amendment: Full-only off-queue merges with base-ancestor precondition on cached hits (#387)

**Status:** Accepted (2026-11-05, #387). Off-queue merges (plain `/merge-and-cleanup` route, not through `/queued-merge`) require a full-mode check gate only (D2): `merge.py::_run_merge_gate_checks` accepts a full-mode record and never a minimum-mode record. A full-mode cached record on the target tree is a valid hit only if a fresh `git fetch` of the base succeeds and `git merge-base --is-ancestor origin/<base> HEAD` succeeds (CP-W5); otherwise, or if the fetch fails, the full check runs. The ancestry check is a consumer precondition, not a cache-key field: both modes stay cached (Q2 = A). The gate resolves the full command through the same chain as `checks resolve-check` (repo-cache, with a package.json-derived value re-derived from the worktree, then project.yaml, then the justfile), so a repo whose only check is a justfile recipe is gated, and the repo-cache early returns are removed (the #387 regression). The `just merge` justfile recipe (Path 1) is not a declared exception: until that is decided, `apply_merge` runs the same full-only gate, with the precondition, before Path 1, and a non-pass refuses the merge before `just merge` runs. This is the stricter default, and it changes only in repos that define `just merge`. Queue-routing detection stays a fresh live check (2026-10-01 amendment), with no cache. The forged-receipt backstop now rests on the full gate at merge, on sanctioned routes only: a direct `gh pr merge` bypasses it.

**Amendment (#462, unit 3a).** The merge gate runs `checks validate` with the main worktree, so the `command` and `timeout_secs` it reports are the values that ran, and it fails closed: when no full check resolves but cached commands exist, the merge is refused with "no full check resolved, but cached commands exist" (a repo with no cached commands still merges with the no-gate marker).

**Live-at-default change L0 (`worktree_tree`).** `checks.py`'s `worktree_tree` resolves the git dir with `git rev-parse --git-dir`, so linked worktrees (where `.git` is a file) get a real tree. A failure to write the tree logs "idempotency disabled" and never counts as a check failure. Before this fix, receipts from linked worktrees carried `tree: ""`. Cache hits therefore begin for existing callers on the first merge after this change, not only for the new modes. Rollback unit: the `checks.py` change alone.

**Cross-references.** #431 (a branch that merged an older base still takes the replaying rebase in Phase 6) is unchanged by this amendment. #447 was delivered by #460 (`e593c96a`) and is resolved; this amendment adds no work under it.

## Amendment: Queue PR gate reuses a tree-keyed full check record (#491)

**Status:** Accepted (2026-11-12, #491). Phase 7 of the queue may skip the gate steps when a passing full-mode check record already covers the exact tree, command and toolchain the gate would test. Reuse is on by default through one code path. The decision lives in `scripts/workflow/gate_reuse.py` (`decide_gate_reuse`, returning a closed `Reuse | Miss` type), and `merge_queue.py` Phase 7 calls it. A miss, and any exception inside the decision, runs the gate exactly as before.

**Consumer precondition.** The queue is a new consumer of tree-keyed check records. A record satisfies the queue only when all of these hold: it is full-mode and passing; its tree equals the worktree's tree, which equals `tested_sha^{tree}`; its `full_digest` matches the digest the queue recomputes with `checks.expected_full_key` (the resolved full command and its source, the repo-cache commands, the run timeout, and the toolchain/lockfile digest defined in ADR-0013 Amendment 14); and a freshly fetched `origin/<base>` equals the base Phase 4 pinned and is an ancestor of `tested_sha`. As in #387, the ancestry check is a consumer precondition, not a key field. The queue never trusts a record's own claims: it re-derives the expected key with the same `checks.py` code that `checks validate` uses.

**Decision 3 note.** The gate still covers the base plus the PR. Reuse is sound because the tested tree is exactly that content, so a record written for that tree proves the same gate. A branch that replays a rebase (Phase 6 did not skip) changes the tree, so its lookup misses and the steps run. This amendment neither closes nor pre-empts #431 (the older-base replaying rebase).

**The six guards.** Every guard is unconditional, and no config can relax them. They run in this order, and the first failure wins:

1. **Kill switch.** `gate_reuse_disabled` in `merge-queue.json` forces a miss for any value other than the exact boolean `false` (absent counts as `false`), so a malformed value such as `"yes"` also misses. The kill switch can only force a miss. It is not a second path: a miss always runs the gate steps.
2. **Single step.** The queue must have exactly one step, checked against the raw config count with no entries dropped while loading (`steps_dropped == 0`). Multi-step queues always run their steps.
3. **Command.** The step's `cmd` must be byte-equal to the resolved full check command, with no normalisation, and its `timeout_secs` must equal the effective step timeout (through the digest's run-timeout input).
4. **Tracked-state refusal.** `.claude/repo-cache.json`, `.claude/project.yaml` and any path under `.claude/check-record*` must not be tracked at `tested_sha`. The check reads the tested commit through `git ls-tree`, not ignore rules, so a PR that force-adds a forged passing record, or a tracked config, is refused.
5. **Clean tree and tree equality.** The PR worktree must be clean, and `worktree_tree == tested_sha^{tree}`. Both are checked after the rebase-or-skip decision and again immediately before the force-push (W2).
6. **Fail closed.** Any exception is a miss (`error`), never a skip. A fetch failure of `origin/<base>` is `stale-base`. A symlinked records directory or record file is `records-unsafe`, which is a miss.

The reasons are the closed `MissReason` set in `gate_reuse.py`: `disabled`, `steps-unmappable`, `command-mismatch`, `tracked-state`, `dirty-tree`, `tree-mismatch`, `stale-base`, `digest-uncomputable`, `records-unsafe`, `record-unparseable`, `minimum-record-only`, `digest-mismatch`, `no-record`, `audit-write-failed` and `error`. The tests cover one case per reason, including a PR that ships a forged tracked record.

**Audit before skip.** On `Reuse`, the queue appends a `gate_reused` event to `queue.jsonl` (record name, `written_at`, tree, `full_digest`, `tested_sha`) and prints a stderr line before the step loop is skipped. If that append raises, the decision becomes `miss:audit-write-failed` and the steps run. Phase 8 (push), Phase 9 (base unchanged) and the post-gate HEAD and clean-tree assertions run on both paths. On the reuse path the queue also re-checks `HEAD^{tree}` against the record's tree before pushing; a failure kicks back and never pushes. On a miss, a `gate_reuse_miss` event is appended best-effort; if that append fails, the reason goes to stderr and the steps still run.

**Force-push carve-out and the `Merge-Gate:` trailer (supersedes the earlier plan ruling that the trailer is unchanged).** A gate satisfied by a tree-keyed full-mode record counts as "full gate passed" for the force-push carve-out (Decision 6). A reused gate is marked in the trailer: when a record satisfied the gate, the queue writes `Merge-Gate: merge-queue tested-base=<40hex> tested-head=<40hex> gate=reused`. A gate that ran its steps writes the unchanged trailer. `parse_trailer` returns `(tested_base, tested_head)` for both forms, and `trailer_gate_reused` reads the marker alone. Provenance also lives in `result-<pr>.json` (`gate_reuse`, `gate_reuse_record`, `gate_reuse_tree`, `gate_reuse_digest`, `gate_reuse_full_record_seen`, `gate_reuse_decided_at`) and in `queue.jsonl`.

**Records are timeless (no age bound).** A passing record carries `written_at`, but the queue never compares it with the clock. The guards prove that tree, command, toolchain digest and base ancestry match. They do not prove that a check depending on date, network, credentials or gitignored files would still pass today, so a non-hermetic check can pass stale. This is accepted: the controls are the hit-rate telemetry (`merge_queue status`) and the kill switch. A `gate_reuse_max_age_secs` bound with a `record-too-old` reason is the option if telemetry shows drift; it is not built.

**Toolchain digest limits.** The digest (ADR-0013 Amendment 14) covers the lockfiles, the deps stamp, and the `python3` and `node` versions. It omits ruff, mypy, just and shellcheck. A check that runs those tools is keyed without their versions, so an upgrade to one of them alone can leave a stale record matching.

**`_main_worktree_of` limitation.** Phase 7 resolves the main worktree through the first-entry `_main_worktree_of` rule in `merge_queue.py`. The primary-checkout work in #472 and #473 removes that rule. Until it lands, this amendment depends on the first-entry rule, and the #472/#473 change must reuse the primary resolver at that call site.

**In-flight overlap.** #476 touches the same Phase 7 signature and `_locked_flow`. This amendment does not set the merge order between #476 and #491.

**Hit sources.** Only `checks validate` callers write tree-keyed records:

- **Shipit** (`commands/shipit.md`, `checks validate --mode minimum --worktree "$(pwd)"`, no `--main-worktree`). It writes a fallback `FullKey` only when `check_minimum` does not resolve from the worktree's own files. That record is on the shipped tree, so it is the one realistic hit source: the queue reuses it when the branch already contains the base and nothing was committed after shipit.
- **Implement and fix** (`commands/implement-with-haiku.md`, `checks validate --mode minimum --main-worktree …`). It writes a fallback `FullKey` only when `check_minimum` is undeclared for that checkout. Later commits usually change the tree, so these rarely match.
- **The off-queue merge gate** (`merge.py::_run_merge_gate_checks`, `checks validate --mode full`). It rarely precedes a queue run of the same tree.
- **A manual `checks validate --mode full`.**

The `/expert-flow` verify step writes no record in either mode. It resolves the command with `checks resolve-check --mode minimum` and a subagent runs the raw command; it never calls `checks validate`. ADR-0013 Amendment 12 (#462, 2026-11-05) says that verify reads the record cache "through `checks validate`", and that wording does not match `commands/expert-flow.md`; this amendment does not rely on it. A minimum record never satisfies the queue's `FullKey` request, so a hit needs a passing full-mode record on the same tree and digest.

**Payoff.** Only single-step queues whose `cmd` byte-equals the resolved check can benefit (1 of 13 configured repos on the machine where this was measured). The queue's `timeout_secs` must equal the timeout the writing `validate` used (`checkTimeoutSecs`), or every lookup is `digest-mismatch`. Where `check_minimum` resolves in every checkout, reuse is expected to be near zero, because shipit and implement/fix then write only minimum records. In a checkout where `check_minimum` does not resolve for shipit, reuse removes the queue's repeat run of shipit's full run but not verify's run, so that checkout drops from three full runs to two, not one.

**Residuals.**

- Records live in the PR worktree, so a flow agent with `Write` can produce one. This is inherited from the #387 off-queue gate and extended to the queue. The tracked-state guard refuses committed records; an untracked record an agent writes is the remaining exposure. The gate already runs PR-authored code unsandboxed, so this is accident protection, not adversary-proofing.
- `validate` exports `CHECK_BASE` and `CHECK_CHANGED_FILES`, and Phase 7 does not. A full check that reads them could behave differently under reuse. This repo's `just check` does not read them; only `scripts/check-minimum.py` does.
- Installed tools outside the toolchain digest (for example user-site `mypy` or `ruff`) are not part of the key.
- Reuse removes the queue's repeat run of a flaky test, so a test that failed in shipit and passed on re-run gets no second chance at the queue.

**`check_minimum` documentation (W3).** Declaring `commands.check_minimum` stops `--mode minimum` from falling back to the full command. This is documented here only; there is no runtime fallback note. The declaration lives in the local, gitignored `.claude/project.yaml`. That file is per-machine state and is not part of any PR diff. It was missing on the machine where this was measured until this change, which is why every `--mode minimum` caller fell back to full. The reach rules are:

- Verify, implement and fix pass `--main-worktree`, so they reach main's `project.yaml` through `resolve_check_detail`'s main-worktree branch.
- Shipit and the stacked-child validates (`commands/cleanup.md`, `prompts/worktree-reference.md`) pass no `--main-worktree` and read only the worktree's own `project.yaml`. Each worktree therefore needs its own copy. New worktrees get one when `project.yaml` is seeded from main at creation.
- After `check_minimum` is declared, shipit and implement/fix write `MinimumKey` records, which never satisfy the queue.

Passing `--main-worktree` from shipit and the stacked-child validates is a separate, small follow-up. Until it lands, the per-worktree copies are the mechanism. Another machine or fork needs its own copies.

**Measurement and rollback.** All actions below are human actions; there is no auto-remediation. The result file and `merge_queue status` report two signals: whether a full record existed for the tested tree (opportunity, `gate_reuse_full_record_seen`), and what happened when one did (correctness: the `reused` count and the guard-refusal and unexpected buckets). `merge_queue status --since <ISO-8601>` restricts the counts to decisions at or after a time. The rules are:

- **Correctness rule (applies from the first merge, transition included).** Every run with `gate_reuse_full_record_seen: true` must end `reused`, or be refused by a guard whose reason explains it (`stale-base`, `dirty-tree`, `disabled`, `steps-unmappable`, `command-mismatch`, `tracked-state`, or a `digest-mismatch` where a human can name the input that changed). Any such run in the unexpected bucket (`error`, `digest-uncomputable`, `records-unsafe`, `tree-mismatch`, `record-unparseable`, `audit-write-failed`) means: set `gate_reuse_disabled: true` in `merge-queue.json` and open an issue to find the fault. The fix is a repair, not removal.
- **Safety trigger.** If a merge whose result shows `gate_reuse: reused` is followed by a failing base verification of that merged commit, set the kill switch at once and open an issue.
- **Opportunity rule: a removal schedule.** After the first 20 post-cutoff runs that carry a `gate_reuse` value, read `merge_queue status --since <transition cutoff>`. If fewer than 2 of those 20 had a full record for the tested tree, reuse has no hit source in this repo. Set the kill switch and open an issue to remove the queue-side reuse code, and quote any `minimum-record-only` count in that issue. This rule is expected to trip once `check_minimum` resolves in every checkout, because shipit and implement/fix then write only minimum records. Tripping it is a scheduled removal ("nothing to reuse here"), not evidence that reuse is broken, and it is an acceptable outcome. If opportunity is 2 or more and the correctness rule is clean, reuse stays.
- **Rollback scope.** Rollback removes `gate_reuse.py`, the Phase 7 hunk, the config keys, the result and status fields, and this amendment (marked superseded). It does not revert the toolchain/lockfile digest in ADR-0013 Amendment 14, which is a separate change to the shared record cache and stands on its own. Reverting it would need its own amendment.
- **Transition cutoff:** to be recorded when the per-worktree project.yaml copies are complete. Runs before the cutoff count for the correctness rule and the safety trigger, but not for the opportunity rule.

## Follow-ups

- **GitHub ruleset:** after two clean weeks of local queuing, add a GitHub ruleset requiring the
  `Merge-Gate:` trailer and `tested-base` ancestry to prevent out-of-order merges via the GitHub UI.
- **Retire or shrink merge.py:** the existing `merge.py` (O_EXCL lock, 1800s timeout, `capture_output`)
  provides non-serialized merging for single-developer workflows and testing; the queue is the
  serialized production path. The `/merge-and-cleanup` enforcement amendment adds a queue guard
  (`detect_merge_queue()` / `queue_guard()`) to `merge.py` that signals when a PR targets the queue's
  base. The guard itself does not refuse the PR; instead, `/merge-and-cleanup.md` (command-level
  routing in Phase 1) intercepts the guard decision and hands the PR to `/queued-merge` for
  serialized merging. The non-queue merge path (PRs targeting other bases) is unchanged.
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

**Amendment (#462, 2026-11-05): force-push gates vs the merge gate.** The authorized force-pushes in Context
(`/stack-sync`, `/shipit`) are gated at PR level by the minimum check (`checks validate --mode minimum`), per
the ADR-0012 and ADR-0023 2026-11-05 amendments. The merge gate, on the queue and off-queue routes, runs the
full check and is the authoritative gate; a minimum pass never satisfies it. The four-command list above is
unchanged, and so is the queue's own pinned push, which still runs only after the full gate passes.
