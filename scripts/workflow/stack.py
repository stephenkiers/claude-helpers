"""
Stack detection (single-driver vs per-branch layout).

Ports ADR-0011's stack detection logic verbatim:
- is_stacked: cache-first, falls back to ancestor search
- detect_layout: structural detection (per-branch if any sibling checked out)
"""

from pathlib import Path
from typing import Optional, Tuple
from .worktrees import parse_worktree_list
from .cache import read_github_cache
from .safety import Unknown, fail_closed
from . import git


StackLayout = str


@fail_closed
def is_stacked(
    branch: Optional[str] = None,
    cwd: Optional[Path] = None,
    cache_path: Optional[Path] = None
) -> Tuple[bool, Optional[str], Optional[int], Optional[Unknown]]:
    """
    Detect whether the current (or specified) branch's parent is another branch.

    Returns (is_stacked, parent_branch, parent_pr, error). error is None on
    a confident result (found or confirmed-not-stacked); it's an Unknown
    when ancestor-search detection itself failed, so a real "not stacked"
    is never indistinguishable from "couldn't tell".

    Logic from ADR-0011 Is-stacked block:
    1. Check cache first (most specific) at .claude/github-cache.json
    2. If cache says stacked=true, use cached parent info
    3. If cache says stacked=false, return not stacked
    4. Else, detect by finding nearest-ancestor worktree branch via merge-base
    """
    if branch is None:
        branch = git.get_current_branch(cwd=cwd)

    default_branch, _ = git.get_default_branch(cwd=cwd)

    cache_result = None
    if cache_path:
        cache_result, _ = read_github_cache(cache_path)

    if cache_result and cache_result.stack is not None and cache_result.stack.is_stacked:
        return (
            True,
            cache_result.stack.parent_branch,
            cache_result.stack.parent_pr,
            None
        )

    if cache_result and cache_result.stack is not None and not cache_result.stack.is_stacked:
        return False, None, None, None

    try:
        porcelain = git.get_worktree_list_porcelain(cwd=cwd)
        worktrees = parse_worktree_list(porcelain)

        best_ancestor = None
        best_distance = None

        for wt_path, wt_branch in worktrees:
            if not wt_branch or wt_branch == default_branch or wt_branch == branch:
                continue

            if git.is_ancestor(wt_branch, "HEAD", cwd=cwd):
                distance = git.rev_list_count(f"{wt_branch}..HEAD", cwd=cwd)
                if distance >= 0:
                    if best_distance is None or distance < best_distance:
                        best_ancestor = wt_branch
                        best_distance = distance

        if best_ancestor:
            parent_pr = None
            pr_lookup_failed = False
            try:
                pr_data = git.pr_view_json(best_ancestor, ["number"], cwd=cwd)
                parent_pr = pr_data.get("number")
            except Exception:
                pr_lookup_failed = True

            error = Unknown(f"PR lookup for parent branch {best_ancestor!r} failed") if pr_lookup_failed else None
            return True, best_ancestor, parent_pr, error

    except Exception as e:
        return False, None, None, Unknown(f"ancestor-search stack detection failed: {e}")

    return False, None, None, None


def detect_layout(
    subject_branch: Optional[str] = None,
    cwd: Optional[Path] = None,
    worktree_dir: Optional[Path] = None
) -> StackLayout:
    """
    Detect the worktree layout for stacked-push routing.

    Returns "single-driver" | "per-branch" | "unknown".

    Precondition: Run is_stacked() first to know if we're stacked.
    Logic from ADR-0011 Detect layout block:
    1. Build branch<TAB>worktree map from git worktree list --porcelain
    2. For subject_branch, find its parent (from cache or is_stacked result)
    3. Check if any OTHER worktree has that parent branch checked out → per-branch
    4. Check if any OTHER worktree's cache has subject as their parent → per-branch
    5. If subject has known parent and no siblings checked out → single-driver
    6. Else (stacked but no parent or sibling info) → unknown (fail closed)
    """
    current_branch = git.get_current_branch(cwd=cwd)
    if subject_branch is None:
        subject_branch = current_branch

    default_branch, _ = git.get_default_branch(cwd=cwd)

    try:
        porcelain = git.get_worktree_list_porcelain(cwd=cwd)
        worktrees = parse_worktree_list(porcelain)

        subject_parent = None
        worktree_branches = {}

        for wt_path, wt_branch in worktrees:
            if wt_branch:
                worktree_branches[wt_branch] = wt_path

            if wt_branch == subject_branch:
                if wt_path and Path(wt_path).is_dir():
                    cache_path = Path(wt_path) / ".claude" / "github-cache.json"
                    cache_data, _ = read_github_cache(cache_path)
                    if cache_data and cache_data.stack and cache_data.stack.parent_branch:
                        subject_parent = cache_data.stack.parent_branch

        if subject_parent is None and subject_branch == current_branch:
            is_stack, parent_branch, _, _ = is_stacked(subject_branch, cwd=cwd, cache_path=None)
            if is_stack and parent_branch:
                subject_parent = parent_branch

        per_branch = False
        for wt_path, wt_branch in worktrees:
            if not wt_branch or wt_branch == subject_branch or wt_branch == default_branch:
                continue

            if subject_parent and wt_branch == subject_parent:
                per_branch = True
                break

            if wt_path and Path(wt_path).is_dir():
                try:
                    cache_path = Path(wt_path) / ".claude" / "github-cache.json"
                    cache_data, _ = read_github_cache(cache_path)
                    if cache_data and cache_data.stack and cache_data.stack.parent_branch == subject_branch:
                        per_branch = True
                        break
                except OSError:
                    pass

        if per_branch:
            return "per-branch"
        elif subject_parent:
            return "single-driver"
        else:
            return "unknown"

    except Exception:
        return "unknown"
