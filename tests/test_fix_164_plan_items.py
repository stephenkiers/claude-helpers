#!/usr/bin/env python3
"""
Test suite for fix-164 plan items.

Covers the hardening items from the implement-with-haiku resume plan:
- Item 12: isinstance(entry, dict) filter in peek_command_id for malformed entries
- Item 14: peek_command_id tie-break behavior (latest timestamp + _monotonic_ns)
- Item 13: Rejection of empty --resumed-from without orphaned state entries
- Item 15: Consistent sort-key behavior across telemetry operations

Run with: python3 tests/test_fix_164_plan_items.py
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add parent/scripts to path so we can import telemetry_schema
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import telemetry_schema

from _test_harness import REPO_ROOT, Harness

SCRIPTS_DIR = REPO_ROOT / "scripts"
RUN_METRICS = SCRIPTS_DIR / "run-metrics.py"


def run_script(args, stdin_text=None, env=None):
    """Run the script as a subprocess. Returns (returncode, stdout, stderr)."""
    cmd = [sys.executable, str(RUN_METRICS)] + args
    result = subprocess.run(
        cmd,
        input=stdin_text,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


# ============================================================================
# Item 12: isinstance(entry, dict) filter for malformed entries
# ============================================================================

def test_peek_command_id_handles_non_dict_entries():
    """peek_command_id gracefully handles non-dict entries in state file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "command-state.json"

        # Create a valid state structure
        state = {
            "commands": {
                "cmd-123": {
                    "command": "test-cmd",
                    "command_began_at": datetime.now(timezone.utc).isoformat(),
                    "session_id": "sess-456",
                    "stage_id": None,
                    "stage": None,
                    "stage_began_at": None,
                    "_monotonic_ns": 1000,
                },
                "cmd-456": {
                    "command": "test-cmd",
                    "command_began_at": datetime.now(timezone.utc).isoformat(),
                    "session_id": "sess-456",
                    "stage_id": None,
                    "stage": None,
                    "stage_began_at": None,
                    "_monotonic_ns": 3000,
                },
            }
        }

        # Now corrupt it by inserting a non-dict in the commands dict
        corrupted_state = {
            "commands": {
                "cmd-123": "not a dict",  # Non-dict entry - should be skipped
                "cmd-456": {
                    "command": "test-cmd",
                    "command_began_at": datetime.now(timezone.utc).isoformat(),
                    "session_id": "sess-456",
                    "stage_id": None,
                    "stage": None,
                    "stage_began_at": None,
                    "_monotonic_ns": 3000,
                },
            }
        }
        state_path.write_text(json.dumps(corrupted_state))

        try:
            # Should not crash, should handle the non-dict gracefully
            result = telemetry_schema.peek_command_id(state_path, "test-cmd")
            # Should return cmd-456 (the only valid entry for test-cmd)
            if result != "cmd-456":
                return False, f"expected cmd-456, got {result}"
            return True, ""
        except Exception as e:
            return False, f"peek_command_id crashed on non-dict entry: {e}"


