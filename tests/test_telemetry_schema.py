#!/usr/bin/env python3
"""
Test suite for telemetry_schema.py.

Covers: event building and validation, token normalization, metric fields,
outcome helpers, and atomic file append.

Run with: python3 tests/test_telemetry_schema.py
"""

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# Add parent/scripts to path so we can import telemetry_schema
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import telemetry_schema

from _test_harness import REPO_ROOT, Harness


def test_invalid_event_type():
    """build_event rejects an invalid event_type."""
    try:
        telemetry_schema.build_event(
            "invalid.type",
            session_id="123",
            timestamp="2026-08-26T12:00:00Z",
        )
        return False, "should have raised ValueError"
    except ValueError as e:
        return "invalid" in str(e).lower(), f"got: {e}"


def test_missing_session_id():
    """build_event rejects missing session_id."""
    try:
        telemetry_schema.build_event(
            "session.begin",
            session_id="",
            timestamp="2026-08-26T12:00:00Z",
        )
        return False, "should have raised ValueError for empty session_id"
    except ValueError as e:
        return "session_id" in str(e).lower(), f"got: {e}"


def test_missing_timestamp():
    """build_event rejects missing timestamp."""
    try:
        telemetry_schema.build_event(
            "session.begin",
            session_id="123",
            timestamp="",
        )
        return False, "should have raised ValueError for empty timestamp"
    except ValueError as e:
        return "timestamp" in str(e).lower(), f"got: {e}"


def test_tokens_filled_with_unknown():
    """build_event fills missing token keys with UNKNOWN."""
    event = telemetry_schema.build_event(
        "command.end",
        session_id="123",
        timestamp="2026-08-26T12:00:00Z",
        tokens={"input": 100},  # missing output, cache_read, cache_creation
    )
    tokens = event.get("tokens", {})
    return (
        tokens.get("input") == 100
        and tokens.get("output") == telemetry_schema.UNKNOWN
        and tokens.get("cache_read") == telemetry_schema.UNKNOWN
        and tokens.get("cache_creation") == telemetry_schema.UNKNOWN
    ), f"got tokens: {tokens}"


def test_metric_fields_default_to_unknown():
    """Metric fields (turns, elapsed_seconds, etc.) default to 'unknown' string."""
    event = telemetry_schema.build_event(
        "command.end",
        session_id="123",
        timestamp="2026-08-26T12:00:00Z",
    )
    return (
        event.get("turns") == telemetry_schema.UNKNOWN
        and event.get("elapsed_seconds") == telemetry_schema.UNKNOWN
        and event.get("retries") == telemetry_schema.UNKNOWN
        and event.get("peak_concurrency") == telemetry_schema.UNKNOWN
        and event.get("transcript_size") == telemetry_schema.UNKNOWN
        and event.get("output_artifact_size") == telemetry_schema.UNKNOWN
    ), f"got event: {event}"


def test_optional_fields_omitted_when_none():
    """Optional fields are omitted from output when None."""
    event = telemetry_schema.build_event(
        "session.begin",
        session_id="123",
        timestamp="2026-08-26T12:00:00Z",
        command_id=None,
        stage_id=None,
        repo=None,
    )
    return (
        "command_id" not in event
        and "stage_id" not in event
        and "repo" not in event
    ), f"got event keys: {event.keys()}"


def test_outcome_success():
    """outcome_success returns correct dict."""
    outcome = telemetry_schema.outcome_success()
    return outcome == {"status": "success"}, f"got: {outcome}"


def test_outcome_failure_valid():
    """outcome_failure accepts valid failure class."""
    outcome = telemetry_schema.outcome_failure("timeout")
    return outcome == {"status": "failure", "class": "timeout"}, f"got: {outcome}"


def test_outcome_failure_invalid():
    """outcome_failure rejects invalid failure class."""
    try:
        telemetry_schema.outcome_failure("invalid_class")
        return False, "should have raised ValueError"
    except ValueError:
        return True, ""


def test_outcome_interrupted():
    """outcome_interrupted returns correct dict."""
    outcome = telemetry_schema.outcome_interrupted()
    return outcome == {"status": "interrupted"}, f"got: {outcome}"


