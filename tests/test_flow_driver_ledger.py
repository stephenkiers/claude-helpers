#!/usr/bin/env python3
"""
Spec-blind tests for scripts/flow_driver/ledger.py (Layer 2, Round 2).

Tests ledger behavior: close_out_directions() is idempotent, and ledger errors map to typed
LedgerError, not exceptions.

Run with: python3 tests/test_flow_driver_ledger.py
"""

import sys
import tempfile
from pathlib import Path
from typing import Callable, Sequence

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from _test_harness import Harness  # noqa: E402

from scripts.flow_driver import ledger, preflight  # noqa: E402


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


def test_close_out_directions_returns_typed_result() -> None:
    """close_out_directions() returns CloseOut or LedgerError, not an exception."""
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        flow_dir.mkdir()

        # Create minimal flow structure
        (flow_dir / "state.json").write_text("{}")

        def mock_run(argv: Sequence[str]) -> preflight.CommandResult:
            return preflight.CommandResult(returncode=0, stdout="", stderr="")

        result = ledger.close_out_directions(flow_dir, "human", run=mock_run)
        expect(
            isinstance(result, (ledger.CloseOut, ledger.LedgerError)),
            f"close_out_directions returned {type(result).__name__}, not CloseOut or LedgerError"
        )


def test_close_out_directions_always_returns_typed_result() -> None:
    """close_out_directions() always returns a typed result, never raises an exception."""
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        flow_dir.mkdir()
        (flow_dir / "state.json").write_text("{}")

        def mock_run(argv: Sequence[str]) -> preflight.CommandResult:
            return preflight.CommandResult(returncode=0, stdout="[]", stderr="")

        result1 = ledger.close_out_directions(flow_dir, "human", run=mock_run)
        result2 = ledger.close_out_directions(flow_dir, "human", run=mock_run)

        # Both calls should return a typed result (CloseOut or LedgerError), not raise
        expect(
            isinstance(result1, (ledger.CloseOut, ledger.LedgerError)),
            f"close_out_directions returned {type(result1).__name__}, not CloseOut or LedgerError"
        )
        expect(
            isinstance(result2, (ledger.CloseOut, ledger.LedgerError)),
            f"second close_out_directions returned {type(result2).__name__}, not CloseOut or LedgerError"
        )


def test_list_records_returns_list_or_error() -> None:
    """list_records() returns a list or LedgerError, not an exception."""
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        flow_dir.mkdir()
        (flow_dir / "state.json").write_text("{}")

        def mock_run(argv: Sequence[str]) -> preflight.CommandResult:
            return preflight.CommandResult(returncode=0, stdout="", stderr="")

        result = ledger.list_records(flow_dir, run=mock_run)
        expect(
            isinstance(result, (list, ledger.LedgerError)),
            f"list_records returned {type(result).__name__}, not list or LedgerError"
        )


def test_carry_forward_returns_typed_result() -> None:
    """carry_forward() returns CarryOk, UncarriedDirection, or LedgerError."""
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        flow_dir.mkdir()
        (flow_dir / "state.json").write_text("{}")

        def mock_run(argv: Sequence[str]) -> preflight.CommandResult:
            return preflight.CommandResult(returncode=0, stdout="", stderr="")

        result = ledger.carry_forward(flow_dir, 1, "receipt text", run=mock_run)
        expect(
            isinstance(result, (ledger.CarryOk, ledger.UncarriedDirection, ledger.LedgerError)),
            f"carry_forward returned {type(result).__name__}, not CarryOk/UncarriedDirection/LedgerError"
        )


def test_ensure_snapshot_returns_snapshot_or_error() -> None:
    """ensure_snapshot() returns Snapshot or LedgerError."""
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        flow_dir.mkdir()
        (flow_dir / "state.json").write_text("{}")

        def mock_run(argv: Sequence[str]) -> preflight.CommandResult:
            return preflight.CommandResult(returncode=0, stdout="", stderr="")

        result = ledger.ensure_snapshot(flow_dir, "plan", 1, 1, run=mock_run)
        expect(
            isinstance(result, (ledger.Snapshot, ledger.LedgerError)),
            f"ensure_snapshot returned {type(result).__name__}, not Snapshot or LedgerError"
        )


