"""Shared harness for this repo's invariant test scripts (no pytest dependency)."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class Harness:
    def __init__(self, title):
        self.pass_count = 0
        self.fail_count = 0
        self.failures = []
        print("=" * len(title))
        print(title)
        print("=" * len(title))
        print()

    def test_result(self, test_name, passed, message=""):
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

    def summarize_and_exit(self):
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
            exit(0)
        else:
            print("✗ Some tests FAILED")
            exit(1)
