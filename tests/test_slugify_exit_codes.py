#!/usr/bin/env python3
"""
Test suite for scripts/slugify.sh exit code contract.

From the action plan (Fix 7): The script header should document its exit code contract:
  Exit: 0 always prints a slug (blank input -> "plan"); 2 if any argument is given (nothing on stdout).

This test suite verifies the exit code behavior matches the contract specification.

Covers:
  - Exit 0: always returns a slug, including fallback "plan"
  - Exit 2: triggered only by arguments, with empty stdout
  - Exit codes are consistent regardless of input content
  - Failure modes (if any) are documented

Run with: python3 tests/test_slugify_exit_codes.py
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT


def test_exit_0_with_valid_input():
    """Test that valid stdin produces exit 0."""
    test_cases = [
        b"Simple Title",
        b"",
        b"   ",
        b"Title with 123 numbers",
        b"Special-Chars!@#$%^&*()",
    ]

    for input_bytes in test_cases:
        result = subprocess.run(
            ["bash", str(REPO_ROOT / "scripts" / "slugify.sh")],
            input=input_bytes,
            capture_output=True,
        )
        if result.returncode != 0:
            return False, f"input {input_bytes!r}: expected exit 0, got {result.returncode}"

    return True, ""


def test_exit_2_with_argument():
    """Test that any argument triggers exit 2."""
    test_arguments = [
        "title",
        "Fix the Widget",
        "",
        "   ",
        "-x",
        "--help",
    ]

    for arg in test_arguments:
        result = subprocess.run(
            ["bash", str(REPO_ROOT / "scripts" / "slugify.sh"), arg],
            capture_output=True,
        )
        if result.returncode != 2:
            return False, f"arg {arg!r}: expected exit 2, got {result.returncode}"

    return True, ""


def test_exit_0_always_produces_output():
    """Test that exit 0 always produces at least some output (a slug)."""
    test_cases = [
        b"Normal Title",
        b"",  # fallback case
        b"!!!",  # symbol-only (fallback)
        b"   ",  # whitespace-only (fallback)
        b"\n",  # newline-only
    ]

    for input_bytes in test_cases:
        result = subprocess.run(
            ["bash", str(REPO_ROOT / "scripts" / "slugify.sh")],
            input=input_bytes,
            capture_output=True,
        )
        if result.returncode != 0:
            return False, f"input {input_bytes!r}: expected exit 0, got {result.returncode}"

        output = result.stdout.decode(errors='replace').strip()
        if not output:
            return False, f"input {input_bytes!r}: exit 0 but no output produced"

    return True, ""


def test_exit_2_produces_no_stdout():
    """Test that exit 2 produces no stdout."""
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "slugify.sh"), "Some Title"],
        capture_output=True,
    )

    if result.returncode != 2:
        return False, f"argument: expected exit 2, got {result.returncode}"

    stdout = result.stdout.decode(errors='replace').strip()
    if stdout:
        return False, f"argument: exit 2 should produce no stdout, got {stdout!r}"

    return True, ""


def test_exit_0_fallback_case():
    """Test that exit 0 fallback case produces 'plan' slug."""
    fallback_cases = [
        b"",
        b"   ",
        b"!!!",
        b"@#$%^&*()",
    ]

    for input_bytes in fallback_cases:
        result = subprocess.run(
            ["bash", str(REPO_ROOT / "scripts" / "slugify.sh")],
            input=input_bytes,
            capture_output=True,
        )

        if result.returncode != 0:
            return False, f"fallback {input_bytes!r}: expected exit 0, got {result.returncode}"

        output = result.stdout.decode(errors='replace').strip()
        if output != "plan":
            return False, f"fallback {input_bytes!r}: expected 'plan', got {output!r}"

    return True, ""


def test_exit_code_with_multiple_arguments():
    """Test that multiple arguments also trigger exit 2."""
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "slugify.sh"), "arg1", "arg2"],
        capture_output=True,
    )

    if result.returncode != 2:
        return False, f"multiple args: expected exit 2, got {result.returncode}"

    return True, ""


def test_exit_0_output_format():
    """Test that exit 0 output is always a valid slug."""
    import re

    slug_pattern = re.compile(r"^[a-z0-9]([a-z0-9-]{0,48}[a-z0-9])?$|^plan$")

    test_cases = [
        b"Test Title",
        b"UPPERCASE",
        b"with-dashes",
        b"123",
        b"a",
        b"",
    ]

    for input_bytes in test_cases:
        result = subprocess.run(
            ["bash", str(REPO_ROOT / "scripts" / "slugify.sh")],
            input=input_bytes,
            capture_output=True,
        )

        if result.returncode != 0:
            return False, f"input {input_bytes!r}: expected exit 0, got {result.returncode}"

        output = result.stdout.decode(errors='replace').strip()
        if not slug_pattern.match(output):
            return False, f"input {input_bytes!r}: output {output!r} doesn't match slug format"

    return True, ""


def test_exit_code_deterministic():
    """Test that exit codes are deterministic for the same input."""
    # Run the same input multiple times
    for _ in range(3):
        result = subprocess.run(
            ["bash", str(REPO_ROOT / "scripts" / "slugify.sh")],
            input=b"Test Title",
            capture_output=True,
        )
        if result.returncode != 0:
            return False, f"exit code not deterministic: expected 0, got {result.returncode}"

    # Run with argument multiple times
    for _ in range(3):
        result = subprocess.run(
            ["bash", str(REPO_ROOT / "scripts" / "slugify.sh"), "Test"],
            capture_output=True,
        )
        if result.returncode != 2:
            return False, f"exit code not deterministic: expected 2, got {result.returncode}"

    return True, ""


def main():
    h = Harness("SLUGIFY EXIT CODE CONTRACT TEST SUITE")

    print()
    print("[Section 1] Exit 0 with valid input (stdin)")

    passed, msg = test_exit_0_with_valid_input()
    h.test_result("exit 0 for all valid stdin inputs", passed, msg)

    print()
    print("[Section 2] Exit 2 with argument")

    passed, msg = test_exit_2_with_argument()
    h.test_result("exit 2 when any argument is given", passed, msg)

    print()
    print("[Section 3] Exit 0 always produces output")

    passed, msg = test_exit_0_always_produces_output()
    h.test_result("exit 0 always produces a slug", passed, msg)

    print()
    print("[Section 4] Exit 2 produces no stdout")

    passed, msg = test_exit_2_produces_no_stdout()
    h.test_result("exit 2 produces no stdout", passed, msg)

    print()
    print("[Section 5] Exit 0 fallback case")

    passed, msg = test_exit_0_fallback_case()
    h.test_result("exit 0 fallback produces 'plan'", passed, msg)

    print()
    print("[Section 6] Exit code with multiple arguments")

    passed, msg = test_exit_code_with_multiple_arguments()
    h.test_result("exit 2 with multiple arguments", passed, msg)

    print()
    print("[Section 7] Exit 0 output format validation")

    passed, msg = test_exit_0_output_format()
    h.test_result("exit 0 output always matches slug format", passed, msg)

    print()
    print("[Section 8] Exit code determinism")

    passed, msg = test_exit_code_deterministic()
    h.test_result("exit codes are deterministic across runs", passed, msg)

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
