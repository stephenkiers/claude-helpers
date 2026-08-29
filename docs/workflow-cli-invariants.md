# Workflow CLI Invariants

This document specifies the safety invariants and contracts that every `scripts/workflow/` module must uphold.

## Unknown State Signal (Decision 8)

**Invariant:** Every operation layer (`inspect`, `plan`, `apply`) that encounters a state it cannot recognize must return an explicit `Unknown(reason: str)` result rather than guessing, defaulting, or silently falling through to a default behavior.

**Rationale:** The wrapper (Markdown command + model) needs to know when the CLI has given up so it can pause and ask the user, or make the judgment call itself. A bare exit code or silent default would lose that signal.

**Implementation:** Use `workflow.safety.Unknown` across all modules:
- `worktrees.detect_worktree_parent()` returns empty string if git repo is undetectable.
- `stack.detect_layout()` returns `"unknown"` when layout is ambiguous.
- `cache.read_github_cache()` returns `(None, Unknown(...))` on read failure.
- `project.detect_repo_identity()` returns `None` for local-plan mode (intentional, not a failure).

## Fail-Closed Layout Detection (ADR-0011)

**Invariant:** Stack layout detection never guesses. When in doubt, return `"unknown"`.

**Layouts:**
- `"single-driver"`: One working copy has all stack branches' state locally; no sibling worktrees are checked out for other stack branches; `gh stack sync` is safe.
- `"per-branch"`: Other stack branches are already checked out in sibling worktrees; `gh stack sync` / `checkout` / `init` are fatal.
- `"unknown"`: The layout cannot be proven from worktree metadata or sibling caches; caller must resolve manually.

**Implementation:** `stack.detect_layout()` must:
1. Check if the subject's parent is checked out in any sibling worktree → per-branch.
2. Check if any sibling worktree marks the subject as their parent → per-branch.
3. Check if the subject has a known parent but no siblings checked out → single-driver.
4. Else → unknown (never default to single-driver or per-branch).

## Deterministic Outputs (Phase 1)

**Invariant:** Given identical repository state, the same inputs produce identical outputs every time. No randomness, no timestamps, no "best guess" heuristics.

**Deterministic operations:**
- Worktree list parsing and parent detection (ADR-0010 logic).
- Stack detection from cache or ancestor search (ADR-0011 logic).
- Project root and repo identity via `gh repo view`.
- Toolchain detection from config file presence.
- Cache validation against schema.

**Non-deterministic operations (out of scope Phase 1):**
- Check execution results (depends on system state, environment, installed tools).
- GitHub API results (depends on remote state, network, API rate limits).
- File permissions and symlink resolution (depends on OS and filesystem state).

## No Side Effects (Phase 1)

**Invariant:** Phase 1 modules only read files, call `git`/`gh` for inspection, and return results. They never mutate anything.

**Read-only operations:**
- Reading `.claude/github-cache.json`, `.claude/repo-cache.json`, `issues.json`.
- Calling `git` with read-only subcommands: `branch`, `rev-parse`, `worktree list`, `symbolic-ref`, `merge-base --is-ancestor`, `rev-list --count`, `ls-remote`, etc.
- Calling `gh` with read-only operations: `repo view`, `api user`, `pr view`, etc.

**Forbidden in Phase 1:**
- Writing cache files (Phase 2+ when mutations begin).
- Creating or deleting worktrees (Phase 3+).
- Creating or updating branches (Phase 2+).
- Staging, committing, or pushing (Phase 2+).

## Worktree Parent Detection Determinism (ADR-0010)

**Invariant:** Worktree parent detection must always return a deterministic path without user input or config.

**Algorithm:**
1. If a second worktree exists, use its parent directory.
2. Else if main worktree is already under `worktrees/`, use that directory.
3. Else create `worktrees` as a sibling directory to main worktree (path only; Phase 1 does not create it).

**Rationale:** Three-way fallback ensures every repo can discover its worktree parent deterministically, enabling local-plan-mode (no GitHub) and avoiding per-fork configuration.

## Cache Schema Versioning

**Invariant:** Every cache file includes a `schema_version` field. Reads validate the version before parsing. Invalid versions cause read failure, not silent parsing or default values.

**Versions:**
- `.claude/github-cache.json`: `schema_version: "1.0"`
- `.claude/repo-cache.json`: `schema_version: "1.0"`
- `issues.json`: `schema_version: "1.0"`

**Validation:** `models.py` provides `validate_*_cache()` functions that all readers must use.

## Subprocess Safety

**Invariant:** All `git` and `gh` invocations must use argument arrays, never `shell=True` or string interpolation into a shell command (CLAUDE.md requirement).

**Implementation:** `git.py` functions accept `List[str]` args only:
```python
def run_git_command(args: List[str], ...) -> str
```

Callers pass arguments as a list:
```python
git.run_git_command(["merge-base", "--is-ancestor", ancestor, descendant])
```

Never:
```python
git.run_git_command(f"merge-base --is-ancestor {ancestor} {descendant}")
```

## Exit Codes and Categorized Results

**Invariant:** Phase 1 modules never return bare exit codes. All failures return categorized, structured results so callers can decide next steps.

**Return types:**
- `(result: T or None, error: Unknown or None)` tuple.
- String literals for enums (`"single-driver"`, `"per-branch"`, `"unknown"`).
- Dict/dataclass results for composite data.

**Caller responsibility:** Inspect results and handle errors explicitly; never assume success.

## No Per-Project Configuration (Decision 3)

**Invariant:** The CLI has no per-project runtime configuration or extension points. Behavior is fixed by the code.

**Consequence:** Forks that need different behavior must edit `scripts/workflow/` directly in their own copy. This is intentional — the repo is designed to be forked and adapted at the source level, not via a runtime config layer.

**Example:** If a fork wants to prioritize different checks or detect additional toolchains, it edits `checks.py` and commits its own version.

## Stack Detection Cache Order (ADR-0011)

**Invariant:** `is_stacked()` checks the cache first, then falls back to ancestor search. Cache is always the source of truth when present.

**Priority:**
1. If `.claude/github-cache.json` exists and contains `.stack.isStacked`, use it (and `.stack.parentBranch`, `.stack.parentPr`).
2. Else, search other worktrees' branches for the tightest (fewest-commits) ancestor.
3. Else, not stacked.

**Rationale:** Cache allows explicit override of auto-detection; users can set `"isStacked": false` to disable stack detection if auto-detection misfires.

## Phase 1 Test Coverage

**Design intent:** Automated tests aim to cover all supported repo states:
- Normal repo (single worktree, stacked, single-driver).
- Bare worktree setup.
- Local-plan mode (no GitHub remote).
- External tracker repos.
- Stacked per-branch layout.
- Dirty working directory.
- Stale cache (content mismatch).
- Missing remote.

**Implementation:** Phase 1 uses inline temporary directories and mocks rather than pre-built fixture files. Future phases may expand to fixture-based testing for readability and reusability.

**Rationale:** Ensures code paths are exercised before mutation phases begin.

## Error Messages and Diagnostics

**Invariant:** All error messages must include enough context for a human to understand what went wrong and what to try next.

**Format:** `Unknown(reason: str)` where `reason` explains:
- What was attempted.
- Why it failed.
- What the caller should try (if obvious).

**Example:**
```python
Unknown("github-cache.json schema validation failed: missing .stack field")
```

Not:
```python
Unknown("cache error")
```
