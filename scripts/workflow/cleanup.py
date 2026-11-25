"""
Cleanup plan and apply (Phase 2 of ADR-0013).

Ports the deterministic cleanup logic from /cleanup into a plan/apply pattern:
- plan_cleanup: read-only inspection of target worktree state
- apply_cleanup: execute worktree removal, branch deletion, validation gates
"""

import json
import os
import re
import signal
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple, NamedTuple, Callable, Union

from . import checks
from . import git
from . import check_diagnostics as diagnostics
from . import validation
from .cache import hash_cache_file, hash_file_content, read_github_cache
from .safety import Unknown, fail_closed
from .models import RepoCacheData
from .merge import merge_lock_path
from .validation import ValidationVerdict
from . import procs
from . import teardown_guard
from . import fsutil

CLEANUP_HOOK_TIMEOUT_SECS = 300
_CLEANUP_KEY_PROBE = re.compile(r"^\s*cleanup\s*:", re.MULTILINE)

# Tri-state dirty check results
CLEAN = "clean"
DIRTY = "dirty"
UNKNOWN = "unknown"

DEFAULT_CLEANUP_CHECK_TIMEOUT_SECS = 300
CHECK_TIMEOUT_MESSAGE_PREFIX = "Check command timed out"

# Removal result status constants
REMOVED = "removed"
ALREADY_GONE = "already_gone"
SKIPPED = "skipped"
FAILED = "failed"


class RemovalResult(NamedTuple):
    """Result of a remove_worktree operation."""
    path: str
    status: str  # One of REMOVED, ALREADY_GONE, SKIPPED, FAILED
    reason: str
    pids: List[int] = []
    used_trash: bool = False


def _get_cleanup_check_timeout() -> int:
    """
    Resolve the timeout for the post-merge regression check commands from the environment.

    Reads CLEANUP_CHECK_TIMEOUT_SECS; returns the value if it's a valid positive
    integer, otherwise returns DEFAULT_CLEANUP_CHECK_TIMEOUT_SECS.

    Never raises; invalid values silently fall back to the default.
    """
    env_value = os.environ.get("CLEANUP_CHECK_TIMEOUT_SECS", "").strip()
    if not env_value:
        return DEFAULT_CLEANUP_CHECK_TIMEOUT_SECS

    try:
        timeout_secs = int(env_value)
        if timeout_secs > 0:
            return timeout_secs
    except (ValueError, TypeError):
        pass

    return DEFAULT_CLEANUP_CHECK_TIMEOUT_SECS


@dataclass
class ChildBranchInfo:
    """Information about a stacked child branch."""
    branch: str
    pr_number: Optional[int] = None
    worktree_path: Optional[str] = None


@dataclass
class CleanupPlan:
    """Plan for cleaning up a worktree and branch."""
    target_worktree: str
    current_branch: str
    pr_state: str
    pr_number: Optional[int] = None
    expected_head_sha: Optional[str] = None
    cache_hash: Optional[str] = None
    check_commands: List[str] = field(default_factory=list)
    stacked_children: List[ChildBranchInfo] = field(default_factory=list)
    plan_hash: Optional[str] = None
    needs_confirmation: List[str] = field(default_factory=list)
    gh_lookup_failed: bool = False
    repo_cache_read_failed: bool = False
    confirmed_dirty: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        d = asdict(self)
        d["stacked_children"] = [asdict(c) for c in self.stacked_children]
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CleanupPlan":
        """Construct from parsed JSON dict."""
        child_field_names = {f.name for f in ChildBranchInfo.__dataclass_fields__.values()}
        children = [
            ChildBranchInfo(**{k: v for k, v in child.items() if k in child_field_names})
            for child in data.get("stacked_children", [])
        ]
        data_copy = data.copy()
        data_copy["stacked_children"] = children
        field_names = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data_copy.items() if k in field_names})


@dataclass
class CleanupResult:
    """
    Result of applying a cleanup plan.

    Fields:
        success: True if worktree was removed and branch was deleted.
        worktree_removed: True if the target worktree was successfully removed.
        branch_deleted: True if the branch was successfully deleted.
        validation: Three-state validation verdict (PASS, SKIPPED, INCONCLUSIVE).
        validation_reason: Human-readable reason for validation verdict.
        queue_tested_steps: List of queue proof steps (populated only when verdict is SKIPPED).
        validation_failures: Errors encountered during validation or worktree cleanup.
        hook_failures: Errors from the commands.cleanup teardown hook (reported separately).
        notes: Non-fatal observations (e.g., dirty-tree retry). Separate from failures.
        tree: For SKIPPED verdict, the commit SHA that was queue-proven (populated by queue_proof).
        error: Set when validation returns without completing due to an error (lock contention,
                stale plan, ff-pull failure, or env-derivation failure).
    """
    success: bool
    worktree_removed: bool = False
    branch_deleted: bool = False
    validation: ValidationVerdict = ValidationVerdict.INCONCLUSIVE
    validation_reason: str = ""
    queue_tested_steps: List[str] = field(default_factory=list)
    validation_failures: List[str] = field(default_factory=list)
    # Errors from the commands.cleanup teardown hook, kept apart from validation_failures.
    hook_failures: List[str] = field(default_factory=list)
    # Non-fatal observations about how the cleanup reached success (e.g. a safe
    # worktree removal that had to be retried with --force). Kept separate from
    # validation_failures so a recovered step is not reported as a failure.
    notes: List[str] = field(default_factory=list)
    tree: Optional[str] = None
    error: Optional[Unknown] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        d = asdict(self)
        if self.error:
            d["error"] = str(self.error)
        d["validation"] = self.validation.value
        # Add deprecated field for backwards compatibility
        d["validation_passed"] = self.validation in (ValidationVerdict.PASS, ValidationVerdict.SKIPPED)
        # tree is already in d via asdict; leave it as-is (None or str)
        return d


