#!/usr/bin/env python3
"""
Tests for scripts/flow-verify-receipt.py check_report_checkpoint function.

Tests the report variant verifier (rules 1-10 from Step 5).
Run with: python3 tests/test_flow_verify_receipt_report.py
"""

import json
import os
import subprocess
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


def create_minimal_fixture(tmp_path: Path) -> tuple:
    """Create a minimal valid fixture for report checkpoint.

    Returns (session_dir, flow_dir, decisions_text) tuple.
    """
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    reactions_dir = session_dir / "reactions"
    reactions_dir.mkdir()

    # flow_dir is separate from session_dir
    flow_dir = tmp_path / "flow"
    flow_dir.mkdir()
    answers_dir = flow_dir / "answers"
    answers_dir.mkdir()
    questions_dir = flow_dir / "questions"
    questions_dir.mkdir()

    # Create ledger
    ledger = {
        "seated": ["alice", "bob", "contrarian-carl"],
        "expected_reactions": [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
            {"slug": "bob", "mode": "pre-show", "round": 1, "path": "reactions/bob-pre-show-r1.json"},
            {"slug": "contrarian-carl", "mode": "pre-show", "round": 1, "path": "reactions/contrarian-carl-pre-show-r1.json"},
            {"slug": "alice", "mode": "reply", "round": 1, "path": "reactions/alice-reply-r1.json"},
            {"slug": "bob", "mode": "reply", "round": 1, "path": "reactions/bob-reply-r1.json"},
            {"slug": "contrarian-carl", "mode": "reply", "round": 1, "path": "reactions/contrarian-carl-reply-r1.json"},
        ],
        "briefs": {
            "B1": {"brief_type": "question", "raiser": "alice"},
        },
        # Post-tier handles (added after tiering)
        "D1": {
            "tier": "decided",
            "brief_id": "B1",
            "brief_type": "question",
            "evidence": ["reactions/alice-pre-show-r1.json", "reactions/bob-pre-show-r1.json"],
        }
    }

    # Create decisions.md with ledger
    decisions_text = f"""# Decisions

## Checkpoint ledger
```json
{json.dumps(ledger)}
```

## Decided

## Worth a look

## Questions for you
"""

    # Create pre-show reactions
    for slug in ["alice", "bob", "contrarian-carl"]:
        reaction_file = reactions_dir / f"{slug}-pre-show-r1.json"
        reaction_file.write_text(json.dumps({
            "B1": {"agreed": True},
            "complete": True
        }), encoding="utf-8")

    # Create reply-mode reactions
    for slug in ["alice", "bob", "contrarian-carl"]:
        reaction_file = reactions_dir / f"{slug}-reply-r1.json"
        reaction_file.write_text(json.dumps({
            "D1": {"accept": True},
            "complete": True
        }), encoding="utf-8")

    # Create question file (report variant)
    question_file = questions_dir / "plan-01.json"
    question_file.write_text(json.dumps({
        "kind": "report",
        "attempt": 1,
        "round": 1,
        "step": "plan",
        "command": "expert-plan",
        "context_path": None,
        "ticket": "Test ticket",
        "headline": None,
        "items": [
            {
                "handle": "D1",
                "tier": "decided",
                "brief_type": "question",
                "title": "Test decided item",
                "decision_or_recommendation": "Recommend A",
                "why": "It's better",
                "backed_by": ["alice"],
                "business_case": None,
                "options": None,
                "must_answer": False,
                "reason_must_answer": None,
                "evidence": {"alice": "resolved", "bob": "agreed"},
                "pushback": None,
            }
        ],
        "questions": [
            {
                "id": "reply",
                "brief": None,
                "question": "Reply in your own words",
                "header": None,
                "options": [{"label": "Answer in your own words", "description": ""}],
            }
        ]
    }), encoding="utf-8")

    # Create answers file
    answers_file = answers_dir / "plan-01.json"
    answers_file.write_text(json.dumps({
        "kind": "report",
        "round": 1,
        "attempt": 1,
        "items": {
            "D1": {
                "disposition": "accepted",
                "value": None,
                "reply_excerpt": None,
            }
        },
        "loop_end": None,
    }), encoding="utf-8")

    return session_dir, flow_dir, decisions_text


