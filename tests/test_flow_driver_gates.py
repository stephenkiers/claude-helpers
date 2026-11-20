#!/usr/bin/env python3
"""
Tests for scripts/flow_driver/gates.py: step gates, the merge gate, and the sole-constructor rule for MergeDirective.

The PR readers are injected fakes, so nothing here touches gh or git.

Run with: python3 tests/test_flow_driver_gates.py
"""

import ast
import sys
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from _test_harness import Harness  # noqa: E402

from scripts.flow_driver import gates, recovery, shipguard as sg  # noqa: E402
from scripts.flow_driver.state import Heads, MergeGateRecord, Sha, V2State  # noqa: E402

FLOW = Path("/nonexistent/flow")
HEAD = Sha("a" * 40)
OTHER = Sha("b" * 40)


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check(h: Harness, name: str, fn: Callable[[], None]) -> None:
    try:
        fn()
    except AssertionError as exc:
        h.test_result(name, False, str(exc) or "assertion failed")
    except Exception as exc:
        h.test_result(name, False, f"{type(exc).__name__}: {exc}")
    else:
        h.test_result(name, True)


def make_state(**kw: object) -> V2State:
    base = dict(
        driver_format=2, run_id="r", issue=1, repo="o/n", created="t", updated="t", step="ship", status="running",
        live=None, pending_spawn=None, attempts={}, failures={}, stop_reason={}, stop_source={}, pr=None,
        worktree=None, healed=False, attempt_log={}, applied=(), extra={}, heads=Heads(ship=HEAD),
    )
    base.update(kw)
    return V2State(**base)  # type: ignore[arg-type]


def facts(landed: str, **kw: object) -> gates.GateFacts:
    return gates.GateFacts(flow=FLOW, nonce="n0", landed=landed, **kw)  # type: ignore[arg-type]


def no_pr(*a: object, **k: object) -> sg.ShipGuardResult:
    raise AssertionError("PR reader must not be called")


def by_branch(result: sg.ShipGuardResult) -> gates.PrReader:
    def reader(branch: str, expected: Sha, run: object = None, repo: Optional[str] = None) -> sg.ShipGuardResult:  # type: ignore[assignment]
        return result
    return reader  # type: ignore[return-value]


def by_number(result: sg.MergeVerdict) -> gates.PrNumberReader:
    def reader(number: int, repo: str, expected: Sha, run: object = None) -> sg.MergeVerdict:  # type: ignore[assignment]
        return result
    return reader  # type: ignore[return-value]


def test_gates_off_advances() -> None:
    expect(isinstance(gates.decide(make_state(), facts("plan")), gates.Advance), "step gates off must advance")


def test_step_gate_parks_with_options() -> None:
    out = gates.decide(make_state(step_gates=True), facts("plan"))
    expect(isinstance(out, gates.Park), f"got {type(out).__name__}")
    expect(out.spec.options == ("Continue", "Redo with notes", "Pause", "Abort"), str(out.spec.options))
    out = gates.decide(make_state(step_gates=True), facts("implement"))
    expect(isinstance(out, gates.Park) and "Redo with notes" not in out.spec.options, "implement offers no redo")


def test_step_gate_answers() -> None:
    s = make_state(step_gates=True)
    expect(isinstance(gates.decide(s, facts("review", answer="Continue")), gates.Advance), "Continue advances")
    pause = gates.decide(s, facts("review", answer="Pause"))
    expect(isinstance(pause, gates.Stop) and pause.status == "paused", "Pause is a paused stop")
    abort = gates.decide(s, facts("review", answer="Abort"))
    expect(isinstance(abort, gates.Stop) and abort.code == "gate-aborted", "Abort stops gate-aborted")
    bad = gates.decide(s, facts("review", answer="Yes please"))
    expect(isinstance(bad, gates.Stop), "an answer outside the options never advances")
    redo = gates.decide(s, facts("review", answer="Redo with notes", note="more tests"))
    expect(isinstance(redo, gates.Redo) and redo.note == "more tests", "Redo carries the note")


def test_redo_cap() -> None:
    log = {"plan": {1: "redone", 2: "redone"}}
    s = make_state(step_gates=True, attempt_log=log)
    out = gates.decide(s, facts("plan", answer="Redo with notes"))
    expect(isinstance(out, gates.Stop) and out.code == "redo-cap", f"cap is {recovery.REDO_CAP}")


def test_merge_gate_asks_even_when_healed() -> None:
    s = make_state(healed=True)
    out = gates.decide(s, facts("ship", branch="b", check_pr=by_branch(sg.SameHead(7, HEAD)), heal_commits=("Flow-Heal: x",)))
    expect(isinstance(out, gates.RecordMergeGate), f"got {type(out).__name__}")
    expect(out.park.spec.options == ("Merge", "Hold", "Take over"), str(out.park.spec.options))
    expect(out.record.approved_head == HEAD and out.record.pr_number == 7, "identity recorded")