@fail_closed
def plan_cleanup(
    target_path: str,
    cwd: Optional[Path] = None
) -> Tuple[Optional[CleanupPlan], Optional[Unknown]]:
    """
    Plan a cleanup operation (read-only).

    Returns (CleanupPlan, None) on success.
    Returns (None, Unknown(...)) if target resolution or state inspection fails,
    including when the worktree parent is unsafe (WorktreeParentError or its
    subclass WorktreeListError from worktrees.detect_worktree_parent is reported
    as an Unknown, never as an empty stacked-children list).
    """
    from . import worktrees

    try:
        target_worktree = Path(target_path).resolve()
        if not target_worktree.exists():
            return None, Unknown(f"Target worktree does not exist: {target_path}")

        if not target_worktree.is_dir():
            return None, Unknown(f"Target path is not a directory: {target_path}")

        current_branch = git.get_current_branch(cwd=target_worktree)

        pr_data = git.pr_view_json(
            current_branch,
            ["state", "number"],
            cwd=target_worktree
        )
        pr_state = pr_data.get("state", "NONE") if pr_data else "NONE"
        pr_number = pr_data.get("number") if pr_data else None

        expected_head_sha = git.get_head_sha(cwd=target_worktree)

        repo_cache_path = target_worktree / ".claude" / "repo-cache.json"
        cache_hash = hash_cache_file(repo_cache_path)

        check_commands: List[str] = []
        repo_cache_read_failed = False
        if repo_cache_path.exists():
            cache_data, cache_err = _read_repo_cache(repo_cache_path)
            if cache_err:
                repo_cache_read_failed = True
            else:
                try:
                    if cache_data:
                        check_commands = _extract_check_commands(cache_data)
                except Exception:
                    pass

        stacked_children, gh_lookup_failed, detection_incomplete = _detect_stacked_children(current_branch, target_worktree, cwd=cwd)

        needs_confirmation: List[str] = []
        if pr_state != "MERGED":
            needs_confirmation.append("pr_state_not_merged")
        if stacked_children:
            needs_confirmation.append("stacked_children_present")
        if detection_incomplete:
            if "stacked_children_present" not in needs_confirmation:
                needs_confirmation.append("stacked_children_present")

        plan = CleanupPlan(
            target_worktree=str(target_worktree),
            current_branch=current_branch,
            pr_state=pr_state,
            pr_number=pr_number,
            expected_head_sha=expected_head_sha,
            cache_hash=cache_hash,
            check_commands=check_commands,
            stacked_children=stacked_children,
            needs_confirmation=needs_confirmation,
            gh_lookup_failed=gh_lookup_failed,
            repo_cache_read_failed=repo_cache_read_failed
        )

        plan_json = json.dumps(plan.to_dict())
        plan.plan_hash = _hash_plan(plan_json)

        return plan, None

    except worktrees.WorktreeParentError as e:
        return None, Unknown(str(e))
    except Exception as e:
        return None, Unknown(f"plan_cleanup failed: {e}")


def _check_plan_fresh(plan: CleanupPlan, target_worktree: Path) -> Optional[Unknown]:
    """
    Verify that a plan is still fresh before running validation.

    Checks:
    1. Worktree still exists and is a directory
    2. Branch hasn't changed
    3. HEAD SHA hasn't changed
    4. Cache hash hasn't changed

    Returns None if all checks pass; returns Unknown(...) if any check fails.
    """
    try:
        if not target_worktree.exists():
            return Unknown(f"Target worktree does not exist: {plan.target_worktree}")

        if not target_worktree.is_dir():
            return Unknown(f"Target path is not a directory: {plan.target_worktree}")

        current_branch = git.get_current_branch(cwd=target_worktree)
        if current_branch != plan.current_branch:
            return Unknown(f"Branch changed: was {plan.current_branch}, now {current_branch}")

        current_head_sha = git.get_head_sha(cwd=target_worktree)
        if current_head_sha != plan.expected_head_sha:
            return Unknown("HEAD SHA changed (plan is stale)")

        cache_hash = hash_cache_file(Path(plan.target_worktree) / ".claude" / "repo-cache.json")
        if cache_hash != plan.cache_hash:
            return Unknown("Cache has changed (plan is stale)")

        return None

    except Exception as e:
        return Unknown(f"Freshness validation failed: {e}")