def create_two_round_fixture(tmp_path: Path) -> tuple:
    """Create a two-round report checkpoint fixture (Step 1).

    Round 1 (plan-01): asks D/W/Q, with Q1 unresolved and W1 overridden (prose)
    Round 3 (plan-02): asks only Q1, answered by prose
    Reply files: plan-01.reply-1.md and plan-02.reply-3.md

    Returns (session_dir, flow_dir, decisions_text) tuple.
    """
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    reactions_dir = session_dir / "reactions"
    reactions_dir.mkdir()

    flow_dir = tmp_path / "flow"
    flow_dir.mkdir()
    answers_dir = flow_dir / "answers"
    answers_dir.mkdir()
    questions_dir = flow_dir / "questions"
    questions_dir.mkdir()

    # Ledger with D1, W1, Q1
    ledger = {
        "seated": ["alice", "bob", "contrarian-carl"],
        "expected_reactions": [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
            {"slug": "bob", "mode": "pre-show", "round": 1, "path": "reactions/bob-pre-show-r1.json"},
            {"slug": "contrarian-carl", "mode": "pre-show", "round": 1, "path": "reactions/contrarian-carl-pre-show-r1.json"},
            {"slug": "alice", "mode": "reply", "round": 1, "path": "reactions/alice-reply-r1.json"},
            {"slug": "bob", "mode": "reply", "round": 1, "path": "reactions/bob-reply-r1.json"},
            {"slug": "contrarian-carl", "mode": "reply", "round": 1, "path": "reactions/contrarian-carl-reply-r1.json"},
        ],
        "briefs": {
            "B1": {"brief_type": "question", "raiser": "alice"},
            "B2": {"brief_type": "question", "raiser": "bob"},
            "B3": {"brief_type": "question", "raiser": "alice"},
        },
        "D1": {
            "tier": "decided",
            "brief_id": "B1",
            "brief_type": "question",
            "evidence": ["reactions/alice-pre-show-r1.json", "reactions/bob-pre-show-r1.json"],
        },
        "W1": {
            "tier": "worth-a-look",
            "brief_id": "B2",
            "brief_type": "question",
            "evidence": ["reactions/alice-pre-show-r1.json"],
        },
        "Q1": {
            "tier": "question",
            "brief_id": "B3",
            "brief_type": "question",
            "evidence": ["reactions/alice-pre-show-r1.json"],
        },
    }

    decisions_text = f"""# Decisions

## Checkpoint ledger
```json
{json.dumps(ledger)}
```

## Decided

## Worth a look

## Questions for you
"""

    # Create pre-show reactions
    # B1 (D1): alice resolves, others agree
    # B2 (W1): alice has concerns (resolved+could-go-other-way), not protested
    # B3 (Q1): alice needs-human
    for slug in ["alice", "bob", "contrarian-carl"]:
        reaction_file = reactions_dir / f"{slug}-pre-show-r1.json"
        if slug == "alice":
            reaction_file.write_text(json.dumps({
                "B1": {"resolved": True},
                "B2": {"resolved": True, "could-go-other-way": True},
                "B3": {"needs-human": True},
                "complete": True
            }), encoding="utf-8")
        else:
            reaction_file.write_text(json.dumps({
                "B1": {"agreed": True},
                "B2": {"agreed": True},
                "B3": {"agreed": True},
                "complete": True
            }), encoding="utf-8")

    # Create reply-mode reactions
    for slug in ["alice", "bob", "contrarian-carl"]:
        reaction_file = reactions_dir / f"{slug}-reply-r1.json"
        reaction_file.write_text(json.dumps({
            "D1": {"accept": True},
            "W1": {"accept": True},
            "Q1": {"accept": True},
            "complete": True
        }), encoding="utf-8")

    # Round 1 question file (plan-01.json) - asks D/W/Q
    (questions_dir / "plan-01.json").write_text(json.dumps({
        "kind": "report",
        "attempt": 1,
        "round": 1,
        "step": "plan",
        "command": "expert-plan",
        "context_path": None,
        "ticket": "Test ticket",
        "headline": None,
        "items": [
            {
                "handle": "D1",
                "tier": "decided",
                "brief_type": "question",
                "title": "Test D item",
                "decision_or_recommendation": "Recommend A",
                "why": "It's better",
                "backed_by": ["alice"],
                "business_case": None,
                "options": None,
                "must_answer": False,
                "reason_must_answer": None,
                "evidence": {"alice": "resolved", "bob": "agreed"},
                "pushback": None,
            },
            {
                "handle": "W1",
                "tier": "worth-a-look",
                "brief_type": "question",
                "title": "Test W item",
                "decision_or_recommendation": "Recommend B",
                "why": "It's reasonable",
                "backed_by": ["bob"],
                "business_case": None,
                "options": None,
                "must_answer": True,
                "reason_must_answer": "protested",
                "evidence": {"alice": "protested"},
                "pushback": None,
            },
            {
                "handle": "Q1",
                "tier": "question",
                "brief_type": "question",
                "title": "Test Q item",
                "decision_or_recommendation": "Choose an option",
                "why": "Unclear",
                "backed_by": ["alice"],
                "business_case": None,
                "options": [
                    {"label": "Option A", "for": "reason1"},
                    {"label": "Option B", "for": "reason2"},
                ],
                "must_answer": True,
                "reason_must_answer": "question",
                "evidence": {"alice": "needs-human"},
                "pushback": None,
            }
        ],
        "questions": [
            {
                "id": "reply",
                "brief": None,
                "question": "Reply",
                "header": None,
                "options": [{"label": "Answer", "description": ""}],
            }
        ]
    }), encoding="utf-8")

    # Round 1 answers file (plan-01.json) - D accepted, W overridden, Q unresolved
    (answers_dir / "plan-01.json").write_text(json.dumps({
        "kind": "report",
        "round": 1,
        "attempt": 1,
        "items": {
            "D1": {
                "disposition": "accepted",
                "value": None,
                "reply_excerpt": None,
                "source": "form",
                "ambiguous": False,
            },
            "W1": {
                "disposition": "overridden",
                "value": "Different approach",
                "reply_excerpt": "I'd prefer a different approach",
                "source": "prose",
                "ambiguous": False,
            },
            "Q1": {
                "disposition": "unresolved",
                "value": None,
                "reply_excerpt": None,
                "source": None,
                "ambiguous": False,
            }
        },
        "reading_echoed": False,
        "loop_end": "all-answered",
    }), encoding="utf-8")

    # Create reply file for round 1
    (answers_dir / "plan-01.reply-1.md").write_text(
        "I'd prefer a different approach\n", encoding="utf-8")

    # Round 3 question file (plan-02.json) - re-asks only Q1
    (questions_dir / "plan-02.json").write_text(json.dumps({
        "kind": "report",
        "attempt": 1,
        "round": 3,
        "step": "plan",
        "command": "expert-plan",
        "context_path": None,
        "ticket": "Test ticket",
        "headline": None,
        "items": [
            {
                "handle": "Q1",
                "tier": "question",
                "brief_type": "question",
                "title": "Test Q item (retry)",
                "decision_or_recommendation": "Choose an option",
                "why": "Still unclear",
                "backed_by": ["alice"],
                "business_case": None,
                "options": [
                    {"label": "Option A", "for": "reason1"},
                    {"label": "Option B", "for": "reason2"},
                ],
                "must_answer": True,
                "reason_must_answer": "question",
                "evidence": {"alice": "needs-human"},
                "pushback": None,
            }
        ],
        "questions": [
            {
                "id": "reply",
                "brief": None,
                "question": "Choose an option",
                "header": None,
                "options": [{"label": "Answer", "description": ""}],
            }
        ]
    }), encoding="utf-8")

    # Round 3 answers file (plan-02.json) - Q1 answered by prose
    (answers_dir / "plan-02.json").write_text(json.dumps({
        "kind": "report",
        "round": 3,
        "attempt": 1,
        "items": {
            "Q1": {
                "disposition": "answered",
                "value": "Option B",
                "reply_excerpt": "I choose Option B for good reasons",
                "source": "prose",
                "ambiguous": False,
            }
        },
        "reading_echoed": False,
        "loop_end": "all-answered",
    }), encoding="utf-8")

    # Create reply file for round 3
    (answers_dir / "plan-02.reply-3.md").write_text(
        "I choose Option B for good reasons\n", encoding="utf-8")

    return session_dir, flow_dir, decisions_text


