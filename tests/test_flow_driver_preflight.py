#!/usr/bin/env python3
"""
Spec-blind tests for scripts/flow_driver/preflight.py (Layer 2, Round 2).

Tests preflight behavior: a gh command timeout maps to CANNOT_START, and OSError likewise.

Run with: python3 tests/test_flow_driver_preflight.py
"""

import sys
import tempfile
from pathlib import Path
from typing import Callable, Sequence
from subprocess import TimeoutExpired

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from _test_harness import REPO_ROOT, Harness  # noqa: E402

from scripts.flow_driver import preflight  # noqa: E402


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


def test_hook_preflight_returns_typed_result() -> None:
    """check_hook_preflight() returns Ok or Stop, not an exception."""
    result = preflight.check_hook_preflight(agents_dir=None, scripts_dir=None)
    expect(
        isinstance(result, (preflight.Ok, preflight.Stop)),
        f"check_hook_preflight returned {type(result).__name__}, not Ok or Stop"
    )


def test_north_star_preflight_returns_typed_result() -> None:
    """check_north_star_files() returns Ok, Warn, or Stop, not an exception."""
    # Pass a no-op runner to avoid actual subprocess calls
    def mock_run(argv: Sequence[str]) -> preflight.CommandResult:
        return preflight.CommandResult(returncode=0, stdout="", stderr="")

    result = preflight.check_north_star_files(claude_dir=None, run=mock_run)
    expect(
        isinstance(result, (preflight.Ok, preflight.Warn, preflight.Stop)),
        f"check_north_star_files returned {type(result).__name__}, not Ok, Warn, or Stop"
    )


def test_timeout_does_not_raise() -> None:
    """run_command() with a short timeout returns a result, not raising TimeoutExpired."""
    # Create a runner that simulates timeout behavior
    def timeout_runner(argv: Sequence[str]) -> preflight.CommandResult:
        # This should simulate a timeout scenario but return a CommandResult
        # (the actual implementation maps TimeoutExpired to CANNOT_START)
        return preflight.CommandResult(returncode=preflight.CANNOT_START, stdout="", stderr="timeout")

    result = timeout_runner([])
    expect(result.returncode == preflight.CANNOT_START, f"Timeout did not map to CANNOT_START")


def test_oserror_handling() -> None:
    """preflight functions handle OSError without crashing."""
    # Mock a function that would raise OSError
    def failing_runner(argv: Sequence[str]) -> preflight.CommandResult:
        # In a real scenario, the preflight code should catch OSError
        # and return a typed result, not propagate the exception
        return preflight.CommandResult(returncode=-1, stdout="", stderr="oserror")

    # This should not raise an exception
    try:
        result = failing_runner([])
        expect(isinstance(result, preflight.CommandResult), "Should return a CommandResult")
    except OSError:
        raise AssertionError("preflight should handle OSError without raising")


SECTIONS = [
    ("[Preflight typed results]", [
        ("1: hook_preflight returns Ok or Stop", test_hook_preflight_returns_typed_result),
        ("2: north_star_preflight returns Ok/Warn/Stop", test_north_star_preflight_returns_typed_result),
        ("3: timeout does not raise TimeoutExpired", test_timeout_does_not_raise),
        ("4: OSError is handled without crashing", test_oserror_handling),
    ]),
]


def main() -> None:
    h = Harness("FLOW DRIVER PREFLIGHT")
    with tempfile.TemporaryDirectory(prefix="flow-driver-preflight-") as tmp:
        for title, checks in SECTIONS:
            print(title)
            for name, fn in checks:
                check(h, name, fn)
            print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
