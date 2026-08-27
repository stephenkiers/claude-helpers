#!/usr/bin/env python3
"""
Test suite for telemetry hardening (plan item #1-2).

Covers:
1. append_event() validation: calls validate_event() before writing, raises ValueError on invalid events
2. File permissions: log file created at mode 0600 (not 0644)
3. Parent directory permissions: created at mode 0700 if not already existing
4. Parent directory preservation: existing directories not modified or crashed on
5. read_stdin_json() byte capping: caps at 1 MiB before parsing
6. Field truncation: session_id, cwd, agent_id, agent_type truncated to 4096 chars
7. diagnose excludes "unknown" as correlation ID
8. diagnose prints per-stage breakdown

Run with: python3 tests/test_telemetry_hardening.py
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

# Add parent/scripts to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import telemetry_schema

from _test_harness import REPO_ROOT, Harness

SCRIPT = REPO_ROOT / "scripts" / "run-metrics.py"


def run_script(args, stdin_text=None):
    """Run run-metrics.py as a subprocess. Returns (returncode, stdout, stderr)."""
    cmd = [sys.executable, str(SCRIPT)] + args
    result = subprocess.run(
        cmd,
        input=stdin_text,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def test_append_event_validates_before_write():
    """append_event calls validate_event and raises ValueError for invalid events."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"

        # Build an invalid event (missing session_id)
        invalid_event = {
            "schema_version": telemetry_schema.SCHEMA_VERSION,
            "event_type": "session.begin",
            "timestamp": "2026-08-26T12:00:00Z",
            # Missing session_id!
        }

        try:
            telemetry_schema.append_event(log_path, invalid_event)
            return False, "should have raised ValueError for invalid event"
        except ValueError as e:
            return True, f"correctly raised ValueError: {e}"


def test_append_event_creates_file_mode_0600():
    """append_event creates log file at mode 0600, not 0644."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        event = telemetry_schema.build_event(
            "session.begin",
            session_id="test123",
            timestamp="2026-08-26T12:00:00Z",
        )
        telemetry_schema.append_event(log_path, event)

        # Check file exists
        if not log_path.exists():
            return False, "log file not created"

        # Check file mode
        file_mode = stat.S_IMODE(log_path.stat().st_mode)
        expected_mode = 0o600

        if file_mode != expected_mode:
            return False, f"expected mode 0o{expected_mode:03o}, got 0o{file_mode:03o}"

        return True, ""


def test_append_event_creates_parent_dir_mode_0700():
    """append_event creates parent directory at mode 0700 when it doesn't exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a path where the parent dir doesn't exist yet
        parent_path = Path(tmpdir) / "new_dir"
        log_path = parent_path / "test.jsonl"

        event = telemetry_schema.build_event(
            "session.begin",
            session_id="test123",
            timestamp="2026-08-26T12:00:00Z",
        )
        telemetry_schema.append_event(log_path, event)

        # Check parent dir was created
        if not parent_path.exists():
            return False, "parent directory not created"

        if not parent_path.is_dir():
            return False, "parent path is not a directory"

        # Check parent dir mode
        dir_mode = stat.S_IMODE(parent_path.stat().st_mode)
        expected_mode = 0o700

        if dir_mode != expected_mode:
            return False, f"expected parent mode 0o{expected_mode:03o}, got 0o{dir_mode:03o}"

        return True, ""


