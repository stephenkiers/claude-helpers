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
        h.test_result("[Test 2] Latest wins: final unresolved fails",
                      error is not None and "unresolved" in error.lower(),
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
        h.test_result("[Test 9] Round mismatch fails",
                      error is not None and "round mismatch" in error.lower(),
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
        h.test_result("[Test 10] Prose missing reply file fails",
                      error is not None and "reply file missing" in error.lower() and "plan-02.reply-3.md" in (error or ""),
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
        h.test_result("[Test 11] Prose round 0 fails",
                      error is not None and "round not a positive integer" in error.lower(),
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
        h.test_result("[Test 14] Form empty excerpt fails",
                      error is not None and "without quote" in error.lower(),
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
        h.test_result("[Test 16] Accepted-on-continue without loop_end fails",
                      error is not None and "without loop_end" in error.lower(),
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
        h.test_result("[Test 18] Wrong shape: 'answers' key fails",
                      error is not None and ("uses 'answers' key" in error.lower() or "answer file missing handle" in error.lower()),
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
        h.test_result("[Test 19] Wrong shape: missing 'attempt' fails",
                      error is not None and "missing 'attempt' key" in error.lower() and "plan-01.json" in (error or ""),
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
        h.test_result("[Test 20] Mixed shape: legacy round 2 fails with specific reason",
                      error is not None and ("missing 'kind' key" in error.lower() or "uses 'answers' key" in error.lower() or "missing 'attempt' key" in error.lower())
                      and "plan-02.json" in (error or ""),
                      f"error={error}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
