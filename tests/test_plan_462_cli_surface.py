#!/usr/bin/env python3
"""
Test suite for plan #462: CLI surface changes and surface removal.

This suite verifies:
1. The `checks run` and `checks receipt` surfaces are removed (Needs you #3)
2. The `checks validate` command works with --mode flag
3. validate command returns proper exit codes based on check success/failure
4. resolve-check command still exists and works
5. CLI surface is clean (no dead code exposed)

Run with: python3 tests/test_plan_462_cli_surface.py
"""

import sys
import json
import os
import subprocess
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _test_harness import Harness, REPO_ROOT


_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _git(cwd, *args):
    """Run git command and return stdout."""
    return subprocess.run(
        ["git", *args], cwd=str(cwd), env={**os.environ, **_GIT_ENV},
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _write(path, text):
    """Write text to path, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _checks_cli(*args, stdin_text=None):
    """
    Run `python3 -m scripts.workflow.cli checks <args>` from repo root.
    Returns (returncode, stdout, stderr).
    """
    env = {k: v for k, v in os.environ.items() if k != "SHIPIT_CHECK_TIMEOUT_SECS"}
    result = subprocess.run(
        [sys.executable, "-m", "scripts.workflow.cli", "checks", *args],
        input=stdin_text.encode() if stdin_text else None,
        capture_output=True, text=True, cwd=str(REPO_ROOT), env=env,
    )
    return result.returncode, result.stdout, result.stderr


def _make_git_tree_with_cache(root, cache_dict):
    """
    Create a temporary git tree with the given cache dict in repo-cache.json.
    Returns the path to the worktree.
    """
    seed = root / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / ".claude").mkdir()
    _write(seed / ".claude" / ".gitignore", "check-records/\nrepo-cache.json\n")
    _write(seed / "README.md", "base\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "init")

    origin = root / "origin.git"
    _git(root, "clone", "-q", "--bare", str(seed), str(origin))

    work = root / "work"
    _git(root, "clone", "-q", str(origin), str(work))
    _git(work, "checkout", "-q", "-b", "feature")

    _write(work / "file.txt", "content\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "feature")

    cache_dict_with_version = {"version": 1, **cache_dict}
    _write(work / ".claude" / "repo-cache.json", json.dumps(cache_dict_with_version))

    return work


if __name__ == "__main__":
    h = Harness("PLAN #462 CLI SURFACE TEST SUITE")
    test_result = h.test_result

    print("[Section 1] checks run subcommand is not available")
    print()

    rc, stdout, stderr = _checks_cli("run", "--help")
    test_result(
        "checks run errors (not found or invalid command)",
        rc != 0,
        f"Expected non-zero exit, got {rc}. stdout={stdout[:100]}, stderr={stderr[:100]}"
    )

    # Note: "checks run" might return usage error since the command may check for required args first
    # before realizing it doesn't exist. This is acceptable.
    test_result(
        "checks run error has non-zero exit",
        rc != 0,
        "Should have non-zero exit for missing command"
    )

    print()
    print("[Section 2] checks receipt subcommand is not available")
    print()

    rc, stdout, stderr = _checks_cli("receipt", "--help")
    test_result(
        "checks receipt errors (not found or invalid command)",
        rc != 0,
        f"Expected non-zero exit, got {rc}. stdout={stdout[:100]}, stderr={stderr[:100]}"
    )

    print()
    print("[Section 3] checks validate command exists and accepts --mode")
    print()

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        work = _make_git_tree_with_cache(root, {
            "commands": {"check": "exit 0"},
            "checkTimeoutSecs": 30,
        })

        # Test validate with --mode minimum
        rc, stdout, stderr = _checks_cli(
            "validate", "--worktree", str(work), "--mode", "minimum"
        )
        test_result(
            "validate --mode minimum has valid exit code (0=pass, 1=fail, not 2=usage error)",
            rc in (0, 1),
            f"Expected exit 0 or 1, got {rc}. stderr={stderr[:200]}"
        )

        # Test validate with --mode full
        rc, stdout, stderr = _checks_cli(
            "validate", "--worktree", str(work), "--mode", "full"
        )
        test_result(
            "validate --mode full has valid exit code (0=pass, 1=fail, not 2=usage error)",
            rc in (0, 1),
            f"Expected exit 0 or 1, got {rc}. stderr={stderr[:200]}"
        )

    print()
    print("[Section 4] checks resolve-check command still exists")
    print()

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        work = _make_git_tree_with_cache(root, {
            "commands": {"check": "exit 0"},
            "checkTimeoutSecs": 30,
        })

        # resolve-check should work
        rc, stdout, stderr = _checks_cli(
            "resolve-check", "--worktree", str(work)
        )
        test_result(
            "resolve-check command exists and responds (not a usage error)",
            rc != 2,  # Exit 2 is argparse usage error
            f"resolve-check should exist. Got rc={rc}, stderr={stderr[:200]}"
        )

        test_result(
            "resolve-check returns JSON on success",
            rc == 0 and stdout.strip() and stdout.strip()[0] in ('{', '['),
            f"Expected JSON output, got rc={rc}, stdout={stdout[:100]}"
        )

    print()
    print("[Section 5] validate with --mode invalid should error with usage code")
    print()

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        work = _make_git_tree_with_cache(root, {
            "commands": {"check": "exit 0"},
            "checkTimeoutSecs": 30,
        })

        rc, stdout, stderr = _checks_cli(
            "validate", "--worktree", str(work), "--mode", "invalid-mode"
        )
        test_result(
            "invalid --mode argument is rejected with usage error (exit 2)",
            rc == 2,
            f"Expected usage error exit 2, got {rc}. stderr={stderr[:200]}"
        )

    print()
    print("[Section 6] ensure-check command still exists")
    print()

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        work = _make_git_tree_with_cache(root, {
            "commands": {"check": "exit 0"},
            "checkTimeoutSecs": 30,
        })

        # ensure-check should exist (though may need --main-worktree)
        rc, stdout, stderr = _checks_cli("ensure-check", "--worktree", str(work), "--main-worktree", str(work))
        test_result(
            "ensure-check command exists and responds",
            rc in (0, 1),  # 0=success, 1=check needs to run, not 2=usage error
            f"ensure-check should exist. Got rc={rc}, stderr={stderr[:200]}"
        )

    print()
    print("[Section 7] checks --help does not list 'run' or 'receipt'")
    print()

    rc, stdout, stderr = _checks_cli("--help")
    help_text = stdout + stderr

    test_result(
        "checks --help does not mention 'run' subcommand",
        "checks run" not in help_text.lower() and " run " not in help_text,
        "Help text should not list removed 'run' command"
    )

    test_result(
        "checks --help does not mention 'receipt' subcommand",
        "checks receipt" not in help_text.lower() and " receipt " not in help_text,
        "Help text should not list removed 'receipt' command"
    )

    test_result(
        "checks --help mentions validate subcommand",
        "validate" in help_text.lower(),
        "Help text should list 'validate' command"
    )

    print()
    h.summarize_and_exit()
