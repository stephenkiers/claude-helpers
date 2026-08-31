"""
Merge plan and apply (Phase 2 of ADR-0013).

Ports the deterministic merge logic from /merge-and-cleanup into a plan/apply pattern:
- plan_merge: resolve PR/worktree, run push gate checks
- apply_merge: execute the 3-path merge gate and write cache
"""

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple

from . import git
from .cache import read_repo_cache, write_cache
from .safety import Unknown, fail_closed


# Default timeout for 'just merge' execution (seconds).
# Override with MERGE_APPLY_TIMEOUT_SECS environment variable.
DEFAULT_MERGE_APPLY_TIMEOUT_SECS = 1800


def merge_lock_path(target_worktree: str) -> Path:
    """
    Resolve the merge-lock path for a target worktree.

    Stored under ~/.claude/state/merge-locks/, keyed by a hash of the worktree's
    resolved absolute path, rather than inside the target worktree itself. The lock
    is a durable "already merged" guard (see apply_merge docstring) that is never
    auto-cleared, so writing it inside the target repo leaves a permanent untracked
    file there — tripping that repo's own dirty-tree push gate on the next
    /merge-and-cleanup run unless that repo's .gitignore is manually patched to
    exclude it (a fix that has to be repeated in every repo this runs against).
    Keeping the lock out of the repo entirely fixes this once, for all repos.
    Lock identity depends on the worktree's resolved absolute path at call time;
    if the worktree is later moved, renamed, or recreated at a different path, it
    will resolve to a different lock.
    """
    resolved = str(Path(target_worktree).resolve())
    digest = hashlib.sha256(resolved.encode()).hexdigest()[:16]
    return Path.home() / ".claude" / "state" / "merge-locks" / f"{digest}.lock"


@dataclass
class MergePlan:
    """Plan for merging a PR."""
    pr_number: int
    head_ref: str
    target_worktree: str
    blocking_failures: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MergePlan":
        """Construct from parsed JSON dict."""
        field_names = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in field_names})


@dataclass
class MergeResult:
    """Result of applying a merge plan."""
    success: bool
    pr_merged: bool = False
    merge_gate_used: str = ""
    error: Optional[Unknown] = None
    cache_write_failed: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        d = asdict(self)
        if self.error:
            d["error"] = str(self.error)
        return d


@fail_closed
def plan_merge(
    arguments: Optional[str] = None,
    cwd: Optional[Path] = None
) -> Tuple[Optional[MergePlan], Optional[Unknown]]:
    """
    Plan a merge operation (push gate validation).

    Resolves PR/worktree from arguments (path mode or PR number), or auto-detects
    from the current linked worktree when arguments is empty/None. Then validates the push
    gate's 4 checks:
    1. Not detached HEAD
    2. No uncommitted/untracked changes
    3. Upstream tracking branch exists
    4. No unpushed commits

    Returns (MergePlan, None) if push gate passes.
    Returns (MergePlan with blocking_failures, None) if push gate fails.
    Returns (None, Unknown(...)) if PR/worktree resolution fails.
    """
    try:
        pr_number: Optional[int] = None
        head_ref: Optional[str] = None
        target_worktree: Optional[str] = None

        effective_cwd = (cwd or Path.cwd()).resolve()

        if not arguments or not arguments.strip():
            if not git.is_linked_worktree(cwd=effective_cwd):
                return None, Unknown(
                    "No argument provided and not in a linked worktree. "
                    "Run from the linked worktree you want to merge, or pass a PR number or worktree path."
                )
            target_worktree = str(effective_cwd)
            pr_number, head_ref, _ = _resolve_pr_from_worktree(target_worktree)
            if not pr_number or not head_ref:
                return None, Unknown(_unresolved_pr_message(target_worktree, is_current=True))

        elif Path(arguments).exists():
            target_worktree = str(Path(arguments).resolve())
            pr_number, head_ref, _ = _resolve_pr_from_worktree(target_worktree)
            if not pr_number or not head_ref:
                return None, Unknown(_unresolved_pr_message(target_worktree, is_current=False))

        else:
            pr_number, head_ref, target_worktree = _resolve_pr_from_number(arguments, cwd)
            if not pr_number or not head_ref or not target_worktree:
                return None, Unknown(f"Could not resolve PR from '{arguments}'")
            target_worktree = str(Path(target_worktree).resolve())

        plan = MergePlan(
            pr_number=pr_number,
            head_ref=head_ref,
            target_worktree=target_worktree
        )

        blocking_failures = _run_push_gate(target_worktree, head_ref, cwd)
        plan.blocking_failures = blocking_failures

        return plan, None

    except Exception as e:
        return None, Unknown(f"plan_merge failed: {e}")


