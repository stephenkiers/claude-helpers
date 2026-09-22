#!/usr/bin/env python3
"""
Test suite for scripts/save-usage.py.

Covers:
- detect_session_commands() function: collecting every command run in the session,
  in chronological order, with model/effort overrides when recorded
- detect_worktree() function: extracting git worktree basename
- parse_duration() 'd' (days) unit support
- Fallback label generation when no explicit label provided
- Explicit label handling (never modified)
- worktree/session_id/commands fields in written records
- Error handling and edge cases

Run with: python3 tests/test_save_usage.py
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from _test_harness import REPO_ROOT, Harness

SCRIPT = REPO_ROOT / "scripts" / "save-usage.py"


def _load_save_usage_module():
    """Load save-usage.py as a module (handles dash in filename)."""
    spec = importlib.util.spec_from_file_location("save_usage", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["save_usage_temp"] = module
    spec.loader.exec_module(module)
    return module


def test_parse_duration_handles_days():
    """parse_duration parses a 'd' (days) unit alongside h/m/s."""
    with tempfile.TemporaryDirectory() as tmpdir:
        original_cwd = os.getcwd()
        try:
            os.chdir(tmpdir)
            save_usage = _load_save_usage_module()
            result = save_usage.parse_duration("1d 2h 3m 4s")
            expected = 86400 + 2 * 3600 + 3 * 60 + 4
            return (
                result == expected
            ), f"Expected {expected}, got {result!r}"
        finally:
            os.chdir(original_cwd)


def test_parse_duration_days_only():
    """parse_duration handles a bare days value with no smaller units."""
    with tempfile.TemporaryDirectory() as tmpdir:
        original_cwd = os.getcwd()
        try:
            os.chdir(tmpdir)
            save_usage = _load_save_usage_module()
            result = save_usage.parse_duration("2d")
            return (
                result == 172800
            ), f"Expected 172800, got {result!r}"
        finally:
            os.chdir(original_cwd)


def test_detect_session_commands_returns_chronological_list():
    """detect_session_commands returns all matching commands in timestamp order."""
    with tempfile.TemporaryDirectory() as tmpdir:
        events_dir = Path(tmpdir) / ".claude" / "telemetry"
        events_dir.mkdir(parents=True)
        events_log = events_dir / "events.jsonl"

        session_id = "test-session-" + uuid.uuid4().hex[:8]

        # Write three command.begin events: two for our session (out of order in
        # the file), one for another session that must be excluded.
        events = [
            {
                "session_id": session_id,
                "event_type": "command.begin",
                "command": "expert-plan",
                "timestamp": "2026-01-01T10:00:10Z",
            },
            {
                "session_id": "other-session",
                "event_type": "command.begin",
                "command": "save-usage",
                "timestamp": "2026-01-01T10:00:05Z",
            },
            {
                "session_id": session_id,
                "event_type": "command.begin",
                "command": "expert-review",
                "timestamp": "2026-01-01T10:00:00Z",
            },
        ]

        with open(events_log, "w") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")

        original_home = os.environ.get("HOME")
        try:
            os.environ["HOME"] = tmpdir
            save_usage = _load_save_usage_module()
            result = save_usage.detect_session_commands(session_id)
            expected = [{"command": "expert-review"}, {"command": "expert-plan"}]
            return (
                result == expected
            ), f"Expected {expected}, got {result!r}"
        finally:
            if original_home:
                os.environ["HOME"] = original_home
            elif "HOME" in os.environ:
                del os.environ["HOME"]


def test_detect_session_commands_includes_model_and_effort():
    """detect_session_commands includes model/effort when the event recorded them."""
    with tempfile.TemporaryDirectory() as tmpdir:
        events_dir = Path(tmpdir) / ".claude" / "telemetry"
        events_dir.mkdir(parents=True)
        events_log = events_dir / "events.jsonl"

        session_id = "test-session-" + uuid.uuid4().hex[:8]

        events = [
            {
                "session_id": session_id,
                "event_type": "command.begin",
                "command": "expert-review",
                "model": "opus",
                "effort": "4",
                "timestamp": "2026-01-01T10:00:00Z",
            },
            {
                "session_id": session_id,
                "event_type": "command.begin",
                "command": "save-usage",
                "timestamp": "2026-01-01T10:00:05Z",
            },
        ]

        with open(events_log, "w") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")

        original_home = os.environ.get("HOME")
        try:
            os.environ["HOME"] = tmpdir
            save_usage = _load_save_usage_module()
            result = save_usage.detect_session_commands(session_id)
            expected = [
                {"command": "expert-review", "model": "opus", "effort": "4"},
                {"command": "save-usage"},
            ]
            return (
                result == expected
            ), f"Expected {expected}, got {result!r}"
        finally:
            if original_home:
                os.environ["HOME"] = original_home
            elif "HOME" in os.environ:
                del os.environ["HOME"]


def test_detect_session_commands_empty_without_session_id():
    """detect_session_commands returns [] if session_id is falsy."""
    with tempfile.TemporaryDirectory() as tmpdir:
        original_home = os.environ.get("HOME")
        try:
            os.environ["HOME"] = tmpdir
            save_usage = _load_save_usage_module()
            result = save_usage.detect_session_commands(None)
            return (
                result == []
            ), f"Expected [], got {result!r}"
        finally:
            if original_home:
                os.environ["HOME"] = original_home
            elif "HOME" in os.environ:
                del os.environ["HOME"]


def test_detect_session_commands_empty_missing_file():
    """detect_session_commands returns [] if events log file doesn't exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        session_id = "test-session-" + uuid.uuid4().hex[:8]

        original_home = os.environ.get("HOME")
        try:
            os.environ["HOME"] = tmpdir
            save_usage = _load_save_usage_module()
            result = save_usage.detect_session_commands(session_id)
            return (
                result == []
            ), f"Expected [], got {result!r}"
        finally:
            if original_home:
                os.environ["HOME"] = original_home
            elif "HOME" in os.environ:
                del os.environ["HOME"]


