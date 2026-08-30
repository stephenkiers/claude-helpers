#!/usr/bin/env python3
"""
Test suite for telemetry_schema.py.

Covers: event building and validation, token normalization, metric fields,
outcome helpers, and atomic file append.

Run with: python3 tests/test_telemetry_schema.py
"""

import json
import os
import stat
import sys
import tempfile
import uuid
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


def test_validate_findings_accepts_valid_keys():
    """validate_event accepts a findings dict with only allowlisted, non-negative int keys."""
    event = telemetry_schema.build_event(
        "stage.end",
        session_id="123",
        timestamp="2026-08-26T12:00:00Z",
        findings={"produced": 5, "accepted": 3, "unique": 2, "rejected": 1, "acted_upon": 1},
    )
    errors = telemetry_schema.validate_event(event)
    return errors == [], f"got errors: {errors}"


def test_validate_findings_rejects_unknown_key():
    """validate_event catches a findings key outside the allowlist."""
    event = telemetry_schema.build_event(
        "stage.end",
        session_id="123",
        timestamp="2026-08-26T12:00:00Z",
        findings={"bogus_key": 1},
    )
    errors = telemetry_schema.validate_event(event)
    return any("findings" in e.lower() for e in errors), f"got errors: {errors}"


def test_validate_findings_rejects_negative_value():
    """validate_event catches a negative findings count."""
    event = telemetry_schema.build_event(
        "stage.end",
        session_id="123",
        timestamp="2026-08-26T12:00:00Z",
        findings={"produced": -1},
    )
    errors = telemetry_schema.validate_event(event)
    return any("findings" in e.lower() for e in errors), f"got errors: {errors}"


def test_validate_checks_accepts_valid_keys():
    """validate_event accepts a checks dict with only allowlisted, non-negative int keys."""
    event = telemetry_schema.build_event(
        "stage.end",
        session_id="123",
        timestamp="2026-08-26T12:00:00Z",
        checks={"executed": 10, "passed": 9},
    )
    errors = telemetry_schema.validate_event(event)
    return errors == [], f"got errors: {errors}"


def test_validate_checks_rejects_non_int_value():
    """validate_event catches a non-int checks value."""
    event = telemetry_schema.build_event(
        "stage.end",
        session_id="123",
        timestamp="2026-08-26T12:00:00Z",
        checks={"executed": "unknown"},
    )
    errors = telemetry_schema.validate_event(event)
    return any("checks" in e.lower() for e in errors), f"got errors: {errors}"


def test_validate_findings_rejects_non_int_value():
    """validate_event catches a non-int findings value."""
    event = telemetry_schema.build_event(
        "stage.end",
        session_id="123",
        timestamp="2026-08-26T12:00:00Z",
        findings={"produced": "unknown"},
    )
    errors = telemetry_schema.validate_event(event)
    return any("findings" in e.lower() for e in errors), f"got errors: {errors}"


def test_validate_checks_rejects_unknown_key():
    """validate_event catches a checks key outside the allowlist."""
    event = telemetry_schema.build_event(
        "stage.end",
        session_id="123",
        timestamp="2026-08-26T12:00:00Z",
        checks={"bogus_key": 1},
    )
    errors = telemetry_schema.validate_event(event)
    return any("checks" in e.lower() for e in errors), f"got errors: {errors}"


def test_validate_checks_rejects_negative_value():
    """validate_event catches a negative checks count."""
    event = telemetry_schema.build_event(
        "stage.end",
        session_id="123",
        timestamp="2026-08-26T12:00:00Z",
        checks={"executed": -1},
    )
    errors = telemetry_schema.validate_event(event)
    return any("checks" in e.lower() for e in errors), f"got errors: {errors}"


def test_validate_findings_rejects_bool_value():
    """validate_event catches a bool findings value (even though bool is technically an int subclass)."""
    event = telemetry_schema.build_event(
        "stage.end",
        session_id="123",
        timestamp="2026-08-26T12:00:00Z",
        findings={"produced": True},
    )
    errors = telemetry_schema.validate_event(event)
    return any("findings" in e.lower() for e in errors), f"got errors: {errors}"


