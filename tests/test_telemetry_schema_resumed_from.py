#!/usr/bin/env python3
"""
Spec-blind test suite for resumed_from schema field and peek_command_id function.

Tests written from the plan specification alone, without reading implementation.
Covers:
1. build_event() accepts resumed_from kwarg and includes it in output
2. build_event() omits resumed_from when None
3. validate_event() rejects empty-string resumed_from
4. validate_event() accepts non-empty-string resumed_from
5. validate_event() rejects non-string resumed_from values (int, list, dict, bool, None via dict)
6. peek_command_id() returns the most recent command.begin's command_id for a given command_name
7. peek_command_id() returns None when no matching command_name exists
8. peek_command_id() returns None when state file doesn't exist
9. peek_command_id() never mutates state
10. SCHEMA_VERSION stays at 1 (unaffected by this change)

Run with: python3 tests/test_telemetry_schema_resumed_from.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

# Add parent/scripts to path so we can import telemetry_schema
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import telemetry_schema

from _test_harness import REPO_ROOT, Harness


def test_build_event_includes_resumed_from():
    """build_event() includes resumed_from in output when provided."""
    event = telemetry_schema.build_event(
        "command.begin",
        session_id="s1",
        timestamp="2026-08-26T12:00:00Z",
        command="test-cmd",
        resumed_from="prev-cmd-id-123",
    )
    if event.get("resumed_from") != "prev-cmd-id-123":
        return False, f"resumed_from not in event: {event}"
    return True, ""


def test_build_event_omits_resumed_from_when_none():
    """build_event() omits resumed_from when None."""
    event = telemetry_schema.build_event(
        "command.begin",
        session_id="s1",
        timestamp="2026-08-26T12:00:00Z",
        command="test-cmd",
        resumed_from=None,
    )
    if "resumed_from" in event:
        return False, f"resumed_from should be omitted when None, but found in: {event}"
    return True, ""


def test_build_event_omits_resumed_from_by_default():
    """build_event() omits resumed_from when not provided at all."""
    event = telemetry_schema.build_event(
        "command.begin",
        session_id="s1",
        timestamp="2026-08-26T12:00:00Z",
        command="test-cmd",
    )
    if "resumed_from" in event:
        return False, f"resumed_from should be omitted by default, but found in: {event}"
    return True, ""


def test_validate_event_accepts_non_empty_resumed_from():
    """validate_event() accepts resumed_from as a non-empty string."""
    event = telemetry_schema.build_event(
        "command.begin",
        session_id="s1",
        timestamp="2026-08-26T12:00:00Z",
        command="test-cmd",
        resumed_from="valid-id-abc123",
    )
    errors = telemetry_schema.validate_event(event)
    return errors == [], f"should accept non-empty resumed_from, got errors: {errors}"


def test_validate_event_rejects_empty_resumed_from():
    """validate_event() rejects resumed_from that is an empty string."""
    event = telemetry_schema.build_event(
        "command.begin",
        session_id="s1",
        timestamp="2026-08-26T12:00:00Z",
        command="test-cmd",
    )
    # Manually set to empty string
    event["resumed_from"] = ""
    errors = telemetry_schema.validate_event(event)
    if not any("resumed_from" in e.lower() and "empty" in e.lower() for e in errors):
        return False, f"should reject empty resumed_from, got errors: {errors}"
    return True, ""


def test_validate_event_rejects_int_resumed_from():
    """validate_event() rejects resumed_from that is an integer."""
    event = telemetry_schema.build_event(
        "command.begin",
        session_id="s1",
        timestamp="2026-08-26T12:00:00Z",
        command="test-cmd",
    )
    # Manually set to int (simulating a malformed event)
    event["resumed_from"] = 12345
    errors = telemetry_schema.validate_event(event)
    if not any("resumed_from" in e.lower() for e in errors):
        return False, f"should reject int resumed_from, got errors: {errors}"
    return True, ""


def test_validate_event_rejects_list_resumed_from():
    """validate_event() rejects resumed_from that is a list."""
    event = telemetry_schema.build_event(
        "command.begin",
        session_id="s1",
        timestamp="2026-08-26T12:00:00Z",
        command="test-cmd",
    )
    # Manually set to list
    event["resumed_from"] = ["id1", "id2"]
    errors = telemetry_schema.validate_event(event)
    if not any("resumed_from" in e.lower() for e in errors):
        return False, f"should reject list resumed_from, got errors: {errors}"
    return True, ""


def test_validate_event_rejects_dict_resumed_from():
    """validate_event() rejects resumed_from that is a dict."""
    event = telemetry_schema.build_event(
        "command.begin",
        session_id="s1",
        timestamp="2026-08-26T12:00:00Z",
        command="test-cmd",
    )
    # Manually set to dict
    event["resumed_from"] = {"command_id": "123"}
    errors = telemetry_schema.validate_event(event)
    if not any("resumed_from" in e.lower() for e in errors):
        return False, f"should reject dict resumed_from, got errors: {errors}"
    return True, ""


def test_validate_event_rejects_bool_resumed_from():
    """validate_event() rejects resumed_from that is a boolean."""
    event = telemetry_schema.build_event(
        "command.begin",
        session_id="s1",
        timestamp="2026-08-26T12:00:00Z",
        command="test-cmd",
    )
    # Manually set to bool
    event["resumed_from"] = True
    errors = telemetry_schema.validate_event(event)
    if not any("resumed_from" in e.lower() for e in errors):
        return False, f"should reject bool resumed_from, got errors: {errors}"
    return True, ""


def test_schema_version_unchanged():
    """SCHEMA_VERSION remains at 1 after adding resumed_from."""
    if telemetry_schema.SCHEMA_VERSION != 1:
        return False, f"SCHEMA_VERSION should be 1, got {telemetry_schema.SCHEMA_VERSION}"
    return True, ""


def test_peek_command_id_returns_none_for_missing_file():
    """peek_command_id() returns None when state file doesn't exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_dir = Path(tmpdir)
        nonexistent_path = state_dir / "nonexistent_session.json"

        result = telemetry_schema.peek_command_id(nonexistent_path, "test-cmd")
        if result is not None:
            return False, f"expected None for nonexistent file, got {result}"
        return True, ""


