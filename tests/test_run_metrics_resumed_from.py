#!/usr/bin/env python3
"""
Test suite for run-metrics.py resumed_from field handling.

Tests the new --resumed-from CLI flag for command-begin subcommand,
and verifies schema, command-id peek behavior, and round-trip consistency.

Covers:
1. resumed_from presence when --resumed-from flag is provided
2. resumed_from absence when flag is not provided (key must not exist)
3. Empty-string --resumed-from is rejected
4. SCHEMA_VERSION remains at 1
5. command-id correctness and round-trip
6. command-id non-destructive (can be peeked repeatedly)
7. command-id empty-state behavior (returns empty on fresh state)

Run with: python3 tests/test_run_metrics_resumed_from.py
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
# Test 1: resumed_from presence
# ============================================================================


def test_1_resumed_from_presence():
    """--resumed-from flag results in resumed_from field in event."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "events.jsonl"
        state_dir = Path(tmpdir) / "state"
        test_id = "round1-xyz-123"
        env = dict(os.environ)
        env["CLAUDE_CODE_SESSION_ID"] = "test-1-resumed-from-presence"

        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "--state-dir", str(state_dir),
                "command-begin",
                "--command", "implement-with-haiku",
                "--resumed-from", test_id,
            ],
            env=env,
        )
        if code != 0:
            return False, f"Failed with --resumed-from: {stderr}"

        if not log_path.exists():
            return False, "events.jsonl not created"

        event = json.loads(log_path.read_text().strip())
        if event.get("resumed_from") != test_id:
            return False, f"resumed_from is {event.get('resumed_from')}, expected {test_id}"

        return True, ""


# ============================================================================
# Test 2: resumed_from absence
# ============================================================================


def test_2_resumed_from_absence():
    """Without --resumed-from, resumed_from key must not exist in event."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "events.jsonl"
        state_dir = Path(tmpdir) / "state"
        env = dict(os.environ)
        env["CLAUDE_CODE_SESSION_ID"] = "test-2-resumed-from-absence"

        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "--state-dir", str(state_dir),
                "command-begin",
                "--command", "implement-with-haiku",
            ],
            env=env,
        )
        if code != 0:
            return False, f"Failed: {stderr}"

        if not log_path.exists():
            return False, "events.jsonl not created"

        event = json.loads(log_path.read_text().strip())
        if "resumed_from" in event:
            return False, f"resumed_from should not exist, but got: {event.get('resumed_from')}"

        return True, ""


# ============================================================================
# Test 3: Empty-string resumed_from rejected
# ============================================================================


def test_3_empty_string_resumed_from_rejected():
    """Empty-string --resumed-from must be rejected, with no orphaned state entry."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "events.jsonl"
        state_dir = Path(tmpdir) / "state"
        session_id = "test-3-empty-string-rejected"
        env = dict(os.environ)
        env["CLAUDE_CODE_SESSION_ID"] = session_id

        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "--state-dir", str(state_dir),
                "command-begin",
                "--command", "implement-with-haiku",
                "--resumed-from", "",
            ],
            env=env,
        )
        # Should fail (non-zero exit)
        if code == 0:
            return False, "Empty --resumed-from should be rejected but succeeded"

        # Check that no orphaned state entry exists
        state_file = Path(state_dir) / f"{session_id}.json"
        if state_file.exists():
            with open(state_file) as f:
                state = json.load(f)
            commands = state.get("commands", {})
            if commands:
                return False, f"Orphaned state entry found after rejection: {commands}"

        return True, ""


# ============================================================================
# Test 4: SCHEMA_VERSION unchanged
# ============================================================================


def test_4_schema_version_unchanged():
    """SCHEMA_VERSION must be 1."""
    if telemetry_schema.SCHEMA_VERSION != 1:
        return False, f"SCHEMA_VERSION is {telemetry_schema.SCHEMA_VERSION}, expected 1"

    return True, ""


# ============================================================================
# Test 5: command-id correctness
# ============================================================================


