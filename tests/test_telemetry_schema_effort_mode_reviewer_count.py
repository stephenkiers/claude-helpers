#!/usr/bin/env python3
"""
Test suite for telemetry_schema.py effort/mode/reviewer_count fields.

Tests the new effort, mode, and reviewer_count parameters added to build_event()
and the corresponding validation in validate_event().

Covers:
1. build_event() accepts effort (1-5), mode (local/pr/coworker), reviewer_count (int)
2. Fields are included in the event dict when provided
3. Fields are omitted when None
4. validate_event() rejects invalid effort/mode values
5. validate_event() rejects negative/non-int reviewer_count
6. Valid values pass through without errors

Run with: python3 tests/test_telemetry_schema_effort_mode_reviewer_count.py
"""

import sys
from pathlib import Path

# Add parent/scripts to path so we can import telemetry_schema
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import telemetry_schema

from _test_harness import REPO_ROOT, Harness


def test_build_event_accepts_valid_effort_values():
    """build_event() accepts all valid effort values (1-5)."""
    for effort in ["1", "2", "3", "4", "5"]:
        try:
            event = telemetry_schema.build_event(
                "command.begin",
                session_id="test-session",
                timestamp="2026-09-10T12:00:00Z",
                effort=effort,
            )
            if event.get("effort") != effort:
                return False, f"effort {effort} not in event: {event}"
        except Exception as e:
            return False, f"Failed to build event with effort={effort}: {e}"
    return True, ""


def test_build_event_includes_effort_when_provided():
    """build_event() includes effort field when provided."""
    event = telemetry_schema.build_event(
        "command.begin",
        session_id="test-session",
        timestamp="2026-09-10T12:00:00Z",
        effort="3",
    )
    if "effort" not in event:
        return False, "effort field missing from event"
    if event["effort"] != "3":
        return False, f"effort value is {event['effort']}, expected '3'"
    return True, ""


def test_build_event_omits_effort_when_none():
    """build_event() omits effort field when effort=None."""
    event = telemetry_schema.build_event(
        "command.begin",
        session_id="test-session",
        timestamp="2026-09-10T12:00:00Z",
        effort=None,
    )
    if "effort" in event:
        return False, f"effort field should not be in event when None, but got: {event.get('effort')}"
    return True, ""


def test_build_event_accepts_valid_mode_values():
    """build_event() accepts all valid mode values (local, pr, coworker)."""
    for mode in ["local", "pr", "coworker"]:
        try:
            event = telemetry_schema.build_event(
                "command.begin",
                session_id="test-session",
                timestamp="2026-09-10T12:00:00Z",
                mode=mode,
            )
            if event.get("mode") != mode:
                return False, f"mode {mode} not in event: {event}"
        except Exception as e:
            return False, f"Failed to build event with mode={mode}: {e}"
    return True, ""


def test_build_event_includes_mode_when_provided():
    """build_event() includes mode field when provided."""
    event = telemetry_schema.build_event(
        "command.begin",
        session_id="test-session",
        timestamp="2026-09-10T12:00:00Z",
        mode="pr",
    )
    if "mode" not in event:
        return False, "mode field missing from event"
    if event["mode"] != "pr":
        return False, f"mode value is {event['mode']}, expected 'pr'"
    return True, ""


def test_build_event_omits_mode_when_none():
    """build_event() omits mode field when mode=None."""
    event = telemetry_schema.build_event(
        "command.begin",
        session_id="test-session",
        timestamp="2026-09-10T12:00:00Z",
        mode=None,
    )
    if "mode" in event:
        return False, f"mode field should not be in event when None, but got: {event.get('mode')}"
    return True, ""


def test_build_event_includes_reviewer_count_when_provided():
    """build_event() includes reviewer_count field when provided."""
    event = telemetry_schema.build_event(
        "command.begin",
        session_id="test-session",
        timestamp="2026-09-10T12:00:00Z",
        reviewer_count=14,
    )
    if "reviewer_count" not in event:
        return False, "reviewer_count field missing from event"
    if event["reviewer_count"] != 14:
        return False, f"reviewer_count value is {event['reviewer_count']}, expected 14"
    return True, ""


def test_build_event_omits_reviewer_count_when_none():
    """build_event() omits reviewer_count field when reviewer_count=None."""
    event = telemetry_schema.build_event(
        "command.begin",
        session_id="test-session",
        timestamp="2026-09-10T12:00:00Z",
        reviewer_count=None,
    )
    if "reviewer_count" in event:
        return False, f"reviewer_count field should not be in event when None, but got: {event.get('reviewer_count')}"
    return True, ""


