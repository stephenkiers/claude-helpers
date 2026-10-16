#!/usr/bin/env python3
"""
Tests for scripts/flow-verify-receipt.py check_report_checkpoint function.

Tests the report variant verifier (rules 1-10 from Step 5).
Run with: python3 tests/test_flow_verify_receipt_report.py
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


def main():
    h = Harness("FLOW-VERIFY-RECEIPT REPORT VERIFIER TEST SUITE")

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

    # Test 4: Resume case (attempt 2 with new answers)
    print("[Test 4] Resume - new round with second answers file")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir, decisions_text = create_minimal_fixture(Path(tmp))
        # Add second answers file for round 2
        answers_dir = flow_dir / "answers"
        answers_file_r2 = answers_dir / "plan-01.reply-2.md"
        answers_file_r2.write_text("continuing discussion", encoding="utf-8")

        # Create new answers file for round 2
        answers_file_r2_json = answers_dir / "plan-01.json"
        new_answers = {
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
            "loop_end": "all-answered",
        }
        answers_file_r2_json.write_text(json.dumps(new_answers), encoding="utf-8")
        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("Resume with new round answers", error is None,
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

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