def test_detect_session_commands_ignores_empty_command_field():
    """detect_session_commands skips events with missing or empty command field."""
    with tempfile.TemporaryDirectory() as tmpdir:
        events_dir = Path(tmpdir) / ".claude" / "telemetry"
        events_dir.mkdir(parents=True)
        events_log = events_dir / "events.jsonl"

        session_id = "test-session-" + uuid.uuid4().hex[:8]

        events = [
            {
                "session_id": session_id,
                "event_type": "command.begin",
                "command": "",
                "timestamp": "2026-01-01T10:00:00Z",
            },
            {
                "session_id": session_id,
                "event_type": "command.begin",
                # Missing command field
                "timestamp": "2026-01-01T10:00:05Z",
            },
            {
                "session_id": session_id,
                "event_type": "command.begin",
                "command": "valid-command",
                "timestamp": "2026-01-01T10:00:10Z",
            },
        ]

        with open(events_log, "w") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")

        original_home = os.environ.get("HOME")
        try:
            os.environ["HOME"] = tmpdir
            save_usage = _load_save_usage_module()
            result = save_usage.detect_session_commands(session_id)
            return (
                result == [{"command": "valid-command"}]
            ), f"Expected [{{'command': 'valid-command'}}], got {result!r}"
        finally:
            if original_home:
                os.environ["HOME"] = original_home
            elif "HOME" in os.environ:
                del os.environ["HOME"]


def test_detect_session_commands_handles_malformed_json():
    """detect_session_commands gracefully handles malformed JSON lines."""
    with tempfile.TemporaryDirectory() as tmpdir:
        events_dir = Path(tmpdir) / ".claude" / "telemetry"
        events_dir.mkdir(parents=True)
        events_log = events_dir / "events.jsonl"

        session_id = "test-session-" + uuid.uuid4().hex[:8]

        with open(events_log, "w") as f:
            f.write("not valid json\n")
            f.write(json.dumps({
                "session_id": session_id,
                "event_type": "command.begin",
                "command": "valid-command",
                "timestamp": "2026-01-01T10:00:00Z",
            }) + "\n")

        original_home = os.environ.get("HOME")
        try:
            os.environ["HOME"] = tmpdir
            save_usage = _load_save_usage_module()
            result = save_usage.detect_session_commands(session_id)
            return (
                result == [{"command": "valid-command"}]
            ), f"Expected [{{'command': 'valid-command'}}] even with malformed JSON, got {result!r}"
        finally:
            if original_home:
                os.environ["HOME"] = original_home
            elif "HOME" in os.environ:
                del os.environ["HOME"]


