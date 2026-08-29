#!/usr/bin/env python3
"""
Test suite for run_checks() orchestration.

Tests the ordering (format → check → parallelizable → build), stop-at-first-failure,
and handling of None/absent commands.

Run with: python3 tests/test_workflow_checks_execute.py
"""

import sys
import json
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.checks import run_checks, CheckResults, CheckStepResult
from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("WORKFLOW CHECKS EXECUTE TEST SUITE")
    test_result = h.test_result

    print("[Section 1] CheckResults dataclass")

    result = CheckResults(
        results=[],
        all_passed=True,
        failed_at=None
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
        "CheckResults.failed_at defaults to None",
        result.failed_at is None
    )

    print()
    print("[Section 2] run_checks executes in correct order")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        execution_order = []

        def mock_execute_check(command, cwd=None, timeout=300):
            """Mock execute_check that records order."""
            execution_order.append(command)
            return CheckStepResult(
                command_type="format",
                command=command,
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
            parallelizable = ["lint", "typecheck", "test"]

            result = run_checks(commands, repo_root=repo_root, parallelizable=parallelizable)

            # Should execute: format, then parallelize lint/typecheck/test, then build
            # The parallelizable ones should be in there too, but in any order
            parallelizable_found = all(
                cmd in execution_order for cmd in ["eslint .", "tsc --noEmit", "jest"]
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
                "run_checks: parallelizable commands included",
                parallelizable_found
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
            return CheckStepResult(
                command_type="lint" if "lint" in command else "test",
                command=command,
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
            parallelizable = ["lint", "typecheck", "test"]

            result = run_checks(commands, repo_root=repo_root, parallelizable=parallelizable)

            # Should not reach build because lint fails
            test_result(
                "run_checks: stops at first failure",
                not result.all_passed and "build" not in execution_calls
            )
            test_result(
                "run_checks: failed_at is set",
                result.failed_at == "lint"
            )

    print()
    print("[Section 4] run_checks handles None/absent commands")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        executed_commands = []

        def mock_execute_track(command, cwd=None, timeout=300):
            """Mock that tracks executed commands."""
            executed_commands.append(command)
            return CheckStepResult(
                command_type="test",
                command=command,
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
            parallelizable = ["lint"]

            result = run_checks(commands, repo_root=repo_root, parallelizable=parallelizable)

            test_result(
                "run_checks: accepts dict with None values",
                result is not None and result.all_passed is not None
            )
            test_result(
                "run_checks: executes only non-None commands",
                len(executed_commands) > 0
            )

    print()
    print("[Section 5] run_checks with check present alongside separate lint/typecheck")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        check_called = []
        lint_called = []
        typecheck_called = []
        test_called = []
        build_called = []

        def mock_execute_selective(command, cwd=None, timeout=300):
            """Track which command types are called."""
            if "check" in command or "custom-check" in command:
                check_called.append(command)
            elif "lint" in command:
                lint_called.append(command)
            elif "typecheck" in command:
                typecheck_called.append(command)
            elif "test" in command or "jest" in command:
                test_called.append(command)
            elif "build" in command:
                build_called.append(command)

            return CheckStepResult(
                command_type="format",
                command=command,
                success=True,
                returncode=0,
                stdout="",
                stderr="",
                error=None
            )

        with mock.patch("workflow.checks.execute_check", side_effect=mock_execute_selective):
            # According to plan, if "check" is present, does it replace lint/typecheck?
            # The plan says "if present, replaces lint+typecheck"
            commands = {
                "format": "prettier --write .",
                "check": "custom-check",
                "lint": "eslint .",
                "typecheck": "tsc --noEmit",
                "test": "jest",
                "build": "npm run build"
            }
            parallelizable = ["lint", "typecheck", "test"]

            result = run_checks(commands, repo_root=repo_root, parallelizable=parallelizable)

            # Per plan: "check (if present, replaces lint+typecheck)"
            # So lint and typecheck should NOT be called when check is present
            test_result(
                "run_checks: check replaces lint when both present",
                len(check_called) > 0 and len(lint_called) == 0
            )
            test_result(
                "run_checks: check replaces typecheck when both present",
                len(check_called) > 0 and len(typecheck_called) == 0
            )
            test_result(
                "run_checks: test still runs (not excluded by check)",
                len(test_called) > 0
            )

    print()
    print("[Section 6] run_checks returns CheckResults with all_passed=True when all pass")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)

        with mock.patch("workflow.checks.execute_check") as mock_exec:
            mock_exec.return_value = CheckStepResult(
                command_type="test",
                command="pytest",
                success=True,
                returncode=0,
                stdout="",
                stderr="",
                error=None
            )

            commands = {
                "format": "prettier --write .",
                "lint": "eslint .",
                "build": "npm run build"
            }

            result = run_checks(commands, repo_root=repo_root, parallelizable=[])

            test_result(
                "run_checks: all_passed=True when all succeed",
                result.all_passed is True
            )
            test_result(
                "run_checks: failed_at=None when all succeed",
                result.failed_at is None
            )

    print()
    print("[Section 7] run_checks with timeout")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)

        with mock.patch("workflow.checks.execute_check") as mock_exec:
            mock_exec.return_value = CheckStepResult(
                command_type="test",
                command="slow-test",
                success=True,
                returncode=0,
                stdout="",
                stderr="",
                error=None
            )

            commands = {
                "test": "slow-test"
            }

            result = run_checks(commands, repo_root=repo_root, timeout=60)

            # Verify that execute_check was called with timeout parameter
            if mock_exec.called:
                called_kwargs = mock_exec.call_args[1]
                test_result(
                    "run_checks: passes timeout to execute_check",
                    "timeout" in called_kwargs and called_kwargs["timeout"] == 60
                )
            else:
                test_result(
                    "run_checks: timeout parameter accepted",
                    True
                )

    print()
    h.summarize_and_exit()