def test_5_command_id_correctness():
    """command-id subcommand returns the same ID as command-begin printed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "events.jsonl"
        state_dir = Path(tmpdir) / "state"
        env = dict(os.environ)
        env["CLAUDE_CODE_SESSION_ID"] = "test-5-command-id-correctness"

        # First: run command-begin and capture the printed command_id
        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "--state-dir", str(state_dir),
                "command-begin",
                "--command", "implement-with-haiku",
            ],
            env=env,
        )
        if code != 0:
            return False, f"command-begin failed: {stderr}"

        # Parse stdout for command_id
        printed_id = stdout.strip()
        if not printed_id:
            return False, "command-begin printed no command_id"

        # Second: call command-id and verify it matches
        code2, stdout2, stderr2 = run_script(
            [
                "--log", str(log_path),
                "--state-dir", str(state_dir),
                "command-id",
                "--command", "implement-with-haiku",
            ],
            env=env,
        )
        if code2 != 0:
            return False, f"command-id failed: {stderr2}"

        retrieved_id = stdout2.strip()
        if retrieved_id != printed_id:
            return False, f"command-id returned {retrieved_id}, but command-begin printed {printed_id}"

        return True, ""


# ============================================================================
# Test 6: command-id non-destructive
# ============================================================================


def test_6_command_id_non_destructive():
    """Calling command-id twice returns same ID (never clears state)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "events.jsonl"
        state_dir = Path(tmpdir) / "state"
        env = dict(os.environ)
        env["CLAUDE_CODE_SESSION_ID"] = "test-6-command-id-non-destructive"

        # First: run command-begin
        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "--state-dir", str(state_dir),
                "command-begin",
                "--command", "implement-with-haiku",
            ],
            env=env,
        )
        if code != 0:
            return False, f"command-begin failed: {stderr}"

        printed_id = stdout.strip()

        # Second: call command-id first time
        code2, stdout2, stderr2 = run_script(
            [
                "--log", str(log_path),
                "--state-dir", str(state_dir),
                "command-id",
                "--command", "implement-with-haiku",
            ],
            env=env,
        )
        if code2 != 0:
            return False, f"command-id (first call) failed: {stderr2}"

        first_call = stdout2.strip()

        # Third: call command-id second time
        code3, stdout3, stderr3 = run_script(
            [
                "--log", str(log_path),
                "--state-dir", str(state_dir),
                "command-id",
                "--command", "implement-with-haiku",
            ],
            env=env,
        )
        if code3 != 0:
            return False, f"command-id (second call) failed: {stderr3}"

        second_call = stdout3.strip()

        if first_call != second_call:
            return False, f"command-id changed: first={first_call}, second={second_call}"

        if first_call != printed_id:
            return False, f"command-id differs from original: original={printed_id}, second call={second_call}"

        return True, ""


# ============================================================================
# Test 7: command-id empty-state behavior
# ============================================================================


def test_7_command_id_empty_state():
    """command-id on fresh empty state returns 0 exit with empty stdout."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "events.jsonl"
        state_dir = Path(tmpdir) / "state"
        env = dict(os.environ)
        env["CLAUDE_CODE_SESSION_ID"] = "test-7-command-id-empty-state"

        # Call command-id without any prior command-begin
        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "--state-dir", str(state_dir),
                "command-id",
                "--command", "implement-with-haiku",
            ],
            env=env,
        )

        if code != 0:
            return False, f"command-id should exit 0 on empty state, but got {code}: {stderr}"

        output = stdout.strip()
        if output:
            return False, f"command-id should return empty stdout on empty state, but got: {output}"

        return True, ""


if __name__ == "__main__":
    h = Harness("RUN_METRICS RESUMED_FROM TEST SUITE")
    test_result = h.test_result

    print("[Tests] resumed_from field and command-id behavior")

    passed, msg = test_1_resumed_from_presence()
    test_result("resumed_from presence: --resumed-from flag sets field", passed, msg)

    passed, msg = test_2_resumed_from_absence()
    test_result("resumed_from absence: no flag means key absent (not null)", passed, msg)

    passed, msg = test_3_empty_string_resumed_from_rejected()
    test_result("Empty-string --resumed-from rejected", passed, msg)

    passed, msg = test_4_schema_version_unchanged()
    test_result("SCHEMA_VERSION unchanged (== 1)", passed, msg)

    passed, msg = test_5_command_id_correctness()
    test_result("command-id correctness: matches command-begin output", passed, msg)

    passed, msg = test_6_command_id_non_destructive()
    test_result("command-id non-destructive: repeated calls return same ID", passed, msg)

    passed, msg = test_7_command_id_empty_state()
    test_result("command-id empty-state behavior: exit 0, empty stdout", passed, msg)

    print()
    h.summarize_and_exit()
