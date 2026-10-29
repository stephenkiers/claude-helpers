#!/usr/bin/env python3
"""
Test suite for slugify.sh integration behavior described in the plan.

This tests the integration mechanics described in the plan's approach section:
- The script reads from stdin (never argv)
- File-based title passing through a session directory
- Fallback behavior when the script or title file is missing
- Script invocation patterns as described in the plan

Covers:
- Title file reading and slug generation from file
- Missing file fallback
- Missing script fallback
- Security: hostile titles do not escape into shell
- Script location assumptions (under ~/.claude/scripts/)

Run with: python3 tests/test_slugify_title_file.py
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT


def run_slug_from_file(title_file_path, slugify_script_path, shell="bash"):
    """
    Run the Step 1 slug pattern: read from a file and pipe into the script.
    Simulates: SLUG=$(bash "$SLUGIFY" < "$TITLE_FILE") || SLUG=""
    Returns (returncode, stdout, stderr).
    """
    script_content = f'''
TITLE_FILE="{title_file_path}"
SLUGIFY="{slugify_script_path}"
SLUG=""
[ -f "$SLUGIFY" ] || {{ echo "SLUG WARNING: script not found" >&2; SLUG="plan"; }}
[ -s "$TITLE_FILE" ] || {{ echo "SLUG WARNING: title file empty or missing" >&2; SLUG="plan"; }}
if [ -f "$SLUGIFY" ] && [ -s "$TITLE_FILE" ]; then
    SLUG=$(bash "$SLUGIFY" < "$TITLE_FILE") || SLUG=""
fi
[ -n "$SLUG" ] || SLUG="plan"
printf "%s" "$SLUG"
'''

    result = subprocess.run(
        [shell, "-c", script_content],
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def test_title_from_file():
    """Test that a title read from a file produces the correct slug."""
    with tempfile.TemporaryDirectory() as tmpdir:
        title_file = Path(tmpdir) / "title.txt"
        title_file.write_text("My Project Title")

        code, stdout, stderr = run_slug_from_file(
            str(title_file),
            str(REPO_ROOT / "scripts" / "slugify.sh")
        )

        slug = stdout.strip()
        if slug != "my-project-title":
            return False, f"expected 'my-project-title', got {slug!r}"

        return True, ""


def test_missing_title_file_fallback():
    """Test that a missing title file produces 'plan' and a warning."""
    with tempfile.TemporaryDirectory() as tmpdir:
        title_file = Path(tmpdir) / "nonexistent.txt"

        code, stdout, stderr = run_slug_from_file(
            str(title_file),
            str(REPO_ROOT / "scripts" / "slugify.sh")
        )

        slug = stdout.strip()
        if slug != "plan":
            return False, f"expected 'plan', got {slug!r}"

        if "SLUG WARNING" not in stderr:
            return False, "expected warning in stderr"

        return True, ""


def test_missing_script_fallback():
    """Test that a missing script produces 'plan' and a warning."""
    with tempfile.TemporaryDirectory() as tmpdir:
        title_file = Path(tmpdir) / "title.txt"
        title_file.write_text("Some Title")
        missing_script = Path(tmpdir) / "nonexistent.sh"

        code, stdout, stderr = run_slug_from_file(
            str(title_file),
            str(missing_script)
        )

        slug = stdout.strip()
        if slug != "plan":
            return False, f"expected 'plan', got {slug!r}"

        if "SLUG WARNING" not in stderr:
            return False, "expected warning in stderr"

        return True, ""


def test_hostile_title_from_file():
    """Test that hostile titles in a file do not execute shell commands."""
    with tempfile.TemporaryDirectory() as tmpdir:
        title_file = Path(tmpdir) / "title.txt"
        canary_file = Path(tmpdir) / "CANARY"

        # Write a hostile title that attempts to create a file
        hostile_title = 'x"; touch ' + str(canary_file) + ' || true; echo "y'
        title_file.write_text(hostile_title)

        code, stdout, stderr = run_slug_from_file(
            str(title_file),
            str(REPO_ROOT / "scripts" / "slugify.sh")
        )

        # The canary file should NOT exist (no command execution)
        if canary_file.exists():
            return False, "hostile title attempted to create a file (command execution)"

        # Should still produce a valid slug
        slug = stdout.strip()
        if not re.match(r"^[a-z0-9-]{1,50}$|^plan$", slug):
            return False, f"invalid slug format: {slug!r}"

        return True, ""


def test_hostile_title_backtick():
    """Test that backtick command substitution in a title doesn't execute."""
    with tempfile.TemporaryDirectory() as tmpdir:
        title_file = Path(tmpdir) / "title.txt"
        canary_file = Path(tmpdir) / "CANARY"

        # Backtick command substitution
        hostile_title = f"Title `touch {canary_file}`"
        title_file.write_text(hostile_title)

        code, stdout, stderr = run_slug_from_file(
            str(title_file),
            str(REPO_ROOT / "scripts" / "slugify.sh")
        )

        if canary_file.exists():
            return False, "backtick substitution attempted to execute"

        return True, ""


