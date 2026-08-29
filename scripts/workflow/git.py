"""
Centralized git/gh subprocess wrapper using argument arrays.

All git/gh invocation goes through these functions — never shell=True or
string interpolation into a shell command. Includes timeouts.
"""

import os
import subprocess
import json
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any
from .safety import Unknown, fail_closed


DEFAULT_TIMEOUT = 30

# Git resolves which repository/worktree to operate on via these vars before
# consulting cwd at all. Git sets GIT_DIR automatically inside hooks, and any
# of them can leak from a prior `GIT_DIR=... git ...` invocation in the same
# shell — inheriting them here would silently break cwd-scoped calls (e.g.
# stack.py looping over worktree paths). Stripped only for this known-dangerous
# set, not the whole GIT_ prefix, so callers relying on GIT_AUTHOR_NAME,
# GIT_SSH_COMMAND, etc. are unaffected.
_DANGEROUS_GIT_ENV_VARS = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"
)


def _run(
    executable: str,
    args: List[str],
    cwd: Optional[Path] = None,
    timeout: int = DEFAULT_TIMEOUT,
    check: bool = True
) -> str:
    """
    Run a git/gh command with argument array. Returns stdout, raises on
    error if check=True.
    """
    env = {k: v for k, v in os.environ.items() if k not in _DANGEROUS_GIT_ENV_VARS}
    try:
        result = subprocess.run(
            [executable] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
            env=env
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{executable} command timed out after {timeout}s: {' '.join(args)}")


def run_git_command(
    args: List[str],
    cwd: Optional[Path] = None,
    timeout: int = DEFAULT_TIMEOUT,
    check: bool = True
) -> str:
    """
    Run a git command with argument array. Returns stdout, raises on error if check=True.
    """
    return _run("git", args, cwd=cwd, timeout=timeout, check=check)


def run_gh_command(
    args: List[str],
    cwd: Optional[Path] = None,
    timeout: int = DEFAULT_TIMEOUT,
    check: bool = True
) -> str:
    """
    Run a gh command with argument array. Returns stdout, raises on error if check=True.
    """
    return _run("gh", args, cwd=cwd, timeout=timeout, check=check)


def get_current_branch(cwd: Optional[Path] = None) -> str:
    """Get the current branch name."""
    return run_git_command(["branch", "--show-current"], cwd=cwd)


@fail_closed
def get_default_branch(cwd: Optional[Path] = None) -> Tuple[Optional[str], Optional[Unknown]]:
    """
    Get the default branch.

    1. Try origin/HEAD symbolic-ref (most authoritative).
    2. Probe local refs/heads/main, then refs/heads/master — never guess a
       name that doesn't exist locally.
    3. If neither resolves, return (None, Unknown(...)) rather than a
       load-bearing guess.
    """
    try:
        ref = run_git_command(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd=cwd, check=False)
        if ref:
            return ref.split("/")[-1], None
    except RuntimeError:
        pass

    for candidate in ("main", "master"):
        try:
            run_git_command(["show-ref", "--verify", "--quiet", f"refs/heads/{candidate}"], cwd=cwd, check=True)
            return candidate, None
        except subprocess.CalledProcessError:
            continue
        except RuntimeError:
            break

    return None, Unknown("could not determine default branch: no origin/HEAD and no local main or master")


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
            ["merge-base", "--is-ancestor", "--end-of-options", ancestor, descendant],
            cwd=cwd,
            check=True
        )
        return True
    except subprocess.CalledProcessError:
        return False


def rev_list_count(rev_range: str, cwd: Optional[Path] = None) -> int:
    """Count commits in a revision range (e.g., 'branch..HEAD')."""
    try:
        output = run_git_command(["rev-list", "--count", "--end-of-options", rev_range], cwd=cwd)
        return int(output)
    except (ValueError, subprocess.CalledProcessError):
        return -1


def _json_flag(fields: List[str]) -> List[str]:
    """Build a '--json <comma-joined-fields>' argv fragment for gh commands."""
    return ["--json", ",".join(fields)]


def repo_view_json(json_args: List[str], cwd: Optional[Path] = None) -> Dict[str, Any]:
    """Run 'gh repo view --json <args>' and return parsed JSON."""
    try:
        output = run_gh_command(["repo", "view", *_json_flag(json_args)], cwd=cwd)
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
        output = run_gh_command(["pr", "view", *_json_flag(json_args), "--", branch], cwd=cwd, check=False)
        return json.loads(output) if output else {}
    except (json.JSONDecodeError, subprocess.CalledProcessError):
        return {}


