#!/usr/bin/env python3
"""
Tests for assess.py's v2 stage: the outstanding set from receipts, assessor-record parsing, the batch, the disposition gate,
range/anchor validation, ref checks, and item id quoting in directives.

Run with: python3 tests/test_flow_driver_assess_next.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from _test_harness import Harness  # noqa: E402

from scripts.flow_driver import assess, receipts  # noqa: E402
from scripts.flow_driver.state import InitSpec, init_state, mutating  # noqa: E402

# v1 flow-assessor reply grammar (#538); a records file ends with the assessment-end sentinel.
ASSESS_SENTINEL = "<!-- assessment-end -->"
RECORD = (
    "{id} | status={status} | kind=decision | scope=in-scope | leave_as_is_safe=yes | cause=c | evidence=e"
    " | finish_plan=f | verification_proof=v | options=Finish it => pause"
)


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


def receipt(flow: Path, step: str, decision: str, outstanding: str) -> None:
    path = receipts.receipt_path(flow, step)  # type: ignore[arg-type]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"ATTEMPT: 1\nDecision: {decision}\n{outstanding}<!-- step-end -->\nbody\n", encoding="utf-8")


def run_next(flow: Path) -> assess.AssessOutcome:
    spec = InitSpec(repo="o/n", issue=1, worktree=str(flow))
    if not (flow / "state.json").exists():
        with mutating(flow, create=spec) as locked:
            init_state(locked, spec)
    with mutating(flow) as locked:
        return assess.assess_next(locked, flow, locked.require_state(), nonce="n0", attempt=1)


def test_set_reads_ok_receipts(flow: Path) -> None:
    receipt(flow, "implement", "OK", "OUTSTANDING: Step 3 (tests missing) [self-report]\n")
    receipt(flow, "fix", "FAILED", "OUTSTANDING: D1 (never mind) [verifier]\n")
    items, err = assess.get_outstanding_set(flow)
    expect(err is None, str(err))
    expect([i["id"] for i in items] == ["implement:Step 3"], str(items))


def test_set_empty_and_malformed(flow: Path) -> None:
    receipt(flow, "implement", "OK", "OUTSTANDING: none\n")
    items, err = assess.get_outstanding_set(flow)
    expect(items == [] and err is None, f"{items} {err}")
    receipt(flow, "fix", "OK", "OUTSTANDING: not a valid line\n")
    items, err = assess.get_outstanding_set(flow)
    expect(err is not None, "a malformed OUTSTANDING line must fail closed")


def test_nothing_outstanding_advances(flow: Path) -> None:
    receipt(flow, "implement", "OK", "OUTSTANDING: none\n")
    expect(isinstance(run_next(flow), assess.AssessAdvance), "no items, no assess")


def test_spawn_then_ask_then_gate(flow: Path) -> None:
    receipt(flow, "implement", "OK", "OUTSTANDING: Step 3 (tests missing) [self-report]\n")
    out = run_next(flow)
    expect(isinstance(out, assess.AssessSpawn), f"first call spawns: {out}")
    assess.assess_records_path(flow).write_text(RECORD.format(id="implement:Step 3", status="unfinished") + "\n" + ASSESS_SENTINEL + "\n", encoding="utf-8")
    out = run_next(flow)
    expect(isinstance(out, assess.AssessAsk), f"records land, the batch is asked: {out}")
    expect((flow / "questions" / "assess-1.json").exists(), "question file written")
    expect(isinstance(run_next(flow), assess.AssessAsk), "unanswered stays asked")
    err = assess.save_item_answer(flow, 1, "implement:Step 3", "Leave as-is", None, set())
    expect(err is None, str(err))
    out = run_next(flow)
    expect(isinstance(out, assess.AssessAdvance), f"all answered advances: {out}")
    expect((flow / "steps" / "08-ship.outstanding.md").exists(), "disposition marker written")


def test_bad_records_stop(flow: Path) -> None:
    receipt(flow, "implement", "OK", "OUTSTANDING: Step 3 (tests missing) [self-report]\n")
    assess.assess_records_path(flow).write_text(RECORD.format(id="implement:Step 9", status="unfinished") + "\n" + ASSESS_SENTINEL + "\n", encoding="utf-8")
    out = run_next(flow)
    expect(isinstance(out, assess.AssessStop), f"an id outside the set stops: {out}")
    assess.assess_records_path(flow).write_text(ASSESS_SENTINEL + "\n", encoding="utf-8")
    out = run_next(flow)
    expect(isinstance(out, assess.AssessStop) and "no record" in out.reason, f"a missing record stops: {out}")


def test_done_items_ask_nothing(flow: Path) -> None:
    receipt(flow, "implement", "OK", "OUTSTANDING: Step 3 (tests missing) [self-report]\n")
    assess.assess_records_path(flow).write_text(RECORD.format(id="implement:Step 3", status="already-done") + "\n" + ASSESS_SENTINEL + "\n", encoding="utf-8")
    expect(isinstance(run_next(flow), assess.AssessAdvance), "an already-done item needs no answer")


def test_drop_needs_a_note_and_gate_catches_gaps(flow: Path) -> None:
    receipt(flow, "implement", "OK", "OUTSTANDING: Step 3 (tests missing) [self-report]\n")
    assess.assess_records_path(flow).write_text(RECORD.format(id="implement:Step 3", status="unfinished") + "\n" + ASSESS_SENTINEL + "\n", encoding="utf-8")
    run_next(flow)
    expect(assess.save_item_answer(flow, 1, "implement:Step 3", "Drop with a reason", None, set()) is not None, "Drop needs a note")
    (flow / "answers").mkdir(exist_ok=True)
    (flow / "answers" / "assess-1.json").write_text(json.dumps({"answers": {}, "annotations": {}}), encoding="utf-8")
    expect(assess.invoke_outstanding_gate(flow, 1) is not None, "an unanswered asked item blocks the gate")


def test_save_item_answer_refuses_invalid_options(flow: Path) -> None:
    receipt(flow, "implement", "OK", "OUTSTANDING: Step 3 (tests missing) [self-report]\n")
    assess.assess_records_path(flow).write_text(RECORD.format(id="implement:Step 3", status="unfinished") + "\n" + ASSESS_SENTINEL + "\n", encoding="utf-8")
    run_next(flow)
    err = assess.save_item_answer(flow, 1, "implement:Step 3", "InvalidOption", None, set())
    expect(err is not None, "an invalid option must be refused")
    err = assess.save_item_answer(flow, 1, "unknown:Item", "Finish it", None, set())
    expect(err is not None, "an unknown item must be refused")


def test_assess_disposition_gate_requires_all_answered(flow: Path) -> None:
    receipt(flow, "implement", "OK", "OUTSTANDING: Step 3 (tests missing) [self-report]\nOUTSTANDING: Step 4 (more) [verifier]\n")
    assess.assess_records_path(flow).write_text(
        RECORD.format(id="implement:Step 3", status="unfinished") + "\n" +
        RECORD.format(id="implement:Step 4", status="unfinished") + "\n" + ASSESS_SENTINEL + "\n",
        encoding="utf-8"
    )
    run_next(flow)
    (flow / "answers").mkdir(exist_ok=True)
    (flow / "answers" / "assess-1.json").write_text(json.dumps({"answers": {"implement:Step 3": "Finish it"}, "annotations": {}}), encoding="utf-8")
    err = assess.invoke_outstanding_gate(flow, 1)
    expect(err is not None, "gate must fail when not all items are answered")
    (flow / "answers" / "assess-1.json").write_text(json.dumps({"answers": {"implement:Step 3": "Leave as-is", "implement:Step 4": "Leave as-is"}, "annotations": {}}), encoding="utf-8")
    err = assess.invoke_outstanding_gate(flow, 1)
    expect(err is None, "gate must pass when all items are answered")


def test_assess_disposition_gate_fails_on_missing_questions_file(flow: Path) -> None:
    receipt(flow, "implement", "OK", "OUTSTANDING: Step 3 (tests missing) [self-report]\n")
    assess.assess_records_path(flow).write_text(RECORD.format(id="implement:Step 3", status="unfinished") + "\n" + ASSESS_SENTINEL + "\n", encoding="utf-8")
    run_next(flow)
    (flow / "answers").mkdir(exist_ok=True)
    (flow / "answers" / "assess-1.json").write_text(json.dumps({"answers": {"implement:Step 3": "Finish it"}, "annotations": {}}), encoding="utf-8")
    (flow / "questions" / "assess-1.json").unlink(missing_ok=True)
    err = assess.invoke_outstanding_gate(flow, 1)
    expect(err is not None, "gate must fail closed when questions file is missing")


def test_check_ref_valid(flow: Path) -> None:
    """_check_ref should return None for valid refs (strings without '-')."""
    result = assess._check_ref("main")
    expect(result is None, f"valid ref 'main' returned {result!r}, expected None")
    result = assess._check_ref("abc123def")
    expect(result is None, f"valid ref 'abc123def' returned {result!r}, expected None")


def test_check_ref_non_string(flow: Path) -> None:
    """_check_ref should reject non-strings."""
    result = assess._check_ref(123)
    expect(result is not None and "must be a string" in result,
           f"non-string ref returned {result!r}, expected error about string type")


def test_check_ref_dash_prefix(flow: Path) -> None:
    """_check_ref should reject refs starting with '-'."""
    result = assess._check_ref("-foo")
    expect(result is not None and "starts with" in result,
           f"ref '-foo' returned {result!r}, expected error about dash")


def test_resolve_anchor_valid(flow: Path) -> None:
    """_resolve_anchor should return None for valid paths inside the worktree."""
    wt = flow / "wt"
    wt.mkdir()
    # Initialize a git repo
    subprocess.run(["git", "-C", str(wt), "init", "-q"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "init"], check=True, capture_output=True)

    # Path to a file inside the worktree
    test_file = wt / "test.txt"
    test_file.write_text("test", encoding="utf-8")

    result = assess._resolve_anchor(str(wt), "test.txt")
    expect(result is None, f"valid anchor 'test.txt' returned {result!r}, expected None")


def test_resolve_anchor_escaping(flow: Path) -> None:
    """_resolve_anchor should reject paths with '..' or leading '/'."""
    wt = flow / "wt"
    wt.mkdir()
    subprocess.run(["git", "-C", str(wt), "init", "-q"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "init"], check=True, capture_output=True)

    result = assess._resolve_anchor(str(wt), "../outside")
    expect(result is not None and "escapes" in result,
           f"escaped path returned {result!r}, expected error about escaping")

    result = assess._resolve_anchor(str(wt), "/absolute")
    expect(result is not None and "escapes" in result,
           f"absolute path returned {result!r}, expected error about escaping")


def test_resolve_anchor_toplevel(flow: Path) -> None:
    """_resolve_anchor should accept the toplevel directory itself."""
    wt = flow / "wt"
    wt.mkdir()
    subprocess.run(["git", "-C", str(wt), "init", "-q"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "init"], check=True, capture_output=True)

    result = assess._resolve_anchor(str(wt), ".")
    expect(result is None, f"toplevel anchor '.' returned {result!r}, expected None")


TESTS = [
    ("1: the set reads OK receipts only", test_set_reads_ok_receipts),
    ("2: empty set and malformed line", test_set_empty_and_malformed),
    ("3: nothing outstanding advances", test_nothing_outstanding_advances),
    ("4: spawn, ask, answer, gate", test_spawn_then_ask_then_gate),
    ("5: bad assessor records stop", test_bad_records_stop),
    ("6: done items ask nothing", test_done_items_ask_nothing),
    ("7: Drop note and gate gaps", test_drop_needs_a_note_and_gate_catches_gaps),
    ("8: save_item_answer refuses invalid options", test_save_item_answer_refuses_invalid_options),
    ("9: assess disposition gate requires all answered", test_assess_disposition_gate_requires_all_answered),
    ("10: assess disposition gate fails on missing questions file", test_assess_disposition_gate_fails_on_missing_questions_file),
    ("11: _check_ref accepts valid ref", test_check_ref_valid),
    ("12: _check_ref rejects non-string", test_check_ref_non_string),
    ("13: _check_ref rejects dash-prefix", test_check_ref_dash_prefix),
    ("14: _resolve_anchor accepts valid path", test_resolve_anchor_valid),
    ("15: _resolve_anchor rejects escaping", test_resolve_anchor_escaping),
    ("16: _resolve_anchor accepts toplevel", test_resolve_anchor_toplevel),
]


def main() -> None:
    h = Harness("FLOW DRIVER ASSESS NEXT")
    for name, fn in TESTS:
        check(h, name, fn)
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
