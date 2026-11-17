#!/usr/bin/env python3
"""
Tests for scripts/flow_driver/recovery.py.

Tests the recovery bounds table: each bound is enforced, so a call past the bound
is refused with a typed result, never silently allowed.

Run with: python3 tests/test_flow_driver_recovery.py
"""

import sys
import tempfile
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from _test_harness import Harness  # noqa: E402

from scripts.flow_driver import recovery  # noqa: E402


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check(h: Harness, name: str, fn: Callable[[], None]) -> None:
    """Run one named check. A crash inside a check is a named failure, not a suite abort."""
    try:
        fn()
    except AssertionError as exc:
        h.test_result(name, False, str(exc) or "assertion failed")
    except Exception as exc:
        h.test_result(name, False, f"{type(exc).__name__}: {exc}")
    else:
        h.test_result(name, True)


def test_retry_bound_before_limit() -> None:
    """retry() below the limit returns Retry, not Stop."""
    result = recovery.retry("plan", recovery.RETRY_CAP - 1)
    expect(isinstance(result, recovery.Retry), f"retry below limit returned {type(result).__name__}, not Retry")


def test_retry_bound_at_limit() -> None:
    """retry() at the limit returns Stop with retry-cap code."""
    result = recovery.retry("plan", recovery.RETRY_CAP)
    expect(isinstance(result, recovery.Stop), f"retry at limit returned {type(result).__name__}, not Stop")
    expect(result.code == "retry-cap", f"Stop code is {result.code!r}, not 'retry-cap'")


def test_redo_bound_before_limit() -> None:
    """redo() below the limit returns Redo, not Stop."""
    result = recovery.redo("plan", recovery.REDO_CAP - 1)
    expect(isinstance(result, recovery.Redo), f"redo below limit returned {type(result).__name__}, not Redo")


def test_redo_bound_at_limit() -> None:
    """redo() at the limit returns Stop with redo-cap code."""
    result = recovery.redo("plan", recovery.REDO_CAP)
    expect(isinstance(result, recovery.Stop), f"redo at limit returned {type(result).__name__}, not Stop")
    expect(result.code == "redo-cap", f"Stop code is {result.code!r}, not 'redo-cap'")


def test_rebase_bound_before_limit() -> None:
    """auto_rebase() below the limit returns AutoRebase, not Stop."""
    result = recovery.auto_rebase("plan", recovery.REBASE_CAP - 1)
    expect(isinstance(result, recovery.AutoRebase), f"auto_rebase below limit returned {type(result).__name__}, not AutoRebase")


def test_rebase_bound_at_limit() -> None:
    """auto_rebase() at the limit returns Stop with rebase-cap code."""
    result = recovery.auto_rebase("plan", recovery.REBASE_CAP)
    expect(isinstance(result, recovery.Stop), f"auto_rebase at limit returned {type(result).__name__}, not Stop")
    expect(result.code == "rebase-cap", f"Stop code is {result.code!r}, not 'rebase-cap'")


def test_heal_bound_before_limit() -> None:
    """heal() below the limit returns Heal, not Stop."""
    result = recovery.heal("plan", recovery.HEAL_CAP - 1)
    expect(isinstance(result, recovery.Heal), f"heal below limit returned {type(result).__name__}, not Heal")


def test_heal_bound_at_limit() -> None:
    """heal() at the limit returns Stop with heal-cap code."""
    result = recovery.heal("plan", recovery.HEAL_CAP)
    expect(isinstance(result, recovery.Stop), f"heal at limit returned {type(result).__name__}, not Stop")
    expect(result.code == "heal-cap", f"Stop code is {result.code!r}, not 'heal-cap'")


def test_malformed_bound_at_limit() -> None:
    """malformed_rerun() at the limit returns Stop with malformed-cap code."""
    result = recovery.malformed_rerun("plan", recovery.MALFORMED_CAP, previous_attempt_malformed=True)
    expect(isinstance(result, recovery.Stop), f"malformed_rerun at limit returned {type(result).__name__}, not Stop")
    expect(result.code == "malformed-cap", f"Stop code is {result.code!r}, not 'malformed-cap'")


SECTIONS = [
    ("[Recovery bounds enforcement]", [
        ("1: retry bound below limit returns Retry", test_retry_bound_before_limit),
        ("2: retry bound at limit returns Stop retry-cap", test_retry_bound_at_limit),
        ("3: redo bound below limit returns Redo", test_redo_bound_before_limit),
        ("4: redo bound at limit returns Stop redo-cap", test_redo_bound_at_limit),
        ("5: rebase bound below limit returns AutoRebase", test_rebase_bound_before_limit),
        ("6: rebase bound at limit returns Stop rebase-cap", test_rebase_bound_at_limit),
        ("7: heal bound below limit returns Heal", test_heal_bound_before_limit),
        ("8: heal bound at limit returns Stop heal-cap", test_heal_bound_at_limit),
        ("9: malformed bound at limit returns Stop malformed-cap", test_malformed_bound_at_limit),
    ]),
]


def main() -> None:
    h = Harness("FLOW DRIVER RECOVERY")
    with tempfile.TemporaryDirectory(prefix="flow-driver-recovery-"):
        for title, checks in SECTIONS:
            print(title)
            for name, fn in checks:
                check(h, name, fn)
            print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