def _remove_worktree_and_branch(
    plan: CleanupPlan,
    result: CleanupResult,
    cwd: Optional[Path] = None
) -> None:
    """
    Remove worktree and delete branch (after validation lock is released).

    Mutates result in-place: sets worktree_removed, branch_deleted,
    validation_failures, and notes.
    """
    # Worktree removal: for now, use the old path since remove_worktree requires GuardContext
    # This keeps compatibility until the later unit provides the full teardown orchestration
    try:
        success, err = git.remove_worktree(Path(plan.target_worktree), force=False, cwd=cwd)
        if success:
            result.worktree_removed = True
        elif err:
            # Escalate to --force only for a dirty tree
            reason_lower = (err.reason or "").lower()
            is_dirty_tree_failure = (
                "dirty" in reason_lower
                or "modified or untracked" in reason_lower
                or "use --force" in reason_lower
            )
            first_failure = f"Worktree removal failed: {err.reason}"
            if is_dirty_tree_failure:
                try:
                    success, retry_err = git.remove_worktree(Path(plan.target_worktree), force=True, cwd=cwd)
                    if success:
                        result.worktree_removed = True
                        result.notes.append(f"{first_failure} — retried with --force, succeeded")
                    else:
                        result.validation_failures.append(first_failure)
                        if retry_err:
                            result.validation_failures.append(f"Forced worktree removal failed: {retry_err.reason}")
                except Exception as e:
                    result.validation_failures.append(first_failure)
                    result.validation_failures.append(f"Forced worktree removal error: {e}")
            else:
                result.validation_failures.append(first_failure)
    except Exception as e:
        result.validation_failures.append(f"Worktree removal error: {e}")

    if result.worktree_removed:
        try:
            merge_lock_path(str(plan.target_worktree)).unlink(missing_ok=True)
        except Exception as e:
            result.validation_failures.append(f"Merge lock cleanup failed (non-fatal): {e}")

    # Branch deletion
    force_delete = plan.pr_state == "MERGED"
    try:
        success, err = git.delete_branch(plan.current_branch, force=force_delete, cwd=cwd)
        if success:
            result.branch_deleted = True
        elif err:
            result.validation_failures.append(f"Branch deletion failed: {err.reason}")
    except Exception as e:
        result.validation_failures.append(f"Branch deletion error: {e}")

    result.success = result.worktree_removed and result.branch_deleted


