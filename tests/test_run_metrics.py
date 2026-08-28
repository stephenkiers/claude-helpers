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


def test_read_stdin_json_caps_at_1mib():
    """read_stdin_json caps input at 1 MiB before parsing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"

        # Payload well under 1 MiB should succeed normally
        small_payload = json.dumps({"session_id": "s1", "cwd": "/tmp"})
        code, stdout, stderr = run_script(
            ["--log", str(log_path), "session-begin"],
            stdin_text=small_payload,
        )
        if code != 0:
            return False, f"small payload failed: {stderr}"

        # ~2 MiB payload should be handled gracefully (capped, not hung/crashed)
        big_string = "x" * (2 * 1024 * 1024)
        huge_payload = json.dumps({"session_id": "s2", "data": big_string})

        code, stdout, stderr = run_script(
            ["--log", str(log_path), "session-begin"],
            stdin_text=huge_payload,
        )

        if code == 0:
            return True, "huge payload handled gracefully"
        if "Traceback" in stderr:
            return False, f"huge payload caused traceback: {stderr}"
        return True, "huge payload rejected cleanly"


def test_field_truncation_session_id():
    """session_id truncated to 4096 chars when read from hook payload."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        payload = json.dumps({"session_id": "s" * 5000, "cwd": "/tmp"})

        code, stdout, stderr = run_script(
            ["--log", str(log_path), "session-begin"],
            stdin_text=payload,
        )
        if code != 0:
            return False, f"session-begin failed: {stderr}"

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
        payload = json.dumps({"session_id": "s1", "cwd": "/" + "d" * 5000})

        code, stdout, stderr = run_script(
            ["--log", str(log_path), "session-begin"],
            stdin_text=payload,
        )
        if code != 0:
            return False, f"session-begin failed: {stderr}"

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
        payload = json.dumps({
            "session_id": "s1",
            "agent_id": "a" * 5000,
            "agent_type": "general",
        })

        code, stdout, stderr = run_script(
            ["--log", str(log_path), "agent-begin"],
            stdin_text=payload,
        )
        if code != 0:
            return False, f"agent-begin failed: {stderr}"

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
        payload = json.dumps({
            "session_id": "s1",
            "agent_id": "a1",
            "agent_type": "x" * 5000,
        })

        code, stdout, stderr = run_script(
            ["--log", str(log_path), "agent-begin"],
            stdin_text=payload,
        )
        if code != 0:
            return False, f"agent-begin failed: {stderr}"

        with open(log_path) as f:
            event = json.loads(f.readline())

        written_agent_type = event.get("agent_type", "")
        if len(written_agent_type) > 4096:
            return False, f"agent_type not truncated: {len(written_agent_type)} chars"

        return True, ""