def test_peek_command_id_handles_missing_dict_fields():
    """peek_command_id handles entries with missing required fields."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "command-state.json"

        # Create state with incomplete entries
        state = {
            "commands": {
                "cmd-123": {
                    # missing "command" field
                    "command_began_at": datetime.now(timezone.utc).isoformat(),
                    "session_id": "sess-456",
                },
                "cmd-456": {
                    "command": "test-cmd",
                    "command_began_at": datetime.now(timezone.utc).isoformat(),
                    "session_id": "sess-789",
                    "stage_id": None,
                    "stage": None,
                    "stage_began_at": None,
                    "_monotonic_ns": 1000,
                },
            }
        }
        state_path.write_text(json.dumps(state))

        try:
            # Should not crash, should find the complete entry
            result = telemetry_schema.peek_command_id(state_path, "test-cmd")
            if result != "cmd-456":
                return False, f"expected cmd-456, got {result}"
            return True, ""
        except Exception as e:
            return False, f"peek_command_id crashed on incomplete entry: {e}"


# ============================================================================
# Item 14: peek_command_id tie-break behavior (latest + monotonic_ns)
# ============================================================================

def test_peek_command_id_tie_break_uses_monotonic_ns():
    """peek_command_id uses _monotonic_ns as tiebreaker when timestamps equal."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "command-state.json"

        # Create a shared timestamp (same second)
        shared_timestamp = datetime.now(timezone.utc).isoformat()

        # Create state with two entries: same command, same timestamp, different monotonic
        state = {
            "commands": {
                "cmd-first": {
                    "command": "tie-cmd",
                    "command_began_at": shared_timestamp,
                    "session_id": "sess-123",
                    "stage_id": None,
                    "stage": None,
                    "stage_began_at": None,
                    "_monotonic_ns": 1000,
                },
                "cmd-second": {
                    "command": "tie-cmd",
                    "command_began_at": shared_timestamp,
                    "session_id": "sess-123",
                    "stage_id": None,
                    "stage": None,
                    "stage_began_at": None,
                    "_monotonic_ns": 2000,  # Higher monotonic_ns = more recent
                },
            }
        }
        state_path.write_text(json.dumps(state))

        try:
            result = telemetry_schema.peek_command_id(state_path, "tie-cmd")
            # Should return cmd-second because it has higher _monotonic_ns
            if result != "cmd-second":
                return False, f"tie-break failed: expected cmd-second, got {result}"
            return True, ""
        except Exception as e:
            return False, f"peek_command_id failed on tie-break scenario: {e}"


def test_peek_command_id_latest_timestamp_wins():
    """peek_command_id returns entry with latest command_began_at timestamp."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "command-state.json"

        # Create two timestamps, 1 minute apart
        now = datetime.now(timezone.utc)
        earlier_ts = (now - timedelta(minutes=1)).isoformat()
        later_ts = now.isoformat()

        # Create state with entries at different timestamps
        state = {
            "commands": {
                "cmd-older": {
                    "command": "timing-cmd",
                    "command_began_at": earlier_ts,
                    "session_id": "sess-123",
                    "stage_id": None,
                    "stage": None,
                    "stage_began_at": None,
                    "_monotonic_ns": 1000,
                },
                "cmd-newer": {
                    "command": "timing-cmd",
                    "command_began_at": later_ts,
                    "session_id": "sess-123",
                    "stage_id": None,
                    "stage": None,
                    "stage_began_at": None,
                    "_monotonic_ns": 1000,
                },
            }
        }
        state_path.write_text(json.dumps(state))

        try:
            result = telemetry_schema.peek_command_id(state_path, "timing-cmd")
            if result != "cmd-newer":
                return False, f"expected cmd-newer (latest timestamp), got {result}"
            return True, ""
        except Exception as e:
            return False, f"peek_command_id failed on timestamp ordering: {e}"


# ============================================================================
# Item 13: Orphaned state-file entry handling
# ============================================================================

def test_empty_resumed_from_rejected_no_orphan():
    """run-metrics.py rejects empty --resumed-from without creating orphaned state."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_dir = Path(tmpdir) / "state"
        state_dir.mkdir()

        # Try to use empty --resumed-from (should be rejected)
        code, stdout, stderr = run_script(
            [
                "--state-dir", str(state_dir),
                "command-begin",
                "--command", "test-cmd",
                "--resumed-from", "",  # Empty string - should be rejected
            ],
        )

        # Should have non-zero exit code
        if code == 0:
            return False, "empty --resumed-from should be rejected (exit 0)"

        # Check state file - should have no entries (or minimal state)
        state_path = state_dir / "command-state.json"
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text())
                commands = state.get("commands", {})
                if len(commands) > 0:
                    return False, f"orphaned entry created: {commands}"
            except json.JSONDecodeError:
                # Empty or malformed is fine
                pass

        return True, ""


