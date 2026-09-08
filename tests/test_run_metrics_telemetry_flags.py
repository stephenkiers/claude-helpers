#!/usr/bin/env python3
"""
Test suite for new telemetry flags on run-metrics.py: --turns, --retries, --output-artifact-size.

Per GitHub issue #140 (telemetry sub-task), these flags were added to command-end and stage-end
subcommands (and ONLY those subcommands). When passed, they must appear in the emitted JSON event
as integers under matching keys. When omitted, the event must contain the UNKNOWN sentinel
("unknown" string).

Tests cover:
1. command-end with each new flag individually
2. command-end with all three flags together
3. stage-end with each new flag individually
4. stage-end with all three flags together
5. command-end without flags (should default to "unknown")
6. stage-end without flags (should default to "unknown")
7. Edge case: --turns 0 (zero is a valid count, not same as omitted)
8. Edge case: --retries 0 (zero is a valid count, not same as omitted)
9. Edge case: --output-artifact-size 0 (zero is a valid size, not same as omitted)
10. session-begin rejects --turns (flag should not exist on this subcommand)
11. agent-begin rejects --turns (flag should not exist on this subcommand)

Run with: python3 tests/test_run_metrics_telemetry_flags.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from _test_harness import REPO_ROOT, Harness

SCRIPT = REPO_ROOT / "scripts" / "run-metrics.py"


def run_script(args, stdin_text=None, env=None):
    """Run the script as a subprocess. Returns (returncode, stdout, stderr)."""
    cmd = [sys.executable, str(SCRIPT)] + args
    result = subprocess.run(
        cmd,
        input=stdin_text,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


# ============================================================================
# Tests for command-end with new telemetry flags
# ============================================================================

def test_command_end_with_turns():
    """command-end --turns N writes turns integer to the event."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "command-end",
                "--command-id", "c123",
                "--command", "test",
                "--outcome", "success",
                "--turns", "5",
            ],
        )
        if code != 0:
            return False, f"exit code {code}, stderr: {stderr}"

        with open(log_path) as f:
            event = json.loads(f.readline())

        if event.get("turns") != 5:
            return False, f"expected turns=5, got {event.get('turns')!r}"

        return True, ""


def test_command_end_with_retries():
    """command-end --retries N writes retries integer to the event."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "command-end",
                "--command-id", "c123",
                "--command", "test",
                "--outcome", "success",
                "--retries", "3",
            ],
        )
        if code != 0:
            return False, f"exit code {code}, stderr: {stderr}"

        with open(log_path) as f:
            event = json.loads(f.readline())

        if event.get("retries") != 3:
            return False, f"expected retries=3, got {event.get('retries')!r}"

        return True, ""


def test_command_end_with_output_artifact_size():
    """command-end --output-artifact-size N writes output_artifact_size integer to the event."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "command-end",
                "--command-id", "c123",
                "--command", "test",
                "--outcome", "success",
                "--output-artifact-size", "12345",
            ],
        )
        if code != 0:
            return False, f"exit code {code}, stderr: {stderr}"

        with open(log_path) as f:
            event = json.loads(f.readline())

        if event.get("output_artifact_size") != 12345:
            return False, f"expected output_artifact_size=12345, got {event.get('output_artifact_size')!r}"

        return True, ""