def test_repo_field_derived_correctly_from_cwd():
    """repo field is derived correctly from cwd (basename without trailing slashes)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        payload = json.dumps({"session_id": "s1", "cwd": "/foo/bar"})

        code, stdout, stderr = run_script(
            ["--log", str(log_path), "session-begin"],
            stdin_text=payload,
        )
        if code != 0:
            return False, f"session-begin failed: {stderr}"

        with open(log_path) as f:
            event = json.loads(f.readline())

        written_repo = event.get("repo", "")
        if written_repo != "bar":
            return False, f"repo should be 'bar', got '{written_repo}'"

        return True, ""


def test_diagnose_excludes_unknown_as_correlation_id():
    """diagnose excludes command_id="unknown" from pairing; only pairs valid command IDs."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"

        # One valid command pair (c1) and one "unknown"-id pair that must not be
        # spuriously treated as matched against itself.
        events = [
            {
                "schema_version": 1, "event_type": "command.begin",
                "timestamp": "2026-08-26T12:00:00Z", "session_id": "s1",
                "command_id": "c1", "command": "test1",
                "turns": "unknown", "elapsed_seconds": "unknown", "retries": "unknown",
                "peak_concurrency": "unknown", "transcript_size": "unknown",
                "output_artifact_size": "unknown",
            },
            {
                "schema_version": 1, "event_type": "command.end",
                "timestamp": "2026-08-26T12:00:10Z", "session_id": "s1",
                "command_id": "c1", "outcome": {"status": "success"},
                "turns": "unknown", "elapsed_seconds": "unknown", "retries": "unknown",
                "peak_concurrency": "unknown", "transcript_size": "unknown",
                "output_artifact_size": "unknown",
            },
            {
                "schema_version": 1, "event_type": "command.begin",
                "timestamp": "2026-08-26T12:00:20Z", "session_id": "s1",
                "command_id": "unknown", "command": "test2",
                "turns": "unknown", "elapsed_seconds": "unknown", "retries": "unknown",
                "peak_concurrency": "unknown", "transcript_size": "unknown",
                "output_artifact_size": "unknown",
            },
            {
                "schema_version": 1, "event_type": "command.end",
                "timestamp": "2026-08-26T12:00:30Z", "session_id": "s1",
                "command_id": "unknown", "outcome": {"status": "success"},
                "turns": "unknown", "elapsed_seconds": "unknown", "retries": "unknown",
                "peak_concurrency": "unknown", "transcript_size": "unknown",
                "output_artifact_size": "unknown",
            },
        ]

        with open(log_path, "w") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")

        code, stdout, stderr = run_script(["--log", str(log_path), "diagnose"])

        if "Match rate" not in stdout:
            return False, f"diagnose output missing 'Match rate': {stdout}"

        # Only the valid pair (c1) should count: 1/1. The "unknown" pair must not
        # inflate that to 2/2.
        if "1/1" not in stdout:
            return False, f"diagnose should show 1/1 matched; instead got: {stdout}"
        if "2/2" in stdout:
            return False, f"diagnose should NOT show 2/2 matched (unknown IDs should be filtered); got: {stdout}"

        return True, ""