@fail_closed
def apply_cleanup(
    plan_json: str,
    cwd: Optional[Path] = None,
    queue_started_at: Optional[float] = None
) -> Tuple[CleanupResult, Optional[Unknown]]:
    """
    Apply a cleanup plan (mutating).

    Validates freshness, then executes:
    1. Pull main ff-only (from main worktree)
    2. Run validation check commands (with optional queue proof)
    3. Remove worktree
    4. Delete branch (force if PR_STATE == MERGED)

    Returns (CleanupResult, None) with execution result.
    Returns (CleanupResult, Unknown(...)) if a critical error occurs.
    """
    try:
        plan_data = json.loads(plan_json)
        plan = CleanupPlan.from_dict(plan_data)

        result = CleanupResult(success=False)

        # Check freshness before anything else
        target_worktree = Path(plan.target_worktree)
        freshness_err = _check_plan_fresh(plan, target_worktree)
        if freshness_err:
            result.validation = ValidationVerdict.INCONCLUSIVE
            result.validation_reason = f"validation not run: {freshness_err}"
            result.error = freshness_err
            return result, result.error

        main_worktree_path = cwd or Path.cwd()

        # Enter validation lock, pull main, and run validation.
        # SKIPPED verdict (queue proof) does NOT return early; it falls through to mutation.
        with validation.validation_lock(main_worktree_path) as lock_result:
            if lock_result is not None:
                # Lock failed
                result.validation = ValidationVerdict.INCONCLUSIVE
                result.validation_reason = lock_result
                result.error = Unknown(f"Validation lock failed: {lock_result}")
                return result, None

            # Try to pull main
            try:
                success, err = git.pull_ff_only("origin", "main", cwd=main_worktree_path)
                if not success:
                    result.validation = ValidationVerdict.INCONCLUSIVE
                    result.validation_reason = f"could not fast-forward main: {err.reason if err else 'unknown error'}"
                    result.error = Unknown(result.validation_reason)
                    return result, None
            except Exception as e:
                result.validation = ValidationVerdict.INCONCLUSIVE
                result.validation_reason = f"could not fast-forward main: {e}"
                result.error = Unknown(result.validation_reason)
                return result, None

            # Try queue proof if context is available
            queue_proved = False
            if queue_started_at is not None and plan.pr_number is not None:
                proof_result = validation.queue_proof(plan.pr_number, queue_started_at, main_worktree_path)
                if isinstance(proof_result, validation.Proved):
                    result.validation = ValidationVerdict.SKIPPED
                    result.validation_reason = proof_result.tree
                    result.tree = proof_result.tree
                    result.queue_tested_steps = proof_result.steps
                    queue_proved = True
                    # Proof succeeded; skip validation checks but continue to mutation
                else:
                    # CannotProve; continue to run validation
                    result.notes.append(f"queue proof unavailable: {proof_result.reason}; running validation")

            # If queue proof succeeded, skip the rest of validation
            if not queue_proved:
                # Build validation environment
                check_timeout = _get_cleanup_check_timeout()
                log_dir = diagnostics.make_log_dir() if plan.check_commands else None

                env_derivation = validation.build_validation_env(main_worktree_path, dict(os.environ))
                if env_derivation.inconclusive_reason:
                    result.validation = ValidationVerdict.INCONCLUSIVE
                    result.validation_reason = env_derivation.inconclusive_reason
                    result.notes.extend(env_derivation.notes)
                    result.error = Unknown(result.validation_reason)
                    return result, None

                result.notes.extend(env_derivation.notes)

                # Run validation
                val_run = validation.run_validation(
                    plan.check_commands,
                    main_worktree_path,
                    env_derivation.env,
                    check_timeout,
                    log_dir,
                    env_derivation.dropped_names
                )

                result.validation = val_run.verdict
                result.validation_reason = val_run.reason
                result.validation_failures.extend(val_run.failures)
                result.notes.extend(val_run.notes)

                # Logs are only useful for post-mortems: keep them when any check failed or timed out.
                if val_run.verdict == ValidationVerdict.PASS:
                    try:
                        diagnostics.remove_log_dir(log_dir)
                    except Exception as e:
                        result.notes.append(f"failed to remove log dir: {e}")

        # Re-validate HEAD SHA immediately before mutation
        try:
            current_head_sha_recheck = git.get_head_sha(cwd=target_worktree)
            if current_head_sha_recheck != plan.expected_head_sha:
                result.validation = ValidationVerdict.INCONCLUSIVE
                result.validation_reason = "plan went stale during validation"
                result.error = Unknown("HEAD SHA changed during validation (plan went stale) — aborting before worktree removal")
                return result, result.error
        except Exception as e:
            result.validation = ValidationVerdict.INCONCLUSIVE
            result.validation_reason = "freshness re-validation before mutation failed"
            result.error = Unknown(f"Freshness re-validation before mutation failed: {e}")
            return result, result.error

        # Pre-hook dirty/unpushed/live check: run before the hook to avoid hook side-effects
        # if removal will be blocked anyway
        if not plan.confirmed_dirty:
            dirty_state, dirty_detail = worktree_dirty_state(target_worktree)
            if dirty_state == UNKNOWN:
                result.validation = ValidationVerdict.INCONCLUSIVE
                result.validation_reason = "dirty state check failed before hook"
                result.error = Unknown(f"Dirty state check failed: {dirty_detail}")
                return result, result.error
            elif dirty_state == DIRTY:
                result.error = Unknown(f"Worktree is dirty; use --confirm-dirty to proceed: {dirty_detail}")
                return result, result.error

            # Check unpushed state
            try:
                tip = git.get_head_sha(cwd=target_worktree)
                merged_head_oid = None
                try:
                    pr_data = git.pr_view_json(git.get_current_branch(cwd=target_worktree), ["headRefOid"], cwd=target_worktree)
                    if pr_data and pr_data.get("state") == "MERGED":
                        merged_head_oid = pr_data.get("headRefOid")
                except Exception:
                    pass
                unpushed_state_result, unpushed_detail = unpushed_state(tip, merged_head_oid, target_worktree)
            except Exception as e:
                unpushed_state_result = UNKNOWN
                unpushed_detail = str(e)

            if unpushed_state_result == UNKNOWN:
                result.validation = ValidationVerdict.INCONCLUSIVE
                result.validation_reason = "unpushed state check failed before hook"
                result.error = Unknown(f"Unpushed state check failed: {unpushed_detail}")
                return result, result.error
            elif unpushed_state_result == UNPUSHED:
                result.error = Unknown(f"Worktree has unpushed commits; use --confirm-dirty to proceed: {unpushed_detail}")
                return result, result.error

            # Check if any process is live in the worktree
            caller_chain_result = procs.caller_chain()
            caller_pids: Union[List[int], Unknown]
            if isinstance(caller_chain_result, Unknown):
                caller_pids = caller_chain_result
            else:
                caller_pids = [p.pid for p in caller_chain_result]
            liveness = procs.live_in(str(target_worktree), caller_pids)
            if liveness == procs.LIVE:
                result.error = Unknown("Worktree has live session(s); cannot remove")
                return result, result.error
            elif liveness == procs.UNKNOWN:
                result.validation = ValidationVerdict.INCONCLUSIVE
                result.validation_reason = "liveness check failed before hook"
                result.error = Unknown("Liveness check failed; cannot determine if worktree is in use")
                return result, result.error

        # Teardown hook (commands.cleanup): runs while the worktree still exists, so a project can
        # stop what it started there (containers, temp databases). A failing hook blocks removal.
        hook_error = _run_cleanup_hook(target_worktree, result)
        if hook_error:
            result.error = hook_error
            return result, hook_error

        # Acquire removal lock (non-blocking) for the worktree removal phase
        # This prevents concurrent removals of the same worktree
        # F31: Move lock to stable shared root under ~/.claude instead of cwd
        removal_lock_path = Path.home() / ".claude" / "cleanup-removal.lock"
        try:
            removal_lock_path.parent.mkdir(parents=True, exist_ok=True)
            with fsutil.nonblocking_flock(removal_lock_path):
                # Now proceed with worktree removal and branch deletion
                _remove_worktree_and_branch(plan, result, cwd=cwd)
        except fsutil.LockHeld:
            result.validation = ValidationVerdict.INCONCLUSIVE
            result.validation_reason = "removal lock contended"
            result.error = Unknown("Another cleanup is removing a worktree; try again")
            return result, result.error
        except Exception as e:
            result.validation = ValidationVerdict.INCONCLUSIVE
            result.validation_reason = "removal lock acquisition failed"
            result.error = Unknown(f"Failed to acquire removal lock: {e}")
            return result, result.error

        return result, None

    except json.JSONDecodeError as e:
        return CleanupResult(success=False, error=Unknown(f"Invalid plan JSON: {e}")), None
    except Exception as e:
        return CleanupResult(success=False, error=Unknown(f"apply_cleanup failed: {e}")), None


