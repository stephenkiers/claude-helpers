#!/usr/bin/env python3
"""
Regression test for the #335 multi-round plan checkpoint, re-checked against main (#341, item 6).

The fixture follows the "Report variant" example in prompts/flow-reference.md rather than a shape
built to suit the verifier:

- Round 1 asks D1 (decided, not must-answer) and Q1 (question, must-answer). The human answers Q1.
- Round 2 re-asks Q1 (must-answer, reason "changed") and the human overrides it. D1 is not in
  round 2's file and stands as accepted, per "Unmentioned D and W items stand".

It must pass check_report_checkpoint(), and the answers-file jq from commands/expert-flow.md
(the plan checkpoint verify step, "Verify the answers file") must accept the final round. A final
round left `unresolved` must be rejected by both.

Run with: python3 tests/test_flow_verify_receipt_plan_regression.py
"""

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness

script_path = Path(__file__).parent.parent / "scripts" / "flow-verify-receipt.py"
spec = importlib.util.spec_from_file_location("flow_verify_receipt", script_path)
flow_verify_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flow_verify_module)
check_report_checkpoint = flow_verify_module.check_report_checkpoint

# The answers-file verify expression from commands/expert-flow.md ("Verify the answers file").
ANSWERS_JQ = (
    '.kind=="report" and (.attempt|type=="number") and (.round|type=="number") '
    'and (.items|type=="object") and ([.items[]|select(.disposition=="unresolved")]|length==0) '
    'and (.loop_end=="continue" or ([.items[]|select(.disposition=="accepted-on-continue")]|length==0))'
)

# D12: Verify the jq expression matches what's in the producer doc
PRODUCER_DOC = Path(__file__).parent.parent / "commands" / "expert-flow.md"

SEATED = ["alice", "bob", "contrarian-carl"]

LEDGER = {
    "seated": SEATED,
    "expected_reactions": [
        {"slug": s, "mode": m, "round": 1, "path": f"reactions/{s}-{m}-r1.json"}
        for m in ("pre-show", "reply") for s in SEATED
    ],
    "briefs": {
        "B1": {"brief_type": "decision", "raiser": "alice"},
        "B2": {"brief_type": "question", "raiser": "alice"},
    },
    "D1": {"tier": "decided", "brief_id": "B1", "brief_type": "decision", "evidence": []},
    "Q1": {"tier": "question", "brief_id": "B2", "brief_type": "question", "evidence": []},
}


def decisions_text() -> str:
    return f"# Decisions\n## Checkpoint ledger\n```json\n{json.dumps(LEDGER)}\n```\n"


def create_reactions(session_dir: Path) -> None:
    reactions_dir = session_dir / "reactions"
    reactions_dir.mkdir(exist_ok=True)
    for slug in SEATED:
        (reactions_dir / f"{slug}-pre-show-r1.json").write_text(
            json.dumps({"B1": {"resolved": True}, "B2": {"resolved": True}, "complete": True}),
            encoding="utf-8",
        )
        (reactions_dir / f"{slug}-reply-r1.json").write_text(
            json.dumps({"D1": {"accept": True}, "Q1": {"accept": True}, "complete": True}),
            encoding="utf-8",
        )


def question_file(round_num: int, items: list) -> str:
    """Report question file (flow-reference.md "Report variant" shape)."""
    return json.dumps({
        "kind": "report", "attempt": 1, "round": round_num, "step": "plan", "command": "expert-plan",
        "context_path": None, "ticket": "ticket", "headline": None, "north_star_blind": None,
        "roundup": None, "items": items,
        "questions": [{"id": "reply", "brief": None, "question": "Reply in your own words.",
                       "header": None, "options": [{"label": "Answer in your own words", "description": ""}]}],
    })


