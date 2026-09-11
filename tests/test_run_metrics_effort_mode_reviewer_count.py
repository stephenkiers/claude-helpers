#!/usr/bin/env python3
"""
Test suite for run-metrics.py effort/mode/reviewer_count CLI flags.

Tests the new --effort, --model, --mode, --reviewer-count flags added to
command-begin and stage-end subcommands.

Covers:
1. command-begin accepts --effort, --model, --mode, --reviewer-count flags
2. stage-end accepts --effort, --model, --mode, --reviewer-count flags
3. Invalid --effort/--mode choices are rejected
4. Valid values are recorded in the telemetry event
5. Optional flags can be omitted without error

_spec_blind: This suite is intentionally duplicated with tests/test_run_metrics.py (Section 13)
for independent CLI contract coverage. See cross-reference in that file (line ~2699).

Run with: python3 tests/test_run_metrics_effort_mode_reviewer_count.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

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
# Section 1: command-begin with new flags
# ============================================================================


def test_command_begin_accepts_effort_flag():
    """command-begin accepts --effort flag with valid values."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "events.jsonl"

        for effort in ["1", "2", "3", "4", "5"]:
            code, stdout, stderr = run_script(
                [
                    "--log", str(log_path),
                    "command-begin",
                    "--command", "test-command",
                    "--effort", effort,
                ]
            )
            if code != 0:
                return False, f"Failed with effort={effort}: {stderr}"

        # Verify events were logged
        if not log_path.exists():
            return False, "events.jsonl not created"

        lines = log_path.read_text().strip().split("\n")
        if len(lines) != 5:
            return False, f"Expected 5 events, got {len(lines)}"

        # Check that each event has the correct effort
        for i, line in enumerate(lines):
            event = json.loads(line)
            expected_effort = str(i + 1)
            if event.get("effort") != expected_effort:
                return False, f"Event {i} has effort={event.get('effort')}, expected {expected_effort}"

        return True, ""


def test_command_begin_accepts_mode_flag():
    """command-begin accepts --mode flag with valid values."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "events.jsonl"

        for mode in ["local", "pr", "coworker"]:
            code, stdout, stderr = run_script(
                [
                    "--log", str(log_path),
                    "command-begin",
                    "--command", "test-command",
                    "--mode", mode,
                ]
            )
            if code != 0:
                return False, f"Failed with mode={mode}: {stderr}"

        # Verify events were logged
        if not log_path.exists():
            return False, "events.jsonl not created"

        lines = log_path.read_text().strip().split("\n")
        expected_modes = ["local", "pr", "coworker"]
        for i, line in enumerate(lines):
            event = json.loads(line)
            expected_mode = expected_modes[i]
            if event.get("mode") != expected_mode:
                return False, f"Event {i} has mode={event.get('mode')}, expected {expected_mode}"

        return True, ""


def test_command_begin_accepts_reviewer_count_flag():
    """command-begin accepts --reviewer-count flag with integer values."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "events.jsonl"

        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "command-begin",
                "--command", "test-command",
                "--reviewer-count", "14",
            ]
        )
        if code != 0:
            return False, f"Failed: {stderr}"

        if not log_path.exists():
            return False, "events.jsonl not created"

        event = json.loads(log_path.read_text().strip())
        if event.get("reviewer_count") != 14:
            return False, f"reviewer_count is {event.get('reviewer_count')}, expected 14"

        return True, ""


def test_command_begin_accepts_model_flag():
    """command-begin accepts --model flag with arbitrary string values."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "events.jsonl"

        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "command-begin",
                "--command", "test-command",
                "--model", "sonnet-4",
            ]
        )
        if code != 0:
            return False, f"Failed: {stderr}"

        event = json.loads(log_path.read_text().strip())
        if event.get("model") != "sonnet-4":
            return False, f"model is {event.get('model')}, expected 'sonnet-4'"

        return True, ""


def test_command_begin_with_all_four_new_flags():
    """command-begin accepts all four new flags together."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "events.jsonl"

        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "command-begin",
                "--command", "expert-review",
                "--effort", "3",
                "--model", "opus",
                "--mode", "pr",
                "--reviewer-count", "24",
            ]
        )
        if code != 0:
            return False, f"Failed: {stderr}"

        event = json.loads(log_path.read_text().strip())
        if event.get("effort") != "3":
            return False, f"effort is {event.get('effort')}, expected '3'"
        if event.get("model") != "opus":
            return False, f"model is {event.get('model')}, expected 'opus'"
        if event.get("mode") != "pr":
            return False, f"mode is {event.get('mode')}, expected 'pr'"
        if event.get("reviewer_count") != 24:
            return False, f"reviewer_count is {event.get('reviewer_count')}, expected 24"

        return True, ""


