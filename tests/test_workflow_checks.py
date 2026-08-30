#!/usr/bin/env python3
"""
Test suite for check ordering and execution.

Run with: python3 tests/test_workflow_checks.py
"""

import sys
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.checks import (
    execute_check, build_check_order, CHECK_ORDER, SkippedCheckReason
)
from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("WORKFLOW CHECKS TEST SUITE")
    test_result = h.test_result

    print("[Section 1] build_check_order with standard commands")

    commands = {
        "format": "prettier --write .",
        "check": "custom-check",
        "lint": "eslint .",
        "typecheck": "tsc --noEmit",
        "test": "jest",
        "build": "npm run build"
    }
    planned, skipped = build_check_order(commands)

    test_result(
        "build_check_order: returns tuple",
        isinstance(planned, list) and isinstance(skipped, list)
    )
    test_result(
        "build_check_order: format first",
        len(planned) > 0 and planned[0] == "format"
    )
    test_result(
        "build_check_order: check present",
        "check" in planned
    )
    test_result(
        "build_check_order: lint suppressed by check",
        "lint" not in planned and any(s.command_type == "lint" and s.reason == "superseded_by_check" for s in skipped)
    )
    test_result(
        "build_check_order: typecheck suppressed by check",
        "typecheck" not in planned and any(s.command_type == "typecheck" and s.reason == "superseded_by_check" for s in skipped)
    )
    test_result(
        "build_check_order: test and build still run",
        "test" in planned and "build" in planned
    )

    print()
    print("[Section 2] build_check_order with null commands")

    commands = {
        "format": None,
        "check": None,
        "lint": "eslint .",
        "test": "jest"
    }
    planned, skipped = build_check_order(commands)

    test_result(
        "build_check_order: skips null format",
        any(s.command_type == "format" and s.reason == "null_command" for s in skipped)
    )
    test_result(
        "build_check_order: includes lint when no check",
        "lint" in planned
    )
    test_result(
        "build_check_order: includes test",
        "test" in planned
    )

    print()
    print("[Section 3] build_check_order preserves CHECK_ORDER")

    commands = {
        "format": "fmt",
        "lint": "lint",
        "typecheck": "type",
        "test": "test",
        "build": "build"
    }
    planned, skipped = build_check_order(commands)

    test_result(
        "build_check_order: format before lint",
        planned.index("format") < planned.index("lint")
    )
    test_result(
        "build_check_order: test before build",
        planned.index("test") < planned.index("build")
    )

    print()
    print("[Section 4] Check execution (Fix 11)")

    result = execute_check("true", cwd=None)
    test_result(
        "execute_check: succeeds on passing command",
        result.success and result.returncode == 0
    )

    result = execute_check("false", cwd=None)
    test_result(
        "execute_check: fails on failing command",
        not result.success and result.returncode != 0
    )

    result = execute_check("echo 'test output' >&2 && false", cwd=None)
    test_result(
        "execute_check: captures stderr on failure",
        not result.success and "test output" in result.stderr
    )

    result = execute_check("sleep 5", cwd=None, timeout=1)
    test_result(
        "execute_check: times out after specified timeout",
        not result.success and "timed out" in str(result.error).lower()
    )

    print()
    print("[Section 5] build_check_order with extras and install")

    commands = {
        "format": "fmt",
        "install": "npm install",
        "lint": "lint",
        "test": "test",
        "custom": "custom-check",
        "build": "build"
    }
    planned, skipped = build_check_order(commands)

    test_result(
        "build_check_order: skips install (not_a_check)",
        "install" not in planned and any(s.command_type == "install" and s.reason == "not_a_check" for s in skipped)
    )
    test_result(
        "build_check_order: includes custom command",
        "custom" in planned
    )
    test_result(
        "build_check_order: custom between test and build",
        planned.index("test") < planned.index("custom") < planned.index("build")
    )

    print()
    h.summarize_and_exit()