def pr_list_json(base_branch: str, json_fields: List[str], cwd: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Run 'gh pr list --base <base_branch> --state open --json <fields>' and return parsed JSON list."""
    try:
        args = ["pr", "list", "--base", base_branch, "--state", "open", *_json_flag(json_fields)]
        output = run_gh_command(args, cwd=cwd, check=False)
        return json.loads(output) if output else []
    except (json.JSONDecodeError, subprocess.CalledProcessError):
        return []


def ls_remote_exit_code(ref: str, cwd: Optional[Path] = None) -> int:
    """
    Check if a ref exists on origin via ls-remote.
    Returns 0 if present, non-zero if absent.
    """
    try:
        run_git_command(["ls-remote", "--exit-code", "origin", "--", ref], cwd=cwd, check=True)
        return 0
    except subprocess.CalledProcessError as e:
        return e.returncode


def remove_worktree(path: Path, force: bool = False, cwd: Optional[Path] = None) -> Tuple[bool, Optional[Unknown]]:
    """
    Remove a git worktree via the mutation funnel.

    Args:
        path: Path to the worktree to remove.
        force: If True, use --force flag (git worktree remove --force).
        cwd: Working directory for the git command.

    Returns:
        (True, None) on success.
        (False, Unknown(reason)) if the mutation is not allowed or command fails.
    """
    from .mutations import check_mutation_allowed

    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append("--")
    args.append(str(path))

    allowed, reason = check_mutation_allowed(args)
    if not allowed:
        return False, Unknown(reason or "mutation not allowed")

    try:
        run_git_command(args, cwd=cwd)
        return True, None
    except subprocess.CalledProcessError as e:
        return False, Unknown(f"Failed to remove worktree {path}: {e}")
    except RuntimeError as e:
        return False, Unknown(f"Failed to remove worktree {path}: {e}")


def delete_branch(name: str, force: bool = False, cwd: Optional[Path] = None) -> Tuple[bool, Optional[Unknown]]:
    """
    Delete a git branch via the mutation funnel.

    Args:
        name: Branch name to delete.
        force: If True, use -D flag (force delete); if False, use -d (safe delete).
        cwd: Working directory for the git command.

    Returns:
        (True, None) on success.
        (False, Unknown(reason)) if the mutation is not allowed or command fails.
    """
    from .mutations import check_mutation_allowed

    flag = "-D" if force else "-d"
    args = ["branch", flag, "--", name]

    allowed, reason = check_mutation_allowed(args)
    if not allowed:
        return False, Unknown(reason or "mutation not allowed")

    try:
        run_git_command(args, cwd=cwd)
        return True, None
    except subprocess.CalledProcessError as e:
        return False, Unknown(f"Failed to delete branch {name}: {e}")
    except RuntimeError as e:
        return False, Unknown(f"Failed to delete branch {name}: {e}")


def pull_ff_only(remote: str, branch: str, cwd: Optional[Path] = None) -> Tuple[bool, Optional[Unknown]]:
    """
    Pull from remote branch with --ff-only flag via the mutation funnel.

    Args:
        remote: Remote name (e.g., "origin").
        branch: Branch name to pull.
        cwd: Working directory for the git command.

    Returns:
        (True, None) on success.
        (False, Unknown(reason)) if the mutation is not allowed or command fails.
    """
    from .mutations import check_mutation_allowed

    args = ["pull", "--ff-only", "--", remote, branch]

    allowed, reason = check_mutation_allowed(args)
    if not allowed:
        return False, Unknown(reason or "mutation not allowed")

    try:
        run_git_command(args, cwd=cwd)
        return True, None
    except subprocess.CalledProcessError as e:
        return False, Unknown(f"Failed to pull {remote} {branch}: {e}")
    except RuntimeError as e:
        return False, Unknown(f"Failed to pull {remote} {branch}: {e}")


def pr_merge_squash(pr_number: int, cwd: Optional[Path] = None) -> Tuple[bool, Optional[Unknown]]:
    """
    Merge a PR via 'gh pr merge --squash' through the mutation funnel.

    Args:
        pr_number: PR number to merge.
        cwd: Working directory for the gh command.

    Returns:
        (True, None) on success.
        (False, Unknown(reason)) if the mutation is not allowed or command fails.
    """
    from .mutations import check_mutation_allowed

    args = ["pr", "merge", "--squash", str(pr_number)]

    allowed, reason = check_mutation_allowed(args)
    if not allowed:
        return False, Unknown(reason or "mutation not allowed")

    try:
        run_gh_command(args, cwd=cwd)
        return True, None
    except subprocess.CalledProcessError as e:
        return False, Unknown(f"Failed to merge PR #{pr_number}: {e}")
    except RuntimeError as e:
        return False, Unknown(f"Failed to merge PR #{pr_number}: {e}")


def stage_all(cwd: Optional[Path] = None) -> Tuple[bool, Optional[Unknown]]:
    """
    Stage all changes via 'git add -A' through the mutation funnel.

    Args:
        cwd: Working directory for the git command.

    Returns:
        (True, None) on success.
        (False, Unknown(reason)) if the mutation is not allowed or command fails.
    """
    from .mutations import check_mutation_allowed

    args = ["add", "-A"]

    allowed, reason = check_mutation_allowed(args)
    if not allowed:
        return False, Unknown(reason or "mutation not allowed")

    try:
        run_git_command(args, cwd=cwd)
        return True, None
    except subprocess.CalledProcessError as e:
        return False, Unknown(f"Failed to stage all changes: {e}")
    except RuntimeError as e:
        return False, Unknown(f"Failed to stage all changes: {e}")


def commit_with_message_file(message_path: Path, cwd: Optional[Path] = None) -> Tuple[bool, Optional[Unknown]]:
    """
    Commit with message from file via 'git commit -F <path>' through the mutation funnel.

    Args:
        message_path: Path to file containing commit message.
        cwd: Working directory for the git command.

    Returns:
        (True, None) on success.
        (False, Unknown(reason)) if the mutation is not allowed or command fails.
    """
    from .mutations import check_mutation_allowed

    args = ["commit", "-F", "--", str(message_path)]

    allowed, reason = check_mutation_allowed(args)
    if not allowed:
        return False, Unknown(reason or "mutation not allowed")

    try:
        run_git_command(args, cwd=cwd)
        return True, None
    except subprocess.CalledProcessError as e:
        return False, Unknown(f"Failed to commit: {e}")
    except RuntimeError as e:
        return False, Unknown(f"Failed to commit: {e}")


def push_upstream(remote: str, branch: str, cwd: Optional[Path] = None) -> Tuple[bool, Optional[Unknown]]:
    """
    Push to upstream via 'git push -u <remote> <branch>' through the mutation funnel.

    Plain push only (no force/force-with-lease). On non-fast-forward rejection (detected via
    stderr inspection), returns Unknown with a descriptive message. Other failures also return
    Unknown but do not retry or force.

    Args:
        remote: Remote name (e.g., "origin").
        branch: Branch name to push.
        cwd: Working directory for the git command.

    Returns:
        (True, None) on success.
        (False, Unknown(reason)) on any failure (including non-fast-forward).
    """
    from .mutations import check_mutation_allowed

    args = ["push", "-u", remote, branch]

    allowed, reason = check_mutation_allowed(args)
    if not allowed:
        return False, Unknown(reason or "mutation not allowed")

    try:
        run_git_command(args, cwd=cwd)
        return True, None
    except subprocess.CalledProcessError as e:
        # Inspect stderr for non-fast-forward rejection
        stderr = getattr(e, 'stderr', '')
        if stderr and any(phrase in stderr.lower() for phrase in ['rejected', 'non-fast-forward', 'diverged']):
            return False, Unknown(f"push rejected — remote has diverged; a rebase or stacked-push flow is needed")
        return False, Unknown(f"Failed to push {remote} {branch}: {e}")
    except RuntimeError as e:
        return False, Unknown(f"Failed to push {remote} {branch}: {e}")


def pr_create(title: str, body_file: Path, base: Optional[str] = None, cwd: Optional[Path] = None) -> Tuple[Optional[str], Optional[Unknown]]:
    """
    Create a PR via 'gh pr create' through the mutation funnel.

    Returns PR URL on success (parsed from gh pr create stdout).

    Args:
        title: PR title.
        body_file: Path to file containing PR body.
        base: Optional base branch (for stacked PRs); if None, uses repo default.
        cwd: Working directory for the gh command.

    Returns:
        (pr_url_string, None) on success.
        (None, Unknown(reason)) if the mutation is not allowed or command fails.
    """
    from .mutations import check_mutation_allowed

    if base:
        args = ["pr", "create", "--title", title, "--base", base, "--body-file", str(body_file)]
    else:
        args = ["pr", "create", "--title", title, "--body-file", str(body_file)]

    allowed, reason = check_mutation_allowed(args)
    if not allowed:
        return None, Unknown(reason or "mutation not allowed")

    try:
        output = run_gh_command(args, cwd=cwd)
        # gh pr create outputs the PR URL on success
        return output, None
    except subprocess.CalledProcessError as e:
        return None, Unknown(f"Failed to create PR: {e}")
    except RuntimeError as e:
        return None, Unknown(f"Failed to create PR: {e}")


def pr_edit(pr_number: int, title: str, body_file: Path, cwd: Optional[Path] = None) -> Tuple[bool, Optional[Unknown]]:
    """
    Edit a PR via 'gh pr edit' through the mutation funnel.

    Args:
        pr_number: PR number to edit.
        title: New PR title.
        body_file: Path to file containing new PR body.
        cwd: Working directory for the gh command.

    Returns:
        (True, None) on success.
        (False, Unknown(reason)) if the mutation is not allowed or command fails.
    """
    from .mutations import check_mutation_allowed

    args = ["pr", "edit", str(pr_number), "--title", title, "--body-file", str(body_file)]

    allowed, reason = check_mutation_allowed(args)
    if not allowed:
        return False, Unknown(reason or "mutation not allowed")

    try:
        run_gh_command(args, cwd=cwd)
        return True, None
    except subprocess.CalledProcessError as e:
        return False, Unknown(f"Failed to edit PR #{pr_number}: {e}")
    except RuntimeError as e:
        return False, Unknown(f"Failed to edit PR #{pr_number}: {e}")
