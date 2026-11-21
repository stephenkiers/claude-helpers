#!/usr/bin/env python3
"""
Tests for relay.py, written from its documented contract: question/answer paths per kind, NN allocation, park re-entry,
consumed-answer archival, exact-match answer validation, exclusive answer files, the render failure marker,
and invoke_review_ruling argv handling.

Run with: python3 tests/test_flow_driver_relay.py
"""

import json
import sys
import tempfile
from pathlib import Path
from typing import Callable, List, Optional
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from _test_harness import Harness  # noqa: E402

from scripts.flow_driver import relay  # noqa: E402
from scripts.flow_driver.state import InitSpec, ParkedQuestion, init_state, mutating  # noqa: E402


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


def init(flow: Path) -> None:
    spec = InitSpec(repo="o/n", issue=1, worktree=str(flow))
    with mutating(flow, create=spec) as locked:
        init_state(locked, spec)


def park(flow: Path, step: str, attempt: int, kind: str, nn: Optional[int] = None) -> ParkedQuestion:
    with mutating(flow) as locked:
        return relay.park(locked, flow, step, attempt, kind, nn=nn)  # type: ignore[arg-type]


def write_question(flow: Path, parked: ParkedQuestion, options: object) -> None:
    path = relay.question_path(flow, parked)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"summary": "s", "options": options}), encoding="utf-8")


def refused(flow: Path, text: str, note: Optional[str] = None) -> str:
    try:
        relay.accept_answer(flow, text, note)
    except relay.AnswerRefused as exc:
        return str(exc)
    return ""


def test_paths_per_kind(flow: Path) -> None:
    step = ParkedQuestion(step="plan", attempt=2, nn=3, kind="step")
    expect(relay.question_path(flow, step) == flow / "questions" / "plan-03.json", "step question path is v1 shape")
    expect(relay.answer_path(flow, step) == flow / "answers" / "plan-a2.json", "step answer path")
    gate = ParkedQuestion(step="plan", attempt=2, nn=1, kind="gate")
    expect(relay.question_path(flow, gate) == flow / "questions" / "plan-a2-gate.json", "gate question path")
    expect(relay.answer_path(flow, gate) == flow / "answers" / "plan-a2-gate.json", "gate answer path")
    assess = ParkedQuestion(step="assess", attempt=1, nn=1, kind="assess")
    expect(relay.question_path(flow, assess) == flow / "questions" / "assess-1.json", "assess question path")
    expect(relay.answer_path(flow, assess) == flow / "answers" / "assess-1.json", "assess answer path")


def test_park_allocates_and_reenters(flow: Path) -> None:
    init(flow)
    (flow / "questions").mkdir()
    (flow / "questions" / "plan-01.json").write_text("{}", encoding="utf-8")
    first = park(flow, "plan", 1, "step")
    expect(first.nn == 2, f"NN skips the used 01: {first.nn}")
    again = park(flow, "plan", 1, "step")
    expect(again.nn == first.nn, "re-entry for the same step and attempt keeps the NN")


def test_park_nn_counts_archived_answers(flow: Path) -> None:
    init(flow)
    (flow / "archive" / "plan").mkdir(parents=True)
    (flow / "archive" / "plan" / "a1.answer-1.json").write_text("{}", encoding="utf-8")
    expect(park(flow, "plan", 2, "step").nn == 2, "an archived answer-01 reserves NN 1")


def test_park_non_step_needs_nn(flow: Path) -> None:
    init(flow)
    try:
        park(flow, "plan", 1, "gate")
    except ValueError:
        return
    raise AssertionError("a non-step park without nn must raise")


def test_archive_consumed(flow: Path) -> None:
    parked = ParkedQuestion(step="plan", attempt=1, nn=2, kind="step")
    relay.archive_consumed(flow, parked)  # no answer file: no-op, must not raise
    target = relay.answer_path(flow, parked)
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")
    relay.archive_consumed(flow, parked)
    expect(not target.exists(), "the answer was not moved")
    expect((flow / "archive" / "plan" / "a1.answer-2.json").exists(), "the answer was not archived under its NN")


def test_nothing_parked_refused(flow: Path) -> None:
    init(flow)
    expect("answer refused: nothing is parked" in refused(flow, "x"), "answering with nothing parked must refuse")


