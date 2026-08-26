"""
Repository identity, local-plan-mode detection, and current user.

Read-only module for Phase 1.
"""

from pathlib import Path
from typing import Optional, Tuple
from . import git


def detect_repo_identity(cwd: Optional[Path] = None) -> Optional[Tuple[str, str]]:
    """
    Detect repository identity (owner/name).

    Returns (owner, name) tuple, or None if no GitHub remote.
    Runs: gh repo view --json nameWithOwner -q '.nameWithOwner'
    """
    try:
        result = git.repo_view_json(["nameWithOwner"], cwd=cwd)
        full_name = result.get("nameWithOwner", "")
        if "/" in full_name:
            owner, name = full_name.split("/", 1)
            return (owner, name)
    except Exception:
        pass
    return None


def is_local_plan_mode(cwd: Optional[Path] = None) -> bool:
    """
    Detect local-plan mode (no GitHub remote).

    Returns True if repo has no GitHub remote (no REPO identity).
    """
    return detect_repo_identity(cwd=cwd) is None


def detect_current_user(cwd: Optional[Path] = None) -> Optional[str]:
    """
    Detect current GitHub user login.

    Runs: gh api user -q '.login'
    Returns login or None if not available.
    """
    try:
        result = git.gh_api_user_json(cwd=cwd)
        return result.get("login")
    except Exception:
        pass
    return None