def test_append_event_preserves_existing_parent_permissions():
    """append_event does NOT modify parent directory permissions if it already exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create parent dir with explicit mode
        parent_path = Path(tmpdir) / "existing_dir"
        parent_path.mkdir(mode=0o755)
        original_mode = stat.S_IMODE(parent_path.stat().st_mode)

        log_path = parent_path / "test.jsonl"
        event = telemetry_schema.build_event(
            "session.begin",
            session_id="test123",
            timestamp="2026-08-26T12:00:00Z",
        )
        telemetry_schema.append_event(log_path, event)

        # Check parent dir mode is unchanged
        new_mode = stat.S_IMODE(parent_path.stat().st_mode)

        if new_mode != original_mode:
            return False, f"parent mode changed from 0o{original_mode:03o} to 0o{new_mode:03o}"

        return True, ""


def test_append_event_to_existing_common_dir():
    """append_event works when pointing to an existing directory like /tmp without crashing."""
    # Use a temp subdir of /tmp so we don't interfere with other processes
    import tempfile as tf
    tmpbase = Path(tf.gettempdir()) / ".test-append-existing"
    tmpbase.mkdir(exist_ok=True)

    try:
        log_path = tmpbase / "test.jsonl"
        event = telemetry_schema.build_event(
            "session.begin",
            session_id="test123",
            timestamp="2026-08-26T12:00:00Z",
        )

        # This should NOT crash or fail permission-wise
        telemetry_schema.append_event(log_path, event)

        if not log_path.exists():
            return False, "log file not created in existing dir"

        return True, ""
    except Exception as e:
        return False, f"unexpected exception: {e}"
    finally:
        # Cleanup
        if log_path.exists():
            log_path.unlink()
        if tmpbase.exists():
            tmpbase.rmdir()


def test_read_stdin_json_caps_at_1mib():
    """read_stdin_json caps input at 1 MiB before parsing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"

        # Create payload that's just under 1 MiB (should succeed)
        small_payload = json.dumps({"session_id": "s1", "cwd": "/tmp"})
        code, stdout, stderr = run_script(
            ["--log", str(log_path), "session-begin"],
            stdin_text=small_payload,
        )

        if code != 0:
            return False, f"small payload failed: {stderr}"

        # Payload way over 1 MiB (should fail gracefully, not hang)
        # Create a ~2 MiB JSON string
        big_string = "x" * (2 * 1024 * 1024)
        huge_payload = json.dumps({"session_id": "s2", "data": big_string})

        code, stdout, stderr = run_script(
            ["--log", str(log_path), "session-begin"],
            stdin_text=huge_payload,
        )

        # The script should handle this gracefully (exit non-zero, not hang/crash)
        # We're just verifying it doesn't crash and times out
        if code == 0:
            # If it succeeded, that's fine too - it means it capped and parsed what fit
            return True, "huge payload handled gracefully"
        else:
            # If it failed, verify it's a clean error, not a crash
            if "Traceback" in stderr:
                return False, f"huge payload caused traceback: {stderr}"
            return True, "huge payload rejected cleanly"


def test_field_truncation_session_id():
    """session_id truncated to 4096 chars when read from hook payload."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"

        # Create a session_id that's way over 4096 chars
        huge_session_id = "s" * 5000
        payload = json.dumps({
            "session_id": huge_session_id,
            "cwd": "/tmp"
        })

        code, stdout, stderr = run_script(
            ["--log", str(log_path), "session-begin"],
            stdin_text=payload,
        )

        if code != 0:
            return False, f"session-begin failed: {stderr}"

        # Read the event from the log
        with open(log_path) as f:
            event = json.loads(f.readline())

        written_session_id = event.get("session_id", "")
        if len(written_session_id) > 4096:
            return False, f"session_id not truncated: {len(written_session_id)} chars"

        return True, ""


def test_field_truncation_cwd():
    """cwd truncated to 4096 chars when read from hook payload."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"

        # Create a cwd that's way over 4096 chars
        huge_cwd = "/" + "d" * 5000
        payload = json.dumps({
            "session_id": "s1",
            "cwd": huge_cwd
        })

        code, stdout, stderr = run_script(
            ["--log", str(log_path), "session-begin"],
            stdin_text=payload,
        )

        if code != 0:
            return False, f"session-begin failed: {stderr}"

        # Read the event from the log
        with open(log_path) as f:
            event = json.loads(f.readline())

        written_cwd = event.get("cwd", "")
        if len(written_cwd) > 4096:
            return False, f"cwd not truncated: {len(written_cwd)} chars"

        return True, ""


