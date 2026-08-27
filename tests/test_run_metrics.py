#!/usr/bin/env python3
"""
Test suite for run-metrics.py CLI.

Covers: all subcommands, stdin parsing, JSON output, privacy redaction,
and the diagnose command's match rate calculation.

Run with: python3 tests/test_run_metrics.py
"""

import json
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


def test_session_begin():
    """session-begin reads JSON from stdin and writes an event."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        payload = json.dumps({"session_id": "s123", "cwd": "/tmp/repo"})
        code, stdout, stderr = run_script(
            ["--log", str(log_path), "session-begin"],
            stdin_text=payload,
        )
        if code != 0:
            return False, f"exit code {code}, stderr: {stderr}"

        # Check log file
        if not log_path.exists():
            return False, "log file not created"

        with open(log_path) as f:
            event = json.loads(f.readline())

        if event.get("event_type") != "session.begin":
            return False, f"wrong event_type: {event.get('event_type')}"
        if event.get("session_id") != "s123":
            return False, f"wrong session_id: {event.get('session_id')}"

        return True, ""


def test_session_begin_bad_json():
    """session-begin with garbage JSON on stdin exits non-zero."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        code, stdout, stderr = run_script(
            ["--log", str(log_path), "session-begin"],
            stdin_text="not json",
        )
        if code == 0:
            return False, "should have exited non-zero"
        if not stderr or "Error" not in stderr:
            return False, f"stderr should contain error message, got: {stderr!r}"

        return True, ""


def test_agent_end_redacts_message():
    """agent-end reads JSON but does NOT write last_assistant_message."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        payload = json.dumps({
            "session_id": "s123",
            "agent_id": "a456",
            "agent_type": "general",
            "last_assistant_message": "secret content here",
        })
        code, stdout, stderr = run_script(
            ["--log", str(log_path), "agent-end"],
            stdin_text=payload,
        )
        if code != 0:
            return False, f"exit code {code}, stderr: {stderr}"

        # Read log file and check secret is not present
        with open(log_path) as f:
            log_content = f.read()

        if "secret content" in log_content:
            return False, "last_assistant_message leaked to log"

        return True, ""


def test_command_begin_prints_id():
    """command-begin prints bare command_id to stdout."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        code, stdout, stderr = run_script(
            ["--log", str(log_path), "command-begin", "--command", "test-cmd"],
            env={**__import__("os").environ, "CLAUDE_CODE_SESSION_ID": "s123"},
        )
        if code != 0:
            return False, f"exit code {code}"

        cmd_id = stdout.strip()
        if not cmd_id or len(cmd_id) != 32:  # hex uuid
            return False, f"stdout should be hex uuid, got: {stdout!r}"

        # Check log file has the event with this command_id
        with open(log_path) as f:
            event = json.loads(f.readline())

        if event.get("command_id") != cmd_id:
            return False, f"event command_id doesn't match stdout"

        return True, ""


def test_command_end_failure_requires_class():
    """command-end with --outcome failure but no --failure-class exits non-zero."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "command-end",
                "--command-id", "c123",
                "--command", "test",
                "--outcome", "failure",
            ],
        )
        if code == 0:
            return False, "should have exited non-zero"
        if "Error" not in stderr:
            return False, f"stderr should contain error, got: {stderr!r}"

        return True, ""


def test_command_end_failure_with_class():
    """command-end with --outcome failure and valid --failure-class succeeds."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "command-end",
                "--command-id", "c123",
                "--command", "test",
                "--outcome", "failure",
                "--failure-class", "timeout",
            ],
        )
        if code != 0:
            return False, f"exit code {code}, stderr: {stderr}"

        # Check event
        with open(log_path) as f:
            event = json.loads(f.readline())

        outcome = event.get("outcome")
        if outcome != {"status": "failure", "class": "timeout"}:
            return False, f"wrong outcome: {outcome}"

        return True, ""


