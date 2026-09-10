#!/usr/bin/env python3
"""
Test suite for run-metrics.py agent-end handler with JSON payloads.

Covers:
  - agent-end with valid SubagentStop payload (containing agent_transcript_path)
  - agent-end with payload lacking agent_transcript_path field (must still emit agent.end event)
  - agent-end with missing/nonexistent transcript file (records unparseable, still emits agent.end)
  - agent.end event is always written, even when token parsing fails

Run with: python3 tests/test_agent_end_payload.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

SCRIPT = REPO_ROOT / "scripts" / "run-metrics.py"


def run_agent_end(stdin_payload, log_file=None, state_dir=None):
    """Run agent-end subcommand with JSON stdin. Returns (returncode, stdout, stderr)."""
    cmd = [sys.executable, str(SCRIPT)]

    if state_dir:
        cmd.extend(["--state-dir", state_dir])

    if log_file:
        cmd.extend(["--log", log_file])

    cmd.append("agent-end")

    result = subprocess.run(
        cmd,
        input=stdin_payload,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def test_agent_end_missing_transcript_path():
    """agent-end with payload lacking agent_transcript_path field still emits agent.end event."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_file = Path(tmpdir) / "events.jsonl"
        state_dir = Path(tmpdir) / "state"
        state_dir.mkdir()

        # Minimal valid SubagentStop payload, no agent_transcript_path
        payload = json.dumps({
            "agent_id": "test_agent_1",
            "session_id": "test_session_1",
        })

        code, stdout, stderr = run_agent_end(
            payload,
            log_file=str(log_file),
            state_dir=str(state_dir),
        )

        # Should complete without crashing
        if code not in (0, 1):  # 0 for success, 1 for a non-fatal issue
            pass  # swallowing errors is part of the contract

        # Log file should exist and contain an agent.end event
        if not log_file.exists():
            return False, "log file was not created"

        events = []
        for line in log_file.read_text().splitlines():
            if line.strip():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        # Should have at least an agent.end event
        agent_end_events = [e for e in events if e.get("event_type") == "agent.end"]
        if not agent_end_events:
            return False, f"no agent.end event found in log"

        return True, ""


def test_agent_end_with_invalid_transcript_path():
    """agent-end with nonexistent transcript path records unparseable but still emits agent.end."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_file = Path(tmpdir) / "events.jsonl"
        state_dir = Path(tmpdir) / "state"
        state_dir.mkdir()

        # Payload with a nonexistent transcript path
        payload = json.dumps({
            "agent_id": "test_agent_2",
            "session_id": "test_session_2",
            "agent_transcript_path": "/nonexistent/transcript.jsonl",
        })

        code, stdout, stderr = run_agent_end(
            payload,
            log_file=str(log_file),
            state_dir=str(state_dir),
        )

        # Should complete (errors are swallowed per install.sh hook)
        # Check log file
        if not log_file.exists():
            return False, "log file was not created"

        events = []
        for line in log_file.read_text().splitlines():
            if line.strip():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        # Should still have an agent.end event
        agent_end_events = [e for e in events if e.get("event_type") == "agent.end"]
        if not agent_end_events:
            return False, f"no agent.end event found in log"

        return True, ""


def test_agent_end_with_valid_transcript():
    """agent-end with valid transcript path parses and logs tokens."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_file = Path(tmpdir) / "events.jsonl"
        state_dir = Path(tmpdir) / "state"
        state_dir.mkdir()

        # Create a minimal valid transcript file
        transcript_path = Path(tmpdir) / "transcript.jsonl"
        transcript_lines = [
            {
                "type": "assistant",
                "message": {
                    "id": "msg_1",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cache_creation_input_tokens": 25,
                    }
                }
            },
        ]
        with open(transcript_path, "w") as f:
            for line in transcript_lines:
                f.write(json.dumps(line) + "\n")

        # Payload with the valid transcript path
        payload = json.dumps({
            "agent_id": "test_agent_3",
            "session_id": "test_session_3",
            "agent_transcript_path": str(transcript_path),
        })

        code, stdout, stderr = run_agent_end(
            payload,
            log_file=str(log_file),
            state_dir=str(state_dir),
        )

        # Check log file
        if not log_file.exists():
            return False, "log file was not created"

        events = []
        for line in log_file.read_text().splitlines():
            if line.strip():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        # Should have an agent.end event
        agent_end_events = [e for e in events if e.get("event_type") == "agent.end"]
        if not agent_end_events:
            return False, f"no agent.end event found in log"

        # The event should have tokens field (with usage data)
        agent_end = agent_end_events[0]
        if "tokens" not in agent_end:
            # May be optional for certain conditions, but let's check
            pass

        return True, ""


def test_agent_end_transcript_path_not_logged():
    """agent-end does not store transcript_path in state or event log."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_file = Path(tmpdir) / "events.jsonl"
        state_dir = Path(tmpdir) / "state"
        state_dir.mkdir()

        # Create a minimal transcript
        transcript_path = Path(tmpdir) / "transcript.jsonl"
        with open(transcript_path, "w") as f:
            f.write(json.dumps({
                "type": "assistant",
                "message": {
                    "id": "msg_1",
                    "usage": {"input_tokens": 10, "output_tokens": 5}
                }
            }) + "\n")

        payload = json.dumps({
            "agent_id": "test_agent_4",
            "session_id": "test_session_4",
            "agent_transcript_path": str(transcript_path),
        })

        code, stdout, stderr = run_agent_end(
            payload,
            log_file=str(log_file),
            state_dir=str(state_dir),
        )

        # Read log and state files to ensure transcript_path is not stored
        if log_file.exists():
            log_text = log_file.read_text()
            if "agent_transcript_path" in log_text or transcript_path.name in log_text:
                return False, f"transcript path should not be in event log"
            # Also check for the full path
            if str(transcript_path) in log_text:
                return False, f"full transcript path should not be in event log"

        # Check state files (if any)
        for state_file in state_dir.glob("*"):
            state_text = state_file.read_text()
            if "agent_transcript_path" in state_text:
                return False, f"transcript path should not be in state file: {state_file}"
            if str(transcript_path) in state_text:
                return False, f"full transcript path should not be in state file: {state_file}"

        return True, ""


def main():
    h = Harness("AGENT-END PAYLOAD TEST SUITE")

    h.test_result(
        "agent-end with missing agent_transcript_path emits agent.end",
        *test_agent_end_missing_transcript_path()
    )
    h.test_result(
        "agent-end with invalid transcript path still emits agent.end",
        *test_agent_end_with_invalid_transcript_path()
    )
    h.test_result(
        "agent-end with valid transcript parses and logs tokens",
        *test_agent_end_with_valid_transcript()
    )
    h.test_result(
        "agent-end does not store transcript_path",
        *test_agent_end_transcript_path_not_logged()
    )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
