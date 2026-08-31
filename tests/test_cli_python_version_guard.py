#!/usr/bin/env python3
"""
Test suite for Python 3.8+ version guard in scripts/workflow/cli.py.

This test suite verifies that:
1. scripts/workflow/cli.py's main() function includes a sys.version_info >= (3, 8) guard
2. The guard exits early with a clear error message before any other work happens
3. Error messages mention Python 3.8+ requirement
4. When run with current Python (3.8+), the guard doesn't block normal operation

Run with: python3 tests/test_cli_python_version_guard.py
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _test_harness import Harness, REPO_ROOT


def run_python_with_monkeypatch(version_string, code_to_run):
    """
    Run Python code with a monkeypatched sys.version_info.

    Returns (returncode, stdout, stderr).
    """
    # Create a wrapper script that monkeypatches sys.version_info before running code
    wrapper = f"""
import sys
from types import SimpleNamespace

# Parse version string like "3.6.0" into a version_info tuple
version_parts = {repr(version_string)}.split('.')
major = int(version_parts[0])
minor = int(version_parts[1]) if len(version_parts) > 1 else 0
micro = int(version_parts[2]) if len(version_parts) > 2 else 0

# Create a fake sys.version_info that behaves like a tuple
# This allows code checking sys.version_info >= (3, 8) to work
class FakeVersionInfo(tuple):
    def __new__(cls, major, minor, micro):
        return super().__new__(cls, (major, minor, micro, 'final', 0))

    def __init__(self, major, minor, micro):
        self.major = major
        self.minor = minor
        self.micro = micro

    def __ge__(self, other):
        return (self.major, self.minor, self.micro) >= other

    def __lt__(self, other):
        return (self.major, self.minor, self.micro) < other

    def __le__(self, other):
        return (self.major, self.minor, self.micro) <= other

    def __gt__(self, other):
        return (self.major, self.minor, self.micro) > other

sys.version_info = FakeVersionInfo(major, minor, micro)

# Run the user's code
{code_to_run}
"""

    result = subprocess.run(
        [sys.executable, "-c", wrapper],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    return result.returncode, result.stdout, result.stderr


def test_version_guard_blocks_old_python():
    """main() should exit early when sys.version_info < (3, 8)."""
    code = """
from scripts.workflow.cli import main
try:
    main()
except SystemExit as e:
    sys.exit(e.code)
"""

    returncode, stdout, stderr = run_python_with_monkeypatch("3.6.0", code)

    # Should exit with non-zero code
    if returncode == 0:
        return False, "main() should exit with non-zero code when Python < 3.8"

    # Should have an error message in stderr mentioning Python requirement
    stderr_combined = stderr.lower()
    if "3.8" not in stderr_combined and "python" not in stderr_combined:
        return False, f"expected version requirement error in stderr, got: {stderr}"

    return True, ""


def test_version_guard_blocks_python_37():
    """main() should block Python 3.7 specifically."""
    code = """
from scripts.workflow.cli import main
try:
    main()
except SystemExit as e:
    sys.exit(e.code)
"""

    returncode, stdout, stderr = run_python_with_monkeypatch("3.7.0", code)

    # Should exit with non-zero code
    if returncode == 0:
        return False, "main() should exit with non-zero code when Python = 3.7"

    # Should have an error message mentioning the requirement
    if "3.8" not in stderr and "python" not in stderr.lower():
        return False, f"expected version requirement error in stderr, got: {stderr}"

    return True, ""


def test_version_guard_allows_python_38():
    """main() should allow Python 3.8 to proceed past the version guard."""
    code = """
import sys
from scripts.workflow.cli import main

try:
    main()
except SystemExit:
    pass
"""

    returncode, stdout, stderr = run_python_with_monkeypatch("3.8.0", code)

    # If the version guard blocked Python 3.8, stderr would contain the version guard error message.
    # If the guard passed, stderr would be empty (argparse help printed to stdout, not stderr).
    stderr_lower = stderr.lower()
    if "3.8" in stderr_lower or "required" in stderr_lower:
        return False, f"version guard should not block Python 3.8, but got stderr: {stderr}"

    # If argparse usage is printed to stdout (not stderr) and we get exit code 1 or 2, that's fine
    # (it means the guard passed but argparse failed on missing subcommand)
    if "usage" in stdout.lower():
        return True, ""

    # If we got any other output, that's unexpected
    if stdout or stderr:
        return False, f"unexpected output when calling main() with Python 3.8. stdout: {stdout}, stderr: {stderr}"

    return True, ""


def test_version_guard_allows_python_39():
    """main() should allow Python 3.9 to proceed past the version guard."""
    code = """
import sys
from scripts.workflow.cli import main

try:
    main()
except SystemExit:
    pass
"""

    returncode, stdout, stderr = run_python_with_monkeypatch("3.9.0", code)

    # If the version guard blocked Python 3.9, stderr would contain the version guard error message.
    # If the guard passed, stderr would be empty (argparse help printed to stdout, not stderr).
    stderr_lower = stderr.lower()
    if "3.8" in stderr_lower or "required" in stderr_lower:
        return False, f"version guard should not block Python 3.9, but got stderr: {stderr}"

    # If argparse usage is printed to stdout (not stderr) and we get exit code 1 or 2, that's fine
    # (it means the guard passed but argparse failed on missing subcommand)
    if "usage" in stdout.lower():
        return True, ""

    # If we got any other output, that's unexpected
    if stdout or stderr:
        return False, f"unexpected output when calling main() with Python 3.9. stdout: {stdout}, stderr: {stderr}"

    return True, ""


def test_version_guard_allows_python_310():
    """main() should allow Python 3.10 to proceed past the version guard."""
    code = """
