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

7. **No per-project extension point** (Decision 3): The CLI is not designed for runtime configuration. Forks that need different behavior edit `scripts/workflow/` directly — that IS the extension mechanism, consistent with the repo's fork-and-adapt philosophy.

## Consequences

### Scope included in Phase 1 (read-only primitives)

- `models.py`: Schemas for `.claude/repo-cache.json`, `.claude/github-cache.json`, and local `issues.json` with `schema_version` and hand-rolled validation.
- `git.py`: Centralized git/gh subprocess wrapper using argument arrays (never `shell=True`), with timeouts.
- `worktrees.py`: Worktree parent detection (ADR-0010), graft detection, main/second worktree discovery.
- `project.py`: Repository identity (gh repo view), local-plan-mode detection, current user.
- `cache.py`: Read and validate cache files (phase 1); write/locking stubs citing Decision 1.
- `checks.py`: Toolchain and check detection (read-only; execution in Phase 2+).
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
- **Graft detection** (from ADR-0010): Check `XDG_CONFIG_HOME/graft/config.json` for an entry matching the main worktree's path. Graft is optional; absence is not an error.

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
