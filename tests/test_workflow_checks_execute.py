#!/usr/bin/env python3
"""
Test suite for run_checks() orchestration and coverage assertion.

Tests the canonical order, fail-closed behavior, coverage assertion,
and the guarantee that no gate passes without executing something.

Run with: python3 tests/test_workflow_checks_execute.py
"""

import sys
import json
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.checks import run_checks, CheckResult, CheckResults, CheckStepResult
from workflow.safety import Unknown
from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("WORKFLOW CHECKS EXECUTE TEST SUITE")
    test_result = h.test_result

    print("[Section 1] CheckResults dataclass")

    result = CheckResults(
        results=[],
        all_passed=True,
        failed_at=None,
        planned=[],
        executed=[],
        skipped=[],
        status="passed"
    )
    test_result(
        "CheckResults can be instantiated",
        isinstance(result, CheckResults)
    )
    test_result(
        "CheckResults.all_passed defaults correctly",
        result.all_passed is True
    )
    test_result(
        "CheckResults.status exists",
        hasattr(result, "status") and result.status == "passed"
    )

    print()
    print("[Section 2] run_checks executes in correct order")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        execution_order = []

        def mock_execute_check(command, cwd=None, timeout=300):
            """Mock execute_check that records order."""
            execution_order.append(command)
            return CheckResult(
                success=True,
                returncode=0,
                stdout="",
                stderr="",
                error=None
            )

        with mock.patch("workflow.checks.execute_check", side_effect=mock_execute_check):
            commands = {
                "format": "prettier --write .",
                "lint": "eslint .",
                "typecheck": "tsc --noEmit",
                "test": "jest",
                "build": "npm run build"
            }

            result, err = run_checks(commands, repo_root=repo_root)

            test_result(
                "run_checks: returns tuple",
                isinstance(result, CheckResults) and (err is None or isinstance(err, Unknown))
            )
            test_result(
                "run_checks: format executes first",
                len(execution_order) > 0 and execution_order[0] == "prettier --write ."
            )
            test_result(
                "run_checks: build executes last",
                len(execution_order) > 0 and execution_order[-1] == "npm run build"
            )
            test_result(
                "run_checks: all non-suppressed commands executed",
                len(result.executed) == 5
            )

    print()
    print("[Section 3] run_checks stops at first failure")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        execution_calls = []

        def mock_execute_fail(command, cwd=None, timeout=300):
            """Mock that fails at 'lint' and records all calls."""
            execution_calls.append(command)
            success = "lint" not in command
            return CheckResult(
                success=success,
                returncode=0 if success else 1,
                stdout="",
                stderr="failed" if not success else "",
                error=None
            )

        with mock.patch("workflow.checks.execute_check", side_effect=mock_execute_fail):
            commands = {
                "format": "prettier --write .",
                "lint": "eslint .",
                "typecheck": "tsc --noEmit",
                "test": "jest",
                "build": "npm run build"
            }

            result, err = run_checks(commands, repo_root=repo_root)

            test_result(
                "run_checks: stops at first failure",
                not result.all_passed and "build" not in execution_calls
            )
            test_result(
                "run_checks: failed_at is set",
                result.failed_at == "lint"
            )
            test_result(
                "run_checks: status is 'failed'",
                result.status == "failed"
            )
            test_result(
                "run_checks: a legitimate stop-at-first-failure is not a coverage violation (err is None)",
                err is None
            )
            test_result(
                "run_checks: not-yet-reached commands are recorded as skipped(not_reached)",
                {s.command_type for s in result.skipped if s.reason == "not_reached"} == {"typecheck", "test", "build"}
            )

    print()
    print("[Section 4] run_checks handles None/absent commands")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        executed_commands = []

        def mock_execute_track(command, cwd=None, timeout=300):
            """Mock that tracks executed commands."""
            executed_commands.append(command)
            return CheckResult(
                success=True,
                returncode=0,
                stdout="",
                stderr="",
                error=None
            )

        with mock.patch("workflow.checks.execute_check", side_effect=mock_execute_track):
            commands = {
                "format": None,
                "check": None,
                "lint": "eslint .",
                "test": "jest"
            }

            result, err = run_checks(commands, repo_root=repo_root)

            test_result(
                "run_checks: accepts dict with None values",
                result is not None and result.all_passed is not None
            )
            test_result(
                "run_checks: executes only non-None commands",
                len(executed_commands) == 2 and len(result.executed) == 2
            )
            test_result(
                "run_checks: includes lint (not suppressed when no check)",
                "lint" in result.executed
            )

    print()
    print("[Section 5] run_checks with check present suppresses lint/typecheck")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        executed_types = []

        def mock_execute_selective(command, cwd=None, timeout=300):
            """Track which command types are called."""
            # Parse the command to guess type (simplistic but works for test)
            if "lint" in command:
                executed_types.append("lint")
            elif "typecheck" in command or "tsc" in command:
                executed_types.append("typecheck")
            elif "check" in command or "custom-check" in command:
                executed_types.append("check")
            elif "jest" in command:
                executed_types.append("test")
            elif "build" in command:
                executed_types.append("build")

            return CheckResult(
                success=True,
                returncode=0,
                stdout="",
                stderr="",
                error=None
            )

        with mock.patch("workflow.checks.execute_check", side_effect=mock_execute_selective):
            commands = {
                "format": "prettier --write .",
                "check": "custom-check",
                "lint": "eslint .",
                "typecheck": "tsc --noEmit",
                "test": "jest",
                "build": "npm run build"
            }

            result, err = run_checks(commands, repo_root=repo_root)

            test_result(
                "run_checks: check replaces lint",
                "check" in result.executed and "lint" not in result.executed
            )
            test_result(
                "run_checks: check replaces typecheck",
                "check" in result.executed and "typecheck" not in result.executed
            )
            test_result(
                "run_checks: lint/typecheck are skipped (visible)",
                any(s.command_type == "lint" and s.reason == "superseded_by_check" for s in result.skipped) and
                any(s.command_type == "typecheck" and s.reason == "superseded_by_check" for s in result.skipped)
            )

    print()
    print("[Section 6] run_checks: nothing ran is never a pass")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)

        with mock.patch("workflow.checks.execute_check") as mock_exec:
            # No commands at all
            result, err = run_checks({}, repo_root=repo_root)

            test_result(
                "run_checks: empty cache returns Unknown",
                isinstance(err, Unknown)
            )
            test_result(
                "run_checks: empty cache status is 'no_checks_ran'",
                result.status == "no_checks_ran"
            )
            test_result(
                "run_checks: empty cache all_passed is False",
                result.all_passed is False
            )
            test_result(
                "run_checks: empty cache never executed",
                not mock_exec.called
            )

    print()
    print("[Section 7] run_checks: all-null commands is never a pass")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)

        with mock.patch("workflow.checks.execute_check") as mock_exec:
            commands = {
                "format": None,
                "check": None,
                "lint": None,
                "test": None,
                "build": None
            }
            result, err = run_checks(commands, repo_root=repo_root)

            test_result(
                "run_checks: all-null returns Unknown",
                isinstance(err, Unknown)
            )
            test_result(
                "run_checks: all-null status is 'no_checks_ran'",
                result.status == "no_checks_ran"
            )
            test_result(
                "run_checks: all-null all_passed is False",
                result.all_passed is False
            )
            test_result(
                "run_checks: all-null never executed",
                not mock_exec.called
            )

    print()
    print("[Section 8] run_checks: single test command passes")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)

        with mock.patch("workflow.checks.execute_check") as mock_exec:
            mock_exec.return_value = CheckResult(
                success=True,
                returncode=0,
                stdout="",
                stderr="",
                error=None
            )

            commands = {
                "test": "pytest"
            }

            result, err = run_checks(commands, repo_root=repo_root)

            test_result(
                "run_checks: single command passes without error",
                err is None
            )
            test_result(
                "run_checks: single command all_passed is True",
                result.all_passed is True
            )
            test_result(
                "run_checks: single command status is 'passed'",
                result.status == "passed"
            )
            test_result(
                "run_checks: single command executed",
                result.executed == ["test"]
            )

    print()
    print("[Section 9] run_checks: repo-cache with only commands.test runs it")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        executed = []

        def mock_track(command, cwd=None, timeout=300):
            executed.append(command)
            return CheckResult(
                success=True,
                returncode=0,
                stdout="",
                stderr="",
                error=None
            )

        with mock.patch("workflow.checks.execute_check", side_effect=mock_track):
            commands = {
                "test": "pytest"
            }

            result, err = run_checks(commands, repo_root=repo_root)

            test_result(
                "run_checks: test command executed (live repro)",
                len(executed) == 1 and executed[0] == "pytest"
            )
            test_result(
                "run_checks: test result passes",
                result.all_passed is True
            )

    print()
    print("[Section 10] run_checks with timeout")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)

        with mock.patch("workflow.checks.execute_check") as mock_exec:
            mock_exec.return_value = CheckResult(
                success=True,
                returncode=0,
                stdout="",
                stderr="",
                error=None
            )

            commands = {
                "test": "slow-test"
            }

            result, err = run_checks(commands, repo_root=repo_root, timeout=60)

            test_result(
                "run_checks: passes timeout to execute_check",
                mock_exec.called and mock_exec.call_args[1]["timeout"] == 60
            )

    print()
    h.summarize_and_exit()