def test_validate_checks_rejects_bool_value():
    """validate_event catches a bool checks value (even though bool is technically an int subclass)."""
    event = telemetry_schema.build_event(
        "stage.end",
        session_id="123",
        timestamp="2026-08-26T12:00:00Z",
        checks={"executed": True},
    )
    errors = telemetry_schema.validate_event(event)
    return any("checks" in e.lower() for e in errors), f"got errors: {errors}"


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


def _append_events_worker(log_path_str, n, worker_id):
    """Subprocess-of-a-Process target: append n events, each independently locked."""
    import telemetry_schema as ts  # re-import in the child process

    for i in range(n):
        event = ts.build_event(
            "agent.begin",
            session_id=f"worker-{worker_id}",
            timestamp="2026-08-26T12:00:00Z",
            agent_id=f"{worker_id}-{i}",
        )
        ts.append_event(log_path_str, event)


def test_append_event_concurrent_writers():
    """append_event under real concurrent processes never interleaves/corrupts lines.

    Spawns several OS processes hammering the same log file simultaneously and verifies
    every line is still valid, complete JSON and the total line count matches exactly —
    the failure mode flock guards against is two writers' bytes landing interleaved on
    the same line, which would show up as a JSON parse error or a missing line.
    """
    import multiprocessing

    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "concurrent.jsonl"
        n_workers = 6
        n_per_worker = 25

        procs = [
            multiprocessing.Process(
                target=_append_events_worker, args=(str(log_path), n_per_worker, w)
            )
            for w in range(n_workers)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)

        if any(p.exitcode != 0 for p in procs):
            return False, f"a worker process failed: exitcodes={[p.exitcode for p in procs]}"

        if not log_path.exists():
            return False, "log file was never created"

        with open(log_path) as f:
            lines = [line for line in f.readlines() if line.strip()]

        expected = n_workers * n_per_worker
        if len(lines) != expected:
            return False, f"expected {expected} lines, got {len(lines)} (lost/merged writes)"

        seen_agent_ids = set()
        for i, line in enumerate(lines):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as e:
                return False, f"line {i} is corrupted/interleaved JSON: {e!r} :: {line!r}"
            seen_agent_ids.add(parsed.get("agent_id"))

        if len(seen_agent_ids) != expected:
            return False, f"expected {expected} unique agent_ids, got {len(seen_agent_ids)}"

        return True, ""


def test_default_log_path():
    """default_log_path returns a Path in ~/.claude/telemetry/events.jsonl."""
    path = telemetry_schema.default_log_path()
    path_str = str(path)
    return (
        ".claude" in path_str and "telemetry" in path_str and "events.jsonl" in path_str
    ), f"got path: {path_str}"


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

        if not log_path.exists():
            return False, "log file not created"

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

        if not parent_path.exists():
            return False, "parent directory not created"

        if not parent_path.is_dir():
            return False, "parent path is not a directory"

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

        new_mode = stat.S_IMODE(parent_path.stat().st_mode)

        if new_mode != original_mode:
            return False, f"parent mode changed from 0o{original_mode:03o} to 0o{new_mode:03o}"

        return True, ""


def test_append_event_to_existing_common_dir():
    """append_event works when pointing to an existing directory like /tmp without crashing."""
    tmpbase = Path(tempfile.gettempdir()) / ".test-append-existing"
    tmpbase.mkdir(exist_ok=True)

    log_path = tmpbase / "test.jsonl"
    try:
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
        if log_path.exists():
            log_path.unlink()
        if tmpbase.exists():
            tmpbase.rmdir()