def test_exact_option_match(flow: Path) -> None:
    init(flow)
    parked = park(flow, "plan", 1, "step")
    write_question(flow, parked, ["Continue", "Abort"])
    expect(refused(flow, "continue").startswith("answer refused:"), "options match exactly, case included")
    expect(not relay.answer_path(flow, parked).exists(), "a refused answer wrote nothing")
    ok, _ = relay.accept_answer(flow, "  Continue \n", "because")
    expect(ok, "surrounding whitespace is stripped")
    data = json.loads(relay.answer_path(flow, parked).read_text(encoding="utf-8"))
    expect(data["text"] == "Continue" and data["note"] == "because" and data["attempt"] == 1, str(data))


def test_answer_is_exclusive(flow: Path) -> None:
    init(flow)
    parked = park(flow, "plan", 1, "step")
    write_question(flow, parked, ["Continue"])
    relay.accept_answer(flow, "Continue", None)
    expect("already exists" in refused(flow, "Continue"), "a second answer must refuse")


def test_free_text_accepts_anything(flow: Path) -> None:
    init(flow)
    parked = park(flow, "plan", 1, "step")
    write_question(flow, parked, "free_text")
    ok, _ = relay.accept_answer(flow, "whatever I like", None)
    expect(ok, "free_text takes any text")
    data = json.loads(relay.answer_path(flow, parked).read_text(encoding="utf-8"))
    expect("note" not in data, "no note given, none recorded")


def test_missing_or_malformed_question_refused(flow: Path) -> None:
    init(flow)
    park(flow, "plan", 1, "step")
    expect(refused(flow, "x").startswith("answer refused:"), "a missing question file must refuse")
    parked = park(flow, "plan", 1, "step")
    path = relay.question_path(flow, parked)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")
    expect(refused(flow, "x").startswith("answer refused:"), "a malformed question file must refuse")


def test_render_question(flow: Path) -> None:
    good = flow / "good.json"
    good.write_text(json.dumps({"summary": "Pick one", "questions": [{"id": "q1", "question": "Which?", "header": "Pick", "multiSelect": False, "options": [{"label": "Alpha", "description": "a"}, {"label": "Beta", "description": "b"}]}]}), encoding="utf-8")
    out = relay.render_question(flow, good)
    expect(not out.startswith("(render") and "Alpha" in out, f"a valid question did not render: {out[:120]}")
    bad = flow / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    out = relay.render_question(flow, bad)
    expect(out.startswith("(render"), f"an invalid question must return a (render marker: {out[:120]}")


def test_park_reentry_raises_on_kind_mismatch(flow: Path) -> None:
    init(flow)
    parked = park(flow, "plan", 1, "step")
    expect(parked.kind == "step", "initial park is kind=step")
    try:
        park(flow, "plan", 1, "gate")
    except (ValueError, RuntimeError) as exc:
        expect("mismatch" in str(exc).lower() or "kind" in str(exc).lower() or "is not" in str(exc).lower(), f"error message must mention mismatch: {exc}")
        return
    raise AssertionError("re-entry with different kind must raise")


def test_park_reentry_raises_on_item_mismatch(flow: Path) -> None:
    init(flow)
    with mutating(flow) as locked:
        parked = relay.park(locked, flow, "assess", 1, "assess", nn=1, item="fix:Item 1")
    expect(parked.item == "fix:Item 1", "initial park has item=fix:Item 1")
    try:
        with mutating(flow) as locked:
            relay.park(locked, flow, "assess", 1, "assess", nn=1, item="implement:Item 2")
    except (ValueError, RuntimeError) as exc:
        expect("mismatch" in str(exc).lower() or "item" in str(exc).lower() or "is not" in str(exc).lower(), f"error message must mention mismatch: {exc}")
        return
    raise AssertionError("re-entry with different item must raise")


