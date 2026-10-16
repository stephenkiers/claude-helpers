#!/usr/bin/env python3
"""
Tests for scripts/plan_checkpoint_schema.py module functions.

Tests the schema module's public API:
- extract_ledger_json: fence parsing with ``` and ```json
- classify_reaction: precedence (needs-human > protested > resolved > agreed > no-view)
- tier_item: tier rule with self-agreement exclusion and needs-human
- confine_path_to_dir: path validation (reject outside SESSION_DIR)
- validate_*_shape: input validation

Run with: python3 tests/test_plan_checkpoint_schema.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _test_harness import Harness
import plan_checkpoint_schema as schema


def main():
    h = Harness("PLAN-CHECKPOINT-SCHEMA TESTS")

    # ========== extract_ledger_json tests ==========

    # Test 1: Extract ledger with ``` fence (markdown-style)
    print("[Test 1] Extract ledger with ``` fence")
    markdown_decisions = """# Decisions

## Checkpoint ledger
```
{"seated": ["alice"], "expected_reactions": [], "briefs": {}}
```

## Decided
"""
    result = schema.extract_ledger_json(markdown_decisions)
    h.test_result(
        "Extract ledger with ``` fence",
        result is not None and result.get("seated") == ["alice"],
        f"result={result}"
    )

    # Test 2: Extract ledger with ```json fence
    print("[Test 2] Extract ledger with ```json fence")
    markdown_decisions_json = """# Decisions

## Checkpoint ledger
```json
{"seated": ["alice", "bob"], "expected_reactions": [], "briefs": {}}
```

## Decided
"""
    result = schema.extract_ledger_json(markdown_decisions_json)
    h.test_result(
        "Extract ledger with ```json fence",
        result is not None and result.get("seated") == ["alice", "bob"],
        f"result={result}"
    )

    # Test 3: Fence parser returns None on invalid JSON
    print("[Test 3] Invalid JSON in fence returns None")
    bad_json = """# Decisions

## Checkpoint ledger
```json
{not valid json}
```

## Decided
"""
    result = schema.extract_ledger_json(bad_json)
    h.test_result(
        "Invalid JSON returns None",
        result is None,
        f"result={result}"
    )

    # Test 4: Fence parser returns None when no fence found
    print("[Test 4] No fence in document returns None")
    no_fence = """# Decisions

## Checkpoint ledger

No fence here

## Decided
"""
    result = schema.extract_ledger_json(no_fence)
    h.test_result(
        "No fence returns None",
        result is None,
        f"result={result}"
    )

    # Test 5: Fence parser returns None if result is not a dict
    print("[Test 5] Non-dict result in fence returns None")
    array_fence = """# Decisions

## Checkpoint ledger
```json
["item1", "item2"]
```