def test_validate_well_formed_event():
    """validate_event returns empty list for well-formed event."""
    event = telemetry_schema.build_event(
        "session.begin",
        session_id="abc123",
        timestamp="2026-08-26T12:00:00Z",
    )
    errors = telemetry_schema.validate_event(event)
    return errors == [], f"got errors: {errors}"


def test_validate_missing_schema_version():
    """validate_event catches missing schema_version."""
    event = {"event_type": "session.begin", "timestamp": "2026-08-26T12:00:00Z", "session_id": "123"}
    errors = telemetry_schema.validate_event(event)
    return any("schema_version" in e.lower() for e in errors), f"got errors: {errors}"


def test_validate_bad_event_type():
    """validate_event catches invalid event_type."""
    event = telemetry_schema.build_event(
        "session.begin",
        session_id="123",
        timestamp="2026-08-26T12:00:00Z",
    )
    event["event_type"] = "invalid.type"
    errors = telemetry_schema.validate_event(event)
    return any("event_type" in e.lower() for e in errors), f"got errors: {errors}"


def test_validate_unparseable_timestamp():
    """validate_event catches unparseable timestamp."""
    event = telemetry_schema.build_event(
        "session.begin",
        session_id="123",
        timestamp="2026-08-26T12:00:00Z",
    )
    event["timestamp"] = "not-a-timestamp"
    errors = telemetry_schema.validate_event(event)
    return any("timestamp" in e.lower() for e in errors), f"got errors: {errors}"


def test_validate_timestamp_with_trailing_z():
    """validate_event accepts timestamp with trailing Z."""
    event = telemetry_schema.build_event(
        "session.begin",
        session_id="123",
        timestamp="2026-08-26T12:00:00Z",
    )
    errors = telemetry_schema.validate_event(event)
    return errors == [], f"got errors: {errors}"


def test_validate_missing_session_id():
    """validate_event catches missing session_id."""
    event = {
        "schema_version": telemetry_schema.SCHEMA_VERSION,
        "event_type": "session.begin",
        "timestamp": "2026-08-26T12:00:00Z",
    }
    errors = telemetry_schema.validate_event(event)
    return any("session_id" in e.lower() for e in errors), f"got errors: {errors}"


def test_validate_bad_outcome_status():
    """validate_event catches invalid outcome.status."""
    event = telemetry_schema.build_event(
        "command.end",
        session_id="123",
        timestamp="2026-08-26T12:00:00Z",
        outcome={"status": "invalid_status"},
    )
    errors = telemetry_schema.validate_event(event)
    return any("status" in e.lower() for e in errors), f"got errors: {errors}"


def test_validate_failure_missing_class():
    """validate_event catches failure outcome without class."""
    event = telemetry_schema.build_event(
        "command.end",
        session_id="123",
        timestamp="2026-08-26T12:00:00Z",
        outcome={"status": "failure"},  # missing 'class'
    )
    errors = telemetry_schema.validate_event(event)
    return any("class" in e.lower() for e in errors), f"got errors: {errors}"


def test_validate_failure_bad_class():
    """validate_event catches invalid failure class."""
    event = telemetry_schema.build_event(
        "command.end",
        session_id="123",
        timestamp="2026-08-26T12:00:00Z",
        outcome={"status": "failure", "class": "invalid_class"},
    )
    errors = telemetry_schema.validate_event(event)
    return any("class" in e.lower() for e in errors), f"got errors: {errors}"