def test_detect_worktree_returns_basename():
    """detect_worktree returns git worktree's directory basename."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir) / "test-repo"
        repo_path.mkdir()

        subprocess.run(
            ["git", "init"],
            cwd=repo_path,
            capture_output=True,
        )

        original_cwd = os.getcwd()
        try:
            os.chdir(repo_path)
            save_usage = _load_save_usage_module()
            result = save_usage.detect_worktree()
            return (
                result == "test-repo"
            ), f"Expected 'test-repo', got {result!r}"
        finally:
            os.chdir(original_cwd)


def test_detect_worktree_returns_none_not_in_git():
    """detect_worktree returns None if not in a git repository."""
    with tempfile.TemporaryDirectory() as tmpdir:
        original_cwd = os.getcwd()
        try:
            os.chdir(tmpdir)
            save_usage = _load_save_usage_module()
            result = save_usage.detect_worktree()
            return (
                result is None
            ), f"Expected None when not in git repo, got {result!r}"
        finally:
            os.chdir(original_cwd)


def test_usage_record_includes_worktree_field():
    """Written record includes 'worktree' field (populated from detect_worktree)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Set up git repo and telemetry directories
        repo_path = Path(tmpdir) / "test-repo-dir"
        repo_path.mkdir()

        subprocess.run(["git", "init"], cwd=repo_path, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo_path,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo_path,
            capture_output=True,
        )

        telemetry_dir = Path(tmpdir) / ".claude" / "telemetry"
        telemetry_dir.mkdir(parents=True)

        usage_log = telemetry_dir / "usage-log.jsonl"

        # Create minimal usage panel input
        usage_panel = """Settings  Status   Config   Usage   Stats
Session
Total cost:            $1.00
Total duration (API):  1m 0s
Total duration (wall): 1m 0s
Total code changes:    0 lines added, 0 lines removed
Usage by model:
    claude-haiku:  100 input, 50 output, 0 cache read, 0 cache write ($0.01)
"""

        env = os.environ.copy()
        env["HOME"] = tmpdir

        subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=usage_panel,
            capture_output=True,
            text=True,
            cwd=repo_path,
            env=env,
        )

        # Check the written record
        if not usage_log.exists():
            return False, "usage-log.jsonl was not created"

        with open(usage_log) as f:
            record = json.loads(f.readline())

        has_worktree = "worktree" in record
        worktree_value = record.get("worktree")

        return (
            has_worktree and worktree_value == "test-repo-dir"
        ), f"Expected worktree='test-repo-dir', got {record.get('worktree')!r}"


def test_usage_record_includes_session_id_and_commands():
    """Written record includes 'session_id' and 'commands' fields sourced from telemetry."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir) / "my-repo"
        repo_path.mkdir()

        subprocess.run(["git", "init"], cwd=repo_path, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo_path,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo_path,
            capture_output=True,
        )

        telemetry_dir = Path(tmpdir) / ".claude" / "telemetry"
        telemetry_dir.mkdir(parents=True)
        events_log = telemetry_dir / "events.jsonl"
        usage_log = telemetry_dir / "usage-log.jsonl"

        session_id = "test-session-" + uuid.uuid4().hex[:8]

        with open(events_log, "w") as f:
            f.write(json.dumps({
                "session_id": session_id,
                "event_type": "command.begin",
                "command": "expert-review",
                "model": "opus",
                "timestamp": "2026-01-01T10:00:00Z",
            }) + "\n")
            f.write(json.dumps({
                "session_id": session_id,
                "event_type": "command.begin",
                "command": "save-usage",
                "timestamp": "2026-01-01T10:05:00Z",
            }) + "\n")

        usage_panel = """Settings  Status   Config   Usage   Stats
Session
Total cost:            $1.50
Total duration (API):  2m 0s
Total duration (wall): 2m 0s
Total code changes:    0 lines added, 0 lines removed
Usage by model:
    claude-sonnet:  1000 input, 500 output, 0 cache read, 0 cache write ($1.00)
