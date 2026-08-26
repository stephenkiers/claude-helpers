"""
Centralized git/gh subprocess wrapper using argument arrays.

All git/gh invocation goes through these functions — never shell=True or
string interpolation into a shell command. Includes timeouts.
"""

import subprocess
import json
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any


DEFAULT_TIMEOUT = 30


def run_git_command(
    args: List[str],
    cwd: Optional[Path] = None,
    timeout: int = DEFAULT_TIMEOUT,
    check: bool = True
) -> str:
    """
    Run a git command with argument array. Returns stdout, raises on error if check=True.
    """
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        if check:
            raise
        return e.stdout.strip()
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git command timed out after {timeout}s: {' '.join(args)}")


def run_gh_command(
    args: List[str],
    cwd: Optional[Path] = None,
    timeout: int = DEFAULT_TIMEOUT,
    check: bool = True
) -> str:
    """
    Run a gh command with argument array. Returns stdout, raises on error if check=True.
    """
    try:
        result = subprocess.run(
            ["gh"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        if check:
            raise
        return e.stdout.strip()
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"gh command timed out after {timeout}s: {' '.join(args)}")


def get_current_branch(cwd: Optional[Path] = None) -> str:
    """Get the current branch name."""
    return run_git_command(["branch", "--show-current"], cwd=cwd)


def get_default_branch(cwd: Optional[Path] = None) -> str:
    """
    Get the default branch from origin/HEAD symbolic-ref.
    Falls back to 'main' if not found.
    """
    try:
        ref = run_git_command(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd=cwd, check=False)
        if ref:
            return ref.split("/")[-1]
    except Exception:
        pass
    return "main"


def get_head_sha(cwd: Optional[Path] = None) -> str:
    """Get the SHA of HEAD."""
    return run_git_command(["rev-parse", "HEAD"], cwd=cwd)


def get_worktree_list_porcelain(cwd: Optional[Path] = None) -> str:
    """Get git worktree list --porcelain output."""
    return run_git_command(["worktree", "list", "--porcelain"], cwd=cwd)


def rev_parse(args: List[str], cwd: Optional[Path] = None) -> str:
    """Run git rev-parse with given args."""
    return run_git_command(["rev-parse"] + args, cwd=cwd)


def get_git_dir(cwd: Optional[Path] = None) -> str:
    """Get the .git directory path."""
    return run_git_command(["rev-parse", "--git-dir"], cwd=cwd)


def is_ancestor(ancestor: str, descendant: str, cwd: Optional[Path] = None) -> bool:
    """Check if ancestor is an ancestor of descendant using merge-base."""
    try:
        run_git_command(
            ["merge-base", "--is-ancestor", ancestor, descendant],
            cwd=cwd,
            check=True
        )
        return True
    except subprocess.CalledProcessError:
        return False


def rev_list_count(rev_range: str, cwd: Optional[Path] = None) -> int:
    """Count commits in a revision range (e.g., 'branch..HEAD')."""
    try:
        output = run_git_command(["rev-list", "--count", rev_range], cwd=cwd)
        return int(output)
    except (ValueError, subprocess.CalledProcessError):
        return -1


def repo_view_json(json_args: List[str], cwd: Optional[Path] = None) -> Dict[str, Any]:
    """Run 'gh repo view --json <args>' and return parsed JSON."""
    try:
        output = run_gh_command(["repo", "view", "--json"] + json_args, cwd=cwd)
        return json.loads(output) if output else {}
    except (json.JSONDecodeError, subprocess.CalledProcessError):
        return {}


def gh_api_user_json(cwd: Optional[Path] = None) -> Dict[str, Any]:
    """Run 'gh api user' and return parsed JSON."""
    try:
        output = run_gh_command(["api", "user"], cwd=cwd)
        return json.loads(output) if output else {}
    except (json.JSONDecodeError, subprocess.CalledProcessError):
        return {}


def pr_view_json(branch: str, json_args: List[str], cwd: Optional[Path] = None) -> Dict[str, Any]:
    """Run 'gh pr view <branch> --json <args>' and return parsed JSON."""
    try:
        output = run_gh_command(["pr", "view", branch, "--json"] + json_args, cwd=cwd, check=False)
        return json.loads(output) if output else {}
    except (json.JSONDecodeError, subprocess.CalledProcessError):
        return {}


def ls_remote_exit_code(ref: str, cwd: Optional[Path] = None) -> int:
    """
    Check if a ref exists on origin via ls-remote.
    Returns 0 if present, non-zero if absent.
    """
    try:
        run_git_command(["ls-remote", "--exit-code", "origin", ref], cwd=cwd, check=True)
        return 0
    except subprocess.CalledProcessError as e:
        return e.returncode
