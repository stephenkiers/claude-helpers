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

    # ========== nested_handle_map_reason tests ==========

    # Test 36: Nested handles under 'handles' key returns reason naming the key and handles
    print("[Test 36] Nested handles under 'handles' key returns appropriate reason")
    ledger = {
        "seated": ["alice"],
        "expected_reactions": [],
        "briefs": {},
        "handles": {
            "D1": {"tier": "decided", "brief_id": "B1", "brief_type": "question", "evidence": []},
            "Q1": {"tier": "question", "brief_id": "B2", "brief_type": "question", "evidence": []}
        }
    }
    reason = schema.nested_handle_map_reason(ledger)
    h.test_result(
        "Nested handles under 'handles' key",
        reason is not None and "handles" in reason and "D1" in reason and "Q1" in reason,
        f"reason={reason}"
    )

    # Test 37: Nested handles under 'decisions' key returns reason naming 'decisions'
    print("[Test 37] Nested handles under 'decisions' key returns appropriate reason")
    ledger = {
        "seated": ["alice"],
        "expected_reactions": [],
        "briefs": {},
        "decisions": {
            "W1": {"tier": "worth-a-look", "brief_id": "B3", "brief_type": "question", "evidence": []}
        }
    }
    reason = schema.nested_handle_map_reason(ledger)
    h.test_result(
        "Nested handles under 'decisions' key",
        reason is not None and "decisions" in reason,
        f"reason={reason}"
    )

    # Test 38: Pre-tier ledger (no handles anywhere) returns None
    print("[Test 38] Pre-tier ledger returns None")
    ledger = {
        "seated": ["alice"],
        "expected_reactions": [],
        "briefs": {
            "B1": {"brief_type": "question", "raiser": "alice"}
        }
    }
    reason = schema.nested_handle_map_reason(ledger)
    h.test_result(
        "Pre-tier ledger returns None",
        reason is None,
        f"reason={reason}"
    )

    # Test 39: Correct top-level handles return None
    print("[Test 39] Correct top-level handles return None")
    ledger = {
        "seated": ["alice"],
        "expected_reactions": [],
        "briefs": {},
        "D1": {"tier": "decided", "brief_id": "B1", "brief_type": "question", "evidence": []},
        "Q1": {"tier": "question", "brief_id": "B2", "brief_type": "question", "evidence": []}
    }
    reason = schema.nested_handle_map_reason(ledger)
    h.test_result(
        "Correct top-level handles return None",
        reason is None,
        f"reason={reason}"
    )

    # Test 40: 'briefs' key is excluded (not treated as nested handles)
    print("[Test 40] 'briefs' key is excluded from nested handle detection")
    ledger = {
        "seated": ["alice"],
        "expected_reactions": [],
        "briefs": {
            "B1": {"brief_type": "question", "raiser": "alice"}
        }
    }
    reason = schema.nested_handle_map_reason(ledger)
    h.test_result(
        "'briefs' key excluded",
        reason is None,
        f"reason={reason}"
    )

    # Test 41: Ledger with only 'briefs' (no handles anywhere) returns None
    print("[Test 41] Ledger with only briefs returns None")
    ledger = {
        "seated": ["alice"],
        "expected_reactions": [],
        "briefs": {
            "B1": {"brief_type": "question", "raiser": "alice"},
            "B2": {"brief_type": "question", "raiser": "bob"}
        }
    }
    reason = schema.nested_handle_map_reason(ledger)
    h.test_result(
        "Ledger with only briefs returns None",
        reason is None,
        f"reason={reason}"
    )

    # ========== LEDGER_HANDLE_RE pattern equality test ==========

    # Test 42: Schema's LEDGER_HANDLE_RE pattern equals verifier's _LEDGER_HANDLE_RE pattern
    print("[Test 42] Schema and verifier regex patterns are equal")
    try:
        import importlib.util
        verify_script = Path(__file__).parent.parent / "scripts" / "flow-verify-receipt.py"
        spec = importlib.util.spec_from_file_location("flow_verify_receipt", verify_script)
        flow_verify_receipt = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(flow_verify_receipt)

        schema_pattern = schema.LEDGER_HANDLE_RE.pattern
        verifier_pattern = flow_verify_receipt._LEDGER_HANDLE_RE.pattern
        h.test_result(
            "Schema and verifier regex patterns equal",
            schema_pattern == verifier_pattern,
            f"schema={schema_pattern}, verifier={verifier_pattern}"
        )
    except Exception as e:
        h.test_result(
            "Schema and verifier regex patterns equal",
            False,
            f"Error: {e}"
        )

    # ========== validate_handle_entries tests ==========

    # Test 43: Flat entry (string value instead of dict) returns 'not an object'
    print("[Test 43] Flat entry (string value) returns 'not an object'")
    ledger = {
        "seated": ["alice"],
        "expected_reactions": [],
        "briefs": {},
        "D1": "decided"  # Flat shape, not a dict
    }
    reason = schema.validate_handle_entries(ledger)
    h.test_result(
        "Flat entry returns 'not an object'",
        reason is not None and "not an object" in reason and "D1" in reason,
        f"reason={reason}"
    )

    # Test 44: Entry missing 'tier' field returns appropriate reason
    print("[Test 44] Entry missing 'tier' field")
    ledger = {
        "seated": ["alice"],
        "expected_reactions": [],
        "briefs": {},
        "D1": {"brief_id": "B1", "brief_type": "question", "evidence": []}
    }
    reason = schema.validate_handle_entries(ledger)
    h.test_result(
        "Entry missing 'tier' field",
        reason is not None and "tier" in reason and "D1" in reason,
        f"reason={reason}"
    )

    # Test 45: Entry missing 'brief_id' field returns appropriate reason
    print("[Test 45] Entry missing 'brief_id' field")
    ledger = {
        "seated": ["alice"],
        "expected_reactions": [],
        "briefs": {},
        "D1": {"tier": "decided", "brief_type": "question", "evidence": []}
    }
    reason = schema.validate_handle_entries(ledger)
    h.test_result(
        "Entry missing 'brief_id' field",
        reason is not None and "brief_id" in reason and "D1" in reason,
        f"reason={reason}"
    )

    # Test 46: Entry missing 'brief_type' field returns appropriate reason
    print("[Test 46] Entry missing 'brief_type' field")
    ledger = {
        "seated": ["alice"],
        "expected_reactions": [],
        "briefs": {},
        "D1": {"tier": "decided", "brief_id": "B1", "evidence": []}
    }
    reason = schema.validate_handle_entries(ledger)
    h.test_result(
        "Entry missing 'brief_type' field",
        reason is not None and "brief_type" in reason and "D1" in reason,
        f"reason={reason}"
    )

    # Test 47: Pre-tier ledger (no handles) returns None
    print("[Test 47] Pre-tier ledger returns None")
    ledger = {
        "seated": ["alice"],
        "expected_reactions": [],
        "briefs": {}
    }
    reason = schema.validate_handle_entries(ledger)
    h.test_result(
        "Pre-tier ledger returns None",
        reason is None,
        f"reason={reason}"
    )

    # Test 48: Complete entry with all fields returns None
    print("[Test 48] Complete entry with all fields returns None")
    ledger = {
        "seated": ["alice"],
        "expected_reactions": [],
        "briefs": {},
        "D1": {"tier": "decided", "brief_id": "B1", "brief_type": "question", "evidence": []},
        "Q1": {"tier": "question", "brief_id": "B2", "brief_type": "question", "evidence": []}
    }
    reason = schema.validate_handle_entries(ledger)
    h.test_result(
        "Complete entries return None",
        reason is None,
        f"reason={reason}"
    )

    # Test 49: Entry with empty string for 'tier' is missing (empty string counts as missing)
    print("[Test 49] Entry with empty string for 'tier' is treated as missing")
    ledger = {
        "seated": ["alice"],
        "expected_reactions": [],
        "briefs": {},
        "D1": {"tier": "", "brief_id": "B1", "brief_type": "question", "evidence": []}
    }
    reason = schema.validate_handle_entries(ledger)
    h.test_result(
        "Empty string 'tier' is treated as missing",
        reason is not None and "tier" in reason,
        f"reason={reason}"
    )

    # Test 50: Entry with None for 'brief_id' is missing
    print("[Test 50] Entry with None for 'brief_id' is treated as missing")
    ledger = {
        "seated": ["alice"],
        "expected_reactions": [],
        "briefs": {},
        "D1": {"tier": "decided", "brief_id": None, "brief_type": "question", "evidence": []}
    }
    reason = schema.validate_handle_entries(ledger)
    h.test_result(
        "None 'brief_id' is treated as missing",
        reason is not None and "brief_id" in reason,
        f"reason={reason}"
    )

    # Test 51: Multiple handles, returns reason for first missing field in first handle
    print("[Test 51] Multiple handles, returns error for first missing field in first handle")
    ledger = {
        "seated": ["alice"],
        "expected_reactions": [],
        "briefs": {},
        "D1": {"tier": "decided", "brief_id": "B1", "brief_type": "question", "evidence": []},
        "Q1": {"tier": "question", "brief_id": None, "brief_type": "question", "evidence": []}
    }
    reason = schema.validate_handle_entries(ledger)
    h.test_result(
        "Multiple handles with one missing field",
        reason is not None,
        f"reason={reason}"
    )

    # ========== tier_item outputs test ==========

    # Test 52: tier_item outputs are a subset of HANDLE_TIERS
    print("[Test 52] tier_item outputs are subset of HANDLE_TIERS")
    test_cases = [
        ("disagreement", [], [], [], [], "alice", False),
        ("question", ["alice"], [], ["bob"], [], "alice", False),
        ("question", [], ["alice"], ["bob"], [], "alice", False),
        ("question", [], [], ["alice"], ["bob"], "alice", False),
        ("question", [], [], ["alice"], ["bob"], "alice", True),
        ("question", [], [], ["alice"], ["alice"], "alice", False),
    ]
    all_valid = True
    for brief_type, protested, needs_human, resolved, agreed, raiser, could_go_other_way in test_cases:
        tier = schema.tier_item(
            brief_type=brief_type,
            protested_by=protested,
            needs_human_by=needs_human,
            resolved_by=resolved,
            agreed_by=agreed,
            raiser_slug=raiser,
            could_go_other_way=could_go_other_way
        )
        if tier not in schema.HANDLE_TIERS:
            all_valid = False
            break
    h.test_result(
        "tier_item outputs are subset of HANDLE_TIERS",
        all_valid,
        f"all outputs in {schema.HANDLE_TIERS}"
    )

    # Test 53: ACCEPTABLE_TIERS is subset of HANDLE_TIERS
    print("[Test 53] ACCEPTABLE_TIERS is subset of HANDLE_TIERS")
    acceptable_valid = all(tier in schema.HANDLE_TIERS for tier in schema.ACCEPTABLE_TIERS)
    h.test_result(
        "ACCEPTABLE_TIERS is subset of HANDLE_TIERS",
        acceptable_valid,
        f"acceptable={schema.ACCEPTABLE_TIERS}, all={schema.HANDLE_TIERS}"
    )

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
