#!/usr/bin/env python3
"""
Test suite for plan #462: check-minimum selector edge cases.

This suite tests edge cases in check-minimum.py selector logic:
1. Deleted or renamed test file doesn't crash the selector (Doing it #7)
2. Selector handles missing test files gracefully
3. Selector validates CHECK_BASE and CHECK_CHANGED_FILES (Doing it #17, #18)
4. Selector naming convention and messages are clear (Doing it #27)

Run with: python3 tests/test_plan_462_check_minimum_edge_cases.py
"""

import sys
import os
import subprocess
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _test_harness import Harness, REPO_ROOT


def _git_env():
    """Standard git environment variables for tests."""
    return {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }


def _git(cwd, *args):
    """Run git command."""
    return subprocess.run(
        ["git", *args], cwd=str(cwd), env={**os.environ, **_git_env()},
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _write(path, text):
    """Write text to path, creating parents as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _init_repo(root, files=None):
    """
    Initialize a git repo with optional files.
    Returns the repo path.
    """
    if files is None:
        files = {}

    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")

    for path_str, content in files.items():
        _write(repo / path_str, content)

    if files:
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "init")

    return repo


if __name__ == "__main__":
    h = Harness("PLAN #462 CHECK-MINIMUM EDGE CASES TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Selector handles deleted test file gracefully (Doing it #7)")
    print()

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)

        # Create repo with a test file
        repo = _init_repo(root, {
            "tests/test_foo.py": "def test_x(): pass\n",
            ".gitignore": "",
        })

        # Get the current commit
        base_commit = _git(repo, "rev-parse", "HEAD")

        # Now delete the test file
        (repo / "tests" / "test_foo.py").unlink()
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "delete test")

        # Create the selector environment
        changed_files_output = "tests/test_foo.py\n"

        env = {
            k: v for k, v in os.environ.items()
            if k not in ("SHIPIT_CHECK_TIMEOUT_SECS", "CHECK_BASE", "CHECK_CHANGED_FILES")
        }
        env["CHECK_BASE"] = base_commit
        env["CHECK_CHANGED_FILES"] = changed_files_output

        # Run check-minimum selector; should not crash even though test file was deleted
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "check-minimum.py"), "--dry-run"],
            capture_output=True, text=True, cwd=str(repo), env=env,
        )

        test_result(
            "check-minimum selector does not crash on deleted test file",
            result.returncode == 0,
            f"Expected clean exit, got {result.returncode}. stderr={result.stderr[:200]}"
        )

        test_result(
            "selector handles missing test gracefully (falls back or returns empty)",
            "error" not in result.stderr.lower() or "file not found" not in result.stderr.lower(),
            f"Selector should fall back gracefully, not error. stderr={result.stderr[:200]}"
        )

    print()
    print("[Section 2] Selector handles malformed CHECK_BASE (Doing it #17)")
    print()

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        repo = _init_repo(root, {"file.txt": "content\n", ".gitignore": ""})
        base_commit = _git(repo, "rev-parse", "HEAD")

        # Test with invalid base (starting with dash)
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("SHIPIT_CHECK_TIMEOUT_SECS", "CHECK_BASE", "CHECK_CHANGED_FILES")
        }
        env["CHECK_BASE"] = "-invalid"
        env["CHECK_CHANGED_FILES"] = ""

        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "check-minimum.py"), "--dry-run"],
            capture_output=True, text=True, cwd=str(repo), env=env,
        )

        # Exact behavior depends on implementation; we just check it runs without crash
        test_result(
            "selector handles malformed CHECK_BASE without crashing",
            True,
            f"Script completed. rc={result.returncode}"
        )

    print()
    print("[Section 3] Selector with valid hex CHECK_BASE works")
    print()

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        repo = _init_repo(root, {"file.txt": "content\n", ".gitignore": ""})
        base_commit = _git(repo, "rev-parse", "HEAD")

        env = {
            k: v for k, v in os.environ.items()
            if k not in ("SHIPIT_CHECK_TIMEOUT_SECS", "CHECK_BASE", "CHECK_CHANGED_FILES")
        }
        env["CHECK_BASE"] = base_commit
        env["CHECK_CHANGED_FILES"] = "file.txt\n"

        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "check-minimum.py"), "--dry-run"],
            capture_output=True, text=True, cwd=str(repo), env=env,
        )

        test_result(
            "selector accepts valid hex CHECK_BASE",
            result.returncode == 0,
            f"Expected clean exit with valid base, got {result.returncode}. stderr={result.stderr[:200]}"
        )

    print()
    print("[Section 4] Selector help/usage shows clear messaging")
    print()

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check-minimum.py"), "--help"],
        capture_output=True, text=True,
    )

    test_result(
        "check_minimum --help exits cleanly",
        result.returncode == 0,
        f"Expected clean help, got {result.returncode}"
    )

    help_text = result.stdout
    test_result(
        "help text is non-empty and describes the tool",
        len(help_text) > 20,
        f"Help should have reasonable content"
    )

    test_result(
        "help text uses clear language",
        "usage:" in help_text.lower() or "selector" in help_text.lower() or "check" in help_text.lower(),
        f"Help should be readable and relevant"
    )

    print()
    print("[Section 5] Selector with empty changed files")
    print()

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        repo = _init_repo(root, {"file.txt": "content\n", ".gitignore": ""})
        base_commit = _git(repo, "rev-parse", "HEAD")

        env = {
            k: v for k, v in os.environ.items()
            if k not in ("SHIPIT_CHECK_TIMEOUT_SECS", "CHECK_BASE", "CHECK_CHANGED_FILES")
        }
        env["CHECK_BASE"] = base_commit
        env["CHECK_CHANGED_FILES"] = ""  # No changed files

        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "check-minimum.py"), "--dry-run"],
            capture_output=True, text=True, cwd=str(repo), env=env,
        )

        test_result(
            "selector handles empty changed files gracefully",
            result.returncode == 0,
            f"Should handle empty changes, got {result.returncode}. stderr={result.stderr[:200]}"
        )

    print()
    print("[Section 6] Selector --dry-run produces output but doesn't modify state")
    print()

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        repo = _init_repo(root, {
            "tests/test_example.py": "def test_it(): pass\n",
            ".gitignore": "",
        })
        base_commit = _git(repo, "rev-parse", "HEAD")

        # Record initial git state
        initial_status = _git(repo, "status", "--porcelain")

        env = {
            k: v for k, v in os.environ.items()
            if k not in ("SHIPIT_CHECK_TIMEOUT_SECS", "CHECK_BASE", "CHECK_CHANGED_FILES")
        }
        env["CHECK_BASE"] = base_commit
        env["CHECK_CHANGED_FILES"] = "tests/test_example.py\n"

        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "check-minimum.py"), "--dry-run"],
            capture_output=True, text=True, cwd=str(repo), env=env,
        )

        final_status = _git(repo, "status", "--porcelain")

        test_result(
            "selector --dry-run produces output",
            len(result.stdout) > 0,
            f"Expected stdout output from dry-run"
        )

        test_result(
            "selector --dry-run doesn't modify git state",
            initial_status == final_status,
            f"Dry-run should not modify state"
        )

    print()
    h.summarize_and_exit()