def test_build_event_with_all_three_new_fields():
    """build_event() includes all three fields when all provided."""
    event = telemetry_schema.build_event(
        "stage.end",
        session_id="test-session",
        timestamp="2026-09-10T12:00:00Z",
        effort="2",
        mode="local",
        reviewer_count=8,
    )
    if event.get("effort") != "2":
        return False, f"effort not set correctly: {event.get('effort')}"
    if event.get("mode") != "local":
        return False, f"mode not set correctly: {event.get('mode')}"
    if event.get("reviewer_count") != 8:
        return False, f"reviewer_count not set correctly: {event.get('reviewer_count')}"
    return True, ""


def test_validate_event_accepts_valid_effort():
    """validate_event() accepts events with valid effort values."""
    event = telemetry_schema.build_event(
        "command.begin",
        session_id="test-session",
        timestamp="2026-09-10T12:00:00Z",
        effort="4",
    )
    errors = telemetry_schema.validate_event(event)
    effort_errors = [e for e in errors if "effort" in e.lower()]
    if effort_errors:
        return False, f"validate_event rejected valid effort: {effort_errors}"
    return True, ""


def test_validate_event_rejects_invalid_effort():
    """validate_event() rejects events with invalid effort values."""
    event = telemetry_schema.build_event(
        "command.begin",
        session_id="test-session",
        timestamp="2026-09-10T12:00:00Z",
        effort="6",  # Invalid: outside 1-5 range
    )
    errors = telemetry_schema.validate_event(event)
    effort_errors = [e for e in errors if "effort" in e.lower()]
    if not effort_errors:
        return False, "validate_event should reject effort='6' but didn't"
    return True, ""


def test_validate_event_rejects_effort_zero():
    """validate_event() rejects effort='0' (below valid range)."""
    event = telemetry_schema.build_event(
        "command.begin",
        session_id="test-session",
        timestamp="2026-09-10T12:00:00Z",
        effort="0",
    )
    errors = telemetry_schema.validate_event(event)
    effort_errors = [e for e in errors if "effort" in e.lower()]
    if not effort_errors:
        return False, "validate_event should reject effort='0' but didn't"
    return True, ""


def test_validate_event_accepts_valid_mode():
    """validate_event() accepts events with valid mode values."""
    for mode in ["local", "pr", "coworker"]:
        event = telemetry_schema.build_event(
            "command.begin",
            session_id="test-session",
            timestamp="2026-09-10T12:00:00Z",
            mode=mode,
        )
        errors = telemetry_schema.validate_event(event)
        mode_errors = [e for e in errors if "mode" in e.lower()]
        if mode_errors:
            return False, f"validate_event rejected valid mode '{mode}': {mode_errors}"
    return True, ""


def test_validate_event_rejects_invalid_mode():
    """validate_event() rejects events with invalid mode values."""
    event = telemetry_schema.build_event(
        "command.begin",
        session_id="test-session",
        timestamp="2026-09-10T12:00:00Z",
        mode="invalid_mode",
    )
    errors = telemetry_schema.validate_event(event)
    mode_errors = [e for e in errors if "mode" in e.lower()]
    if not mode_errors:
        return False, "validate_event should reject mode='invalid_mode' but didn't"
    return True, ""


def test_validate_event_rejects_negative_reviewer_count():
    """validate_event() rejects negative reviewer_count."""
    event = telemetry_schema.build_event(
        "command.begin",
        session_id="test-session",
        timestamp="2026-09-10T12:00:00Z",
        reviewer_count=-1,
    )
    errors = telemetry_schema.validate_event(event)
    count_errors = [e for e in errors if "reviewer_count" in e.lower() or "count" in e.lower()]
    if not count_errors:
        return False, "validate_event should reject negative reviewer_count but didn't"
    return True, ""


def test_validate_event_accepts_zero_reviewer_count():
    """validate_event() accepts reviewer_count=0 (valid edge case)."""
    event = telemetry_schema.build_event(
        "command.begin",
        session_id="test-session",
        timestamp="2026-09-10T12:00:00Z",
        reviewer_count=0,
    )
    errors = telemetry_schema.validate_event(event)
    count_errors = [e for e in errors if "reviewer_count" in e.lower()]
    if count_errors:
        return False, f"validate_event should accept reviewer_count=0 but rejected: {count_errors}"
    return True, ""


def test_validate_event_accepts_large_reviewer_count():
    """validate_event() accepts large reviewer_count values."""
    event = telemetry_schema.build_event(
        "command.begin",
        session_id="test-session",
        timestamp="2026-09-10T12:00:00Z",
        reviewer_count=1000,
    )
    errors = telemetry_schema.validate_event(event)
    count_errors = [e for e in errors if "reviewer_count" in e.lower()]
    if count_errors:
        return False, f"validate_event should accept large reviewer_count but rejected: {count_errors}"
    return True, ""