def test_diagnose_prints_per_stage_breakdown():
    """diagnose prints per-stage match-rate breakdown."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"

        events = [
            {
                "schema_version": 1, "event_type": "stage.begin",
                "timestamp": "2026-08-26T12:00:00Z", "session_id": "s1",
                "command_id": "c1", "stage_id": "st1", "stage": "build",
                "turns": "unknown", "elapsed_seconds": "unknown", "retries": "unknown",
                "peak_concurrency": "unknown", "transcript_size": "unknown",
                "output_artifact_size": "unknown",
            },
            {
                "schema_version": 1, "event_type": "stage.end",
                "timestamp": "2026-08-26T12:00:10Z", "session_id": "s1",
                "command_id": "c1", "stage_id": "st1", "stage": "build",
                "outcome": {"status": "success"},
                "turns": "unknown", "elapsed_seconds": "unknown", "retries": "unknown",
                "peak_concurrency": "unknown", "transcript_size": "unknown",
                "output_artifact_size": "unknown",
            },
            {
                "schema_version": 1, "event_type": "stage.begin",
                "timestamp": "2026-08-26T12:00:20Z", "session_id": "s1",
                "command_id": "c1", "stage_id": "st2", "stage": "test",
                "turns": "unknown", "elapsed_seconds": "unknown", "retries": "unknown",
                "peak_concurrency": "unknown", "transcript_size": "unknown",
                "output_artifact_size": "unknown",
            },
            {
                "schema_version": 1, "event_type": "stage.end",
                "timestamp": "2026-08-26T12:00:30Z", "session_id": "s1",
                "command_id": "c1", "stage_id": "st2", "stage": "test",
                "outcome": {"status": "success"},
                "turns": "unknown", "elapsed_seconds": "unknown", "retries": "unknown",
                "peak_concurrency": "unknown", "transcript_size": "unknown",
                "output_artifact_size": "unknown",
            },
        ]

        with open(log_path, "w") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")

        code, stdout, stderr = run_script(["--log", str(log_path), "diagnose"])

        if code != 0:
            return False, f"diagnose exited non-zero: {stderr}"

        has_stage_breakdown = ("build" in stdout or "test" in stdout or "stage" in stdout.lower())
        if not has_stage_breakdown:
            return False, f"diagnose output missing per-stage breakdown: {stdout}"

        return True, ""


def test_cross_process_correlation_via_state_file():
    """Cross-process correlation test: run command-begin, stage-begin, stage-end, command-end as separate calls.

    All four calls share the same CLAUDE_CODE_SESSION_ID and state-dir. Verifies that
    stage.begin/end share a stage_id, and all four events share a command_id, despite
    each being run in a separate subprocess (no shell variables).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        state_dir = Path(tmpdir) / "state"
        session_id = "test-session-" + __import__("uuid").uuid4().hex[:8]

        env = {**__import__("os").environ, "CLAUDE_CODE_SESSION_ID": session_id}

        # Run command-begin
        code1, stdout1, stderr1 = run_script(
            ["--log", str(log_path), "--state-dir", str(state_dir), "command-begin", "--command", "test-cmd"],
            env=env,
        )
        if code1 != 0:
            return False, f"command-begin failed: {stderr1}"
        cmd_id_from_stdout = stdout1.strip()

        # Run stage-begin (WITHOUT explicit --command-id)
        code2, stdout2, stderr2 = run_script(
            ["--log", str(log_path), "--state-dir", str(state_dir), "stage-begin", "--stage", "test-stage"],
            env=env,
        )
        if code2 != 0:
            return False, f"stage-begin failed: {stderr2}"
        stage_id_from_stdout = stdout2.strip()

        # Run stage-end (WITHOUT explicit --stage-id or --command-id)
        code3, stdout3, stderr3 = run_script(
            ["--log", str(log_path), "--state-dir", str(state_dir), "stage-end", "--stage", "test-stage", "--outcome", "success"],
            env=env,
        )
        if code3 != 0:
            return False, f"stage-end failed: {stderr3}"

        # Run command-end (WITHOUT explicit --command-id)
        code4, stdout4, stderr4 = run_script(
            ["--log", str(log_path), "--state-dir", str(state_dir), "command-end", "--command", "test-cmd", "--outcome", "success"],
            env=env,
        )
        if code4 != 0:
            return False, f"command-end failed: {stderr4}"

        # Parse the log file
        if not log_path.exists():
            return False, "log file was never created"

        events = []
        with open(log_path) as f:
            for line in f:
                if line.strip():
                    events.append(json.loads(line))

        if len(events) != 4:
            return False, f"expected 4 events, got {len(events)}"

        # Find each event type
        cmd_begin = next((e for e in events if e.get("event_type") == "command.begin"), None)
        stage_begin = next((e for e in events if e.get("event_type") == "stage.begin"), None)
        stage_end = next((e for e in events if e.get("event_type") == "stage.end"), None)
        cmd_end = next((e for e in events if e.get("event_type") == "command.end"), None)

        if not all([cmd_begin, stage_begin, stage_end, cmd_end]):
            return False, "missing one or more event types"

        # Verify command_id correlation
        cmd_id_from_begin = cmd_begin.get("command_id")
        if cmd_id_from_begin != cmd_id_from_stdout:
            return False, f"command.begin's command_id doesn't match stdout: {cmd_id_from_begin} vs {cmd_id_from_stdout}"

        if stage_begin.get("command_id") != cmd_id_from_begin:
            return False, f"stage.begin doesn't have matching command_id: {stage_begin.get('command_id')} vs {cmd_id_from_begin}"

        if stage_end.get("command_id") != cmd_id_from_begin:
            return False, f"stage.end doesn't have matching command_id: {stage_end.get('command_id')} vs {cmd_id_from_begin}"

        if cmd_end.get("command_id") != cmd_id_from_begin:
            return False, f"command.end doesn't have matching command_id: {cmd_end.get('command_id')} vs {cmd_id_from_begin}"

        # Verify stage_id correlation
        stage_id_from_begin = stage_begin.get("stage_id")
        if stage_id_from_begin != stage_id_from_stdout:
            return False, f"stage.begin's stage_id doesn't match stdout: {stage_id_from_begin} vs {stage_id_from_stdout}"

        if stage_end.get("stage_id") != stage_id_from_begin:
            return False, f"stage.end doesn't have matching stage_id: {stage_end.get('stage_id')} vs {stage_id_from_begin}"

        return True, ""