def test_command_end_with_all_three_flags():
    """command-end with --turns, --retries, --output-artifact-size all specified."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "command-end",
                "--command-id", "c123",
                "--command", "test",
                "--outcome", "success",
                "--turns", "7",
                "--retries", "2",
                "--output-artifact-size", "54321",
            ],
        )
        if code != 0:
            return False, f"exit code {code}, stderr: {stderr}"

        with open(log_path) as f:
            event = json.loads(f.readline())

        if event.get("turns") != 7:
            return False, f"expected turns=7, got {event.get('turns')!r}"
        if event.get("retries") != 2:
            return False, f"expected retries=2, got {event.get('retries')!r}"
        if event.get("output_artifact_size") != 54321:
            return False, f"expected output_artifact_size=54321, got {event.get('output_artifact_size')!r}"

        return True, ""


def test_command_end_without_telemetry_flags_defaults_to_unknown():
    """command-end without --turns/--retries/--output-artifact-size defaults to "unknown"."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "command-end",
                "--command-id", "c123",
                "--command", "test",
                "--outcome", "success",
            ],
        )
        if code != 0:
            return False, f"exit code {code}, stderr: {stderr}"

        with open(log_path) as f:
            event = json.loads(f.readline())

        if event.get("turns") != "unknown":
            return False, f"expected turns='unknown', got {event.get('turns')!r}"
        if event.get("retries") != "unknown":
            return False, f"expected retries='unknown', got {event.get('retries')!r}"
        if event.get("output_artifact_size") != "unknown":
            return False, f"expected output_artifact_size='unknown', got {event.get('output_artifact_size')!r}"

        return True, ""


def test_command_end_with_turns_zero():
    """command-end --turns 0 writes 0 (not treated as omitted)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "command-end",
                "--command-id", "c123",
                "--command", "test",
                "--outcome", "success",
                "--turns", "0",
            ],
        )
        if code != 0:
            return False, f"exit code {code}, stderr: {stderr}"

        with open(log_path) as f:
            event = json.loads(f.readline())

        # Zero is a valid value, must not be treated as "omitted"
        if event.get("turns") != 0:
            return False, f"expected turns=0, got {event.get('turns')!r}"

        return True, ""


def test_command_end_with_retries_zero():
    """command-end --retries 0 writes 0 (not treated as omitted)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "command-end",
                "--command-id", "c123",
                "--command", "test",
                "--outcome", "success",
                "--retries", "0",
            ],
        )
        if code != 0:
            return False, f"exit code {code}, stderr: {stderr}"

        with open(log_path) as f:
            event = json.loads(f.readline())

        if event.get("retries") != 0:
            return False, f"expected retries=0, got {event.get('retries')!r}"

        return True, ""


def test_command_end_with_output_artifact_size_zero():
    """command-end --output-artifact-size 0 writes 0 (not treated as omitted)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "command-end",
                "--command-id", "c123",
                "--command", "test",
                "--outcome", "success",
                "--output-artifact-size", "0",
            ],
        )
        if code != 0:
            return False, f"exit code {code}, stderr: {stderr}"

        with open(log_path) as f:
            event = json.loads(f.readline())

        if event.get("output_artifact_size") != 0:
            return False, f"expected output_artifact_size=0, got {event.get('output_artifact_size')!r}"

        return True, ""


# ============================================================================
# Tests for stage-end with new telemetry flags
# ============================================================================

def test_stage_end_with_turns():
    """stage-end --turns N writes turns integer to the event."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "stage-end",
                "--stage-id", "st1",
                "--command-id", "c1",
                "--stage", "build",
                "--outcome", "success",
                "--turns", "4",
            ],
        )
        if code != 0:
            return False, f"exit code {code}, stderr: {stderr}"

        with open(log_path) as f:
            event = json.loads(f.readline())

        if event.get("turns") != 4:
            return False, f"expected turns=4, got {event.get('turns')!r}"

        return True, ""


def test_stage_end_with_retries():
    """stage-end --retries N writes retries integer to the event."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "stage-end",
                "--stage-id", "st1",
                "--command-id", "c1",
                "--stage", "build",
                "--outcome", "success",
                "--retries", "1",
            ],
        )
        if code != 0:
            return False, f"exit code {code}, stderr: {stderr}"

        with open(log_path) as f:
            event = json.loads(f.readline())

        if event.get("retries") != 1:
            return False, f"expected retries=1, got {event.get('retries')!r}"

        return True, ""


def test_stage_end_with_output_artifact_size():
    """stage-end --output-artifact-size N writes output_artifact_size integer to the event."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "stage-end",
                "--stage-id", "st1",
                "--command-id", "c1",
                "--stage", "build",
                "--outcome", "success",
                "--output-artifact-size", "99999",
            ],
        )
        if code != 0:
            return False, f"exit code {code}, stderr: {stderr}"

        with open(log_path) as f:
            event = json.loads(f.readline())

        if event.get("output_artifact_size") != 99999:
            return False, f"expected output_artifact_size=99999, got {event.get('output_artifact_size')!r}"

        return True, ""