import sys
from scripts.workflow.cli import main

try:
    main()
except SystemExit:
    pass
"""

    returncode, stdout, stderr = run_python_with_monkeypatch("3.10.0", code)

    # If the version guard blocked Python 3.10, stderr would contain the version guard error message.
    # If the guard passed, stderr would be empty (argparse help printed to stdout, not stderr).
    stderr_lower = stderr.lower()
    if "3.8" in stderr_lower or "required" in stderr_lower:
        return False, f"version guard should not block Python 3.10, but got stderr: {stderr}"

    # If argparse usage is printed to stdout (not stderr) and we get exit code 1 or 2, that's fine
    # (it means the guard passed but argparse failed on missing subcommand)
    if "usage" in stdout.lower():
        return True, ""

    # If we got any other output, that's unexpected
    if stdout or stderr:
        return False, f"unexpected output when calling main() with Python 3.10. stdout: {stdout}, stderr: {stderr}"

    return True, ""


def test_version_check_happens_early_in_main():
    """The version guard should be near the start of main(), before imports."""
    code = """
import sys
import unittest.mock as mock

# Patch a common operation to detect if it runs
with mock.patch('sys.argv', ['cli', 'cleanup']):
    with mock.patch('argparse.ArgumentParser.parse_args', side_effect=RuntimeError("Should not reach parse_args")):
        try:
            from scripts.workflow.cli import main
            main()
        except RuntimeError as e:
            if "Should not reach" in str(e):
                # If we hit parse_args, the version guard didn't block us early enough
                sys.exit(2)
            raise
        except SystemExit as e:
            # Expected: version guard should cause early exit
            sys.exit(e.code if e.code else 0)
"""

    returncode, stdout, stderr = run_python_with_monkeypatch("3.6.0", code)

    # Should NOT exit with code 2 (which would mean parse_args was reached)
    if returncode == 2:
        return False, "version guard did not block before parse_args"

    # Should exit with non-zero (and not 2)
    if returncode == 0:
        return False, "main() should exit non-zero for old Python"

    return True, ""


def test_version_error_mentions_affected_commands():
    """The version error should mention which commands are affected."""
    code = """
from scripts.workflow.cli import main
try:
    main()
except SystemExit:
    pass
"""

    returncode, stdout, stderr = run_python_with_monkeypatch("3.6.0", code)

    # Stderr must be present
    if not stderr:
        return False, "main() should print error to stderr for old Python"

    # Should mention Python 3.8+ requirement
    stderr_lower = stderr.lower()
    if "3.8" not in stderr_lower:
        return False, f"expected '3.8' version requirement in stderr, got: {stderr}"

    # Should mention at least one of the affected commands
    affected_commands = ["track-and-start", "shipit", "cleanup", "merge-and-cleanup"]
    if not any(cmd in stderr for cmd in affected_commands):
        return False, f"expected affected commands (e.g., '/track-and-start', '/shipit') in stderr, got: {stderr}"

    # Should indicate that Python is required
    if "required" not in stderr_lower and "require" not in stderr_lower:
        return False, f"expected 'required' or 'require' in error message, got: {stderr}"

    return True, ""


def test_direct_import_and_call_with_old_python():
    """Directly importing and calling main with old Python should fail early."""
    code = """
from scripts.workflow.cli import main

# Call main (will fail due to version check, which should happen first)
try:
    main()
except SystemExit as e:
    sys.exit(e.code if e.code else 1)
"""

    returncode, stdout, stderr = run_python_with_monkeypatch("3.6.0", code)

    # Must exit non-zero
    if returncode == 0:
        return False, "main() should exit non-zero when Python < 3.8"

    # Should have an error message
    if not stderr:
        return False, "expected error message in stderr for old Python"

    return True, ""


if __name__ == "__main__":
    h = Harness("CLI PYTHON 3.8+ VERSION GUARD TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Version guard blocks old Python (< 3.8)")
    passed, msg = test_version_guard_blocks_old_python()
    test_result("main() exits non-zero when Python < 3.8", passed, msg)

    passed, msg = test_version_guard_blocks_python_37()
    test_result("main() blocks Python 3.7 specifically", passed, msg)

    print()
    print("[Section 2] Version guard allows Python 3.8+")
    passed, msg = test_version_guard_allows_python_38()
    test_result("main() allows Python 3.8", passed, msg)

    passed, msg = test_version_guard_allows_python_39()
    test_result("main() allows Python 3.9", passed, msg)

    passed, msg = test_version_guard_allows_python_310()
    test_result("main() allows Python 3.10", passed, msg)

    print()
    print("[Section 3] Guard placement and timing")
    passed, msg = test_version_check_happens_early_in_main()
    test_result("version check happens early, before parsing arguments", passed, msg)

    passed, msg = test_version_error_mentions_affected_commands()
    test_result("version error is present in stderr", passed, msg)

    print()
    print("[Section 4] Direct import and call")
    passed, msg = test_direct_import_and_call_with_old_python()
    test_result("direct call to main() exits on old Python", passed, msg)

    print()
    h.summarize_and_exit()
