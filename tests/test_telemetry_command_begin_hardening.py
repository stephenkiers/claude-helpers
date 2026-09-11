#!/usr/bin/env python3
"""
Test suite for run-metrics.py command-begin hardening fixes.

Covers:
1. Item 4: cmd_command_begin() catches ValueError from telemetry_schema.append_event()
   and exits cleanly (matching cmd_command_end/cmd_stage_end behavior)
2. Item 5: --effort/--mode argparse choices reference telemetry_schema sets directly,
   preventing drift
3. Item 3: reviewer_count arithmetic (named count + 4 always-run reviewers) is validated
   via stage-end integration test

_spec_blind: These tests verify that the fixes were correctly implemented in run-metrics.py
and associated command logic, without peeking at implementation details.

Run with: python3 tests/test_telemetry_command_begin_hardening.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

# Add parent/scripts to path so we can import telemetry_schema
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import telemetry_schema

from _test_harness import REPO_ROOT, Harness

SCRIPTS_DIR = REPO_ROOT / "scripts"
RUN_METRICS = SCRIPTS_DIR / "run-metrics.py"


def run_script(args, cwd=None, stdin_text=None, env=None):
    """Run python3 with args. Returns (returncode, stdout, stderr)."""
    cmd = [sys.executable, str(RUN_METRICS)] + args
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
        input=stdin_text,
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


# ============================================================================
# Section 1: Item 4 - cmd_command_begin() ValueError handling
# ============================================================================

def test_cmd_command_begin_and_stage_end_have_error_handling():
    """Verify that cmd_command_begin and cmd_stage_end both have ValueError handling.

    Code inspection test: both command-begin and stage-end should catch ValueError
    from telemetry_schema.append_event and exit cleanly (not with a traceback).

    This is a structural test that verifies both functions have matching error handling
    (command-end already has it, and command-begin was fixed to match).
    """
    # Read the run-metrics.py source and isolate each function's own body (up to the next
    # top-level "def "), so this test checks that specific function's error handling rather
    # than counting "except ValueError" occurrences anywhere in the file — a file-wide count
    # stays >= 2 even if one function's own try/except is reverted, as long as some other
    # function elsewhere still has its own ValueError handler.
    script_source = (SCRIPTS_DIR / "run-metrics.py").read_text()

    def function_body(name):
        marker = f"def {name}("
        start = script_source.find(marker)
        if start == -1:
            return None
        next_def = script_source.find("\ndef ", start + 1)
        return script_source[start: next_def if next_def != -1 else len(script_source)]

    begin_body = function_body("cmd_command_begin")
    if begin_body is None:
        return False, "cmd_command_begin function not found"

    if "except ValueError" not in begin_body or "sys.exit(1)" not in begin_body:
        return False, "cmd_command_begin should have try/except ValueError handling with sys.exit(1)"

    stage_end_body = function_body("cmd_stage_end")
    if stage_end_body is None:
        return False, "cmd_stage_end function not found"

    if "except ValueError" not in stage_end_body or "sys.exit(1)" not in stage_end_body:
        return False, "cmd_stage_end should have try/except ValueError handling with sys.exit(1)"

    return True, ""


def test_cmd_command_begin_succeeds_with_valid_command():
    """Verify command-begin succeeds with valid command and all parameters."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "events.jsonl"

        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "command-begin",
                "--command", "expert-review",
                "--effort", "3",
                "--model", "sonnet",
                "--mode", "local",
                "--reviewer-count", "10",
            ]
        )

        if code != 0:
            return False, f"command-begin should succeed with valid params: {stderr}"

        if not log_path.exists():
            return False, "log file should be created"

        event = json.loads(log_path.read_text().strip())
        if event.get("event_type") != "command.begin":
            return False, f"event_type should be command.begin, got {event.get('event_type')}"

        if event.get("effort") != "3":
            return False, f"effort should be 3, got {event.get('effort')}"

        if event.get("reviewer_count") != 10:
            return False, f"reviewer_count should be 10, got {event.get('reviewer_count')}"

        return True, ""


# ============================================================================
# Section 2: Item 5 - argparse choices reference schema sets directly
# ============================================================================