def test_command_begin_rejects_invalid_effort():
    """command-begin rejects invalid --effort values."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "events.jsonl"

        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "command-begin",
                "--command", "test-command",
                "--effort", "10",  # Invalid: outside 1-5 range
            ]
        )
        if code == 0:
            return False, "Should have rejected effort=10 but succeeded"

        return True, ""


def test_command_begin_rejects_invalid_mode():
    """command-begin rejects invalid --mode values."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "events.jsonl"

        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "command-begin",
                "--command", "test-command",
                "--mode", "invalid",  # Invalid: not in {local, pr, coworker}
            ]
        )
        if code == 0:
            return False, "Should have rejected mode=invalid but succeeded"

        return True, ""


def test_command_begin_omits_flags_when_not_provided():
    """command-begin creates event without effort/mode/reviewer_count when not provided."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "events.jsonl"

        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "command-begin",
                "--command", "test-command",
            ]
        )
        if code != 0:
            return False, f"Failed: {stderr}"

        event = json.loads(log_path.read_text().strip())
        if "effort" in event:
            return False, f"effort should not be in event when not provided, but got: {event.get('effort')}"
        if "mode" in event:
            return False, f"mode should not be in event when not provided, but got: {event.get('mode')}"
        if "reviewer_count" in event:
            return False, f"reviewer_count should not be in event when not provided, but got: {event.get('reviewer_count')}"

        return True, ""


# ============================================================================
# Section 2: stage-end with new flags
# ============================================================================


def test_stage_end_accepts_effort_flag():
    """stage-end accepts --effort flag with valid values."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "events.jsonl"

        for effort in ["1", "2", "3", "4", "5"]:
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
                return False, f"Failed with effort={effort}: {stderr}"

        # Verify events were logged
        if not log_path.exists():
            return False, "events.jsonl not created"

        lines = log_path.read_text().strip().split("\n")
        for i, line in enumerate(lines):
            event = json.loads(line)
            expected_effort = str(i + 1)
            if event.get("effort") != expected_effort:
                return False, f"Event {i} has effort={event.get('effort')}, expected {expected_effort}"

        return True, ""


def test_stage_end_accepts_mode_flag():
    """stage-end accepts --mode flag with valid values."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "events.jsonl"

        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "stage-end",
                "--stage", "test-stage",
                "--outcome", "success",
                "--mode", "coworker",
            ]
        )
        if code != 0:
            return False, f"Failed: {stderr}"

        event = json.loads(log_path.read_text().strip())
        if event.get("mode") != "coworker":
            return False, f"mode is {event.get('mode')}, expected 'coworker'"

        return True, ""


def test_stage_end_accepts_reviewer_count_flag():
    """stage-end accepts --reviewer-count flag."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "events.jsonl"

        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "stage-end",
                "--stage", "test-stage",
                "--outcome", "success",
                "--reviewer-count", "8",
            ]
        )
        if code != 0:
            return False, f"Failed: {stderr}"

        event = json.loads(log_path.read_text().strip())
        if event.get("reviewer_count") != 8:
            return False, f"reviewer_count is {event.get('reviewer_count')}, expected 8"

        return True, ""


def test_stage_end_accepts_model_flag():
    """stage-end accepts --model flag."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "events.jsonl"

        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "stage-end",
                "--stage", "route",
                "--outcome", "success",
                "--model", "haiku",
            ]
        )
        if code != 0:
            return False, f"Failed: {stderr}"

        event = json.loads(log_path.read_text().strip())
        if event.get("model") != "haiku":
            return False, f"model is {event.get('model')}, expected 'haiku'"

        return True, ""


def test_stage_end_with_all_new_flags():
    """stage-end accepts all four new flags together."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "events.jsonl"

        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "stage-end",
                "--stage", "amalgamate",
                "--outcome", "success",
                "--effort", "4",
                "--model", "sonnet",
                "--mode", "local",
                "--reviewer-count", "16",
            ]
        )
        if code != 0:
            return False, f"Failed: {stderr}"

        event = json.loads(log_path.read_text().strip())
        if event.get("effort") != "4":
            return False, f"effort is {event.get('effort')}, expected '4'"
        if event.get("model") != "sonnet":
            return False, f"model is {event.get('model')}, expected 'sonnet'"
        if event.get("mode") != "local":
            return False, f"mode is {event.get('mode')}, expected 'local'"
        if event.get("reviewer_count") != 16:
            return False, f"reviewer_count is {event.get('reviewer_count')}, expected 16"

        return True, ""