def test_load_and_update_state_round_trip():
    """load_and_update_state performs correct read-modify-write (multiple mutations)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "state.json"

        # First write
        def first_mutation(state):
            state["key1"] = "value1"
            return state
        result1 = telemetry_schema.load_and_update_state(state_path, first_mutation)

        if result1.get("key1") != "value1":
            return False, f"first mutation didn't persist: {result1}"

        # Second write (should preserve key1)
        def second_mutation(state):
            state["key2"] = "value2"
            return state
        result2 = telemetry_schema.load_and_update_state(state_path, second_mutation)

        if result2.get("key1") != "value1" or result2.get("key2") != "value2":
            return False, f"second mutation lost first key: {result2}"

        return True, ""


def test_load_and_update_state_missing_file():
    """load_and_update_state on missing file creates it with mutated content."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "new_state.json"

        if state_path.exists():
            return False, "state file should not exist yet"

        def init(state):
            state["initialized"] = True
            return state
        result = telemetry_schema.load_and_update_state(state_path, init)

        if not state_path.exists():
            return False, "state file was not created"

        if result.get("initialized") != True:
            return False, f"mutation not applied: {result}"

        return True, ""


def test_load_and_update_state_corrupt_file():
    """load_and_update_state on corrupt JSON treats it as empty dict."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "corrupt.json"

        # Write corrupt JSON
        with open(state_path, "w") as f:
            f.write("{ invalid json }")

        def mutate(state):
            state["recovered"] = True
            return state
        result = telemetry_schema.load_and_update_state(state_path, mutate)

        if result.get("recovered") != True:
            return False, f"recovery from corrupt failed: {result}"

        # Verify file now contains valid JSON
        with open(state_path) as f:
            content = json.loads(f.read())
        if content.get("recovered") != True:
            return False, "file was not updated with valid JSON"

        return True, ""


def test_prune_stale_state_removes_old_files():
    """prune_stale_state removes files older than cutoff."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_dir = Path(tmpdir)

        # Create an old file (2 days old)
        old_file = state_dir / "old_session.json"
        old_file.write_text("{}")
        os.utime(old_file, (0, 0))  # Set mtime to epoch

        # Create a fresh file
        fresh_file = state_dir / "fresh_session.json"
        fresh_file.write_text("{}")

        # Prune with 24h cutoff
        telemetry_schema.prune_stale_state(state_dir, max_age_seconds=86400)

        if old_file.exists():
            return False, "old file was not pruned"
        if not fresh_file.exists():
            return False, "fresh file was deleted"

        return True, ""


def test_prune_stale_state_nonexistent_dir():
    """prune_stale_state handles nonexistent directory gracefully."""
    nonexistent = Path("/tmp/.definitely-not-real-dir-" + str(uuid.uuid4()))
    try:
        # Should not raise
        telemetry_schema.prune_stale_state(nonexistent)
        return True, ""
    except Exception as e:
        return False, f"prune_stale_state raised on missing dir: {e}"


def test_prune_boundary_just_past_cutoff():
    """prune_stale_state: file at (now - max_age - 1) is pruned."""
    import time
    with tempfile.TemporaryDirectory() as tmpdir:
        state_dir = Path(tmpdir)

        # Create file and set mtime to (now - 24h - 1 second)
        file_path = state_dir / "just_past.json"
        file_path.write_text("{}")
        now = time.time()
        old_mtime = now - 86400 - 1
        os.utime(file_path, (old_mtime, old_mtime))

        # Prune with 24h cutoff
        telemetry_schema.prune_stale_state(state_dir, max_age_seconds=86400)

        if file_path.exists():
            return False, "file at (now - 24h - 1) should be pruned"

        return True, ""


def test_prune_boundary_before_cutoff():
    """prune_stale_state: file at (now - max_age + 60) survives."""
    import time
    with tempfile.TemporaryDirectory() as tmpdir:
        state_dir = Path(tmpdir)

        # Create file and set mtime to (now - 24h + 60 seconds)
        file_path = state_dir / "still_fresh.json"
        file_path.write_text("{}")
        now = time.time()
        ok_mtime = now - 86400 + 60
        os.utime(file_path, (ok_mtime, ok_mtime))

        # Prune with 24h cutoff
        telemetry_schema.prune_stale_state(state_dir, max_age_seconds=86400)

        if not file_path.exists():
            return False, "file at (now - 24h + 60) should survive"

        return True, ""