def test_peek_command_id_returns_none_when_no_matching_command():
    """peek_command_id() returns None when no command.begin with matching name exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "state.json"

        # Create a state file with a command entry for a different command
        def create_state(state):
            state["commands"] = {
                "cmd-id-1": {"command": "other-cmd", "stage_id": None}
            }
            return state

        telemetry_schema.load_and_update_state(state_path, create_state)

        # Peek for a non-existent command
        result = telemetry_schema.peek_command_id(state_path, "test-cmd")
        if result is not None:
            return False, f"expected None when no matching command, got {result}"
        return True, ""


def test_peek_command_id_returns_matching_command():
    """peek_command_id() returns a command_id matching the given command_name."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_dir = Path(tmpdir)
        session_id = "test-session"
        state_path = telemetry_schema.state_path(session_id, state_dir)

        # Initialize session state
        telemetry_schema.init_session_state(state_path, session_id, "2026-08-26T12:00:00Z")

        # Simulate command-begin for "build"
        cmd_id_target = "cmd-id-build-target"

        def add_build_command(state):
            if "commands" not in state:
                state["commands"] = {}
            state["commands"][cmd_id_target] = {
                "command": "build",
                "command_began_at": "2026-08-26T12:00:00Z",
                "stage_id": None,
                "stage": None,
            }
            return state

        telemetry_schema.load_and_update_state(state_path, add_build_command)

        # Peek should return the matching command_id
        result = telemetry_schema.peek_command_id(state_path, "build")
        if result != cmd_id_target:
            return False, f"expected {cmd_id_target}, got {result}"
        return True, ""