def test_stage_end_rejects_invalid_effort():
    """stage-end rejects invalid --effort values."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "events.jsonl"

        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "stage-end",
                "--stage", "test-stage",
                "--outcome", "success",
                "--effort", "6",  # Invalid
            ]
        )
        if code == 0:
            return False, "Should have rejected effort=6 but succeeded"

        return True, ""


def test_stage_end_rejects_invalid_mode():
    """stage-end rejects invalid --mode values."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "events.jsonl"

        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "stage-end",
                "--stage", "test-stage",
                "--outcome", "success",
                "--mode", "invalid",
            ]
        )
        if code == 0:
            return False, "Should have rejected mode=invalid but succeeded"

        return True, ""


def test_stage_end_omits_flags_when_not_provided():
    """stage-end creates event without effort/mode/reviewer_count when not provided."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "events.jsonl"

        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "stage-end",
                "--stage", "test-stage",
                "--outcome", "success",
            ]
        )
        if code != 0:
            return False, f"Failed: {stderr}"

        event = json.loads(log_path.read_text().strip())
        if "effort" in event:
            return False, f"effort should not be in event when not provided"
        if "mode" in event:
            return False, f"mode should not be in event when not provided"
        if "reviewer_count" in event:
            return False, f"reviewer_count should not be in event when not provided"

        return True, ""


if __name__ == "__main__":
    h = Harness("RUN_METRICS CLI EFFORT/MODE/REVIEWER_COUNT TEST SUITE")
    test_result = h.test_result

    print("[Section 1] command-begin with new flags")
    passed, msg = test_command_begin_accepts_effort_flag()
    test_result("command-begin accepts --effort flag (1-5)", passed, msg)

    passed, msg = test_command_begin_accepts_mode_flag()
    test_result("command-begin accepts --mode flag (local/pr/coworker)", passed, msg)

    passed, msg = test_command_begin_accepts_reviewer_count_flag()
    test_result("command-begin accepts --reviewer-count flag", passed, msg)

    passed, msg = test_command_begin_accepts_model_flag()
    test_result("command-begin accepts --model flag", passed, msg)

    passed, msg = test_command_begin_with_all_four_new_flags()
    test_result("command-begin accepts all four new flags together", passed, msg)

    passed, msg = test_command_begin_rejects_invalid_effort()
    test_result("command-begin rejects invalid --effort values", passed, msg)

    passed, msg = test_command_begin_rejects_invalid_mode()
    test_result("command-begin rejects invalid --mode values", passed, msg)

    passed, msg = test_command_begin_omits_flags_when_not_provided()
    test_result("command-begin omits fields when flags not provided", passed, msg)

    print()
    print("[Section 2] stage-end with new flags")
    passed, msg = test_stage_end_accepts_effort_flag()
    test_result("stage-end accepts --effort flag (1-5)", passed, msg)

    passed, msg = test_stage_end_accepts_mode_flag()
    test_result("stage-end accepts --mode flag", passed, msg)

    passed, msg = test_stage_end_accepts_reviewer_count_flag()
    test_result("stage-end accepts --reviewer-count flag", passed, msg)

    passed, msg = test_stage_end_accepts_model_flag()
    test_result("stage-end accepts --model flag", passed, msg)

    passed, msg = test_stage_end_with_all_new_flags()
    test_result("stage-end accepts all four new flags together", passed, msg)

    passed, msg = test_stage_end_rejects_invalid_effort()
    test_result("stage-end rejects invalid --effort values", passed, msg)

    passed, msg = test_stage_end_rejects_invalid_mode()
    test_result("stage-end rejects invalid --mode values", passed, msg)

    passed, msg = test_stage_end_omits_flags_when_not_provided()
    test_result("stage-end omits fields when flags not provided", passed, msg)

    print()
    h.summarize_and_exit()
