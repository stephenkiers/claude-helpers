#!/usr/bin/env python3
"""
Test suite for plan #462: merge gate fail-closed behavior and minimum check gates.

This suite tests the three key decisions from the #462 action plan:
1. Merge gate fails closed when no full check resolves but cached commands exist (Needs you #1)
2. Minimum check acceptable for force-push; merge gate is authoritative (Needs you #2)
3. `checks run` / `checks receipt` surface removed (Needs you #3)

Key testable behaviors:
- Merge gate with only lint/test in cache (no check) -> fail with specific message
- Merge gate with full check in cache -> pass/fail based on check result
- Force-push gates use minimum mode
- CLI doesn't expose checks run or receipt commands

Run with: python3 tests/test_plan_462_merge_gate_fail_closed.py
"""

import sys
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.merge import plan_merge, apply_merge, MergePlan, MergeResult
from workflow.safety import Unknown
from workflow.merge import QueueDetection, QueueGuardDecision
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


def _clone_fixture_with_cache(root, cache_commands):
    """
    Create a bare origin with a main branch, clone it, and set up cache.
    Returns the work tree path.
    """
    seed = root / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / ".claude").mkdir()
    (seed / ".claude" / ".gitignore").write_text("check-records/\nrepo-cache.json\n")
    _write(seed / "README.md", "base\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "init")
    origin = root / "origin.git"
    _git(root, "clone", "-q", "--bare", str(seed), str(origin))
    work = root / "work"
    _git(root, "clone", "-q", str(origin), str(work))
    _git(work, "checkout", "-q", "-b", "feature")
    _write(work / "feature.txt", "one\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "feature")
    _write(work / ".claude" / "repo-cache.json", json.dumps(cache_commands) + "\n")
    return work


def _checks_cli(*args):
    """Run `python3 -m scripts.workflow.cli checks <args>` from repo root."""
    env = {k: v for k, v in os.environ.items() if k != "SHIPIT_CHECK_TIMEOUT_SECS"}
    return subprocess.run(
        [sys.executable, "-m", "scripts.workflow.cli", "checks", *args],
        capture_output=True, text=True, cwd=str(REPO_ROOT), env=env,
    )


if __name__ == "__main__":
    fake_home = tempfile.mkdtemp()
    os.environ["HOME"] = fake_home

    # Pin queue guard to "no queue" for all tests in this suite
    mock.patch(
        "workflow.merge.queue_guard",
        return_value=QueueGuardDecision(decision="proceed", detection=QueueDetection(state="absent")),
    ).start()

    try:
        h = Harness("PLAN #462 MERGE GATE FAIL-CLOSED TEST SUITE")
        test_result = h.test_result

        print("[Section 1] Merge gate with only lint/test (no check): should fail closed")
        print()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache_commands = {
                "version": 1,
                "commands": {
                    "lint": "exit 0",
                    "test": "exit 0",
                    # Notably: NO "check" command
                },
                "checkTimeoutSecs": 900,
            }
            work = _clone_fixture_with_cache(root, cache_commands)

            # Create a merge plan
            plan = MergePlan(
                pr_number=42,
                head_ref="feature",
                target_worktree=str(work),
                blocking_failures=[]
            )

            plan_json = json.dumps(plan.to_dict())

            # Mock the merge-related functions to focus on the gate check
            with mock.patch("workflow.merge._check_just_merge") as mock_just:
                with mock.patch("workflow.merge._run_gh_pr_merge") as mock_merge:
                    mock_just.return_value = False
                    mock_merge.return_value = (True, None)

                    # Run apply_merge, which should fail the gate check
                    result, err = apply_merge(plan_json)

                    # Check that the result indicates the gate failed
                    test_result(
                        "apply_merge returns MergeResult with success=False when no full check",
                        result.success is False,
                        f"Expected success=False, got {result.success}, err={err}"
                    )

                    if not result.success:
                        error_str = str(result.error or "")
                        test_result(
                            "Error message contains exact phrase about no full check",
                            "no full check resolved" in error_str,
                            f"Expected 'no full check resolved' in error, got: {result.error}"
                        )

        print()
        print("[Section 2] Merge gate with full check command: should use it")
        print()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache_commands = {
                "version": 1,
                "commands": {
                    "lint": "exit 0",
                    "test": "exit 0",
                    "check": "exit 0",  # Full check present
                },
                "checkTimeoutSecs": 900,
            }
            work = _clone_fixture_with_cache(root, cache_commands)

            plan = MergePlan(
                pr_number=42,
                head_ref="feature",
                target_worktree=str(work),
                blocking_failures=[]
            )

            plan_json = json.dumps(plan.to_dict())

            with mock.patch("workflow.merge._check_just_merge") as mock_just:
                with mock.patch("workflow.merge._run_gh_pr_merge") as mock_merge:
                    with mock.patch("workflow.merge._run_merge_gate_checks") as mock_gate:
                        mock_just.return_value = False
                        mock_merge.return_value = (True, None)
                        mock_gate.return_value = (True, None)

                        result, err = apply_merge(plan_json)

                        test_result(
                            "apply_merge can proceed when full check is available (gate called)",
                            mock_gate.called,
                            f"Expected merge gate to be called when check is available"
                        )

        print()
        print("[Section 3] Checks CLI: validate subcommand supports --mode flag")
        print()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            wt = root / "wt"
            (wt / ".claude").mkdir(parents=True)
            _write(wt / ".claude" / "repo-cache.json",
                   json.dumps({"commands": {"check": "echo test"}, "checkTimeoutSecs": 30}))

            # Test that validate command accepts --mode flag
            result = _checks_cli(
                "validate", "--worktree", str(wt),
                "--mode", "minimum",
            )

            test_result(
                "checks validate accepts --mode argument (not usage error 2)",
                result.returncode != 2,  # 0=pass, 1=fail check, 2=usage error
                f"Expected valid exit (0 or 1), not usage error 2, got {result.returncode}. "
                f"stderr: {result.stderr}"
            )

        print()
        print("[Section 4] Checks CLI: run and receipt commands should not exist")
        print()

        # Test that 'checks run' command is removed or errors appropriately
        result = _checks_cli("run", "--help")
        test_result(
            "checks run command returns usage error (exit 2)",
            result.returncode == 2,  # Should error with argparse usage error
            f"Expected 'checks run' to be unavailable with exit 2. "
            f"returncode={result.returncode}, stderr={result.stderr[:100]}"
        )

        # Test that 'checks receipt' command is removed or errors appropriately
        result = _checks_cli("receipt", "--help")
        test_result(
            "checks receipt command returns usage error (exit 2)",
            result.returncode == 2,  # Should error with argparse usage error
            f"Expected 'checks receipt' to be unavailable with exit 2. "
            f"returncode={result.returncode}, stderr={result.stderr[:100]}"
        )

        print()
        print("[Section 5] Checks CLI: validate --mode full vs minimum are distinct paths")
        print()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            wt = root / "wt"
            (wt / ".claude").mkdir(parents=True)
            _write(wt / ".claude" / "repo-cache.json",
                   json.dumps({"commands": {"check": "echo full-check"}, "checkTimeoutSecs": 30}))

            # Both should accept the flag; exact behavior depends on implementation
            result_full = _checks_cli(
                "validate", "--worktree", str(wt), "--mode", "full"
            )
            result_min = _checks_cli(
                "validate", "--worktree", str(wt), "--mode", "minimum"
            )

            test_result(
                "validate --mode full is not a usage error (exit != 2)",
                result_full.returncode != 2,
                f"Expected valid exit code from --mode full, got {result_full.returncode}"
            )

            test_result(
                "validate --mode minimum is not a usage error (exit != 2)",
                result_min.returncode != 2,
                f"Expected valid exit code from --mode minimum, got {result_min.returncode}"
            )

        print()
        h.summarize_and_exit()

    except Exception as e:
        print(f"Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