def _main_worktree_of(target_worktree: Path) -> Optional[Path]:
    """The main worktree: the resolved parent of the common .git directory, or None if unknown."""
    common = git.abs_git_common_dir(target_worktree)
    if common is None:
        return None
    return common.resolve().parent


def _main_cleanup_command(main_worktree: Path) -> Tuple[Optional[str], Optional[str]]:
    """
    commands.cleanup from the MAIN worktree's project.yaml only (a branch's own copy can be
    written by the work being cleaned up, so it is never trusted).

    Returns (command, error). This is the one place that decides whether a problem reading the
    file blocks cleanup. An absent file or key gives (None, None). A file that cannot be read or
    parsed (including PyYAML missing) blocks only when a cheap text probe finds a `cleanup:` line,
    so an unrelated broken project.yaml never blocks a project with no hook. A present key whose
    value is not a non-empty string blocks.
    """
    path = main_worktree / ".claude" / "project.yaml"
    if not path.exists():
        return None, None
    data, note = checks._load_project_yaml(path)
    if data is None:
        try:
            text = path.read_text()
        except (OSError, ValueError):
            text = ""
        if _CLEANUP_KEY_PROBE.search(text):
            return None, (
                "commands.cleanup unreadable: main project.yaml may declare commands.cleanup "
                f"but cannot be parsed: {note}"
            )
        return None, None
    commands = data.get("commands")
    if not isinstance(commands, dict) or commands.get("cleanup") is None:
        return None, None
    cmd = checks._command_from_mapping(data, "cleanup")
    if cmd is None:
        return None, "commands.cleanup unreadable: value must be a non-empty string"
    return cmd, None


def _failure_tail(stdout: Optional[str], stderr: Optional[str]) -> str:
    """Last 5 lines of stdout then stderr combined, each truncated to 200 chars."""
    lines = ((stdout or "").strip().splitlines() + (stderr or "").strip().splitlines())[-5:]
    return "\n".join(line[:200] for line in lines)


def worktree_dirty_state(target_worktree: Path) -> Tuple[str, str]:
    """
    Tri-state dirty check using --no-optional-locks.

    Returns (state, detail) where state is one of CLEAN, DIRTY, or UNKNOWN.
    - CLEAN: git status --porcelain returns empty (no changes)
    - DIRTY: git status --porcelain returns non-empty text (changes present, excluding untracked .claude/)
    - UNKNOWN: any error (non-zero exit, timeout, exception) with error reason

    Untracked .claude/ state files are not treated as "dirty" since they are runtime state.
    "not a git repository" errors are treated as CLEAN, as there's nothing to check in non-git directories.
    """
    try:
        output = git.run_git_command(
            ["--no-optional-locks", "status", "--porcelain", "--end-of-options"],
            cwd=target_worktree,
            check=True
        )
        if output:
            # Filter out untracked .claude/ files (runtime state, not part of the working tree dirtiness)
            lines = output.splitlines()
            filtered = [line for line in lines if not (line.startswith("?? .claude/"))]
            if filtered:
                return DIRTY, "\n".join(filtered)
            else:
                return CLEAN, ""
        else:
            return CLEAN, ""
    except subprocess.CalledProcessError as e:
        reason = str(e)
        if e.stderr:
            reason = e.stderr.strip()
        # F15: Map probe errors (not a git repo, git failure) to UNKNOWN, never CLEAN/PUSHED
        return UNKNOWN, f"git status failed: {reason}"
    except RuntimeError as e:
        return UNKNOWN, f"git status timed out or failed: {e}"
    except Exception as e:
        return UNKNOWN, f"git status error: {e}"


def _worktree_status(target_worktree: Path) -> str:
    """
    Compatibility wrapper; prefer worktree_dirty_state for new code.

    Returns the porcelain text if dirty, empty string if clean, empty string if unknown.
    For tri-state handling, use worktree_dirty_state instead.
    """
    state, detail = worktree_dirty_state(target_worktree)
    if state == DIRTY:
        return detail
    return ""


# Tri-state unpushed check results
PUSHED = "pushed"
UNPUSHED = "unpushed"


