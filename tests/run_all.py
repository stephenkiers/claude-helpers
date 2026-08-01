#!/usr/bin/env python3
"""Aggregate runner: run every tests/test_*.py and report one combined result.

Each suite drives the shared Harness, which calls sys.exit() at the end — so the suites cannot be
imported into a single process. Run them as subprocesses instead. A fork inherits coverage for free:
drop a new test_*.py into this directory and it runs here with no extra wiring.

Run with: python3 tests/run_all.py
"""

import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def main():
    suites = sorted(TESTS_DIR.glob("test_*.py"))
    if not suites:
        print("No test_*.py suites found.")
        return 1

    failures = []
    for suite in suites:
        print(f"\n{'#' * 70}\n# {suite.name}\n{'#' * 70}")
        result = subprocess.run([sys.executable, str(suite)])
        if result.returncode != 0:
            failures.append(suite.name)

    print(f"\n{'=' * 70}\nAGGREGATE SUMMARY\n{'=' * 70}")
    print(f"Suites run:    {len(suites)}")
    print(f"Suites failed: {len(failures)}")
    for name in failures:
        print(f"  ✗ {name}")
    if not failures:
        print("✓ All suites PASSED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
