#!/usr/bin/env python3
"""
Black-box tests for scripts/flow-verify-receipt.py ledger-handle handling and the plan.md presence check.

The verifier is driven only through its CLI (subprocess), with --step plan so the report-kind
checkpoint path is exercised. Every fixture lives in a throwaway temp dir:

- HOME is pointed at the temp dir; the plan session lives under
  $HOME/.claude/plan-sessions/test/test-<invocation-id>/ and FINAL_PLAN_PATH points at
  $HOME/.claude/plans/test-<invocation-id>.md. Nothing under the real ~/.claude is read or written.

Covers:
1. An empty-string ledger key (dict or scalar value) does not crash the verifier and does not count
   as a handle that needs an answer.
2. Ledger handle detection is consistent: D/W/Q-style keys (D2, W2, Q3) are handles and a missing
   answer for them fails; lowercase and garbage keys (d9, Dx, X1, q1) are not handles.
3. An unanswered ledger handle fails closed and the reason names that handle.
4. An unreadable answers file for a ledger handle fails closed without the misleading
   "no report question files" reason (the question file is present).
5. plan.md that is a directory is not accepted as present; a regular plan.md is (control).
6. Receipt awaiting-answers markers are only an index: with them stripped, answers on disk still
   verify (exit 0), and a missing answer for a handle still fails (exit 1).

Run with: python3 tests/test_flow_verify_receipt_ledger_handles.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

VERIFIER = REPO_ROOT / "scripts" / "flow-verify-receipt.py"
INVOCATION_ID = "20261015T100000-30926"
SLUGS = ["alice", "bob", "contrarian-carl"]


def ledger_entry(tier, brief_id="B1"):
    """A post-tier ledger handle entry (the dict shape the verifier reads as a handle)."""
    return {"tier": tier, "brief_id": brief_id, "brief_type": "question", "evidence": []}


def item(handle):
    """One report-kind question item. Q-handles are questions; D-handles are decided."""
    is_question = handle.startswith("Q")
    return {
        "handle": handle,
        "tier": "question" if is_question else "decided",
        "brief_type": "question",
        "title": f"Item {handle}",
        "decision_or_recommendation": "A",
        "why": "Better",
        "backed_by": ["alice"],
        "business_case": None,
        "options": [{"label": "A", "for": "reason", "against": ""}] if is_question else None,
        "must_answer": False,
        "reason_must_answer": None,
        "evidence": {"alice": "resolved"},
        "pushback": None,
    }


class PlanFixture:
    """A plan session whose ledger carries the given extra entries and whose question file carries
    one item per handle in item_handles."""

    def __init__(self, root, extra_ledger, item_handles, answers=None, answers_raw=None,
                 plan_md_is_dir=False):
        self.root = Path(root)
        self.home = self.root / "home"
        self.session = self.home / ".claude" / "plan-sessions" / "test" / f"test-{INVOCATION_ID}"
        self.flow = self.session / "flow"
        self.receipt = self.root / "receipt.md"
        plans = self.home / ".claude" / "plans"
        plans.mkdir(parents=True)
        plan_file = plans / f"test-{INVOCATION_ID}.md"
        plan_file.write_text("# Plan", encoding="utf-8")
        self.session.mkdir(parents=True)
        (self.session / "context.md").write_text("# Context", encoding="utf-8")
        if plan_md_is_dir:
            (self.session / "plan.md").mkdir()
        else:
            (self.session / "plan.md").write_text("# Plan", encoding="utf-8")
        (self.session / "selected-experts.md").write_text(
            "| Expert |\n|--------|\n" + "".join(f"| {s} |\n" for s in SLUGS), encoding="utf-8")
        for slug in SLUGS:
            (self.session / f"{slug}-contribution.md").write_text(
                f"Contribution from {slug}\n<!-- contribution-end -->", encoding="utf-8")

        ledger = {
            "seated": list(SLUGS),
            "expected_reactions": [
                {"slug": s, "mode": m, "round": 1, "path": f"reactions/{s}-{m}-r1.json"}
                for m in ("pre-show", "reply") for s in SLUGS
            ],
            "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}},
        }
        ledger.update(extra_ledger)
        (self.session / "decisions.md").write_text(
            "# Decisions\n\n## Checkpoint ledger\n```json\n" + json.dumps(ledger) + "\n```\n",
            encoding="utf-8")

        reactions = self.session / "reactions"
        reactions.mkdir()
        for slug in SLUGS:
            (reactions / f"{slug}-pre-show-r1.json").write_text(
                json.dumps({"B1": {"agreed": True}, "complete": True}), encoding="utf-8")
            (reactions / f"{slug}-reply-r1.json").write_text(
                json.dumps({"complete": True}), encoding="utf-8")

        (self.flow / "questions").mkdir(parents=True)
        (self.flow / "answers").mkdir(parents=True)
        question = {
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [item(h) for h in item_handles],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None,
                           "options": [{"label": "Answer", "description": ""}]}],
        }
        (self.flow / "questions" / "plan-01.json").write_text(json.dumps(question), encoding="utf-8")

        if answers_raw is not None:
            (self.flow / "answers" / "plan-01.json").write_text(answers_raw, encoding="utf-8")
        elif answers is not None:
            (self.flow / "answers" / "plan-01.json").write_text(json.dumps({
                "kind": "report", "round": 1, "attempt": 1,
                "items": answers, "loop_end": None,
            }), encoding="utf-8")
        else:
            (self.flow / "answers" / "plan-01.json").write_text(json.dumps({
                "kind": "report", "round": 1, "attempt": 1,
                "items": {h: {"disposition": "accepted", "value": None, "reply_excerpt": None}
                          for h in item_handles},
                "loop_end": None,
            }), encoding="utf-8")

        self.receipt.write_text(
            f"FINAL_PLAN_PATH: {plan_file}\n"
            "<!-- awaiting-answers: questions/plan-01.json -->\n"
            "ATTEMPT: 1\nDecision: OK\n<!-- step-end -->\n",
            encoding="utf-8")


def run_verifier(fx):
    """Run the verifier CLI against a plan fixture. Returns (exit code, combined output)."""
    args = [
        sys.executable, str(VERIFIER),
        "--step", "plan",
        "--receipt", str(fx.receipt),
        "--since", "1970-01-01T00:00:00Z",
        "--flow-dir", str(fx.flow),
    ]
    env = {**os.environ, "HOME": str(fx.home)}
    result = subprocess.run(args, capture_output=True, text=True, env=env)
    return result.returncode, result.stdout + result.stderr


def main():
    h = Harness("flow-verify-receipt.py: ledger handles and plan.md presence")

    # --- Empty-string ledger key ----------------------------------------------------------------
    print("[Test 1] Empty-string ledger key with a dict value is not a handle and does not crash")
    with tempfile.TemporaryDirectory() as tmp:
        fx = PlanFixture(tmp, {
            "D1": ledger_entry("decided"),
            "": ledger_entry("question"),
        }, item_handles=["D1"])
        rc, out = run_verifier(fx)
        h.test_result("Empty-string dict key: exit 0, ARTIFACTS: ok, no traceback",
                      rc == 0 and "ARTIFACTS: ok" in out and "Traceback" not in out,
                      f"rc={rc} out={out.strip()[-300:]}")

    print("[Test 2] Empty-string ledger key with a scalar value does not crash")
    with tempfile.TemporaryDirectory() as tmp:
        fx = PlanFixture(tmp, {"D1": ledger_entry("decided"), "": "x"}, item_handles=["D1"])
        rc, out = run_verifier(fx)
        h.test_result("Empty-string scalar key: exit 0, ARTIFACTS: ok, no traceback",
                      rc == 0 and "ARTIFACTS: ok" in out and "Traceback" not in out,
                      f"rc={rc} out={out.strip()[-300:]}")

    print("[Test 3] Empty-string key does not mask a real missing handle")
    with tempfile.TemporaryDirectory() as tmp:
        fx = PlanFixture(tmp, {
            "D1": ledger_entry("decided"),
            "": ledger_entry("question"),
            "W2": ledger_entry("decided"),
        }, item_handles=["D1"])
        rc, out = run_verifier(fx)
        h.test_result("Empty-string key plus real W2 handle: exit 1 naming W2, no traceback",
                      rc == 1 and "missing W2" in out and "Traceback" not in out,
                      f"rc={rc} out={out.strip()[-300:]}")

    # --- Handle detection consistency -----------------------------------------------------------
    print("[Test 4] D/W/Q-style ledger keys are handles (a missing answer fails)")
    for key in ("D2", "W2", "Q3"):
        with tempfile.TemporaryDirectory() as tmp:
            tier = "question" if key.startswith("Q") else "decided"
            fx = PlanFixture(tmp, {"D1": ledger_entry("decided"), key: ledger_entry(tier)},
                             item_handles=["D1"])
            rc, out = run_verifier(fx)
            h.test_result(f"Ledger handle {key} is detected: exit 1 with 'missing {key}'",
                          rc == 1 and f"missing {key}" in out and "Traceback" not in out,
                          f"rc={rc} out={out.strip()[-300:]}")

    print("[Test 5] Lowercase and garbage ledger keys are not handles")
    with tempfile.TemporaryDirectory() as tmp:
        fx = PlanFixture(tmp, {
            "D1": ledger_entry("decided"),
            "d9": ledger_entry("decided"),
            "Dx": ledger_entry("decided"),
            "X1": ledger_entry("question"),
            "q1": ledger_entry("question"),
        }, item_handles=["D1"])
        rc, out = run_verifier(fx)
        h.test_result("Lowercase and garbage keys (d9, Dx, X1, q1) are ignored: exit 0",
                      rc == 0 and "ARTIFACTS: ok" in out and "Traceback" not in out,
                      f"rc={rc} out={out.strip()[-300:]}")

    # --- Fail closed on unanswered / unreadable ledger handles ----------------------------------
    print("[Test 6] Unanswered ledger handle fails closed and the reason names it")
    with tempfile.TemporaryDirectory() as tmp:
        fx = PlanFixture(tmp, {"Q1": ledger_entry("question")}, item_handles=["Q1"], answers={})
        rc, out = run_verifier(fx)
        h.test_result("Unanswered ledger handle Q1: exit 1 and reason names Q1",
                      rc == 1 and "Q1" in out and "Traceback" not in out,
                      f"rc={rc} out={out.strip()[-300:]}")

    print("[Test 7] Unreadable answers file for a present ledger handle fails closed without the no-question-file reason")
    with tempfile.TemporaryDirectory() as tmp:
        fx = PlanFixture(tmp, {"Q1": ledger_entry("question")}, item_handles=["Q1"],
                         answers_raw="not json at all")
        rc, out = run_verifier(fx)
        h.test_result("Unreadable answers for Q1: exit 1, no traceback, not 'no report question files'",
                      rc == 1 and "Traceback" not in out and "no report question files" not in out,
                      f"rc={rc} out={out.strip()[-300:]}")

    # --- Receipt markers are only an index ------------------------------------------------------
    print("[Test 8a] Receipt with awaiting-answers markers stripped still verifies from disk")
    with tempfile.TemporaryDirectory() as tmp:
        fx = PlanFixture(tmp, {"D1": ledger_entry("decided")}, item_handles=["D1"])
        fx.receipt.write_text(
            "\n".join(line for line in fx.receipt.read_text(encoding="utf-8").splitlines()
                      if "awaiting-answers" not in line) + "\n",
            encoding="utf-8")
        rc, out = run_verifier(fx)
        h.test_result("Markers stripped, answers on disk for D1: exit 0, ARTIFACTS: ok",
                      rc == 0 and "ARTIFACTS: ok" in out and "Traceback" not in out,
                      f"rc={rc} out={out.strip()[-300:]}")

    print("[Test 8b] Markers stripped, answer file missing a handle still fails (markers do not weaken checks)")
    with tempfile.TemporaryDirectory() as tmp:
        fx = PlanFixture(tmp, {"D1": ledger_entry("decided"), "Q1": ledger_entry("question")},
                         item_handles=["D1", "Q1"],
                         answers={"D1": {"disposition": "accepted", "value": None, "reply_excerpt": None}})
        fx.receipt.write_text(
            "\n".join(line for line in fx.receipt.read_text(encoding="utf-8").splitlines()
                      if "awaiting-answers" not in line) + "\n",
            encoding="utf-8")
        rc, out = run_verifier(fx)
        h.test_result("Markers stripped, Q1 unanswered on disk: exit 1 and reason names Q1",
                      rc == 1 and "Q1" in out and "Traceback" not in out,
                      f"rc={rc} out={out.strip()[-300:]}")

    # --- plan.md presence -----------------------------------------------------------------------
    print("[Test 8] plan.md as a directory is not accepted as present")
    with tempfile.TemporaryDirectory() as tmp:
        fx = PlanFixture(tmp, {"D1": ledger_entry("decided")}, item_handles=["D1"], plan_md_is_dir=True)
        rc, out = run_verifier(fx)
        h.test_result("plan.md is a directory: exit 1 with plan.md reason",
                      rc == 1 and "plan.md" in out and "Traceback" not in out,
                      f"rc={rc} out={out.strip()[-300:]}")

    print("[Test 9] Control: regular plan.md with the same ledger passes")
    with tempfile.TemporaryDirectory() as tmp:
        fx = PlanFixture(tmp, {"D1": ledger_entry("decided")}, item_handles=["D1"])
        rc, out = run_verifier(fx)
        h.test_result("Regular plan.md control: exit 0, ARTIFACTS: ok",
                      rc == 0 and "ARTIFACTS: ok" in out,
                      f"rc={rc} out={out.strip()[-300:]}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
