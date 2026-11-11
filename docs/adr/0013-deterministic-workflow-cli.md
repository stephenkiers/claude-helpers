# ADR-0013: Deterministic Workflow CLI

**Status:** Accepted

## Context

Three slash commands — `/track-and-start`, `/shipit`, and `/cleanup` — currently interpret approximately 2,000 lines of deterministic git/GitHub/cache/check/worktree/stack instructions as prose embedded in Markdown and markdown-wrapped scripts. This prose-based approach:

1. Requires model interpretation of every invocation (high token cost)
2. Makes it difficult to test edge cases without running the full command
3. Duplicates logic across commands and copies
4. Makes it harder to reason about failure modes and recovery

These three commands represent only ~8% of measured usage but consume a significant fraction of modeling cost. Extracting the deterministic part into a tested, provider-neutral Python CLI should reduce token and round-trip costs.

The approach preserves the current semantics:
- The wrapper (Markdown command + model) retains semantic judgment, duplicate/ambiguity resolution, and confirmation of unsafe actions.
- The CLI explicitly signals unknown/unrecognized states (fail-closed, Decision 8) rather than guessing, allowing the wrapper to pause and ask.
- Existing commands continue to call git/gh directly for mutations until Phases 2-4 migrate those operations into the CLI.

## Decision

**Build a provider-neutral Python CLI in `scripts/workflow/`** with these properties:

1. **Stdlib only** (Decision 6): dataclasses, json, subprocess, pathlib, hashlib, re, typing.Protocol — no third-party dependencies beyond what Python provides.

2. **Fail-closed unknown detection** (Decision 8): Every operation layer (`inspect`/`plan`/`apply`) that encounters an unrecognized state returns an explicit `Unknown(reason: str)` result rather than guessing or defaulting. The wrapper receives this signal and decides whether to ask the user or resolve it itself.

3. **Cache locking and atomic writes** (Decision 1): Read-only in Phase 1; writes in Phase 2+ use temp-file + `os.replace()` atomic rename plus sidecar lock file to serialize concurrent writers. All failures return a categorized, structured result so the wrapper can retry.

4. **Stale-plan detection** (Decision 2): `apply` rejects a mutation plan if any of {expected HEAD SHA, cache content hash, branch name} differs from current repo state, forcing re-planning.

5. **Verbatim ADR-0010/0011 port** (Decision 4): `stack.py` and `worktrees.py` port the layout/stack detection logic **verbatim** from `prompts/worktree-reference.md` — no reinterpretation, no improvements, exactly as specified. Fail-closed to `unknown` layout detection exactly per ADR-0011.

6. **Provider protocol** (Sam System, ADR-0005): `providers/base.py` defines a `Provider` Protocol; both `github.py` and `local.py` implement the same contract so `cli.py` never special-cases them (stub implementations Phase 1; full implementations Phases 2-3).

7. **No per-project extension point** (Decision 3): The CLI is not designed for runtime configuration. Forks that need different behavior edit `scripts/workflow/` directly — that IS the extension mechanism, consistent with the repo's fork-and-adapt philosophy. *Amended by Amendment 10 (2026-11-01): the CLI now reads and writes the main worktree's config and runs bounded install subprocesses.*

## Consequences

### Scope included in Phase 1 (read-only primitives)

- `models.py`: Schemas for `.claude/repo-cache.json`, `.claude/github-cache.json`, and local `issues.json` with `schema_version` and hand-rolled validation.
- `git.py`: Centralized git/gh subprocess wrapper using argument arrays (never `shell=True`), with timeouts.
- `worktrees.py`: Worktree parent detection (ADR-0010), main/second worktree discovery.
- `project.py`: Repository identity (gh repo view), local-plan-mode detection, current user.
- `cache.py`: Read and validate cache files (phase 1); write/locking stubs citing Decision 1.
- `checks.py`: Toolchain and check detection (read-only; execution in Phase 2+). The read-only scope of Decision 3 is amended by Amendment 10 (2026-11-01) for the check-answer write and the deps chore.
- `stack.py`: Stack detection (ADR-0011): `is_stacked()` (cache-first, ancestor fallback), `detect_layout()` (single-driver | per-branch | unknown).
- `safety.py`: Shared `Unknown(reason: str)` type for fail-closed behavior.
- `providers/base.py`: Read-only provider contract (`Provider` Protocol).
- Test suite: Unit tests for all modules using existing `_test_harness.py` conventions.

### Scope excluded in Phase 1 (Phases 2+)

- `cli.py`: Not built Phase 1; the wrapper commands still call deterministic logic via prose.
- `providers/github.py` and `providers/local.py`: Stub implementations or omitted; full implementations Phases 2-3.
- Cache writes and locking (`cache.py` write side): Phase 2+ when mutations begin.
- Check execution and output capture (`checks.py` execute side): Phase 2+.
- Any mutation operations: All mutations stay in commands/.md until Phases 2-4 migrate them.

### Phase-1 invariants

- **Every read operation is deterministic**: Given identical repo state, the same inputs produce the same output every time.
- **No side effects**: Phase 1 modules only read files, call git/gh for inspection, and return results; they never mutate.
- **Unknown state is explicit**: Any module that cannot determine an answer returns `Unknown(reason)` rather than defaulting or guessing.
- **Fail-closed layout detection**: Stack layout detection follows ADR-0011 exactly — `unknown` when ambiguous, never a best-effort guess.
- **No per-worktree state assumed**: Primitives work across multiple worktrees; layout detection is structural (worktree-list + cache), not implicit.

### Relationship to ADR-0010 and ADR-0011

