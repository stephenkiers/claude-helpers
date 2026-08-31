"""
Centralized git/gh subprocess wrapper using argument arrays.

All git/gh invocation goes through these functions — never shell=True or
string interpolation into a shell command. Includes timeouts.
"""

import os
import sys
import subprocess
import json
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any, Union
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


class GitCommandError(subprocess.CalledProcessError):
    """
    A CalledProcessError whose str() also carries the subprocess's stderr.

    subprocess.CalledProcessError.__str__ reports only the exit status, so the
    f"...: {e}" wrapping used throughout this module discarded git's own
    diagnostic ("contains modified or untracked files, use --force to delete
    it", "is not a working tree", ...). Callers that branch on *why* a command
    failed therefore never matched: cleanup.py's dirty-tree retry grepped
    err.reason for "modified or untracked", which could not appear, so the
    forced-removal fallback was unreachable dead code.

    Subclassing CalledProcessError (rather than raising a new exception type)
    keeps every existing `except subprocess.CalledProcessError` call site
    working unchanged.
    """

    def __str__(self) -> str:
        base = super().__str__()
        detail = (self.stderr or "").strip() or (self.output or "").strip()
        return f"{base}: {detail}" if detail else base


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
    except subprocess.CalledProcessError as e:
        # Re-raise carrying stderr so callers can see (and branch on) the real reason.
        raise GitCommandError(e.returncode, e.cmd, output=e.output, stderr=e.stderr) from None
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


def get_git_common_dir(cwd: Optional[Path] = None) -> str:
    """Get git rev-parse --git-common-dir output."""
    return run_git_command(["rev-parse", "--git-common-dir"], cwd=cwd)


def is_linked_worktree(cwd: Optional[Path] = None) -> bool:
    """True if cwd is inside a linked worktree (not main/bare)."""
    resolved_cwd = Path(cwd or Path.cwd()).resolve()
    git_dir = get_git_dir(cwd=cwd)
    git_common_dir = get_git_common_dir(cwd=cwd)

    # Resolve both paths, handling relative paths by resolving relative to cwd
    git_dir_resolved = (resolved_cwd / git_dir).resolve() if not Path(git_dir).is_absolute() else Path(git_dir).resolve()
    git_common_dir_resolved = (resolved_cwd / git_common_dir).resolve() if not Path(git_common_dir).is_absolute() else Path(git_common_dir).resolve()

    return git_dir_resolved != git_common_dir_resolved


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


def branch_exists(branch: str, cwd: Optional[Path] = None) -> Tuple[bool, Optional[Unknown]]:
    """
    Check if a branch exists.

    Returns (True, None) if the branch exists, (False, None) if it doesn't,
    or (False, Unknown(...)) if the check itself fails.
    """
    try:
        run_git_command(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=cwd,
            check=True
        )
        return True, None
    except subprocess.CalledProcessError:
        return False, None
    except Exception as e:
        return False, Unknown(f"branch existence check failed: {e}")


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


def _warn_gh_failure(context: str, e: Exception) -> None:
    """Print a diagnostic for a swallowed gh failure. Stderr only — cli.py's
    output contract is a single JSON blob on stdout."""
    print(f"[workflow.git] {context}: {e}", file=sys.stderr)


def repo_view_json(json_args: List[str], cwd: Optional[Path] = None) -> Dict[str, Any]:
    """Run 'gh repo view --json <args>' and return parsed JSON."""
    try:
        output = run_gh_command(["repo", "view", *_json_flag(json_args)], cwd=cwd)
        return json.loads(output) if output else {}
    except (json.JSONDecodeError, subprocess.CalledProcessError) as e:
        _warn_gh_failure(f"repo_view_json({json_args})", e)
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
        output = run_gh_command(["pr", "view", *_json_flag(json_args), "--", branch], cwd=cwd)
        return json.loads(output) if output else {}
    except (json.JSONDecodeError, subprocess.CalledProcessError) as e:
        _warn_gh_failure(f"pr_view_json({branch!r}, {json_args})", e)
        return {}


