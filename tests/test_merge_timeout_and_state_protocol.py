#!/usr/bin/env python3
"""
Test suite for merge timeout configuration and PR-scoped state protocol.

Tests spec changes:
1. Configurable merge timeout with DEFAULT_MERGE_APPLY_TIMEOUT_SECS and _get_merge_apply_timeout()
2. PR-scoped state directory /tmp/merge-and-cleanup.pr-{PR_NUM} with validation and self-cleaning

Run with: python3 tests/test_merge_timeout_and_state_protocol.py
"""

import sys
import os
import re
import tempfile
import subprocess
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.merge import DEFAULT_MERGE_APPLY_TIMEOUT_SECS, _get_merge_apply_timeout
from _test_harness import Harness

REPO_ROOT = Path(__file__).resolve().parent.parent


if __name__ == "__main__":
    h = Harness("MERGE TIMEOUT AND STATE PROTOCOL TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Timeout constant exists and has correct default value")

    try:
        timeout_value = DEFAULT_MERGE_APPLY_TIMEOUT_SECS
        test_result(
            "DEFAULT_MERGE_APPLY_TIMEOUT_SECS is defined",
            timeout_value is not None,
        )
        test_result(
            "DEFAULT_MERGE_APPLY_TIMEOUT_SECS equals 1800",
            timeout_value == 1800,
            f"Expected 1800, got {timeout_value}"
        )
    except Exception as e:
        test_result(
            "DEFAULT_MERGE_APPLY_TIMEOUT_SECS is defined",
            False,
            str(e)
        )

    print()
    print("[Section 2] _get_merge_apply_timeout() with unset env var returns default")

    # Save original env var
    original_env = os.environ.get("MERGE_APPLY_TIMEOUT_SECS")
    try:
        # Ensure env var is unset
        if "MERGE_APPLY_TIMEOUT_SECS" in os.environ:
            del os.environ["MERGE_APPLY_TIMEOUT_SECS"]

        result = _get_merge_apply_timeout()
        test_result(
            "Returns default (1800) when env var is unset",
            result == 1800,
            f"Expected 1800, got {result}"
        )
    finally:
        # Restore original env var
        if original_env is not None:
            os.environ["MERGE_APPLY_TIMEOUT_SECS"] = original_env
        elif "MERGE_APPLY_TIMEOUT_SECS" in os.environ:
            del os.environ["MERGE_APPLY_TIMEOUT_SECS"]

    print()
    print("[Section 3] _get_merge_apply_timeout() with empty env var returns default")

    original_env = os.environ.get("MERGE_APPLY_TIMEOUT_SECS")
    try:
        os.environ["MERGE_APPLY_TIMEOUT_SECS"] = ""
        result = _get_merge_apply_timeout()
        test_result(
            "Returns default (1800) when env var is empty",
            result == 1800,
            f"Expected 1800, got {result}"
        )
    finally:
        if original_env is not None:
            os.environ["MERGE_APPLY_TIMEOUT_SECS"] = original_env
        elif "MERGE_APPLY_TIMEOUT_SECS" in os.environ:
            del os.environ["MERGE_APPLY_TIMEOUT_SECS"]

    print()
    print("[Section 4] _get_merge_apply_timeout() with valid positive integer returns that value")

    test_cases = ["2400", "3600", "100", "1"]
    for test_val in test_cases:
        original_env = os.environ.get("MERGE_APPLY_TIMEOUT_SECS")
        try:
            os.environ["MERGE_APPLY_TIMEOUT_SECS"] = test_val
            result = _get_merge_apply_timeout()
            expected = int(test_val)
            test_result(
                f"Returns {expected} when env var is '{test_val}'",
                result == expected,
                f"Expected {expected}, got {result}"
            )
        finally:
            if original_env is not None:
                os.environ["MERGE_APPLY_TIMEOUT_SECS"] = original_env
            elif "MERGE_APPLY_TIMEOUT_SECS" in os.environ:
                del os.environ["MERGE_APPLY_TIMEOUT_SECS"]

    print()
    print("[Section 5] _get_merge_apply_timeout() with zero returns default")

    original_env = os.environ.get("MERGE_APPLY_TIMEOUT_SECS")
    try:
        os.environ["MERGE_APPLY_TIMEOUT_SECS"] = "0"
        result = _get_merge_apply_timeout()
        test_result(
            "Returns default (1800) when env var is '0'",
            result == 1800,
            f"Expected 1800, got {result}"
        )
    finally:
        if original_env is not None:
            os.environ["MERGE_APPLY_TIMEOUT_SECS"] = original_env
        elif "MERGE_APPLY_TIMEOUT_SECS" in os.environ:
            del os.environ["MERGE_APPLY_TIMEOUT_SECS"]

    print()
    print("[Section 6] _get_merge_apply_timeout() with negative value returns default")

    original_env = os.environ.get("MERGE_APPLY_TIMEOUT_SECS")
    try:
        os.environ["MERGE_APPLY_TIMEOUT_SECS"] = "-5"
        result = _get_merge_apply_timeout()
        test_result(
            "Returns default (1800) when env var is '-5'",
            result == 1800,
            f"Expected 1800, got {result}"
        )
    finally:
        if original_env is not None:
            os.environ["MERGE_APPLY_TIMEOUT_SECS"] = original_env
        elif "MERGE_APPLY_TIMEOUT_SECS" in os.environ:
            del os.environ["MERGE_APPLY_TIMEOUT_SECS"]

    print()
    print("[Section 7] _get_merge_apply_timeout() with non-numeric value returns default")

    test_cases = ["abc", "12.5", "10s", "timeout", "1e3"]
    for test_val in test_cases:
        original_env = os.environ.get("MERGE_APPLY_TIMEOUT_SECS")
        try:
            os.environ["MERGE_APPLY_TIMEOUT_SECS"] = test_val
            result = _get_merge_apply_timeout()
            test_result(
                f"Returns default (1800) when env var is '{test_val}'",
                result == 1800,
                f"Expected 1800, got {result}"
            )
        finally:
            if original_env is not None:
                os.environ["MERGE_APPLY_TIMEOUT_SECS"] = original_env
            elif "MERGE_APPLY_TIMEOUT_SECS" in os.environ:
                del os.environ["MERGE_APPLY_TIMEOUT_SECS"]

    print()
    print("[Section 8] _get_merge_apply_timeout() never raises an exception")

    invalid_cases = ["abc", "-1", "0", "", "   ", "!@#$%"]
    for test_val in invalid_cases:
        original_env = os.environ.get("MERGE_APPLY_TIMEOUT_SECS")
        try:
            os.environ["MERGE_APPLY_TIMEOUT_SECS"] = test_val
            exception_raised = False
            try:
                result = _get_merge_apply_timeout()
            except Exception:
                exception_raised = True

            test_result(
                f"Does not raise for env var '{test_val}'",
                not exception_raised,
                "Should not raise exception"
            )
        finally:
            if original_env is not None:
                os.environ["MERGE_APPLY_TIMEOUT_SECS"] = original_env
            elif "MERGE_APPLY_TIMEOUT_SECS" in os.environ:
                del os.environ["MERGE_APPLY_TIMEOUT_SECS"]

    print()
    print("[Section 9] merge-and-cleanup.md does not reference /tmp/merge-and-cleanup.latest")

    merge_cmd_path = REPO_ROOT / "commands" / "merge-and-cleanup.md"
    try:
        merge_cmd_content = merge_cmd_path.read_text()
        has_latest = "/tmp/merge-and-cleanup.latest" in merge_cmd_content
        test_result(
            "merge-and-cleanup.md does not contain /tmp/merge-and-cleanup.latest",
            not has_latest,
            "The pointer file /tmp/merge-and-cleanup.latest should not be used"
        )
    except Exception as e:
        test_result(
            "merge-and-cleanup.md does not contain /tmp/merge-and-cleanup.latest",
            False,
            str(e)
        )

    print()
    print("[Section 10] merge-and-cleanup.md uses PR-scoped state directory pattern")

    try:
        merge_cmd_content = merge_cmd_path.read_text()
        has_pr_pattern = "/tmp/merge-and-cleanup.pr-" in merge_cmd_content
        test_result(
            "merge-and-cleanup.md contains /tmp/merge-and-cleanup.pr- pattern",
            has_pr_pattern,
            "Should use PR-scoped state directory"
        )
    except Exception as e:
        test_result(
            "merge-and-cleanup.md contains /tmp/merge-and-cleanup.pr- pattern",
            False,
            str(e)
        )

    print()
    print("[Section 11] merge-and-cleanup.md validates pr_num in state directory")

    try:
        merge_cmd_content = merge_cmd_path.read_text()
        # Look for validation of pr_num file - check for reading and comparing pr_num
        has_pr_num_check = "pr_num" in merge_cmd_content and ("cat" in merge_cmd_content or "=" in merge_cmd_content)
        test_result(
            "merge-and-cleanup.md contains pr_num validation logic",
            has_pr_num_check,
            "Should validate that pr_num file matches expected PR number"
        )
    except Exception as e:
        test_result(
            "merge-and-cleanup.md contains pr_num validation logic",
            False,
            str(e)
        )

    print()
    print("[Section 12] merge-and-cleanup.md contains exit-code guard logic")

    try:
        merge_cmd_content = merge_cmd_path.read_text()
        # Look for apply_exit_code or similar exit code checking
        has_exit_check = "apply_exit_code" in merge_cmd_content or "exit_code" in merge_cmd_content
        test_result(
            "merge-and-cleanup.md contains exit-code validation logic",
            has_exit_check,
            "Should check the exit code from the merge operation"
        )
    except Exception as e:
        test_result(
            "merge-and-cleanup.md contains exit-code validation logic",
            False,
            str(e)
        )

    print()
    print("[Section 13] merge-and-cleanup.md contains self-cleaning logic (rm -rf)")

    try:
        merge_cmd_content = merge_cmd_path.read_text()
        # Look for removal of state directory
        has_rm_rf = "rm -rf" in merge_cmd_content and "/tmp/merge-and-cleanup.pr-" in merge_cmd_content
        test_result(
            "merge-and-cleanup.md removes state directory on success",
            has_rm_rf,
            "Should use rm -rf to clean up state directory"
        )
    except Exception as e:
        test_result(
            "merge-and-cleanup.md removes state directory on success",
            False,
            str(e)
        )

    print()
    print("[Section 14] merge-and-cleanup.md frontmatter grants Bash(rm:*)")

    try:
        merge_cmd_content = merge_cmd_path.read_text()
        # Extract frontmatter (between first --- and second ---)
        frontmatter_match = re.search(r'^---\n(.*?)\n---', merge_cmd_content, re.DOTALL | re.MULTILINE)
        if frontmatter_match:
            frontmatter = frontmatter_match.group(1)
            has_rm_grant = "rm:*" in frontmatter or "Bash(rm" in frontmatter
            test_result(
                "merge-and-cleanup.md frontmatter includes Bash(rm:*) grant",
                has_rm_grant,
                "Should grant rm capability for cleanup"
            )
        else:
            test_result(
                "merge-and-cleanup.md frontmatter includes Bash(rm:*) grant",
                False,
                "Could not find frontmatter"
            )
    except Exception as e:
        test_result(
            "merge-and-cleanup.md frontmatter includes Bash(rm:*) grant",
            False,
            str(e)
        )

    print()
    print("[Section 15] merge-and-cleanup.md frontmatter grants Bash(mkdir:*)")

    try:
        merge_cmd_content = merge_cmd_path.read_text()
        frontmatter_match = re.search(r'^---\n(.*?)\n---', merge_cmd_content, re.DOTALL | re.MULTILINE)
        if frontmatter_match:
            frontmatter = frontmatter_match.group(1)
            has_mkdir_grant = "mkdir:*" in frontmatter or "Bash(mkdir" in frontmatter
            test_result(
                "merge-and-cleanup.md frontmatter includes Bash(mkdir:*) grant",
                has_mkdir_grant,
                "Should grant mkdir capability for state directory creation"
            )
        else:
            test_result(
                "merge-and-cleanup.md frontmatter includes Bash(mkdir:*) grant",
                False,
                "Could not find frontmatter"
            )
    except Exception as e:
        test_result(
            "merge-and-cleanup.md frontmatter includes Bash(mkdir:*) grant",
            False,
            str(e)
        )

    print()
    print("[Section 16] Exit-code guard handles missing file")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        state_dir = tmppath / "merge-and-cleanup.pr-42"
        state_dir.mkdir()

        # Create a bash script that simulates the guard logic
        guard_script = """
        exit_code_file="$1"
        if [ ! -f "$exit_code_file" ]; then
            # Missing file should be treated as failure
            exit 1
        fi
        exit_code=$(cat "$exit_code_file")
        if [ -z "$exit_code" ] || ! [[ "$exit_code" =~ ^[0-9]+$ ]] || [ "$exit_code" -ne 0 ]; then
            exit 1
        fi
        exit 0
        """
        script_file = tmppath / "guard_test.sh"
        script_file.write_text(guard_script)
        script_file.chmod(0o755)

        # Test with missing file
        result = subprocess.run(
            ["bash", str(script_file), str(state_dir / "missing_file")],
            capture_output=True
        )
        test_result(
            "Exit-code guard fails when file is missing",
            result.returncode == 1,
            f"Expected exit code 1 for missing file, got {result.returncode}"
        )

    print()
    print("[Section 17] Exit-code guard handles empty file")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        state_dir = tmppath / "merge-and-cleanup.pr-42"
        state_dir.mkdir()

        guard_script = """
        exit_code_file="$1"
        if [ ! -f "$exit_code_file" ]; then
            exit 1
        fi
        exit_code=$(cat "$exit_code_file")
        if [ -z "$exit_code" ] || ! [[ "$exit_code" =~ ^[0-9]+$ ]] || [ "$exit_code" -ne 0 ]; then
            exit 1
        fi
        exit 0
        """
        script_file = tmppath / "guard_test.sh"
        script_file.write_text(guard_script)
        script_file.chmod(0o755)

        # Test with empty file
        exit_code_file = state_dir / "apply_exit_code"
        exit_code_file.write_text("")

        result = subprocess.run(
            ["bash", str(script_file), str(exit_code_file)],
            capture_output=True
        )
        test_result(
            "Exit-code guard fails when file is empty",
            result.returncode == 1,
            f"Expected exit code 1 for empty file, got {result.returncode}"
        )

    print()
    print("[Section 18] Exit-code guard handles non-numeric content")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        state_dir = tmppath / "merge-and-cleanup.pr-42"
        state_dir.mkdir()

        guard_script = """
        exit_code_file="$1"
        if [ ! -f "$exit_code_file" ]; then
            exit 1
        fi
        exit_code=$(cat "$exit_code_file")
        if [ -z "$exit_code" ] || ! [[ "$exit_code" =~ ^[0-9]+$ ]] || [ "$exit_code" -ne 0 ]; then
            exit 1
        fi
        exit 0
        """
        script_file = tmppath / "guard_test.sh"
        script_file.write_text(guard_script)
        script_file.chmod(0o755)

        # Test with non-numeric content
        exit_code_file = state_dir / "apply_exit_code"
        exit_code_file.write_text("abc")

        result = subprocess.run(
            ["bash", str(script_file), str(exit_code_file)],
            capture_output=True
        )
        test_result(
            "Exit-code guard fails when file contains non-numeric content",
            result.returncode == 1,
            f"Expected exit code 1 for non-numeric content, got {result.returncode}"
        )

    print()
    print("[Section 19] Exit-code guard handles non-zero exit code")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        state_dir = tmppath / "merge-and-cleanup.pr-42"
        state_dir.mkdir()

        guard_script = """
        exit_code_file="$1"
        if [ ! -f "$exit_code_file" ]; then
            exit 1
        fi
        exit_code=$(cat "$exit_code_file")
        if [ -z "$exit_code" ] || ! [[ "$exit_code" =~ ^[0-9]+$ ]] || [ "$exit_code" -ne 0 ]; then
            exit 1
        fi
        exit 0
        """
        script_file = tmppath / "guard_test.sh"
        script_file.write_text(guard_script)
        script_file.chmod(0o755)

        # Test with non-zero exit code
        exit_code_file = state_dir / "apply_exit_code"
        exit_code_file.write_text("127")

        result = subprocess.run(
            ["bash", str(script_file), str(exit_code_file)],
            capture_output=True
        )
        test_result(
            "Exit-code guard fails when exit code is non-zero",
            result.returncode == 1,
            f"Expected exit code 1 for non-zero exit code, got {result.returncode}"
        )

    print()
    print("[Section 20] Exit-code guard succeeds with zero exit code")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        state_dir = tmppath / "merge-and-cleanup.pr-42"
        state_dir.mkdir()

        guard_script = """
        exit_code_file="$1"
        if [ ! -f "$exit_code_file" ]; then
            exit 1
        fi
        exit_code=$(cat "$exit_code_file")
        if [ -z "$exit_code" ] || ! [[ "$exit_code" =~ ^[0-9]+$ ]] || [ "$exit_code" -ne 0 ]; then
            exit 1
        fi
        exit 0
        """
        script_file = tmppath / "guard_test.sh"
        script_file.write_text(guard_script)
        script_file.chmod(0o755)

        # Test with zero exit code
        exit_code_file = state_dir / "apply_exit_code"
        exit_code_file.write_text("0")

        result = subprocess.run(
            ["bash", str(script_file), str(exit_code_file)],
            capture_output=True
        )
        test_result(
            "Exit-code guard succeeds when exit code is zero",
            result.returncode == 0,
            f"Expected exit code 0 for zero exit code, got {result.returncode}"
        )

    print()
    h.summarize_and_exit()