- **ADR-0010** (worktree clone layout): `worktrees.py` ports its detection logic verbatim.
  - Main worktree is the first entry from `git worktree list --porcelain`.
  - Worktree parent: if a second worktree exists, use its parent; else if main is under `worktrees/`, use that; else create `worktrees/` as sibling to main.
  - Project root: if main is under `worktrees/`, use its grandparent; else use main itself.

- **ADR-0011** (stacked-PR layout model): `stack.py` ports its detection logic verbatim.
  - `is_stacked()`: Cache-first (`.claude/github-cache.json` `.stack.isStacked`); falls back to ancestor search among worktree branches.
  - `detect_layout()`: Structural, not commit-ancestry. Per-branch if any sibling worktree is checked out for this stack's parent or a child. Single-driver if stacked but no siblings. Unknown if stacked but parent/sibling info unresolvable.
  - Fail-closed to `unknown` exactly as ADR-0011 specifies — never guess a layout.

### Centralized subprocess wrapper

All `git` and `gh` invocations go through `git.py` functions that:
- Use argument arrays only (never shell=True or string interpolation).
- Include timeouts (default 30s, configurable).
- Raise `subprocess.CalledProcessError` on failure (default) or return empty string if `check=False`.

This satisfies CLAUDE.md's documented shell-injection concern and makes it easy to audit where and how external processes are invoked.

### Failure as structured data

Every module returns explicit results:
- `read_github_cache(path) -> (GitHubCacheData or None, Unknown error or None)`
- `detect_layout(...) -> "single-driver" | "per-branch" | "unknown"`
- `detect_worktree_parent(...) -> str` (empty if unknown)

Callers can inspect the result and decide how to respond; the CLI never returns a bare exit code or silent default.

### Forward compatibility

Phase 2+ will wire Phases 1's read-only primitives into mutation planning:
- `plan` operations read repo state via Phase 1 modules and emit a JSON plan with resolved paths, repo identity, branch, expected HEAD, cache hashes, and intended operations.
- `apply` uses Phase 1 modules to validate the plan (stale-plan detection per Decision 2) before executing.
- GitHub operations route through a provider adapter (`providers.github.ProviderImpl`) that wraps the `gh` CLI.
- Local-plan-mode repos route through `providers.local.ProviderImpl` for issue allocation and worktree naming.

## Reference Implementation Notes

- **Stack detection algorithm** (from ADR-0011): Find the tightest (fewest-commits) ancestor branch among other worktrees' branches, excluding the default branch and current branch. Use this ancestor's open PR number if available.
- **Layout detection algorithm** (from ADR-0011): Build a branch → worktree map. Check if any OTHER worktree has the subject's parent checked out (per-branch), or if any OTHER worktree's cache marks the subject as their parent (per-branch), or if the subject has a known parent but no siblings (single-driver), or else unknown.
- **Worktree parent detection** (from ADR-0010): Deterministic three-way fallback ensures the worktree parent is always discoverable without user input or config.

## ADR-0013 Amendments

### Amendment 1: Phase 2 scope and implementation details

**Scope expanded:** `/merge-and-cleanup` is included in the deterministic workflow CLI alongside `/cleanup` (not originally in ADR text).