def pr_list_json(base_branch: str, json_fields: List[str], cwd: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Run 'gh pr list --base <base_branch> --state open --json <fields>' and return parsed JSON list."""
    try:
        args = ["pr", "list", "--base", base_branch, "--state", "open", *_json_flag(json_fields)]
        output = run_gh_command(args, cwd=cwd)
        return json.loads(output) if output else []
    except (json.JSONDecodeError, subprocess.CalledProcessError) as e:
        _warn_gh_failure(f"pr_list_json({base_branch!r}, {json_fields})", e)
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

    args = ["commit", "-F", str(message_path)]

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


def issue_list_json(json_fields: List[str], cwd: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Run 'gh issue list --state open --json <fields>' and return parsed JSON list."""
    try:
        args = ["issue", "list", "--state", "open", *_json_flag(json_fields)]
        output = run_gh_command(args, cwd=cwd, check=False)
        return json.loads(output) if output else []
    except (json.JSONDecodeError, subprocess.CalledProcessError):
        return []


def issue_create(title: str, body_file: Path, assignee: Optional[str] = None, labels: Optional[List[str]] = None, cwd: Optional[Path] = None) -> Tuple[Optional[str], Optional[Unknown]]:
    """
    Create an issue via 'gh issue create' through the mutation funnel.

    Returns issue URL on success (parsed from gh issue create stdout).

    Args:
        title: Issue title
        body_file: Path to file containing issue body
        assignee: Optional assignee (@me or username); if None, no assignee flag is passed
        labels: Optional list of label names; if None or empty, no label flag is passed
        cwd: Working directory for the gh command

    Returns:
        (issue_url_string, None) on success.
        (None, Unknown(reason)) if the mutation is not allowed or command fails.
    """
    from .mutations import check_mutation_allowed

    # Build args, supporting combinations with/without label and assignee
    if labels and assignee:
        args = ["issue", "create", "--title", title, "--label", ",".join(labels), "--assignee", assignee, "--body-file", str(body_file)]
    elif labels:
        args = ["issue", "create", "--title", title, "--label", ",".join(labels), "--body-file", str(body_file)]
    elif assignee:
        args = ["issue", "create", "--title", title, "--assignee", assignee, "--body-file", str(body_file)]
    else:
        args = ["issue", "create", "--title", title, "--body-file", str(body_file)]

    allowed, reason = check_mutation_allowed(args)
    if not allowed:
        return None, Unknown(reason or "mutation not allowed")

    try:
        output = run_gh_command(args, cwd=cwd)
        # gh issue create outputs the issue URL on success
        return output, None
    except subprocess.CalledProcessError as e:
        return None, Unknown(f"Failed to create issue: {e}")
    except RuntimeError as e:
        return None, Unknown(f"Failed to create issue: {e}")


def issue_comment(number: Union[int, str], body_file: Path, cwd: Optional[Path] = None) -> Tuple[bool, Optional[Unknown]]:
    """
    Add a comment to an issue via 'gh issue comment' through the mutation funnel.

    Args:
        number: Issue number (int) or string ID
        body_file: Path to file containing comment body
        cwd: Working directory for the gh command

    Returns:
        (True, None) on success.
        (False, Unknown(reason)) if the mutation is not allowed or command fails.
    """
    from .mutations import check_mutation_allowed

    args = ["issue", "comment", str(number), "--body-file", str(body_file)]

    allowed, reason = check_mutation_allowed(args)
    if not allowed:
        return False, Unknown(reason or "mutation not allowed")

    try:
        run_gh_command(args, cwd=cwd)
        return True, None
    except subprocess.CalledProcessError as e:
        return False, Unknown(f"Failed to comment on issue {number}: {e}")
    except RuntimeError as e:
        return False, Unknown(f"Failed to comment on issue {number}: {e}")


def issue_edit_body(number: Union[int, str], body_file: Path, cwd: Optional[Path] = None) -> Tuple[bool, Optional[Unknown]]:
    """
    Replace an issue's body via 'gh issue edit' through the mutation funnel.

    Args:
        number: Issue number (int) or string ID
        body_file: Path to file containing new issue body
        cwd: Working directory for the gh command

    Returns:
        (True, None) on success.
        (False, Unknown(reason)) if the mutation is not allowed or command fails.
    """
    from .mutations import check_mutation_allowed

    args = ["issue", "edit", str(number), "--body-file", str(body_file)]

    allowed, reason = check_mutation_allowed(args)
    if not allowed:
        return False, Unknown(reason or "mutation not allowed")

    try:
        run_gh_command(args, cwd=cwd)
        return True, None
    except subprocess.CalledProcessError as e:
        return False, Unknown(f"Failed to edit issue {number}: {e}")
    except RuntimeError as e:
        return False, Unknown(f"Failed to edit issue {number}: {e}")


def add_worktree(path: Path, branch: str, base: Optional[str] = None, cwd: Optional[Path] = None) -> Tuple[bool, Optional[Unknown]]:
    """
    Create a git worktree via 'git worktree add' through the mutation funnel.

    Args:
        path: Path for the new worktree
        branch: Branch name to create/check out
        base: Optional base commit/branch; if None, uses default
        cwd: Working directory for the git command (typically the main worktree)

    Returns:
        (True, None) on success.
        (False, Unknown(reason)) if the mutation is not allowed or command fails.
    """
    from .mutations import check_mutation_allowed

    if base:
        args = ["worktree", "add", "-b", branch, "--", str(path), base]
    else:
        args = ["worktree", "add", "-b", branch, "--", str(path)]

    allowed, reason = check_mutation_allowed(args)
    if not allowed:
        return False, Unknown(reason or "mutation not allowed")

    try:
        run_git_command(args, cwd=cwd)
        return True, None
    except subprocess.CalledProcessError as e:
        return False, Unknown(f"Failed to create worktree at {path}: {e}")
    except RuntimeError as e:
        return False, Unknown(f"Failed to create worktree at {path}: {e}")