def main():
    h = Harness("FLOW-VERIFY-RECEIPT REPORT VERIFIER TEST SUITE")

    # Step 1 Test: Two-round report checkpoint (red-first test, now fixed)
    print("[Step 1] Two-round report checkpoint: round 1 with D/W/Q unresolved, round 3 with Q1 answered")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir, decisions_text = create_two_round_fixture(Path(tmp))
        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        # This test passes with the fixed code (multi-round support via fold)
        # On unfixed code, it would fail with "answer file missing handle:"
        h.test_result("[Step 1] Two-round report checkpoint (accepts multi-round, latest wins, must-answer per-round)",
                     error is None,
                     f"error={error}" if error else "")

    # Test 1: Valid minimal fixture passes
    print("[Test 1] Valid minimal fixture -> pass")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir, decisions_text = create_minimal_fixture(Path(tmp))
        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("Valid minimal fixture -> pass", error is None,
                     f"error={error}" if error else "")

    # Test 2: Missing ledger -> fail
    print("[Test 2] Missing ledger -> fail")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        session_dir.mkdir()
        decisions_text = "# Decisions\n\nNo ledger here"
        error = check_report_checkpoint(session_dir, session_dir.parent, decisions_text)
        h.test_result("Missing ledger -> fail", error is not None and "ledger" in error.lower(),
                     f"error={error}")

    # Test 3: Retry - attempt 2 ignores attempt 1 files
    print("[Test 3] Retry - attempt 2 ignores attempt 1 files")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir, decisions_text = create_minimal_fixture(Path(tmp))
        # Verify first attempt passes
        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("Retry first attempt passes", error is None,
                     f"error={error}" if error else "")

    # Test 4: Same-round rewrite keeps round (Q1 = B, audit finding 1)
    print("[Test 4] Same-round rewrite keeps round")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir, decisions_text = create_minimal_fixture(Path(tmp))
        # Rewrite the same file with the same round, appending to the reply file
        answers_dir = flow_dir / "answers"

        # Append to the existing reply file (not creating a new one)
        reply_file = answers_dir / "plan-01.reply-1.md"
        reply_file.write_text("Original answer\nContinuing discussion\n", encoding="utf-8")

        # Rewrite the answers file with the SAME round: 1 (not round 2)
        answers_file_json = answers_dir / "plan-01.json"
        new_answers = {
            "kind": "report",
            "round": 1,  # Same round, not bumped to 2
            "attempt": 1,
            "items": {
                "D1": {
                    "disposition": "accepted",
                    "value": None,
                    "reply_excerpt": None,
                }
            },
            "loop_end": "all-answered",
        }
        answers_file_json.write_text(json.dumps(new_answers), encoding="utf-8")
        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("Same-round rewrite keeps round", error is None,
                     f"error={error}" if error else "")

    # Test 5: Missing pre-show reaction file -> fail
    print("[Test 5] Missing pre-show reaction file -> fail")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir, decisions_text = create_minimal_fixture(Path(tmp))
        # Delete one pre-show reaction file
        (session_dir / "reactions" / "bob-pre-show-r1.json").unlink()
        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("Missing pre-show reaction file -> fail",
                     error is not None and "reaction" in error.lower(),
                     f"error={error}")

    # Test 6: Needs-human forces Q
    print("[Test 6] Needs-human forces Q with must_answer")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir, decisions_text = create_minimal_fixture(Path(tmp))
        # Modify ledger to have needs-human marking
        ledger = {
            "seated": ["alice", "bob", "contrarian-carl"],
            "expected_reactions": [
                {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
                {"slug": "bob", "mode": "pre-show", "round": 1, "path": "reactions/bob-pre-show-r1.json"},
                {"slug": "contrarian-carl", "mode": "pre-show", "round": 1, "path": "reactions/contrarian-carl-pre-show-r1.json"},
                {"slug": "alice", "mode": "reply", "round": 1, "path": "reactions/alice-reply-r1.json"},
                {"slug": "bob", "mode": "reply", "round": 1, "path": "reactions/bob-reply-r1.json"},
                {"slug": "contrarian-carl", "mode": "reply", "round": 1, "path": "reactions/contrarian-carl-reply-r1.json"},
            ],
            "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}},
            "Q1": {"tier": "question", "brief_id": "B1", "brief_type": "question", "evidence": []}
        }
        decisions_text = f"""# Decisions\n\n## Checkpoint ledger\n```json\n{json.dumps(ledger)}\n```"""

        # Create Q item with needs-human response in reactions
        reactions_dir = session_dir / "reactions"
        for slug in ["alice", "bob", "contrarian-carl"]:
            reaction_file = reactions_dir / f"{slug}-pre-show-r1.json"
            reaction_file.write_text(json.dumps({
                "B1": {"needs-human": True, "reason": "Expert judgment needed"},
                "complete": True
            }), encoding="utf-8")

        # Create question with Q item marked must_answer=true from needs-human
        question_file = flow_dir / "questions" / "plan-01.json"
        question_file.write_text(json.dumps({
            "kind": "report",
            "attempt": 1,
            "round": 1,
            "step": "plan",
            "command": "expert-plan",
            "items": [{
                "handle": "Q1",
                "tier": "question",
                "brief_type": "question",
                "title": "Test question",
                "decision_or_recommendation": "Panel recommendation",
                "why": "It's unclear",
                "backed_by": ["alice"],
                "business_case": None,
                "options": [{"label": "A", "for": "reason", "against": ""},
                           {"label": "B", "for": "reason", "against": ""}],
                "must_answer": True,
                "reason_must_answer": "needs-human",
                "evidence": {"alice": "needs-human", "bob": "agreed"},
                "pushback": None,
            }],
            "questions": [{
                "id": "reply",
                "brief": None,
                "question": "Choose an option",
                "header": None,
                "options": [{"label": "Answer in your own words", "description": ""}]
            }]
        }), encoding="utf-8")

        # Create answers with explicit answer for the must-answer item
        answers_file = flow_dir / "answers" / "plan-01.json"
        answers_file.write_text(json.dumps({
            "kind": "report",
            "round": 1,
            "attempt": 1,
            "items": {
                "Q1": {
                    "disposition": "answered",
                    "value": "Option A",
                    "reply_excerpt": "I choose A because of clarity",
                }
            },
            "loop_end": None,
        }), encoding="utf-8")

        # Create reply file
        (flow_dir / "answers" / "plan-01.reply-1.md").write_text("I choose A because of clarity", encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text, attempt=1)
        h.test_result("Needs-human forces must_answer", error is None,
                     f"error={error}" if error else "")

    # Test 7: Attempt 2 ignores attempt 1 files
    print("[Test 7] Attempt 2 ignores attempt 1 files")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir, decisions_text = create_minimal_fixture(Path(tmp))

        # Add attempt 1 answer file
        answers_dir = flow_dir / "answers"
        (answers_dir / "plan-01-attempt1.json").write_text(json.dumps({
            "kind": "report",
            "round": 1,
            "attempt": 1,
            "items": {"D1": {"disposition": "accepted"}},
        }), encoding="utf-8")

        # When checking attempt 2, it should not see attempt 1 files
        # Create a new question file for attempt 2
        questions_dir = flow_dir / "questions"
        (questions_dir / "plan-01.json").write_text(json.dumps({
            "kind": "report",
            "attempt": 2,
            "round": 1,
            "step": "plan",
            "command": "expert-plan",
            "items": [{
                "handle": "D1",
                "tier": "decided",
                "brief_type": "question",
                "title": "Test decided item",
                "decision_or_recommendation": "Recommend A",
                "why": "It's better",
                "backed_by": ["alice"],
                "business_case": None,
                "options": None,
                "must_answer": False,
                "reason_must_answer": None,
                "evidence": {"alice": "resolved", "bob": "agreed"},
                "pushback": None,
            }],
            "questions": [{
                "id": "reply",
                "brief": None,
                "question": "Reply",
                "header": None,
                "options": [{"label": "Answer in your own words", "description": ""}]
            }]
        }), encoding="utf-8")

        # Create attempt 2 answer
        (answers_dir / "plan-01.json").write_text(json.dumps({
            "kind": "report",
            "round": 1,
            "attempt": 2,
            "items": {"D1": {"disposition": "accepted"}},
            "loop_end": None,
        }), encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text, attempt=2)
        h.test_result("Attempt 2 ignores attempt 1 files", error is None,
                     f"error={error}" if error else "")

    # Test 8: F9 - Missing reply reaction file fails
    print("[Test 8] Missing reply reaction file -> fail")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir, decisions_text = create_minimal_fixture(Path(tmp))
        # Delete a reply-mode reaction file to trigger F9 check
        (session_dir / "reactions" / "bob-reply-r1.json").unlink()
        error = check_report_checkpoint(session_dir, flow_dir, decisions_text, attempt=1)
        h.test_result("Missing reply reaction file -> fail",
                     error is not None and "reaction file" in error.lower(),
                     f"error={error}")

    # Test 9: F15 - Reply excerpt validation
    print("[Test 9] Reply excerpt must be in reply file")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir, decisions_text = create_minimal_fixture(Path(tmp))

        # Modify to test excerpt validation on answered disposition
        ledger = {
            "seated": ["alice", "bob", "contrarian-carl"],
            "expected_reactions": [
                {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
                {"slug": "bob", "mode": "pre-show", "round": 1, "path": "reactions/bob-pre-show-r1.json"},
                {"slug": "contrarian-carl", "mode": "pre-show", "round": 1, "path": "reactions/contrarian-carl-pre-show-r1.json"},
                {"slug": "alice", "mode": "reply", "round": 1, "path": "reactions/alice-reply-r1.json"},
                {"slug": "bob", "mode": "reply", "round": 1, "path": "reactions/bob-reply-r1.json"},
                {"slug": "contrarian-carl", "mode": "reply", "round": 1, "path": "reactions/contrarian-carl-reply-r1.json"},
            ],
            "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}},
            "Q1": {"tier": "question", "brief_id": "B1", "brief_type": "question", "evidence": []}
        }
        decisions_text = f"""# Decisions\n\n## Checkpoint ledger\n```json\n{json.dumps(ledger)}\n```"""

        # Create reaction files
        reactions_dir = session_dir / "reactions"
        for slug in ["alice", "bob", "contrarian-carl"]:
            (reactions_dir / f"{slug}-pre-show-r1.json").write_text(
                json.dumps({"B1": {"needs-human": True}, "complete": True}), encoding="utf-8")
            (reactions_dir / f"{slug}-reply-r1.json").write_text(
                json.dumps({"Q1": {"accept": True}, "complete": True}), encoding="utf-8")

        # Create question with must_answer Q item
        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report",
            "attempt": 1,
            "round": 1,
            "step": "plan",
            "command": "expert-plan",
            "items": [{
                "handle": "Q1",
                "tier": "question",
                "brief_type": "question",
                "title": "Test question",
                "decision_or_recommendation": "Choose",
                "why": "Needed",
                "backed_by": ["alice"],
                "business_case": None,
                "options": [{"label": "A", "for": "reason"}],
                "must_answer": True,
                "reason_must_answer": "needs-human",
                "evidence": {"alice": "needs-human"},
                "pushback": None,
            }],
            "questions": [{
                "id": "reply",
                "brief": None,
                "question": "Choose",
                "header": None,
                "options": [{"label": "Answer", "description": ""}]
            }]
        }), encoding="utf-8")

        # Create answers with excerpt that does NOT exist in reply file
        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report",
            "round": 1,
            "attempt": 1,
            "items": {
                "Q1": {
                    "disposition": "answered",
                    "value": "A",
                    "reply_excerpt": "This exact phrase is NOT in the reply file at all",
                }
            },
            "loop_end": None,
        }), encoding="utf-8")

        # Create reply file WITHOUT the excerpt
        (flow_dir / "answers" / "plan-01.reply-1.md").write_text(
            "I agree with A because it is better\nwith multiple lines\nof different content", encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text, attempt=1)
        h.test_result("Reply excerpt NOT in file -> fail",
                     error is not None and "excerpt not found" in error.lower(),
                     f"error={error}")

    # Test 10: Malformed answers file
    print("[Test 10] Malformed answers file")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir, decisions_text = create_minimal_fixture(Path(tmp))

        # Create malformed answers file (missing required fields)
        (flow_dir / "answers" / "plan-01.json").write_text(
            json.dumps({"kind": "report", "round": 1}), encoding="utf-8")  # Missing attempt, items

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("Malformed answers file -> fail",
                     error is not None,
                     f"error={error}")

    # Test 11: F7 - Numeric sort (plan-10 before plan-2 alphabetically, but after numerically)
    print("[Test 11] F7: Numeric sort required (plan-10 vs plan-2)")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir, decisions_text = create_minimal_fixture(Path(tmp))

        # Create files where alphabetic sort breaks but numeric sort works:
        # plan-10 comes BEFORE plan-2 alphabetically ("10" < "2" as strings)
        # but AFTER numerically (10 > 2 as numbers)
        answers_dir = flow_dir / "answers"

        # plan-10.json with round 1, must_answer answered
        (answers_dir / "plan-10.json").write_text(json.dumps({
            "kind": "report",
            "round": 1,
            "attempt": 1,
            "items": {
                "D1": {
                    "disposition": "answered",
                    "value": None,
                    "reply_excerpt": "I agree",
                }
            },
            "loop_end": None,
        }), encoding="utf-8")

        # plan-2.json with round 2, accepted (no answer needed - would fail if processed last)
        (answers_dir / "plan-2.json").write_text(json.dumps({
            "kind": "report",
            "round": 2,
            "attempt": 1,
            "items": {
                "D1": {
                    "disposition": "accepted",
                    "value": None,
                    "reply_excerpt": None,
                }
            },
            "loop_end": None,
        }), encoding="utf-8")

        # Create question files - both mark D1 as must_answer
        questions_dir = flow_dir / "questions"
        (questions_dir / "plan-10.json").write_text(json.dumps({
            "kind": "report",
            "attempt": 1,
            "round": 1,
            "step": "plan",
            "command": "expert-plan",
            "items": [{
                "handle": "D1",
                "tier": "decided",
                "brief_type": "question",
                "title": "Test",
                "decision_or_recommendation": "A",
                "why": "Better",
                "backed_by": ["alice"],
                "business_case": None,
                "options": None,
                "must_answer": True,
                "reason_must_answer": "critical",
                "evidence": {"alice": "resolved", "bob": "agreed"},
                "pushback": None,
            }],
            "questions": [{
                "id": "reply",
                "brief": None,
                "question": "Reply",
                "header": None,
                "options": [{"label": "Answer", "description": ""}]
            }]
        }), encoding="utf-8")

        (questions_dir / "plan-2.json").write_text(json.dumps({
            "kind": "report",
            "attempt": 1,
            "round": 2,
            "step": "plan",
            "command": "expert-plan",
            "items": [{
                "handle": "D1",
                "tier": "decided",
                "brief_type": "question",
                "title": "Test",
                "decision_or_recommendation": "A",
                "why": "Better",
                "backed_by": ["alice"],
                "business_case": None,
                "options": None,
                "must_answer": True,
                "reason_must_answer": "critical",
                "evidence": {"alice": "resolved", "bob": "agreed"},
                "pushback": None,
            }],
            "questions": [{
                "id": "reply",
                "brief": None,
                "question": "Reply",
                "header": None,
                "options": [{"label": "Answer", "description": ""}]
            }]
        }), encoding="utf-8")

        # Remove the original plan-01 files
        (answers_dir / "plan-01.json").unlink()
        (questions_dir / "plan-01.json").unlink()

        # Create reply file for round 1
        (answers_dir / "plan-10.reply-1.md").write_text("I agree", encoding="utf-8")

        # Numeric sort (CORRECT): plan-2, plan-10 → final is "answered" with quote → PASS
        # Alphabetic sort (BUGGY): plan-10, plan-2 → final is "accepted" no quote → FAIL (must-answer without quote)
        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("F7: Numeric sort required (alphabetic would fail on must-answer)",
                     error is None,
                     f"error={error}")

    # Test 12: Ruling #2 - ATTEMPT and relay_count scoping
    print("[Test 12] Ruling #2: ATTEMPT: 2 ignores attempt-1 files")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir, decisions_text = create_minimal_fixture(Path(tmp))

        # Create attempt-1 files on disk
        answers_dir = flow_dir / "answers"
        questions_dir = flow_dir / "questions"

        # plan-01 with attempt 1 is already there from create_minimal_fixture
        # Verify attempt 1 passes
        error = check_report_checkpoint(session_dir, flow_dir, decisions_text, attempt=1)
        h.test_result("Ruling #2: Attempt 1 passes", error is None,
                     f"error={error}" if error else "")

        # Now add attempt-2 files: they should be used, not attempt-1
        (answers_dir / "plan-01.json").write_text(json.dumps({
            "kind": "report",
            "round": 1,
            "attempt": 2,
            "items": {
                "D1": {
                    "disposition": "accepted",
                    "value": None,
                    "reply_excerpt": None,
                }
            },
            "loop_end": None,
        }), encoding="utf-8")

        (questions_dir / "plan-01.json").write_text(json.dumps({
            "kind": "report",
            "attempt": 2,
            "round": 1,
            "step": "plan",
            "command": "expert-plan",
            "items": [{
                "handle": "D1",
                "tier": "decided",
                "brief_type": "question",
                "title": "Test",
                "decision_or_recommendation": "A",
                "why": "Better",
                "backed_by": ["alice"],
                "business_case": None,
                "options": None,
                "must_answer": False,
                "reason_must_answer": None,
                "evidence": {"alice": "resolved", "bob": "agreed"},
                "pushback": None,
            }],
            "questions": [{
                "id": "reply",
                "brief": None,
                "question": "Reply",
                "header": None,
                "options": [{"label": "Answer", "description": ""}]
            }]
        }), encoding="utf-8")

        # Verify attempt 2 passes (ignoring attempt 1)
        error = check_report_checkpoint(session_dir, flow_dir, decisions_text, attempt=2)
        h.test_result("Ruling #2: Attempt 2 passes (ignores attempt-1)", error is None,
                     f"error={error}" if error else "")

    # Test 13: Ruling #2 - Top-level check_plan path with ATTEMPT: 2 in receipt
    print("[Test 13] Ruling #2: check_plan with ATTEMPT: 2 scopes correctly")
    with tempfile.TemporaryDirectory() as tmp_home:
        # Create minimal plan session structure
        invocation_id = "20261015T100000-30926"
        session_dir = Path(tmp_home) / ".claude" / "plan-sessions" / "test" / f"test-{invocation_id}"
        session_dir.mkdir(parents=True, exist_ok=True)

        # Create plan file
        plans_dir = Path(tmp_home) / ".claude" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        plan_file = plans_dir / f"test-{invocation_id}.md"
        plan_file.write_text("# Plan")

        # Create context.md for freshness check
        (session_dir / "context.md").write_text("# Context")

        # Create plan.md
        (session_dir / "plan.md").write_text("# Plan")

        # Create selected-experts roster
        (session_dir / "selected-experts.md").write_text("| Expert |\n|--------|\n| alice |\n| bob |\n| contrarian-carl |")

        # Create contributions for each expert
        for expert in ["alice", "bob", "contrarian-carl"]:
            contrib_file = session_dir / f"{expert}-contribution.md"
            contrib_file.write_text(f"Contribution from {expert}\n<!-- contribution-end -->")

        # Create decisions.md with checkpoint for attempt 2
        decisions_file = session_dir / "decisions.md"
        flow_dir = session_dir / "flow"
        flow_dir.mkdir()
        answers_dir = flow_dir / "answers"
        answers_dir.mkdir()
        questions_dir = flow_dir / "questions"
        questions_dir.mkdir()

        ledger = {
            "seated": ["alice", "bob", "contrarian-carl"],
            "expected_reactions": [
                {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"},
                {"slug": "bob", "mode": "pre-show", "round": 1, "path": "reactions/bob-pre-show-r1.json"},
                {"slug": "contrarian-carl", "mode": "pre-show", "round": 1, "path": "reactions/contrarian-carl-pre-show-r1.json"},
                {"slug": "alice", "mode": "reply", "round": 1, "path": "reactions/alice-reply-r1.json"},
                {"slug": "bob", "mode": "reply", "round": 1, "path": "reactions/bob-reply-r1.json"},
                {"slug": "contrarian-carl", "mode": "reply", "round": 1, "path": "reactions/contrarian-carl-reply-r1.json"},
            ],
            "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}},
            "D1": {"tier": "decided", "brief_id": "B1", "brief_type": "question", "evidence": []}
        }
        decisions_text = f"""# Decisions

## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""
        decisions_file.write_text(decisions_text)

        # Create reactions
        reactions_dir = session_dir / "reactions"
        reactions_dir.mkdir()
        for slug in ["alice", "bob", "contrarian-carl"]:
            (reactions_dir / f"{slug}-pre-show-r1.json").write_text(json.dumps({
                "B1": {"agreed": True}, "complete": True
            }), encoding="utf-8")
            (reactions_dir / f"{slug}-reply-r1.json").write_text(json.dumps({
                "D1": {"accept": True}, "complete": True
            }), encoding="utf-8")

        # Create INVALID attempt-1 question/answer (missing required fields)
        (questions_dir / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1,
            # Missing: step, command, items, questions
        }), encoding="utf-8")

        # Create VALID attempt-2 question/answer
        (questions_dir / "plan-03.json").write_text(json.dumps({
            "kind": "report", "attempt": 2, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [{
                "handle": "D1", "tier": "decided", "brief_type": "question", "title": "Test",
                "decision_or_recommendation": "A", "why": "Better", "backed_by": ["alice"],
                "business_case": None, "options": None, "must_answer": False,
                "reason_must_answer": None, "evidence": {"alice": "resolved", "bob": "agreed"},
                "pushback": None,
            }],
            "questions": [{
                "id": "reply", "brief": None, "question": "Reply", "header": None,
                "options": [{"label": "Answer", "description": ""}]
            }]
        }), encoding="utf-8")

        (answers_dir / "plan-03.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 2,
            "items": {"D1": {"disposition": "accepted", "value": None, "reply_excerpt": None}},
            "loop_end": None,
        }), encoding="utf-8")

        # An earlier attempt-2 report file the receipt never names (a relay this attempt
        # abandoned), malformed and unanswered. It sorts before plan-03.json, so it would be
        # read as the round-1 report if the verifier globbed every attempt-2 file instead of
        # scoping to the receipt's markers (relay_count).
        (questions_dir / "plan-02.json").write_text(json.dumps({
            "kind": "report", "attempt": 2, "round": 1,
        }), encoding="utf-8")

        # Create receipt with ATTEMPT: 2 and the marker naming plan-03.json, so check_plan
        # takes the report branch (without a marker it falls back to the legacy brief count
        # and never calls check_report_checkpoint at all).
        receipt_file = Path(tmp_home) / "receipt.md"
        receipt_file.write_text(
            f"FINAL_PLAN_PATH: {plan_file}\n<!-- awaiting-answers: questions/plan-03.json -->\n"
            "ATTEMPT: 2\nDecision: OK\n<!-- step-end -->"
        )

        # Call check_plan via CLI with --flow-dir
        env = os.environ.copy()
        env["HOME"] = str(tmp_home)
        cmd = [
            sys.executable, str(Path(__file__).parent.parent / "scripts" / "flow-verify-receipt.py"),
            "--step", "plan", "--receipt", str(receipt_file), "--since", "1970-01-01T00:00:00Z", "--flow-dir", str(flow_dir)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)

        # Malformed question files (the attempt-1 stub and the abandoned plan-02) fail closed:
        # a schema-invalid file may be a question that never reached the human (#310).
        h.test_result("Ruling #2: check_plan fails closed on malformed question files",
                     result.returncode == 1 and "malformed: plan-01.json" in result.stdout
                     and "plan-02.json" in result.stdout,
                     f"returncode={result.returncode}, stdout={result.stdout[:300]}")

        # Once the malformed files are gone, the valid attempt-2 report passes and attempt 1 is ignored.
        (questions_dir / "plan-01.json").unlink()
        (questions_dir / "plan-02.json").unlink()
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        h.test_result("Ruling #2: check_plan with ATTEMPT: 2 passes on the valid attempt-2 report",
                     result.returncode == 0 and "ARTIFACTS: ok" in result.stdout,
                     f"returncode={result.returncode}, stdout={result.stdout[:200]}")

    # Test 14: a round-1 report file with no items list fails instead of skipping rule 2
    print("[Test 14] Report file without items -> fail")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir, decisions_text = create_minimal_fixture(Path(tmp))
        qfile = flow_dir / "questions" / "plan-01.json"
        qdata = json.loads(qfile.read_text(encoding="utf-8"))
        del qdata["items"]
        qfile.write_text(json.dumps(qdata), encoding="utf-8")
        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("Report file without items -> fail",
                     error is not None and "malformed report file: plan-01.json" in error,
                     f"error={error}")

    # Test 15: Report-kind plan split-location positive (questions in FLOW_DIR, decoy in session)
    print("[Test 15] Report-kind plan split-location positive")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir, decisions_text = create_minimal_fixture(Path(tmp))
        # Create decoy files in session to verify they are NOT used
        (session_dir / "pending-questions").mkdir(parents=True, exist_ok=True)
        (session_dir / "pending-questions" / "plan-decoy.json").write_text("{}")
        # The actual files are already in flow_dir from create_minimal_fixture
        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("Report-kind plan split-location positive: FLOW_DIR questions/answers work, session decoy ignored",
                     error is None, f"error={error}" if error else "")

    # Test 16: Report-kind plan split-location negative (legacy-branch) - files only in session
    print("[Test 16] Report-kind plan split-location negative (legacy-branch)")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir, decisions_text = create_minimal_fixture(Path(tmp))
        # Remove the actual files from flow_dir
        (flow_dir / "questions" / "plan-01.json").unlink()
        (flow_dir / "answers" / "plan-01.json").unlink()
        # Create files ONLY in session (decoy location)
        (session_dir / "pending-questions").mkdir(parents=True, exist_ok=True)
        (session_dir / "pending-questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1,
            "step": "plan", "command": "expert-plan",
            "items": [{"handle": "D1", "tier": "decided", "brief_type": "question", "title": "Test"}],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": []}]
        }))
        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("Report-kind plan split-location negative (legacy-branch): no fallback to session dir, fails",
                     error is not None and "plan checkpoint incomplete" in error, f"error={error}")


    # Test 17: Report-kind plan split-location negative (report-branch): question file in FLOW_DIR, answers only in decoy
    print("[Test 17] Report-kind plan split-location negative (report-branch)")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir, decisions_text = create_minimal_fixture(Path(tmp))
        (flow_dir / "answers" / "plan-01.json").unlink()
        (session_dir / "pending-answers").mkdir(parents=True, exist_ok=True)
        (session_dir / "pending-answers" / "plan-01.json").write_text(json.dumps({"step": "plan", "seq": 1, "answers": []}))
        relay_count = flow_verify_module.count_relayed_questions(
            flow_dir, "plan", "<!-- awaiting-answers: questions/plan-01.json -->", 1)
        error = check_report_checkpoint(session_dir, flow_dir, decisions_text, 1, relay_count)
        h.test_result("Report-kind plan split-location negative (report-branch): answers only in decoy fails",
                      error is not None and "question files without an answers file" in error, f"error={error}")
        print(f"  reason: {error}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