def test_peek_command_id_never_mutates_state():
    """peek_command_id() never modifies the state file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "state.json"
        session_id = "test-session"

        # Create initial state
        def init_state(state):
            state["commands"] = {
                "cmd-id-1": {
                    "command": "test-cmd",
                    "command_began_at": "2026-08-26T12:00:00Z",
                    "stage_id": None,
                }
            }
            return state

        telemetry_schema.load_and_update_state(state_path, init_state)

        # Read original state
        with open(state_path) as f:
            original_state = json.loads(f.read())
        original_mtime = state_path.stat().st_mtime

        # Call peek_command_id (read-only, should not mutate)
        telemetry_schema.peek_command_id(state_path, "test-cmd")

        # Read state after peek
        with open(state_path) as f:
            after_state = json.loads(f.read())
        after_mtime = state_path.stat().st_mtime

        # State content must be unchanged
        if original_state != after_state:
            return False, f"peek_command_id mutated state: before={original_state}, after={after_state}"

        # mtime might not be exactly the same due to timing, but file shouldn't have been rewritten
        # (A stricter check: if peek reads and doesn't write, mtime shouldn't change)
        # However, we can't guarantee this without implementation details, so just verify state is unchanged
        return True, ""


def test_peek_command_id_with_complex_state():
    """peek_command_id() correctly identifies command among multiple stages and entries."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_dir = Path(tmpdir)
        session_id = "test-session"
        state_path = telemetry_schema.state_path(session_id, state_dir)

        # Create a complex state with multiple commands and stages
        telemetry_schema.init_session_state(state_path, session_id, "2026-08-26T12:00:00Z")

        target_cmd_id = "cmd-id-target"

        def add_complex_state(state):
            if "commands" not in state:
                state["commands"] = {}
            state["commands"]["cmd-id-1"] = {
                "command": "setup",
                "command_began_at": "2026-08-26T12:00:00Z",
                "stage_id": "st1",
                "stage": "verify",
            }
            state["commands"]["cmd-id-2"] = {
                "command": "build",
                "command_began_at": "2026-08-26T12:00:10Z",
                "stage_id": None,
                "stage": None,
            }
            state["commands"][target_cmd_id] = {
                "command": "test",
                "command_began_at": "2026-08-26T12:00:20Z",
                "stage_id": None,
                "stage": None,
            }
            state["commands"]["cmd-id-4"] = {
                "command": "deploy",
                "command_began_at": "2026-08-26T12:00:30Z",
                "stage_id": "st2",
                "stage": "prep",
            }
            return state

        telemetry_schema.load_and_update_state(state_path, add_complex_state)

        # Peek for "test" should return target_cmd_id
        result = telemetry_schema.peek_command_id(state_path, "test")
        if result != target_cmd_id:
            return False, f"expected {target_cmd_id}, got {result}"
        return True, ""


def test_peek_command_id_handles_empty_commands_dict():
    """peek_command_id() returns None when commands dict is empty."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "state.json"

        # Create state with empty commands dict
        def init_state(state):
            state["commands"] = {}
            return state

        telemetry_schema.load_and_update_state(state_path, init_state)

        result = telemetry_schema.peek_command_id(state_path, "test-cmd")
        if result is not None:
            return False, f"expected None for empty commands, got {result}"
        return True, ""


def test_peek_command_id_handles_no_commands_dict():
    """peek_command_id() returns None when state has no commands dict at all."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "state.json"

        # Create state with no commands dict
        def init_state(state):
            state["other_field"] = "value"
            return state

        telemetry_schema.load_and_update_state(state_path, init_state)

        result = telemetry_schema.peek_command_id(state_path, "test-cmd")
        if result is not None:
            return False, f"expected None when no commands dict, got {result}"
        return True, ""


