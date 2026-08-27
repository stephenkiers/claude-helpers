"""
Merge plan and apply (Phase 2 of ADR-0013).

Ports the deterministic merge logic from /merge-and-cleanup into a plan/apply pattern:
- plan_merge: resolve PR/worktree, run push gate checks
- apply_merge: execute the 3-path merge gate and write cache
"""

import json
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple

from . import git
from .safety import Unknown, fail_closed


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
        return cls(**{k: v for k, v in data.items() if k in ["pr_number", "head_ref", "target_worktree", "blocking_failures"]})


@dataclass
class MergeResult:
    """Result of applying a merge plan."""
    success: bool
    pr_merged: bool = False
    merge_gate_used: str = ""
    error: Optional[Unknown] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        d = asdict(self)
        if self.error:
            d["error"] = str(self.error)
        return d


@fail_closed
def plan_merge(
    arguments: str,
    cwd: Optional[Path] = None
) -> Tuple[Optional[MergePlan], Optional[Unknown]]:
    """
    Plan a merge operation (push gate validation).

    Resolves PR/worktree from arguments (path mode or PR number), then validates
    the push gate's 4 checks:
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

        if Path(arguments).exists():
            target_worktree = str(Path(arguments).resolve())
            pr_number, head_ref, _ = _resolve_pr_from_worktree(target_worktree, cwd)
            if not pr_number or not head_ref:
                return None, Unknown(f"Could not resolve PR from worktree {target_worktree}")

        else:
            pr_number, head_ref, target_worktree = _resolve_pr_from_number(arguments, cwd)
            if not pr_number or not head_ref or not target_worktree:
                return None, Unknown(f"Could not resolve PR from '{arguments}'")

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

    Creates/preserves .merge-and-cleanup.lock file (never auto-cleared).

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

        lock_file = Path(plan.target_worktree) / ".claude" / ".merge-and-cleanup.lock"
        if lock_file.exists():
            result.error = Unknown(f"Merge lock file exists (concurrent merge or prior failure)")
            return result, result.error

        try:
            lock_file.parent.mkdir(parents=True, exist_ok=True)
            lock_file.write_text(f"PR #{plan.pr_number} locked by merge-and-cleanup\n")
        except Exception as e:
            result.error = Unknown(f"Failed to create lock file: {e}")
            return result, result.error

        merge_gate_used = ""
        merge_succeeded = False

        if _check_just_merge(plan.target_worktree):
            if _run_just_merge(plan.target_worktree, cwd):
                merge_gate_used = "just merge"
                merge_succeeded = True
            else:
                result.error = Unknown("'just merge' failed")
                return result, result.error

        if not merge_succeeded:
            check_cmd = _get_repo_cache_check_cmd(Path(plan.target_worktree))
            if check_cmd:
                if _run_check_command(check_cmd, plan.target_worktree, cwd):
                    merge_gate_used = "repo-cache check"
                else:
                    result.error = Unknown("Merge gate check failed")
                    return result, result.error

        if not merge_succeeded:
            if merge_gate_used == "":
                merge_gate_used = "gh pr merge (no gate)"
            if _run_gh_pr_merge(plan.pr_number, cwd):
                merge_succeeded = True
            else:
                result.error = Unknown("gh pr merge failed")
                return result, result.error

        if merge_gate_used == "just merge":
            merge_succeeded = True
        elif merge_gate_used not in ("", "gh pr merge (no gate)"):
            if not _run_gh_pr_merge(plan.pr_number, cwd):
                result.error = Unknown("gh pr merge failed after gate")
                return result, result.error
            merge_succeeded = True

        if merge_succeeded:
            _write_merge_cache(Path(plan.target_worktree))
            result.success = True
            result.pr_merged = True
            result.merge_gate_used = merge_gate_used

        return result, None

    except json.JSONDecodeError as e:
        return MergeResult(success=False, error=Unknown(f"Invalid plan JSON: {e}")), None
    except Exception as e:
        return MergeResult(success=False, error=Unknown(f"apply_merge failed: {e}")), None


def _resolve_pr_from_worktree(target_worktree: str, cwd: Optional[Path]) -> Tuple[Optional[int], Optional[str], str]:
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
            import re
            match = re.search(r"/pull/(\d+)", arguments)
            if match:
                pr_number = int(match.group(1))
        else:
            import re
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


def _check_just_merge(target_worktree: str) -> bool:
    """Check if 'just merge' recipe exists."""
    try:
        justfile = Path(target_worktree) / "justfile"
        if not justfile.exists():
            return False
        result = git.run_git_command(["--version"], cwd=Path(target_worktree), check=False)
        import re
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


def _run_just_merge(target_worktree: str, cwd: Optional[Path]) -> bool:
    """Run 'just merge' command."""
    try:
        result = subprocess.run(
            ["just", "merge"],
            cwd=target_worktree,
            timeout=600,
            capture_output=True,
            check=True
        )
        return True
    except Exception:
        return False


def _get_repo_cache_check_cmd(target_worktree: Path) -> Optional[str]:
    """Get check command from repo-cache.json if it exists."""
    try:
        cache_file = target_worktree / ".claude" / "repo-cache.json"
        if not cache_file.exists():
            return None
        cache_data = json.loads(cache_file.read_text())
        return cache_data.get("commands", {}).get("check")
    except Exception:
        return None


def _run_check_command(cmd: str, target_worktree: str, cwd: Optional[Path]) -> bool:
    """Run a check command."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=target_worktree,
            timeout=300,
            capture_output=True,
            check=True
        )
        return True
    except Exception:
        return False


def _run_gh_pr_merge(pr_number: int, cwd: Optional[Path]) -> bool:
    """Run 'gh pr merge --squash' command."""
    try:
        result = git.run_gh_command(["pr", "merge", "--squash", str(pr_number)], cwd=cwd, check=True)
        return True
    except Exception:
        return False


def _write_merge_cache(target_worktree: Path) -> None:
    """Write merged state to cache."""
    try:
        cache_file = target_worktree / ".claude" / "github-cache.json"
        cache_file.parent.mkdir(parents=True, exist_ok=True)

        existing = {}
        if cache_file.exists():
            existing = json.loads(cache_file.read_text())

        if "pr" not in existing:
            existing["pr"] = {}
        existing["pr"]["state"] = "MERGED"

        cache_file.write_text(json.dumps(existing))
    except Exception:
        pass
