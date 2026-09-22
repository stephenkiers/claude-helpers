#!/usr/bin/env python3
"""
Spec-blind test suite for scripts/reviewer-selection-audit.py functions:
  - load_reviewer_index() error handling for missing contexts
  - parse_inline_flow_map() input validation and error behavior

Per the plan:
1. load_reviewer_index() must raise ValueError when a reviewer entry lacks a contexts field
   (instead of silently defaulting). sys.exit(1) must NOT happen inside the loader itself.

2. parse_inline_flow_map() now:
   - Only accepts dict input (manual string-parsing branch is deleted)
   - Validates keys against {review, plan, write}
   - Validates values against {primary, secondary, named-only}
   - Raises ValueError (not TypeError) on invalid keys, invalid values, or non-string values

Run with: python3 tests/test_load_reviewer_index_and_parse_inline_flow_map.py
"""

import importlib.util
import sys
import tempfile
from pathlib import Path

from _test_harness import REPO_ROOT, Harness

SCRIPT = REPO_ROOT / "scripts" / "reviewer-selection-audit.py"


def _load_module():
    """Load reviewer-selection-audit.py as a module (handles dash in filename)."""
    spec = importlib.util.spec_from_file_location("reviewer_selection_audit", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["reviewer_selection_audit_temp"] = module
    spec.loader.exec_module(module)
    return module


def test_load_reviewer_index_missing_contexts_raises_valueerror(h):
    """load_reviewer_index() raises ValueError when a reviewer lacks contexts field."""
    module = _load_module()

    with tempfile.TemporaryDirectory() as tmp:
        index_file = Path(tmp) / "index-missing-contexts.yaml"
        # Reviewer with no contexts field at all
        index_file.write_text(
            "reviewers:\n"
            "  - name: Test Reviewer\n"
            "    file: test-reviewer.yaml\n"
            "    priority: high\n"
            "    triggers: [test]\n"
        )

        try:
            module.load_reviewer_index(index_file)
            h.test_result(
                "load_reviewer_index raises ValueError for missing contexts",
                False,
                "Expected ValueError but function succeeded"
            )
        except ValueError as e:
            h.test_result(
                "load_reviewer_index raises ValueError for missing contexts",
                True,
                f"got ValueError: {e}"
            )
        except SystemExit as e:
            h.test_result(
                "load_reviewer_index raises ValueError for missing contexts",
                False,
                f"Expected ValueError but got SystemExit({e.code})"
            )
        except Exception as e:
            h.test_result(
                "load_reviewer_index raises ValueError for missing contexts",
                False,
                f"Expected ValueError but got {type(e).__name__}: {e}"
            )


def test_parse_inline_flow_map_accepts_dict(h):
    """parse_inline_flow_map() accepts dict input."""
    module = _load_module()

    valid_flow_map = {"review": "primary", "plan": "secondary"}

    try:
        result = module.parse_inline_flow_map(valid_flow_map)
        h.test_result(
            "parse_inline_flow_map accepts dict input",
            result == valid_flow_map,
            f"got result: {result}"
        )
    except Exception as e:
        h.test_result(
            "parse_inline_flow_map accepts dict input",
            False,
            f"Expected success but got {type(e).__name__}: {e}"
        )


def test_parse_inline_flow_map_rejects_string_input(h):
    """parse_inline_flow_map() rejects string input (no manual parsing)."""
    module = _load_module()

    # This should no longer work - the manual string-parsing branch was deleted
    string_flow_map = "review: primary, plan: secondary"

    try:
        result = module.parse_inline_flow_map(string_flow_map)
        h.test_result(
            "parse_inline_flow_map rejects string input",
            False,
            f"Expected ValueError/TypeError but got result: {result}"
        )
    except (ValueError, TypeError) as e:
        h.test_result(
            "parse_inline_flow_map rejects string input",
            True,
            f"correctly rejected string with {type(e).__name__}: {e}"
        )
    except Exception as e:
        h.test_result(
            "parse_inline_flow_map rejects string input",
            False,
            f"Expected ValueError/TypeError but got {type(e).__name__}: {e}"
        )


def test_parse_inline_flow_map_validates_keys(h):
    """parse_inline_flow_map() validates keys against {review, plan, write}."""
    module = _load_module()

    invalid_key_map = {"review": "primary", "invalid_key": "primary"}

    try:
        result = module.parse_inline_flow_map(invalid_key_map)
        h.test_result(
            "parse_inline_flow_map validates keys",
            False,
            f"Expected ValueError but got result: {result}"
        )
    except ValueError as e:
        h.test_result(
            "parse_inline_flow_map validates keys",
            True,
            f"correctly raised ValueError: {e}"
        )
    except Exception as e:
        h.test_result(
            "parse_inline_flow_map validates keys",
            False,
            f"Expected ValueError but got {type(e).__name__}: {e}"
        )


def test_parse_inline_flow_map_validates_values(h):
    """parse_inline_flow_map() validates values against {primary, secondary, named-only}."""
    module = _load_module()

    invalid_value_map = {"review": "invalid_value"}

    try:
        result = module.parse_inline_flow_map(invalid_value_map)
        h.test_result(
            "parse_inline_flow_map validates values",
            False,
            f"Expected ValueError but got result: {result}"
        )
    except ValueError as e:
        h.test_result(
            "parse_inline_flow_map validates values",
            True,
            f"correctly raised ValueError: {e}"
        )
    except Exception as e:
        h.test_result(
            "parse_inline_flow_map validates values",
            False,
            f"Expected ValueError but got {type(e).__name__}: {e}"
        )


def test_parse_inline_flow_map_non_string_value_raises_valueerror(h):
    """parse_inline_flow_map() raises ValueError (not TypeError) on non-string values."""
    module = _load_module()

    non_string_value_map = {"review": 123}

    try:
        result = module.parse_inline_flow_map(non_string_value_map)
        h.test_result(
            "parse_inline_flow_map raises ValueError on non-string values",
            False,
            f"Expected ValueError but got result: {result}"
        )
    except ValueError as e:
        h.test_result(
            "parse_inline_flow_map raises ValueError on non-string values",
            True,
            f"correctly raised ValueError: {e}"
        )
    except TypeError as e:
        h.test_result(
            "parse_inline_flow_map raises ValueError on non-string values",
            False,
            f"Expected ValueError but got TypeError: {e}"
        )
    except Exception as e:
        h.test_result(
            "parse_inline_flow_map raises ValueError on non-string values",
            False,
            f"Expected ValueError but got {type(e).__name__}: {e}"
        )


def test_parse_inline_flow_map_unhashable_value_raises_valueerror(h):
    """parse_inline_flow_map() raises ValueError (not TypeError) on an unhashable value.

    A hashable-but-wrong-type value (e.g. an int) can't distinguish the fixed behavior
    from the old code: both old and new code test `val not in VALID_CONTEXT_VALUES`,
    which raises ValueError for any hashable non-match. An *unhashable* value (list,
    dict) is the case the fix actually changes: old code's bare `val not in
    valid_values` raises an uncaught TypeError on an unhashable val, while new code's
    `not isinstance(val, str) or ...` short-circuits before the `in` check ever runs.
    """
    module = _load_module()

    unhashable_value_map = {"review": ["not", "a", "string"]}

    try:
        result = module.parse_inline_flow_map(unhashable_value_map)
        h.test_result(
            "parse_inline_flow_map raises ValueError on an unhashable value",
            False,
            f"Expected ValueError but got result: {result}"
        )
    except ValueError as e:
        h.test_result(
            "parse_inline_flow_map raises ValueError on an unhashable value",
            True,
            f"correctly raised ValueError: {e}"
        )
    except TypeError as e:
        h.test_result(
            "parse_inline_flow_map raises ValueError on an unhashable value",
            False,
            f"Expected ValueError but got TypeError: {e}"
        )
    except Exception as e:
        h.test_result(
            "parse_inline_flow_map raises ValueError on an unhashable value",
            False,
            f"Expected ValueError but got {type(e).__name__}: {e}"
        )


def test_parse_inline_flow_map_all_valid_keys_and_values(h):
    """parse_inline_flow_map() accepts all valid key-value combinations."""
    module = _load_module()

    # Test a few valid combinations
    test_cases = [
        {"review": "primary"},
        {"plan": "secondary"},
        {"write": "named-only"},
        {"review": "primary", "plan": "secondary", "write": "named-only"},
    ]

    all_passed = True
    for test_case in test_cases:
        try:
            result = module.parse_inline_flow_map(test_case)
            if result != test_case:
                all_passed = False
        except Exception:
            all_passed = False

    h.test_result(
        "parse_inline_flow_map accepts all valid key-value combinations",
        all_passed
    )


def test_parse_inline_flow_map_rejects_non_dict_list(h):
    """parse_inline_flow_map() rejects list input."""
    module = _load_module()

    list_input = ["review", "primary"]

    try:
        result = module.parse_inline_flow_map(list_input)
        h.test_result(
            "parse_inline_flow_map rejects list input",
            False,
            f"Expected ValueError/TypeError but got result: {result}"
        )
    except (ValueError, TypeError) as e:
        h.test_result(
            "parse_inline_flow_map rejects list input",
            True,
            f"correctly rejected list with {type(e).__name__}"
        )
    except Exception as e:
        h.test_result(
            "parse_inline_flow_map rejects list input",
            False,
            f"Expected ValueError/TypeError but got {type(e).__name__}: {e}"
        )


def main():
    h = Harness("LOAD_REVIEWER_INDEX AND PARSE_INLINE_FLOW_MAP TEST SUITE")

    test_load_reviewer_index_missing_contexts_raises_valueerror(h)
    test_parse_inline_flow_map_accepts_dict(h)
    test_parse_inline_flow_map_rejects_string_input(h)
    test_parse_inline_flow_map_validates_keys(h)
    test_parse_inline_flow_map_validates_values(h)
    test_parse_inline_flow_map_non_string_value_raises_valueerror(h)
    test_parse_inline_flow_map_unhashable_value_raises_valueerror(h)
    test_parse_inline_flow_map_all_valid_keys_and_values(h)
    test_parse_inline_flow_map_rejects_non_dict_list(h)

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