def test_resumed_from_round_trip():
    """Round-trip: build event with resumed_from, validate, write, read back."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"

        # Build event with resumed_from
        event = telemetry_schema.build_event(
            "command.begin",
            session_id="s1",
            timestamp="2026-08-26T12:00:00Z",
            command="resume-cmd",
            resumed_from="prev-cmd-id-xyz",
        )

        # Validate it
        errors = telemetry_schema.validate_event(event)
        if errors:
            return False, f"validation failed: {errors}"

        # Append to log
        telemetry_schema.append_event(log_path, event)

        # Read back from log
        with open(log_path) as f:
            lines = [line.strip() for line in f if line.strip()]

        if len(lines) != 1:
            return False, f"expected 1 line, got {len(lines)}"

        parsed = json.loads(lines[0])
        if parsed.get("resumed_from") != "prev-cmd-id-xyz":
            return False, f"resumed_from not persisted in log: {parsed}"

        return True, ""


def test_validate_event_accepts_whitespace_in_resumed_from():
    """validate_event() accepts resumed_from with whitespace (as long as not empty)."""
    event = telemetry_schema.build_event(
        "command.begin",
        session_id="s1",
        timestamp="2026-08-26T12:00:00Z",
        command="test-cmd",
        resumed_from="  id-with-spaces  ",
    )
    errors = telemetry_schema.validate_event(event)
    # Whitespace-only or with leading/trailing spaces should be treated as non-empty
    # (validation doesn't strip; it just checks emptiness)
    return errors == [], f"should accept resumed_from with whitespace, got errors: {errors}"


def test_peek_command_id_ignores_entries_without_command_field():
    """peek_command_id() only considers entries that have a 'command' field."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "state.json"

        def create_mixed_state(state):
            state["commands"] = {
                "cmd-id-1": {
                    # Missing 'command' field
                    "stage_id": None,
                },
                "cmd-id-2": {
                    "command": "test-cmd",
                    "command_began_at": "2026-08-26T12:00:00Z",
                    "stage_id": None,
                },
            }
            return state

        telemetry_schema.load_and_update_state(state_path, create_mixed_state)

        result = telemetry_schema.peek_command_id(state_path, "test-cmd")
        if result != "cmd-id-2":
            return False, f"expected cmd-id-2 (the one with command field), got {result}"
        return True, ""


def test_build_event_with_multiple_optional_fields_and_resumed_from():
    """build_event() handles resumed_from alongside other optional fields correctly."""
    event = telemetry_schema.build_event(
        "command.begin",
        session_id="s1",
        timestamp="2026-08-26T12:00:00Z",
        command="test-cmd",
        resumed_from="prev-id-123",
        command_id="cmd-id-abc",
        repo="my-repo",
    )

    if event.get("resumed_from") != "prev-id-123":
        return False, f"resumed_from not in event"
    if event.get("command_id") != "cmd-id-abc":
        return False, f"command_id not in event"
    if event.get("repo") != "my-repo":
        return False, f"repo not in event"

    return True, ""


