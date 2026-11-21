#!/usr/bin/env python3
"""
Tests for `recover --choice resume`: a flow paused at a step gate or held at the merge gate is re-asked, never advanced.

Run with: python3 tests/test_flow_driver_resume.py
"""

import dataclasses
import json
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from _test_harness import Harness  # noqa: E402

from scripts.flow_driver.loop import LoopRefusal, run_recover  # noqa: E402
from scripts.flow_driver.state import InitSpec, PendingSpawn, init_state, mutating  # noqa: E402


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check(h: Harness, name: str, fn: Callable[[Path], None]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            fn(Path(tmp) / "flow")
        except AssertionError as exc:
            h.test_result(name, False, str(exc) or "assertion failed")
        except Exception as exc:
            h.test_result(name, False, f"{type(exc).__name__}: {exc}")
        else:
            h.test_result(name, True)


def paused_flow(flow: Path, step: str, code: str, status: str = "paused", landed: bool = True) -> None:
    flow.mkdir(parents=True)
    spec = InitSpec(repo="o/n", issue=1, worktree=str(flow))
    with mutating(flow, create=spec) as locked:
        init_state(locked, spec)
        state = locked.require_state()
        pending: Optional[PendingSpawn] = (
            PendingSpawn(step=step, attempt=1, nonce="n0", state="receipt-landed", receipt_seen=None)  # type: ignore[arg-type]
            if landed
            else None
        )
        locked.commit(
            dataclasses.replace(
                state,
                step=step,  # type: ignore[arg-type]
                status=status,  # type: ignore[arg-type]
                attempts={step: 1},  # type: ignore[dict-item]
                pending_spawn=pending,
                stop_reason={step: f"{code}: the human chose it"},  # type: ignore[dict-item]
                stop_source={step: "receipt"},  # type: ignore[dict-item]
            )
        )


def read_state(flow: Path) -> dict:  # type: ignore[type-arg]
    return json.loads((flow / "state.json").read_text(encoding="utf-8"))


def test_resume_clears_step_gate_pause(flow: Path) -> None:
    paused_flow(flow, "plan", "gate-paused")
    answer = flow / "answers" / "plan-a1-gate.json"
    answer.parent.mkdir(parents=True)
    answer.write_text(json.dumps({"text": "Pause"}), encoding="utf-8")
    run_recover(flow, "resume")
    state = read_state(flow)
    expect(state["status"] == "running", f"status is {state['status']}")
    expect("plan" not in state["stop_reason"], "the stop reason was not cleared")
    expect(not answer.exists(), "the consumed answer was not archived")
    expect((flow / "archive" / "plan" / "a1.answer-1.json").exists(), "the answer is not in the archive")


def test_resume_clears_merge_hold(flow: Path) -> None:
    paused_flow(flow, "ship", "merge-held")
    answer = flow / "answers" / "merge-a1-merge-gate.json"
    answer.parent.mkdir(parents=True)
    answer.write_text(json.dumps({"text": "Hold"}), encoding="utf-8")
    run_recover(flow, "resume")
    expect(read_state(flow)["status"] == "running", "the held merge did not resume")
    expect(not answer.exists(), "the Hold answer was left in place, so the gate would re-hold")


def test_resume_refused_for_other_stops(flow: Path) -> None:
    paused_flow(flow, "plan", "take-over")
    try:
        run_recover(flow, "resume")
    except LoopRefusal:
        return
    raise AssertionError("resume of a take-over stop was not refused")


def test_resume_refused_when_failed(flow: Path) -> None:
    paused_flow(flow, "plan", "gate-paused", status="failed")
    try:
        run_recover(flow, "resume")
    except LoopRefusal:
        return
    raise AssertionError("resume of a failed flow was not refused")


def test_resume_needs_landed_attempt(flow: Path) -> None:
    paused_flow(flow, "plan", "gate-paused", landed=False)
    try:
        run_recover(flow, "resume")
    except LoopRefusal:
        return
    raise AssertionError("resume without a landed attempt was not refused")


CHECKS = [
    ("1: resume clears a paused step gate stop and archives its answer", test_resume_clears_step_gate_pause),
    ("2: resume clears a held merge gate stop and archives the Hold answer", test_resume_clears_merge_hold),
    ("3: other stops are refused", test_resume_refused_for_other_stops),
    ("4: a failed flow is refused", test_resume_refused_when_failed),
    ("5: no landed attempt is refused", test_resume_needs_landed_attempt),
]


def main() -> None:
    h = Harness("FLOW DRIVER RESUME")
    for name, fn in CHECKS:
        check(h, name, fn)
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