def unpushed_state(
    tip: str,
    merged_head_oid: Optional[str] = None,
    repo: Optional[Path] = None
) -> Tuple[str, str]:
    """
    Tri-state unpushed check.

    A worktree tip is "pushed" if:
    (a) It is reachable from some remote-tracking ref (`git rev-list <tip> --not --remotes` is empty), OR
    (b) It equals or is an ancestor of a MERGED PR's headRefOid

    Returns (state, detail) where state is one of PUSHED, UNPUSHED, or UNKNOWN.
    - PUSHED: tip is reachable from remotes OR merged_head_oid check passes
    - UNPUSHED: tip is not reachable and merged check doesn't apply/fails
    - UNKNOWN: any error (missing PR data, timeout, exception, not a git repo)

    Args:
        tip: Commit/ref to check
        merged_head_oid: Optional headRefOid from a MERGED PR
        repo: Working directory for git commands (defaults to cwd)

    F15: Probe errors (not a git repository, git failures) are UNKNOWN, never PUSHED/CLEAN.
    """
    # Check (a): reachability from remotes
    # F1: Put tip FIRST so --not doesn't negate the tip
    rev_list_error: Optional[str] = None
    try:
        sha = git.resolve_commit_sha(tip, cwd=repo)
        output = git.run_git_command(
            ["--no-optional-locks", "rev-list", sha, "--not", "--remotes"],
            cwd=repo,
            check=True
        )
        if not output:
            # Empty output: tip is reachable from remotes
            return PUSHED, "reachable from remote-tracking refs"
    except subprocess.CalledProcessError as e:
        reason = str(e)
        if e.stderr:
            reason = e.stderr.strip()
        # F15: Probe errors go to rev_list_error; will decide UNKNOWN vs UNPUSHED at end
        rev_list_error = reason
    except (RuntimeError, Exception) as e:
        rev_list_error = str(e)

    # Check (b): ancestry from merged PR's headRefOid
    if merged_head_oid:
        try:
            git.run_git_command(
                ["--no-optional-locks", "merge-base", "--is-ancestor", "--end-of-options", tip, merged_head_oid],
                cwd=repo,
                check=True
            )
            return PUSHED, f"ancestor of merged PR (headRefOid: {merged_head_oid[:8]})"
        except subprocess.CalledProcessError:
            # Not an ancestor
            pass
        except (RuntimeError, Exception) as e:
            return UNKNOWN, f"merge-base check failed: {e}"

    # Neither reachability nor merged PR ancestry check passed
    # F15: If rev-list failed and no merged PR available, it's a probe error (UNKNOWN)
    if rev_list_error and not merged_head_oid:
        return UNKNOWN, f"reachability check failed and no merged PR data: {rev_list_error}"

    if merged_head_oid is not None:
        return UNPUSHED, "not reachable from remotes and not ancestor of merged PR"
    else:
        return UNPUSHED, "not reachable from remotes (no merged PR data)"