def test_stage_end_with_all_three_flags():
    """stage-end with --turns, --retries, --output-artifact-size all specified."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "stage-end",
                "--stage-id", "st1",
                "--command-id", "c1",
                "--stage", "build",
                "--outcome", "success",
                "--turns", "6",
                "--retries", "4",
                "--output-artifact-size", "77777",
            ],
        )
        if code != 0:
            return False, f"exit code {code}, stderr: {stderr}"

        with open(log_path) as f:
            event = json.loads(f.readline())

        if event.get("turns") != 6:
            return False, f"expected turns=6, got {event.get('turns')!r}"
        if event.get("retries") != 4:
            return False, f"expected retries=4, got {event.get('retries')!r}"
        if event.get("output_artifact_size") != 77777:
            return False, f"expected output_artifact_size=77777, got {event.get('output_artifact_size')!r}"

        return True, ""


def test_stage_end_without_telemetry_flags_defaults_to_unknown():
    """stage-end without --turns/--retries/--output-artifact-size defaults to "unknown"."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "stage-end",
                "--stage-id", "st1",
                "--command-id", "c1",
                "--stage", "build",
                "--outcome", "success",
            ],
        )
        if code != 0:
            return False, f"exit code {code}, stderr: {stderr}"

        with open(log_path) as f:
            event = json.loads(f.readline())

        if event.get("turns") != "unknown":
            return False, f"expected turns='unknown', got {event.get('turns')!r}"
        if event.get("retries") != "unknown":
            return False, f"expected retries='unknown', got {event.get('retries')!r}"
        if event.get("output_artifact_size") != "unknown":
            return False, f"expected output_artifact_size='unknown', got {event.get('output_artifact_size')!r}"

        return True, ""


def test_stage_end_with_turns_zero():
    """stage-end --turns 0 writes 0 (not treated as omitted)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "stage-end",
                "--stage-id", "st1",
                "--command-id", "c1",
                "--stage", "build",
                "--outcome", "success",
                "--turns", "0",
            ],
        )
        if code != 0:
            return False, f"exit code {code}, stderr: {stderr}"

        with open(log_path) as f:
            event = json.loads(f.readline())

        if event.get("turns") != 0:
            return False, f"expected turns=0, got {event.get('turns')!r}"

        return True, ""


# ============================================================================
# Tests for scope enforcement (these flags should NOT exist on other subcommands)
# ============================================================================

def test_session_begin_rejects_turns_flag():
    """session-begin should not accept --turns (flag is only for command-end/stage-end)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        payload = json.dumps({"session_id": "s1", "cwd": "/tmp"})
        code, stdout, stderr = run_script(
            ["--log", str(log_path), "session-begin", "--turns", "5"],
            stdin_text=payload,
        )
        # Should fail: unrecognized argument
        if code == 0:
            return False, "session-begin should reject --turns flag"
        if "unrecognized" not in stderr.lower() and "error" not in stderr.lower():
            return False, f"stderr should indicate unrecognized argument, got: {stderr}"

        return True, ""


def test_session_begin_rejects_retries_flag():
    """session-begin should not accept --retries (flag is only for command-end/stage-end)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        payload = json.dumps({"session_id": "s1", "cwd": "/tmp"})
        code, stdout, stderr = run_script(
            ["--log", str(log_path), "session-begin", "--retries", "3"],
            stdin_text=payload,
        )
        # Should fail: unrecognized argument
        if code == 0:
            return False, "session-begin should reject --retries flag"

        return True, ""


def test_session_begin_rejects_output_artifact_size_flag():
    """session-begin should not accept --output-artifact-size (flag is only for command-end/stage-end)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        payload = json.dumps({"session_id": "s1", "cwd": "/tmp"})
        code, stdout, stderr = run_script(
            ["--log", str(log_path), "session-begin", "--output-artifact-size", "1000"],
            stdin_text=payload,
        )
        # Should fail: unrecognized argument
        if code == 0:
            return False, "session-begin should reject --output-artifact-size flag"

        return True, ""


