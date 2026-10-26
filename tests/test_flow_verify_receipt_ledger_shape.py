#!/usr/bin/env python3
"""
Tests for scripts/flow-verify-receipt.py ledger shape validation.

Tests the verifier's handling of nested handle maps, missing entry fields, and the
updated Rule 6 failure reasons. Uses the PlanFixture pattern from test_flow_verify_receipt_ledger_handles.py.

Run with: python3 tests/test_flow_verify_receipt_ledger_shape.py
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
INVOCATION_ID = "20261025T154017-10721"
SLUGS = ["alice", "bob", "contrarian-carl"]


def ledger_entry(tier, brief_id="B1", brief_type="question"):
    """A post-tier ledger handle entry (the dict shape the verifier reads as a handle)."""
    return {"tier": tier, "brief_id": brief_id, "brief_type": brief_type, "evidence": []}


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

    def __init__(self, root, extra_ledger, item_handles, answers=None, answers_raw=None):
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
    h = Harness("flow-verify-receipt.py: ledger shape validation")

    # --- Nested handle map detection ---------------------------------------------------------------

    print("[Test 1] Nested handles under 'handles' key with a report fails with reason naming the key")
    with tempfile.TemporaryDirectory() as tmp:
        fx = PlanFixture(tmp, {
            "handles": {
                "D1": ledger_entry("decided"),
                "Q1": ledger_entry("question"),
            }
        }, item_handles=["D1", "Q1"])
        rc, out = run_verifier(fx)
        h.test_result("Nested 'handles' key fails with reason naming it",
                      rc == 1 and "nests handle keys under 'handles'" in out and "extra D1" not in out,
                      f"rc={rc} out_snippet={out.strip()[-300:]}")

    print("[Test 2] Paired control: D and W handles at top-level pass")
    with tempfile.TemporaryDirectory() as tmp:
        fx = PlanFixture(tmp, {
            "D1": ledger_entry("decided", "B1"),
            "W1": ledger_entry("worth-a-look", "B2"),
        }, item_handles=["D1", "W1"])
        rc, out = run_verifier(fx)
        h.test_result("Top-level D and W handles pass",
                      rc == 0 and "ARTIFACTS: ok" in out,
                      f"rc={rc} out_snippet={out.strip()[-300:]}")

    print("[Test 3] Nested handles under 'decisions' key fails with reason naming that key")
    with tempfile.TemporaryDirectory() as tmp:
        fx = PlanFixture(tmp, {
            "decisions": {
                "W1": ledger_entry("worth-a-look"),
            }
        }, item_handles=["W1"])
        rc, out = run_verifier(fx)
        h.test_result("Nested 'decisions' key fails with reason naming it",
                      rc == 1 and "nests handle keys under 'decisions'" in out,
                      f"rc={rc} out_snippet={out.strip()[-300:]}")

    # --- Nested handle map on legacy path (no report files) ---------------------------------------

    print("[Test 4] Legacy path: nested handles with no report files fails with named reason")
    with tempfile.TemporaryDirectory() as tmp:
        fx = PlanFixture(tmp, {
            "handles": {
                "D1": ledger_entry("decided"),
            }
        }, item_handles=[])  # No items in report, so no report question file
        rc, out = run_verifier(fx)
        h.test_result("Legacy path: nested handles fail with named reason",
                      rc == 1 and "nests handle keys under 'handles'" in out and "Traceback" not in out,
                      f"rc={rc} out_snippet={out.strip()[-300:]}")

    print("[Test 5] Legacy path: top-level D1 with no report files still fails with report mismatch reason")
    with tempfile.TemporaryDirectory() as tmp:
        fx = PlanFixture(tmp, {
            "D1": ledger_entry("decided"),
        }, item_handles=[])  # No items in report, so D1 is not in report
        rc, out = run_verifier(fx)
        # This should fail because D1 is in ledger but not in the report (mismatch)
        h.test_result("Legacy path: top-level handle with no report",
                      rc == 1 and "missing D1" in out and "Traceback" not in out,
                      f"rc={rc} out_snippet={out.strip()[-300:]}")

    # --- Flat entry shape (string instead of dict) -----------------------------------------------

    print("[Test 6] Flat entry (string value) fails with 'not an object' reason")
    with tempfile.TemporaryDirectory() as tmp:
        fx = PlanFixture(tmp, {
            "D1": "decided",  # Flat shape, not a dict
        }, item_handles=["D1"])
        rc, out = run_verifier(fx)
        h.test_result("Flat entry fails with 'not an object'",
                      rc == 1 and "not an object" in out and "extra D1" not in out,
                      f"rc={rc} out_snippet={out.strip()[-300:]}")

    # --- Missing field in entries ----------------------------------------------------------------

    print("[Test 7] Entry missing 'brief_id' fails with named reason")
    with tempfile.TemporaryDirectory() as tmp:
        fx = PlanFixture(tmp, {
            "D1": {"tier": "decided", "brief_type": "question", "evidence": []},  # No brief_id
        }, item_handles=["D1"])
        rc, out = run_verifier(fx)
        h.test_result("Missing 'brief_id' fails with named reason",
                      rc == 1 and "has no brief_id" in out,
                      f"rc={rc} out_snippet={out.strip()[-300:]}")

    print("[Test 8] Entry missing 'brief_type' fails with named reason")
    with tempfile.TemporaryDirectory() as tmp:
        fx = PlanFixture(tmp, {
            "D1": {"tier": "decided", "brief_id": "B1", "evidence": []},  # No brief_type
        }, item_handles=["D1"])
        rc, out = run_verifier(fx)
        h.test_result("Missing 'brief_type' fails with named reason",
                      rc == 1 and "has no brief_type" in out,
                      f"rc={rc} out_snippet={out.strip()[-300:]}")

    # --- #363 shape (dict without tier) ----------------------------------------------------------

    print("[Test 9] #363 shape (entry without tier) fails with 'has no tier' reason")
    with tempfile.TemporaryDirectory() as tmp:
        fx = PlanFixture(tmp, {
            "D1": {"brief_id": "B1", "brief_type": "question", "evidence": []},  # No tier
        }, item_handles=["D1"])
        rc, out = run_verifier(fx)
        h.test_result("#363 shape fails with 'has no tier'",
                      rc == 1 and "has no tier" in out and "accepted non-D/W" not in out,
                      f"rc={rc} out_snippet={out.strip()[-300:]}")

    # --- Rule 6: real non-D/W tier ---------------------------------------------------------------

    print("[Test 10] Rule 6: entry with real non-D/W tier (e.g., 'question' in a post-tier ledger)")
    with tempfile.TemporaryDirectory() as tmp:
        # Use Q1 which is a valid question handle, but set tier to something invalid
        fx = PlanFixture(tmp, {
            "Q1": {"tier": "invalid-tier", "brief_id": "B1", "brief_type": "question", "evidence": []},
        }, item_handles=["Q1"])
        rc, out = run_verifier(fx)
        h.test_result("Invalid tier triggers Rule 6",
                      rc == 1 and "accepted non-D/W" in out,
                      f"rc={rc} out_snippet={out.strip()[-300:]}")

    # --- Control: valid entries pass --------------------------------------------------------------

    print("[Test 11] Valid entries with all fields and correct tiers pass")
    with tempfile.TemporaryDirectory() as tmp:
        fx = PlanFixture(tmp, {
            "D1": ledger_entry("decided", "B1", "question"),
            "W1": ledger_entry("worth-a-look", "B2", "question"),
        }, item_handles=["D1", "W1"])
        rc, out = run_verifier(fx)
        h.test_result("Valid entries pass",
                      rc == 0 and "ARTIFACTS: ok" in out,
                      f"rc={rc} out_snippet={out.strip()[-300:]}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