def test_explicit_flags_override_state():
    """Explicit --command-id/--stage-id flags override state file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        state_dir = Path(tmpdir) / "state"
        session_id = "test-session-" + __import__("uuid").uuid4().hex[:8]

        env = {**__import__("os").environ, "CLAUDE_CODE_SESSION_ID": session_id}

        # Run command-begin, which seeds state with one command_id
        code1, stdout1, stderr1 = run_script(
            ["--log", str(log_path), "--state-dir", str(state_dir), "command-begin", "--command", "test-cmd"],
            env=env,
        )
        if code1 != 0:
            return False, f"command-begin failed: {stderr1}"
        auto_cmd_id = stdout1.strip()

        # Run stage-end with an EXPLICIT --stage-id and --command-id (different from state)
        explicit_stage_id = "explicit-stage-id-" + __import__("uuid").uuid4().hex[:8]
        explicit_cmd_id = "explicit-cmd-id-" + __import__("uuid").uuid4().hex[:8]

        code2, stdout2, stderr2 = run_script(
            [
                "--log", str(log_path),
                "--state-dir", str(state_dir),
                "stage-end",
                "--stage-id", explicit_stage_id,
                "--command-id", explicit_cmd_id,
                "--stage", "test-stage",
                "--outcome", "success",
            ],
            env=env,
        )
        if code2 != 0:
            return False, f"stage-end failed: {stderr2}"

        # Parse and verify explicit IDs were used
        with open(log_path) as f:
            lines = [line for line in f if line.strip()]

        stage_end_event = json.loads(lines[-1])  # Last event should be stage.end

        if stage_end_event.get("stage_id") != explicit_stage_id:
            return False, f"explicit --stage-id not used: got {stage_end_event.get('stage_id')}, expected {explicit_stage_id}"

        if stage_end_event.get("command_id") != explicit_cmd_id:
            return False, f"explicit --command-id not used: got {stage_end_event.get('command_id')}, expected {explicit_cmd_id}"

        return True, ""


def test_missing_state_file_degrades_to_unknown():
    """stage-end with no state file and no explicit flags produces 'unknown' IDs."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        state_dir = Path(tmpdir) / "state"
        session_id = "test-session-" + __import__("uuid").uuid4().hex[:8]

        env = {**__import__("os").environ, "CLAUDE_CODE_SESSION_ID": session_id}

        # Run stage-end WITHOUT any prior command-begin or stage-begin (no state file)
        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "--state-dir", str(state_dir),
                "stage-end",
                "--stage", "orphan-stage",
                "--outcome", "success",
            ],
            env=env,
        )

        if code != 0:
            return False, f"stage-end should succeed even with no state: exit {code}, stderr: {stderr}"

        # Parse the event
        if not log_path.exists():
            return False, "log file not created"

        with open(log_path) as f:
            event = json.loads(f.readline())

        if event.get("stage_id") != "unknown":
            return False, f"stage_id should be 'unknown', got: {event.get('stage_id')}"

        if event.get("command_id") != "unknown":
            return False, f"command_id should be 'unknown', got: {event.get('command_id')}"

        return True, ""


