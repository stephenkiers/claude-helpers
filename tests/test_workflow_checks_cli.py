#!/usr/bin/env python3
"""
Test suite for checks CLI subprocess invocation and exit codes.

Tests that the CLI properly exits with:
- 0: checks passed
- 1: a check failed
- 2: no checks ran / coverage violation

Run with: python3 tests/test_workflow_checks_cli.py
"""

import sys
import json
import subprocess
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("WORKFLOW CHECKS CLI TEST SUITE")
    test_result = h.test_result

    print("[Section 1] CLI exit code 2 when no checks ran (empty cache)")

    with tempfile.TemporaryDirectory() as tmpdir:
        cache_json = "{}"
        proc = subprocess.run(
            [sys.executable, "-m", "scripts.workflow.cli", "checks", "run", "-"],
            input=cache_json,
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent
        )

        test_result(
            "checks run with empty cache exits 2",
            proc.returncode == 2
        )
        try:
            output = json.loads(proc.stdout)
            test_result(
                "checks run output is valid JSON",
                isinstance(output, dict)
            )
            test_result(
                "checks run status is 'no_checks_ran'",
                output.get("status") == "no_checks_ran"
            )
            test_result(
                "checks run all_passed is False",
                output.get("all_passed") is False
            )
        except json.JSONDecodeError:
            test_result(
                "checks run output is valid JSON",
                False
            )

    print()
    print("[Section 2] CLI exit code 0 when checks pass")

    with tempfile.TemporaryDirectory() as tmpdir:
        cache_json = json.dumps({
            "commands": {
                "test": "true"
            }
        })
        proc = subprocess.run(
            [sys.executable, "-m", "scripts.workflow.cli", "checks", "run", "-"],
            input=cache_json,
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent
        )

        test_result(
            "checks run with passing test exits 0",
            proc.returncode == 0
        )
        try:
            output = json.loads(proc.stdout)
            test_result(
                "checks run status is 'passed'",
                output.get("status") == "passed"
            )
            test_result(
                "checks run all_passed is True",
                output.get("all_passed") is True
            )
        except json.JSONDecodeError:
            test_result(
                "checks run output is valid JSON",
                False
            )

    print()
    print("[Section 3] CLI exit code 1 when check fails")

    with tempfile.TemporaryDirectory() as tmpdir:
        cache_json = json.dumps({
            "commands": {
                "test": "false"
            }
        })
        proc = subprocess.run(
            [sys.executable, "-m", "scripts.workflow.cli", "checks", "run", "-"],
            input=cache_json,
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent
        )

        test_result(
            "checks run with failing test exits 1",
            proc.returncode == 1
        )
        try:
            output = json.loads(proc.stdout)
            test_result(
                "checks run status is 'failed'",
                output.get("status") == "failed"
            )
            test_result(
                "checks run all_passed is False",
                output.get("all_passed") is False
            )
        except json.JSONDecodeError:
            test_result(
                "checks run output is valid JSON",
                False
            )

    print()
    print("[Section 4] CLI --allow-no-checks flag converts exit 2 to 0")

    with tempfile.TemporaryDirectory() as tmpdir:
        cache_json = "{}"
        proc = subprocess.run(
            [sys.executable, "-m", "scripts.workflow.cli", "checks", "run", "--allow-no-checks", "-"],
            input=cache_json,
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent
        )

        test_result(
            "checks run --allow-no-checks with empty cache exits 0",
            proc.returncode == 0
        )
        try:
            output = json.loads(proc.stdout)
            test_result(
                "checks run --allow-no-checks status still 'no_checks_ran'",
                output.get("status") == "no_checks_ran"
            )
        except json.JSONDecodeError:
            test_result(
                "checks run --allow-no-checks output is valid JSON",
                False
            )

    print()
    print("[Section 5] CLI --timeout flag")

    with tempfile.TemporaryDirectory() as tmpdir:
        cache_json = json.dumps({
            "commands": {
                "test": "true"
            }
        })
        proc = subprocess.run(
            [sys.executable, "-m", "scripts.workflow.cli", "checks", "run", "--timeout", "10", "-"],
            input=cache_json,
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent
        )

        test_result(
            "checks run --timeout completes successfully",
            proc.returncode == 0
        )

    print()
    h.summarize_and_exit()
