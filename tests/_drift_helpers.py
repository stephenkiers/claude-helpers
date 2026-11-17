"""Shared helpers for the primary-checkout-drift test suites (not a test suite itself).

The leading underscore keeps tests/run_all.py (which globs ``test_*.py``) from running it.
"""

import subprocess
import sys
from pathlib import Path

from _test_harness import REPO_ROOT
from _inproc import run_python

DRIFT_SCRIPT = REPO_ROOT / "scripts" / "primary-checkout-drift.py"


def _git(cwd, *args: str) -> str:
    """Run a git command in cwd (check=True) and return stripped stdout."""
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def attach_upstream(primary_path, tmpdir) -> str:
    """Give primary a bare origin, push its current branch, set it as upstream.

    Returns the bare origin path (as a string).
    """
    origin = Path(tmpdir) / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], capture_output=True, check=True)
    branch = _git(primary_path, "branch", "--show-current")
    _git(primary_path, "remote", "add", "origin", str(origin))
    _git(primary_path, "push", "-u", "origin", branch)
    return str(origin)


def land_commit_on_upstream(primary_path, origin: str, tmpdir) -> None:
    """Commit in a second clone, push to origin, then fast-forward primary onto it."""
    other = Path(tmpdir) / "other"
    subprocess.run(["git", "clone", origin, str(other)], capture_output=True, check=True)
    _git(other, "config", "user.email", "test@example.com")
    _git(other, "config", "user.name", "Test User")
    (other / "upstream.txt").write_text("landed upstream")
    _git(other, "add", "upstream.txt")
    _git(other, "commit", "-m", "upstream commit")
    _git(other, "push", "origin", "HEAD")
    branch = _git(primary_path, "branch", "--show-current")
    _git(primary_path, "pull", "--ff-only", "origin", branch)


def run_script(cmd: str, *args: str) -> tuple:
    """Run the drift script in-process and return (exit_code, stdout, stderr)."""
    result = run_python([str(DRIFT_SCRIPT), cmd] + list(args))
    return result.returncode, result.stdout, result.stderr


def run_script_e2e(cmd: str, *args: str) -> tuple:
    """Run the drift script as a real subprocess and return (exit_code, stdout, stderr)."""
    result = subprocess.run(
        [sys.executable, str(DRIFT_SCRIPT), cmd] + list(args),
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr
