"""
Thin, explicit wrappers over git for Flow v3. Every call is a subprocess with an argument list (no
shell). Nothing here ever force-pushes: Flow v3 merges base into the branch rather than rewriting history.
"""

import fnmatch
import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence, Set, Tuple


class GitError(Exception):
    """A git command failed. The message carries the command and its stderr."""


def git(args: List[str], cwd: Path, check: bool = True) -> str:
    """
    Run `git <args>` in cwd and return stripped stdout.

    Raises GitError (with stderr) when check is true and git exits non-zero. With check false the
    stdout is returned whatever the exit code.
    """
    try:
        proc = subprocess.run(["git"] + list(args), cwd=str(cwd), stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True)
    except OSError as exc:
        raise GitError("git %s: %s" % (" ".join(args), exc))
    if check and proc.returncode != 0:
        raise GitError("git %s failed (exit %d): %s"
                       % (" ".join(args), proc.returncode, proc.stderr.strip()))
    return proc.stdout.strip()


def _git_rc(args: List[str], cwd: Path) -> Tuple[int, str, str]:
    proc = subprocess.run(["git"] + list(args), cwd=str(cwd), stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    return proc.returncode, proc.stdout, proc.stderr


def head_sha(cwd: Path) -> str:
    return git(["rev-parse", "HEAD"], cwd)


def tree_id(cwd: Path, ref: str = "HEAD") -> str:
    """The git tree id of ref (what a check ran against)."""
    return git(["rev-parse", ref + "^{tree}"], cwd)


def worktree_path_for(repo_root: Path, issue: int, slug: str) -> Path:
    """A sibling of repo_root named <issue>-<slug>."""
    return repo_root.parent / ("%d-%s" % (issue, slug))


def _identity_args(cwd: Path) -> List[str]:
    """`-c` options supplying a fallback committer identity only when the repo has none."""
    args: List[str] = []
    if not git(["config", "user.name"], cwd, check=False):
        args += ["-c", "user.name=flow3"]
    if not git(["config", "user.email"], cwd, check=False):
        args += ["-c", "user.email=flow3@example.invalid"]
    return args


def _same_path(a: Path, b: Path) -> bool:
    return os.path.realpath(str(a)) == os.path.realpath(str(b))


def _worktrees(repo_root: Path) -> List[Tuple[Path, str]]:
    """(path, branch short name or "") for every worktree of the repo."""
    out = git(["worktree", "list", "--porcelain"], repo_root)
    found: List[Tuple[Path, str]] = []
    path: Optional[Path] = None
    branch = ""
    for line in out.splitlines() + [""]:
        if line.startswith("worktree "):
            path = Path(line[len("worktree "):])
            branch = ""
        elif line.startswith("branch "):
            branch = line[len("branch "):]
            if branch.startswith("refs/heads/"):
                branch = branch[len("refs/heads/"):]
        elif line == "" and path is not None:
            found.append((path, branch))
            path = None
    return found


def create_worktree(repo_root: Path, path: Path, branch: str, base_ref: str) -> None:
    """
    Create a worktree at path on `branch`, starting a new branch from base_ref. An existing branch is
    reused. When path already is a worktree on that branch this does nothing.
    """
    for wt_path, wt_branch in _worktrees(repo_root):
        if _same_path(wt_path, path):
            if wt_branch == branch:
                return
            raise GitError("%s is already a worktree on branch %r, not %r" % (path, wt_branch, branch))
    exists = _git_rc(["rev-parse", "--verify", "--quiet", "refs/heads/" + branch], repo_root)[0] == 0
    if exists:
        git(["worktree", "add", str(path), branch], repo_root)
    else:
        git(["worktree", "add", "-b", branch, str(path), base_ref], repo_root)


def remove_worktree(repo_root: Path, path: Path) -> None:
    """Remove a worktree (forced: uncommitted work in it is discarded) and prune."""
    if path.exists():
        git(["worktree", "remove", "--force", str(path)], repo_root)
        if path.exists():
            shutil.rmtree(str(path), ignore_errors=True)
    git(["worktree", "prune"], repo_root)


def changed_files(cwd: Path) -> List[str]:
    """
    Uncommitted paths, including untracked files inside new directories. A rename lists both the new
    and the old path, so a protected-path check sees either side.
    """
    out = subprocess.run(["git", "status", "--porcelain", "-z", "--untracked-files=all"], cwd=str(cwd),
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if out.returncode != 0:
        raise GitError("git status failed: %s" % out.stderr.decode("utf-8", "replace").strip())
    entries = out.stdout.decode("utf-8", "surrogateescape").split("\0")
    files: Set[str] = set()
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        status, name = entry[:2], entry[3:]
        files.add(name)
        if "R" in status or "C" in status:
            if i < len(entries) and entries[i]:
                files.add(entries[i])
            i += 1
    return sorted(files)


def commit_all(cwd: Path, message: str, paths: Optional[List[str]] = None,
               hooks: bool = True) -> Optional[str]:
    """Stage everything (or only paths) and commit. Returns the new sha, or None when nothing changed.
    With hooks false the commit skips the repo's commit hooks."""
    if paths is None:
        git(["add", "-A"], cwd)
    elif paths:
        git(["add", "-A", "--"] + list(paths), cwd)
    else:
        return None
    if _git_rc(["diff", "--cached", "--quiet"], cwd)[0] == 0:
        return None
    git(_identity_args(cwd) + ["commit", "-q"] + ([] if hooks else ["--no-verify"]) + ["-m", message], cwd)
    return head_sha(cwd)


def diff(cwd: Path, base_ref: str, head: str = "HEAD") -> str:
    """The diff of head against its merge base with base_ref."""
    proc = subprocess.run(["git", "diff", "%s...%s" % (base_ref, head)], cwd=str(cwd),
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise GitError("git diff failed: %s" % proc.stderr.decode("utf-8", "replace").strip())
    return proc.stdout.decode("utf-8", "replace")


def diff_names(cwd: Path, base_ref: str, head: str = "HEAD") -> List[str]:
    out = git(["diff", "--name-only", "%s...%s" % (base_ref, head)], cwd)
    return [line for line in out.splitlines() if line]


def fetch(cwd: Path) -> None:
    git(["fetch", "--quiet", "origin"], cwd)


def is_behind(cwd: Path, base_ref: str) -> bool:
    """True when base_ref has commits that HEAD lacks."""
    return int(git(["rev-list", "--count", "HEAD..%s" % base_ref], cwd) or "0") > 0


def merge_in(cwd: Path, base_ref: str) -> Tuple[bool, List[str]]:
    """
    Merge base_ref into HEAD. Returns (True, []) when clean (including already up to date). On
    conflict returns (False, conflicted files) and leaves the merge in progress for the caller to
    resolve or abort_merge. Raises GitError for any failure that is not a conflict.
    """
    rc, _out, err = _git_rc(_identity_args(cwd) + ["merge", "--no-edit", base_ref], cwd)
    if rc == 0:
        return True, []
    conflicted = [line for line in git(["diff", "--name-only", "--diff-filter=U"], cwd,
                                       check=False).splitlines() if line]
    if conflicted:
        return False, conflicted
    raise GitError("git merge %s failed: %s" % (base_ref, err.strip()))


def abort_merge(cwd: Path) -> None:
    git(["merge", "--abort"], cwd)


def push(cwd: Path, branch: str) -> None:
    """A normal push of branch to origin. Never forces."""
    args = ["push", "origin", "%s:%s" % (branch, branch)]
    for arg in args:
        assert not arg.startswith("--force") and not arg.startswith("+"), "force push is not allowed"
    git(args, cwd)


def revert_paths(cwd: Path, paths: List[str]) -> None:
    """Restore tracked paths to HEAD and delete untracked files among them."""
    for path in paths:
        if git(["ls-files", "--", path], cwd):
            git(["checkout", "HEAD", "--", path], cwd)
        git(["clean", "-fdq", "--", path], cwd)


def match_any(path: str, patterns: Sequence[str]) -> bool:
    """
    True when a pattern equals path, is a directory prefix of it (`docs/` or `docs` matches
    `docs/a/b.md`), or matches by fnmatch (`*` and `**` may span directories).
    """
    for pattern in patterns:
        if not pattern:
            continue
        base = pattern.rstrip("/")
        if path == pattern or path == base or path.startswith(base + "/"):
            return True
        if fnmatch.fnmatchcase(path, pattern):
            return True
    return False
