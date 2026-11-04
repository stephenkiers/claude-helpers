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

import argparse
import os
import site
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

# Computed once in the parent, before any HOME override.
REAL_USER_BASE = os.environ.get("PYTHONUSERBASE") or site.getuserbase()

# Wall-clock-bounded subprocess cases; run alone after the pool.
SERIAL_ONLY = frozenset({"test_flow_guard_fail_closed.py"})


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


def _run_one(suite):
    """Run a single suite and return (suite, rc, stdout, leaked, elapsed)."""
    start = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp_home:
        result = subprocess.run(
            [sys.executable, str(suite)],
            env=suite_env(tmp_home),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        leaked = telemetry_leak(tmp_home)
    return (suite, result.returncode, result.stdout.decode("utf-8", "replace"), leaked, time.monotonic() - start)


def run_suites(suites, jobs=1, timings=None):
    """Run each suite under its own temp HOME; return the list of failure labels."""
    failures = []
    lock = threading.Lock()

    # Split suites into parallel and serial
    parallel = [s for s in suites if Path(s).name not in SERIAL_ONLY]
    serial = [s for s in suites if Path(s).name in SERIAL_ONLY]

    # Run parallel suites through ThreadPoolExecutor
    results = []
    if parallel:
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as executor:
            futures = {executor.submit(_run_one, suite): suite for suite in parallel}
            from concurrent.futures import as_completed
            for future in as_completed(futures):
                suite, rc, stdout, leaked, elapsed = future.result()
                results.append((suite, rc, stdout, leaked, elapsed))

    # Run serial suites directly
    for suite in serial:
        suite_path, rc, stdout, leaked, elapsed = _run_one(suite)
        results.append((suite_path, rc, stdout, leaked, elapsed))

    # Process all results
    for suite_path, rc, stdout, leaked, elapsed in results:
        suite_name = Path(suite_path).name
        with lock:
            # Print header
            print(f"\n{'#' * 70}\n# {suite_name}\n{'#' * 70}")
            # Print captured output
            print(stdout, end='')
            # Print timing line
            print(f"# {suite_name}: {elapsed:.1f}s (exit {rc})")

        # Record timing if requested
        if timings is not None:
            timings[suite_name] = elapsed

        # Check for failures
        if leaked:
            detail = f"leaked telemetry: {', '.join(leaked)}"
            if rc != 0:
                detail += f"; exit {rc}"
            failures.append(f"{suite_name} ({detail})")
            with lock:
                print(f"✗ {suite_name} wrote under the real-telemetry path: {', '.join(leaked)}"
                      + (f" (exit {rc})" if rc != 0 else ""))
        elif rc != 0:
            failures.append(suite_name)

    return sorted(failures)


def main():
    parser = argparse.ArgumentParser(description="Run all test suites")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1,
                        help="Number of parallel jobs (default: CPU count)")
    args = parser.parse_args()

    if args.jobs < 1:
        parser.error("--jobs must be at least 1")

    suites = sorted(TESTS_DIR.glob("test_*.py"))
    if not suites:
        print("No test_*.py suites found.")
        return 1

    print("Note: each suite runs under a temporary HOME (PYTHONUSERBASE pinned); "
          "telemetry writes under it fail the run.")

    timings = {}
    failures = run_suites(suites, jobs=args.jobs, timings=timings)

    print(f"\n{'=' * 70}\nSLOWEST 15 SUITES\n{'=' * 70}")
    sorted_timings = sorted(timings.items(), key=lambda x: x[1], reverse=True)[:15]
    for name, secs in sorted_timings:
        print(f"  {secs:7.1f}s  {name}")

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