def test_hostile_title_command_substitution():
    """Test that $(...) command substitution in a title doesn't execute."""
    with tempfile.TemporaryDirectory() as tmpdir:
        title_file = Path(tmpdir) / "title.txt"
        canary_file = Path(tmpdir) / "CANARY"

        # $(...) command substitution
        hostile_title = f"Title $(touch {canary_file})"
        title_file.write_text(hostile_title)

        code, stdout, stderr = run_slug_from_file(
            str(title_file),
            str(REPO_ROOT / "scripts" / "slugify.sh")
        )

        if canary_file.exists():
            return False, "$(...) substitution attempted to execute"

        return True, ""


def test_multiline_title_from_file():
    """Test that a multi-line title in a file produces a single-line slug."""
    with tempfile.TemporaryDirectory() as tmpdir:
        title_file = Path(tmpdir) / "title.txt"
        title_file.write_text("Line One\nLine Two\nLine Three")

        code, stdout, stderr = run_slug_from_file(
            str(title_file),
            str(REPO_ROOT / "scripts" / "slugify.sh")
        )

        slug = stdout.strip()
        if "\n" in slug or "\r" in slug:
            return False, f"slug contains newline: {slug!r}"

        if not re.match(r"^[a-z0-9-]{1,50}$|^plan$", slug):
            return False, f"invalid slug format: {slug!r}"

        return True, ""


def test_long_title_truncation():
    """Test that a long title is truncated to 50 characters."""
    with tempfile.TemporaryDirectory() as tmpdir:
        title_file = Path(tmpdir) / "title.txt"
        long_title = "A" * 100
        title_file.write_text(long_title)

        code, stdout, stderr = run_slug_from_file(
            str(title_file),
            str(REPO_ROOT / "scripts" / "slugify.sh")
        )

        slug = stdout.strip()

        # Should be at most 50 characters
        if len(slug) > 50:
            return False, f"slug length {len(slug)} exceeds 50"

        # Should not have trailing dash
        if slug.endswith("-"):
            return False, f"slug has trailing dash: {slug!r}"

        return True, ""


def test_empty_title_file():
    """Test that an empty title file produces 'plan'."""
    with tempfile.TemporaryDirectory() as tmpdir:
        title_file = Path(tmpdir) / "title.txt"
        title_file.write_text("")

        code, stdout, stderr = run_slug_from_file(
            str(title_file),
            str(REPO_ROOT / "scripts" / "slugify.sh")
        )

        slug = stdout.strip()
        if slug != "plan":
            return False, f"expected 'plan' for empty file, got {slug!r}"

        return True, ""


def test_title_with_special_characters():
    """Test that titles with special characters produce valid slugs."""
    with tempfile.TemporaryDirectory() as tmpdir:
        title_file = Path(tmpdir) / "title.txt"
        title_file.write_text("Fix Bug! (Security-Related) [URGENT]")

        code, stdout, stderr = run_slug_from_file(
            str(title_file),
            str(REPO_ROOT / "scripts" / "slugify.sh")
        )

        slug = stdout.strip()

        # All should be lowercase and only contain a-z0-9 and dash
        if not re.match(r"^[a-z0-9-]{1,50}$|^plan$", slug):
            return False, f"invalid slug format: {slug!r}"

        # Should not contain any of the original special chars
        if any(c in slug for c in "!()[]"):
            return False, f"slug contains special characters: {slug!r}"

        return True, ""


def main():
    h = Harness("SLUGIFY INTEGRATION TEST SUITE")

    print()
    print("[Section 1] File-based title reading")

    passed, msg = test_title_from_file()
    h.test_result("title from file is correctly slugified", passed, msg)

    print()
    print("[Section 2] Missing file fallback")

    passed, msg = test_missing_title_file_fallback()
    h.test_result("missing title file falls back to 'plan' with warning", passed, msg)

    print()
    print("[Section 3] Missing script fallback")

    passed, msg = test_missing_script_fallback()
    h.test_result("missing script falls back to 'plan' with warning", passed, msg)

    print()
    print("[Section 4] Security: hostile titles")

    passed, msg = test_hostile_title_from_file()
    h.test_result("quoted double-dash title does not execute", passed, msg)

    passed, msg = test_hostile_title_backtick()
    h.test_result("backtick substitution in title does not execute", passed, msg)

    passed, msg = test_hostile_title_command_substitution()
    h.test_result("$(...) substitution in title does not execute", passed, msg)

    print()
    print("[Section 5] Input transformations")

    passed, msg = test_multiline_title_from_file()
    h.test_result("multi-line title produces single-line slug", passed, msg)

    passed, msg = test_long_title_truncation()
    h.test_result("long title is truncated to 50 characters max", passed, msg)

    passed, msg = test_empty_title_file()
    h.test_result("empty title file falls back to 'plan'", passed, msg)

    print()
    print("[Section 6] Special characters")

    passed, msg = test_title_with_special_characters()
    h.test_result("title with special characters produces valid slug", passed, msg)

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