def test_agent_begin_rejects_turns_flag():
    """agent-begin should not accept --turns (flag is only for command-end/stage-end)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        payload = json.dumps({
            "session_id": "s1",
            "agent_id": "a1",
            "agent_type": "general",
        })
        code, stdout, stderr = run_script(
            ["--log", str(log_path), "agent-begin", "--turns", "5"],
            stdin_text=payload,
        )
        # Should fail: unrecognized argument
        if code == 0:
            return False, "agent-begin should reject --turns flag"

        return True, ""


if __name__ == "__main__":
    h = Harness("TELEMETRY FLAGS TEST SUITE")

    test_result = h.test_result

    print("[Section 1] command-end: individual flags")
    passed, msg = test_command_end_with_turns()
    test_result("command-end --turns writes turns integer", passed, msg)

    passed, msg = test_command_end_with_retries()
    test_result("command-end --retries writes retries integer", passed, msg)

    passed, msg = test_command_end_with_output_artifact_size()
    test_result("command-end --output-artifact-size writes integer", passed, msg)

    print()

    print("[Section 2] command-end: combined and omitted")
    passed, msg = test_command_end_with_all_three_flags()
    test_result("command-end with all three flags", passed, msg)

    passed, msg = test_command_end_without_telemetry_flags_defaults_to_unknown()
    test_result("command-end without flags defaults to 'unknown'", passed, msg)

    print()

    print("[Section 3] command-end: edge cases (zero values)")
    passed, msg = test_command_end_with_turns_zero()
    test_result("command-end --turns 0 is 0 (not 'unknown')", passed, msg)

    passed, msg = test_command_end_with_retries_zero()
    test_result("command-end --retries 0 is 0 (not 'unknown')", passed, msg)

    passed, msg = test_command_end_with_output_artifact_size_zero()
    test_result("command-end --output-artifact-size 0 is 0 (not 'unknown')", passed, msg)

    print()

    print("[Section 4] stage-end: individual flags")
    passed, msg = test_stage_end_with_turns()
    test_result("stage-end --turns writes turns integer", passed, msg)

    passed, msg = test_stage_end_with_retries()
    test_result("stage-end --retries writes retries integer", passed, msg)

    passed, msg = test_stage_end_with_output_artifact_size()
    test_result("stage-end --output-artifact-size writes integer", passed, msg)

    print()

    print("[Section 5] stage-end: combined and omitted")
    passed, msg = test_stage_end_with_all_three_flags()
    test_result("stage-end with all three flags", passed, msg)

    passed, msg = test_stage_end_without_telemetry_flags_defaults_to_unknown()
    test_result("stage-end without flags defaults to 'unknown'", passed, msg)

    print()

    print("[Section 6] stage-end: edge cases (zero values)")
    passed, msg = test_stage_end_with_turns_zero()
    test_result("stage-end --turns 0 is 0 (not 'unknown')", passed, msg)

    print()

    print("[Section 7] Scope enforcement (flags only on command-end/stage-end)")
    passed, msg = test_session_begin_rejects_turns_flag()
    test_result("session-begin rejects --turns", passed, msg)

    passed, msg = test_session_begin_rejects_retries_flag()
    test_result("session-begin rejects --retries", passed, msg)

    passed, msg = test_session_begin_rejects_output_artifact_size_flag()
    test_result("session-begin rejects --output-artifact-size", passed, msg)

    passed, msg = test_agent_begin_rejects_turns_flag()
    test_result("agent-begin rejects --turns", passed, msg)

    print()

    h.summarize_and_exit()
