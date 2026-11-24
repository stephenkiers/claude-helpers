#!/usr/bin/env python3
"""
Tests for receipts.classify and receipts.settle_in_progress on parked and resumed records (#549, ADR-0026).

An incomplete receipt while the pending record is parked-awaiting-answer or resumed is InProgress with
basis "awaiting-answer" and is never Malformed. Any other incomplete receipt keeps the two-read rule.
settle_in_progress stores a fingerprint only for basis "fingerprint-changed".

Run with: python3 tests/test_flow_driver_receipts.py
"""

import dataclasses
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from _test_harness import Harness  # noqa: E402

from scripts.flow_driver import receipts  # noqa: E402
from scripts.flow_driver.state import InitSpec, PendingSpawn, ReceiptFingerprint, TransitionKey, init_state, mutating  # noqa: E402

PLACEHOLDER = "ATTEMPT: 1\n<!-- awaiting-answers: questions/fix-01.json -->\n"


def _transition_key(locked: object) -> TransitionKey:
    return TransitionKey(run_id=locked.require_state().run_id, step="fix", attempt=1)  # type: ignore[attr-defined]


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check(h: Harness, name: str, fn: Callable[[Path], None]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        flow = Path(tmp) / "flow"
        flow.mkdir()
        try:
            fn(flow)
        except AssertionError as exc:
            h.test_result(name, False, str(exc) or "assertion failed")
        except Exception as exc:
            h.test_result(name, False, f"{type(exc).__name__}: {exc}")
        else:
            h.test_result(name, True)


def _has_state(flow: Path) -> bool:
    return (flow / "state.json").exists()


def _commit_fix_record(locked: object, state: str, receipt_seen: Optional[object]) -> None:
    current = locked.require_state()  # type: ignore[attr-defined]
    pending = PendingSpawn(step="fix", attempt=1, nonce="n0", state=state, receipt_seen=receipt_seen)  # type: ignore[arg-type]
    locked.commit(dataclasses.replace(current, step="fix", pending_spawn=pending, attempts={"fix": 1}))  # type: ignore[attr-defined]


def make_flow(flow: Path, state: str, receipt_seen: Optional[object] = None, text: Optional[str] = PLACEHOLDER) -> None:
    """A flow with one fix record in the given state and, when text is given, an incomplete receipt on disk."""
    spec = InitSpec(repo="o/n", issue=1, worktree=str(flow))
    if _has_state(flow):
        with mutating(flow) as locked:
            _commit_fix_record(locked, state, receipt_seen)
    else:
        with mutating(flow, create=spec) as locked:
            init_state(locked, spec)
            _commit_fix_record(locked, state, receipt_seen)
    if text is not None:
        path = receipts.receipt_path(flow, "fix")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def classify_now(flow: Path) -> receipts.Classification:
    with mutating(flow) as locked:
        return receipts.classify(locked, locked.require_state().pending_spawn)  # type: ignore[arg-type]


def stored_fingerprint(flow: Path) -> object:
    with mutating(flow) as locked:
        return locked.require_state().pending_spawn.receipt_seen  # type: ignore[union-attr]


def test_549_05a_parked_and_resumed_are_awaiting_answer(flow: Path) -> None:
    make_flow(flow, "parked-awaiting-answer", receipt_seen=None)
    result = classify_now(flow)
    expect(isinstance(result, receipts.InProgress), f"a parked incomplete receipt classified as {type(result).__name__}")
    expect(result.basis == "awaiting-answer", f"a parked record has basis {result.basis!r}, expected awaiting-answer")
    make_flow(flow, "resumed", receipt_seen=None)
    result = classify_now(flow)
    expect(isinstance(result, receipts.InProgress) and result.basis == "awaiting-answer",
           f"a resumed record with no fingerprint classified as {result!r}")
    make_flow(flow, "resumed", receipt_seen=None)
    expect(not isinstance(classify_now(flow), receipts.Malformed), "a resumed placeholder was classified as Malformed")


def test_549_05b_resumed_with_matching_fingerprint_is_still_awaiting_answer(flow: Path) -> None:
    make_flow(flow, "spawned", receipt_seen=None)
    first = classify_now(flow)
    expect(isinstance(first, receipts.InProgress) and first.basis == "fingerprint-changed",
           f"a spawned first look classified as {first!r}, expected InProgress fingerprint-changed")
    make_flow(flow, "resumed", receipt_seen=first.fingerprint)
    result = classify_now(flow)
    expect(isinstance(result, receipts.InProgress) and result.basis == "awaiting-answer",
           f"a resumed record whose receipt_seen matches classified as {result!r}")


def test_549_05c_spawned_two_reads_still_reach_malformed(flow: Path) -> None:
    make_flow(flow, "spawned", receipt_seen=None, text="ATTEMPT: 1\npartial, no step-end\n")
    first = classify_now(flow)
    expect(isinstance(first, receipts.InProgress) and first.basis == "fingerprint-changed",
           f"the first spawned read classified as {first!r}, expected InProgress fingerprint-changed")
    with mutating(flow) as locked:
        receipts.settle_in_progress(locked, _transition_key(locked), locked.require_state().pending_spawn, first)  # type: ignore[arg-type]
    second = classify_now(flow)
    expect(isinstance(second, receipts.Malformed), f"the second spawned read of identical bytes classified as {second!r}, expected Malformed")


def test_549_05d_resumed_two_markers_is_awaiting_answer(flow: Path) -> None:
    text = (
        "ATTEMPT: 1\n<!-- awaiting-answers: questions/fix-01.json -->\n"
        "<!-- awaiting-answers: questions/fix-02.json -->\n"
    )
    make_flow(flow, "resumed", receipt_seen=None, text=text)
    result = classify_now(flow)
    expect(isinstance(result, receipts.InProgress) and result.basis == "awaiting-answer",
           f"a resumed two-marker receipt classified as {result!r}, expected InProgress awaiting-answer")


def test_549_06a_settle_does_not_store_for_awaiting_answer(flow: Path) -> None:
    make_flow(flow, "resumed", receipt_seen=None)
    held = classify_now(flow)
    expect(isinstance(held, receipts.InProgress) and held.basis == "awaiting-answer", f"precondition: {held!r}")
    with mutating(flow) as locked:
        receipts.settle_in_progress(locked, _transition_key(locked), locked.require_state().pending_spawn, held)  # type: ignore[arg-type]
    expect(stored_fingerprint(flow) is None, "settle_in_progress stored a fingerprint for an awaiting-answer hold")


def test_549_06b_settle_does_not_overwrite_parked_fingerprint(flow: Path) -> None:
    make_flow(flow, "parked-awaiting-answer", receipt_seen=None)
    held = classify_now(flow)
    expect(isinstance(held, receipts.InProgress) and held.basis == "awaiting-answer", f"precondition: {held!r}")
    parked_value = ReceiptFingerprint(sha256="f" * 64, size=1)
    make_flow(flow, "parked-awaiting-answer", receipt_seen=parked_value)
    with mutating(flow) as locked:
        receipts.settle_in_progress(locked, _transition_key(locked), locked.require_state().pending_spawn, held)  # type: ignore[arg-type]
    expect(stored_fingerprint(flow) == parked_value, "settle_in_progress changed a parked record's fingerprint")


CHECKS = [
    ("549-05a: parked and resumed incomplete receipts are InProgress awaiting-answer", test_549_05a_parked_and_resumed_are_awaiting_answer),
    ("549-05b: resumed with a matching fingerprint is still awaiting-answer", test_549_05b_resumed_with_matching_fingerprint_is_still_awaiting_answer),
    ("549-05c: spawned incomplete receipt still reaches Malformed on the second read", test_549_05c_spawned_two_reads_still_reach_malformed),
    ("549-05d: resumed two-marker receipt is awaiting-answer", test_549_05d_resumed_two_markers_is_awaiting_answer),
    ("549-06a: settle_in_progress stores no fingerprint for awaiting-answer", test_549_06a_settle_does_not_store_for_awaiting_answer),
    ("549-06b: settle_in_progress leaves a parked fingerprint alone", test_549_06b_settle_does_not_overwrite_parked_fingerprint),
]


def main() -> None:
    h = Harness("FLOW DRIVER RECEIPTS (#549 HELD PLACEHOLDERS)")
    for name, fn in CHECKS:
        check(h, name, fn)
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