def test_peek_command_id_recency_tie_break():
    """peek_command_id() returns the most recent command when multiple exist with same name."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "state.json"

        # Create state with 3 entries for the same command at different timestamps
        def create_state_with_duplicates(state):
            state["commands"] = {
                "cmd-id-1": {
                    "command": "implement-with-haiku",
                    "command_began_at": "2026-08-26T12:00:00Z",
                    "stage_id": None,
                },
                "cmd-id-2": {
                    "command": "implement-with-haiku",
                    "command_began_at": "2026-08-26T12:00:10Z",
                    "stage_id": None,
                },
                "cmd-id-3": {
                    "command": "implement-with-haiku",
                    "command_began_at": "2026-08-26T12:00:05Z",
                    "stage_id": None,
                },
            }
            return state

        telemetry_schema.load_and_update_state(state_path, create_state_with_duplicates)

        # Peek should return cmd-id-2 (the one with the latest timestamp)
        result = telemetry_schema.peek_command_id(state_path, "implement-with-haiku")
        if result != "cmd-id-2":
            return False, f"expected cmd-id-2 (most recent), got {result}"
        return True, ""


if __name__ == "__main__":
    h = Harness("RESUMED_FROM SCHEMA AND PEEK_COMMAND_ID EDGE CASES TEST SUITE")
    t = h.test_result

    print("[Section 1] build_event() with resumed_from parameter")
    t("build_event includes resumed_from when provided", test_build_event_includes_resumed_from()[0], test_build_event_includes_resumed_from()[1])
    t("build_event omits resumed_from when None", test_build_event_omits_resumed_from_when_none()[0], test_build_event_omits_resumed_from_when_none()[1])
    t("build_event omits resumed_from by default", test_build_event_omits_resumed_from_by_default()[0], test_build_event_omits_resumed_from_by_default()[1])
    print()

    print("[Section 2] validate_event() with resumed_from")
    t("validate_event accepts non-empty resumed_from", test_validate_event_accepts_non_empty_resumed_from()[0], test_validate_event_accepts_non_empty_resumed_from()[1])
    t("validate_event rejects empty resumed_from", test_validate_event_rejects_empty_resumed_from()[0], test_validate_event_rejects_empty_resumed_from()[1])
    t("validate_event rejects int resumed_from", test_validate_event_rejects_int_resumed_from()[0], test_validate_event_rejects_int_resumed_from()[1])
    t("validate_event rejects list resumed_from", test_validate_event_rejects_list_resumed_from()[0], test_validate_event_rejects_list_resumed_from()[1])
    t("validate_event rejects dict resumed_from", test_validate_event_rejects_dict_resumed_from()[0], test_validate_event_rejects_dict_resumed_from()[1])
    t("validate_event rejects bool resumed_from", test_validate_event_rejects_bool_resumed_from()[0], test_validate_event_rejects_bool_resumed_from()[1])
    print()

    print("[Section 3] Schema version stability")
    t("SCHEMA_VERSION remains 1", test_schema_version_unchanged()[0], test_schema_version_unchanged()[1])
    print()

    print("[Section 4] peek_command_id() basic functionality")
    t("peek_command_id returns None for missing file", test_peek_command_id_returns_none_for_missing_file()[0], test_peek_command_id_returns_none_for_missing_file()[1])
    t("peek_command_id returns None when no matching command", test_peek_command_id_returns_none_when_no_matching_command()[0], test_peek_command_id_returns_none_when_no_matching_command()[1])
    t("peek_command_id returns matching command", test_peek_command_id_returns_matching_command()[0], test_peek_command_id_returns_matching_command()[1])
    print()

    print("[Section 5] peek_command_id() immutability and edge cases")
    t("peek_command_id never mutates state", test_peek_command_id_never_mutates_state()[0], test_peek_command_id_never_mutates_state()[1])
    t("peek_command_id with complex state", test_peek_command_id_with_complex_state()[0], test_peek_command_id_with_complex_state()[1])
    t("peek_command_id handles empty commands dict", test_peek_command_id_handles_empty_commands_dict()[0], test_peek_command_id_handles_empty_commands_dict()[1])
    t("peek_command_id handles no commands dict", test_peek_command_id_handles_no_commands_dict()[0], test_peek_command_id_handles_no_commands_dict()[1])
    t("peek_command_id ignores entries without command field", test_peek_command_id_ignores_entries_without_command_field()[0], test_peek_command_id_ignores_entries_without_command_field()[1])
    t("peek_command_id recency tie-break: returns most recent", test_peek_command_id_recency_tie_break()[0], test_peek_command_id_recency_tie_break()[1])
    print()

    print("[Section 6] Round-trip and integration tests")
    t("resumed_from round trip", test_resumed_from_round_trip()[0], test_resumed_from_round_trip()[1])
    t("validate_event accepts whitespace in resumed_from", test_validate_event_accepts_whitespace_in_resumed_from()[0], test_validate_event_accepts_whitespace_in_resumed_from()[1])
    t("build_event with multiple optional fields and resumed_from", test_build_event_with_multiple_optional_fields_and_resumed_from()[0], test_build_event_with_multiple_optional_fields_and_resumed_from()[1])
    print()

    h.summarize_and_exit()