def test_stage_begin_prints_id():
    """stage-begin prints bare stage_id to stdout."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "stage-begin",
                "--command-id", "c123",
                "--stage", "build",
            ],
            env={**__import__("os").environ, "CLAUDE_CODE_SESSION_ID": "s123"},
        )
        if code != 0:
            return False, f"exit code {code}"

        stage_id = stdout.strip()
        if not stage_id or len(stage_id) != 32:
            return False, f"stdout should be hex uuid, got: {stdout!r}"

        return True, ""


def test_diagnose_empty_log():
    """diagnose against empty/nonexistent log exits 0 and doesn't crash."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"  # doesn't exist
        code, stdout, stderr = run_script(
            ["--log", str(log_path), "diagnose"],
        )
        if code != 0:
            return False, f"exit code {code} on empty log (should be 0)"
        if "Match rate" not in stdout:
            return False, f"stdout should contain 'Match rate', got: {stdout!r}"

        return True, ""


def test_diagnose_incomplete_pairs():
    """diagnose with unmatched begin/end pairs exits 1."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"

        # Write a command.begin without matching end
        events = [
            {
                "schema_version": 1,
                "event_type": "command.begin",
                "timestamp": "2026-08-26T12:00:00Z",
                "session_id": "s1",
                "command_id": "c1",
                "command": "test",
                "turns": "unknown",
                "elapsed_seconds": "unknown",
                "retries": "unknown",
                "peak_concurrency": "unknown",
                "transcript_size": "unknown",
                "output_artifact_size": "unknown",
            },
            {
                "schema_version": 1,
                "event_type": "command.begin",
                "timestamp": "2026-08-26T12:00:10Z",
                "session_id": "s1",
                "command_id": "c2",
                "command": "test",
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
                "timestamp": "2026-08-26T12:00:20Z",
                "session_id": "s1",
                "command_id": "c2",
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
            ["--log", str(log_path), "diagnose"],
        )

        if code == 0:
            return False, "exit code should be 1 for incomplete pairs"
        if "Match rate" not in stdout or "FAIL" not in stdout:
            return False, f"stdout should show FAIL, got: {stdout!r}"

        return True, ""


def test_stage_end_requires_failure_class():
    """stage-end with --outcome failure but no --failure-class exits non-zero."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "stage-end",
                "--stage-id", "st1",
                "--command-id", "c1",
                "--stage", "build",
                "--outcome", "failure",
            ],
        )
        if code == 0:
            return False, "should have exited non-zero"

        return True, ""


if __name__ == "__main__":
    h = Harness("RUN_METRICS TEST SUITE")

    test_result = h.test_result

    print("[Section 1] session-begin")
    passed, msg = test_session_begin()
    test_result("session-begin writes event", passed, msg)

    passed, msg = test_session_begin_bad_json()
    test_result("session-begin rejects bad JSON", passed, msg)

    print()

    print("[Section 2] agent-end privacy")
    passed, msg = test_agent_end_redacts_message()
    test_result("agent-end redacts last_assistant_message", passed, msg)

    print()

    print("[Section 3] command-begin/end")
    passed, msg = test_command_begin_prints_id()
    test_result("command-begin prints command_id", passed, msg)

    passed, msg = test_command_end_failure_requires_class()
    test_result("command-end requires failure-class for failure", passed, msg)

    passed, msg = test_command_end_failure_with_class()
    test_result("command-end accepts valid failure-class", passed, msg)

    print()

    print("[Section 4] stage-begin/end")
    passed, msg = test_stage_begin_prints_id()
    test_result("stage-begin prints stage_id", passed, msg)

    passed, msg = test_stage_end_requires_failure_class()
    test_result("stage-end requires failure-class for failure", passed, msg)

    print()

    print("[Section 5] diagnose")
    passed, msg = test_diagnose_empty_log()
    test_result("diagnose on empty log exits 0", passed, msg)

    passed, msg = test_diagnose_incomplete_pairs()
    test_result("diagnose detects incomplete pairs", passed, msg)

    print()

    h.summarize_and_exit()