def test_append_event_creates_file():
    """append_event creates the log file and appends one line."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"
        event = telemetry_schema.build_event(
            "session.begin",
            session_id="123",
            timestamp="2026-08-26T12:00:00Z",
        )
        telemetry_schema.append_event(log_path, event)

        # Check file exists and contains one line
        if not log_path.exists():
            return False, "log file was not created"

        with open(log_path) as f:
            lines = f.readlines()

        if len(lines) != 1:
            return False, f"expected 1 line, got {len(lines)}"

        # Parse the line
        try:
            parsed = json.loads(lines[0].strip())
            if parsed.get("session_id") != "123":
                return False, f"session_id not found in line"
            return True, ""
        except json.JSONDecodeError as e:
            return False, f"line is not valid JSON: {e}"


def test_append_event_appends_multiple():
    """append_event appends to existing file, doesn't overwrite."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"

        # Append first event
        event1 = telemetry_schema.build_event(
            "session.begin",
            session_id="111",
            timestamp="2026-08-26T12:00:00Z",
        )
        telemetry_schema.append_event(log_path, event1)

        # Append second event
        event2 = telemetry_schema.build_event(
            "session.end",
            session_id="111",
            timestamp="2026-08-26T12:01:00Z",
        )
        telemetry_schema.append_event(log_path, event2)

        # Check file has two lines
        with open(log_path) as f:
            lines = f.readlines()

        if len(lines) != 2:
            return False, f"expected 2 lines, got {len(lines)}"

        # Parse both lines
        try:
            parsed1 = json.loads(lines[0].strip())
            parsed2 = json.loads(lines[1].strip())
            if parsed1.get("event_type") != "session.begin":
                return False, "first event type wrong"
            if parsed2.get("event_type") != "session.end":
                return False, "second event type wrong"
            return True, ""
        except json.JSONDecodeError as e:
            return False, f"line is not valid JSON: {e}"


def test_default_log_path():
    """default_log_path returns a Path in ~/.claude/telemetry/events.jsonl."""
    path = telemetry_schema.default_log_path()
    path_str = str(path)
    return (
        ".claude" in path_str and "telemetry" in path_str and "events.jsonl" in path_str
    ), f"got path: {path_str}"


if __name__ == "__main__":
    h = Harness("TELEMETRY_SCHEMA TEST SUITE")

    test_result = h.test_result

    # Event building tests
    print("[Section 1] Event building")
    passed, msg = test_invalid_event_type()
    test_result("invalid event_type rejected", passed, msg)

    passed, msg = test_missing_session_id()
    test_result("missing session_id rejected", passed, msg)

    passed, msg = test_missing_timestamp()
    test_result("missing timestamp rejected", passed, msg)

    passed, msg = test_tokens_filled_with_unknown()
    test_result("tokens dict filled with UNKNOWN", passed, msg)

    passed, msg = test_metric_fields_default_to_unknown()
    test_result("metric fields default to 'unknown'", passed, msg)

    passed, msg = test_optional_fields_omitted_when_none()
    test_result("optional fields omitted when None", passed, msg)

    print()

    # Outcome tests
    print("[Section 2] Outcome helpers")
    passed, msg = test_outcome_success()
    test_result("outcome_success", passed, msg)

    passed, msg = test_outcome_failure_valid()
    test_result("outcome_failure accepts valid class", passed, msg)

    passed, msg = test_outcome_failure_invalid()
    test_result("outcome_failure rejects invalid class", passed, msg)

    passed, msg = test_outcome_interrupted()
    test_result("outcome_interrupted", passed, msg)

    print()

    # Validation tests
    print("[Section 3] Event validation")
    passed, msg = test_validate_well_formed_event()
    test_result("well-formed event validates", passed, msg)

    passed, msg = test_validate_missing_schema_version()
    test_result("missing schema_version caught", passed, msg)

    passed, msg = test_validate_bad_event_type()
    test_result("bad event_type caught", passed, msg)

    passed, msg = test_validate_unparseable_timestamp()
    test_result("unparseable timestamp caught", passed, msg)

    passed, msg = test_validate_timestamp_with_trailing_z()
    test_result("timestamp with trailing Z accepted", passed, msg)

    passed, msg = test_validate_missing_session_id()
    test_result("missing session_id caught", passed, msg)

    passed, msg = test_validate_bad_outcome_status()
    test_result("bad outcome.status caught", passed, msg)

    passed, msg = test_validate_failure_missing_class()
    test_result("failure without class caught", passed, msg)

    passed, msg = test_validate_failure_bad_class()
    test_result("invalid failure.class caught", passed, msg)

    print()

    # File append tests
    print("[Section 4] File operations")
    passed, msg = test_append_event_creates_file()
    test_result("append_event creates file and appends", passed, msg)

    passed, msg = test_append_event_appends_multiple()
    test_result("append_event appends to existing file", passed, msg)

    passed, msg = test_default_log_path()
    test_result("default_log_path returns valid path", passed, msg)

    print()

    h.summarize_and_exit()
