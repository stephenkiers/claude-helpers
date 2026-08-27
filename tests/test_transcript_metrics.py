#!/usr/bin/env python3
"""
Test suite for claude-transcript-metrics.py.

Covers: parsing transcript JSONL, deduplication by message.id, cost-state extraction,
malformed line tolerance, and missing file handling.

Run with: python3 tests/test_transcript_metrics.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from _test_harness import REPO_ROOT, Harness

SCRIPT = REPO_ROOT / "scripts" / "claude-transcript-metrics.py"


def run_script(args, stdin_text=None):
    """Run the script as a subprocess. Returns (returncode, stdout, stderr)."""
    cmd = [sys.executable, str(SCRIPT)] + args
    result = subprocess.run(
        cmd,
        input=stdin_text,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def test_dedup_by_message_id():
    """Parser deduplicates by message.id (keeps last occurrence)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        transcript_path = Path(tmpdir) / "transcript.jsonl"

        # Write two lines with the same message.id (simulating streaming chunks)
        lines = [
            {
                "type": "assistant",
                "message": {
                    "id": "msg_123",
                    "usage": {"input_tokens": 100, "output_tokens": 10},
                }
            },
            {
                "type": "assistant",
                "message": {
                    "id": "msg_123",
                    "usage": {"input_tokens": 100, "output_tokens": 20},  # updated
                }
            },
        ]

        with open(transcript_path, "w") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")

        code, stdout, stderr = run_script(
            ["parse", "--transcript", str(transcript_path)]
        )

        if code != 0:
            return False, f"exit code {code}, stderr: {stderr}"

        result = json.loads(stdout)
        # Should have deduplicated to 1 turn (1 unique message.id)
        if result.get("turns") != 1:
            return False, f"expected 1 turn (deduplicated), got {result.get('turns')}"

        # Token totals should reflect only the last occurrence
        tokens = result.get("tokens", {})
        if tokens.get("output") != 20:
            return False, f"expected output_tokens=20 (last), got {tokens.get('output')}"

        return True, ""


def test_cost_state_included():
    """Parser includes cost-state in output if present."""
    with tempfile.TemporaryDirectory() as tmpdir:
        transcript_path = Path(tmpdir) / "transcript.jsonl"

        lines = [
            {
                "type": "assistant",
                "message": {
                    "id": "msg_1",
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                }
            },
            {
                "type": "cost-state",
                "sessionId": "sess_1",
                "totalCostUSD": 1.23,
                "modelUsage": {"claude-sonnet": {"inputTokens": 100}},
            },
        ]

        with open(transcript_path, "w") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")

        code, stdout, stderr = run_script(
            ["parse", "--transcript", str(transcript_path)]
        )

        if code != 0:
            return False, f"exit code {code}"

        result = json.loads(stdout)
        cost_state = result.get("cost_state")
        if not cost_state or cost_state.get("totalCostUSD") != 1.23:
            return False, f"cost_state not found or incomplete: {cost_state}"

        return True, ""


def test_malformed_line_tolerance():
    """Parser skips unparseable lines and reports count."""
    with tempfile.TemporaryDirectory() as tmpdir:
        transcript_path = Path(tmpdir) / "transcript.jsonl"

        lines = [
            '{"type":"assistant","message":{"id":"msg_1","usage":{"input_tokens":100}}}',
            'not valid json at all',
            '{"type":"assistant","message":{"id":"msg_2","usage":{"output_tokens":50}}}',
        ]

        with open(transcript_path, "w") as f:
            f.write("\n".join(lines) + "\n")

        code, stdout, stderr = run_script(
            ["parse", "--transcript", str(transcript_path)]
        )

        if code != 0:
            return False, f"exit code {code}"

        result = json.loads(stdout)
        if result.get("lines_skipped") != 1:
            return False, f"expected 1 skipped line, got {result.get('lines_skipped')}"

        if result.get("turns") != 2:
            return False, f"expected 2 turns from valid lines, got {result.get('turns')}"

        return True, ""


def test_nonexistent_file():
    """Parser exits non-zero if transcript file doesn't exist."""
    code, stdout, stderr = run_script(
        ["parse", "--transcript", "/nonexistent/path/transcript.jsonl"]
    )

    if code == 0:
        return False, "should have exited non-zero"

    if "Error" not in stderr or "not found" not in stderr.lower():
        return False, f"stderr should contain error, got: {stderr!r}"

    # Check no traceback
    if "Traceback" in stderr:
        return False, "stderr contains traceback"

    return True, ""