**I/O contract (wrapper-CLI boundary):** All CLI operations output JSON to stdout via `json.dumps(dataclasses.asdict(...))`. The `.md` wrapper commands extract fields via `jq` invocations (one variable per `--arg`/`--argjson`, never interpolating a shell variable into a `--argjson` string literal — per CLAUDE.md's documented shell-injection safety constraint).

**Forward compatibility implementation:** The plan/apply pattern realizes the "Forward compatibility" section (Consequences): `plan_*` reads repo state via Phase 1 primitives and returns a JSON plan with resolved state, freshness triple, and intended operations. `apply_*` validates freshness (Decision 2: all three of {expected HEAD SHA, cache content hash, branch name} must match current repo state or plan is rejected), then executes mutations. This separates read-only inspection from destructive operations, enabling safe re-planning if state changes.

**Mutation allowlist mechanism:** `scripts/workflow/mutations.py` implements the centralized mutation funnel — `check_mutation_allowed(args)` rejects anything not in the exact-shape allowlist, replacing the Bash-tool-allowlist enforcement the `.md` docs previously relied on. The allowlist is data-driven (dict of subcommand → permitted argument shapes), and every mutation function in `git.py` must route through the funnel before invoking the underlying command — the git mutations (`remove_worktree`, `delete_branch`, `pull_ff_only`) before calling `run_git_command`, and the one `gh` mutation (`pr_merge_squash`, backing `gh pr merge --squash`) before calling `run_gh_command`. The allowlist accordingly has four subcommand keys: `worktree`, `branch`, `pull`, and `pr`.

### Amendment 2: /shipit golden-path scope

**Scope expanded:** The /shipit command's check execution, commit/push/PR-mechanics are migrated into `shipit.py` (deterministic portions only — PR title/body authoring stays prose, stacked push orchestration stays delegated to `/stack-sync`).

**Explicit scope boundary (golden path only):**
- **In scope:** check execution via `run_checks()` (format → check → parallelizable → build, stopping at first failure); `git add -A` + commit with message file; plain `git push -u origin <branch>` (never forced); mechanical `gh pr create`/`gh pr edit` invocation (title/body supplied by wrapper as a file, not authored by the CLI).
- **Out of scope permanently:** PR title/body authoring (stays model judgment in `.md`), recognized-vs-novel-content merge logic (stays prose), stacked push orchestration (delegated to `/stack-sync` via skill invocation), descendant sync (delegated to `/stack-sync`), force-with-lease (plain push only, no forced variants in allowlist).

**Check execution design (Decision 5):** `run_checks()` is a bare function (not plan/apply), non-destructive and idempotent. Cached check commands execute via `shell=True` (matches `execute_check`'s existing implementation — intentional for shell syntax support like `&&` and pipes). Trust boundary: `.claude/repo-cache.json` is repo-committer-controlled, not PR/attacker input. Returns `CheckResults` dataclass with ordered list of per-command results and `all_passed`/`failed_at` fields.

**Cache schema extended:** `RepoCacheData.parallelizable: List[str]` added (defaults to `[]` if absent in file), round-trips through `to_dict()`/`from_dict()` for freshn
ess tracking via cache hash.

**Mutation allowlist additions:** Three new git keys (`add`, `commit`, `push`) plus extended `pr` shapes:
- `"add": {("-A",): "git add -A"}` — no other shape.
- `"commit": {("-F", "<path>"): "git commit -F <path>"}` — message file only.
- `"push": {("-u", "<remote>", "<branch>"): "git push -u <remote> <branch>"}` — plain, non-forced only.
- `"pr"` extended with: `("create", "--title", "<title>", "--body-file", "<path>")`, `("create", "--title", "<title>", "--base", "<branch>", "--body-file", "<path>")` (stacked), `("edit", "<pr_number>", "--title", "<title>", "--body-file", "<path>")`.

**Plan/apply pattern:** `ShipitPlan` (freshness triple: branch, expected HEAD SHA, cache hash; commit message path; pr_number/pr_exists; stack info; base branch) and `apply_shipit()` (validates freshness, stages, commits, pushes, creates/edits PR, writes cache back). Mirrors `CleanupPlan`/`apply_cleanup` structure exactly.

### Amendment 3: Check gate coverage (supersedes Amendment 2's check-order line)

**Problem:** Amendment 2 specified `run_checks()` ordering as `format → check → parallelizable →
build` — but no documented procedure ever populated `parallelizable`, so any cache with only, say,
`commands.test` set produced zero executed checks and `all_passed: true`. The same gap let
`/merge-and-cleanup` merge a PR with `⚠️ merged with NO GATE` (it read only `commands.check`, no
`commands.test` fallback), and let `/cleanup`'s post-merge regression gate silently omit
`lint`/`typecheck`. See issue #116.

**Superseded:** Amendment 2's `format → check → parallelizable → build` ordering description. The
`parallelizable` field is retained on-disk (round-trips for cache-hash freshness) but is display
metadata only — the executor never reads it to decide what runs.

**New canonical order (`checks.py`'s `CHECK_ORDER`):**
`format → check → lint → typecheck → vet → test → build`, with `check` superseding `lint` and
`typecheck` when all three are set (`SUPERSEDED_BY_CHECK`), `install` excluded as not-a-check
(`NON_CHECK_COMMANDS`), and any other cached command key sorted deterministically between `test`
and `build`.

**Single source of truth:** `build_check_order(commands) -> (order, skipped)` is a pure function in
`checks.py`, reused by `run_checks()` (the `/shipit` gate), `merge.py`'s merge gate, and
`cleanup.py`'s post-merge regression gate — one ordering implementation instead of three
independently-drifting ones.

**Coverage assertion (the actual fix):** `run_checks()` asserts, before returning, that
`set(executed) | {s.command_type for s in skipped}` equals `set(commands.keys())` — the full set of
command keys present in the cache, null-valued ones included, since `build_check_order` records a
`null_command` skip entry for every present key, not just non-null ones. (An earlier version of this
assertion compared against only the non-null keys; that failed on any cache explicitly listing every
command type — the exact schema `commands/shipit.md` documents — since the null-valued keys' skip
entries then had nothing to match on the right-hand side. Fixed 2026-08-30.) A violation returns
`Unknown` rather than a silently-incomplete result — this is what makes a future silent skip
unwritable rather than merely fixed once. Zero eligible commands (empty or all-null `commands`)
returns `status="no_checks_ran"` and `all_passed=False`, so any legacy wrapper still gating on
`jq -r '.all_passed // false'` fails closed for free.

**Signature change:** `run_checks(commands, repo_root, timeout) -> Tuple[CheckResults, Optional[Unknown]]`,
decorated with `@fail_closed`, matching the `(result, Unknown)` convention used elsewhere in the
package. The `parallelizable` parameter is removed from the function signature (the field stays on
`RepoCacheData` for round-tripping only). This is a breaking change for any fork calling
`run_checks()` directly.

**CLI exit codes (`checks run`):** `0` passed, `1` a check failed, `2` nothing ran / coverage
violation. `--allow-no-checks` converts `2` → `0` while still reporting `status`. `--timeout`
(default from `SHIPIT_CHECK_TIMEOUT_SECS`, mirroring `MERGE_APPLY_TIMEOUT_SECS`) bounds the whole
suite under one ceiling.

**Merge gate fix:** `merge.py` now calls `run_checks()` over the full command set (via
`_run_merge_gate_checks`) instead of reading only `commands.check`, closing the "NO GATE" hole for
caches where only `commands.test` (or any other single command) is set. It also now runs the check
in the *target worktree's* directory (previously ran in the main worktree's cwd — a second, related
bug) and threads `_get_merge_apply_timeout()` (`MERGE_APPLY_TIMEOUT_SECS`, 1800s) instead of
inheriting `execute_check`'s 300s default.

### Amendment 4: Plan-file path argument support for `/track-and-start`

**Wrapper-side change:** The wrapper (`commands/track-and-start.md`) now always explicitly resolves
and assigns `PLAN_FILE` and `TITLE` (via a new shared Entry Mode Dispatch + Plan File Resolution
sections) before invoking `track plan`, instead of relying on live plan-mode session context to
silently supply them. This enables `/track-and-start` to accept a plan-file path argument (`$ARG1`
as a path, or `$ARG2` alongside a tracker ticket ID in `$ARG1`), and to write plan-mode plans to
disk (`~/.claude/plans/<timestamp>[-<slug>].md`) before processing.

**Validation and title derivation moved to wrapper:** Plan-file path validation (existence,
regular-file, readable, non-empty, size cap ≤1 MiB) and title derivation (first `# ` H1 or
slugified filename, with denylist `plan|untitled|draft|new|readme`) both live in the wrapper, not
the CLI — consistent with the wrapper-does-judgment / CLI-stays-deterministic split this ADR
establishes. The wrapper performs these checks because they require judgment about user intent
(e.g., distinguishing a missing file from a typo in a ticket ID, or deciding whether a filename
"plan.md" is a legitimate title or a denylist hit) and UX clarity (error messages surface to the
user before any mutations). The CLI assumes it receives only pre-validated, pre-derived inputs.

**CLI contract unchanged:** `scripts/workflow/cli.py`'s `--plan-file` and `--title` arguments already
accepted arbitrary paths and titles. The wrapper now provides them explicitly and validated; the CLI
continues to treat them as opaque inputs (no re-validation, no re-derivation).

**Known limitation — bash slugify duplication:** The wrapper's title derivation in `commands/track-and-start.md`
(slugifying filename stems) duplicates the `slugify()` logic from `scripts/workflow/track.py`, neither is
tested directly against the other, and they can diverge over time. This duplication is accepted in the
interest of keeping the wrapper's deterministic bash blocks self-contained and testable without invoking
Python. A future amendment may extract a shared slugify function and verify both callers use it
identically, but doing so now is out of scope for this ticket.

### Amendment 5: Mutation-allowlist exception for merge-queue module

**Amended by ADR-0021:** The local merge queue's mutations (`scripts/workflow/merge_queue.py`'s
`Runner.run_git`/`Runner.run_gh`) bypass the centralized `check_mutation_allowed()` allowlist
(Decision 1, Amendment 1). This is intentional per [ADR-0021 Decision 7](0021-local-merge-queue.md):
the queue's mutating calls (rebase, the pinned force-push, `gh pr merge` with queue-specific flags)
are shaped differently from what the allowlist covers. 

The queue module provides a narrower, focused audit covering only the pinned force-push via 
`force_push_tested()` (validates shas, checks lease semantics, distinguishes lease rejection from 
other errors). Rebase, fetch, and `gh pr merge` mutations are gated by broader infrastructure:
the full gate passes before mutations run, and the queue enforces serialization via kernel flock.
This exception is documented in both ADRs to mark the boundary clearly.

### Amendment 6: Cache trust, PR creation recovery, and the plan-twice pattern (2026-10-13)

**Problem:** When `.claude/github-cache.json` does not describe the current branch (e.g., the branch has moved to a different parent, or the cache is orphaned from a stale worktree), stale PR numbers and stack decisions can silently corrupt a fresh PR creation or edit.

**Cache trust model (branch equality):** `.claude/github-cache.json` is trusted only when its `branch` field matches the current branch (read by the CLI `plan` step). A cache describing a different branch is explicitly ignored by the CLI verdict (marked with `cache_stale: "branch_mismatch:..."` in the plan output). The wrapper (`commands/shipit.md`) detects this via the early plan and logs it as a NOTE, informing the user that the cache does not apply. Wrappers other than `/shipit` that consume `github-cache.json` (e.g., `/cleanup`, `/merge-and-cleanup`) similarly check the cached branch before trusting any cached decisions.

**Hint-then-verify pattern:** The `.claude/github-cache.json` file is a **hint**, not a source of truth. The `shipit apply` step always verifies the PR (live `gh pr view`: state and head branch) before any edit. On `shipit create`, when no cached PR number exists, the CLI does a by-branch lookup (listing open PRs with `head=<current-branch>`) and adopts any matching PR, allowing recovery from a missing cache. This separation ensures that a stale or missing cache does not prevent the command from completing.

**CLI as sole cache reader for PR/issue/stack decisions:** Only the deterministic CLI (`scripts/workflow/cli.py` and `scripts/workflow/shipit.py`) reads `.claude/github-cache.json` for PR existence, PR number, issue number, and stack layout decisions in `/shipit`. Direct reads in command prose (e.g., `jq '.pr.number' < .claude/github-cache.json`) have been removed. This centralizes the cache trust logic in one place and makes it testable.

**Partial-success contract:** When `shipit apply` verification refuses a PR edit (e.g., the PR is no longer OPEN), the branch has already been pushed. The PR is left untouched, and the user can inspect the live state and decide whether to create a fresh PR or fix the existing one. This is the intended outcome: a pushed branch is never left on a commit that was not applied, and the PR decision (create vs. edit) is always made via live verification, never from stale cache state.

**Plan-twice agreement pattern:** The `/shipit` command plans twice — once early (before push, to read cache verdicts and detect stale stacks) and once late (after push, with the final PR title and body). Before applying the late plan, the wrapper compares key fields (pr_exists, pr_number, cache_stale, issue_number, stale_stack_claimed, base_branch) between the early and late plans. If any field changed, the cache shifted during `/shipit` and the user is asked to re-run. This prevents the body merge (which used the early verdict) from being applied against a different PR verdict. See `commands/shipit.md` "Stack Detection" and "Create PR" sections for the implementation.

**Updated cache trust wording:** The Consequences section's wording for `is_stacked()` (lines ~43 and ~78 in the Decision section) now reads "cache-first, or falls back to ancestor search" — acknowledging that the cache is consulted first but not trusted without verification. Similarly, "Fail-closed to `unknown` exactly as ADR-0011 specifies" applies when stack detection cannot resolve the layout, not when the cache is merely absent or stale.

**Remaining direct cache readers (follow-up):** Other scripts that directly read `.claude/github-cache.json` (e.g., `scripts/workflow/stack.py`) may not yet be patched to trust only the branch-matching cache. These readers are routed through the CLI's trusted wrappers or are being addressed in follow-up work. The cache trust model is authoritative in the CLI; scripts outside the CLI are out-of-scope for this amendment but should migrate to the same model when touched.

### Amendment 7: PyYAML dependency for project.yaml reading (2026-10-13)

**Problem:** Decision 6 declared "stdlib only" (no third-party dependencies beyond Python's standard library). However, reading `.claude/project.yaml` (added in issue #275) requires YAML parsing. Using only stdlib's poor `configparser` or hand-rolled regex parsing is fragile and unmaintainable.

**Resolution:** Declare **PyYAML** as an approved, required dependency for `scripts/workflow/`. The import in `scripts/workflow/checks.py` uses a try/except guard to handle missing PyYAML gracefully during the import phase (see lines 20–23):

```python
try:
    import yaml  # type: ignore[import-untyped]
except ImportError:
    yaml = None  # type: ignore[assignment]
```

When `resolve_check_command()` reads `.claude/project.yaml` and `yaml` is `None`, it skips that source and falls back to the next one (justfile). This ensures the code does not crash if PyYAML is absent, though the resolve will fail with a clear message if all sources are exhausted.

**Installation and testing:** `install.sh` now checks for PyYAML availability and instructs the user to install it via `pip3 install PyYAML` or `brew install pyyaml` if missing (lines 54–60). `requirements-dev.txt` declares both `PyYAML>=6.0` (runtime) and `types-PyYAML>=6.0` (for mypy type checking), allowing `just typecheck` to pass without `type: ignore` comments. The guarded import is kept for runtime robustness (graceful fallback if the environment lacks PyYAML), but during development and CI the full suite is expected to install the dependency.

**Rationale:** YAML is a standard config format across the Python ecosystem (Ansible, Docker Compose, Kubernetes, etc.), and PyYAML is the de facto standard library for YAML in Python. The dependency is lightweight, well-maintained, and acceptable as the one approved exception to the "stdlib only" principle for the specific purpose of reading declarative project configuration files (`.claude/project.yaml`). This amendment supersedes Decision 6's strict "stdlib only" requirement for the YAML-reading use case.

### Amendment 8: Worktree cache seeding and fallback resolution (2026-10-13)

**Problem:** When `/track-and-start` creates a new worktree, the new worktree lacks `.claude/repo-cache.json` and `.claude/project.yaml`, which are needed by downstream commands like `/shipit`, `/cleanup`, and `/verify`. Manually creating these files in new worktrees is error-prone; they should be automatically seeded from the main worktree during worktree creation.

**Seeding mechanism:** `scripts/workflow/cache.py`'s `seed_worktree_cache(main_worktree, new_worktree)` atomically copies `repo-cache.json` and `project.yaml` from the main worktree to the new worktree. Copies are skipped if:
- Source file is absent
- Source file is not a regular file (e.g., a directory or symlink)
- Destination file already exists (never overwrites)

The function returns `(seeded: List[str], skipped: List[str], error_msg: Optional[str])`:
- `seeded`: filenames successfully copied
- `skipped`: filenames skipped
- `error_msg`: errors encountered during copying (non-None even if some files succeeded; fatal errors like "cannot create .claude directory" prevent all copying)

**Integration into track:** `/track-and-start` (and `/track`) invoke `seed_worktree_cache()` after creating the worktree and writing the initial cache. Seeding is non-fatal: any errors are logged but do not fail the command.

**Fallback resolution in downstream commands:** Commands that need check configuration (`/shipit`, `/cleanup`, `/merge-and-cleanup`, `/verify`) use `resolve_check_command(worktree, main_worktree)` to find the check command with fallback:
1. Try `<worktree>/.claude/repo-cache.json` → commands.check
2. If absent, try `<main_worktree>/.claude/repo-cache.json` → commands.check (seeding fallback)
3. If absent, try `<worktree>/.claude/project.yaml` → commands.check
4. If absent, try `<main_worktree>/.claude/project.yaml` (project.yaml fallback)
5. If absent, try `<worktree>/justfile check` recipe
6. If all fail, return explicit failure reason ("no check command configured")

This two-stage approach (seed-then-fallback) ensures:
- New worktrees inherit the main worktree's commands configuration without manual intervention
- If seeding fails (non-fatal), downstream commands can still find configuration via fallback
- A single source of truth (main worktree) drives configuration for all worktrees in the project

**Snapshot semantics:** Seeded files represent a snapshot of the main worktree's configuration at worktree-creation time. If the main worktree's configuration changes later (e.g., `/shipit` is run in the main worktree and updates `repo-cache.json`), the new worktree's seeded copy is NOT automatically updated. This is intentional: the new worktree's seeded snapshot is stable and independent from later changes in the main worktree. Downstream commands' fallback resolution (step 2 and 4 above) bridges this gap by consulting the main worktree's current files when needed.

**Failure modes:** If seeding fails completely (e.g., permission denied), worktrees can still be created and used — downstream commands fall back to the main worktree's current configuration. If fallback sources are also absent, the command fails with a clear message ("no check command configured"). This design ensures robustness: the absence of seeded files is never a blocker.

### Amendment 9: package.json check detection with provenance (2026-11-01, #417)

**Problem:** A repo whose only check command is `package.json` `scripts.check` resolves to nothing in the chain above. `/expert-flow` needs that command before implement, and the chain must not gain a live `package.json` source.

**Decision:**
- `checks.detect_package_json_check` is a deterministic file read of `package.json` `scripts.check`, prefixed by the package manager named by the root lockfile (`npm`, `pnpm`, `yarn`, or `bun`, from the same closed table `commands/shipit.md` documents). It is not a guess: no package manager is probed, and an ambiguous lockfile yields no command.
- `checks ensure-check` writes the detected command into the **main** `repo-cache.json` `commands.check`, with a top-level sidecar `commandSources: {"check": "package-json"}`. Resolution reports the source as `repo-cache (package-json)`. The write is a raw JSON update that preserves every other key. It does not round-trip through `RepoCacheData`, which drops unknown keys.
- **Chain order is unchanged.** `resolve_check_detail` keeps the six-step order above. The detector writes into the cache rather than adding a live `package.json` source, so the step and the verifier resolve the same value.
- **Human answers go to `project.yaml`, not the cache.** `checks record-check-answer` text-appends a human's answer to the main `.claude/project.yaml`. A `CLAUDE.md`-derived candidate is suggest-only and is never persisted by the CLI.
- **D4, #387:** the `/merge-and-cleanup` chain unification (#387) should reuse the shared resolver (`resolve_check_detail`) and this detector, not add its own `package.json` read. This amendment does not change #387's behavior and does not pin its design.

The mapping between the detector and `commands/shipit.md`'s lockfile table is pinned by a test, so the repo has one rule rather than two. See `docs/workflow-cli-invariants.md`, Decision 3 and Decision 8.

### Amendment 10: CLI-owned writes and install subprocesses for the environment chores (2026-11-01, #417)

**Problem:** Decision 3 keeps the CLI free of runtime configuration, and Phase 1 is read-only. The `/expert-flow` environment chores (ADR-0023, 2026-11-01 amendment) need the CLI to do more: read the main worktree's `.claude/project.yaml`, append a human's check answer to it, write `repo-cache.json` and the deps stamp, and run package-manager install subprocesses through `scripts/workflow/deps.py`. None of these is a step-agent write under the flow-directory prefix (ADR-0018, 2026-10-13 amendment), so each needs an explicit rule.

**Decision:** The CLI is permitted these behaviors, and only these, within the bounds below.

- **Reads:** the main worktree's `.claude/project.yaml` (`commands.install`, `commands.check`, `checkOptOut`).
- **Writes:**
  - The main-config writes (`checks record-check-answer`, `checks ensure-check`) take an advisory `flock` on `claude-checks-<hash>.lock` in the system temp directory, keyed by the main worktree path. The lock degrades to none if it cannot be opened.
  - `checks record-check-answer` text-appends a human's answer to the main `.claude/project.yaml`. Only a human's answer is written there; a `CLAUDE.md`-derived candidate is never persisted by the CLI (Amendment 9).
  - `checks ensure-check` writes a `package.json`-detected command into the main `repo-cache.json`, with the `commandSources` sidecar (Amendment 9).
  - `deps ensure` writes the deps stamp at `<git dir>/claude-helpers/deps-stamp.json`. It records the install command, the lockfile hash, a manifest hash (`package.json`, the table lockfiles, the install-config files `.npmrc`, `.yarnrc.yml`, `.pnpmfile.cjs` and `bunfig.toml`, and every file under `.yarn/releases/`) and any manifest approval. A stamp counts as current only if the command, lockfile hash and manifest hash all match (the manifest hash is what catches a change when there is no lockfile) and `node_modules` or `.pnp.cjs` also exists, so an install that leaves neither is reinstalled on every run. The stamp's install record is deleted before an install starts and written only after exit 0; a prior manifest approval is preserved by rewriting an approval-only stamp before the install runs.
- **Install subprocesses (`scripts/workflow/deps.py`):**
  - Frozen or immutable installs from the lockfile table: `npm ci`, `pnpm install --frozen-lockfile`, `yarn install --immutable` (Yarn 2 and later) or `yarn install --frozen-lockfile` (Yarn 1.x), or `bun install --frozen-lockfile`, selected from the root lockfile. The Yarn major comes from `package.json` `packageManager`, and an unreadable version fails closed; or the main worktree's explicit `commands.install`. The lockfile table never runs a non-frozen install. `commands.install` is the human's own command from the main `project.yaml` and is run as written, so it is not constrained to a frozen mode. "As written" means the string is split with `shlex.split` and run with `shell=False`, so shell syntax (`&&`, pipes, environment-variable prefixes) does not work; an unparseable, empty or non-string value, or an unreadable `project.yaml`, yields `failed`. When set, it takes priority over the lockfile table and the Yarn version check.
  - Fixed argv, `shell=False`, stdin closed, Corepack prompts disabled, a hard timeout, and the process group killed on timeout or on SIGINT/SIGTERM (a SIGKILL cannot run the cleanup).
  - Manifest-change approval gate: if `package.json`, any lockfile in the table, an install-config file (`.npmrc`, `.yarnrc.yml`, `.pnpmfile.cjs`, `bunfig.toml`) or a file under `.yarn/releases/` differs between the merge-base of the base ref and `HEAD` (committed or uncommitted changes count), nothing installs and the result is `manifest-changed`. `deps ensure --approve-manifest` records the human's approval of the current manifest hash; a later manifest edit re-arms the gate. The gate does not apply at worktree time, because the branch equals its base.
  - Output is redacted and bounded to a tail before it reaches a receipt, `state.json`, or a park file.
  - The command prints a `DEPS: <status>[: detail]` line. Statuses are `current`, `installed`, `failed`, `ambiguous` (including a `packageManager` that names a different manager than the lockfile), `manifest-changed` and `skipped`; exits 3, 4 and 5 are expected outcomes (`ambiguous`, `manifest-changed`, `skipped`), not command failures.
- **Environment (accepted residual):** installs run with the full environment (`dict(os.environ)`), so lifecycle scripts can see credentials such as `GH_TOKEN` and `NPM_TOKEN`. This exposure is accepted, not mitigated. Installs already execute branch-controlled lifecycle scripts by design, so scrubbing the environment would not close the exposure to those scripts, and it would break private-registry authentication, which installs need.

**Consequences:** The CLI's writes outside the flow-directory prefix are CLI-owned, not step-agent `Write`, and their paths are limited to the files listed above. The orchestrator and step agents still hold no package-manager grant; install execution happens only inside the CLI. This rule does not extend to any other CLI write or subprocess. A new one needs its own amendment.

### Amendment 11: `commands.check_minimum` selector, changed-files export, per-key records, and caching contract (2026-11-05, #387)

**Problem:** Minimum checks per PR (ADR-0023 amendment 2026-11-05) require a project-declared `check_minimum` selector, its inputs must be exported for non-cooperating selectors, and records must be per-key (FullKey vs MinimumKey) with distinct cache semantics and no read-modify-write.

**Decision:**

- **`commands.check_minimum` (new key):**
  - Optional top-level key in `project.yaml` (local tier, ADR-0005). If absent, steps fall back to `commands.check`.
  - Defines a shell command that selects and runs a subset of the full check suite. One declared command; no composition within it.
  - `check_minimum` is not run as an extra check by `build_check_order`; it is a standalone selector resolved once per flow step or `/shipit` run.
  
- **Changed-files export (canonical list computation):**
  - `scripts/workflow/checks.py` defines `canonical_changed_files(worktree, base) -> Optional[List[str]]` (committed, staged, uncommitted, untracked non-ignored, both sides of renames, deletions; sorted, unique; `None` when there is no base or a subprocess fails, so an unknown changed set can never be hashed as an empty one). The list is serialized NUL-separated and hashed with `sha256` to produce `changed_files_hash`.
  - `resolve_check_base(worktree) -> Optional[str]` computes the merge-base of HEAD with `origin/<default-branch>`, taking the default branch from `git.get_default_branch` (origin/HEAD, then local `main`/`master`), the same resolver the merge gate uses. When the base is `None`, there is no hash, and minimum mode returns uncached.
  - `checks validate --mode minimum|full [--worktree path]` computes `CHECK_BASE` and `CHECK_CHANGED_FILES` (the path of a scratch file whose first line is the base and whose remainder is the NUL-separated list) and exports them as environment variables to the resolved `check_minimum` or `commands.check` command. It hashes the same list it exports. When the list cannot be computed (the base is unresolved, including on a detached HEAD, or a subprocess fails) no minimum key is formed: the run is uncached and writes no minimum record. The full and minimum digests also cover the resolved command string and its source, so a command that changes with an unchanged tree is a miss.
  - `checks resolve-check --mode minimum|full` (default `full`, so existing callers are unchanged) returns the command for that mode and its provenance. For `minimum` with `check_minimum` undeclared it returns the full command with `fell_back_to_full: true`; it never skips silently.
  - The exports are written to a scratch file under `tempfile.gettempdir()` (outside the worktree) for the selector's reference, created and deleted by the CLI.
  - **Selector non-cooperation:** A selector that ignores the exported variables but selects from the tree, base and changed files (e.g., by re-computing them) is still keyed correctly by `validate`'s own list. One that selects from anything outside that set (env vars not exported, `HEAD~1`, network, files outside the tree) is unsafe to cache; the caching contract (below) documents this risk and requires selector authors to document their inputs. This repo's `scripts/check-minimum.py` selector reads only the tree, base and exported list, with an invariant comment.

- **Per-key records under `.claude/check-records/`:**
  - One file per key, named `<sha256(canonical-key-serialization)>.json`.
  - Record contains the key (embedded, for integrity re-check on read), `passed: true`, and a timestamp.
  - Atomic: tmp file plus `os.replace`; no read-modify-write.
  - `CheckRecord.from_dict` (`scripts/workflow/models.py`) parses and validates the exact key set, `passed: true`, `schema_version` constant match, all hex fields, and the mode (FullKey vs MinimumKey).
  - The effective run timeout is part of the full digest (and so of every key) in both `checks validate` and `checks run`: a pass under a looser limit never satisfies a request under a stricter one.
  - A failed run evicts a stale pass by moving it aside with one atomic rename, judging the moved file, and restoring it if it is fresh, so a concurrent fresh pass is never deleted (the worst case is one transient miss).
  - **Receipts go through the same boundary.** `check-receipt.json` (schema 2) is an embedded `CheckRecord` (a full-mode record) plus `base_ref`/`base_sha`; `parse_receipt` in `checks.py` calls `parse_check_record`, so there is one parser. The base is receipt-only: it feeds the merge-gate base-currency rule and is never part of a key. Schema-1 (flat, pre-record) receipts, records with an empty tree, and any record the boundary rejects are a miss.
  - `checks run` keeps its CLI contract (same arguments, JSON shape, exit codes, the invalid-cache-JSON exit 1) but is no longer a pure pass-through: in a git worktree it consults and writes the same per-key record cache (a full-mode key from the tree, the commands digest and the timeout), so a second run on an unchanged tree is a hit (L0). Outside a git tree it runs every time and writes no record.
  - A hit is reported as `cache: hit`, `executed: []`. A miss is `cache: miss` or `cache: forced` (when `--force` is passed).

- **Caching contract (key hashing invariant):**
  - The minimum key hashes the list `validate` computes itself: `MinimumKey = (tree, minimum_digest, full_digest, changed_files_hash)`.
  - A `check_minimum` command that selects from anything other than the tree, `CHECK_BASE` and `CHECK_CHANGED_FILES` is unsafe to cache. A selector that reads network state, other env vars, or files outside the tree may see different inputs on a second run with the same tree and base, causing a miss when a hit should have occurred, or worse, a hit when the cache is stale.
  - Project authors declaring `check_minimum` must document their selector's inputs. This repo's selector and its test enforce this invariant for this codebase.

**Consequences:** Verify, implement and fix steps get cache hits across unchanged code at different bases. The minimum and full digests are separate, so a single repo can use both modes without evicting each other's caches. Off-queue and queue merges both run the full check (ADR-0021), so the full key (missing the base) correctly ignores base movement. The caching contract is a residual risk: a selector that ignores the exports and reads outside the tree will silently produce stale results on a hit. This is why `resolve-check` is the only entry point for the resolver, and why selector authors must document their invariants.

### Amendment 12: Removal of `checks run` and `checks receipt` (2026-11-05, #462)

**Problem:** `checks run` and `checks receipt` (the check-receipt chain) were added under Amendment 11 but had no caller on the merge path. The merge gate and the `/expert-flow` verify step read the per-key record cache through `checks validate` and `resolve_check_detail`, never the `run` CLI or the `check-receipt.json` artifact. Keeping an unreachable write surface and a second receipt schema doubled the parser and cache paths the gate must trust.

**Decision:** Remove both subcommands and the receipt chain.
- Removed: the `checks run` and `checks receipt` CLI subcommands and their handlers in `scripts/workflow/cli.py`.
- Removed: `check-receipt.json` (schema 2) and `check-attestation.json`, their schema constants, and the functions `write_check_receipt`, `record_check_attestation`, `parse_receipt`, `check_receipt_fresh`, `receipt_base_current`, `run_checks_recorded` and `_resolve_sha` in `scripts/workflow/checks.py`.
- Kept: `checks validate` and its per-key record cache under `.claude/check-records/` (`FullKey` and `MinimumKey`, the `CHECK_BASE` and `CHECK_CHANGED_FILES` exports, and the eviction rule). `resolve_check_detail` remains the resolver, and `resolve_check_command` remains as a wrapper because a test still calls it. `resolve-check`, `ensure-check` and `record-check-answer` are unchanged.

**Supersedes:** Amendment 11's `checks run` contract (the bullet beginning "`checks run` keeps its CLI contract") and its receipt paragraph (the bullet beginning "**Receipts go through the same boundary**"). The `checks validate` contract, the record cache and the caching contract stay in force.

**Consequences:** `checks run` and `checks receipt` are no longer subcommands and fail as unknown commands. Any external caller of those commands must move to `checks validate`. No receipt file is written or read; the merge gate reads per-key records only.

### Amendment 13: gate modes for `checks validate` (2026-11-05, #462)

**Decision (no code change).** `checks validate --mode minimum` is the gate that PR-level steps and force-push gates run (`/shipit`, `/stack-sync`, and the flow verify/implement/fix steps). `checks validate --mode full` is the gate the merge path runs (ADR-0021), and it is the authoritative one. A minimum record never satisfies the merge gate. This is the human ruling on #462 (Needs-you #2, option A) and refines the ADR-0012 2026-11-05 amendment and ADR-0023 2026-11-05 amendment (#462). Amendment 12's removal of `checks run` and `checks receipt` stands. The exit-code and timeout paragraphs of earlier amendments that name `checks run` are superseded by Amendment 12; `docs/workflow-cli-invariants.md` now describes `checks validate`.

### Amendment 12: `commands.cleanup` teardown hook (2026-11-11)

**Problem:** A worktree can own resources outside the tree (containers, temporary databases) that only the project knows how to stop. `cleanup apply` removed the worktree without giving the project a chance to tear them down, which orphaned them. Decision 3 and Amendment 10 allow the CLI no such subprocess.

**Decision:** `cleanup apply` runs `commands.cleanup` once, after validation and the HEAD recheck and immediately before the worktree is removed.

- **Read:** `commands.cleanup` from the main worktree's `.claude/project.yaml` only, through the shared `checks._load_project_yaml` and `checks._command_from_mapping`. The main worktree is resolved inside the hook path (the parent of the resolved git common dir), not from the caller's cwd. A branch's own copy is never read, because the work being cleaned up could have written it; when the target is the main worktree no hook runs. Scripts the hook invokes live in the branch being cleaned, which is an accepted residual of running a main-declared command.
- **Execution:** `shell=True`, deliberately, so documented usage such as `npm run sandbox:down` and `&&` chains works. This differs from `commands.install` (Amendment 10), which is `shlex`-split, `shell=False` and behind a manifest-approval gate. The trust boundary here is main's `project.yaml`, a human-owned per-machine file. The working directory is the target worktree, stdin is closed, the parent environment is inherited, and the hook runs in its own session with a 300s timeout after which the whole process group is killed.
- **Failure:** a non-zero exit, timeout, launch failure, or an unreadable or non-string declaration keeps the worktree and branch and exits with an error. Errors go in `CleanupResult.hook_failures`, apart from `validation_failures`. An unparseable `project.yaml` blocks only when a text probe finds a `cleanup:` line. The output tail (last 5 lines, 200 characters each) is echoed and may contain secrets.
- **Retry:** the hook must be idempotent; a failed run is retried by re-running `/cleanup`. A hook that can never succeed is escaped by editing main's `project.yaml`.

**Consequences:** This is the CLI's only shell-string execution, and it is limited to this one key from this one file. A new one needs its own amendment.