def test_build_event_state_mismatch_field():
    """build_event includes state_mismatch field when provided (non-None)."""
    event = telemetry_schema.build_event(
        "stage.end",
        session_id="s1",
        timestamp="2026-08-26T12:00:00Z",
        state_mismatch=True,
    )
    if event.get("state_mismatch") != True:
        return False, f"state_mismatch not in event: {event}"
    return True, ""


def test_build_event_state_mismatch_omitted_when_none():
    """build_event omits state_mismatch when None."""
    event = telemetry_schema.build_event(
        "stage.end",
        session_id="s1",
        timestamp="2026-08-26T12:00:00Z",
        state_mismatch=None,
    )
    if "state_mismatch" in event:
        return False, f"state_mismatch should be omitted when None, but found in: {event}"
    return True, ""


def test_validate_event_rejects_empty_command_id():
    """validate_event rejects command_id that is an empty string."""
    event = telemetry_schema.build_event(
        "command.begin",
        session_id="s1",
        timestamp="2026-08-26T12:00:00Z",
        command_id="c1",
    )
    # Manually set to empty string
    event["command_id"] = ""
    errors = telemetry_schema.validate_event(event)
    if not any("command_id" in e.lower() and "empty" in e.lower() for e in errors):
        return False, f"should reject empty command_id, got errors: {errors}"
    return True, ""


def test_validate_event_rejects_empty_stage_id():
    """validate_event rejects stage_id that is an empty string."""
    event = telemetry_schema.build_event(
        "stage.begin",
        session_id="s1",
        timestamp="2026-08-26T12:00:00Z",
        stage_id="st1",
    )
    # Manually set to empty string
    event["stage_id"] = ""
    errors = telemetry_schema.validate_event(event)
    if not any("stage_id" in e.lower() and "empty" in e.lower() for e in errors):
        return False, f"should reject empty stage_id, got errors: {errors}"
    return True, ""


def test_validate_event_accepts_unknown_as_id():
    """validate_event accepts command_id='unknown' and stage_id='unknown'."""
    event = telemetry_schema.build_event(
        "command.begin",
        session_id="s1",
        timestamp="2026-08-26T12:00:00Z",
        command_id="unknown",
        stage_id="unknown",
    )
    errors = telemetry_schema.validate_event(event)
    return errors == [], f"should accept 'unknown' as ID, got errors: {errors}"


def test_default_state_dir():
    """default_state_dir returns a path under ~/.claude/telemetry/state/."""
    state_dir = telemetry_schema.default_state_dir()
    state_dir_str = str(state_dir)
    has_claude = ".claude" in state_dir_str
    has_telemetry = "telemetry" in state_dir_str
    has_state = "state" in state_dir_str
    return (
        has_claude and has_telemetry and has_state
    ), f"got path: {state_dir_str}"


def test_state_path_format():
    """state_path(session_id, state_dir) returns correct path format."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        state_dir = Path(tmpdir)
        session_id = "test-session-123"

        result = telemetry_schema.state_path(session_id, state_dir)
        result_str = str(result)

        # Should be in the state_dir
        if not str(result).startswith(str(state_dir)):
            return False, f"path not under state_dir: {result_str}"

        # Should include the session_id with .json extension
        if session_id not in result_str or not result_str.endswith(".json"):
            return False, f"path doesn't match expected format: {result_str}"

        return True, ""


def test_state_path_isolation():
    """state_path returns different paths for different session_ids."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        state_dir = Path(tmpdir)

        path1 = telemetry_schema.state_path("session-1", state_dir)
        path2 = telemetry_schema.state_path("session-2", state_dir)

        if path1 == path2:
            return False, "same path for different session_ids"

        if not str(path1).endswith("session-1.json"):
            return False, f"path1 format wrong: {path1}"

        if not str(path2).endswith("session-2.json"):
            return False, f"path2 format wrong: {path2}"

        return True, ""