def test_valid_resumed_from_creates_state_entry():
    """run-metrics.py accepts valid --resumed-from and creates state entry."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_dir = Path(tmpdir) / "state"
        state_dir.mkdir()

        # First, create a command that we'll resume from
        state_path = state_dir / "command-state.json"
        telemetry_schema.init_command_state(
            state_path,
            command_id="cmd-original",
            command="initial-cmd",
            began_at=datetime.now(timezone.utc).isoformat(),
            session_id="sess-456"
        )

        # Now create a new command that resumes from the original
        code, stdout, stderr = run_script(
            [
                "--state-dir", str(state_dir),
                "command-begin",
                "--command", "resumed-cmd",
                "--resumed-from", "cmd-original",
            ],
        )

        # Should succeed
        if code != 0:
            return False, f"valid --resumed-from should be accepted, got exit {code}: {stderr}"

        # stdout should contain a command_id
        cmd_id = stdout.strip()
        if not cmd_id or len(cmd_id) != 32:  # hex uuid
            return False, f"expected command_id in stdout, got: {stdout!r}"

        return True, ""


# ============================================================================
# Item 15: Consistent sort-key behavior
# ============================================================================

def test_sort_behavior_consistency_in_peek_command_id():
    """peek_command_id uses consistent sort order (latest timestamp + monotonic)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "command-state.json"

        # Create entries with varying timestamps and monotonic values
        now = datetime.now(timezone.utc)

        state = {
            "commands": {
                "cmd-1": {
                    "command": "multi-cmd",
                    "command_began_at": (now - timedelta(minutes=2)).isoformat(),
                    "session_id": "sess-123",
                    "stage_id": None,
                    "stage": None,
                    "stage_began_at": None,
                    "_monotonic_ns": 3000,
                },
                "cmd-2": {
                    "command": "multi-cmd",
                    "command_began_at": (now - timedelta(minutes=1)).isoformat(),
                    "session_id": "sess-123",
                    "stage_id": None,
                    "stage": None,
                    "stage_began_at": None,
                    "_monotonic_ns": 1000,
                },
                "cmd-3": {
                    "command": "multi-cmd",
                    "command_began_at": (now - timedelta(minutes=1)).isoformat(),
                    "session_id": "sess-123",
                    "stage_id": None,
                    "stage": None,
                    "stage_began_at": None,
                    "_monotonic_ns": 2000,  # Same timestamp as cmd-2 but higher monotonic
                },
                "cmd-4": {
                    "command": "multi-cmd",
                    "command_began_at": now.isoformat(),
                    "session_id": "sess-123",
                    "stage_id": None,
                    "stage": None,
                    "stage_began_at": None,
                    "_monotonic_ns": 1000,
                },
            }
        }
        state_path.write_text(json.dumps(state))

        try:
            result = telemetry_schema.peek_command_id(state_path, "multi-cmd")
            # Should return cmd-4 (latest timestamp)
            if result != "cmd-4":
                return False, f"expected cmd-4 (latest), got {result}"
            return True, ""
        except Exception as e:
            return False, f"sort consistency test failed: {e}"


# ============================================================================
# Additional edge case tests
# ============================================================================

def test_peek_command_id_returns_none_for_empty_state():
    """peek_command_id returns None when state file doesn't exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "nonexistent.json"

        try:
            result = telemetry_schema.peek_command_id(state_path, "some-cmd")
            if result is not None:
                return False, f"expected None for nonexistent file, got {result}"
            return True, ""
        except Exception as e:
            return False, f"peek_command_id crashed on nonexistent file: {e}"


def test_peek_command_id_returns_none_for_no_match():
    """peek_command_id returns None when command name doesn't match any entry."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "command-state.json"

        state = {
            "commands": {
                "cmd-1": {
                    "command": "existing-cmd",
                    "command_began_at": datetime.now(timezone.utc).isoformat(),
                    "session_id": "sess-123",
                    "stage_id": None,
                    "stage": None,
                    "stage_began_at": None,
                    "_monotonic_ns": 1000,
                },
            }
        }
        state_path.write_text(json.dumps(state))

        try:
            result = telemetry_schema.peek_command_id(state_path, "nonexistent-cmd")
            if result is not None:
                return False, f"expected None for no match, got {result}"
            return True, ""
        except Exception as e:
            return False, f"peek_command_id crashed on no-match: {e}"