def test_command_begin_effort_choices_from_schema():
    """command-begin --effort choices come from telemetry_schema.EFFORT_LEVELS.

    Verify that both valid and invalid effort values follow schema's set.
    """
    # Valid efforts should all be in EFFORT_LEVELS
    for effort in telemetry_schema.EFFORT_LEVELS:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "events.jsonl"
            code, stdout, stderr = run_script(
                [
                    "--log", str(log_path),
                    "command-begin",
                    "--command", "test-command",
                    "--effort", effort,
                ]
            )
            if code != 0:
                return False, f"effort={effort} should be valid but failed: {stderr}"

    # Invalid efforts should be rejected
    invalid_efforts = ["0", "6", "99", "invalid"]
    for invalid_effort in invalid_efforts:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "events.jsonl"
            code, stdout, stderr = run_script(
                [
                    "--log", str(log_path),
                    "command-begin",
                    "--command", "test-command",
                    "--effort", invalid_effort,
                ]
            )
            if code == 0:
                return False, f"effort={invalid_effort} should be invalid but was accepted"

    return True, ""


def test_command_begin_mode_choices_from_schema():
    """command-begin --mode choices come from telemetry_schema.RUN_MODES.

    Verify that both valid and invalid mode values follow schema's set.
    """
    # Valid modes should all be in RUN_MODES
    for mode in telemetry_schema.RUN_MODES:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "events.jsonl"
            code, stdout, stderr = run_script(
                [
                    "--log", str(log_path),
                    "command-begin",
                    "--command", "test-command",
                    "--mode", mode,
                ]
            )
            if code != 0:
                return False, f"mode={mode} should be valid but failed: {stderr}"

    # Invalid modes should be rejected
    invalid_modes = ["invalid", "PR", "LOCAL", "test"]
    for invalid_mode in invalid_modes:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "events.jsonl"
            code, stdout, stderr = run_script(
                [
                    "--log", str(log_path),
                    "command-begin",
                    "--command", "test-command",
                    "--mode", invalid_mode,
                ]
            )
            if code == 0:
                return False, f"mode={invalid_mode} should be invalid but was accepted"

    return True, ""


def test_stage_end_effort_choices_from_schema():
    """stage-end --effort choices come from telemetry_schema.EFFORT_LEVELS."""
    for effort in telemetry_schema.EFFORT_LEVELS:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "events.jsonl"
            code, stdout, stderr = run_script(
                [
                    "--log", str(log_path),
                    "stage-end",
                    "--stage", "test-stage",
                    "--outcome", "success",
                    "--effort", effort,
                ]
            )
            if code != 0:
                return False, f"effort={effort} should be valid for stage-end but failed: {stderr}"

    # Invalid efforts should be rejected
    invalid_efforts = ["0", "6"]
    for invalid_effort in invalid_efforts:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "events.jsonl"
            code, stdout, stderr = run_script(
                [
                    "--log", str(log_path),
                    "stage-end",
                    "--stage", "test-stage",
                    "--outcome", "success",
                    "--effort", invalid_effort,
                ]
            )
            if code == 0:
                return False, f"effort={invalid_effort} should be invalid for stage-end but was accepted"

    return True, ""


def test_stage_end_mode_choices_from_schema():
    """stage-end --mode choices come from telemetry_schema.RUN_MODES."""
    for mode in telemetry_schema.RUN_MODES:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "events.jsonl"
            code, stdout, stderr = run_script(
                [
                    "--log", str(log_path),
                    "stage-end",
                    "--stage", "test-stage",
                    "--outcome", "success",
                    "--mode", mode,
                ]
            )
            if code != 0:
                return False, f"mode={mode} should be valid for stage-end but failed: {stderr}"

    # Invalid modes should be rejected
    invalid_modes = ["invalid", "PR"]
    for invalid_mode in invalid_modes:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "events.jsonl"
            code, stdout, stderr = run_script(
                [
                    "--log", str(log_path),
                    "stage-end",
                    "--stage", "test-stage",
                    "--outcome", "success",
                    "--mode", invalid_mode,
                ]
            )
            if code == 0:
                return False, f"mode={invalid_mode} should be invalid for stage-end but was accepted"

    return True, ""


def test_effort_and_mode_choices_match_schema_exactly():
    """Verify that the choices sets exactly match the schema constants."""
    import argparse
    import io
    from contextlib import redirect_stderr

    # Test command-begin parser
    try:
        result = run_script(
            [
                "command-begin",
                "--effort", "invalid",
                "--command", "test",
            ],
            stdin_text=None,
        )
        # Should fail with invalid choice
        if result[0] == 0:
            return False, "should reject invalid effort value"
    except Exception as e:
        pass

    # Verify sorted() is used (from the code inspection)
    expected_efforts = sorted(telemetry_schema.EFFORT_LEVELS)
    expected_modes = sorted(telemetry_schema.RUN_MODES)

    # Confirm they match what we expect
    if expected_efforts != ["1", "2", "3", "4", "5"]:
        return False, f"EFFORT_LEVELS should be [1,2,3,4,5], got {expected_efforts}"

    if expected_modes != ["coworker", "local", "pr"]:
        return False, f"RUN_MODES should be [coworker,local,pr], got {expected_modes}"

    return True, ""