"""

        env = os.environ.copy()
        env["HOME"] = tmpdir
        env["CLAUDE_CODE_SESSION_ID"] = session_id

        subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=usage_panel,
            capture_output=True,
            text=True,
            cwd=repo_path,
            env=env,
        )

        if not usage_log.exists():
            return False, "usage-log.jsonl was not created"

        with open(usage_log) as f:
            record = json.loads(f.readline())

        expected_commands = [
            {"command": "expert-review", "model": "opus"},
            {"command": "save-usage"},
        ]

        return (
            record.get("session_id") == session_id
            and record.get("commands") == expected_commands
        ), f"Expected session_id={session_id!r} and commands={expected_commands!r}, got session_id={record.get('session_id')!r} commands={record.get('commands')!r}"


def test_fallback_label_uses_session_commands():
    """Fallback label includes every distinct command run in the session, joined with '+'."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Set up repo and telemetry
        repo_path = Path(tmpdir) / "my-repo"
        repo_path.mkdir()

        subprocess.run(["git", "init"], cwd=repo_path, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo_path,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo_path,
            capture_output=True,
        )

        # Create a test branch
        subprocess.run(
            ["git", "checkout", "-b", "feature-x"],
            cwd=repo_path,
            capture_output=True,
        )

        telemetry_dir = Path(tmpdir) / ".claude" / "telemetry"
        telemetry_dir.mkdir(parents=True)
        events_log = telemetry_dir / "events.jsonl"
        usage_log = telemetry_dir / "usage-log.jsonl"

        session_id = "test-session-" + uuid.uuid4().hex[:8]

        # Write two distinct commands in this session — the label should reflect
        # both, not just whichever ran last.
        with open(events_log, "w") as f:
            f.write(json.dumps({
                "session_id": session_id,
                "event_type": "command.begin",
                "command": "expert-plan",
                "timestamp": "2026-01-01T10:00:00Z",
            }) + "\n")
            f.write(json.dumps({
                "session_id": session_id,
                "event_type": "command.begin",
                "command": "expert-review",
                "timestamp": "2026-01-01T10:10:00Z",
            }) + "\n")

        usage_panel = """Settings  Status   Config   Usage   Stats
Session
Total cost:            $1.50
Total duration (API):  2m 0s
Total duration (wall): 2m 0s
Total code changes:    0 lines added, 0 lines removed
Usage by model:
    claude-sonnet:  1000 input, 500 output, 0 cache read, 0 cache write ($1.00)
"""

        env = os.environ.copy()
        env["HOME"] = tmpdir
        env["CLAUDE_CODE_SESSION_ID"] = session_id

        subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=usage_panel,
            capture_output=True,
            text=True,
            cwd=repo_path,
            env=env,
        )

        if not usage_log.exists():
            return False, "usage-log.jsonl was not created"

        with open(usage_log) as f:
            record = json.loads(f.readline())

        label = record.get("label", "")
        has_both_commands = "expert-plan" in label and "expert-review" in label
        has_branch = "feature-x" in label

        return (
            has_both_commands and has_branch
        ), f"Expected label to contain both commands and 'feature-x', got '{label}'"


def test_explicit_label_never_modified():
    """Explicit label from stdin is never modified by auto-generation logic."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir) / "repo"
        repo_path.mkdir()

        subprocess.run(["git", "init"], cwd=repo_path, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo_path,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo_path,
            capture_output=True,
        )

        telemetry_dir = Path(tmpdir) / ".claude" / "telemetry"
        telemetry_dir.mkdir(parents=True)
        events_log = telemetry_dir / "events.jsonl"
        usage_log = telemetry_dir / "usage-log.jsonl"

        session_id = "test-session-" + uuid.uuid4().hex[:8]

        # Write a command event that would be picked up by auto-generation
        with open(events_log, "w") as f:
            f.write(json.dumps({
                "session_id": session_id,
                "event_type": "command.begin",
                "command": "expert-review",
                "timestamp": "2026-01-01T10:00:00Z",
            }) + "\n")

        # Input with explicit label on first line
        input_text = """my-custom-label
Settings  Status   Config   Usage   Stats
Session
Total cost:            $1.00
Total duration (API):  1m 0s
Total duration (wall): 1m 0s
Total code changes:    0 lines added, 0 lines removed
Usage by model:
    claude-haiku:  100 input, 50 output, 0 cache read, 0 cache write ($0.01)