def remove_worktree(
    path: str,
    ctx: teardown_guard.GuardContext,
    confirmed: bool,
    *,
    repo: Optional[Path] = None,
    live_probe: Optional[Callable] = None,
    caller_pids: Optional[Union[List[int], Unknown]] = None,
    dirty_probe: Optional[Callable[[Path], Tuple[str, str]]] = None,
    unpushed_probe: Optional[Callable[[str, Optional[str], Optional[Path]], Tuple[str, str]]] = None,
    git_runner: Optional[Callable] = None,
    force_after_hook: bool = False
) -> RemovalResult:
    """
    Remove a worktree with comprehensive safety checks and fallback.

    Args:
        path: Worktree path to remove
        ctx: GuardContext for validation
        confirmed: Whether the user confirmed removal of dirty/unpushed trees
        repo: Optional working directory for git/dirty checks
        live_probe: Injectable live_in function (procs.live_in)
        caller_pids: Injectable caller pids (from procs.caller_chain)
        dirty_probe: Injectable dirty state check
        unpushed_probe: Injectable unpushed state check
        git_runner: Injectable git command runner
        force_after_hook: If True, use --force for git worktree remove

    Returns:
        RemovalResult with status, reason, pids list, and trash flag.
    """
    path_obj = Path(path)

    # Inject defaults
    if live_probe is None:
        live_probe = procs.live_in  # type: ignore[assignment]
    if dirty_probe is None:
        dirty_probe = worktree_dirty_state
    if unpushed_probe is None:
        unpushed_probe = unpushed_state
    if caller_pids is None:
        caller_chain_result = procs.caller_chain()
        if isinstance(caller_chain_result, Unknown):
            caller_pids = caller_chain_result
        else:
            caller_pids = [p.pid for p in caller_chain_result]

    # 1. Live-session check
    liveness = live_probe(path, caller_pids)
    if liveness == procs.LIVE:
        return RemovalResult(path, SKIPPED, "worktree has live session(s)", pids=[])
    elif liveness == procs.UNKNOWN:
        return RemovalResult(path, SKIPPED, "liveness check failed: cannot determine if worktree is in use", pids=[])

    # Extract pids if liveness was not determined but we have a list
    pids_list: List[int] = []
    if isinstance(caller_pids, list):
        pids_list = caller_pids

    # 2. Dirty and unpushed check
    dirty_state, dirty_detail = dirty_probe(path_obj)
    if dirty_state == UNKNOWN:
        return RemovalResult(path, SKIPPED, f"dirty state check failed: {dirty_detail}", pids=pids_list)

    # Check unpushed state (need tip and optional merged_head_oid)
    try:
        tip = git.get_head_sha(cwd=path_obj)
        merged_head_oid = None
        # Try to get merged PR's headRefOid if available
        try:
            pr_data = git.pr_view_json(git.get_current_branch(cwd=path_obj), ["headRefOid"], cwd=path_obj)
            if pr_data and pr_data.get("state") == "MERGED":
                merged_head_oid = pr_data.get("headRefOid")
        except Exception:
            pass

        unpushed_state_result, unpushed_detail = unpushed_probe(tip, merged_head_oid, path_obj)
    except Exception as e:
        unpushed_state_result = UNKNOWN
        unpushed_detail = str(e)

    if unpushed_state_result == UNKNOWN:
        return RemovalResult(path, SKIPPED, f"unpushed state check failed: {unpushed_detail}", pids=pids_list)

    # Check if tree is dirty or unpushed; block removal unless confirmed
    if not confirmed:
        if dirty_state == DIRTY:
            return RemovalResult(path, SKIPPED, f"worktree is dirty: {dirty_detail}", pids=pids_list)
        if unpushed_state_result == UNPUSHED:
            return RemovalResult(path, SKIPPED, f"worktree has unpushed commits: {unpushed_detail}", pids=pids_list)

    # 3. Try git worktree remove
    try:
        success, err = git.remove_worktree(path_obj, force=force_after_hook, cwd=repo)
        if success:
            return RemovalResult(path, REMOVED, "", pids=pids_list, used_trash=False)
        elif err:
            # On failure, try fallback: move_to_trash, git worktree prune, guarded_rmtree
            # F21: Only fall back for known benign refusals, not unknown errors
            reason_lower = (err.reason or "").lower()
            is_directory_not_empty = "directory not empty" in reason_lower
            is_benign_refusal = is_directory_not_empty or "worktree is locked" in reason_lower

            if is_benign_refusal and not force_after_hook:
                # Try fallback path
                try:
                    success_trash, trash_reason, new_path = teardown_guard.move_to_trash(path, ctx)
                    if success_trash and new_path:
                        # Prune after move
                        try:
                            git.run_git_command(["worktree", "prune"], cwd=repo, check=False)
                        except Exception:
                            pass

                        # Try guarded_rmtree on the trash item
                        # F6: Create a new context that includes the trash path so validation passes
                        trash_ctx = teardown_guard.GuardContext(
                            worktrees_parent=ctx.worktrees_parent,
                            primary_worktree=ctx.primary_worktree,
                            registered=list(ctx.registered) + [new_path],
                            plan_paths=list(ctx.plan_paths) + [new_path],
                            trash_root=ctx.trash_root,
                            home=ctx.home,
                            claude_dir=ctx.claude_dir
                        )
                        success_rm, rm_reason = teardown_guard.guarded_rmtree(
                            teardown_guard.KIND_TRASH, new_path, trash_ctx
                        )
                        if success_rm:
                            return RemovalResult(path, REMOVED, "", pids=pids_list, used_trash=True)
                        else:
                            return RemovalResult(path, FAILED, f"trash removal failed: {rm_reason}", pids=pids_list, used_trash=True)
                    else:
                        return RemovalResult(path, FAILED, f"move to trash failed: {trash_reason}", pids=pids_list, used_trash=False)
                except Exception as e:
                    return RemovalResult(path, FAILED, f"fallback to trash failed: {e}", pids=pids_list, used_trash=False)
            else:
                return RemovalResult(path, FAILED, f"worktree removal failed: {err.reason}", pids=pids_list, used_trash=False)
        else:
            return RemovalResult(path, FAILED, "worktree removal failed: unknown error", pids=pids_list, used_trash=False)

    except Exception as e:
        return RemovalResult(path, FAILED, f"worktree removal error: {e}", pids=pids_list, used_trash=False)


