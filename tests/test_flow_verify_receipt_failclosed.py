#!/usr/bin/env python3
"""
Fail-closed behavior tests for multi-round plan checkpoint verification.

Tests the fail-closed policy for edge cases and error conditions:
- Non-plan-<digits> filenames (F1)
- Unreadable/non-dict/orphaned question files (F2)
- Falsy dispositions (F12)
- Invalid source values (F8)
- Non-must-answer prose handling (F5)

Run with: python3 tests/test_flow_verify_receipt_failclosed.py
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


def create_base_fixture(tmp_path: Path):
    """Create a base fixture with session and flow directories."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    flow_dir = tmp_path / "flow"
    flow_dir.mkdir()
    (flow_dir / "answers").mkdir()
    (flow_dir / "questions").mkdir()

    return session_dir, flow_dir


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


def create_ledger(handles=None):
    """Create a minimal ledger with the given handles."""
    if handles is None:
        handles = {"Q1": {"tier": "question", "brief_id": "B1", "brief_type": "question", "evidence": []}}

    ledger = {
        "seated": ["alice", "bob", "contrarian-carl"],
        "expected_reactions": [
            {"slug": s, "mode": m, "round": 1, "path": f"reactions/{s}-{m}-r1.json"}
            for m in ("pre-show", "reply") for s in ["alice", "bob", "contrarian-carl"]
        ],
        "briefs": {"B1": {"brief_type": "question", "raiser": "alice"}},
    }
    ledger.update(handles)
    return ledger


def create_question_item(handle, must_answer=True):
    """Create a question item for a handle."""
    is_question = handle.startswith("Q")
    return {
        "handle": handle,
        "tier": "question" if is_question else "decided",
        "brief_type": "question",
        "title": f"Item {handle}",
        "decision_or_recommendation": "Choose" if is_question else "A",
        "why": "Reason",
        "backed_by": ["alice"],
        "must_answer": must_answer,
        "reason_must_answer": "question" if must_answer else None,
        "evidence": {},
        "options": [{"label": "A"}] if is_question else None,
        "business_case": None,
        "pushback": None,
    }


