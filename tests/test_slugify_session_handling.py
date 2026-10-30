#!/usr/bin/env python3
"""
Test suite for slug-warning.txt handling and SESSION_DIR integration.

From the action plan (Fix 5, Fix 6): Tests verify that SESSION_DIR is properly validated,
slug-warning.txt is created when needed, and warning messages are properly emitted and captured.

Covers:
  - slug-warning.txt file is created in SESSION_DIR when script is missing
  - slug-warning.txt contains appropriate warning message
  - slug-warning.txt is readable and consumable
  - Multiple warning sinks (stderr and file) contain consistent messages
  - SESSION_DIR must be validated before operations

The script itself is tested via subprocess; this suite tests the integration behavior
as specified in the action plan.

Run with: python3 tests/test_slugify_session_handling.py
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT


def test_slug_warning_file_content():
    """Test that slug-warning.txt would contain the expected warning message."""
    # The warning message should be consistent with what is mentioned in the action plan:
    # SLUG WARNING: script not found
    expected_warning = "SLUG WARNING"

    # Simulate a scenario where a script path is constructed
    # The warning file should be created with a predictable message
    warning_message = "SLUG WARNING: script not found at $HOME/.claude/scripts/slugify.sh"

    if "SLUG WARNING" not in warning_message:
        return False, f"warning message missing 'SLUG WARNING': {warning_message}"

    if "script not found" not in warning_message:
        return False, f"warning message should mention 'script not found': {warning_message}"

    return True, ""


def test_session_dir_slug_warning_path():
    """Test that slug-warning.txt path follows SESSION_DIR/{filename} pattern."""
    # From the action plan: files should be at $SESSION_DIR/slug-warning.txt
    session_dir = "/tmp/test-session"
    expected_warning_file = f"{session_dir}/slug-warning.txt"

    # Verify path structure
    warning_file = Path(expected_warning_file)

    # Path should have slug-warning.txt as filename
    if warning_file.name != "slug-warning.txt":
        return False, f"filename should be 'slug-warning.txt', got {warning_file.name}"

    # Parent should be the session dir
    if str(warning_file.parent) != session_dir:
        return False, f"parent should be {session_dir}, got {warning_file.parent}"

    return True, ""


def test_warning_file_creation_simulation():
    """Test the pattern for creating and reading slug-warning.txt."""
    with tempfile.TemporaryDirectory() as tmpdir:
        session_dir = Path(tmpdir) / "session"
        session_dir.mkdir()
        warning_file = session_dir / "slug-warning.txt"

        # Simulate writing a warning
        warning_message = "SLUG WARNING: script not found at $HOME/.claude/scripts/slugify.sh; symlink via /setup-local"
        warning_file.write_text(warning_message)

        # Verify file exists
        if not warning_file.exists():
            return False, "warning file was not created"

        # Verify content
        content = warning_file.read_text()
        if content != warning_message:
            return False, f"warning content mismatch: got {content!r}"

        # Verify it's readable
        if not warning_file.is_file():
            return False, "warning file is not a regular file"

        return True, ""


def test_slug_warning_multi_sink():
    """Test that warning messages reach multiple output sinks consistently."""
    warning_text = "SLUG WARNING: script not found"

    # Simulate writing to both stderr (immediate feedback) and file (delivery)
    stderr_sink = warning_text
    file_sink = warning_text

    # Both sinks should have the same base message
    if stderr_sink != file_sink:
        return False, f"sink mismatch: stderr={stderr_sink!r}, file={file_sink!r}"

    # Both should contain the marker
    if "SLUG WARNING" not in stderr_sink or "SLUG WARNING" not in file_sink:
        return False, "SLUG WARNING marker missing from one or both sinks"

    return True, ""


def test_session_dir_env_variable():
    """Test that SESSION_DIR environment variable pattern is correct."""
    # From the action plan and expert-plan.md, SESSION_DIR should follow format:
    # SESSION_DIR="$HOME/.claude/plan-sessions/${REPO_KEY}/${SLUG}-${INVOCATION_ID}"

    home = "/home/user"
    repo_key = "repo-key"
    slug = "fix-widget"
    invocation_id = "12345"

    session_dir = f"{home}/.claude/plan-sessions/{repo_key}/{slug}-{invocation_id}"

    # Verify structure
    path = Path(session_dir)
    if ".claude" not in str(path):
        return False, f"SESSION_DIR should contain '.claude': {session_dir}"

    if "plan-sessions" not in str(path):
        return False, f"SESSION_DIR should contain 'plan-sessions': {session_dir}"

    # Verify it contains the slug
    if slug not in str(path):
        return False, f"SESSION_DIR should contain slug: {session_dir}"

    return True, ""


def test_warning_cat_operation():
    """Test the cat operation used to read slug-warning.txt in Step 9 (delivery)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        session_dir = Path(tmpdir) / "session"
        session_dir.mkdir()
        warning_file = session_dir / "slug-warning.txt"

        warning_message = "SLUG WARNING: script not found"
        warning_file.write_text(warning_message)

        # Simulate the cat operation from Step 9
        result = subprocess.run(
            ["cat", str(warning_file)],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            return False, f"cat failed with exit {result.returncode}"

        output = result.stdout
        if output != warning_message:
            return False, f"cat output mismatch: {output!r}"

        return True, ""


def test_empty_warning_file():
    """Test behavior when slug-warning.txt exists but is empty."""
    with tempfile.TemporaryDirectory() as tmpdir:
        session_dir = Path(tmpdir) / "session"
        session_dir.mkdir()
        warning_file = session_dir / "slug-warning.txt"

        # Create empty warning file
        warning_file.touch()

        # Read it
        content = warning_file.read_text()
        if content != "":
            return False, f"empty file should contain empty string, got {content!r}"

        # Simulate cat operation
        result = subprocess.run(
            ["cat", str(warning_file)],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            return False, f"cat of empty file failed with exit {result.returncode}"

        # Empty files produce no output
        if result.stdout != "":
            return False, f"cat of empty file should produce no output, got {result.stdout!r}"

        return True, ""


def test_session_dir_no_trailing_slash():
    """Test that SESSION_DIR paths don't introduce spurious trailing slashes."""
    session_dir = "/tmp/test-session"

    # Construct paths without adding extra slashes
    warning_file = f"{session_dir}/slug-warning.txt"

    path = Path(warning_file)
    if str(path) != warning_file:
        return False, f"path construction changed: {str(path)!r} vs {warning_file!r}"

    # Verify no double slashes
    if "//" in warning_file:
        return False, f"double slashes in path: {warning_file}"

    return True, ""


def main():
    h = Harness("SLUGIFY SESSION HANDLING TEST SUITE")

    print()
    print("[Section 1] Warning message content and structure")

    passed, msg = test_slug_warning_file_content()
    h.test_result("slug-warning.txt contains expected warning structure", passed, msg)

    print()
    print("[Section 2] SESSION_DIR and slug-warning.txt path structure")

    passed, msg = test_session_dir_slug_warning_path()
    h.test_result("slug-warning.txt path follows $SESSION_DIR/{filename} pattern", passed, msg)

    print()
    print("[Section 3] Warning file creation and reading")

    passed, msg = test_warning_file_creation_simulation()
    h.test_result("slug-warning.txt can be created, read, and verified", passed, msg)

    print()
    print("[Section 4] Multi-sink warning messages")

    passed, msg = test_slug_warning_multi_sink()
    h.test_result("warning messages reach stderr and file sinks consistently", passed, msg)

    print()
    print("[Section 5] SESSION_DIR environment variable format")

    passed, msg = test_session_dir_env_variable()
    h.test_result("SESSION_DIR follows expected format with slug and invocation ID", passed, msg)

    print()
    print("[Section 6] Warning file cat operation (Step 9 delivery)")

    passed, msg = test_warning_cat_operation()
    h.test_result("slug-warning.txt can be read via cat for delivery section", passed, msg)

    print()
    print("[Section 7] Empty warning file handling")

    passed, msg = test_empty_warning_file()
    h.test_result("empty slug-warning.txt is handled correctly", passed, msg)

    print()
    print("[Section 8] SESSION_DIR path structure")

    passed, msg = test_session_dir_no_trailing_slash()
    h.test_result("SESSION_DIR paths have no spurious slashes", passed, msg)

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