"""

        env = os.environ.copy()
        env["HOME"] = tmpdir
        env["CLAUDE_CODE_SESSION_ID"] = session_id

        subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=input_text,
            capture_output=True,
            text=True,
            cwd=repo_path,
            env=env,
        )

        if not usage_log.exists():
            return False, "usage-log.jsonl was not created"

        with open(usage_log) as f:
            record = json.loads(f.readline())

        label = record.get("label", "")
        is_exact = label == "my-custom-label"

        return (
            is_exact
        ), f"Expected label 'my-custom-label', got '{label}'"


def test_fallback_label_omits_falsy_parts():
    """Fallback label filters out None/'unknown' parts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir) / "repo"
        repo_path.mkdir()

        subprocess.run(["git", "init"], cwd=repo_path, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo_path,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo_path,
            capture_output=True,
        )

        telemetry_dir = Path(tmpdir) / ".claude" / "telemetry"
        telemetry_dir.mkdir(parents=True)
        usage_log = telemetry_dir / "usage-log.jsonl"

        # No events log (detect_session_commands returns [])

        usage_panel = """Settings  Status   Config   Usage   Stats
Session
Total cost:            $1.00
Total duration (API):  1m 0s
Total duration (wall): 1m 0s
Total code changes:    0 lines added, 0 lines removed
Usage by model:
    claude-haiku:  100 input, 50 output, 0 cache read, 0 cache write ($0.01)
"""

        env = os.environ.copy()
        env["HOME"] = tmpdir

        subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=usage_panel,
            capture_output=True,
            text=True,
            cwd=repo_path,
            env=env,
        )

        if not usage_log.exists():
            return False, "usage-log.jsonl was not created"

        with open(usage_log) as f:
            record = json.loads(f.readline())

        label = record.get("label", "")
        # When no command available, label should still have repo and branch parts
        # (detect_session_commands returns [], so it falls back to repo-branch-timestamp)
        has_repo_part = "repo" in label
        has_timestamp = any(char.isdigit() for char in label)

        return (
            has_repo_part and has_timestamp
        ), f"Expected label to have repo and timestamp parts, got '{label}'"


def run_all_tests():
    """Run all tests and report results."""
    h = Harness("SAVE-USAGE TEST SUITE")

    # Test parse_duration 'd' unit support
    h.test_result(
        "parse_duration handles days alongside h/m/s",
        *test_parse_duration_handles_days(),
    )
    h.test_result(
        "parse_duration handles a bare days value",
        *test_parse_duration_days_only(),
    )

    # Test detect_session_commands
    h.test_result(
        "detect_session_commands returns chronological list for the session",
        *test_detect_session_commands_returns_chronological_list(),
    )
    h.test_result(
        "detect_session_commands includes model/effort when recorded",
        *test_detect_session_commands_includes_model_and_effort(),
    )
    h.test_result(
        "detect_session_commands returns [] without a session_id",
        *test_detect_session_commands_empty_without_session_id(),
    )
    h.test_result(
        "detect_session_commands returns [] if events log missing",
        *test_detect_session_commands_empty_missing_file(),
    )
    h.test_result(
        "detect_session_commands ignores events with empty/missing command",
        *test_detect_session_commands_ignores_empty_command_field(),
    )
    h.test_result(
        "detect_session_commands handles malformed JSON gracefully",
        *test_detect_session_commands_handles_malformed_json(),
    )

    # Test detect_worktree
    h.test_result(
        "detect_worktree returns git repo basename",
        *test_detect_worktree_returns_basename(),
    )
    h.test_result(
        "detect_worktree returns None outside git repo",
        *test_detect_worktree_returns_none_not_in_git(),
    )

    # Test integration: worktree/session_id/commands fields in record
    h.test_result(
        "usage record includes worktree field",
        *test_usage_record_includes_worktree_field(),
    )
    h.test_result(
        "usage record includes session_id and commands fields",
        *test_usage_record_includes_session_id_and_commands(),
    )

    # Test fallback label logic
    h.test_result(
        "fallback label includes every session command, not just the last",
        *test_fallback_label_uses_session_commands(),
    )
    h.test_result(
        "explicit label never modified by auto-generation",
        *test_explicit_label_never_modified(),
    )
    h.test_result(
        "fallback label falls back to repo-branch-timestamp",
        *test_fallback_label_omits_falsy_parts(),
    )

    h.summarize_and_exit()


if __name__ == "__main__":
    run_all_tests()