def test_next_gate_nn_allocates_monotonic(flow: Path) -> None:
    """next_gate_nn should return a monotonic sequence based on archived answers."""
    init(flow)
    # Initial call with no archives returns 1
    nn = relay.next_gate_nn(flow, "plan", 1)
    expect(nn == 1, f"first NN should be 1, got {nn}")

    # Archive the first answer (at NN 1)
    (flow / "archive" / "plan").mkdir(parents=True)
    (flow / "archive" / "plan" / "a1.answer-1.json").write_text("{}", encoding="utf-8")

    # Next call should return 2
    nn = relay.next_gate_nn(flow, "plan", 1)
    expect(nn == 2, f"NN after one archive should be 2, got {nn}")

    # Archive the second answer (at NN 2)
    (flow / "archive" / "plan" / "a1.answer-2.json").write_text("{}", encoding="utf-8")

    # Next call should return 3
    nn = relay.next_gate_nn(flow, "plan", 1)
    expect(nn == 3, f"NN after two archives should be 3, got {nn}")


def test_archive_answer_uses_monotonic_nn(flow: Path) -> None:
    """archive_answer should use next_gate_nn and never overwrite prior archives."""
    init(flow)
    parked = ParkedQuestion(step="merge", attempt=1, nn=1, kind="gate")

    # First answer
    (flow / "answers").mkdir(parents=True)
    answer_path = relay.answer_path(flow, parked)
    answer_path.write_text(json.dumps({"text": "first answer"}), encoding="utf-8")
    relay.archive_answer(flow, parked)

    # Verify the first answer was archived at the right path
    expect((flow / "archive" / "merge" / "a1.answer-1.json").exists(), "first archive should exist at a1.answer-1.json")
    expect(json.loads((flow / "archive" / "merge" / "a1.answer-1.json").read_text(encoding="utf-8"))["text"] == "first answer", "first archive has correct content")

    # Second answer (simulating a retry)
    answer_path.write_text(json.dumps({"text": "second answer"}), encoding="utf-8")
    relay.archive_answer(flow, parked)

    # Verify the second answer was archived at a different NN
    expect((flow / "archive" / "merge" / "a1.answer-2.json").exists(), "second archive should exist at a1.answer-2.json")
    expect(json.loads((flow / "archive" / "merge" / "a1.answer-2.json").read_text(encoding="utf-8"))["text"] == "second answer", "second archive has correct content")
    expect(json.loads((flow / "archive" / "merge" / "a1.answer-1.json").read_text(encoding="utf-8"))["text"] == "first answer", "first archive not overwritten")


def test_invoke_review_ruling_argv_with_option(flow: Path) -> None:
    """invoke_review_ruling builds correct argv with --option= for valid options."""
    init(flow)
    parked = ParkedQuestion(step="review", attempt=1, nn=1, kind="review", item=None)

    # Create a question file with a review context and valid options
    review_dir = flow / "reviews" / "test-review"
    review_dir.mkdir(parents=True)
    action_plan = review_dir / "claude-action-plan.md"
    action_plan.write_text("# Plan\n### 1. Some decision\ntext", encoding="utf-8")

    question_path = relay.question_path(flow, parked)
    question_path.parent.mkdir(parents=True, exist_ok=True)
    question_path.write_text(json.dumps({
        "summary": "Test question",
        "options": ["Accept", "Reject"],
        "context_path": str(action_plan),
        "brief": "### 1. Test item",
    }), encoding="utf-8")

    # Mock the run_command to capture the argv
    captured_argv = []
    def mock_run_command(argv):
        captured_argv.extend(argv)
        return MagicMock(returncode=0, stderr="")

    with patch("scripts.flow_driver.preflight.run_command", mock_run_command):
        relay.invoke_review_ruling(flow, parked, "Accept", "my reason")

    # Verify the argv structure
    expect(len(captured_argv) > 0, "run_command was not called")
    expect("review-ruling.py" in captured_argv[1], f"script path not found in {captured_argv}")
    expect("decide" in captured_argv, "decide command not found")
    expect(f"--review-dir={str(review_dir)}" in captured_argv or "--review-dir" in captured_argv, f"review dir not found in {captured_argv}")
    expect("--option=Accept" in captured_argv, f"--option=Accept not in {captured_argv}")
    expect("--reasoning=my reason" in captured_argv, f"--reasoning not found in {captured_argv}")


