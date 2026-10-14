#!/usr/bin/env python3
"""Aggregate runner: run every tests/test_*.py and report one combined result.

Each suite drives the shared Harness, which calls sys.exit() at the end — so the suites cannot be
imported into a single process. Run them as subprocesses instead. A fork inherits coverage for free:
drop a new test_*.py into this directory and it runs here with no extra wiring.

Every suite runs under its own temporary HOME (with PYTHONUSERBASE pinned to the real user base so
user site-packages still resolve). After each suite the runner checks that nothing was written under
``<tmp HOME>/.claude/telemetry/``; a suite that does so fails the run, by name (#280). If a suite
fails only here, suspect that it needs the real installed ``~/.claude`` (symlinks, ~/.gitconfig).

Known limit: a child that replaces its environment rather than extending it (e.g. ``env={}``, as in
``test_session_id_env_fallback``) loses HOME; ``Path.home()`` then falls back to the pwd database,
which is the real home, and this guard cannot see that write. The guard does not claim more.

Run with: python3 tests/run_all.py
"""

import os
import site
import subprocess
import sys
import tempfile
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

# Computed once in the parent, before any HOME override.
REAL_USER_BASE = os.environ.get("PYTHONUSERBASE") or site.getuserbase()


def suite_env(tmp_home):
    """Child environment: temp HOME, PYTHONUSERBASE pinned to the real user base, and the ambient
    CLAUDE_CODE_SESSION_ID dropped (a suite must not depend on, or write state for, the session
    that happens to be running it)."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    return {**env, "HOME": str(tmp_home), "PYTHONUSERBASE": REAL_USER_BASE}


def telemetry_leak(tmp_home):
    """Paths (relative to tmp_home) under <tmp_home>/.claude/telemetry/; empty if absent or empty."""
    root = Path(tmp_home) / ".claude" / "telemetry"
    if not root.exists():
        return []
    return sorted(str(p.relative_to(tmp_home)) for p in root.rglob("*") if p.is_file())


def run_suites(suites):
    """Run each suite under its own temp HOME; return the list of failure labels."""
    failures = []
    for suite in suites:
        suite = Path(suite)
        print(f"\n{'#' * 70}\n# {suite.name}\n{'#' * 70}")
        with tempfile.TemporaryDirectory() as tmp_home:
            result = subprocess.run([sys.executable, str(suite)], env=suite_env(tmp_home))
            leaked = telemetry_leak(tmp_home)
        if leaked:
            failures.append(f"{suite.name} (leaked telemetry: {', '.join(leaked)})")
            print(f"✗ {suite.name} wrote under the real-telemetry path: {', '.join(leaked)}")
        elif result.returncode != 0:
            failures.append(suite.name)
    return failures


def main():
    suites = sorted(TESTS_DIR.glob("test_*.py"))
    if not suites:
        print("No test_*.py suites found.")
        return 1

    print("Note: each suite runs under a temporary HOME (PYTHONUSERBASE pinned); "
          "telemetry writes under it fail the run.")
    failures = run_suites(suites)

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
