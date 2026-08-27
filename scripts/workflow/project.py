"""
Repository identity, local-plan-mode detection, and current user.

Read-only module for Phase 1.
"""

from pathlib import Path
from typing import Optional, Tuple
from . import git
from .safety import Unknown, fail_closed


@fail_closed
def detect_repo_identity(cwd: Optional[Path] = None) -> Tuple[Optional[Tuple[str, str]], Optional[Unknown]]:
    """
    Detect repository identity (owner/name).

    Returns ((owner, name), None) on success, or (None, Unknown(...)) if
    there's no GitHub remote or the gh call itself failed — those are not
    the same state, so the error channel says which.
    Runs: gh repo view --json nameWithOwner -q '.nameWithOwner'
    """
    try:
        result = git.repo_view_json(["nameWithOwner"], cwd=cwd)
    except Exception as e:
        return None, Unknown(f"gh repo view failed: {e}")

    full_name = result.get("nameWithOwner", "")
    if "/" in full_name:
        owner, name = full_name.split("/", 1)
        return (owner, name), None
    return None, Unknown("no GitHub remote (nameWithOwner not found)")


@fail_closed
def is_local_plan_mode(cwd: Optional[Path] = None) -> Tuple[bool, Optional[Unknown]]:
    """
    Detect local-plan mode (no GitHub remote).

    Returns (True, None) if the repo confidently has no GitHub remote,
    (False, None) if it confidently has one, or (False, Unknown(...)) if
    detection itself failed (distinct from "confirmed no remote").
    """
    identity, err = detect_repo_identity(cwd=cwd)
    if err is not None:
        return False, err
    return identity is None, None


@fail_closed
def detect_current_user(cwd: Optional[Path] = None) -> Tuple[Optional[str], Optional[Unknown]]:
    """
    Detect current GitHub user login.

    Runs: gh api user -q '.login'
    Returns (login, None) or (None, Unknown(...)) if not available.
    """
    try:
        result = git.gh_api_user_json(cwd=cwd)
    except Exception as e:
        return None, Unknown(f"gh api user failed: {e}")

    login = result.get("login")
    if login:
        return login, None
    return None, Unknown("gh api user returned no login")
