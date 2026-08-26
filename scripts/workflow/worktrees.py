"""
Worktree parent detection and graft detection.

Ports ADR-0010's worktree-parent detection logic verbatim, plus graft detection.
"""

import json
from pathlib import Path
from typing import Optional, Tuple
from . import git
from .safety import Unknown


def parse_worktree_list(porcelain_output: str) -> Tuple[list, list]:
    """
    Parse git worktree list --porcelain output.
    Returns (worktree_paths, worktree_branches).
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


def detect_second_worktree(cwd: Optional[Path] = None) -> Optional[str]:
    """Get the second worktree path, if it exists."""
    try:
        porcelain = git.get_worktree_list_porcelain(cwd=cwd)
        worktrees = parse_worktree_list(porcelain)
        if len(worktrees) > 1:
            return worktrees[1][0]
    except Exception:
        pass
    return None


def detect_worktree_parent(cwd: Optional[Path] = None) -> str:
    """
    Detect the worktree parent directory.

    Logic from ADR-0010 Project Detection, step 3:
    1. If a second worktree exists, use its parent directory.
    2. Else if main worktree is already under 'worktrees/', use that directory.
    3. Else create 'worktrees' as a sibling directory to main worktree.
    """
    second = detect_second_worktree(cwd=cwd)
    if second:
        return str(Path(second).parent)

    main = detect_main_worktree(cwd=cwd)
    if main:
        main_path = Path(main)
        if main_path.parent.name == "worktrees":
            return str(main_path.parent)
        return str(main_path / "worktrees")

    return ""


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


def detect_graft_config_path() -> Path:
    """Get the graft config file path (~/.config/graft/config.json)."""
    xdg_config = Path.home() / ".config"
    return xdg_config / "graft" / "config.json"


def detect_graft_usage(main_worktree: str) -> Tuple[bool, str]:
    """
    Detect if graft manages this repo's worktrees.

    Returns (use_graft: bool, graft_repo_name: str).
    Logic from ADR-0010 Graft Detection block.
    """
    import shutil

    if not shutil.which("graft"):
        return False, ""

    config_path = detect_graft_config_path()
    if not config_path.exists():
        return False, ""

    try:
        config_data = json.loads(config_path.read_text())
        repos = config_data.get("repos", {})
        for repo_name, repo_info in repos.items():
            if repo_info.get("path") == main_worktree:
                return True, repo_name
    except (json.JSONDecodeError, OSError):
        pass

    return False, ""


def is_in_git_repo(cwd: Optional[Path] = None) -> bool:
    """Check if we're currently in a git repository."""
    try:
        git.get_git_dir(cwd=cwd)
        return True
    except Exception:
        return False
