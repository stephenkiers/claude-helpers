#!/usr/bin/env python3
"""
Comprehensive tests for multi-round plan checkpoint verification.

Tests the new multi-round behaviors from plan #335:
- Union coverage, per-round must-answer coverage, subset of ledger
- Round matching, excerpt binding to the producing file's round
- Form vs prose branching, same-round append behavior
- Wrong-shape detection with specific failure reasons

Run with: python3 tests/test_flow_verify_receipt_multiround.py
"""

import json
import sys
import tempfile
from pathlib import Path

# Add paths for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness
import importlib.util

# Import check_report_checkpoint
script_path = Path(__file__).parent.parent / "scripts" / "flow-verify-receipt.py"
spec = importlib.util.spec_from_file_location("flow_verify_receipt", script_path)
flow_verify_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flow_verify_module)
check_report_checkpoint = flow_verify_module.check_report_checkpoint


def create_base_reactions(session_dir, slugs=None):
    """Create basic pre-show and reply reactions for all slugs."""
    if slugs is None:
        slugs = ["alice", "bob", "contrarian-carl"]
    reactions_dir = session_dir / "reactions"
    reactions_dir.mkdir(exist_ok=True)

    for slug in slugs:
        # Minimal pre-show reactions
        (reactions_dir / f"{slug}-pre-show-r1.json").write_text(
            json.dumps({"B1": {"resolved": True}, "complete": True}),
            encoding="utf-8"
        )
        # Minimal reply reactions
        (reactions_dir / f"{slug}-reply-r1.json").write_text(
            json.dumps({"Q1": {"accept": True}, "complete": True}),
            encoding="utf-8"
        )