## Decided
"""
    result = schema.extract_ledger_json(array_fence)
    h.test_result(
        "Non-dict result returns None",
        result is None,
        f"result={result}"
    )

    # ========== classify_reaction tests ==========

    # Test 6: Precedence: needs-human > protested > resolved > agreed > no-view
    print("[Test 6] needs-human has highest precedence")
    reaction = {
        "needs-human": True,
        "protested": True,
        "resolved": True,
        "agreed": True,
        "no-view": True,
        "reason": "Expert judgment needed"
    }
    classification = schema.classify_reaction(reaction, "alice")
    h.test_result(
        "needs-human has highest precedence",
        classification == "needs-human",
        f"classification={classification}"
    )

    # Test 7: Protested beats resolved/agreed/no-view
    print("[Test 7] protested beats resolved/agreed/no-view")
    reaction = {
        "protested": True,
        "resolved": True,
        "agreed": True,
        "no-view": True
    }
    classification = schema.classify_reaction(reaction, "alice")
    h.test_result(
        "protested beats resolved/agreed/no-view",
        classification == "protested",
        f"classification={classification}"
    )

    # Test 8: Resolved beats agreed/no-view
    print("[Test 8] resolved beats agreed/no-view")
    reaction = {
        "resolved": True,
        "agreed": True,
        "no-view": True
    }
    classification = schema.classify_reaction(reaction, "alice")
    h.test_result(
        "resolved beats agreed/no-view",
        classification == "resolved",
        f"classification={classification}"
    )

    # Test 9: Agreed beats no-view
    print("[Test 9] agreed beats no-view")
    reaction = {
        "agreed": True,
        "no-view": True
    }
    classification = schema.classify_reaction(reaction, "alice")
    h.test_result(
        "agreed beats no-view",
        classification == "agreed",
        f"classification={classification}"
    )

    # Test 10: No-view when all are false/absent
    print("[Test 10] no-view as fallback")
    reaction = {
        "resolved": False,
        "agreed": False,
        "protested": False,
        "needs-human": False
    }
    classification = schema.classify_reaction(reaction, "alice")
    h.test_result(
        "no-view as fallback",
        classification == "no-view",
        f"classification={classification}"
    )

    # Test 11: Empty reaction dict -> no-view
    print("[Test 11] Empty reaction dict -> no-view")
    reaction = {}
    classification = schema.classify_reaction(reaction, "alice")
    h.test_result(
        "Empty reaction dict -> no-view",
        classification == "no-view",
        f"classification={classification}"
    )

    # ========== tier_item tests ==========

    # Test 12: Disagreement brief -> Q (regardless of votes)
    print("[Test 12] Disagreement brief_type -> Q")
    tier = schema.tier_item(
        brief_type="disagreement",
        protested_by=[],
        needs_human_by=[],
        resolved_by=["alice"],
        agreed_by=["bob"],
        raiser_slug="alice",
        could_go_other_way=False
    )
    h.test_result(
        "Disagreement brief_type -> Q",
        tier == "question",
        f"tier={tier}"
    )

    # Test 13: Protested by any expert -> Q
    print("[Test 13] Any protested -> Q")
    tier = schema.tier_item(
        brief_type="question",
        protested_by=["alice"],
        needs_human_by=[],
        resolved_by=[],
        agreed_by=["bob"],
        raiser_slug="alice",
        could_go_other_way=False
    )
    h.test_result(
        "Any protested -> Q",
        tier == "question",
        f"tier={tier}"
    )

    # Test 14: needs-human by any expert -> Q
    print("[Test 14] Any needs-human -> Q")
    tier = schema.tier_item(
        brief_type="question",
        protested_by=[],
        needs_human_by=["alice"],
        resolved_by=[],
        agreed_by=["bob"],
        raiser_slug="alice",
        could_go_other_way=False
    )
    h.test_result(
        "Any needs-human -> Q",
        tier == "question",
        f"tier={tier}"
    )

    # Test 15: Resolved + other agreement (not resolver) + no could-go-other-way -> D
    print("[Test 15] Resolved + other-agreement + no could-go-other-way -> D")
    tier = schema.tier_item(
        brief_type="question",
        protested_by=[],
        needs_human_by=[],
        resolved_by=["alice"],
        agreed_by=["bob"],
        raiser_slug="alice",
        could_go_other_way=False
    )
    h.test_result(
        "Resolved + other-agreement + no could-go-other-way -> D",
        tier == "decided",
        f"tier={tier}"
    )

    # Test 16: Resolved + could-go-other-way -> W (demoted from D)
    print("[Test 16] Resolved + could-go-other-way -> W")
    tier = schema.tier_item(
        brief_type="question",
        protested_by=[],
        needs_human_by=[],
        resolved_by=["alice"],
        agreed_by=["bob"],
        raiser_slug="alice",
        could_go_other_way=True
    )
    h.test_result(
        "Resolved + could-go-other-way -> W",
        tier == "worth-a-look",
        f"tier={tier}"
    )

    # Test 17: Self-agreement doesn't count (resolver + only self agreement -> W)
    print("[Test 17] Self-agreement doesn't count for D rule")
    tier = schema.tier_item(
        brief_type="question",
        protested_by=[],
        needs_human_by=[],
        resolved_by=["alice"],
        agreed_by=["alice"],  # self-agreement
        raiser_slug="alice",
        could_go_other_way=False
    )
    h.test_result(
        "Self-agreement doesn't count for D rule",
        tier == "worth-a-look",
        f"tier={tier}"
    )

    # Test 18: Resolved + other agreement but no-view doesn't count as disagreement
    print("[Test 18] no-view doesn't demote D")
    tier = schema.tier_item(
        brief_type="question",
        protested_by=[],
        needs_human_by=[],
        resolved_by=["alice"],
        agreed_by=["bob"],
        raiser_slug="alice",
        could_go_other_way=False
    )
    h.test_result(
        "no-view doesn't demote D",
        tier == "decided",
        f"tier={tier}"
    )

    # Test 19: Resolved with no other agreement -> W
    print("[Test 19] Resolved with no other agreement -> W")
    tier = schema.tier_item(
        brief_type="question",
        protested_by=[],
        needs_human_by=[],
        resolved_by=["alice"],
        agreed_by=[],
        raiser_slug="alice",
        could_go_other_way=False
    )
    h.test_result(
        "Resolved with no other agreement -> W",
        tier == "worth-a-look",
        f"tier={tier}"
    )

    # ========== confine_path_to_dir tests ==========

    # Test 20: Absolute path outside directory -> returns error string
    print("[Test 20] Absolute path outside dir returns error")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp)
        outside_path = "/etc/passwd"
        error = schema.confine_path_to_dir(outside_path, session_dir)
        h.test_result("Absolute path outside dir returns error", error is not None, f"Error: {error}")

    # Test 21: Relative path escape attack (../) -> returns error string
    print("[Test 21] Relative path escape (../) returns error")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp)
        escape_path = "../../etc/passwd"
        error = schema.confine_path_to_dir(escape_path, session_dir)
        h.test_result("Relative path escape (../) returns error", error is not None, f"Error: {error}")

    # Test 22: Valid relative path within directory -> returns None
    print("[Test 22] Valid relative path within dir returns None")
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp)
        valid_path = "reactions/alice-pre-show-r1.json"
        result = schema.confine_path_to_dir(valid_path, session_dir)
        h.test_result("Valid relative path within dir returns None", result is None, f"Result: {result}")

    # ========== validate_reaction_file_shape tests ==========

    # Test 23: Valid pre-show reaction file
    print("[Test 23] Valid pre-show reaction file")
    reaction_data = {
        "complete": True,
        "B1": {"resolved": True, "agreed": False, "reason": "Agree with resolution"},
        "B2": {"no-view": True}
    }
    try:
        error = schema.validate_reaction_file_shape(reaction_data)
        h.test_result("Valid pre-show reaction file", error is None, f"Error: {error}")
    except Exception as e:
        h.test_result("Valid pre-show reaction file", False, f"Exception: {e}")

    # Test 24: Missing complete sentinel -> error
    print("[Test 24] Missing complete sentinel -> error")
    reaction_data = {
        "B1": {"resolved": True}
    }
    try:
        error = schema.validate_reaction_file_shape(reaction_data)
        h.test_result("Missing complete sentinel -> error", error is not None, f"Error: {error}")
    except Exception as e:
        h.test_result("Missing complete sentinel -> error", False, f"Exception: {e}")

    # Test 25: Valid reply-mode reaction file
    print("[Test 25] Valid reply-mode reaction file")
    reaction_data = {
        "complete": True,
        "D1": {"accept": True},
        "W1": {"pushback": True, "reason": "Needs more thought"}
    }
    try:
        error = schema.validate_reaction_file_shape(reaction_data)
        h.test_result("Valid reply-mode reaction file", error is None, f"Error: {error}")
    except Exception as e:
        h.test_result("Valid reply-mode reaction file", False, f"Exception: {e}")

    # Test 26: Valid pre-show reaction with at least one boolean classification key
    print("[Test 26] Pre-show reaction with classification keys")
    reaction_data = {
        "complete": True,
        "B1": {"resolved": True, "reason": "Agree"}
    }
    try:
        error = schema.validate_reaction_file_shape(reaction_data)
        h.test_result("Pre-show reaction with classification keys", error is None, f"Error: {error}")
    except Exception as e:
        h.test_result("Pre-show reaction with classification keys", False, f"Exception: {e}")

    # ========== validate_ledger_shape tests ==========

    # Test 27: Valid ledger shape
    print("[Test 27] Valid ledger shape")
    ledger = {
        "seated": ["alice", "bob"],
        "expected_reactions": [
            {"slug": "alice", "mode": "pre-show", "round": 1, "path": "reactions/alice-pre-show-r1.json"}
        ],
        "briefs": {
            "B1": {"brief_type": "question", "raiser": "alice"}
        }
    }
    try:
        error = schema.validate_ledger_shape(ledger)
        h.test_result("Valid ledger shape", error is None, f"Error: {error}")
    except Exception as e:
        h.test_result("Valid ledger shape", False, f"Exception: {e}")

    # Test 28: Missing seated -> error
    print("[Test 28] Missing seated in ledger -> error")
    ledger = {
        "expected_reactions": [],
        "briefs": {}
    }
    try:
        error = schema.validate_ledger_shape(ledger)
        h.test_result("Missing seated in ledger -> error", error is not None, f"Error: {error}")
    except Exception as e:
        h.test_result("Missing seated in ledger -> error", False, f"Exception: {e}")

    # Test 29: Ledger with post-tier handles (D/W/Q)
    print("[Test 29] Ledger with post-tier handles")
    ledger = {
        "seated": ["alice", "bob"],
        "expected_reactions": [],
        "briefs": {
            "B1": {"brief_type": "question", "raiser": "alice"}
        },
        "D1": {
            "tier": "decided",
            "brief_id": "B1",
            "brief_type": "question",
            "evidence": []
        }
    }
    try:
        error = schema.validate_ledger_shape(ledger)
        h.test_result("Ledger with post-tier handles", error is None, f"Error: {error}")
    except Exception as e:
        h.test_result("Ledger with post-tier handles", False, f"Exception: {e}")

    # ========== validate_answers_file_shape tests ==========

    # Test 30: Valid report-kind answers file
    print("[Test 30] Valid report-kind answers file")
    answers = {
        "kind": "report",
        "round": 1,
        "attempt": 1,
        "items": {
            "D1": {"disposition": "answered", "reply_excerpt": "I agree"}
        }
    }
    try:
        error = schema.validate_answers_file_shape(answers, "report")
        h.test_result("Valid report-kind answers file", error is None, f"Error: {error}")
    except Exception as e:
        h.test_result("Valid report-kind answers file", False, f"Exception: {e}")

    # Test 31: Missing kind -> error
    print("[Test 31] Missing kind in answers -> error")
    answers = {
        "round": 1,
        "attempt": 1,
        "items": {}
    }
    try:
        error = schema.validate_answers_file_shape(answers, "report")
        h.test_result("Missing kind in answers -> error", error is not None, f"Error: {error}")
    except Exception as e:
        h.test_result("Missing kind in answers -> error", False, f"Exception: {e}")

    # Test 32: Report answers missing round/attempt -> error
    print("[Test 32] Report answers missing round/attempt -> error")
    answers = {
        "kind": "report",
        "items": {}
    }
    try:
        error = schema.validate_answers_file_shape(answers, "report")
        h.test_result("Report answers missing round/attempt -> error", error is not None, f"Error: {error}")
    except Exception as e:
        h.test_result("Report answers missing round/attempt -> error", False, f"Exception: {e}")

    # Test 33: Multiple resolvers (2+ resolved=True) still requires other agreement for D
    print("[Test 33] Multiple resolvers still need other agreement for D")
    tier = schema.tier_item(
        brief_type="question",
        protested_by=[],
        needs_human_by=[],
        resolved_by=["alice", "bob"],
        agreed_by=[],
        raiser_slug="alice",
        could_go_other_way=False
    )
    h.test_result(
        "Multiple resolvers still need other agreement for D",
        tier == "worth-a-look",
        f"tier={tier}"
    )

    # Test 34: Resolver + multiple other agreements -> D
    print("[Test 34] Resolver + multiple other agreements -> D")
    tier = schema.tier_item(
        brief_type="question",
        protested_by=[],
        needs_human_by=[],
        resolved_by=["alice"],
        agreed_by=["bob", "carol"],
        raiser_slug="alice",
        could_go_other_way=False
    )
    h.test_result(
        "Resolver + multiple other agreements -> D",
        tier == "decided",
        f"tier={tier}"
    )

    # Test 35: Both fence styles work with whitespace variations
    print("[Test 35] Fence parsing with whitespace variations")
    markdown_with_whitespace = """# Decisions

## Checkpoint ledger
```
{"seated": ["alice"], "expected_reactions": [], "briefs": {}}
```

## Decided
"""
    result = schema.extract_ledger_json(markdown_with_whitespace)
    h.test_result(
        "Fence parsing with whitespace variations",
        result is not None and result.get("seated") == ["alice"],
        f"result={result}"
    )

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