def test_validate_event_rejects_non_int_reviewer_count():
    """validate_event() rejects non-integer reviewer_count (forced to int before validation)."""
    # Note: build_event may coerce this, so we manually create an invalid event dict
    event = {
        "schema_version": telemetry_schema.SCHEMA_VERSION,
        "event_type": "command.begin",
        "session_id": "test-session",
        "timestamp": "2026-09-10T12:00:00Z",
        "reviewer_count": "not-an-int",  # Invalid: string instead of int
    }
    errors = telemetry_schema.validate_event(event)
    count_errors = [e for e in errors if "reviewer_count" in e.lower()]
    if not count_errors:
        return False, "validate_event should reject non-integer reviewer_count but didn't"
    return True, ""


def test_build_event_effort_modes_defined():
    """Constants EFFORT_LEVELS and RUN_MODES are properly defined."""
    if not hasattr(telemetry_schema, "EFFORT_LEVELS"):
        return False, "EFFORT_LEVELS constant not found"
    if not hasattr(telemetry_schema, "RUN_MODES"):
        return False, "RUN_MODES constant not found"

    expected_efforts = {"1", "2", "3", "4", "5"}
    if telemetry_schema.EFFORT_LEVELS != expected_efforts:
        return False, f"EFFORT_LEVELS is {telemetry_schema.EFFORT_LEVELS}, expected {expected_efforts}"

    expected_modes = {"local", "pr", "coworker"}
    if telemetry_schema.RUN_MODES != expected_modes:
        return False, f"RUN_MODES is {telemetry_schema.RUN_MODES}, expected {expected_modes}"

    return True, ""


if __name__ == "__main__":
    h = Harness("TELEMETRY_SCHEMA EFFORT/MODE/REVIEWER_COUNT TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Constants and imports")
    passed, msg = test_build_event_effort_modes_defined()
    test_result("EFFORT_LEVELS and RUN_MODES constants defined", passed, msg)

    print()
    print("[Section 2] build_event() effort field")
    passed, msg = test_build_event_accepts_valid_effort_values()
    test_result("build_event accepts all valid effort values (1-5)", passed, msg)

    passed, msg = test_build_event_includes_effort_when_provided()
    test_result("build_event includes effort field when provided", passed, msg)

    passed, msg = test_build_event_omits_effort_when_none()
    test_result("build_event omits effort field when None", passed, msg)

    print()
    print("[Section 3] build_event() mode field")
    passed, msg = test_build_event_accepts_valid_mode_values()
    test_result("build_event accepts all valid mode values (local/pr/coworker)", passed, msg)

    passed, msg = test_build_event_includes_mode_when_provided()
    test_result("build_event includes mode field when provided", passed, msg)

    passed, msg = test_build_event_omits_mode_when_none()
    test_result("build_event omits mode field when None", passed, msg)

    print()
    print("[Section 4] build_event() reviewer_count field")
    passed, msg = test_build_event_includes_reviewer_count_when_provided()
    test_result("build_event includes reviewer_count field when provided", passed, msg)

    passed, msg = test_build_event_omits_reviewer_count_when_none()
    test_result("build_event omits reviewer_count field when None", passed, msg)

    print()
    print("[Section 5] build_event() with all three new fields")
    passed, msg = test_build_event_with_all_three_new_fields()
    test_result("build_event includes all three fields when all provided", passed, msg)

    print()
    print("[Section 6] validate_event() effort validation")
    passed, msg = test_validate_event_accepts_valid_effort()
    test_result("validate_event accepts valid effort values", passed, msg)

    passed, msg = test_validate_event_rejects_invalid_effort()
    test_result("validate_event rejects invalid effort (out of range)", passed, msg)

    passed, msg = test_validate_event_rejects_effort_zero()
    test_result("validate_event rejects effort='0' (below range)", passed, msg)

    print()
    print("[Section 7] validate_event() mode validation")
    passed, msg = test_validate_event_accepts_valid_mode()
    test_result("validate_event accepts valid mode values", passed, msg)

    passed, msg = test_validate_event_rejects_invalid_mode()
    test_result("validate_event rejects invalid mode", passed, msg)

    print()
    print("[Section 8] validate_event() reviewer_count validation")
    passed, msg = test_validate_event_rejects_negative_reviewer_count()
    test_result("validate_event rejects negative reviewer_count", passed, msg)

    passed, msg = test_validate_event_accepts_zero_reviewer_count()
    test_result("validate_event accepts reviewer_count=0", passed, msg)

    passed, msg = test_validate_event_accepts_large_reviewer_count()
    test_result("validate_event accepts large reviewer_count", passed, msg)

    passed, msg = test_validate_event_rejects_non_int_reviewer_count()
    test_result("validate_event rejects non-integer reviewer_count", passed, msg)

    print()
    h.summarize_and_exit()