@fail_closed
def apply_merge(plan_json: str, cwd: Optional[Path] = None) -> Tuple[MergeResult, Optional[Unknown]]:
    """
    Apply a merge plan (mutating).

    Executes the 3-path merge gate:
    1. Path 1: 'just merge' (if recipe exists)
    2. Path 2: repo-cache.json check gate + gh pr merge --squash
    3. Path 3: gh pr merge --squash (no gate, with warning marker)

    Note: 'just merge' (Path 1) and the repo-cache check command (Path 2) are deliberately
    NOT routed through mutations.check_mutation_allowed() — they're already-trusted,
    repo-configured content the maintainer wrote (same trust boundary the .md wrapper relied
    on before this CLI existed), not an argv shape this module constructed itself. The funnel
    only guards git/gh calls this module builds directly (gh pr merge, the reentrancy lock).

    Creates/preserves a merge lock file under ~/.claude/state/merge-locks/ (never
    auto-cleared; see merge_lock_path).

    Returns (MergeResult, None) with execution result.
    Returns (MergeResult, Unknown(...)) if a critical error occurs.
    """
    try:
        plan_data = json.loads(plan_json)
        plan = MergePlan.from_dict(plan_data)

        result = MergeResult(success=False)

        if plan.blocking_failures:
            result.error = Unknown(f"Push gate failed: {'; '.join(plan.blocking_failures)}")
            return result, result.error

        lock_file = merge_lock_path(plan.target_worktree)

        try:
            lock_file.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(lock_file.parent, 0o700)
            lock_content = f"PR #{plan.pr_number} locked by merge-and-cleanup at {time.time()}\n"
            lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(lock_fd, lock_content.encode())
            finally:
                os.close(lock_fd)
        except FileExistsError:
            try:
                existing_content = lock_file.read_text()
                result.error = Unknown(f"Merge lock file exists at {lock_file} (concurrent merge or prior failure): {existing_content}")
            except Exception:
                result.error = Unknown(f"Merge lock file exists at {lock_file} (concurrent merge or prior failure)")
            return result, result.error
        except OSError as e:
            result.error = Unknown(f"Failed to create lock file: {e}")
            return result, result.error

        merge_gate_used = ""
        merge_succeeded = False

        if _check_just_merge(plan.target_worktree):
            success, detail = _run_just_merge(plan.target_worktree)
            if success:
                merge_gate_used = "just merge"
                merge_succeeded = True
            else:
                result.error = Unknown(f"'just merge' failed: {detail}")
                return result, result.error

        if not merge_succeeded:
            check_success, gate_applied, check_detail = _run_merge_gate_checks(Path(plan.target_worktree))
            if not check_success:
                if check_detail:
                    result.error = Unknown(f"Merge gate check failed: {check_detail}")
                else:
                    result.error = Unknown("Merge gate check failed")
                return result, result.error
            if gate_applied:
                merge_gate_used = "repo-cache check"

        if not merge_succeeded:
            if merge_gate_used == "":
                merge_gate_used = "gh pr merge (no gate)"
            success, detail = _run_gh_pr_merge(plan.pr_number, cwd)
            if success:
                merge_succeeded = True
            else:
                result.error = Unknown(f"gh pr merge failed: {detail}")
                return result, result.error

        if merge_succeeded:
            cache_success, cache_err = _write_merge_cache(Path(plan.target_worktree))
            if not cache_success:
                result.cache_write_failed = str(cache_err) if cache_err else "unknown cache write failure"
            result.success = True
            result.pr_merged = True
            result.merge_gate_used = merge_gate_used

        return result, None

    except json.JSONDecodeError as e:
        return MergeResult(success=False, error=Unknown(f"Invalid plan JSON: {e}")), None
    except Exception as e:
        return MergeResult(success=False, error=Unknown(f"apply_merge failed: {e}")), None