def test_merge_gate_already_merged_has_no_question() -> None:
    out = gates.decide(make_state(), facts("ship", branch="b", check_pr=by_branch(sg.Merged(7))))
    expect(isinstance(out, gates.MergeDone), f"got {type(out).__name__}")


def test_merge_gate_stops() -> None:
    cases = [
        (sg.HeadDiffers(7, HEAD, OTHER), "merge-head-mismatch"),
        (sg.Closed(7), "merge-not-merged"),
        (sg.NoPr(), "gh-error"),
        (sg.GhError("boom"), "gh-error"),
    ]
    for result, code in cases:
        out = gates.decide(make_state(), facts("ship", branch="b", check_pr=by_branch(result)))
        expect(isinstance(out, gates.Stop) and out.code == code, f"{type(result).__name__}: {out}")


def test_merge_answer_rereads_by_number() -> None:
    rec = MergeGateRecord(pr_number=7, approved_head=HEAD, repo="o/n")
    s = make_state(merge_gate=rec)
    ok = gates.decide(s, facts("ship", answer="Merge", check_pr=no_pr, check_pr_by_number=by_number(sg.NotMerged(7, "OPEN"))))  # type: ignore[arg-type]
    expect(isinstance(ok, gates.MergeSpawn) and ok.directive.step == "merge", f"got {ok}")
    moved = gates.decide(s, facts("ship", answer="Merge", check_pr_by_number=by_number(sg.HeadMismatch(7, HEAD, OTHER))))
    expect(isinstance(moved, gates.Stop) and moved.code == "merge-head-mismatch", "a moved head never merges")
    err = gates.decide(s, facts("ship", answer="Merge", check_pr_by_number=by_number(sg.GhError("x"))))
    expect(isinstance(err, gates.Stop) and err.code == "gh-error", "a read failure never merges")
    hold = gates.decide(s, facts("ship", answer="Hold", check_pr_by_number=by_number(sg.NotMerged(7, "OPEN"))))
    expect(isinstance(hold, gates.Stop) and hold.status == "paused", "Hold pauses")
    junk = gates.decide(s, facts("ship", answer="merge now", check_pr_by_number=by_number(sg.NotMerged(7, "OPEN"))))
    expect(isinstance(junk, gates.Stop), "free text never merges")
    unanswered = gates.decide(s, facts("ship", check_pr_by_number=by_number(sg.NotMerged(7, "OPEN"))))
    expect(isinstance(unanswered, gates.Park), "no answer re-asks")


def test_only_gates_constructs_merge_directive() -> None:
    root = Path(__file__).parent.parent / "scripts"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "gates.py" and path.parent.name == "flow_driver":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
                if name == "MergeDirective":
                    offenders.append(f"{path}:{node.lineno}")
    expect(not offenders, f"MergeDirective constructed outside gates.py: {offenders}")


def test_unrecognized_merge_answer_maps_to_distinct_code() -> None:
    rec = MergeGateRecord(pr_number=7, approved_head=HEAD, repo="o/n")
    s = make_state(merge_gate=rec)
    result = gates.decide(s, facts("ship", answer="Merge later maybe", check_pr_by_number=by_number(sg.NotMerged(7, "OPEN"))))
    expect(isinstance(result, gates.Stop), f"unrecognized answer should be a Stop, got {type(result).__name__}")
    expect(result.code != "take-over", f"unrecognized answer must map to a distinct code, not take-over, got {result.code}")
    expect(result.code == "gate-aborted", f"expected gate-aborted, got {result.code}")


SECTIONS = [
    ("[Step gates]", [
        ("1: gates off advances", test_gates_off_advances),
        ("2: parks with the exact options", test_step_gate_parks_with_options),
        ("3: answers map to outcomes", test_step_gate_answers),
        ("4: redo cap", test_redo_cap),
    ]),
    ("[Merge gate]", [
        ("5: asks even when healed", test_merge_gate_asks_even_when_healed),
        ("6: already merged asks nothing", test_merge_gate_already_merged_has_no_question),
        ("7: PR states that stop", test_merge_gate_stops),
        ("8: the answer re-reads by number", test_merge_answer_rereads_by_number),
        ("9: only gates.py constructs MergeDirective", test_only_gates_constructs_merge_directive),
        ("10: unrecognized merge answer maps to gate-aborted", test_unrecognized_merge_answer_maps_to_distinct_code),
    ]),
]


def main() -> None:
    h = Harness("FLOW DRIVER GATES")
    for title, checks in SECTIONS:
        print(title)
        for name, fn in checks:
            check(h, name, fn)
        print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