def create_base_fixture(tmp_path: Path):
    """Create a base fixture with session and flow directories."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    flow_dir = tmp_path / "flow"
    flow_dir.mkdir()
    (flow_dir / "answers").mkdir()
    (flow_dir / "questions").mkdir()

    return session_dir, flow_dir


def _two_round_checkpoint(tmp, r1_handles, r1_answers, r2_handles, r2_answers):
    """Build an attempt-1 two-round report checkpoint (Q1 question, D1 decided).

    Returns (session_dir, flow_dir, decisions_text). Round 1/2 answers are the
    raw dicts written to answers/plan-01.json and answers/plan-02.json; reply
    files exist for both rounds.
    """
    session_dir, flow_dir = create_base_fixture(Path(tmp))
    create_base_reactions(session_dir)
    tiers = {"Q1": "question", "D1": "decided"}
    ledger = {
        "seated": ["alice", "bob", "contrarian-carl"],
        "expected_reactions": [
            {"slug": s, "mode": m, "round": 1, "path": f"reactions/{s}-{m}-r1.json"}
            for m in ("pre-show", "reply") for s in ["alice", "bob", "contrarian-carl"]
        ],
        "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}, "B2": {"brief_type": "question", "raiser": "alice"}},
    }
    for handle in r1_handles:
        ledger[handle] = {"tier": tiers[handle], "brief_id": "B1" if handle == "Q1" else "B2",
                          "brief_type": "question", "evidence": []}
    decisions_text = f"# Decisions\n## Checkpoint ledger\n```json\n{json.dumps(ledger)}\n```\n"

    def item(handle):
        must = tiers[handle] == "question"
        return {"handle": handle, "tier": tiers[handle], "brief_type": "question", "title": handle,
                "decision_or_recommendation": "Choose", "why": "Unclear", "backed_by": ["alice"],
                "must_answer": must, "reason_must_answer": "question" if must else None, "evidence": {},
                "options": [{"label": "A"}, {"label": "B"}] if must else None,
                "business_case": None, "pushback": None}

    for seq, rnd, handles, answers in ((1, 1, r1_handles, r1_answers), (2, 2, r2_handles, r2_answers)):
        (flow_dir / "questions" / f"plan-0{seq}.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": rnd, "step": "plan", "command": "expert-plan",
            "items": [item(x) for x in handles],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}],
        }), encoding="utf-8")
        (flow_dir / "answers" / f"plan-0{seq}.json").write_text(json.dumps(answers), encoding="utf-8")
        (flow_dir / "answers" / f"plan-0{seq}.reply-{rnd}.md").write_text("A\nB\n", encoding="utf-8")
    return session_dir, flow_dir, decisions_text


def _ans(value):
    return {"disposition": "answered", "value": value, "reply_excerpt": value, "source": "prose"}


def main():
    h = Harness("FLOW-VERIFY-RECEIPT MULTI-ROUND TESTS")

    # Test 2: Latest wins
    print("[Test 2] Latest wins: later file in SEQ order overrides earlier")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = {
            "seated": ["alice", "bob", "contrarian-carl"],
            "expected_reactions": [
                {"slug": s, "mode": m, "round": 1, "path": f"reactions/{s}-{m}-r1.json"}
                for m in ("pre-show", "reply") for s in ["alice", "bob", "contrarian-carl"]
            ],
            "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}},
            "Q1": {"tier": "question", "brief_id": "B1", "brief_type": "question", "evidence": []},
        }
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        # Round 1: Q1 answered
        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "Q1", "tier": "question", "brief_type": "question", "title": "Q",
                 "decision_or_recommendation": "Choose", "why": "Unclear", "backed_by": ["alice"],
                 "must_answer": True, "reason_must_answer": "question", "evidence": {},
                 "options": [{"label": "A"}, {"label": "B"}], "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {"Q1": {"disposition": "answered", "value": "A", "reply_excerpt": "I choose A", "source": "prose", "ambiguous": False}},
            "reading_echoed": False, "loop_end": "all-answered",
        }), encoding="utf-8")
        (flow_dir / "answers" / "plan-01.reply-1.md").write_text("I choose A\n", encoding="utf-8")

        # Round 3: Q1 now unresolved (later wins, so should fail)
        (flow_dir / "questions" / "plan-02.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 3, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "Q1", "tier": "question", "brief_type": "question", "title": "Q (retry)",
                 "decision_or_recommendation": "Choose", "why": "Still unclear", "backed_by": ["alice"],
                 "must_answer": True, "reason_must_answer": "question", "evidence": {},
                 "options": [{"label": "A"}, {"label": "B"}], "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Choose", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        (flow_dir / "answers" / "plan-02.json").write_text(json.dumps({
            "kind": "report", "round": 3, "attempt": 1,
            "items": {"Q1": {"disposition": "unresolved", "value": None, "reply_excerpt": None, "source": None, "ambiguous": False}},
            "reading_echoed": False, "loop_end": None,
        }), encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 2] Latest wins: final unresolved fails with 'unresolved: Q1'",
                      error is not None and "unresolved: Q1" in error,
                      f"error={error}")

    # Test 9: Round mismatch
    print("[Test 9] Round mismatch: answers file round 3, question file round 2")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = {
            "seated": ["alice", "bob", "contrarian-carl"],
            "expected_reactions": [
                {"slug": s, "mode": m, "round": 1, "path": f"reactions/{s}-{m}-r1.json"}
                for m in ("pre-show", "reply") for s in ["alice", "bob", "contrarian-carl"]
            ],
            "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}},
            "Q1": {"tier": "question", "brief_id": "B1", "brief_type": "question", "evidence": []},
        }
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        # Round 1 base
        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "Q1", "tier": "question", "brief_type": "question", "title": "Q",
                 "decision_or_recommendation": "Choose", "why": "Unclear", "backed_by": ["alice"],
                 "must_answer": True, "reason_must_answer": "question", "evidence": {},
                 "options": [{"label": "A"}], "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {"Q1": {"disposition": "unresolved", "value": None, "reply_excerpt": None, "source": None}},
            "reading_echoed": False, "loop_end": None,
        }), encoding="utf-8")

        # Round 2 question, but round 3 answer (mismatch)
        (flow_dir / "questions" / "plan-02.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 2, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "Q1", "tier": "question", "brief_type": "question", "title": "Q",
                 "decision_or_recommendation": "Choose", "why": "Still unclear", "backed_by": ["alice"],
                 "must_answer": True, "reason_must_answer": "question", "evidence": {},
                 "options": [{"label": "A"}], "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Choose", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        (flow_dir / "answers" / "plan-02.json").write_text(json.dumps({
            "kind": "report", "round": 3, "attempt": 1,
            "items": {"Q1": {"disposition": "answered", "value": "A", "reply_excerpt": "A", "source": "form"}},
            "reading_echoed": False, "loop_end": "all-answered",
        }), encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 9] Round mismatch fails with 'answers file round mismatch'",
                      error is not None and "answers file round mismatch" in error,
                      f"error={error}")

    # Test 10: Prose missing reply file
    print("[Test 10] Prose fail-closed: reply file missing")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = {
            "seated": ["alice", "bob", "contrarian-carl"],
            "expected_reactions": [
                {"slug": s, "mode": m, "round": 1, "path": f"reactions/{s}-{m}-r1.json"}
                for m in ("pre-show", "reply") for s in ["alice", "bob", "contrarian-carl"]
            ],
            "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}},
            "Q1": {"tier": "question", "brief_id": "B1", "brief_type": "question", "evidence": []},
        }
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        # Question file round 3
        (flow_dir / "questions" / "plan-02.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 3, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "Q1", "tier": "question", "brief_type": "question", "title": "Q",
                 "decision_or_recommendation": "Choose", "why": "Unclear", "backed_by": ["alice"],
                 "must_answer": True, "reason_must_answer": "question", "evidence": {},
                 "options": [{"label": "A"}], "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Choose", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # Answers with prose but no reply file
        (flow_dir / "answers" / "plan-02.json").write_text(json.dumps({
            "kind": "report", "round": 3, "attempt": 1,
            "items": {"Q1": {"disposition": "answered", "value": "A", "reply_excerpt": "I choose A", "source": "prose"}},
            "reading_echoed": False, "loop_end": "all-answered",
        }), encoding="utf-8")

        # Don't create plan-02.reply-3.md

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 10] Prose missing reply file fails with 'reply file missing: plan-02.reply-3.md'",
                      error is not None and "reply file missing: plan-02.reply-3.md" in error,
                      f"error={error}")

    # Test 11: Prose with non-positive round
    print("[Test 11] Prose fail-closed: round 0 (non-positive)")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = {
            "seated": ["alice", "bob", "contrarian-carl"],
            "expected_reactions": [
                {"slug": s, "mode": m, "round": 1, "path": f"reactions/{s}-{m}-r1.json"}
                for m in ("pre-show", "reply") for s in ["alice", "bob", "contrarian-carl"]
            ],
            "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}},
            "Q1": {"tier": "question", "brief_id": "B1", "brief_type": "question", "evidence": []},
        }
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        # Question file has round 0
        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 0, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "Q1", "tier": "question", "brief_type": "question", "title": "Q",
                 "decision_or_recommendation": "Choose", "why": "Unclear", "backed_by": ["alice"],
                 "must_answer": True, "reason_must_answer": "question", "evidence": {},
                 "options": [{"label": "A"}], "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 0, "attempt": 1,
            "items": {"Q1": {"disposition": "answered", "value": "A", "reply_excerpt": "A", "source": "prose"}},
            "reading_echoed": False, "loop_end": "all-answered",
        }), encoding="utf-8")
        (flow_dir / "answers" / "plan-01.reply-0.md").write_text("A\n", encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 11] Prose round 0 fails with 'round not a positive integer'",
                      error is not None and "round not a positive integer" in error,
                      f"error={error}")

    # Test 14: Form answer with empty excerpt
    print("[Test 14] Form answer with empty excerpt")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = {
            "seated": ["alice", "bob", "contrarian-carl"],
            "expected_reactions": [
                {"slug": s, "mode": m, "round": 1, "path": f"reactions/{s}-{m}-r1.json"}
                for m in ("pre-show", "reply") for s in ["alice", "bob", "contrarian-carl"]
            ],
            "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}},
            "Q1": {"tier": "question", "brief_id": "B1", "brief_type": "question", "evidence": []},
        }
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "Q1", "tier": "question", "brief_type": "question", "title": "Q",
                 "decision_or_recommendation": "Choose", "why": "Unclear", "backed_by": ["alice"],
                 "must_answer": True, "reason_must_answer": "question", "evidence": {},
                 "options": [{"label": "Option A"}], "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Choose", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # Form answer with empty excerpt
        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {"Q1": {"disposition": "answered", "value": "Option A", "reply_excerpt": "", "source": "form"}},
            "reading_echoed": False, "loop_end": "all-answered",
        }), encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 14] Form empty excerpt fails with 'must-answer without quote'",
                      error is not None and "must-answer without quote" in error,
                      f"error={error}")

    # Test 16: Accepted-on-continue without loop_end
    print("[Test 16] Accepted-on-continue without loop_end")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = {
            "seated": ["alice", "bob", "contrarian-carl"],
            "expected_reactions": [
                {"slug": s, "mode": m, "round": 1, "path": f"reactions/{s}-{m}-r1.json"}
                for m in ("pre-show", "reply") for s in ["alice", "bob", "contrarian-carl"]
            ],
            "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}},
            "Q1": {"tier": "question", "brief_id": "B1", "brief_type": "question", "evidence": []},
        }
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "Q1", "tier": "question", "brief_type": "question", "title": "Q",
                 "decision_or_recommendation": "Choose", "why": "Unclear", "backed_by": ["alice"],
                 "must_answer": True, "reason_must_answer": "question", "evidence": {},
                 "options": [{"label": "A"}], "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Choose", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # Accepted-on-continue without loop_end
        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {"Q1": {"disposition": "accepted-on-continue", "value": None, "reply_excerpt": "continue", "source": "prose"}},
            "reading_echoed": False, "loop_end": None,
        }), encoding="utf-8")

        (flow_dir / "answers" / "plan-01.reply-1.md").write_text("continue\n", encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 16] Accepted-on-continue without loop_end fails with 'accepted-on-continue without loop_end'",
                      error is not None and "accepted-on-continue without loop_end" in error,
                      f"error={error}")

    # Test 18: Wrong shape - uses 'answers' key
    print("[Test 18] Wrong shape: uses 'answers' key instead of 'items'")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = {
            "seated": ["alice", "bob", "contrarian-carl"],
            "expected_reactions": [
                {"slug": s, "mode": m, "round": 1, "path": f"reactions/{s}-{m}-r1.json"}
                for m in ("pre-show", "reply") for s in ["alice", "bob", "contrarian-carl"]
            ],
            "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}},
            "Q1": {"tier": "question", "brief_id": "B1", "brief_type": "question", "evidence": []},
        }
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "Q1", "tier": "question", "brief_type": "question", "title": "Q",
                 "decision_or_recommendation": "Choose", "why": "Unclear", "backed_by": ["alice"],
                 "must_answer": True, "reason_must_answer": "question", "evidence": {},
                 "options": [{"label": "A"}], "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Choose", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # Wrong shape: uses 'answers' key instead of 'items'
        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "answers": {"Q1": {"disposition": "answered", "value": "A"}},
        }), encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        # F3: The shape validation check must catch 'answers' key and report the specific legacy-key reason
        h.test_result("[Test 18] Wrong shape: 'answers' key fails with specific reason",
                      error is not None and "uses 'answers' key; expected 'items'" in error,
                      f"error={error}")

    # Test 19: Wrong shape - missing 'attempt'
    print("[Test 19] Wrong shape: missing 'attempt' key")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = {
            "seated": ["alice", "bob", "contrarian-carl"],
            "expected_reactions": [
                {"slug": s, "mode": m, "round": 1, "path": f"reactions/{s}-{m}-r1.json"}
                for m in ("pre-show", "reply") for s in ["alice", "bob", "contrarian-carl"]
            ],
            "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}},
            "Q1": {"tier": "question", "brief_id": "B1", "brief_type": "question", "evidence": []},
        }
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "Q1", "tier": "question", "brief_type": "question", "title": "Q",
                 "decision_or_recommendation": "Choose", "why": "Unclear", "backed_by": ["alice"],
                 "must_answer": True, "reason_must_answer": "question", "evidence": {},
                 "options": [{"label": "A"}], "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Choose", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # Missing 'attempt' key
        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 1,
            "items": {"Q1": {"disposition": "answered", "value": "A", "reply_excerpt": "A", "source": "form"}},
        }), encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 19] Wrong shape: missing 'attempt' fails with 'report answers file missing 'attempt' key: plan-01.json'",
                      error is not None and "report answers file missing 'attempt' key: plan-01.json" in error,
                      f"error={error}")

    # Test 20: Mixed shape
    print("[Test 20] Mixed shape: round 1 valid report, round 2 legacy with 'answers' key")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = {
            "seated": ["alice", "bob", "contrarian-carl"],
            "expected_reactions": [
                {"slug": s, "mode": m, "round": 1, "path": f"reactions/{s}-{m}-r1.json"}
                for m in ("pre-show", "reply") for s in ["alice", "bob", "contrarian-carl"]
            ],
            "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}},
            "Q1": {"tier": "question", "brief_id": "B1", "brief_type": "question", "evidence": []},
        }
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        # Round 1 question
        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "Q1", "tier": "question", "brief_type": "question", "title": "Q",
                 "decision_or_recommendation": "Choose", "why": "Unclear", "backed_by": ["alice"],
                 "must_answer": True, "reason_must_answer": "question", "evidence": {},
                 "options": [{"label": "A"}], "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Choose", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # Round 1 valid answer
        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {"Q1": {"disposition": "unresolved", "value": None, "reply_excerpt": None, "source": None}},
        }), encoding="utf-8")

        # Round 2 question re-asks Q1
        (flow_dir / "questions" / "plan-02.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 2, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "Q1", "tier": "question", "brief_type": "question", "title": "Q",
                 "decision_or_recommendation": "Choose", "why": "Still unclear", "backed_by": ["alice"],
                 "must_answer": True, "reason_must_answer": "question", "evidence": {},
                 "options": [{"label": "A"}], "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Choose", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # Round 2 uses legacy {'answers': {...}} shape
        (flow_dir / "answers" / "plan-02.json").write_text(json.dumps({
            "answers": {"Q1": {"disposition": "answered", "value": "A"}},
        }), encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 20] Mixed shape: legacy round 2 fails with specific reason and 'plan-02.json'",
                      error is not None and ("missing 'kind' key: plan-02.json" in error or "uses 'answers' key" in error or "missing 'attempt' key: plan-02.json" in error),
                      f"error={error}")

    # Test 3: Excerpt binding negative - Q1 excerpt in wrong round's reply file
    print("[Test 3] Excerpt binding negative: Q1 excerpt in plan-01.reply-1.md but needed in plan-02.reply-3.md")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = {
            "seated": ["alice", "bob", "contrarian-carl"],
            "expected_reactions": [
                {"slug": s, "mode": m, "round": 1, "path": f"reactions/{s}-{m}-r1.json"}
                for m in ("pre-show", "reply") for s in ["alice", "bob", "contrarian-carl"]
            ],
            "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}},
            "Q1": {"tier": "question", "brief_id": "B1", "brief_type": "question", "evidence": []},
        }
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        # Round 1: Q1 unresolved
        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "Q1", "tier": "question", "brief_type": "question", "title": "Q",
                 "decision_or_recommendation": "Choose", "why": "Unclear", "backed_by": ["alice"],
                 "must_answer": True, "reason_must_answer": "question", "evidence": {},
                 "options": [{"label": "A"}, {"label": "B"}], "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {"Q1": {"disposition": "unresolved", "value": None, "reply_excerpt": None, "source": None}},
            "reading_echoed": False, "loop_end": None,
        }), encoding="utf-8")

        # Round 2 intentionally skipped (testing excerpt binding across non-contiguous rounds)
        # Round 3: Q1 answered, excerpt in plan-01.reply-1.md but should be in plan-02.reply-3.md
        (flow_dir / "questions" / "plan-02.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 3, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "Q1", "tier": "question", "brief_type": "question", "title": "Q",
                 "decision_or_recommendation": "Choose", "why": "Retry", "backed_by": ["alice"],
                 "must_answer": True, "reason_must_answer": "question", "evidence": {},
                 "options": [{"label": "A"}, {"label": "B"}], "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Choose", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        (flow_dir / "answers" / "plan-02.json").write_text(json.dumps({
            "kind": "report", "round": 3, "attempt": 1,
            "items": {"Q1": {"disposition": "answered", "value": "B", "reply_excerpt": "I prefer B", "source": "prose"}},
            "reading_echoed": False, "loop_end": "all-answered",
        }), encoding="utf-8")

        # Excerpt is only in plan-01.reply-1.md, not in plan-02.reply-3.md
        (flow_dir / "answers" / "plan-01.reply-1.md").write_text("I prefer B\n", encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 3] Excerpt binding: excerpt in wrong file fails",
                      error is not None and ("reply file missing: plan-02.reply-3.md" in error or "reply excerpt not found" in error),
                      f"error={error}")

    # Test 4: Excerpt binding negative - W1 excerpt in wrong file
    print("[Test 4] Excerpt binding negative: W1 only in round 1, excerpt in round 3")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = {
            "seated": ["alice", "bob", "contrarian-carl"],
            "expected_reactions": [
                {"slug": s, "mode": m, "round": 1, "path": f"reactions/{s}-{m}-r1.json"}
                for m in ("pre-show", "reply") for s in ["alice", "bob", "contrarian-carl"]
            ],
            "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}},
            "W1": {"tier": "worth-a-look", "brief_id": "B1", "brief_type": "question", "evidence": []},
        }
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        # Round 1: W1 overridden with excerpt
        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "W1", "tier": "worth-a-look", "brief_type": "question", "title": "W",
                 "decision_or_recommendation": "Maybe", "why": "Unclear", "backed_by": ["alice"],
                 "must_answer": True, "reason_must_answer": "protested", "evidence": {},
                 "options": None, "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {"W1": {"disposition": "overridden", "value": "Other way", "reply_excerpt": "Different path", "source": "prose"}},
            "reading_echoed": False, "loop_end": None,
        }), encoding="utf-8")
        (flow_dir / "answers" / "plan-01.reply-1.md").write_text("Different path\n", encoding="utf-8")

        # Round 3: W1 not asked again, but excerpt mistakenly in plan-02.reply-3.md
        (flow_dir / "questions" / "plan-02.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 3, "step": "plan", "command": "expert-plan",
            "items": [],
            "questions": []
        }), encoding="utf-8")

        (flow_dir / "answers" / "plan-02.json").write_text(json.dumps({
            "kind": "report", "round": 3, "attempt": 1,
            "items": {},
            "reading_echoed": False, "loop_end": None,
        }), encoding="utf-8")
        (flow_dir / "answers" / "plan-02.reply-3.md").write_text("Different path\n", encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 4] Excerpt binding: W1 overridden only in round 1, checked against round 1",
                      error is None,
                      f"error={error}")

    # Test 5: Coverage - handle absent from all rounds
    print("[Test 5] Coverage: handle W3 absent from all rounds")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = {
            "seated": ["alice", "bob", "contrarian-carl"],
            "expected_reactions": [
                {"slug": s, "mode": m, "round": 1, "path": f"reactions/{s}-{m}-r1.json"}
                for m in ("pre-show", "reply") for s in ["alice", "bob", "contrarian-carl"]
            ],
            "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}},
            "Q1": {"tier": "question", "brief_id": "B1", "brief_type": "question", "evidence": []},
            "W3": {"tier": "worth-a-look", "brief_id": "B1", "brief_type": "question", "evidence": []},
        }
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "Q1", "tier": "question", "brief_type": "question", "title": "Q",
                 "decision_or_recommendation": "Choose", "why": "Unclear", "backed_by": ["alice"],
                 "must_answer": True, "reason_must_answer": "question", "evidence": {},
                 "options": [{"label": "A"}], "business_case": None, "pushback": None},
                {"handle": "W3", "tier": "worth-a-look", "brief_type": "question", "title": "W",
                 "decision_or_recommendation": "Maybe", "why": "Unclear", "backed_by": ["alice"],
                 "must_answer": False, "reason_must_answer": None, "evidence": {},
                 "options": None, "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # Answers file omits W3
        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {"Q1": {"disposition": "answered", "value": "A", "reply_excerpt": "A", "source": "prose"}},
            "reading_echoed": False, "loop_end": "all-answered",
        }), encoding="utf-8")
        (flow_dir / "answers" / "plan-01.reply-1.md").write_text("A\n", encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 5] Coverage: missing ledger handle fails",
                      error is not None and "answer file missing handle: W3" in error,
                      f"error={error}")

    # Test 6: Round-1 file omits ledger handle
    print("[Test 6] Round-1 file omits ledger handle")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = {
            "seated": ["alice", "bob", "contrarian-carl"],
            "expected_reactions": [
                {"slug": s, "mode": m, "round": 1, "path": f"reactions/{s}-{m}-r1.json"}
                for m in ("pre-show", "reply") for s in ["alice", "bob", "contrarian-carl"]
            ],
            "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}},
            "D1": {"tier": "decided", "brief_id": "B1", "brief_type": "question", "evidence": []},
            "W1": {"tier": "worth-a-look", "brief_id": "B1", "brief_type": "question", "evidence": []},
        }
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "D1", "tier": "decided", "brief_type": "question", "title": "D",
                 "decision_or_recommendation": "A", "why": "Good", "backed_by": ["alice"],
                 "must_answer": False, "reason_must_answer": None, "evidence": {},
                 "options": None, "business_case": None, "pushback": None},
                {"handle": "W1", "tier": "worth-a-look", "brief_type": "question", "title": "W",
                 "decision_or_recommendation": "Maybe", "why": "Unclear", "backed_by": ["alice"],
                 "must_answer": False, "reason_must_answer": None, "evidence": {},
                 "options": None, "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # First report file omits W1 (not allowed for round 1)
        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {"D1": {"disposition": "accepted", "value": None, "reply_excerpt": None}},
            "reading_echoed": False, "loop_end": None,
        }), encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 6] Round-1 file omits ledger handle fails",
                      error is not None and "answer file missing handle: W1" in error,
                      f"error={error}")

    # Test 7: Unknown handle in round-2 file
    print("[Test 7] Unknown handle W33 in round-2 file")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = {
            "seated": ["alice", "bob", "contrarian-carl"],
            "expected_reactions": [
                {"slug": s, "mode": m, "round": 1, "path": f"reactions/{s}-{m}-r1.json"}
                for m in ("pre-show", "reply") for s in ["alice", "bob", "contrarian-carl"]
            ],
            "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}},
            "Q1": {"tier": "question", "brief_id": "B1", "brief_type": "question", "evidence": []},
        }
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        # Round 1: Q1
        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "Q1", "tier": "question", "brief_type": "question", "title": "Q",
                 "decision_or_recommendation": "Choose", "why": "Unclear", "backed_by": ["alice"],
                 "must_answer": True, "reason_must_answer": "question", "evidence": {},
                 "options": [{"label": "A"}], "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {"Q1": {"disposition": "answered", "value": "A", "reply_excerpt": "A", "source": "prose"}},
            "reading_echoed": False, "loop_end": "all-answered",
        }), encoding="utf-8")
        (flow_dir / "answers" / "plan-01.reply-1.md").write_text("A\n", encoding="utf-8")

        # Round 2: Q1 again, but also includes W33 not in ledger
        (flow_dir / "questions" / "plan-02.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 2, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "Q1", "tier": "question", "brief_type": "question", "title": "Q",
                 "decision_or_recommendation": "Choose", "why": "Still unclear", "backed_by": ["alice"],
                 "must_answer": True, "reason_must_answer": "question", "evidence": {},
                 "options": [{"label": "A"}], "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Choose", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        (flow_dir / "answers" / "plan-02.json").write_text(json.dumps({
            "kind": "report", "round": 2, "attempt": 1,
            "items": {
                "Q1": {"disposition": "answered", "value": "A", "reply_excerpt": "A", "source": "prose"},
                "W33": {"disposition": "answered", "value": "B", "reply_excerpt": "B", "source": "prose"},
            },
            "reading_echoed": False, "loop_end": "all-answered",
        }), encoding="utf-8")
        (flow_dir / "answers" / "plan-02.reply-2.md").write_text("A\nB\n", encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 7] Unknown handle fails",
                      error is not None and "unknown handle in plan-02.json: W33" in error,
                      f"error={error}")

    # Test 8: Per-round must-answer coverage
    print("[Test 8] Per-round must-answer coverage: D2 in round 2 must-answer but missing from answer file")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = {
            "seated": ["alice", "bob", "contrarian-carl"],
            "expected_reactions": [
                {"slug": s, "mode": m, "round": 1, "path": f"reactions/{s}-{m}-r1.json"}
                for m in ("pre-show", "reply") for s in ["alice", "bob", "contrarian-carl"]
            ],
            "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}},
            "D1": {"tier": "decided", "brief_id": "B1", "brief_type": "question", "evidence": []},
            "D2": {"tier": "decided", "brief_id": "B1", "brief_type": "question", "evidence": []},
        }
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        # Round 1: D1 answered, D2 accepted
        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "D1", "tier": "decided", "brief_type": "question", "title": "D1",
                 "decision_or_recommendation": "A", "why": "Good", "backed_by": ["alice"],
                 "must_answer": False, "reason_must_answer": None, "evidence": {},
                 "options": None, "business_case": None, "pushback": None},
                {"handle": "D2", "tier": "decided", "brief_type": "question", "title": "D2",
                 "decision_or_recommendation": "B", "why": "Good", "backed_by": ["alice"],
                 "must_answer": False, "reason_must_answer": None, "evidence": {},
                 "options": None, "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {
                "D1": {"disposition": "answered", "value": "A", "reply_excerpt": "A", "source": "prose"},
                "D2": {"disposition": "accepted", "value": None, "reply_excerpt": None},
            },
            "reading_echoed": False, "loop_end": "all-answered",
        }), encoding="utf-8")
        (flow_dir / "answers" / "plan-01.reply-1.md").write_text("A\n", encoding="utf-8")

        # Round 2: D2 must-answer (changed), but answer file omits it
        (flow_dir / "questions" / "plan-02.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 2, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "D2", "tier": "decided", "brief_type": "question", "title": "D2",
                 "decision_or_recommendation": "B", "why": "Changed", "backed_by": ["alice"],
                 "must_answer": True, "reason_must_answer": "changed", "evidence": {},
                 "options": None, "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        (flow_dir / "answers" / "plan-02.json").write_text(json.dumps({
            "kind": "report", "round": 2, "attempt": 1,
            "items": {},  # D2 is missing even though it's must-answer
            "reading_echoed": False, "loop_end": None,
        }), encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 8] Per-round must-answer coverage fails",
                      error is not None and "answer file missing handle: D2 in plan-02.json" in error,
                      f"error={error}")

    # Test 12: Form answer with label, no reply file needed
    print("[Test 12] Form answer with option label, no reply file")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = {
            "seated": ["alice", "bob", "contrarian-carl"],
            "expected_reactions": [
                {"slug": s, "mode": m, "round": 1, "path": f"reactions/{s}-{m}-r1.json"}
                for m in ("pre-show", "reply") for s in ["alice", "bob", "contrarian-carl"]
            ],
            "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}},
            "Q1": {"tier": "question", "brief_id": "B1", "brief_type": "question", "evidence": []},
        }
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "Q1", "tier": "question", "brief_type": "question", "title": "Q",
                 "decision_or_recommendation": "Choose", "why": "Unclear", "backed_by": ["alice"],
                 "must_answer": True, "reason_must_answer": "question", "evidence": {},
                 "options": [{"label": "Option A"}, {"label": "Option B"}], "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Choose", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # Form answer with label excerpt
        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {"Q1": {"disposition": "answered", "value": "Option A", "reply_excerpt": "Option A", "source": "form"}},
            "reading_echoed": False, "loop_end": "all-answered",
        }), encoding="utf-8")

        # No reply file needed for form answers
        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 12] Form answer with label passes",
                      error is None,
                      f"error={error}")

    # Test 13: Form answer with Other text, no reply file needed
    print("[Test 13] Form answer with Other text, no reply file")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = {
            "seated": ["alice", "bob", "contrarian-carl"],
            "expected_reactions": [
                {"slug": s, "mode": m, "round": 1, "path": f"reactions/{s}-{m}-r1.json"}
                for m in ("pre-show", "reply") for s in ["alice", "bob", "contrarian-carl"]
            ],
            "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}},
            "Q1": {"tier": "question", "brief_id": "B1", "brief_type": "question", "evidence": []},
        }
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "Q1", "tier": "question", "brief_type": "question", "title": "Q",
                 "decision_or_recommendation": "Choose", "why": "Unclear", "backed_by": ["alice"],
                 "must_answer": True, "reason_must_answer": "question", "evidence": {},
                 "options": [{"label": "Option A"}], "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Choose", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # Form answer with Other text (non-option text)
        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {"Q1": {"disposition": "answered", "value": "Custom response", "reply_excerpt": "Custom response", "source": "form"}},
            "reading_echoed": False, "loop_end": "all-answered",
        }), encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 13] Form answer with Other text passes",
                      error is None,
                      f"error={error}")

    # Test 15: Accepted-on-continue with loop_end
    print("[Test 15] Accepted-on-continue with loop_end and continue text")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = {
            "seated": ["alice", "bob", "contrarian-carl"],
            "expected_reactions": [
                {"slug": s, "mode": m, "round": 1, "path": f"reactions/{s}-{m}-r1.json"}
                for m in ("pre-show", "reply") for s in ["alice", "bob", "contrarian-carl"]
            ],
            "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}},
            "Q1": {"tier": "question", "brief_id": "B1", "brief_type": "question", "evidence": []},
        }
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "Q1", "tier": "question", "brief_type": "question", "title": "Q",
                 "decision_or_recommendation": "Choose", "why": "Unclear", "backed_by": ["alice"],
                 "must_answer": True, "reason_must_answer": "question", "evidence": {},
                 "options": [{"label": "A"}], "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Choose", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {"Q1": {"disposition": "accepted-on-continue", "value": None, "reply_excerpt": "continue", "source": "prose"}},
            "reading_echoed": False, "loop_end": "continue",
        }), encoding="utf-8")

        (flow_dir / "answers" / "plan-01.reply-1.md").write_text("continue\n", encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 15] Accepted-on-continue with loop_end passes",
                      error is None,
                      f"error={error}")

    # Test 17: Same-round append (same SEQ, same round, re-ask in same file)
    # Contract: the latest answers file (by filesystem write time, not timestamp) for a SEQ wins the fold.
    # A same-round re-ask keeps the same SEQ and round, just appends to the reply file and rewrites the answers file.
    # This tests that the final (re-asked) disposition wins over the initial (unresolved) one.
    print("[Test 17] Same-round append: excerpt from re-asked answer passes")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = {
            "seated": ["alice", "bob", "contrarian-carl"],
            "expected_reactions": [
                {"slug": s, "mode": m, "round": 1, "path": f"reactions/{s}-{m}-r1.json"}
                for m in ("pre-show", "reply") for s in ["alice", "bob", "contrarian-carl"]
            ],
            "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}},
            "Q1": {"tier": "question", "brief_id": "B1", "brief_type": "question", "evidence": []},
        }
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "Q1", "tier": "question", "brief_type": "question", "title": "Q",
                 "decision_or_recommendation": "Choose", "why": "Unclear", "backed_by": ["alice"],
                 "must_answer": True, "reason_must_answer": "question", "evidence": {},
                 "options": [{"label": "A"}, {"label": "B"}], "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # First write: unresolved (the initial response was insufficient)
        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {"Q1": {"disposition": "unresolved", "value": None, "reply_excerpt": None, "source": None}},
            "reading_echoed": False, "loop_end": None,
        }), encoding="utf-8")

        # Second write: re-asked in same round, same SEQ (overwrites the first write; fold sees latest)
        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {"Q1": {"disposition": "answered", "value": "B", "reply_excerpt": "I choose B", "source": "prose"}},
            "reading_echoed": False, "loop_end": "all-answered",
        }), encoding="utf-8")

        # Reply file appends: original unresolved text, then re-ask answer
        (flow_dir / "answers" / "plan-01.reply-1.md").write_text(
            "Original unresolved\n\n--- Re-ask ---\nI choose B\n", encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 17] Same-round append: final answers file wins the fold",
                      error is None,
                      f"error={error}")

    # Test 22: Non-must-answer prose item (F5: reply-file check applies to all non-form, not just must-answer)
    print("[Test 22] Non-must-answer prose item with reply file")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = {
            "seated": ["alice", "bob", "contrarian-carl"],
            "expected_reactions": [
                {"slug": s, "mode": m, "round": 1, "path": f"reactions/{s}-{m}-r1.json"}
                for m in ("pre-show", "reply") for s in ["alice", "bob", "contrarian-carl"]
            ],
            "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}},
            "W1": {"tier": "worth-a-look", "brief_id": "B1", "brief_type": "question", "evidence": []},
        }
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "W1", "tier": "worth-a-look", "brief_type": "question", "title": "W",
                 "decision_or_recommendation": "Maybe", "why": "Unclear", "backed_by": ["alice"],
                 "must_answer": False, "reason_must_answer": None, "evidence": {},
                 "options": None, "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # Non-must-answer item with prose disposition and reply file
        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {"W1": {"disposition": "overridden", "value": "Different path", "reply_excerpt": "I prefer a different path", "source": "prose"}},
            "reading_echoed": False, "loop_end": None,
        }), encoding="utf-8")
        (flow_dir / "answers" / "plan-01.reply-1.md").write_text("I prefer a different path\n", encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 22] Non-must-answer prose item with reply file passes",
                      error is None,
                      f"error={error}")

    # Test 21: Numeric sort dependency - round-1 selection must pick plan-2 (numeric 2, not alphabetic "10")
    # If alphabetic sort is used, plan-10 would be selected and Q1 wouldn't be in round 1, failing the test.
    print("[Test 21] Numeric sort: round-1 selection picks plan-2 (SEQ 2, not 10), Q1 only in plan-2")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = {
            "seated": ["alice", "bob", "contrarian-carl"],
            "expected_reactions": [
                {"slug": s, "mode": m, "round": 1, "path": f"reactions/{s}-{m}-r1.json"}
                for m in ("pre-show", "reply") for s in ["alice", "bob", "contrarian-carl"]
            ],
            "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}, "B2": {"brief_type": "question", "raiser": "alice"}},
            "Q1": {"tier": "question", "brief_id": "B1", "brief_type": "question", "evidence": []},
            "W1": {"tier": "worth-a-look", "brief_id": "B2", "brief_type": "question", "evidence": []},
        }
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        # plan-2: round 1, has Q1 and W1 (both required for round 1 - all ledger handles)
        (flow_dir / "questions" / "plan-2.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "Q1", "tier": "question", "brief_type": "question", "title": "Q",
                 "decision_or_recommendation": "Choose", "why": "Unclear", "backed_by": ["alice"],
                 "must_answer": True, "reason_must_answer": "question", "evidence": {},
                 "options": [{"label": "A"}], "business_case": None, "pushback": None},
                {"handle": "W1", "tier": "worth-a-look", "brief_type": "question", "title": "W",
                 "decision_or_recommendation": "Maybe", "why": "Different", "backed_by": ["alice"],
                 "must_answer": False, "reason_must_answer": None, "evidence": {},
                 "options": None, "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        (flow_dir / "answers" / "plan-2.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {
                "Q1": {"disposition": "answered", "value": "A", "reply_excerpt": "A", "source": "prose"},
                "W1": {"disposition": "accepted", "value": None, "reply_excerpt": None, "source": None},
            },
            "reading_echoed": False, "loop_end": "all-answered",
        }), encoding="utf-8")
        (flow_dir / "answers" / "plan-2.reply-1.md").write_text("A\n", encoding="utf-8")

        # plan-10: round 2 (different round than plan-2), answers later
        # With numeric sort: plan-2 (2 < 10) is selected as round-1 base, has Q1 and W1 → PASS
        # With alphabetic sort: plan-10 ("10" < "2") would be selected, but it's round 2 → FAIL (round mismatch)
        (flow_dir / "questions" / "plan-10.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 2, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "Q1", "tier": "question", "brief_type": "question", "title": "Q (re-ask)",
                 "decision_or_recommendation": "Choose", "why": "Unclear", "backed_by": ["alice"],
                 "must_answer": True, "reason_must_answer": "question", "evidence": {},
                 "options": [{"label": "A"}, {"label": "B"}], "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        (flow_dir / "answers" / "plan-10.json").write_text(json.dumps({
            "kind": "report", "round": 2, "attempt": 1,
            "items": {"Q1": {"disposition": "answered", "value": "B", "reply_excerpt": "B", "source": "prose"}},
            "reading_echoed": False, "loop_end": "all-answered",
        }), encoding="utf-8")
        (flow_dir / "answers" / "plan-10.reply-2.md").write_text("B\n", encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 21] Numeric sort: round-1 has every ledger handle (test fails if alphabetic sort is used)",
                      error is None,
                      f"error={error}")

    # Test 23: Zero answers files - no report-kind answers files exist for the attempt
    # Contract: verifier should fail closed when no answers files contain any ledger handles (F2)
    print("[Test 23] Zero answers files: missing report-kind answers file fails closed")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = {
            "seated": ["alice", "bob", "contrarian-carl"],
            "expected_reactions": [
                {"slug": s, "mode": m, "round": 1, "path": f"reactions/{s}-{m}-r1.json"}
                for m in ("pre-show", "reply") for s in ["alice", "bob", "contrarian-carl"]
            ],
            "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}},
            "Q1": {"tier": "question", "brief_id": "B1", "brief_type": "question", "evidence": []},
        }
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        # Create question file but NO answers file
        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "Q1", "tier": "question", "brief_type": "question", "title": "Q",
                 "decision_or_recommendation": "Choose", "why": "Unclear", "backed_by": ["alice"],
                 "must_answer": True, "reason_must_answer": "question", "evidence": {},
                 "options": [{"label": "A"}], "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 23] Zero answers files fails closed",
                      error is not None and "answer file missing handle" in error,
                      f"error={error}")

    # Test 24: Empty items dict - answers file has empty items dict
    # Contract: fail closed on empty items (F2)
    print("[Test 24] Empty items dict: fails closed on empty items")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = {
            "seated": ["alice", "bob", "contrarian-carl"],
            "expected_reactions": [
                {"slug": s, "mode": m, "round": 1, "path": f"reactions/{s}-{m}-r1.json"}
                for m in ("pre-show", "reply") for s in ["alice", "bob", "contrarian-carl"]
            ],
            "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}},
            "Q1": {"tier": "question", "brief_id": "B1", "brief_type": "question", "evidence": []},
        }
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "Q1", "tier": "question", "brief_type": "question", "title": "Q",
                 "decision_or_recommendation": "Choose", "why": "Unclear", "backed_by": ["alice"],
                 "must_answer": True, "reason_must_answer": "question", "evidence": {},
                 "options": [{"label": "A"}], "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # Answers file with empty items dict - fails validation
        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {},  # Empty items dict - should fail on coverage check
            "reading_echoed": False, "loop_end": None,
        }), encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 24] Empty items dict fails closed",
                      error is not None,
                      f"error={error}")

    # Test 25: Malformed items (non-dict) - items field is not a dict
    # Contract: fail closed on non-dict items (F2)
    print("[Test 25] Malformed items (non-dict): fails closed")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = {
            "seated": ["alice", "bob", "contrarian-carl"],
            "expected_reactions": [
                {"slug": s, "mode": m, "round": 1, "path": f"reactions/{s}-{m}-r1.json"}
                for m in ("pre-show", "reply") for s in ["alice", "bob", "contrarian-carl"]
            ],
            "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}},
            "Q1": {"tier": "question", "brief_id": "B1", "brief_type": "question", "evidence": []},
        }
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [
                {"handle": "Q1", "tier": "question", "brief_type": "question", "title": "Q",
                 "decision_or_recommendation": "Choose", "why": "Unclear", "backed_by": ["alice"],
                 "must_answer": True, "reason_must_answer": "question", "evidence": {},
                 "options": [{"label": "A"}], "business_case": None, "pushback": None},
            ],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # Answers file with items as a list instead of dict - fails validation
        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": [{"handle": "Q1"}],  # List instead of dict - malformed
            "reading_echoed": False, "loop_end": None,
        }), encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 25] Malformed items (non-dict) fails closed",
                      error is not None,
                      f"error={error}")

    # Test 26: wrong kind on a round 2 file must not be silently dropped (#363)
    print("[Test 26] Round 2 answers file with kind 'answers' overriding an earlier answer fails closed")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir, decisions_text = _two_round_checkpoint(
            tmp, ["Q1"],
            {"kind": "report", "round": 1, "attempt": 1, "items": {"Q1": _ans("A")},
             "reading_echoed": False, "loop_end": "all-answered"},
            ["Q1"],
            {"kind": "answers", "round": 2, "attempt": 1, "items": {"Q1": _ans("B")},
             "reading_echoed": False, "loop_end": "all-answered"})
        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 26] Wrong kind names plan-02.json and the bad value",
                      error is not None and "plan-02.json" in error and "kind 'answers', expected 'report'" in error,
                      f"error={error}")

    # Test 27: attempt mismatch on a round 2 file must not be silently dropped (#363)
    print("[Test 27] Round 2 answers file with attempt 2 in an attempt-1 checkpoint fails closed")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir, decisions_text = _two_round_checkpoint(
            tmp, ["Q1"],
            {"kind": "report", "round": 1, "attempt": 1, "items": {"Q1": _ans("A")},
             "reading_echoed": False, "loop_end": "all-answered"},
            ["Q1"],
            {"kind": "report", "round": 2, "attempt": 2, "items": {"Q1": _ans("B")},
             "reading_echoed": False, "loop_end": "all-answered"})
        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 27] Attempt mismatch names plan-02.json, attempt 2 and receipt attempt 1",
                      error is not None and "plan-02.json" in error and "attempt 2" in error
                      and "receipt attempt 1" in error,
                      f"error={error}")

    # Test 28: the #363 ticket shape - strict-subset round 2 supersedes a round-1 unresolved
    print("[Test 28] Round 1 D1 accepted + Q1 unresolved, round 2 only Q1 answered: verifies")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir, decisions_text = _two_round_checkpoint(
            tmp, ["D1", "Q1"],
            {"kind": "report", "round": 1, "attempt": 1,
             "items": {"D1": {"disposition": "accepted", "value": None, "reply_excerpt": None, "source": None},
                       "Q1": {"disposition": "unresolved", "value": None, "reply_excerpt": None, "source": None}},
             "reading_echoed": False, "loop_end": None},
            ["Q1"],
            {"kind": "report", "round": 2, "attempt": 1, "items": {"Q1": _ans("B")},
             "reading_echoed": False, "loop_end": "all-answered"})
        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 28] Strict-subset round 2 supersedes round-1 unresolved",
                      error is None,
                      f"error={error}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
