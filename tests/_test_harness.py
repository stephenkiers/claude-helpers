"""Shared harness for this repo's invariant test scripts (no pytest dependency)."""

import sys
from pathlib import Path
from typing import NoReturn

REPO_ROOT = Path(__file__).resolve().parent.parent


class Harness:
    def __init__(self, title):
        """Initialise a named test suite and print its banner."""
        self.pass_count = 0
        self.fail_count = 0
        self.failures = []
        print("=" * len(title))
        print(title)
        print("=" * len(title))
        print()

    def test_result(self, test_name, passed, message=""):
        """Record one test result, printing a checkmark or failure line."""
        if passed:
            print(f"✓ {test_name}")
            self.pass_count += 1
        else:
            error_msg = f"✗ {test_name}"
            if message:
                error_msg += f": {message}"
            print(error_msg)
            self.failures.append(error_msg)
            self.fail_count += 1

    def summarize_and_exit(self) -> NoReturn:
        """Print the pass/fail summary and exit with an appropriate status code."""
        print()
        print("=" * 60)
        print("TEST SUMMARY")
        print("=" * 60)
        print(f"Passed: {self.pass_count}")
        print(f"Failed: {self.fail_count}")
        print()

        if self.failures:
            print("FAILURES:")
            for failure in self.failures:
                print(f"  {failure}")
            print()

        if self.fail_count == 0:
            print("✓ All tests PASSED")
            sys.exit(0)
        else:
            print("✗ Some tests FAILED")
            sys.exit(1)