def _unresolved_pr_message(target_worktree: str, is_current: bool) -> str:
    """Build the 'could not resolve PR' error message, worded correctly for the two call sites."""
    branch_desc = "current branch" if is_current else "worktree's checked-out branch"
    return (
        f"Could not resolve PR from worktree {target_worktree}: "
        f"no cached PR and {branch_desc} has no associated PR (has it been pushed with an open PR?)"
    )


def _resolve_pr_from_worktree(target_worktree: str) -> Tuple[Optional[int], Optional[str], str]:
    """Resolve PR number and head ref from a worktree path."""
    try:
        cache_file = Path(target_worktree) / ".claude" / "github-cache.json"
        if cache_file.exists():
            cache_data = json.loads(cache_file.read_text())
            pr_number = cache_data.get("pr", {}).get("number")
            if pr_number:
                pr_data = git.pr_view_json(str(pr_number), ["headRefName", "state"], cwd=Path(target_worktree))
                if pr_data:
                    return pr_number, pr_data.get("headRefName"), target_worktree

        head_ref = git.get_current_branch(cwd=Path(target_worktree))
        pr_data = git.pr_view_json(head_ref, ["number", "state"], cwd=Path(target_worktree))
        if pr_data:
            return pr_data.get("number"), head_ref, target_worktree

        return None, None, target_worktree
    except Exception:
        return None, None, target_worktree


def _resolve_pr_from_number(arguments: str, cwd: Optional[Path]) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """Resolve PR number and head ref from a PR number or URL."""
    try:
        pr_number = None
        if "/pull/" in arguments:
            match = re.search(r"/pull/(\d+)", arguments)
            if match:
                pr_number = int(match.group(1))
        else:
            match = re.search(r"\d+", arguments)
            if match:
                pr_number = int(match.group(0))

        if not pr_number:
            return None, None, None

        pr_data = git.pr_view_json(str(pr_number), ["headRefName", "state"], cwd=cwd)
        if not pr_data:
            return None, None, None

        head_ref = pr_data.get("headRefName")
        porcelain = git.get_worktree_list_porcelain(cwd=cwd)
        target_worktree = _find_worktree_by_branch(porcelain, head_ref)

        return pr_number, head_ref, target_worktree
    except Exception:
        return None, None, None


def _find_worktree_by_branch(porcelain_output: str, branch: str) -> Optional[str]:
    """Find worktree path that has the given branch checked out."""
    from .worktrees import parse_worktree_list
    worktree_list = parse_worktree_list(porcelain_output)
    for wt_path, wt_branch in worktree_list:
        if wt_branch == branch:
            return wt_path
    return None


def _run_push_gate(target_worktree: str, head_ref: str, cwd: Optional[Path]) -> List[str]:
    """Run push gate checks. Returns list of failure messages (empty if all pass)."""
    failures = []
    wt_path = Path(target_worktree)

    try:
        git.run_git_command(["symbolic-ref", "-q", "HEAD"], cwd=wt_path, check=True)
    except Exception:
        failures.append("Detached HEAD")

    try:
        status = git.run_git_command(["status", "--porcelain"], cwd=wt_path)
        if status:
            failures.append("Uncommitted or untracked changes")
    except Exception:
        failures.append("Could not check status")

    try:
        git.run_git_command(["rev-parse", "@{u}"], cwd=wt_path, check=True)
    except Exception:
        failures.append("No upstream tracking branch")

    unpushed_count = git.rev_list_count("@{u}..", cwd=wt_path)
    if unpushed_count == -1:
        failures.append("Could not determine unpushed commit count")
    elif unpushed_count > 0:
        failures.append(f"{unpushed_count} unpushed commits")

    return failures