def _run_cleanup_hook(
    target_worktree: Path,
    result: CleanupResult,
) -> Optional[Unknown]:
    """
    Run commands.cleanup from the target worktree before it is removed.

    The main worktree is resolved here rather than taken from the caller's cwd; if it is the
    target itself there is no separate trusted copy, so no hook runs.

    Trust note: the hook command comes from main's project.yaml, but any script it invokes lives
    in the branch being cleaned up, which the work under cleanup could have changed.

    The hook runs with shell=True (documented usage such as `npm run sandbox:down` and `&&`
    chains), stdin closed, in its own session so a timeout kills the whole process group, and
    inherits the parent environment. Its output tail is echoed on failure and may contain secrets;
    hooks should not print secrets.

    Mutates `result` in place: appends to `result.notes` and, on any hook error,
    `result.hook_failures` (kept apart from validation_failures). Returns an Unknown if the hook
    cannot be read, fails, or times out, so the caller leaves the worktree and branch in place.
    Returns None on success or no hook.
    """
    def _fail(msg: str, suffix: str) -> Unknown:
        result.hook_failures.append(msg)
        return Unknown(f"{msg}\n{suffix}")

    main_worktree = _main_worktree_of(target_worktree)
    if main_worktree is None:
        # Git cannot name the main checkout, so there is no trusted project.yaml to read.
        result.notes.append("commands.cleanup skipped: cannot resolve the main worktree")
        return None
    if main_worktree == target_worktree.resolve():
        result.notes.append("commands.cleanup skipped: target is the main worktree")
        return None
    cmd, read_err = _main_cleanup_command(main_worktree)
    if read_err:
        return _fail(read_err, "Worktree kept; fix main's project.yaml and re-run /cleanup.")
    if cmd is None:
        return None
    state_before, status_before = worktree_dirty_state(target_worktree)
    retry = "Worktree kept; fix the teardown and re-run /cleanup."
    try:
        proc = subprocess.Popen(
            cmd, shell=True, cwd=str(target_worktree), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
    except OSError as e:
        return _fail(f"commands.cleanup could not run: {cmd}\n{str(e)[:200]}", retry)
    try:
        stdout, stderr = proc.communicate(timeout=CLEANUP_HOOK_TIMEOUT_SECS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.communicate()
        return _fail(f"commands.cleanup timed out after {CLEANUP_HOOK_TIMEOUT_SECS}s: {cmd}", retry)
    if proc.returncode != 0:
        tail = _failure_tail(stdout, stderr)
        return _fail(f"commands.cleanup failed (exit {proc.returncode}): {cmd}"
                     + ("\n" + tail if tail else ""), retry)
    result.notes.append(f"commands.cleanup ran: {cmd}")
    state_after, status_after = worktree_dirty_state(target_worktree)
    if state_before == UNKNOWN or state_after == UNKNOWN:
        result.notes.append(
            "could not check whether commands.cleanup modified the worktree")
    elif state_after == DIRTY and state_before != DIRTY:
        result.notes.append(
            "commands.cleanup modified the worktree; removal may need --force")
    return None


def _extract_check_commands(cache_data: RepoCacheData) -> List[str]:
    """Extract check commands from repo cache data using build_check_order."""
    from .checks import build_check_order

    if not hasattr(cache_data, "commands") or not isinstance(cache_data.commands, dict):
        return []

    planned_order, _ = build_check_order(cache_data.commands)
    commands = []
    for cmd_type in planned_order:
        cmd_value = cache_data.commands.get(cmd_type)
        if cmd_value:
            commands.append(cmd_value)
    return commands


def _read_repo_cache(path: Path) -> Tuple[Optional[RepoCacheData], Optional[Unknown]]:
    """Helper to read repo cache (simplified)."""
    from .cache import read_repo_cache
    return read_repo_cache(path)


def _detect_stacked_children(
    branch: str,
    target_worktree: Path,
    cwd: Optional[Path] = None
) -> Tuple[List[ChildBranchInfo], bool, bool]:
    """
    Detect stacked child branches using two detectors:
    1. Cache scan: find github-cache.json files with stack.parentBranch == branch
    2. gh pr list: find open PRs targeting branch
    Union and dedup by branch name.

    Returns (children_list, gh_lookup_failed, detection_incomplete).

    Raises worktrees.WorktreeParentError (or its subclass WorktreeListError) when
    the detected worktree parent is unsafe (the main worktree itself or under
    ~/.claude). These exceptions are not swallowed: the caller (plan_cleanup)
    catches and reports them as Unknown, so cleanup fails instead of planning as if
    there were no stacked children.
    """
    from . import worktrees

    detected: Dict[str, ChildBranchInfo] = {}
    gh_lookup_failed = False
    detection_incomplete = False

    try:
        worktree_parent_str = worktrees.detect_worktree_parent(cwd)
        if not worktree_parent_str:
            return [], False, False

        worktree_parent = Path(worktree_parent_str)
        if not worktree_parent.is_dir():
            return [], False, False
    except worktrees.WorktreeParentError:
        raise
    except Exception:
        return [], False, False

    try:
        porcelain = git.get_worktree_list_porcelain(cwd=cwd)
        worktree_list = worktrees.parse_worktree_list(porcelain)
        worktree_map = {wt_path: wt_branch for wt_path, wt_branch in worktree_list}
        branch_to_path = {wt_branch: wt_path for wt_path, wt_branch in worktree_list}
    except Exception:
        worktree_map = {}
        branch_to_path = {}

    try:
        for cache_file in worktree_parent.rglob("github-cache.json"):
            if not cache_file.is_file():
                continue
            rel_parts = cache_file.relative_to(worktree_parent).parts
            if len(rel_parts) > 3:
                continue

            try:
                cache_data, cache_err = read_github_cache(cache_file)
                if cache_err or not cache_data or not cache_data.stack:
                    continue

                if cache_data.stack.parent_branch != branch:
                    continue

                worktree_dir = cache_file.parent.parent
                if not worktree_dir.is_dir():
                    continue

                worktree_path = str(worktree_dir)
                child_branch = worktree_map.get(worktree_path)

                if child_branch:
                    if child_branch not in detected:
                        detected[child_branch] = ChildBranchInfo(
                            branch=child_branch,
                            pr_number=cache_data.stack.parent_pr if cache_data.stack else None,
                            worktree_path=worktree_path
                        )
            except Exception:
                detection_incomplete = True
                continue
    except Exception:
        pass

    try:
        pr_list = git.pr_list_json(branch, ["number", "headRefName"], cwd=cwd)
        for pr_record in pr_list:
            pr_branch = pr_record.get("headRefName")
            pr_number = pr_record.get("number")

            if pr_branch and pr_branch not in detected:
                detected[pr_branch] = ChildBranchInfo(
                    branch=pr_branch,
                    pr_number=pr_number,
                    worktree_path=branch_to_path.get(pr_branch)
                )
            elif pr_branch and pr_branch in detected and pr_number:
                detected[pr_branch].pr_number = pr_number
    except Exception:
        gh_lookup_failed = True

    return list(detected.values()), gh_lookup_failed, detection_incomplete


def _hash_plan(plan_json: str) -> str:
    """Compute a hash of the plan for freshness detection."""
    return hash_file_content(plan_json)
