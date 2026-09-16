#!/usr/bin/env python3
"""
Test suite for scripts/save-usage.py.

Covers:
- detect_last_command() function: finding latest command from telemetry events
- detect_worktree() function: extracting git worktree basename
- Fallback label generation when no explicit label provided
- Explicit label handling (never modified)
- worktree field in written records
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
from datetime import datetime, timezone
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


def test_detect_last_command_finds_latest_event():
    """detect_last_command finds event with latest timestamp matching session_id and command field."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create telemetry events log
        events_dir = Path(tmpdir) / ".claude" / "telemetry"
        events_dir.mkdir(parents=True)
        events_log = events_dir / "events.jsonl"

        session_id = "test-session-" + uuid.uuid4().hex[:8]

        # Write three events: two for our session, one for another
        events = [
            {
                "session_id": session_id,
                "event_type": "command.begin",
                "command": "expert-review",
                "timestamp": "2026-01-01T10:00:00Z",
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
                "command": "expert-plan",
                "timestamp": "2026-01-01T10:00:10Z",
            },
        ]

        with open(events_log, "w") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")

        # Temporarily override home and session id BEFORE importing
        original_home = os.environ.get("HOME")
        original_session = os.environ.get("CLAUDE_CODE_SESSION_ID")

        try:
            os.environ["HOME"] = tmpdir
            os.environ["CLAUDE_CODE_SESSION_ID"] = session_id

            # Import AFTER setting HOME
            save_usage = _load_save_usage_module()
            result = save_usage.detect_last_command()
            return (
                result == "expert-plan"
            ), f"Expected 'expert-plan', got {result!r}"
        finally:
            if original_home:
                os.environ["HOME"] = original_home
            elif "HOME" in os.environ:
                del os.environ["HOME"]
            if original_session:
                os.environ["CLAUDE_CODE_SESSION_ID"] = original_session
            elif "CLAUDE_CODE_SESSION_ID" in os.environ:
                del os.environ["CLAUDE_CODE_SESSION_ID"]


def test_detect_last_command_returns_none_without_session_id():
    """detect_last_command returns None if CLAUDE_CODE_SESSION_ID is unset."""
    with tempfile.TemporaryDirectory() as tmpdir:
        events_dir = Path(tmpdir) / ".claude" / "telemetry"
        events_dir.mkdir(parents=True)
        events_log = events_dir / "events.jsonl"

        events = [
            {
                "session_id": "some-session",
                "event_type": "command.begin",
                "command": "test",
                "timestamp": "2026-01-01T10:00:00Z",
            }
        ]

        with open(events_log, "w") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")

        original_home = os.environ.get("HOME")
        original_session = os.environ.get("CLAUDE_CODE_SESSION_ID")

        try:
            os.environ["HOME"] = tmpdir
            if "CLAUDE_CODE_SESSION_ID" in os.environ:
                del os.environ["CLAUDE_CODE_SESSION_ID"]

            save_usage = _load_save_usage_module()
            result = save_usage.detect_last_command()
            return (
                result is None
            ), f"Expected None, got {result!r}"
        finally:
            if original_home:
                os.environ["HOME"] = original_home
            if original_session:
                os.environ["CLAUDE_CODE_SESSION_ID"] = original_session


