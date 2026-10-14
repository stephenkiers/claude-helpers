"""
Worktree parent detection.

Ports ADR-0010's worktree-parent detection logic verbatim.
"""

import os
from pathlib import Path
from typing import Optional, Tuple, List
from . import git


def parse_worktree_list(porcelain_output: str) -> List[Tuple[str, str]]:
    """
    Parse git worktree list --porcelain output.
    Returns list of (worktree_path, worktree_branch) tuples.
    Only includes worktrees with a branch (skips detached HEAD entries).
    """
    worktrees = []
    current_wt = None
    current_branch = None

    for line in porcelain_output.split("\n"):
        if line.startswith("worktree "):
            if current_wt is not None and current_branch is not None:
                worktrees.append((current_wt, current_branch))
            current_wt = line[9:].strip()
            current_branch = None
        elif line.startswith("branch "):
            branch_ref = line[7:].strip()
            if branch_ref.startswith("refs/heads/"):
                current_branch = branch_ref[11:]
            else:
                current_branch = branch_ref

    if current_wt is not None and current_branch is not None:
        worktrees.append((current_wt, current_branch))

    return worktrees


def detect_main_worktree(cwd: Optional[Path] = None) -> Optional[str]:
    """Get the main worktree path (first entry from git worktree list)."""
    try:
        porcelain = git.get_worktree_list_porcelain(cwd=cwd)
        worktrees = parse_worktree_list(porcelain)
        if worktrees:
            return worktrees[0][0]
    except Exception:
        pass
    return None


def _all_worktree_paths(cwd: Optional[Path] = None) -> List[str]:
    """
    All worktree paths in `git worktree list` order, including detached entries.

    Swallows every exception raised while listing worktrees (git not runnable,
    not inside a repository, non-zero exit). The caller sees an empty list, never
    an error, so detect_worktree_parent returns "" and cannot tell "no worktrees"
    apart from "git failed". Callers that need the distinction must check git
    themselves.
    """
    try:
        porcelain = git.get_worktree_list_porcelain(cwd=cwd)
    except Exception:
        return []
    return [
        line[9:].strip()
        for line in porcelain.split("\n")
        if line.startswith("worktree ")
    ]


_TYPE_PREFIXES = {"feature", "fix", "chore"}


class WorktreeParentError(RuntimeError):
    """Detected WORKTREE_PARENT is unsafe (main worktree itself or under ~/.claude)."""


def _is_under(path: str, root: str) -> bool:
    """
    True if path is strictly inside root (component-wise, not a string prefix).

    Both sides are resolved with os.path.realpath before comparing, so symlinked
    spellings of the same directory compare equal.
    """
    real_root = os.path.realpath(root).rstrip("/")
    return os.path.realpath(path).startswith(real_root + "/")


def _home_claude() -> str:
    """Resolved (os.path.realpath) path of ~/.claude."""
    home = os.environ.get("HOME") or os.path.expanduser("~")
    return os.path.realpath(os.path.join(home, ".claude"))


def detect_worktree_parent(cwd: Optional[Path] = None) -> str:
    """
    Detect the worktree parent directory.

    Implements the same precedence as the bash block in
    prompts/worktree-reference.md (ADR-0010 Project Detection, step 3):
    1. Canonical layout first: if main worktree's parent dir is named
       'worktrees', use that directory. No other heuristic applies.
    2. Else take the first non-main worktree that is a usable sibling
       candidate and use its parent directory. Skipped candidates: anything
       under ~/.claude/ (merge-queue scratch, .claude/worktrees, PR-review
       worktrees), anything nested under the main worktree, and any whose
       parent dir is a type prefix (feature/fix/chore).
    3. Else fall back to '<main>/worktrees' (legit only for a bare top-level
       clone with no 'worktrees/' parent).

    Fail closed: raises WorktreeParentError if the result is the main
    worktree itself or lies under ~/.claude/. Returns "" only when no
    worktree can be determined at all.
    """
    paths = _all_worktree_paths(cwd=cwd)
    if not paths:
        return ""
    main = paths[0].rstrip("/") or paths[0]
    real_main = os.path.realpath(main)
    home_claude = _home_claude()

    parent = ""
    if os.path.basename(os.path.dirname(main)) == "worktrees":
        parent = os.path.dirname(main)
    else:
        for cand in paths[1:]:
            if os.path.realpath(cand) == real_main or _is_under(cand, home_claude) or _is_under(cand, main):
                continue
            cand_parent = os.path.dirname(cand)
            if os.path.basename(cand_parent) in _TYPE_PREFIXES:
                continue
            parent = cand_parent
            break
        else:
            parent = main + "/worktrees"

    real_parent = os.path.realpath(parent)
    if real_parent == real_main or real_parent == home_claude or _is_under(parent, home_claude):
        raise WorktreeParentError(
            f"refusing unsafe WORKTREE_PARENT {parent!r} (main worktree {main!r})"
        )
    return parent


def detect_project_root(cwd: Optional[Path] = None) -> str:
    """
    Detect the project root.

    Logic from ADR-0010 Project Detection, step 6:
    1. If main worktree is under 'worktrees/', project root is parent of parent.
    2. Else project root is the main worktree itself.
    """
    main = detect_main_worktree(cwd=cwd)
    if not main:
        return ""

    main_path = Path(main)
    if main_path.parent.name == "worktrees":
        return str(main_path.parent.parent)
    return str(main_path)


def is_in_git_repo(cwd: Optional[Path] = None) -> bool:
    """Check if we're currently in a git repository."""
    try:
        git.get_git_dir(cwd=cwd)
        return True
    except Exception:
        return False
