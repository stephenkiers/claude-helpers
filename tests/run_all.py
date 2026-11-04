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

Concurrency: suites run in parallel (``--jobs``, default: CPU count) except those in SERIAL_ONLY,
which run alone after the pool. A suite that is wall-clock-bounded (timing asserts, or waits on real
child processes) belongs in SERIAL_ONLY. Parallel-safe invariant: a suite must not read, clear, or
assert on a fixed path shared with other suites (the system temp dir, a fixed name under it). Use its
own temp HOME, a ``mkdtemp`` name, or a per-pid name instead. A suite that cannot meet this goes in
SERIAL_ONLY. Each suite is killed after SUITE_TIMEOUT_SECS and counted as a failure (exit 124).

Run with: python3 tests/run_all.py
"""

import argparse
import os
import site
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

# Computed once in the parent, before any HOME override.
REAL_USER_BASE = os.environ.get("PYTHONUSERBASE") or site.getuserbase()

# Wall-clock-bounded suites (timing asserts, or waits on real child processes); run alone after the pool.
SERIAL_ONLY = frozenset({"test_flow_guard_fail_closed.py", "test_fleet.py"})

# A suite that runs longer than this is killed and counted as a failure (exit 124).
SUITE_TIMEOUT_SECS = 900


def suite_env(tmp_home):
    """Child environment: temp HOME, PYTHONUSERBASE pinned to the real user base, and the ambient
    CLAUDE_CODE_SESSION_ID dropped (a suite must not depend on, or write state for, the session
    that happens to be running it)."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    # Suites run in parallel, so scripts' short git timeouts are raised (load, not a hang, is the
    # usual cause of a slow git call here). A suite that sets its own value keeps it.
    for key in ("FLOW_VERIFY_GIT_TIMEOUT_SECS", "PRECEDENT_DIGEST_GIT_TIMEOUT_SECS"):
        env.setdefault(key, "60")
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
        try:
            result = subprocess.run(
                [sys.executable, str(suite)],
                env=suite_env(tmp_home),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=SUITE_TIMEOUT_SECS,
            )
            rc, output = result.returncode, result.stdout or b""
        except subprocess.TimeoutExpired as exc:
            rc = 124
            note = f"\n[runner] {Path(suite).name} timed out after {SUITE_TIMEOUT_SECS}s and was killed\n"
            output = (exc.stdout or b"") + note.encode("utf-8")
        leaked = telemetry_leak(tmp_home)
    return (suite, rc, output.decode("utf-8", "replace"), leaked, time.monotonic() - start)


def _failed_run(suite, exc):
    """Result tuple for a suite whose runner raised instead of returning (counted as a failure)."""
    text = f"[runner] {Path(suite).name} did not complete: {type(exc).__name__}: {exc}\n"
    return (suite, 1, text, [], 0.0)


def _report(result, index, total, timings):
    """Print one suite's block and return its failure label, or None when it passed."""
    suite, rc, stdout, leaked, elapsed = result
    suite_name = Path(suite).name
    print(f"[{index}/{total}] {suite_name} finished (exit {rc})")
    print(f"\n{'#' * 70}\n# {suite_name}\n{'#' * 70}")
    print(stdout, end='')
    print(f"# {suite_name}: {elapsed:.1f}s (exit {rc})")

    if timings is not None:
        timings[suite_name] = elapsed

    if leaked:
        detail = f"leaked telemetry: {', '.join(leaked)}"
        if rc != 0:
            detail += f"; exit {rc}"
        print(f"✗ {suite_name} wrote under the real-telemetry path: {', '.join(leaked)}"
              + (f" (exit {rc})" if rc != 0 else ""))
        return f"{suite_name} ({detail})"
    if rc != 0:
        return suite_name
    return None


def run_suites(suites, jobs=1, timings=None):
    """Run each suite under its own temp HOME; return the sorted list of failure labels.

    Parallel suites are reported as each one finishes (streamed, in completion order). All reporting
    happens in this thread, so no lock is needed. SERIAL_ONLY suites run after the pool, one at a time."""
    parallel = [s for s in suites if Path(s).name not in SERIAL_ONLY]
    serial = [s for s in suites if Path(s).name in SERIAL_ONLY]
    total = len(suites)
    done = 0
    failures = []

    def finish(result):
        nonlocal done
        done += 1
        failure = _report(result, done, total, timings)
        if failure:
            failures.append(failure)

    if parallel:
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as executor:
            futures = {executor.submit(_run_one, suite): suite for suite in parallel}
            for future in as_completed(futures):
                try:
                    result = future.result()
                except Exception as exc:  # one broken suite must not abort the whole run
                    result = _failed_run(futures[future], exc)
                finish(result)

    for suite in serial:
        try:
            result = _run_one(suite)
        except Exception as exc:
            result = _failed_run(suite, exc)
        finish(result)

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
