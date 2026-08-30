#!/usr/bin/env python3
"""
Test suite for CLI entry point and exit code handling.

Run with: python3 tests/test_workflow_cli.py
"""

import sys
import subprocess
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _test_harness import Harness


def run_cli(*args, cwd=None):
    """Run the CLI as a subprocess and return result."""
    cmd = [sys.executable, "-m", "scripts.workflow.cli"] + list(args)
    result = subprocess.run(
        cmd,
        cwd=cwd or Path(__file__).parent.parent,
        capture_output=True,
        text=True
    )
    return result


if __name__ == "__main__":
    h = Harness("WORKFLOW CLI TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Exit code on invalid command")

    result = run_cli("invalid-command")
    test_result(
        "CLI exits non-zero on invalid command",
        result.returncode != 0
    )
    test_result(
        "CLI prints error on invalid command",
        "usage:" in result.stderr or "invalid choice" in result.stderr or "Deterministic workflow CLI" in result.stderr
    )

    print()
    print("[Section 2] Exit code on cleanup plan with nonexistent target")

    result = run_cli("cleanup", "plan", "/nonexistent/path/definitely/does/not/exist")
    test_result(
        "CLI exits 1 on cleanup plan error",
        result.returncode == 1
    )
    try:
        output = json.loads(result.stdout)
        test_result(
            "CLI outputs valid JSON on error",
            isinstance(output, dict) and output.get("success") is False
        )
        test_result(
            "Error output contains error message",
            "error" in output
        )
    except json.JSONDecodeError:
        test_result(
            "CLI output is valid JSON on error",
            False,
            f"Got: {result.stdout}"
        )

    print()
    print("[Section 3] Exit code on cleanup apply with invalid plan")

    result = run_cli("cleanup", "apply", "not-valid-json")
    test_result(
        "CLI exits 1 on cleanup apply error",
        result.returncode == 1
    )
    try:
        output = json.loads(result.stdout)
        test_result(
            "CLI outputs valid JSON on apply error",
            isinstance(output, dict)
        )
    except json.JSONDecodeError:
        test_result(
            "CLI output is valid JSON on apply error",
            False,
            f"Got: {result.stdout}"
        )

    print()
    print("[Section 4] Exit code on merge plan with nonexistent target")

    result = run_cli("merge", "plan", "/nonexistent/path/definitely/does/not/exist")
    test_result(
        "CLI exits 1 on merge plan error",
        result.returncode == 1
    )
    try:
        output = json.loads(result.stdout)
        test_result(
            "CLI outputs valid JSON on merge plan error",
            isinstance(output, dict) and output.get("success") is False
        )
    except json.JSONDecodeError:
        test_result(
            "CLI output is valid JSON on merge plan error",
            False,
            f"Got: {result.stdout}"
        )

    print()
    print("[Section 5] Exit code on merge apply with invalid plan")

    result = run_cli("merge", "apply", "not-valid-json")
    test_result(
        "CLI exits 1 on merge apply error",
        result.returncode == 1
    )
    try:
        output = json.loads(result.stdout)
        test_result(
            "CLI outputs valid JSON on merge apply error",
            isinstance(output, dict)
        )
    except json.JSONDecodeError:
        test_result(
            "CLI output is valid JSON on merge apply error",
            False,
            f"Got: {result.stdout}"
        )

    print()
    print("[Section 6] No cleanup or merge subcommand exits 1")

    result = run_cli("cleanup")
    test_result(
        "CLI exits 1 on cleanup without subcommand",
        result.returncode == 1
    )

    result = run_cli("merge")
    test_result(
        "CLI exits 1 on merge without subcommand",
        result.returncode == 1
    )

    print()
    print("[Section 7] merge plan with zero arguments (optional argument)")

    # Test that merge plan accepts zero arguments without argparse error
    # Use a scratch tempdir (not a linked worktree) to test deterministically
    with tempfile.TemporaryDirectory() as scratch_dir:
        result = run_cli("merge", "plan", cwd=scratch_dir)
        test_result(
            "CLI accepts 'merge plan' with zero arguments (no argparse error)",
            result.returncode != 2,  # argparse errors return 2
            f"Got exit code {result.returncode}, stderr: {result.stderr}"
        )

        # When run from non-linked worktree/main, it should fail gracefully
        # (not with argparse error)
        try:
            output = json.loads(result.stdout)
            test_result(
                "CLI outputs valid JSON on 'merge plan' with zero args",
                isinstance(output, dict),
                f"Got: {result.stdout}"
            )
        except json.JSONDecodeError:
            # It's okay if it's not JSON - the important thing is it didn't
            # raise an argparse error (exit code 2)
            test_result(
                "CLI doesn't raise argparse error on 'merge plan' zero args",
                result.returncode == 1,  # Should be a normal error, not argparse error
                f"Exit code {result.returncode}, stderr: {result.stderr}"
            )

    print()
    h.summarize_and_exit()
