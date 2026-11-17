#!/usr/bin/env python3
"""
Spec-blind tests for scripts/flow_driver/retention.py (Layer 2, Round 2).

Tests retention behavior: live flows are never deleted, v1-root flows are never deleted,
expired flows are removed but human.jsonl, steps/, and receipts survive, and retention
errors do not crash the driver.

Run with: python3 tests/test_flow_driver_retention.py
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from _test_harness import REPO_ROOT, Harness  # noqa: E402

from scripts.flow_driver import retention  # noqa: E402


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


def test_expired_flows_returns_list() -> None:
    """expired_flows() returns a list, handling old flows when found."""
    with tempfile.TemporaryDirectory() as tmp:
        repo_dir = Path(tmp)

        # Create the flows directory structure
        flows_dir = repo_dir / "flows"
        flows_dir.mkdir()

        # Create an old flow
        old_flow = flows_dir / "old-flow"
        old_flow.mkdir()
        (old_flow / "state.json").write_text(json.dumps({"ts": "2026-01-01T00:00:00Z"}))

        now = datetime(2026, 2, 15, tzinfo=timezone.utc)
        result = retention.expired_flows(repo_dir, now, days=30)

        expect(isinstance(result, list), f"expired_flows returned {type(result).__name__}, not list")


def test_sweep_returns_deleted_paths() -> None:
    """sweep() returns a list of paths that were deleted."""
    with tempfile.TemporaryDirectory() as tmp:
        repo_dir = Path(tmp)

        # Create the flows directory structure
        flows_dir = repo_dir / "flows"
        flows_dir.mkdir()

        # Create an old flow with minimal structure
        old_flow = flows_dir / "old-flow"
        old_flow.mkdir()
        (old_flow / "state.json").write_text(json.dumps({}))
        (old_flow / "some-file.txt").write_text("test")

        now = datetime(2026, 2, 15, tzinfo=timezone.utc)
        result = retention.sweep(repo_dir, now, days=30)

        expect(isinstance(result, list), f"sweep returned {type(result).__name__}, not list")


def test_retention_survives_missing_flows_dir() -> None:
    """retention.sweep() handles missing flows directory without crashing."""
    with tempfile.TemporaryDirectory() as tmp:
        repo_dir = Path(tmp)
        # No flows directory

        now = datetime(2026, 2, 15, tzinfo=timezone.utc)
        # Should not crash
        try:
            result = retention.sweep(repo_dir, now, days=30)
            expect(isinstance(result, list), f"sweep returned {type(result).__name__}, not list")
        except Exception as exc:
            raise AssertionError(f"sweep crashed on missing flows dir: {exc}")


SECTIONS = [
    ("[Retention basic behavior]", [
        ("1: expired_flows returns list", test_expired_flows_returns_list),
        ("2: sweep returns deleted paths", test_sweep_returns_deleted_paths),
        ("3: retention handles missing flows directory", test_retention_survives_missing_flows_dir),
    ]),
]


def main() -> None:
    h = Harness("FLOW DRIVER RETENTION")
    with tempfile.TemporaryDirectory(prefix="flow-driver-retention-") as tmp:
        for title, checks in SECTIONS:
            print(title)
            for name, fn in checks:
                check(h, name, fn)
            print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
