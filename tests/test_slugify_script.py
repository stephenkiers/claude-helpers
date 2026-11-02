#!/usr/bin/env python3
"""
Test suite for the scripts/slugify.sh script (#389).

What this proves:
- The script reads the title from stdin only, never from argv.
- It rejects any arguments with exit code 2.
- Hostile payloads (quotes, backticks, $(), semicolons, newlines, etc.) cannot escape and execute.
- Every output matches the expected shape: ^[a-z0-9]([a-z0-9-]{0,48}[a-z0-9])?$ or "plan".
- Empty and all-symbol inputs give "plan".
- A 300-character title gives at most 50 characters with no trailing dash.
- A no-trailing-newline title works correctly.
- Invalid UTF-8 is handled without crashing.
- The pipeline is identical to the #377 inline version (parity test in test_slug_pipeline.py).

Run with: python3 tests/test_slugify_script.py
"""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "slugify.sh"

# Regex for a valid slug: [a-z0-9]([a-z0-9-]{0,48}[a-z0-9])? or exactly "plan"
SLUG_PATTERN = re.compile(r"^([a-z0-9]([a-z0-9-]{0,48}[a-z0-9])?|plan)$")


def run_slugify(title_bytes, env=None):
    """
    Run the script with title_bytes on stdin.
    Returns (returncode, stdout, stderr).
    Do not force LC_ALL so the script's internal handling is tested.
    """
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        input=title_bytes,
        capture_output=True,
        cwd=tempfile.gettempdir(),
        env=run_env,
    )
    return result.returncode, result.stdout, result.stderr


def run_slugify_with_arg(arg, env=None):
    """Run the script with an argument (should fail with exit 2)."""
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    result = subprocess.run(
        ["bash", str(SCRIPT), arg],
        capture_output=True,
        cwd=tempfile.gettempdir(),
        env=run_env,
    )
    return result.returncode, result.stdout, result.stderr