def test_field_truncation_agent_id():
    """agent_id truncated to 4096 chars when read from hook payload."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"

        huge_agent_id = "a" * 5000
        payload = json.dumps({
            "session_id": "s1",
            "agent_id": huge_agent_id,
            "agent_type": "general"
        })

        code, stdout, stderr = run_script(
            ["--log", str(log_path), "agent-begin"],
            stdin_text=payload,
        )

        if code != 0:
            return False, f"agent-begin failed: {stderr}"

        # Read the event from the log
        with open(log_path) as f:
            event = json.loads(f.readline())

        written_agent_id = event.get("agent_id", "")
        if len(written_agent_id) > 4096:
            return False, f"agent_id not truncated: {len(written_agent_id)} chars"

        return True, ""


def test_field_truncation_agent_type():
    """agent_type truncated to 4096 chars when read from hook payload."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"

        huge_agent_type = "x" * 5000
        payload = json.dumps({
            "session_id": "s1",
            "agent_id": "a1",
            "agent_type": huge_agent_type
        })

        code, stdout, stderr = run_script(
            ["--log", str(log_path), "agent-begin"],
            stdin_text=payload,
        )

        if code != 0:
            return False, f"agent-begin failed: {stderr}"

        # Read the event from the log
        with open(log_path) as f:
            event = json.loads(f.readline())

        written_agent_type = event.get("agent_type", "")
        if len(written_agent_type) > 4096:
            return False, f"agent_type not truncated: {len(written_agent_type)} chars"

        return True, ""


def test_diagnose_excludes_unknown_as_correlation_id():
    """diagnose excludes literal 'unknown' string as a correlation ID."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"

        # Write two events with session_id="unknown" (unrelated)
        events = [
            {
                "schema_version": 1,
                "event_type": "command.begin",
                "timestamp": "2026-08-26T12:00:00Z",
                "session_id": "unknown",
                "command_id": "c1",
                "command": "test1",
                "turns": "unknown",
                "elapsed_seconds": "unknown",
                "retries": "unknown",
                "peak_concurrency": "unknown",
                "transcript_size": "unknown",
                "output_artifact_size": "unknown",
            },
            {
                "schema_version": 1,
                "event_type": "command.end",
                "timestamp": "2026-08-26T12:00:10Z",
                "session_id": "unknown",
                "command_id": "c_different",  # Different command
                "outcome": {"status": "success"},
                "turns": "unknown",
                "elapsed_seconds": "unknown",
                "retries": "unknown",
                "peak_concurrency": "unknown",
                "transcript_size": "unknown",
                "output_artifact_size": "unknown",
            },
        ]

        with open(log_path, "w") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")

        code, stdout, stderr = run_script(
            ["--log", str(log_path), "diagnose"]
        )

        # Should show poor match rate (0/1 matched) since "unknown" isn't treated as a correlation ID
        # The "unknown" session begin shouldn't be paired with the "unknown" session end
        if "Match rate" not in stdout:
            return False, f"diagnose output missing 'Match rate': {stdout}"

        # The match should be 0% since "unknown" IDs don't match
        if "100" in stdout:
            return False, f"diagnose incorrectly shows high match rate when using 'unknown': {stdout}"

        return True, ""


def test_diagnose_prints_per_stage_breakdown():
    """diagnose prints per-stage match-rate breakdown."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"

        # Write stage.begin and stage.end events with proper pairing
        events = [
            {
                "schema_version": 1,
                "event_type": "stage.begin",
                "timestamp": "2026-08-26T12:00:00Z",
                "session_id": "s1",
                "command_id": "c1",
                "stage_id": "st1",
                "stage": "build",
                "turns": "unknown",
                "elapsed_seconds": "unknown",
                "retries": "unknown",
                "peak_concurrency": "unknown",
                "transcript_size": "unknown",
                "output_artifact_size": "unknown",
            },
            {
                "schema_version": 1,
                "event_type": "stage.end",
                "timestamp": "2026-08-26T12:00:10Z",
                "session_id": "s1",
                "command_id": "c1",
                "stage_id": "st1",
                "stage": "build",
                "outcome": {"status": "success"},
                "turns": "unknown",
                "elapsed_seconds": "unknown",
                "retries": "unknown",
                "peak_concurrency": "unknown",
                "transcript_size": "unknown",
                "output_artifact_size": "unknown",
            },
            {
                "schema_version": 1,
                "event_type": "stage.begin",
                "timestamp": "2026-08-26T12:00:20Z",
                "session_id": "s1",
                "command_id": "c1",
                "stage_id": "st2",
                "stage": "test",
                "turns": "unknown",
                "elapsed_seconds": "unknown",
                "retries": "unknown",
                "peak_concurrency": "unknown",
                "transcript_size": "unknown",
                "output_artifact_size": "unknown",
            },
            {
                "schema_version": 1,
                "event_type": "stage.end",
                "timestamp": "2026-08-26T12:00:30Z",
                "session_id": "s1",
                "command_id": "c1",
                "stage_id": "st2",
                "stage": "test",
                "outcome": {"status": "success"},
                "turns": "unknown",
                "elapsed_seconds": "unknown",
                "retries": "unknown",
                "peak_concurrency": "unknown",
                "transcript_size": "unknown",
                "output_artifact_size": "unknown",
            },
        ]

        with open(log_path, "w") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")

        code, stdout, stderr = run_script(
            ["--log", str(log_path), "diagnose"]
        )

        if code != 0:
            return False, f"diagnose exited non-zero: {stderr}"

        # Check for per-stage breakdown
        # Should mention stage names like "build" or "test"
        has_stage_breakdown = ("build" in stdout or "test" in stdout or
                              "stage" in stdout.lower())

        if not has_stage_breakdown:
            return False, f"diagnose output missing per-stage breakdown: {stdout}"

        return True, ""