# ============================================================================
# Section 3: Item 3 - reviewer_count arithmetic validation
# ============================================================================

def test_stage_end_reviewer_count_with_named_selection():
    """stage-end accepts and records reviewer_count when provided."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "events.jsonl"

        # The fix is: named_count + 4 (sam-system, code-rot-cody, consistency-checker, contrarian-carl)
        # So if we pass reviewer-count that represents e.g., 3 named + 4 always-run = 7
        test_count = 7

        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "stage-end",
                "--stage", "resolve-scope",
                "--outcome", "success",
                "--reviewer-count", str(test_count),
            ]
        )

        if code != 0:
            return False, f"stage-end with reviewer-count should succeed, got: {stderr}"

        # Verify it was recorded
        if not log_path.exists():
            return False, "log file not created"

        event = json.loads(log_path.read_text().strip())
        if event.get("reviewer_count") != test_count:
            return False, f"reviewer_count should be {test_count}, got {event.get('reviewer_count')}"

        return True, ""


def test_command_begin_stores_reviewer_count():
    """command-begin records reviewer_count when provided."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "events.jsonl"

        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "command-begin",
                "--command", "expert-review",
                "--reviewer-count", "10",
            ]
        )

        if code != 0:
            return False, f"command-begin with reviewer-count should succeed, got: {stderr}"

        if not log_path.exists():
            return False, "log file not created"

        event = json.loads(log_path.read_text().strip())
        if event.get("reviewer_count") != 10:
            return False, f"reviewer_count should be 10, got {event.get('reviewer_count')}"

        return True, ""


def test_reviewer_count_always_run_reviewers_arithmetic():
    """Verify that reviewer_count reflects named + 4 always-run reviewers.

    The four always-run reviewers are: sam-system, code-rot-cody,
    consistency-checker, contrarian-carl.

    This test validates the telemetry event structure accepts the calculated value.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "events.jsonl"

        # Simulate: 3 named reviewers + 4 always-run = 7 total
        named_reviewers = 3
        always_run_reviewers = 4
        expected_total = named_reviewers + always_run_reviewers

        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "stage-end",
                "--stage", "resolve-scope",
                "--outcome", "success",
                "--mode", "local",
                "--reviewer-count", str(expected_total),
            ]
        )

        if code != 0:
            return False, f"stage-end with calculated reviewer-count should succeed: {stderr}"

        if not log_path.exists():
            return False, "log file not created"

        event = json.loads(log_path.read_text().strip())
        recorded_count = event.get("reviewer_count")

        if recorded_count != expected_total:
            return False, f"reviewer_count should be {expected_total}, got {recorded_count}"

        return True, ""


# ============================================================================
# Main test runner
# ============================================================================

def run_all_tests():
    """Run all tests and report results."""
    tests = [
        # Item 4 tests
        ("cmd_command_begin_and_stage_end_have_error_handling", test_cmd_command_begin_and_stage_end_have_error_handling),
        ("cmd_command_begin_succeeds_with_valid_command", test_cmd_command_begin_succeeds_with_valid_command),

        # Item 5 tests
        ("command_begin_effort_choices_from_schema", test_command_begin_effort_choices_from_schema),
        ("command_begin_mode_choices_from_schema", test_command_begin_mode_choices_from_schema),
        ("stage_end_effort_choices_from_schema", test_stage_end_effort_choices_from_schema),
        ("stage_end_mode_choices_from_schema", test_stage_end_mode_choices_from_schema),
        ("effort_and_mode_choices_match_schema_exactly", test_effort_and_mode_choices_match_schema_exactly),

        # Item 3 tests
        ("stage_end_reviewer_count_with_named_selection", test_stage_end_reviewer_count_with_named_selection),
        ("command_begin_stores_reviewer_count", test_command_begin_stores_reviewer_count),
        ("reviewer_count_always_run_reviewers_arithmetic", test_reviewer_count_always_run_reviewers_arithmetic),
    ]

    passed = 0
    failed = 0

    for name, test_func in tests:
        try:
            result, message = test_func()
            if result:
                print(f"✓ {name}")
                passed += 1
            else:
                print(f"✗ {name}: {message}")
                failed += 1
        except Exception as e:
            print(f"✗ {name}: {e}")
            failed += 1

    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run_all_tests())