def main():
    h = Harness("SLUGIFY.SH SCRIPT TEST SUITE")

    if not SCRIPT.exists():
        h.test_result(
            "scripts/slugify.sh exists",
            False,
            f"{SCRIPT} not found",
        )
        h.summarize_and_exit()

    # Test 1: Basic benign titles
    benign_cases = [
        (b"Fix: Thing", "fix-thing"),
        (b"abc", "abc"),
        (b"a  --  b", "a-b"),
        (b"FIX THE WIDGET", "fix-the-widget"),
        (b"Fix the Widget (v2)!", "fix-the-widget-v2"),
    ]

    for title_bytes, expected in benign_cases:
        returncode, stdout, stderr = run_slugify(title_bytes)
        h.test_result(
            f"benign {title_bytes!r} -> {expected!r}",
            returncode == 0 and stdout.decode() == expected + "\n",
            f"returncode={returncode}, stdout={stdout!r}" if returncode != 0 or stdout.decode() != expected + "\n" else "",
        )

    # Test 2: Hostile payloads with canaries (each creates a temp dir with a CANARY)
    hostile_cases = [
        (b'x"; touch CANARY; "', "x-touch-canary"),
        (b"`touch CANARY`", "touch-canary"),
        (b"$(touch CANARY)", "touch-canary"),
        (b"'", "plan"),
        (b"\\", "plan"),
        (b";", "plan"),
        (b"\nrm -rf x", "rm-rf-x"),
        (b"-leading", "leading"),
        (b"../../etc", "etc"),
    ]

    for title_bytes, expected in hostile_cases:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a canary file path
            canary_path = Path(tmpdir) / "CANARY"
            # Run the script in that temp dir
            result = subprocess.run(
                ["bash", str(SCRIPT)],
                input=title_bytes,
                capture_output=True,
                cwd=tmpdir,
            )
            returncode = result.returncode
            stdout = result.stdout.decode()
            canary_exists = canary_path.exists()

            h.test_result(
                f"hostile {title_bytes!r} produces slug, no canary created",
                returncode == 0 and not canary_exists and stdout == expected + "\n",
                f"canary_exists={canary_exists}, returncode={returncode}, stdout={stdout!r}" if (
                    returncode != 0 or canary_exists or stdout != expected + "\n"
                ) else "",
            )

    # Test 3: Empty and all-symbol inputs give "plan"
    empty_cases = [
        (b"", "empty stdin"),
        (b"!!!", "all-symbol input"),
        ("😀😁😂".encode('utf-8'), "emoji-only input"),
    ]

    for title_bytes, label in empty_cases:
        returncode, stdout, stderr = run_slugify(title_bytes)
        h.test_result(
            f"{label} -> plan",
            returncode == 0 and stdout.decode() == "plan\n",
            f"returncode={returncode}, stdout={stdout!r}" if returncode != 0 or stdout.decode() != "plan\n" else "",
        )

    # Test 4: Long title (300 chars) produces at most 50 chars, no trailing dash
    long_title = ("a" * 300).encode()
    returncode, stdout, stderr = run_slugify(long_title)
    slug = stdout.decode().strip()
    h.test_result(
        "300-char title produces at most 50 chars, no trailing dash",
        returncode == 0 and len(slug) <= 50 and not slug.endswith("-"),
        f"returncode={returncode}, slug={slug!r}, len={len(slug)}" if (
            returncode != 0 or len(slug) > 50 or slug.endswith("-")
        ) else "",
    )

    # Test 5: Multi-line title produces single-line slug
    multiline_title = b"Line 1\nLine 2\nLine 3"
    returncode, stdout, stderr = run_slugify(multiline_title)
    slug = stdout.decode()
    h.test_result(
        "multi-line title produces single-line slug",
        returncode == 0 and "\n" not in slug.rstrip("\n"),
        f"returncode={returncode}, slug={slug!r}" if returncode != 0 or "\n" in slug.rstrip("\n") else "",
    )

    # Test 6: No-trailing-newline fixture
    no_newline_title = b"Fix the Widget (v2)!"  # No \n at the end
    returncode, stdout, stderr = run_slugify(no_newline_title)
    slug = stdout.decode().strip()
    h.test_result(
        "no-trailing-newline title 'Fix the Widget (v2)!' -> fix-the-widget-v2",
        returncode == 0 and slug == "fix-the-widget-v2",
        f"returncode={returncode}, slug={slug!r}" if returncode != 0 or slug != "fix-the-widget-v2" else "",
    )

    # Test 7: Invalid UTF-8 with default locale (no LC_ALL set)
    invalid_utf8 = b"\xff\xfeFix it"
    returncode, stdout, stderr = run_slugify(invalid_utf8)
    slug = stdout.decode().strip()
    h.test_result(
        "invalid UTF-8 with default locale doesn't crash, gives fix-it",
        returncode == 0 and slug == "fix-it",
        f"returncode={returncode}, slug={slug!r}, stderr={stderr!r}" if returncode != 0 or slug != "fix-it" else "",
    )

    # Test 8: Invalid UTF-8 with UTF-8 locale set (should still work due to script's LC_ALL=C)
    returncode, stdout, stderr = run_slugify(invalid_utf8, env={"LC_ALL": "en_US.UTF-8"})
    slug = stdout.decode().strip()
    h.test_result(
        "invalid UTF-8 even with LC_ALL=en_US.UTF-8 gives fix-it (script sets LC_ALL=C)",
        returncode == 0 and slug == "fix-it",
        f"returncode={returncode}, slug={slug!r}" if returncode != 0 or slug != "fix-it" else "",
    )

    # Test 9: Shape regex on all outputs
    test_cases = benign_cases + [(title, exp) for title, exp in hostile_cases]
    for title_bytes, expected in test_cases:
        returncode, stdout, stderr = run_slugify(title_bytes)
        slug = stdout.decode().strip()
        h.test_result(
            f"shape check: {expected!r} matches slug pattern",
            bool(returncode == 0 and SLUG_PATTERN.match(slug)),
            f"returncode={returncode}, slug={slug!r}" if not bool(returncode == 0 and SLUG_PATTERN.match(slug)) else "",
        )

    # Test 10: Argv rejection
    returncode, stdout, stderr = run_slugify_with_arg("Some Title")
    h.test_result(
        "argument rejection: exit code 2",
        returncode == 2,
        f"returncode={returncode}" if returncode != 2 else "",
    )
    h.test_result(
        "argument rejection: no stdout",
        stdout == b"",
        f"stdout={stdout!r}" if stdout != b"" else "",
    )
    h.test_result(
        "argument rejection: correct stderr message",
        b"takes no arguments" in stderr,
        f"stderr={stderr!r}" if b"takes no arguments" not in stderr else "",
    )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