def test_session_id_from_first_event():
    """Parser extracts session_id from first event if not provided via --session-id."""
    with tempfile.TemporaryDirectory() as tmpdir:
        transcript_path = Path(tmpdir) / "transcript.jsonl"

        lines = [
            {
                "type": "assistant",
                "sessionId": "sess_abc123",
                "message": {
                    "id": "msg_1",
                    "usage": {"input_tokens": 100},
                }
            },
        ]

        with open(transcript_path, "w") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")

        code, stdout, stderr = run_script(
            ["parse", "--transcript", str(transcript_path)]
        )

        if code != 0:
            return False, f"exit code {code}"

        result = json.loads(stdout)
        if result.get("session_id") != "sess_abc123":
            return False, f"session_id not extracted, got {result.get('session_id')}"

        return True, ""


def test_session_id_override():
    """Parser uses --session-id arg when provided."""
    with tempfile.TemporaryDirectory() as tmpdir:
        transcript_path = Path(tmpdir) / "transcript.jsonl"

        lines = [
            {
                "type": "assistant",
                "sessionId": "sess_original",
                "message": {
                    "id": "msg_1",
                    "usage": {"input_tokens": 100},
                }
            },
        ]

        with open(transcript_path, "w") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")

        code, stdout, stderr = run_script(
            ["parse", "--transcript", str(transcript_path), "--session-id", "sess_override"]
        )

        if code != 0:
            return False, f"exit code {code}"

        result = json.loads(stdout)
        if result.get("session_id") != "sess_override":
            return False, f"--session-id not used, got {result.get('session_id')}"

        return True, ""


def test_agent_id_override():
    """Parser uses --agent-id arg when provided."""
    with tempfile.TemporaryDirectory() as tmpdir:
        transcript_path = Path(tmpdir) / "transcript.jsonl"

        lines = [
            {
                "type": "assistant",
                "message": {
                    "id": "msg_1",
                    "usage": {"input_tokens": 100},
                }
            },
        ]

        with open(transcript_path, "w") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")

        code, stdout, stderr = run_script(
            ["parse", "--transcript", str(transcript_path), "--agent-id", "agent_xyz"]
        )

        if code != 0:
            return False, f"exit code {code}"

        result = json.loads(stdout)
        if result.get("agent_id") != "agent_xyz":
            return False, f"--agent-id not used, got {result.get('agent_id')}"

        return True, ""


def test_token_confidence_low():
    """Parser sets token_confidence to 'low' by default."""
    with tempfile.TemporaryDirectory() as tmpdir:
        transcript_path = Path(tmpdir) / "transcript.jsonl"

        lines = [
            {
                "type": "assistant",
                "message": {
                    "id": "msg_1",
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                }
            },
        ]

        with open(transcript_path, "w") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")

        code, stdout, stderr = run_script(
            ["parse", "--transcript", str(transcript_path)]
        )

        if code != 0:
            return False, f"exit code {code}"

        result = json.loads(stdout)
        if result.get("token_confidence") != "low":
            return False, f"token_confidence should be 'low', got {result.get('token_confidence')}"

        return True, ""


def test_empty_transcript():
    """Parser handles empty transcript file gracefully."""
    with tempfile.TemporaryDirectory() as tmpdir:
        transcript_path = Path(tmpdir) / "transcript.jsonl"
        transcript_path.touch()  # create empty file

        code, stdout, stderr = run_script(
            ["parse", "--transcript", str(transcript_path)]
        )

        if code != 0:
            return False, f"exit code {code} on empty file"

        result = json.loads(stdout)
        if result.get("turns") != 0:
            return False, f"expected 0 turns, got {result.get('turns')}"

        return True, ""


if __name__ == "__main__":
    h = Harness("TRANSCRIPT_METRICS TEST SUITE")

    test_result = h.test_result

    print("[Section 1] Message deduplication")
    passed, msg = test_dedup_by_message_id()
    test_result("dedup by message.id (keep last)", passed, msg)

    print()

    print("[Section 2] Cost-state extraction")
    passed, msg = test_cost_state_included()
    test_result("cost_state included in output", passed, msg)

    print()

    print("[Section 3] Malformed line tolerance")
    passed, msg = test_malformed_line_tolerance()
    test_result("skips unparseable lines", passed, msg)

    print()

    print("[Section 4] File handling")
    passed, msg = test_nonexistent_file()
    test_result("nonexistent file exits non-zero", passed, msg)

    passed, msg = test_empty_transcript()
    test_result("empty transcript handled gracefully", passed, msg)

    print()

    print("[Section 5] Session/agent ID handling")
    passed, msg = test_session_id_from_first_event()
    test_result("session_id extracted from event", passed, msg)

    passed, msg = test_session_id_override()
    test_result("--session-id overrides event value", passed, msg)

    passed, msg = test_agent_id_override()
    test_result("--agent-id overrides default", passed, msg)

    print()

    print("[Section 6] Token confidence")
    passed, msg = test_token_confidence_low()
    test_result("token_confidence set to 'low'", passed, msg)

    print()

    h.summarize_and_exit()