def main():
    h = Harness("FLOW-VERIFY-RECEIPT FAIL-CLOSED TESTS")

    # --- Non-plan-<digits> filenames (F1) ----------------------------------------------------------
    print("[Test 1] Non-plan-<digits> filename: answers.json instead of plan-01.json")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = create_ledger()
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [create_question_item("Q1")],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # Use non-standard filename
        (flow_dir / "answers" / "answers.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {"Q1": {"disposition": "answered", "value": "A", "reply_excerpt": "A", "source": "prose"}},
            "loop_end": None,
        }), encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 1] Non-standard filename fails closed",
                      error is not None and "answer file missing handle" in error,
                      f"error={error}")

    print("[Test 2] Non-plan-<digits> filename: plan-abc.json")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = create_ledger()
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [create_question_item("Q1")],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # Use non-numeric filename
        (flow_dir / "answers" / "plan-abc.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {"Q1": {"disposition": "answered", "value": "A", "reply_excerpt": "A", "source": "prose"}},
            "loop_end": None,
        }), encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 2] plan-abc.json fails closed",
                      error is not None and "unreadable question file" in error,
                      f"error={error}")

    print("[Test 3] Non-plan-<digits> filename: plan01.json (no dash)")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = create_ledger()
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [create_question_item("Q1")],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # Use non-dashed filename
        (flow_dir / "answers" / "plan01.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {"Q1": {"disposition": "answered", "value": "A", "reply_excerpt": "A", "source": "prose"}},
            "loop_end": None,
        }), encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 3] plan01.json fails closed",
                      error is not None and "answer file missing handle" in error,
                      f"error={error}")

    # --- Unreadable question file (F2) ---------------------------------------------------------------
    print("[Test 4] Unreadable question file: invalid JSON")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = create_ledger()
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        # Question file with invalid JSON
        (flow_dir / "questions" / "plan-01.json").write_text("not valid json at all", encoding="utf-8")

        # But answer file is fine
        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {"Q1": {"disposition": "answered", "value": "A", "reply_excerpt": "A", "source": "prose"}},
            "loop_end": None,
        }), encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 4] Unreadable question file fails closed",
                      error is not None and "Traceback" not in str(error),
                      f"error={error}")

    # --- Question file is not a dict (F2) -----------------------------------------------------------
    print("[Test 5] Question file not a dict: question file is a list")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = create_ledger()
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        # Question file is a list, not a dict
        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps([
            {"kind": "report", "items": []}
        ]), encoding="utf-8")

        # But answer file is fine
        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {"Q1": {"disposition": "answered", "value": "A", "reply_excerpt": "A", "source": "prose"}},
            "loop_end": None,
        }), encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 5] Question file as list fails closed",
                      error is not None and ("no report question files" in error or "unreadable question file" in error),
                      f"error={error}")

    # --- Orphaned question file (F2) ----------------------------------------------------------------
    print("[Test 6] Orphaned question file: question without matching answer file")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = create_ledger()
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        # Question file exists
        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [create_question_item("Q1")],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # But no answer file (already tested in Test 23 of multiround, but included for completeness)
        # (answer file is NOT created)

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 6] Orphaned question file fails closed",
                      error is not None and "answer file missing handle" in error,
                      f"error={error}")

    # --- Falsy disposition (F12) -------------------------------------------------------------------
    print("[Test 7] Falsy disposition: empty string disposition")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = create_ledger()
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [create_question_item("Q1")],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # Answer file with empty-string disposition
        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {"Q1": {"disposition": "", "value": "A", "reply_excerpt": "A", "source": "prose"}},
            "loop_end": None,
        }), encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 7] Empty-string disposition fails closed",
                      error is not None and "missing disposition" in error,
                      f"error={error}")

    print("[Test 8] Falsy disposition: None disposition")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = create_ledger()
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [create_question_item("Q1")],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # Answer file with None disposition
        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {"Q1": {"disposition": None, "value": "A", "reply_excerpt": "A", "source": "prose"}},
            "loop_end": None,
        }), encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 8] None disposition fails closed",
                      error is not None and "missing disposition" in error,
                      f"error={error}")

    # --- Invalid source values (F8) -----------------------------------------------------------------
    print("[Test 9] Invalid source: source is 'question'")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = create_ledger()
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [create_question_item("Q1")],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # Answer file with invalid source value
        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {"Q1": {"disposition": "answered", "value": "A", "reply_excerpt": "A", "source": "question"}},
            "loop_end": None,
        }), encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 9] Invalid source 'question' fails closed",
                      error is not None and "unknown source value" in error,
                      f"error={error}")

    print("[Test 10] Invalid source: source is 'user'")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = create_ledger()
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [create_question_item("Q1")],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # Answer file with invalid source value
        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {"Q1": {"disposition": "answered", "value": "A", "reply_excerpt": "A", "source": "user"}},
            "loop_end": None,
        }), encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 10] Invalid source 'user' fails closed",
                      error is not None and "unknown source value" in error,
                      f"error={error}")

    # --- Non-must-answer prose handling (F5) -------------------------------------------------------
    # F5: Reply-file checks must apply to ALL non-form records, not just must-answer items.
    # This test verifies that non-must-answer prose without a reply file is rejected.
    print("[Test 11] Non-must-answer prose: fails without reply file (F5 fix)")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = create_ledger()
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [create_question_item("Q1", must_answer=False)],  # Non-must-answer
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # Answer file with prose source but no reply file
        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {"Q1": {"disposition": "answered", "value": "A", "reply_excerpt": "I choose A", "source": "prose"}},
            "loop_end": None,
        }), encoding="utf-8")

        # Note: No plan-01.reply-1.md file exists

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        # F5 fix: non-must-answer prose must have reply file, same as must-answer
        h.test_result("[Test 11] Non-must-answer prose without reply file fails",
                      error is not None and "reply file missing" in error,
                      f"error={error}")

    print("[Test 12] Non-must-answer prose: valid with reply file")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = create_ledger()
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [create_question_item("Q1", must_answer=False)],  # Non-must-answer
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # Answer file with prose source
        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 1, "attempt": 1,
            "items": {"Q1": {"disposition": "answered", "value": "A", "reply_excerpt": "I choose A", "source": "prose"}},
            "loop_end": None,
        }), encoding="utf-8")

        # Create reply file for prose source in answers directory (now with non-must-answer, this should still work)
        (flow_dir / "answers" / "plan-01.reply-1.md").write_text("I choose A\n", encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 12] Non-must-answer prose with reply file passes",
                      error is None,
                      f"error={error}")

    # --- Unreadable answer file (complement to unreadable question file) ----
    print("[Test 13] Unreadable answer file: invalid JSON in answers")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = create_ledger()
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        # Question file is fine
        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [create_question_item("Q1")],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # Answer file with invalid JSON
        (flow_dir / "answers" / "plan-01.json").write_text("{ not valid json", encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 13] Unreadable answer file fails closed",
                      error is not None and "Traceback" not in str(error),
                      f"error={error}")

    print("[Test 14] Answer file not a dict: answer file is a list")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = create_ledger()
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [create_question_item("Q1")],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # Answer file is a list, not a dict
        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps([
            {"kind": "report", "items": {}}
        ]), encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 14] Answer file as list fails closed",
                      error is not None and "unreadable answers file" in error,
                      f"error={error}")

    # --- Shape validation on fold data (F3) ------------------------------------------------
    print("[Test 15] Shape validation: missing 'kind' in answer file fails")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = create_ledger()
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 1, "step": "plan", "command": "expert-plan",
            "items": [create_question_item("Q1")],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # Answer file missing 'kind' key
        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "round": 1, "attempt": 1,  # Missing 'kind'
            "items": {"Q1": {"disposition": "answered", "value": "A", "reply_excerpt": "A", "source": "prose"}},
            "loop_end": None,
        }), encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 15] Missing 'kind' key in answer file fails closed",
                      error is not None and ("missing 'kind'" in error or "plan-01" in error),
                      f"error={error}")

    # --- Mutation flag: round validation (0 and bool must be rejected) --------
    print("[Test 16] Invalid round: round is 0")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = create_ledger()
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": 0, "step": "plan", "command": "expert-plan",
            "items": [create_question_item("Q1")],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # Answer file with round 0 (invalid)
        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": 0, "attempt": 1,
            "items": {"Q1": {"disposition": "answered", "value": "A", "reply_excerpt": "A", "source": "prose"}},
            "loop_end": None,
        }), encoding="utf-8")

        # Create reply file in answers directory
        (flow_dir / "answers" / "plan-01.reply-0.md").write_text("A\n", encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 16] Round 0 fails closed",
                      error is not None and "not a positive integer" in error,
                      f"error={error}")

    print("[Test 17] Invalid round: round is a bool (true)")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir, flow_dir = create_base_fixture(Path(tmp))
        create_base_reactions(session_dir)

        ledger = create_ledger()
        decisions_text = f"""# Decisions
## Checkpoint ledger
```json
{json.dumps(ledger)}
```
"""

        # Note: bool True will be treated as round 1 by JSON, so we need a different round value in question file
        (flow_dir / "questions" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "attempt": 1, "round": True, "step": "plan", "command": "expert-plan",
            "items": [create_question_item("Q1")],
            "questions": [{"id": "reply", "brief": None, "question": "Reply", "header": None, "options": [{"label": "Answer"}]}]
        }), encoding="utf-8")

        # Answer file with round as bool (invalid)
        (flow_dir / "answers" / "plan-01.json").write_text(json.dumps({
            "kind": "report", "round": True, "attempt": 1,
            "items": {"Q1": {"disposition": "answered", "value": "A", "reply_excerpt": "A", "source": "prose"}},
            "loop_end": None,
        }), encoding="utf-8")

        # Create reply file in answers directory
        (flow_dir / "answers" / "plan-01.reply-1.md").write_text("A\n", encoding="utf-8")

        error = check_report_checkpoint(session_dir, flow_dir, decisions_text)
        h.test_result("[Test 17] Bool round fails closed",
                      error is not None and "not a positive integer" in error,
                      f"error={error}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
