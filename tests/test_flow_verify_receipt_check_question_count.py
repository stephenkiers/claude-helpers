#!/usr/bin/env python3
"""
Tests that the plan step's `asked` count excludes the plan-time check-command question.

The orchestrator asks the check-command question at plan time and writes it into questions/plan-NN.json
with `command: expert-flow`. That question is not a plan brief, so count_relayed_questions() called for
the plan step (with exclude_command="expert-flow") must leave it out of `asked`. A question file from
any other command (here `expert-plan`) must still be counted. Unanswered check questions stay selected,
so they are still reported as unanswered.

Driven in-process through count_relayed_questions(), with fixtures built on disk the way the verifier
reads them: questions/<step>-NN.json with a matching attempt, and answers/<step>-NN.json when answered.

Runs under pytest (`python3 -m pytest tests/test_flow_verify_receipt_check_question_count.py`) and as
a script (`python3 tests/test_flow_verify_receipt_check_question_count.py`).
"""

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
script_path = REPO_ROOT / "scripts" / "flow-verify-receipt.py"
spec = importlib.util.spec_from_file_location("flow_verify_receipt", script_path)
flow_verify_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flow_verify_module)
count_relayed_questions = flow_verify_module.count_relayed_questions

ATTEMPT = 1
RECEIPT = f"ATTEMPT: {ATTEMPT}\n"


def write_question_file(flow_dir: Path, name: str, command: str, question_ids: list) -> None:
    """Write questions/<name> with one question per id, and its answers file."""
    qdir = flow_dir / "questions"
    adir = flow_dir / "answers"
    qdir.mkdir(parents=True, exist_ok=True)
    adir.mkdir(parents=True, exist_ok=True)
    payload = {
        "step": "plan",
        "attempt": ATTEMPT,
        "command": command,
        "questions": [
            {"id": qid, "brief": None, "question": f"{qid}?", "header": None,
             "options": [{"label": "Yes", "description": ""}]}
            for qid in question_ids
        ],
    }
    (qdir / name).write_text(json.dumps(payload), encoding="utf-8")
    (adir / name).write_text(json.dumps({"kind": "answers", "attempt": ATTEMPT}), encoding="utf-8")


def write_unanswered_question_file(flow_dir: Path, name: str, command: str, question_ids: list) -> None:
    """Write questions/<name> only, with no answers file."""
    qdir = flow_dir / "questions"
    qdir.mkdir(parents=True, exist_ok=True)
    payload = {
        "step": "plan",
        "attempt": ATTEMPT,
        "command": command,
        "questions": [
            {"id": qid, "brief": None, "question": f"{qid}?", "header": None,
             "options": [{"label": "Yes", "description": ""}]}
            for qid in question_ids
        ],
    }
    (qdir / name).write_text(json.dumps(payload), encoding="utf-8")


def test_check_question_excluded_from_plan_asked(tmp_path: Path) -> None:
    """One expert-flow check question plus two real plan questions: asked counts only the two plan questions."""
    flow_dir = tmp_path / "flow"
    write_question_file(flow_dir, "plan-01.json", "expert-plan", ["q1", "q2"])
    write_question_file(flow_dir, "plan-02.json", "expert-flow", ["q1"])

    result = count_relayed_questions(flow_dir, "plan", RECEIPT, ATTEMPT, exclude_command="expert-flow")

    assert result.asked == 2, f"asked={result.asked}, expected 2 (check question excluded)"
    assert result.unanswered == []
    assert sorted(result.files) == ["plan-01.json", "plan-02.json"]


def test_non_expert_flow_question_still_counted(tmp_path: Path) -> None:
    """A plan question from another command is still counted when the check question is excluded."""
    flow_dir = tmp_path / "flow"
    write_question_file(flow_dir, "plan-01.json", "expert-plan", ["q1"])
    write_question_file(flow_dir, "plan-02.json", "expert-flow", ["q1", "q2"])
    write_question_file(flow_dir, "plan-03.json", "expert-plan", ["q1", "q2", "q3"])

    result = count_relayed_questions(flow_dir, "plan", RECEIPT, ATTEMPT, exclude_command="expert-flow")

    assert result.asked == 4, f"asked={result.asked}, expected 4 (1 + 3 plan questions; 2 check questions excluded)"


def test_no_exclusion_counts_check_question(tmp_path: Path) -> None:
    """Without exclude_command the check question is counted, so the default path is unchanged."""
    flow_dir = tmp_path / "flow"
    write_question_file(flow_dir, "plan-01.json", "expert-plan", ["q1", "q2"])
    write_question_file(flow_dir, "plan-02.json", "expert-flow", ["q1"])

    result = count_relayed_questions(flow_dir, "plan", RECEIPT, ATTEMPT)

    assert result.asked == 3, f"asked={result.asked}, expected 3 with no exclusion"


def test_unanswered_check_question_still_reported(tmp_path: Path) -> None:
    """An unanswered check question is excluded from asked but still listed as unanswered."""
    flow_dir = tmp_path / "flow"
    write_question_file(flow_dir, "plan-01.json", "expert-plan", ["q1"])
    write_unanswered_question_file(flow_dir, "plan-02.json", "expert-flow", ["q1"])

    result = count_relayed_questions(flow_dir, "plan", RECEIPT, ATTEMPT, exclude_command="expert-flow")

    assert result.asked == 1, f"asked={result.asked}, expected 1"
    assert result.unanswered == ["plan-02.json"], f"unanswered={result.unanswered}"


def main() -> int:
    cases = (
        test_check_question_excluded_from_plan_asked,
        test_non_expert_flow_question_still_counted,
        test_no_exclusion_counts_check_question,
        test_unanswered_check_question_still_reported,
    )
    failures = []
    for case in cases:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                case(Path(tmp))
                print(f"PASS {case.__name__}")
            except AssertionError as exc:
                print(f"FAIL {case.__name__}: {exc}")
                failures.append(case.__name__)
    print(f"Passed: {len(cases) - len(failures)}  Failed: {len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