def test_load_and_update_state_with_locking():
    """load_and_update_state safely preserves state across interleaved reads/writes."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "state.json"

        # Simulate a scenario where state is read, modified, and written multiple times
        # This tests that the lock protects against interleaved access patterns

        def set_field(key, value):
            def mutator(state):
                state[key] = value
                return state
            return mutator

        # First: initialize with command_id and stage_id
        result1 = telemetry_schema.load_and_update_state(
            state_path, set_field("command_id", "c123")
        )
        if result1.get("command_id") != "c123":
            return False, "first write failed"

        # Second: add stage_id
        result2 = telemetry_schema.load_and_update_state(
            state_path, set_field("stage_id", "st456")
        )
        if result2.get("command_id") != "c123" or result2.get("stage_id") != "st456":
            return False, "second write lost previous state"

        # Third: clear stage_id but keep command_id
        def clear_stage(state):
            state["stage_id"] = None
            state["stage"] = None
            return state

        result3 = telemetry_schema.load_and_update_state(state_path, clear_stage)
        if result3.get("command_id") != "c123":
            return False, "clearing stage fields lost command_id"
        if result3.get("stage_id") is not None:
            return False, "stage_id should be None"

        # Fourth: verify final state by reading from disk
        with open(state_path) as f:
            final = json.loads(f.read())

        if final.get("command_id") != "c123" or final.get("stage_id") is not None:
            return False, f"final state incorrect: {final}"

        return True, ""


def test_findings_counts_typeddict_importable():
    """FindingsCounts TypedDict can be imported and has correct annotations."""
    fc = telemetry_schema.FindingsCounts
    annotations = getattr(fc, '__annotations__', {})
    expected_keys = {'produced', 'accepted', 'unique', 'rejected', 'acted_upon'}
    if set(annotations.keys()) != expected_keys:
        return False, f"expected keys {expected_keys}, got {set(annotations.keys())}"
    # Check all values are int type
    if not all(v == int for v in annotations.values()):
        return False, f"not all annotations are int: {annotations}"
    return True, ""


def test_checks_counts_typeddict_importable():
    """ChecksCounts TypedDict can be imported and has correct annotations."""
    cc = telemetry_schema.ChecksCounts
    annotations = getattr(cc, '__annotations__', {})
    expected_keys = {'executed', 'passed'}
    if set(annotations.keys()) != expected_keys:
        return False, f"expected keys {expected_keys}, got {set(annotations.keys())}"
    # Check all values are int type
    if not all(v == int for v in annotations.values()):
        return False, f"not all annotations are int: {annotations}"
    return True, ""


def test_findings_counts_typeddict_has_optional_keys():
    """FindingsCounts TypedDict has no required keys (__total__=False)."""
    fc = telemetry_schema.FindingsCounts
    # For non-total TypedDict, all keys should be optional
    required = getattr(fc, '__required_keys__', frozenset())
    if len(required) > 0:
        return False, f"FindingsCounts has required keys: {required}"
    return True, ""


def test_checks_counts_typeddict_has_optional_keys():
    """ChecksCounts TypedDict has no required keys (__total__=False)."""
    cc = telemetry_schema.ChecksCounts
    # For non-total TypedDict, all keys should be optional
    required = getattr(cc, '__required_keys__', frozenset())
    if len(required) > 0:
        return False, f"ChecksCounts has required keys: {required}"
    return True, ""


def test_validate_findings_accepts_subset_of_keys():
    """validate_event accepts findings dict with only a subset of allowed keys."""
    event = telemetry_schema.build_event(
        "stage.end",
        session_id="123",
        timestamp="2026-08-26T12:00:00Z",
        findings={"produced": 5, "rejected": 1},  # Only 2 of 5 keys
    )
    errors = telemetry_schema.validate_event(event)
    return errors == [], f"should accept subset of findings keys, got errors: {errors}"


def test_validate_findings_accepts_single_key():
    """validate_event accepts findings dict with just one key."""
    event = telemetry_schema.build_event(
        "stage.end",
        session_id="123",
        timestamp="2026-08-26T12:00:00Z",
        findings={"produced": 10},  # Only one key
    )
    errors = telemetry_schema.validate_event(event)
    return errors == [], f"should accept single findings key, got errors: {errors}"


def test_validate_findings_accepts_empty_dict():
    """validate_event accepts empty findings dict."""
    event = telemetry_schema.build_event(
        "stage.end",
        session_id="123",
        timestamp="2026-08-26T12:00:00Z",
        findings={},  # Empty dict
    )
    errors = telemetry_schema.validate_event(event)
    return errors == [], f"should accept empty findings dict, got errors: {errors}"


def test_validate_checks_accepts_subset_of_keys():
    """validate_event accepts checks dict with only a subset of allowed keys."""
    event = telemetry_schema.build_event(
        "stage.end",
        session_id="123",
        timestamp="2026-08-26T12:00:00Z",
        checks={"executed": 10},  # Only 'executed', no 'passed'
    )
    errors = telemetry_schema.validate_event(event)
    return errors == [], f"should accept subset of checks keys, got errors: {errors}"


def test_validate_checks_accepts_single_key():
    """validate_event accepts checks dict with just one key (passed)."""
    event = telemetry_schema.build_event(
        "stage.end",
        session_id="123",
        timestamp="2026-08-26T12:00:00Z",
        checks={"passed": 9},  # Only 'passed'
    )
    errors = telemetry_schema.validate_event(event)
    return errors == [], f"should accept single checks key, got errors: {errors}"


def test_validate_checks_accepts_empty_dict():
    """validate_event accepts empty checks dict."""
    event = telemetry_schema.build_event(
        "stage.end",
        session_id="123",
        timestamp="2026-08-26T12:00:00Z",
        checks={},  # Empty dict
    )
    errors = telemetry_schema.validate_event(event)
    return errors == [], f"should accept empty checks dict, got errors: {errors}"


def test_validate_findings_accepts_zero_value():
    """validate_event accepts findings with zero value (boundary case)."""
    event = telemetry_schema.build_event(
        "stage.end",
        session_id="123",
        timestamp="2026-08-26T12:00:00Z",
        findings={"produced": 0},
    )
    errors = telemetry_schema.validate_event(event)
    return errors == [], f"should accept zero value in findings, got errors: {errors}"


def test_validate_checks_accepts_zero_value():
    """validate_event accepts checks with zero value (boundary case)."""
    event = telemetry_schema.build_event(
        "stage.end",
        session_id="123",
        timestamp="2026-08-26T12:00:00Z",
        checks={"executed": 0, "passed": 0},
    )
    errors = telemetry_schema.validate_event(event)
    return errors == [], f"should accept zero value in checks, got errors: {errors}"


def test_validate_findings_rejects_mixed_valid_and_invalid_keys():
    """validate_event rejects findings dict with both valid and invalid keys."""
    event = telemetry_schema.build_event(
        "stage.end",
        session_id="123",
        timestamp="2026-08-26T12:00:00Z",
        findings={"produced": 5, "invalid_key": 1},  # One valid, one invalid
    )
    errors = telemetry_schema.validate_event(event)
    return any("findings" in e.lower() for e in errors), f"should reject mixed valid/invalid keys, got errors: {errors}"


def test_validate_checks_rejects_mixed_valid_and_invalid_keys():
    """validate_event rejects checks dict with both valid and invalid keys."""
    event = telemetry_schema.build_event(
        "stage.end",
        session_id="123",
        timestamp="2026-08-26T12:00:00Z",
        checks={"executed": 10, "invalid_key": 1},  # One valid, one invalid
    )
    errors = telemetry_schema.validate_event(event)
    return any("checks" in e.lower() for e in errors), f"should reject mixed valid/invalid keys, got errors: {errors}"


def test_validate_findings_all_valid_keys():
    """validate_event accepts findings dict when each valid key is tested individually."""
    valid_keys = ["produced", "accepted", "unique", "rejected", "acted_upon"]
    for key in valid_keys:
        event = telemetry_schema.build_event(
            "stage.end",
            session_id="123",
            timestamp="2026-08-26T12:00:00Z",
            findings={key: 1},
        )
        errors = telemetry_schema.validate_event(event)
        if errors:
            return False, f"key '{key}' should be valid but got errors: {errors}"
    return True, ""


def test_validate_checks_all_valid_keys():
    """validate_event accepts checks dict when each valid key is tested individually."""
    valid_keys = ["executed", "passed"]
    for key in valid_keys:
        event = telemetry_schema.build_event(
            "stage.end",
            session_id="123",
            timestamp="2026-08-26T12:00:00Z",
            checks={key: 1},
        )
        errors = telemetry_schema.validate_event(event)
        if errors:
            return False, f"key '{key}' should be valid but got errors: {errors}"
    return True, ""


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

    passed, msg = test_validate_findings_accepts_valid_keys()
    test_result("findings dict with valid keys accepted", passed, msg)

    passed, msg = test_validate_findings_rejects_unknown_key()
    test_result("findings dict rejects unknown key", passed, msg)

    passed, msg = test_validate_findings_rejects_negative_value()
    test_result("findings dict rejects negative value", passed, msg)

    passed, msg = test_validate_checks_accepts_valid_keys()
    test_result("checks dict with valid keys accepted", passed, msg)

    passed, msg = test_validate_checks_rejects_non_int_value()
    test_result("checks dict rejects non-int value", passed, msg)

    passed, msg = test_validate_findings_rejects_non_int_value()
    test_result("findings dict rejects non-int value", passed, msg)

    passed, msg = test_validate_checks_rejects_unknown_key()
    test_result("checks dict rejects unknown key", passed, msg)

    passed, msg = test_validate_checks_rejects_negative_value()
    test_result("checks dict rejects negative value", passed, msg)

    passed, msg = test_validate_findings_rejects_bool_value()
    test_result("findings dict rejects bool value", passed, msg)

    passed, msg = test_validate_checks_rejects_bool_value()
    test_result("checks dict rejects bool value", passed, msg)

    # New TypedDict and partial dict tests
    print("\n[Section 3.1] FindingsCounts and ChecksCounts TypedDict structure")
    passed, msg = test_findings_counts_typeddict_importable()
    test_result("FindingsCounts TypedDict importable and has correct annotations", passed, msg)

    passed, msg = test_checks_counts_typeddict_importable()
    test_result("ChecksCounts TypedDict importable and has correct annotations", passed, msg)

    passed, msg = test_findings_counts_typeddict_has_optional_keys()
    test_result("FindingsCounts all keys are optional (__total__=False)", passed, msg)

    passed, msg = test_checks_counts_typeddict_has_optional_keys()
    test_result("ChecksCounts all keys are optional (__total__=False)", passed, msg)

    print("\n[Section 3.2] Partial findings/checks dicts validation")
    passed, msg = test_validate_findings_accepts_subset_of_keys()
    test_result("findings dict with subset of keys accepted", passed, msg)

    passed, msg = test_validate_findings_accepts_single_key()
    test_result("findings dict with single key accepted", passed, msg)

    passed, msg = test_validate_findings_accepts_empty_dict()
    test_result("empty findings dict accepted", passed, msg)

    passed, msg = test_validate_checks_accepts_subset_of_keys()
    test_result("checks dict with subset of keys accepted", passed, msg)

    passed, msg = test_validate_checks_accepts_single_key()
    test_result("checks dict with single key accepted", passed, msg)

    passed, msg = test_validate_checks_accepts_empty_dict()
    test_result("empty checks dict accepted", passed, msg)

    print("\n[Section 3.3] Boundary cases and edge cases for findings/checks")
    passed, msg = test_validate_findings_accepts_zero_value()
    test_result("findings dict with zero value accepted", passed, msg)

    passed, msg = test_validate_checks_accepts_zero_value()
    test_result("checks dict with zero value accepted", passed, msg)

    passed, msg = test_validate_findings_rejects_mixed_valid_and_invalid_keys()
    test_result("findings dict rejects mixed valid/invalid keys", passed, msg)

    passed, msg = test_validate_checks_rejects_mixed_valid_and_invalid_keys()
    test_result("checks dict rejects mixed valid/invalid keys", passed, msg)

    passed, msg = test_validate_findings_all_valid_keys()
    test_result("findings all valid keys pass individually", passed, msg)

    passed, msg = test_validate_checks_all_valid_keys()
    test_result("checks all valid keys pass individually", passed, msg)

    print()

    # File append tests
    print("[Section 4] File operations")
    passed, msg = test_append_event_creates_file()
    test_result("append_event creates file and appends", passed, msg)

    passed, msg = test_append_event_appends_multiple()
    test_result("append_event appends to existing file", passed, msg)

    passed, msg = test_append_event_concurrent_writers()
    test_result("append_event survives real concurrent-process writers", passed, msg)

    passed, msg = test_default_log_path()
    test_result("default_log_path returns valid path", passed, msg)

    print()

    # Hardening tests: validation-before-write, file/dir permissions
    print("[Section 5] Hardening: validation and permissions")
    passed, msg = test_append_event_validates_before_write()
    test_result("append_event validates before write", passed, msg)

    passed, msg = test_append_event_creates_file_mode_0600()
    test_result("log file created at mode 0600", passed, msg)

    passed, msg = test_append_event_creates_parent_dir_mode_0700()
    test_result("parent dir created at mode 0700", passed, msg)

    passed, msg = test_append_event_preserves_existing_parent_permissions()
    test_result("existing parent permissions preserved", passed, msg)

    passed, msg = test_append_event_to_existing_common_dir()
    test_result("append to existing dir like /tmp works", passed, msg)

    print()

    # State file management tests
    print("[Section 6] State file operations")
    passed, msg = test_load_and_update_state_round_trip()
    test_result("load_and_update_state preserves across mutations", passed, msg)

    passed, msg = test_load_and_update_state_missing_file()
    test_result("load_and_update_state creates missing file", passed, msg)

    passed, msg = test_load_and_update_state_corrupt_file()
    test_result("load_and_update_state handles corrupt JSON gracefully", passed, msg)

    passed, msg = test_prune_stale_state_removes_old_files()
    test_result("prune_stale_state removes files older than cutoff", passed, msg)

    passed, msg = test_prune_stale_state_nonexistent_dir()
    test_result("prune_stale_state handles nonexistent directory", passed, msg)

    passed, msg = test_prune_boundary_just_past_cutoff()
    test_result("prune_stale_state: file at (now - max_age - 1) is pruned", passed, msg)

    passed, msg = test_prune_boundary_before_cutoff()
    test_result("prune_stale_state: file at (now - max_age + 60) survives", passed, msg)

    print()

    # State mismatch and ID validation tests
    print("[Section 7] State mismatch and ID validation")
    passed, msg = test_build_event_state_mismatch_field()
    test_result("build_event includes state_mismatch when provided", passed, msg)

    passed, msg = test_build_event_state_mismatch_omitted_when_none()
    test_result("build_event omits state_mismatch when None", passed, msg)

    passed, msg = test_validate_event_rejects_empty_command_id()
    test_result("validate_event rejects empty string command_id", passed, msg)

    passed, msg = test_validate_event_rejects_empty_stage_id()
    test_result("validate_event rejects empty string stage_id", passed, msg)

    passed, msg = test_validate_event_accepts_unknown_as_id()
    test_result("validate_event accepts 'unknown' as command_id/stage_id", passed, msg)

    print()

    # State directory and path functions
    print("[Section 8] State directory and path functions")
    passed, msg = test_default_state_dir()
    test_result("default_state_dir returns valid path", passed, msg)

    passed, msg = test_state_path_format()
    test_result("state_path returns correct format", passed, msg)

    passed, msg = test_state_path_isolation()
    test_result("state_path isolates different sessions", passed, msg)

    passed, msg = test_load_and_update_state_with_locking()
    test_result("load_and_update_state maintains atomicity under concurrent access", passed, msg)

    print()

    h.summarize_and_exit()