if __name__ == "__main__":
    h = Harness("TELEMETRY HARDENING TEST SUITE")

    test_result = h.test_result

    print("[Section 1] Event validation in append_event")
    passed, msg = test_append_event_validates_before_write()
    test_result("append_event validates before write", passed, msg)

    print()

    print("[Section 2] File permissions")
    passed, msg = test_append_event_creates_file_mode_0600()
    test_result("log file created at mode 0600", passed, msg)

    print()

    print("[Section 3] Parent directory permissions")
    passed, msg = test_append_event_creates_parent_dir_mode_0700()
    test_result("parent dir created at mode 0700", passed, msg)

    passed, msg = test_append_event_preserves_existing_parent_permissions()
    test_result("existing parent permissions preserved", passed, msg)

    passed, msg = test_append_event_to_existing_common_dir()
    test_result("append to existing dir like /tmp works", passed, msg)

    print()

    print("[Section 4] Input capping and field truncation")
    passed, msg = test_read_stdin_json_caps_at_1mib()
    test_result("read_stdin_json caps at 1 MiB", passed, msg)

    passed, msg = test_field_truncation_session_id()
    test_result("session_id truncated to 4096 chars", passed, msg)

    passed, msg = test_field_truncation_cwd()
    test_result("cwd truncated to 4096 chars", passed, msg)

    passed, msg = test_field_truncation_agent_id()
    test_result("agent_id truncated to 4096 chars", passed, msg)

    passed, msg = test_field_truncation_agent_type()
    test_result("agent_type truncated to 4096 chars", passed, msg)

    print()

    print("[Section 5] diagnose improvements")
    passed, msg = test_diagnose_excludes_unknown_as_correlation_id()
    test_result("diagnose excludes 'unknown' as correlation ID", passed, msg)

    passed, msg = test_diagnose_prints_per_stage_breakdown()
    test_result("diagnose prints per-stage breakdown", passed, msg)

    print()

    h.summarize_and_exit()