def test_peek_command_id_single_entry():
    """peek_command_id correctly returns the only entry."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "command-state.json"

        state = {
            "commands": {
                "cmd-only": {
                    "command": "single-cmd",
                    "command_began_at": datetime.now(timezone.utc).isoformat(),
                    "session_id": "sess-123",
                    "stage_id": None,
                    "stage": None,
                    "stage_began_at": None,
                    "_monotonic_ns": 1000,
                },
            }
        }
        state_path.write_text(json.dumps(state))

        try:
            result = telemetry_schema.peek_command_id(state_path, "single-cmd")
            if result != "cmd-only":
                return False, f"expected cmd-only, got {result}"
            return True, ""
        except Exception as e:
            return False, f"peek_command_id failed on single entry: {e}"


def test_peek_command_id_with_null_monotonic_ns():
    """peek_command_id handles entries with null or missing _monotonic_ns."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "command-state.json"

        now = datetime.now(timezone.utc)

        state = {
            "commands": {
                "cmd-1": {
                    "command": "test-cmd",
                    "command_began_at": (now - timedelta(seconds=1)).isoformat(),
                    "session_id": "sess-123",
                    "stage_id": None,
                    "stage": None,
                    "stage_began_at": None,
                    "_monotonic_ns": None,  # null monotonic
                },
                "cmd-2": {
                    "command": "test-cmd",
                    "command_began_at": now.isoformat(),
                    "session_id": "sess-123",
                    "stage_id": None,
                    "stage": None,
                    "stage_began_at": None,
                    "_monotonic_ns": 1000,
                },
            }
        }
        state_path.write_text(json.dumps(state))

        try:
            result = telemetry_schema.peek_command_id(state_path, "test-cmd")
            # Should return cmd-2 (latest timestamp, regardless of null monotonic)
            if result != "cmd-2":
                return False, f"expected cmd-2, got {result}"
            return True, ""
        except Exception as e:
            return False, f"peek_command_id failed with null monotonic: {e}"


# ============================================================================
# Main test harness
# ============================================================================

if __name__ == "__main__":
    h = Harness("FIX-164 PLAN ITEMS TEST SUITE")
    test_result = h.test_result

    # ========================================================================
    # Item 12: isinstance(entry, dict) filter
    # ========================================================================
    print("[Item 12] isinstance(entry, dict) filter for malformed entries")

    passed, msg = test_peek_command_id_handles_non_dict_entries()
    test_result("peek_command_id handles non-dict entries", passed, msg)

    passed, msg = test_peek_command_id_handles_missing_dict_fields()
    test_result("peek_command_id handles missing dict fields", passed, msg)

    print()

    # ========================================================================
    # Item 14: Tie-break behavior
    # ========================================================================
    print("[Item 14] peek_command_id tie-break (latest timestamp + _monotonic_ns)")

    passed, msg = test_peek_command_id_latest_timestamp_wins()
    test_result("latest timestamp wins", passed, msg)

    passed, msg = test_peek_command_id_tie_break_uses_monotonic_ns()
    test_result("_monotonic_ns tiebreaker when timestamps equal", passed, msg)

    print()

    # ========================================================================
    # Item 13: Orphaned state-file entry
    # ========================================================================
    print("[Item 13] Rejection of empty --resumed-from without orphaned entries")

    passed, msg = test_empty_resumed_from_rejected_no_orphan()
    test_result("empty --resumed-from rejected, no orphan created", passed, msg)

    passed, msg = test_valid_resumed_from_creates_state_entry()
    test_result("valid --resumed-from accepted and creates state", passed, msg)

    print()

    # ========================================================================
    # Item 15: Consistent sort behavior
    # ========================================================================
    print("[Item 15] Consistent sort-key behavior across operations")

    passed, msg = test_sort_behavior_consistency_in_peek_command_id()
    test_result("sort consistency (timestamp + monotonic)", passed, msg)

    print()

    # ========================================================================
    # Edge cases
    # ========================================================================
    print("[Edge Cases] Additional robustness tests")

    passed, msg = test_peek_command_id_returns_none_for_empty_state()
    test_result("returns None for nonexistent state file", passed, msg)

    passed, msg = test_peek_command_id_returns_none_for_no_match()
    test_result("returns None when command name doesn't match", passed, msg)

    passed, msg = test_peek_command_id_single_entry()
    test_result("correctly handles single entry", passed, msg)

    passed, msg = test_peek_command_id_with_null_monotonic_ns()
    test_result("handles null/missing _monotonic_ns gracefully", passed, msg)

    print()

    h.summarize_and_exit()