def test_detect_last_command_returns_none_missing_file():
    """detect_last_command returns None if events log file doesn't exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        session_id = "test-session-" + uuid.uuid4().hex[:8]

        original_home = os.environ.get("HOME")
        original_session = os.environ.get("CLAUDE_CODE_SESSION_ID")

        try:
            os.environ["HOME"] = tmpdir
            os.environ["CLAUDE_CODE_SESSION_ID"] = session_id

            save_usage = _load_save_usage_module()
            result = save_usage.detect_last_command()
            return (
                result is None
            ), f"Expected None, got {result!r}"
        finally:
            if original_home:
                os.environ["HOME"] = original_home
            elif "HOME" in os.environ:
                del os.environ["HOME"]
            if original_session:
                os.environ["CLAUDE_CODE_SESSION_ID"] = original_session
            elif "CLAUDE_CODE_SESSION_ID" in os.environ:
                del os.environ["CLAUDE_CODE_SESSION_ID"]


def test_detect_last_command_ignores_empty_command_field():
    """detect_last_command skips events with missing or empty command field."""
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
        original_session = os.environ.get("CLAUDE_CODE_SESSION_ID")

        try:
            os.environ["HOME"] = tmpdir
            os.environ["CLAUDE_CODE_SESSION_ID"] = session_id

            save_usage = _load_save_usage_module()
            result = save_usage.detect_last_command()
            return (
                result == "valid-command"
            ), f"Expected 'valid-command', got {result!r}"
        finally:
            if original_home:
                os.environ["HOME"] = original_home
            elif "HOME" in os.environ:
                del os.environ["HOME"]
            if original_session:
                os.environ["CLAUDE_CODE_SESSION_ID"] = original_session
            elif "CLAUDE_CODE_SESSION_ID" in os.environ:
                del os.environ["CLAUDE_CODE_SESSION_ID"]


def test_detect_last_command_handles_malformed_json():
    """detect_last_command gracefully handles malformed JSON lines."""
    with tempfile.TemporaryDirectory() as tmpdir:
        events_dir = Path(tmpdir) / ".claude" / "telemetry"
        events_dir.mkdir(parents=True)
        events_log = events_dir / "events.jsonl"

        session_id = "test-session-" + uuid.uuid4().hex[:8]

        # Mix of valid and malformed lines
        with open(events_log, "w") as f:
            f.write("not valid json\n")
            f.write(json.dumps({
                "session_id": session_id,
                "event_type": "command.begin",
                "command": "valid-command",
                "timestamp": "2026-01-01T10:00:00Z",
            }) + "\n")

        original_home = os.environ.get("HOME")
        original_session = os.environ.get("CLAUDE_CODE_SESSION_ID")

        try:
            os.environ["HOME"] = tmpdir
            os.environ["CLAUDE_CODE_SESSION_ID"] = session_id

            save_usage = _load_save_usage_module()
            result = save_usage.detect_last_command()
            return (
                result == "valid-command"
            ), f"Expected 'valid-command' even with malformed JSON, got {result!r}"
        finally:
            if original_home:
                os.environ["HOME"] = original_home
            elif "HOME" in os.environ:
                del os.environ["HOME"]
            if original_session:
                os.environ["CLAUDE_CODE_SESSION_ID"] = original_session
            elif "CLAUDE_CODE_SESSION_ID" in os.environ:
                del os.environ["CLAUDE_CODE_SESSION_ID"]


def test_detect_last_command_prefers_latest_timestamp():
    """detect_last_command uses timestamp to break ties, not order in file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        events_dir = Path(tmpdir) / ".claude" / "telemetry"
        events_dir.mkdir(parents=True)
        events_log = events_dir / "events.jsonl"

        session_id = "test-session-" + uuid.uuid4().hex[:8]

        # Write events out of chronological order
        events = [
            {
                "session_id": session_id,
                "event_type": "command.begin",
                "command": "cmd-third",
                "timestamp": "2026-01-01T10:00:30Z",
            },
            {
                "session_id": session_id,
                "event_type": "command.end",
                "command": "cmd-first",
                "timestamp": "2026-01-01T10:00:00Z",
            },
            {
                "session_id": session_id,
                "event_type": "command.begin",
                "command": "cmd-latest",
                "timestamp": "2026-01-01T10:00:50Z",
            },
        ]

        with open(events_log, "w") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")

        original_home = os.environ.get("HOME")
        original_session = os.environ.get("CLAUDE_CODE_SESSION_ID")

        try:
            os.environ["HOME"] = tmpdir
            os.environ["CLAUDE_CODE_SESSION_ID"] = session_id

            save_usage = _load_save_usage_module()
            result = save_usage.detect_last_command()
            return (
                result == "cmd-latest"
            ), f"Expected 'cmd-latest' (latest timestamp), got {result!r}"
        finally:
            if original_home:
                os.environ["HOME"] = original_home
            elif "HOME" in os.environ:
                del os.environ["HOME"]
            if original_session:
                os.environ["CLAUDE_CODE_SESSION_ID"] = original_session
            elif "CLAUDE_CODE_SESSION_ID" in os.environ:
                del os.environ["CLAUDE_CODE_SESSION_ID"]


def test_detect_worktree_returns_basename():
    """detect_worktree returns git worktree's directory basename."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir) / "test-repo"
        repo_path.mkdir()

        # Initialize a git repo
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

        code = subprocess.run(
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


def test_fallback_label_uses_last_command():
    """Fallback label includes last command when available."""
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

        # Write a command event
        with open(events_log, "w") as f:
            f.write(json.dumps({
                "session_id": session_id,
                "event_type": "command.begin",
                "command": "expert-review",
                "timestamp": "2026-01-01T10:00:00Z",
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

        code = subprocess.run(
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
        has_command = "expert-review" in label
        has_branch = "feature-x" in label

        return (
            has_command and has_branch
        ), f"Expected label to contain 'expert-review' and 'feature-x', got '{label}'"


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

        code = subprocess.run(
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

        # No events log (detect_last_command returns None)

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

        code = subprocess.run(
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
        # (detect_last_command returns None, so it falls back to repo-branch-timestamp)
        has_repo_part = "repo" in label
        has_timestamp = any(char.isdigit() for char in label)

        return (
            has_repo_part and has_timestamp
        ), f"Expected label to have repo and timestamp parts, got '{label}'"


def run_all_tests():
    """Run all tests and report results."""
    h = Harness("SAVE-USAGE TEST SUITE")

    # Test detect_last_command
    h.test_result(
        "detect_last_command finds latest event by timestamp",
        *test_detect_last_command_finds_latest_event(),
    )
    h.test_result(
        "detect_last_command returns None without CLAUDE_CODE_SESSION_ID",
        *test_detect_last_command_returns_none_without_session_id(),
    )
    h.test_result(
        "detect_last_command returns None if events log missing",
        *test_detect_last_command_returns_none_missing_file(),
    )
    h.test_result(
        "detect_last_command ignores events with empty/missing command",
        *test_detect_last_command_ignores_empty_command_field(),
    )
    h.test_result(
        "detect_last_command handles malformed JSON gracefully",
        *test_detect_last_command_handles_malformed_json(),
    )
    h.test_result(
        "detect_last_command prefers latest timestamp over file order",
        *test_detect_last_command_prefers_latest_timestamp(),
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

    # Test integration: worktree field in record
    h.test_result(
        "usage record includes worktree field",
        *test_usage_record_includes_worktree_field(),
    )

    # Test fallback label logic
    h.test_result(
        "fallback label includes last command when available",
        *test_fallback_label_uses_last_command(),
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