def _get_merge_apply_timeout() -> int:
    """
    Resolve the timeout for 'just merge' execution from the environment.

    Reads MERGE_APPLY_TIMEOUT_SECS; returns the value if it's a valid positive
    integer, otherwise returns DEFAULT_MERGE_APPLY_TIMEOUT_SECS.

    Never raises; invalid values silently fall back to the default.
    """
    env_value = os.environ.get("MERGE_APPLY_TIMEOUT_SECS", "").strip()
    if not env_value:
        return DEFAULT_MERGE_APPLY_TIMEOUT_SECS

    try:
        timeout_secs = int(env_value)
        if timeout_secs > 0:
            return timeout_secs
    except (ValueError, TypeError):
        pass

    return DEFAULT_MERGE_APPLY_TIMEOUT_SECS


def _check_just_merge(target_worktree: str) -> bool:
    """Check if 'just merge' recipe exists."""
    try:
        justfile = Path(target_worktree) / "justfile"
        if not justfile.exists():
            return False
        summary = subprocess.run(
            ["just", "-f", str(justfile), "--summary"],
            cwd=target_worktree,
            capture_output=True,
            text=True,
            timeout=5
        )
        if summary.returncode == 0:
            return "merge" in summary.stdout.split()
        return False
    except Exception:
        return False


def _run_just_merge(target_worktree: str) -> Tuple[bool, Optional[str]]:
    """
    Run 'just merge' command.

    Returns (True, None) on success.
    Returns (False, diagnostic_message) on failure.
    """
    try:
        # /merge-and-cleanup now runs this step via a backgrounded Bash call (no harness
        # foreground timeout ceiling), so this timeout is the only remaining limiter — give
        # a full build + E2E boot real headroom instead of cutting it close at 600s.
        # Override with MERGE_APPLY_TIMEOUT_SECS environment variable (default: 1800s).
        timeout_secs = _get_merge_apply_timeout()
        result = subprocess.run(
            ["just", "merge"],
            cwd=target_worktree,
            timeout=timeout_secs,
            capture_output=True,
            text=True,
            check=True
        )
        return True, None
    except subprocess.CalledProcessError as e:
        return False, e.stderr or str(e)
    except Exception as e:
        return False, str(e)


def _run_merge_gate_checks(target_worktree: Path) -> Tuple[bool, bool, Optional[str]]:
    """
    Run repo-cache check gate via run_checks.

    Returns (True, False, None) if no cache exists (gate not applied).
    Returns (True, True, None) if gate passes.
    Returns (False, True, diagnostic_message) if gate fails.
    """
    from .checks import run_checks

    cache_file = target_worktree / ".claude" / "repo-cache.json"
    if not cache_file.exists():
        return True, False, None

    cache_data, err = read_repo_cache(cache_file)
    if err:
        return False, True, str(err)
    if not cache_data:
        return True, False, None

    timeout = _get_merge_apply_timeout()
    result, check_err = run_checks(
        commands=cache_data.commands,
        repo_root=target_worktree,
        timeout=timeout
    )

    if check_err:
        return False, True, str(check_err)

    if not result.all_passed:
        failed_cmd = result.failed_at or "unknown"
        return False, True, f"Check '{failed_cmd}' failed"

    if result.status == "no_checks_ran":
        return False, True, "No checks configured or all checks null"

    return True, True, None


def _run_gh_pr_merge(pr_number: int, cwd: Optional[Path]) -> Tuple[bool, Optional[str]]:
    """
    Run 'gh pr merge --squash' command via the mutation funnel.

    Returns (True, None) on success.
    Returns (False, diagnostic_message) on failure.
    """
    success, err = git.pr_merge_squash(pr_number, cwd=cwd)
    return success, str(err) if err else None


def _write_merge_cache(target_worktree: Path) -> Tuple[bool, Optional[Unknown]]:
    """
    Write merged state to cache using atomic write_cache().

    Returns (True, None) on success.
    Returns (False, Unknown(...)) on failure.
    """
    try:
        cache_file = target_worktree / ".claude" / "github-cache.json"
        cache_file.parent.mkdir(parents=True, exist_ok=True)

        existing = {}
        if cache_file.exists():
            existing = json.loads(cache_file.read_text())

        if "pr" not in existing:
            existing["pr"] = {}
        existing["pr"]["state"] = "MERGED"

        return write_cache(cache_file, existing)
    except Exception as e:
        return False, Unknown(f"Failed to prepare cache for writing: {e}")