def build_fixture(tmp: Path, final_disposition: str, final_loop_end: Optional[str]):
    """Two-round fixture. Returns (session_dir, flow_dir)."""
    session_dir = tmp / "session"
    session_dir.mkdir(parents=True)
    create_reactions(session_dir)
    flow_dir = tmp / "flow"
    (flow_dir / "questions").mkdir(parents=True)
    (flow_dir / "answers").mkdir(parents=True)

    d1 = {"handle": "D1", "tier": "decided", "brief_type": "decision", "title": "D1",
          "decision_or_recommendation": "Use X", "why": "why", "backed_by": ["alice"],
          "business_case": None, "options": None, "must_answer": False, "reason_must_answer": None,
          "evidence": {"alice": "resolved"}, "pushback": None}
    q1 = {"handle": "Q1", "tier": "question", "brief_type": "question", "title": "Q1",
          "decision_or_recommendation": "Choose", "why": "why", "backed_by": ["alice"],
          "business_case": None, "options": [{"label": "A", "for": "a", "against": "-"},
                                             {"label": "B", "for": "b", "against": "-"}],
          "must_answer": True, "reason_must_answer": "question", "evidence": {"alice": "protested"},
          "pushback": None}

    # Round 1: D1 and Q1 asked; Q1 answered in prose.
    (flow_dir / "questions" / "plan-01.json").write_text(question_file(1, [d1, q1]), encoding="utf-8")
    (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
        "kind": "report", "round": 1, "attempt": 1,
        "items": {
            "D1": {"disposition": "accepted", "value": None, "reply_excerpt": None, "source": None, "ambiguous": False},
            "Q1": {"disposition": "answered", "value": "A", "reply_excerpt": "I pick A",
                   "source": "prose", "ambiguous": False},
        },
        "reading_echoed": False, "loop_end": "all-answered",
    }), encoding="utf-8")
    (flow_dir / "answers" / "plan-01.reply-1.md").write_text("I pick A\n", encoding="utf-8")

    # Round 2: Q1 re-asked as must-answer (reason "changed") and overridden; D1 not in this round.
    q1_changed = dict(q1, must_answer=True, reason_must_answer="changed", pushback=None)
    (flow_dir / "questions" / "plan-02.json").write_text(question_file(2, [q1_changed]), encoding="utf-8")
    (flow_dir / "answers" / "plan-02.json").write_text(json.dumps({
        "kind": "report", "round": 2, "attempt": 1,
        "items": {
            "Q1": {"disposition": final_disposition, "value": "B" if final_disposition == "overridden" else None,
                   "reply_excerpt": "Actually B" if final_disposition == "overridden" else None,
                   "source": "prose" if final_disposition == "overridden" else None, "ambiguous": False},
        },
        "reading_echoed": False, "loop_end": final_loop_end,
    }), encoding="utf-8")
    (flow_dir / "answers" / "plan-02.reply-2.md").write_text("Actually B\n", encoding="utf-8")
    return session_dir, flow_dir


def answers_jq_accepts(answers_file: Path) -> bool:
    result = subprocess.run(
        ["jq", "-e", ANSWERS_JQ, str(answers_file)],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def main() -> None:
    h = Harness("PLAN CHECKPOINT REGRESSION (#335 re-check against main)")

    # D12: Verify the jq copy matches the producer doc
    if PRODUCER_DOC.exists():
        doc_text = PRODUCER_DOC.read_text(encoding="utf-8")
        # The jq must appear somewhere in the doc (the part after "jq -e ")
        jq_fragment = (
            '.kind=="report" and (.attempt|type=="number") and (.round|type=="number") '
            'and (.items|type=="object") and ([.items[]|select(.disposition=="unresolved")]|length==0) '
            'and (.loop_end=="continue" or ([.items[]|select(.disposition=="accepted-on-continue")]|length==0))'
        )
        h.test_result("ANSWERS_JQ matches the expression in commands/expert-flow.md",
                      jq_fragment in doc_text,
                      f"jq fragment not found in {PRODUCER_DOC}")
    else:
        h.test_result("ANSWERS_JQ: producer doc exists", False, "commands/expert-flow.md not found")

    if shutil.which("jq") is None:
        h.test_result("jq is available for the answers-file verify expression", False, "jq not on PATH")
        h.summarize_and_exit()

    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = build_fixture(Path(tmp) / "ok", "overridden", "all-answered")
        error = check_report_checkpoint(session_dir, flow_dir, decisions_text(), 1, None)
        h.test_result("Two-round override with D1 unmentioned in round 2 passes the verifier",
                      error is None, f"error={error}")
        h.test_result("Final round passes the expert-flow.md answers-file jq",
                      answers_jq_accepts(flow_dir / "answers" / "plan-02.json"))

    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = build_fixture(Path(tmp) / "bad", "unresolved", None)
        error = check_report_checkpoint(session_dir, flow_dir, decisions_text(), 1, None)
        h.test_result("Final unresolved Q1 in round 2 is rejected by the verifier with 'unresolved: Q1'",
                      error is not None and "unresolved: Q1" in error, f"error={error}")
        h.test_result("Final unresolved Q1 in round 2 is rejected by the answers-file jq",
                      not answers_jq_accepts(flow_dir / "answers" / "plan-02.json"))

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