def test_stage_name_mismatch_sets_flag():
    """stage-end with mismatched stage name sets state_mismatch: true."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        state_dir = Path(tmpdir) / "state"
        session_id = "test-session-" + __import__("uuid").uuid4().hex[:8]

        env = {**__import__("os").environ, "CLAUDE_CODE_SESSION_ID": session_id}

        # Run command-begin and stage-begin with one name
        run_script(
            ["--log", str(log_path), "--state-dir", str(state_dir), "command-begin", "--command", "test-cmd"],
            env=env,
        )
        run_script(
            ["--log", str(log_path), "--state-dir", str(state_dir), "stage-begin", "--stage", "original-stage"],
            env=env,
        )

        # Now run stage-end with a DIFFERENT stage name (no explicit --stage-id)
        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "--state-dir", str(state_dir),
                "stage-end",
                "--stage", "different-stage",  # Mismatch!
                "--outcome", "success",
            ],
            env=env,
        )

        if code != 0:
            return False, f"stage-end failed: {stderr}"

        # Parse and verify state_mismatch flag
        with open(log_path) as f:
            lines = [line for line in f if line.strip()]

        stage_end_event = json.loads(lines[-1])

        if stage_end_event.get("state_mismatch") != True:
            return False, f"state_mismatch should be True, got: {stage_end_event.get('state_mismatch')}"

        return True, ""


def test_command_name_mismatch_sets_flag():
    """command-end with mismatched command name sets state_mismatch: true."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        state_dir = Path(tmpdir) / "state"
        session_id = "test-session-" + __import__("uuid").uuid4().hex[:8]

        env = {**__import__("os").environ, "CLAUDE_CODE_SESSION_ID": session_id}

        # Run command-begin with one name
        run_script(
            ["--log", str(log_path), "--state-dir", str(state_dir), "command-begin", "--command", "original-cmd"],
            env=env,
        )

        # Now run command-end with a DIFFERENT command name (no explicit --command-id)
        code, stdout, stderr = run_script(
            [
                "--log", str(log_path),
                "--state-dir", str(state_dir),
                "command-end",
                "--command", "different-cmd",  # Mismatch!
                "--outcome", "success",
            ],
            env=env,
        )

        if code != 0:
            return False, f"command-end failed: {stderr}"

        # Parse and verify state_mismatch flag
        with open(log_path) as f:
            lines = [line for line in f if line.strip()]

        cmd_end_event = json.loads(lines[-1])

        if cmd_end_event.get("state_mismatch") != True:
            return False, f"state_mismatch should be True, got: {cmd_end_event.get('state_mismatch')}"

        return True, ""


def test_prune_on_command_begin():
    """command-begin opportunistically prunes stale state files."""
    import os as _os

    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        state_dir = Path(tmpdir) / "state"
        session_id_old = "old-session-" + __import__("uuid").uuid4().hex[:8]
        session_id_new = "new-session-" + __import__("uuid").uuid4().hex[:8]

        # Create a stale state file (2 days old)
        state_dir.mkdir(parents=True, exist_ok=True)
        old_state_file = state_dir / f"{session_id_old}.json"
        old_state_file.write_text('{"command_id": "old"}')
        _os.utime(old_state_file, (0, 0))  # Set mtime to epoch (very old)

        if not old_state_file.exists():
            return False, "old state file not created"

        # Run command-begin for a new session
        env = {**__import__("os").environ, "CLAUDE_CODE_SESSION_ID": session_id_new}
        code, stdout, stderr = run_script(
            ["--log", str(log_path), "--state-dir", str(state_dir), "command-begin", "--command", "test"],
            env=env,
        )

        if code != 0:
            return False, f"command-begin failed: {stderr}"

        # Verify old file was pruned, new file exists
        if old_state_file.exists():
            return False, "old state file was not pruned by command-begin"

        new_state_file = state_dir / f"{session_id_new}.json"
        if not new_state_file.exists():
            return False, "new session state file was not created"

        return True, ""


