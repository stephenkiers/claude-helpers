"""
Cleanup plan and apply (Phase 2 of ADR-0013).

Ports the deterministic cleanup logic from /cleanup into a plan/apply pattern:
- plan_cleanup: read-only inspection of target worktree state
- apply_cleanup: execute worktree removal, branch deletion, validation gates
"""

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple

from . import git
from .cache import hash_cache_file, hash_file_content, read_github_cache
from .safety import Unknown, fail_closed
from .models import RepoCacheData
from .merge import merge_lock_path


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
    """Result of applying a cleanup plan."""
    success: bool
    worktree_removed: bool = False
    branch_deleted: bool = False
    validation_passed: bool = True
    validation_failures: List[str] = field(default_factory=list)
    error: Optional[Unknown] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        d = asdict(self)
        if self.error:
            d["error"] = str(self.error)
        return d


@fail_closed
def plan_cleanup(
    target_path: str,
    cwd: Optional[Path] = None
) -> Tuple[Optional[CleanupPlan], Optional[Unknown]]:
    """
    Plan a cleanup operation (read-only).

    Returns (CleanupPlan, None) on success.
    Returns (None, Unknown(...)) if target resolution or state inspection fails.
    """
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

    except Exception as e:
        return None, Unknown(f"plan_cleanup failed: {e}")


@fail_closed
def apply_cleanup(plan_json: str, cwd: Optional[Path] = None) -> Tuple[CleanupResult, Optional[Unknown]]:
    """
    Apply a cleanup plan (mutating).

    Validates freshness, then executes:
    1. Pull main ff-only (from main worktree)
    2. Run validation check commands (non-blocking)
    3. Remove worktree
    4. Delete branch (force if PR_STATE == MERGED)

    Returns (CleanupResult, None) with execution result.
    Returns (CleanupResult, Unknown(...)) if a critical error occurs.
    """
    try:
        plan_data = json.loads(plan_json)
        plan = CleanupPlan.from_dict(plan_data)

        result = CleanupResult(success=False)

        try:
            target_worktree = Path(plan.target_worktree)
            if not target_worktree.exists():
                result.error = Unknown(f"Target worktree no longer exists: {plan.target_worktree}")
                return result, result.error

            current_branch = git.get_current_branch(cwd=target_worktree)
            if current_branch != plan.current_branch:
                result.error = Unknown(f"Branch changed: was {plan.current_branch}, now {current_branch}")
                return result, result.error

            current_head_sha = git.get_head_sha(cwd=target_worktree)
            if current_head_sha != plan.expected_head_sha:
                result.error = Unknown(f"HEAD SHA changed (plan is stale)")
                return result, result.error

            cache_hash = hash_cache_file(Path(plan.target_worktree) / ".claude" / "repo-cache.json")
            if cache_hash != plan.cache_hash:
                result.error = Unknown(f"Cache has changed (plan is stale)")
                return result, result.error

        except Exception as e:
            result.error = Unknown(f"Freshness validation failed: {e}")
            return result, result.error

        main_worktree_path = cwd or Path.cwd()
        try:
            success, err = git.pull_ff_only("origin", "main", cwd=main_worktree_path)
            if not success and err:
                result.validation_passed = False
                result.validation_failures.append(f"Could not fast-forward main: {err.reason}")
        except Exception as e:
            result.validation_passed = False
            result.validation_failures.append(f"Pull main ff-only failed: {e}")

        from .checks import execute_check

        for cmd in plan.check_commands:
            check_result = execute_check(cmd, cwd=main_worktree_path)
            if not check_result.success:
                result.validation_passed = False
                detail = check_result.error or check_result.stderr or f"exit code {check_result.returncode}"
                result.validation_failures.append(f"Check command failed: {cmd}: {detail}")

        # Re-validate HEAD SHA immediately before mutation
        try:
            current_head_sha_recheck = git.get_head_sha(cwd=Path(plan.target_worktree))
            if current_head_sha_recheck != plan.expected_head_sha:
                result.validation_passed = False
                result.error = Unknown("HEAD SHA changed during check-commands execution (plan went stale) — aborting before worktree removal")
                return result, result.error
        except Exception as e:
            result.validation_passed = False
            result.error = Unknown(f"Freshness re-validation before mutation failed: {e}")
            return result, result.error

        try:
            success, err = git.remove_worktree(Path(plan.target_worktree), force=False, cwd=cwd)
            if success:
                result.worktree_removed = True
            elif err:
                result.validation_failures.append(f"Worktree removal failed: {err.reason}")
                is_dirty_tree_failure = err.reason and ("dirty" in err.reason.lower() or "modified or untracked" in err.reason.lower())
                if is_dirty_tree_failure:
                    try:
                        success, err = git.remove_worktree(Path(plan.target_worktree), force=True, cwd=cwd)
                        if success:
                            result.worktree_removed = True
                    except Exception as e:
                        result.validation_failures.append(f"Forced worktree removal error: {e}")
        except Exception as e:
            result.validation_failures.append(f"Worktree removal error: {e}")

        if result.worktree_removed:
            try:
                merge_lock_path(str(plan.target_worktree)).unlink(missing_ok=True)
            except Exception as e:
                result.validation_failures.append(f"Merge lock cleanup failed (non-fatal): {e}")

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
        return result, None

    except json.JSONDecodeError as e:
        return CleanupResult(success=False, error=Unknown(f"Invalid plan JSON: {e}")), None
    except Exception as e:
        return CleanupResult(success=False, error=Unknown(f"apply_cleanup failed: {e}")), None


def _extract_check_commands(cache_data: RepoCacheData) -> List[str]:
    """Extract check commands from repo cache data."""
    commands = []
    if hasattr(cache_data, "commands") and isinstance(cache_data.commands, dict):
        order = ["format", "check", "vet", "test", "build"]
        for key in order:
            if key in cache_data.commands and cache_data.commands[key]:
                commands.append(cache_data.commands[key])
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
