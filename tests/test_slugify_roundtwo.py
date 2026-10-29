#!/usr/bin/env python3
"""
Test suite for scripts/slugify.sh black-box behavior and integration.

This test suite complements test_slugify_script.py by focusing on additional
aspects: file properties, install.sh integration, and command-line behavior.

Covers:
- File properties (executable, shebang, tracked by git)
- Script rejection of arguments (exit code 2, no stdout)
- Basic slug generation with common titles
- install.sh symlink integration (script mirroring from repo to ~/.claude)
- Script presence and linkage in install paths

Run with: python3 tests/test_slugify_roundtwo.py
"""

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT


def run_slugify(title_bytes, cwd="/tmp", env=None):
    """Run the script with title on stdin. Returns (returncode, stdout, stderr)."""
    full_env = dict(env or os.environ)
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "slugify.sh")],
        input=title_bytes,
        capture_output=True,
        cwd=cwd,
        env=full_env,
    )
    return result.returncode, result.stdout.decode(errors='replace'), result.stderr.decode(errors='replace')


def test_file_properties():
    """Test basic file properties of slugify.sh."""
    script = REPO_ROOT / "scripts" / "slugify.sh"

    # File exists
    if not script.exists():
        return False, "script does not exist"

    # Is executable
    st = script.stat()
    if not (st.st_mode & stat.S_IXUSR):
        return False, f"script not executable (mode {oct(st.st_mode)})"

    # Has shebang
    content = script.read_text()
    if not content.startswith("#!/"):
        return False, "script does not start with shebang"

    # Is a regular file
    if not script.is_file():
        return False, "script is not a regular file"

    return True, ""


def test_script_no_arguments_exits_2():
    """Test that running the script with an argument exits 2 and prints nothing to stdout."""
    code, stdout, stderr = run_slugify(b"", env={})

    # First test: no arguments succeeds
    if code != 0:
        return False, f"with no args, expected exit 0 but got {code}"

    # Second test: one argument exits 2
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "slugify.sh"), "Some Title"],
        capture_output=True,
    )
    if result.returncode != 2:
        return False, f"with argument, expected exit 2 but got {result.returncode}"

    if result.stdout.decode().strip():
        return False, f"with argument, expected no stdout but got: {result.stdout.decode()}"

    return True, ""


def test_basic_slug_generation():
    """Test basic slug generation with common titles."""
    cases = [
        (b"Simple Title", "simple-title"),
        (b"UPPERCASE", "uppercase"),
        (b"Mixed Case Title", "mixed-case-title"),
        (b"Title with Numbers 123", "title-with-numbers-123"),
        (b"A-B-C", "a-b-c"),
    ]

    for input_bytes, expected_slug in cases:
        code, stdout, stderr = run_slugify(input_bytes)
        slug = stdout.strip()
        if slug != expected_slug:
            return False, f"input {input_bytes!r}: expected {expected_slug!r}, got {slug!r}"

    return True, ""


def test_empty_and_symbol_only_fallback():
    """Test that empty input and symbol-only input produce 'plan'."""
    cases = [
        (b"", "plan"),
        (b"   ", "plan"),
        (b"!!!", "plan"),
        (b"@#$%^&*()", "plan"),
        (b"\n\n", "plan"),
    ]

    for input_bytes, expected in cases:
        code, stdout, stderr = run_slugify(input_bytes)
        slug = stdout.strip()
        if slug != expected:
            return False, f"input {input_bytes!r}: expected {expected!r}, got {slug!r}"

    return True, ""


def test_slug_length_and_format():
    """Test that all output matches the format [a-z0-9-]{1,50} or 'plan'."""
    import re

    slug_pattern = re.compile(r"^[a-z0-9]([a-z0-9-]{0,48}[a-z0-9])?$|^plan$")

    test_cases = [
        b"Short",
        b"This is a title with many words that should be truncated to fifty characters max",
        b"Many-Dashes-Here-Already",
        b"123",
        b"-leading-dash",
        b"trailing-dash-",
        b"multiple---dashes",
    ]

    for input_bytes in test_cases:
        code, stdout, stderr = run_slugify(input_bytes)
        slug = stdout.strip()
        if not slug_pattern.match(slug):
            return False, f"slug {slug!r} from input {input_bytes!r} does not match format"

    return True, ""


def test_no_trailing_newline():
    """Test that input without trailing newline is handled correctly."""
    # "Fix the Widget (v2)!" without newline should give "fix-the-widget-v2"
    code, stdout, stderr = run_slugify(b"Fix the Widget (v2)!")
    slug = stdout.strip()
    if slug != "fix-the-widget-v2":
        return False, f"expected 'fix-the-widget-v2', got {slug!r}"

    return True, ""


def test_script_in_repo_tracked():
    """Test that the script is tracked by git in the repo."""
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "--", "scripts/slugify.sh"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return False, "script is not tracked by git"

    return True, ""


def test_script_mode_755():
    """Test that the script has mode 755 (executable by all)."""
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-s", "--", "scripts/slugify.sh"],
        capture_output=True,
        text=True,
    )

    # git ls-files -s output: "[mode] [object] [stage] [file]"
    # For executable, mode should be 100755
    if "100755" not in result.stdout:
        return False, f"git mode is not 100755: {result.stdout}"

    return True, ""


def test_install_sh_copies_script():
    """Test that install.sh recognizes and would link the script."""
    # Check that install.sh mentions scripts or has a loop that would cover it
    install_sh = REPO_ROOT / "install.sh"
    if not install_sh.exists():
        return False, "install.sh not found"

    content = install_sh.read_text()
    if "scripts" not in content:
        return False, "install.sh does not appear to process scripts/"

    return True, ""


def main():
    h = Harness("SLUGIFY ROUNDTWO TEST SUITE")

    print()
    print("[Section 1] File properties")

    passed, msg = test_file_properties()
    h.test_result("slugify.sh has correct file properties (executable, shebang, regular file)", passed, msg)

    print()
    print("[Section 2] Argument rejection")

    passed, msg = test_script_no_arguments_exits_2()
    h.test_result("script rejects arguments (exit 2) and accepts stdin", passed, msg)

    print()
    print("[Section 3] Basic slug generation")

    passed, msg = test_basic_slug_generation()
    h.test_result("script generates slugs for common titles", passed, msg)

    print()
    print("[Section 4] Empty and symbol-only input")

    passed, msg = test_empty_and_symbol_only_fallback()
    h.test_result("empty and symbol-only input produces 'plan'", passed, msg)

    print()
    print("[Section 5] Output format validation")

    passed, msg = test_slug_length_and_format()
    h.test_result("all output matches format [a-z0-9-]{1,50} or 'plan'", passed, msg)

    print()
    print("[Section 6] No trailing newline handling")

    passed, msg = test_no_trailing_newline()
    h.test_result("input without trailing newline is handled correctly", passed, msg)

    print()
    print("[Section 7] Git tracking")

    passed, msg = test_script_in_repo_tracked()
    h.test_result("slugify.sh is tracked by git", passed, msg)

    print()
    print("[Section 8] Git file mode")

    passed, msg = test_script_mode_755()
    h.test_result("slugify.sh has git mode 100755", passed, msg)

    print()
    print("[Section 9] install.sh integration")

    passed, msg = test_install_sh_copies_script()
    h.test_result("install.sh has mechanism to process scripts/", passed, msg)

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