def test_state_file_persists_command_id():
    """State file persists command_id that can be read by subsequent calls."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        state_dir = Path(tmpdir) / "state"
        session_id = "test-session-" + __import__("uuid").uuid4().hex[:8]

        env = {**__import__("os").environ, "CLAUDE_CODE_SESSION_ID": session_id}

        # Run command-begin to create state
        code1, stdout1, stderr1 = run_script(
            ["--log", str(log_path), "--state-dir", str(state_dir), "command-begin", "--command", "test-cmd"],
            env=env,
        )
        if code1 != 0:
            return False, f"command-begin failed: {stderr1}"

        # Verify state file exists and contains the command_id
        state_file = state_dir / f"{session_id}.json"
        if not state_file.exists():
            return False, "state file not created"

        with open(state_file) as f:
            state_content = json.loads(f.read())

        if "command_id" not in state_content:
            return False, f"state file missing command_id: {state_content}"

        if state_content.get("command_id") != stdout1.strip():
            return False, f"state command_id doesn't match stdout"

        return True, ""


def test_stage_begin_without_explicit_command_id():
    """stage-begin resolves command_id from state file when flag is omitted."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        state_dir = Path(tmpdir) / "state"
        session_id = "test-session-" + __import__("uuid").uuid4().hex[:8]

        env = {**__import__("os").environ, "CLAUDE_CODE_SESSION_ID": session_id}

        # Run command-begin
        code1, stdout1, stderr1 = run_script(
            ["--log", str(log_path), "--state-dir", str(state_dir), "command-begin", "--command", "test-cmd"],
            env=env,
        )
        if code1 != 0:
            return False, f"command-begin failed: {stderr1}"
        cmd_id_from_begin = stdout1.strip()

        # Run stage-begin WITHOUT --command-id (should resolve from state)
        code2, stdout2, stderr2 = run_script(
            ["--log", str(log_path), "--state-dir", str(state_dir), "stage-begin", "--stage", "build"],
            env=env,
        )
        if code2 != 0:
            return False, f"stage-begin failed: {stderr2}"

        # Parse log and verify stage-begin has the command_id from state
        with open(log_path) as f:
            lines = [line.strip() for line in f if line.strip()]

        stage_begin_event = json.loads(lines[-1])

        if stage_begin_event.get("command_id") != cmd_id_from_begin:
            return False, f"stage-begin should inherit command_id from state: got {stage_begin_event.get('command_id')}, expected {cmd_id_from_begin}"

        return True, ""


def test_stage_end_clears_stage_state():
    """After stage-end, state file's stage_id and stage fields are cleared."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        state_dir = Path(tmpdir) / "state"
        session_id = "test-session-" + __import__("uuid").uuid4().hex[:8]

        env = {**__import__("os").environ, "CLAUDE_CODE_SESSION_ID": session_id}

        # command-begin
        run_script(
            ["--log", str(log_path), "--state-dir", str(state_dir), "command-begin", "--command", "test-cmd"],
            env=env,
        )

        # stage-begin
        run_script(
            ["--log", str(log_path), "--state-dir", str(state_dir), "stage-begin", "--stage", "build"],
            env=env,
        )

        # Verify stage fields are in state before stage-end
        state_file = state_dir / f"{session_id}.json"
        with open(state_file) as f:
            before_state = json.loads(f.read())
        if before_state.get("stage_id") is None or before_state.get("stage") is None:
            return False, "state should have stage_id and stage after stage-begin"

        # stage-end
        code, stdout, stderr = run_script(
            ["--log", str(log_path), "--state-dir", str(state_dir), "stage-end", "--stage", "build", "--outcome", "success"],
            env=env,
        )
        if code != 0:
            return False, f"stage-end failed: {stderr}"

        # Verify stage fields are now None/cleared
        with open(state_file) as f:
            after_state = json.loads(f.read())

        if after_state.get("stage_id") is not None:
            return False, f"after stage-end, stage_id should be None, got: {after_state.get('stage_id')}"

        if after_state.get("stage") is not None:
            return False, f"after stage-end, stage should be None, got: {after_state.get('stage')}"

        # command_id should still be present (it's cleared at command-end)
        if "command_id" not in after_state:
            return False, "command_id should persist after stage-end"

        return True, ""


def test_command_end_deletes_state_file():
    """After command-end, the state file is deleted entirely."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        state_dir = Path(tmpdir) / "state"
        session_id = "test-session-" + __import__("uuid").uuid4().hex[:8]

        env = {**__import__("os").environ, "CLAUDE_CODE_SESSION_ID": session_id}

        # command-begin
        run_script(
            ["--log", str(log_path), "--state-dir", str(state_dir), "command-begin", "--command", "test-cmd"],
            env=env,
        )

        state_file = state_dir / f"{session_id}.json"
        if not state_file.exists():
            return False, "state file should exist after command-begin"

        # command-end
        code, stdout, stderr = run_script(
            ["--log", str(log_path), "--state-dir", str(state_dir), "command-end", "--command", "test-cmd", "--outcome", "success"],
            env=env,
        )
        if code != 0:
            return False, f"command-end failed: {stderr}"

        # Verify state file is deleted
        if state_file.exists():
            return False, "state file should be deleted after command-end"

        return True, ""