def test_invoke_review_ruling_argv_with_custom(flow: Path) -> None:
    """invoke_review_ruling builds correct argv with --custom= for custom text."""
    init(flow)
    parked = ParkedQuestion(step="review", attempt=1, nn=1, kind="review", item=None)

    review_dir = flow / "reviews" / "test-review"
    review_dir.mkdir(parents=True)
    action_plan = review_dir / "claude-action-plan.md"
    action_plan.write_text("# Plan\n### 2. Another decision\ntext", encoding="utf-8")

    question_path = relay.question_path(flow, parked)
    question_path.parent.mkdir(parents=True, exist_ok=True)
    question_path.write_text(json.dumps({
        "summary": "Test question",
        "options": ["Accept", "Reject"],
        "context_path": str(action_plan),
        "brief": "### 2. Test item",
    }), encoding="utf-8")

    captured_argv = []
    def mock_run_command(argv):
        captured_argv.extend(argv)
        return MagicMock(returncode=0, stderr="")

    with patch("scripts.flow_driver.preflight.run_command", mock_run_command):
        relay.invoke_review_ruling(flow, parked, "Custom reason text", "my reason")

    # Verify custom text is passed with --custom=
    expect("--custom=Custom reason text" in captured_argv, f"--custom not in {captured_argv}")
    expect("--option=" not in " ".join(captured_argv) or "--option=Custom" not in captured_argv, "should use --custom not --option for custom text")


def test_invoke_review_ruling_surfaces_failure(flow: Path) -> None:
    """invoke_review_ruling raises RuntimeError when script fails."""
    init(flow)
    parked = ParkedQuestion(step="review", attempt=1, nn=1, kind="review", item=None)

    review_dir = flow / "reviews" / "test-review"
    review_dir.mkdir(parents=True)
    action_plan = review_dir / "claude-action-plan.md"
    action_plan.write_text("# Plan\n### 3. Item\ntext", encoding="utf-8")

    question_path = relay.question_path(flow, parked)
    question_path.parent.mkdir(parents=True, exist_ok=True)
    question_path.write_text(json.dumps({
        "summary": "Test question",
        "options": ["Accept"],
        "context_path": str(action_plan),
        "brief": "### 3. Test item",
    }), encoding="utf-8")

    def mock_run_command_fail(argv):
        return MagicMock(returncode=3, stderr="Conflict: prior ruling exists\n")

    try:
        with patch("scripts.flow_driver.preflight.run_command", mock_run_command_fail):
            relay.invoke_review_ruling(flow, parked, "Accept", None)
        expect(False, "invoke_review_ruling should raise on script failure")
    except RuntimeError as exc:
        expect("review-ruling.py failed" in str(exc) and "Conflict" in str(exc), f"error message not informative: {exc}")


TESTS: List = [
    ("1: paths per kind", test_paths_per_kind),
    ("2: park allocates NN and re-enters", test_park_allocates_and_reenters),
    ("3: archived answers reserve NNs", test_park_nn_counts_archived_answers),
    ("4: non-step park needs an nn", test_park_non_step_needs_nn),
    ("5: archive_consumed moves the answer", test_archive_consumed),
    ("6: nothing parked is refused", test_nothing_parked_refused),
    ("7: options match exactly", test_exact_option_match),
    ("8: an answer file is exclusive", test_answer_is_exclusive),
    ("9: free text accepts anything", test_free_text_accepts_anything),
    ("10: bad question files refuse", test_missing_or_malformed_question_refused),
    ("11: render succeeds and fails as a marker", test_render_question),
    ("12: park re-entry raises on kind mismatch", test_park_reentry_raises_on_kind_mismatch),
    ("13: park re-entry raises on item mismatch", test_park_reentry_raises_on_item_mismatch),
    ("14: next_gate_nn allocates monotonic NN", test_next_gate_nn_allocates_monotonic),
    ("15: archive_answer uses monotonic NN never overwrites", test_archive_answer_uses_monotonic_nn),
    ("16: invoke_review_ruling builds argv with --option=", test_invoke_review_ruling_argv_with_option),
    ("17: invoke_review_ruling builds argv with --custom=", test_invoke_review_ruling_argv_with_custom),
    ("18: invoke_review_ruling surfaces script failure", test_invoke_review_ruling_surfaces_failure),
]


def main() -> None:
    h = Harness("FLOW DRIVER RELAY")
    for name, fn in TESTS:
        check(h, name, fn)
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
