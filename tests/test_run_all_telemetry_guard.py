#!/usr/bin/env python3
"""
Meta-test for the temp-HOME telemetry guard in tests/run_all.py (issue #280).

Proves the guard fires on a leaking suite (naming it, even when it exits 0), fires on a state-dir
only leak, stays quiet on a clean suite, and that the child environment carries the temp HOME and
the pinned PYTHONUSERBASE.

Run with: python3 tests/test_run_all_telemetry_guard.py
"""

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import run_all
from _test_harness import Harness

LEAK = (
    "from pathlib import Path\n"
    "d = Path.home() / '.claude' / 'telemetry'\n"
    "d.mkdir(parents=True)\n"
    "(d / 'events.jsonl').write_text('{}\\n')\n"
)
STATE_LEAK = (
    "from pathlib import Path\n"
    "d = Path.home() / '.claude' / 'telemetry' / 'state'\n"
    "d.mkdir(parents=True)\n"
    "(d / 'x.json').write_text('{}')\n"
)
CLEAN = "pass\n"
CANARY = (
    "import os, site\n"
    "from pathlib import Path\n"
    "print(Path.home())\n"
    "print(os.environ['PYTHONUSERBASE'])\n"
    "print(site.getusersitepackages())\n"
)


def _write(tmp, name, body):
    path = Path(tmp) / name
    path.write_text(body)
    return path


def test_leaking_suite_named_even_when_exit_zero():
    with tempfile.TemporaryDirectory() as tmp:
        leaking = _write(tmp, "leaker_case.py", LEAK)
        clean = _write(tmp, "clean_case.py", CLEAN)
        failures = run_all.run_suites([leaking, clean])
        if len(failures) != 1:
            return False, f"expected exactly one failure, got {failures}"
        if "leaker_case.py" not in failures[0] or "events.jsonl" not in failures[0]:
            return False, f"label should name the suite and leaked path: {failures[0]}"
        return True, ""


def test_nested_telemetry_leak_reported():
    with tempfile.TemporaryDirectory() as tmp:
        failures = run_all.run_suites([_write(tmp, "nested_leaker_case.py", STATE_LEAK)])
        if len(failures) != 1:
            return False, f"expected exactly one nested leak failure, got {failures}"
        if "telemetry/state" not in failures[0] and "x.json" not in failures[0]:
            return False, f"label should name the nested leaked path (telemetry/state or x.json): {failures[0]}"
        return True, ""


def test_clean_suite_passes():
    with tempfile.TemporaryDirectory() as tmp:
        failures = run_all.run_suites([_write(tmp, "clean_case.py", CLEAN)])
        if failures:
            return False, f"clean suite should not fail: {failures}"
        return True, ""


def test_env_carries_temp_home_and_user_base():
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as fake_home:
        script = _write(tmp, "canary_case.py", CANARY)
        out = subprocess.run(
            [sys.executable, str(script)], env=run_all.suite_env(fake_home),
            capture_output=True, text=True,
        ).stdout.splitlines()
        if len(out) != 3:
            return False, f"unexpected canary output: {out}"
        if Path(out[0]).resolve() != Path(fake_home).resolve():
            return False, f"child HOME is {out[0]}, expected {fake_home}"
        if out[1] != run_all.REAL_USER_BASE:
            return False, f"PYTHONUSERBASE {out[1]} != {run_all.REAL_USER_BASE}"
        expected = subprocess.run(
            [sys.executable, "-c", "import site; print(site.getusersitepackages())"],
            capture_output=True, text=True,
            env={**run_all.os.environ, "PYTHONUSERBASE": run_all.REAL_USER_BASE},
        ).stdout.strip()
        if out[2] != expected:
            return False, f"user site-packages {out[2]} != {expected}"
        return True, ""


def main():
    h = Harness("RUN_ALL TELEMETRY GUARD TEST SUITE")
    h.test_result("leaking suite named even when exit 0", *test_leaking_suite_named_even_when_exit_zero())
    h.test_result("nested telemetry leak reported", *test_nested_telemetry_leak_reported())
    h.test_result("clean suite passes", *test_clean_suite_passes())
    h.test_result("child env carries temp HOME and pinned user base", *test_env_carries_temp_home_and_user_base())
    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