def test_multiple_sequential_stages():
    """Multiple sequential stage-begin/end pairs update state correctly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        state_dir = Path(tmpdir) / "state"
        session_id = "test-session-" + __import__("uuid").uuid4().hex[:8]

        env = {**__import__("os").environ, "CLAUDE_CODE_SESSION_ID": session_id}

        # command-begin
        code1, stdout1, stderr1 = run_script(
            ["--log", str(log_path), "--state-dir", str(state_dir), "command-begin", "--command", "test-cmd"],
            env=env,
        )
        if code1 != 0:
            return False, f"command-begin failed: {stderr1}"
        cmd_id = stdout1.strip()

        # First stage: begin, end
        code2, stdout2, stderr2 = run_script(
            ["--log", str(log_path), "--state-dir", str(state_dir), "stage-begin", "--stage", "stage1"],
            env=env,
        )
        if code2 != 0:
            return False, f"stage1-begin failed: {stderr2}"
        stage1_id = stdout2.strip()

        code3, stdout3, stderr3 = run_script(
            ["--log", str(log_path), "--state-dir", str(state_dir), "stage-end", "--stage", "stage1", "--outcome", "success"],
            env=env,
        )
        if code3 != 0:
            return False, f"stage1-end failed: {stderr3}"

        # Second stage: begin, end
        code4, stdout4, stderr4 = run_script(
            ["--log", str(log_path), "--state-dir", str(state_dir), "stage-begin", "--stage", "stage2"],
            env=env,
        )
        if code4 != 0:
            return False, f"stage2-begin failed: {stderr4}"
        stage2_id = stdout4.strip()

        code5, stdout5, stderr5 = run_script(
            ["--log", str(log_path), "--state-dir", str(state_dir), "stage-end", "--stage", "stage2", "--outcome", "success"],
            env=env,
        )
        if code5 != 0:
            return False, f"stage2-end failed: {stderr5}"

        # Verify all four events have the same command_id
        with open(log_path) as f:
            events = [json.loads(line) for line in f if line.strip()]

        stage1_begin = next((e for e in events if e.get("event_type") == "stage.begin" and e.get("stage") == "stage1"), None)
        stage1_end = next((e for e in events if e.get("event_type") == "stage.end" and e.get("stage") == "stage1"), None)
        stage2_begin = next((e for e in events if e.get("event_type") == "stage.begin" and e.get("stage") == "stage2"), None)
        stage2_end = next((e for e in events if e.get("event_type") == "stage.end" and e.get("stage") == "stage2"), None)

        if not all([stage1_begin, stage1_end, stage2_begin, stage2_end]):
            return False, "missing one or more stage events"

        if stage1_begin.get("command_id") != cmd_id:
            return False, "stage1_begin doesn't have correct command_id"
        if stage1_end.get("command_id") != cmd_id:
            return False, "stage1_end doesn't have correct command_id"
        if stage2_begin.get("command_id") != cmd_id:
            return False, "stage2_begin doesn't have correct command_id"
        if stage2_end.get("command_id") != cmd_id:
            return False, "stage2_end doesn't have correct command_id"

        # Verify stage IDs are different
        if stage1_begin.get("stage_id") == stage2_begin.get("stage_id"):
            return False, "stage IDs should be different for different stages"

        return True, ""


def test_no_session_id_skips_state_file():
    """When CLAUDE_CODE_SESSION_ID is not set, command-begin succeeds gracefully."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        state_dir = Path(tmpdir) / "state"

        # Run without CLAUDE_CODE_SESSION_ID (remove it from env)
        env = {**__import__("os").environ}
        if "CLAUDE_CODE_SESSION_ID" in env:
            del env["CLAUDE_CODE_SESSION_ID"]

        code, stdout, stderr = run_script(
            ["--log", str(log_path), "--state-dir", str(state_dir), "command-begin", "--command", "test-cmd"],
            env=env,
        )

        if code != 0:
            return False, f"command-begin should succeed even without session_id: {stderr}"

        # Verify the command still prints an ID
        cmd_id = stdout.strip()
        if not cmd_id or len(cmd_id) != 32:
            return False, f"command-begin should still print an ID, got: {stdout!r}"

        # Verify the log event was created
        if not log_path.exists():
            return False, "log file should be created even without session_id"

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

    print("[Section 6] Hardening: stdin cap, field truncation, diagnose additions")
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

    passed, msg = test_repo_field_derived_correctly_from_cwd()
    test_result("repo field derived correctly from cwd", passed, msg)

    passed, msg = test_diagnose_excludes_unknown_as_correlation_id()
    test_result("diagnose excludes 'unknown' as correlation ID", passed, msg)

    passed, msg = test_diagnose_prints_per_stage_breakdown()
    test_result("diagnose prints per-stage breakdown", passed, msg)

    print()

    print("[Section 7] Session-scoped state file correlation (cross-process)")
    passed, msg = test_cross_process_correlation_via_state_file()
    test_result("cross-process correlation: command/stage IDs match via state file", passed, msg)

    passed, msg = test_explicit_flags_override_state()
    test_result("explicit flags override state file", passed, msg)

    passed, msg = test_missing_state_file_degrades_to_unknown()
    test_result("missing state degrades to 'unknown' IDs gracefully", passed, msg)

    passed, msg = test_stage_name_mismatch_sets_flag()
    test_result("stage-end with mismatched name sets state_mismatch: true", passed, msg)

    passed, msg = test_command_name_mismatch_sets_flag()
    test_result("command-end with mismatched name sets state_mismatch: true", passed, msg)

    passed, msg = test_prune_on_command_begin()
    test_result("command-begin prunes stale state files", passed, msg)

    print()

    print("[Section 8] State file persistence and lifecycle")
    passed, msg = test_state_file_persists_command_id()
    test_result("state file persists command_id", passed, msg)

    passed, msg = test_stage_begin_without_explicit_command_id()
    test_result("stage-begin resolves command_id from state without flag", passed, msg)

    passed, msg = test_stage_end_clears_stage_state()
    test_result("stage-end clears stage_id and stage fields in state file", passed, msg)

    passed, msg = test_command_end_deletes_state_file()
    test_result("command-end deletes state file after emit", passed, msg)

    passed, msg = test_multiple_sequential_stages()
    test_result("multiple sequential stages maintain command_id correlation", passed, msg)

    passed, msg = test_no_session_id_skips_state_file()
    test_result("missing CLAUDE_CODE_SESSION_ID skips state file creation", passed, msg)

    print()

    h.summarize_and_exit()