def _fake_ledger(pending_seqs: Sequence[int]):
    """A fake flow-ledger Runner: `list` prints the still-pending direction records, `mark` removes one. Returns (runner, calls)."""
    import json as _json
    pending = list(pending_seqs)
    calls = []

    def run(argv: Sequence[str]) -> "preflight.CommandResult":
        verb = argv[2]
        calls.append(list(argv))
        if verb == "list":
            rows = [{"seq": n, "ts": "t", "step": "plan", "attempt": 1, "kind": "direction", "text": "x",
                     "transcript": None, "status": "pending", "addressed_by": None, "supersedes": None} for n in pending]
            return preflight.CommandResult(0, _json.dumps(rows), "")
        if verb == "mark":
            pending.remove(int(argv[argv.index("--seq") + 1]))
            return preflight.CommandResult(0, "", "")
        return preflight.CommandResult(1, "", "unexpected verb")

    return run, calls


def test_close_out_marks_each_pending_once_and_is_idempotent() -> None:
    run, calls = _fake_ledger([3, 5])
    first = ledger.close_out_directions(Path("/nonexistent-flow"), "steps/06-verify.md", run)
    expect(isinstance(first, ledger.CloseOut) and first.marked == (3, 5), f"first close-out was {first}")
    marks_after_first = [c for c in calls if c[2] == "mark"]
    expect(len(marks_after_first) == 2, f"expected 2 mark calls, got {len(marks_after_first)}")
    second = ledger.close_out_directions(Path("/nonexistent-flow"), "steps/06-verify.md", run)
    expect(isinstance(second, ledger.CloseOut) and second.marked == (), f"second close-out marked {second}")
    expect(len([c for c in calls if c[2] == "mark"]) == 2, "the second close-out wrote a mark")


def test_close_out_failed_mark_is_ledger_error_and_stops() -> None:
    def run(argv: Sequence[str]) -> "preflight.CommandResult":
        if argv[2] == "list":
            row = {"seq": 1, "ts": "t", "step": "plan", "attempt": 1, "kind": "direction", "text": "x",
                   "transcript": None, "status": "pending", "addressed_by": None, "supersedes": None}
            return preflight.CommandResult(0, __import__("json").dumps([row]), "")
        return preflight.CommandResult(2, "", "boom")

    result = ledger.close_out_directions(Path("/nonexistent-flow"), "steps/06-verify.md", run)
    expect(isinstance(result, ledger.LedgerError), f"a failed mark returned {result}")


def test_close_out_rejects_unsafe_artifact_path() -> None:
    run, calls = _fake_ledger([1])
    for bad in ("/abs/path.md", "../escape.md"):
        result = ledger.close_out_directions(Path("/nonexistent-flow"), bad, run)
        expect(isinstance(result, ledger.LedgerError), f"{bad} was accepted: {result}")
    expect(calls == [], "a rejected path still ran the ledger")


SECTIONS = [
    ("[Ledger typed results]", [
        ("1: close_out_directions returns CloseOut or LedgerError", test_close_out_directions_returns_typed_result),
        ("2: close_out_directions always returns typed result", test_close_out_directions_always_returns_typed_result),
        ("3: list_records returns list or LedgerError", test_list_records_returns_list_or_error),
        ("4: carry_forward returns typed result", test_carry_forward_returns_typed_result),
        ("5: ensure_snapshot returns Snapshot or LedgerError", test_ensure_snapshot_returns_snapshot_or_error),
        ("6: close-out marks each pending once, second run writes nothing", test_close_out_marks_each_pending_once_and_is_idempotent),
        ("7: a failed mark is a LedgerError", test_close_out_failed_mark_is_ledger_error_and_stops),
        ("8: unsafe artifact path refused before any ledger call", test_close_out_rejects_unsafe_artifact_path),
    ]),
]


def main() -> None:
    h = Harness("FLOW DRIVER LEDGER")
    with tempfile.TemporaryDirectory(prefix="flow-driver-ledger-"):
        for title, checks in SECTIONS:
            print(title)
            for name, fn in checks:
                check(h, name, fn)
            print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
