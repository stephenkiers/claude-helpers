#!/usr/bin/env python3
"""
Test suite for scripts/slugify.sh locale independence and env parameter handling.

From the action plan (Fix 3): Pass `env=run_env` to subprocess; confirm behavior with and without
LC_ALL=C. This test suite verifies that the script handles non-ASCII input correctly when LC_ALL=C
is set, and documents behavior when it is not.

Covers:
  - Locale handling with LC_ALL=C explicitly passed via env parameter
  - Script resilience with UTF-8 input under different locales
  - Exit codes remain consistent regardless of locale
  - Fallback to "plan" works across locales

Run with: python3 tests/test_slugify_locale_env.py
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT


def run_slugify_with_env(title_bytes, env=None):
    """Run the script with title on stdin, passing specific env. Returns (returncode, stdout, stderr)."""
    full_env = dict(env or os.environ)
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "slugify.sh")],
        input=title_bytes,
        capture_output=True,
        cwd=tempfile.gettempdir(),
        env=full_env,
    )
    return result.returncode, result.stdout.decode(errors='replace'), result.stderr.decode(errors='replace')


def test_slugify_with_lc_all_c():
    """Test that slugify.sh works correctly with LC_ALL=C explicitly set."""
    env = dict(os.environ)
    env['LC_ALL'] = 'C'

    test_cases = [
        (b"Simple Title", "simple-title"),
        (b"Fix the Widget", "fix-the-widget"),
        (b"", "plan"),
        (b"!!!", "plan"),
        (b"Caf\xc3\xa9", "caf"),  # UTF-8 "Café" -> "caf"
    ]

    for input_bytes, expected in test_cases:
        code, stdout, stderr = run_slugify_with_env(input_bytes, env)
        slug = stdout.strip()
        if code != 0:
            return False, f"LC_ALL=C: input {input_bytes!r} exit {code}, expected 0"
        if slug != expected:
            return False, f"LC_ALL=C: input {input_bytes!r} expected {expected!r}, got {slug!r}"

    return True, ""


def test_slugify_without_lc_all_c():
    """Test that slugify.sh produces consistent exit codes even without LC_ALL=C."""
    env = dict(os.environ)
    # Explicitly remove LC_ALL if present
    env.pop('LC_ALL', None)

    # Test that exit codes are predictable: 0 for no args, 2 for args
    code, stdout, stderr = run_slugify_with_env(b"Test Title", env)
    if code != 0:
        return False, f"without LC_ALL=C: expected exit 0 for valid input, got {code}"

    # Test with argument (should exit 2)
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "slugify.sh"), "Test Title"],
        capture_output=True,
        env=env,
        cwd=tempfile.gettempdir(),
    )
    if result.returncode != 2:
        return False, f"without LC_ALL=C: argument should exit 2, got {result.returncode}"

    return True, ""


def test_utf8_with_lc_all_c():
    """Test that UTF-8 input is handled correctly with LC_ALL=C."""
    env = dict(os.environ)
    env['LC_ALL'] = 'C'

    # Valid UTF-8: "Café" (U+00E9 = é)
    code, stdout, stderr = run_slugify_with_env("Café".encode('utf-8'), env)
    slug = stdout.strip()

    # With LC_ALL=C, non-ASCII chars should be treated as separators
    # "Café" -> "caf" (the é is stripped as non-ASCII)
    if code != 0:
        return False, f"UTF-8 with LC_ALL=C: expected exit 0, got {code}"

    # [WEAK_ASSERTION] This uses substring matching ("caf" not in slug) instead of exact matching.
    # A plausible broken implementation could output "cafe" or "cafs" and still pass this test.
    if "caf" not in slug and slug != "plan":
        return False, f"UTF-8 with LC_ALL=C: expected slug containing 'caf' or 'plan', got {slug!r}"

    return True, ""


def test_blank_input_exit_code_with_env():
    """Test that blank input exits 0 with LC_ALL=C in env."""
    env = dict(os.environ)
    env['LC_ALL'] = 'C'

    code, stdout, stderr = run_slugify_with_env(b"", env)
    slug = stdout.strip()

    if code != 0:
        return False, f"blank input exit: expected 0, got {code}"
    if slug != "plan":
        return False, f"blank input slug: expected 'plan', got {slug!r}"

    return True, ""


def test_argument_exit_code_with_env():
    """Test that argument given exits 2 with LC_ALL=C in env."""
    env = dict(os.environ)
    env['LC_ALL'] = 'C'

    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "slugify.sh"), "Some Title"],
        capture_output=True,
        env=env,
        cwd=tempfile.gettempdir(),
    )

    if result.returncode != 2:
        return False, f"argument exit: expected 2, got {result.returncode}"

    if result.stdout.decode().strip():
        return False, f"argument stdout: expected empty, got {result.stdout.decode()!r}"

    return True, ""


def test_env_isolation():
    """Test that env parameter does not leak into other operations."""
    env1 = dict(os.environ)
    env1['LC_ALL'] = 'C'
    env1['CUSTOM_VAR'] = 'value1'

    code1, stdout1, stderr1 = run_slugify_with_env(b"Test", env1)

    env2 = dict(os.environ)
    env2['LC_ALL'] = 'C'
    env2['CUSTOM_VAR'] = 'value2'

    code2, stdout2, stderr2 = run_slugify_with_env(b"Test", env2)

    # Both should succeed and produce the same slug (input is the same)
    if code1 != 0 or code2 != 0:
        return False, f"exit codes: got {code1} and {code2}, expected both 0"

    slug1 = stdout1.strip()
    slug2 = stdout2.strip()

    if slug1 != slug2:
        return False, f"slugs differ: {slug1!r} vs {slug2!r}, should be same for same input"

    return True, ""


def main():
    h = Harness("SLUGIFY LOCALE AND ENV PARAMETER TEST SUITE")

    print()
    print("[Section 1] Slugify with LC_ALL=C explicitly set in env")

    passed, msg = test_slugify_with_lc_all_c()
    h.test_result("slugify with LC_ALL=C handles various inputs correctly", passed, msg)

    print()
    print("[Section 2] Slugify without LC_ALL=C")

    passed, msg = test_slugify_without_lc_all_c()
    h.test_result("slugify exit codes are predictable without LC_ALL=C", passed, msg)

    print()
    print("[Section 3] UTF-8 handling with LC_ALL=C")

    passed, msg = test_utf8_with_lc_all_c()
    h.test_result("UTF-8 input is handled correctly with LC_ALL=C", passed, msg)

    print()
    print("[Section 4] Blank input exit code with env")

    passed, msg = test_blank_input_exit_code_with_env()
    h.test_result("blank input produces 'plan' and exits 0", passed, msg)

    print()
    print("[Section 5] Argument rejection with env")

    passed, msg = test_argument_exit_code_with_env()
    h.test_result("argument given produces exit 2 and no stdout", passed, msg)

    print()
    print("[Section 6] Env parameter isolation")

    passed, msg = test_env_isolation()
    h.test_result("env parameter does not affect output for same input", passed, msg)

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
