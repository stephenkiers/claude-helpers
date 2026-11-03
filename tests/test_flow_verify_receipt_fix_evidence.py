#!/usr/bin/env python3
"""
Evidence artifact tests for scripts/flow-verify-receipt.py (#435).

Tests for _verify_unknown_already_done, the evidence artifact structure, and coverage-mode
--evidence-out and --coverage-out support.

Run with: python3 tests/test_flow_verify_receipt_fix_evidence.py
"""

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "flow-verify-receipt.py"

_spec = importlib.util.spec_from_file_location("flow_verify_receipt", SCRIPT)
fvr = importlib.util.module_from_spec(_spec)
sys.modules["flow_verify_receipt"] = fvr
_spec.loader.exec_module(fvr)


def case_verify_unknown_already_done_passes_with_both_anchors() -> Tuple[bool, str]:
    """Rule 5 positive: a fix:D1 unknown with B overlap and evidence anchor → passes."""
    artifact = {
        "format": 1,
        "available": True,
        "base": "base_sha",
        "tip": "tip_sha",
        "changed_files": ["scripts/workflow/deps.py"],
        "hunks": {
            "scripts/workflow/deps.py": [[416, 5]],  # start, count
        },
        "base_line_counts": {"scripts/workflow/deps.py": 536},
        "tip_line_counts": {"scripts/workflow/deps.py": 541},
        "commits": ["commit1", "commit2"],
        "rows": {
            "fix:D1": [
                {"path": "scripts/workflow/deps.py", "start": 416, "end": 435},
            ],
        },
    }
    evidence = "scripts/workflow/deps.py:420"
    passes, reason = fvr._verify_unknown_already_done("fix:D1", evidence, artifact)
    if not passes:
        return (False, f"Expected passes, got reason: {reason}")
    return (True, "")


def case_verify_unknown_already_done_fails_no_range_overlap() -> Tuple[bool, str]:
    """Required negative (Q1 round 2): range does not overlap hunk → fails range-overlap."""
    artifact = {
        "format": 1,
        "available": True,
        "base": "base_sha",
        "tip": "tip_sha",
        "changed_files": ["scripts/workflow/deps.py"],
        "hunks": {
            "scripts/workflow/deps.py": [[100, 5]],  # Hunk at 100-105
        },
        "base_line_counts": {"scripts/workflow/deps.py": 536},
        "tip_line_counts": {"scripts/workflow/deps.py": 541},
        "commits": ["commit1"],
        "rows": {
            "fix:D1": [
                {"path": "scripts/workflow/deps.py", "start": 200, "end": 210},  # No overlap
            ],
        },
    }
    evidence = "scripts/workflow/deps.py:420"
    passes, reason = fvr._verify_unknown_already_done("fix:D1", evidence, artifact)
    if passes:
        return (False, "Expected failure")
    if reason != "range-overlap":
        return (False, f"Expected range-overlap, got {reason}")
    return (True, "")


def case_verify_unknown_already_done_fails_no_anchor() -> Tuple[bool, str]:
    """Prose-only evidence fails anchor check."""
    artifact = {
        "format": 1,
        "available": True,
        "changed_files": ["scripts/workflow/deps.py"],
        "hunks": {
            "scripts/workflow/deps.py": [[416, 5]],
        },
        "base_line_counts": {"scripts/workflow/deps.py": 536},
        "rows": {
            "fix:D1": [
                {"path": "scripts/workflow/deps.py", "start": 416, "end": 435},
            ],
        },
    }
    evidence = "This is prose only, no anchors"
    passes, reason = fvr._verify_unknown_already_done("fix:D1", evidence, artifact)
    if passes:
        return (False, "Expected failure")
    if reason != "anchor":
        return (False, f"Expected anchor, got {reason}")
    return (True, "")


def case_verify_unknown_already_done_fails_missing_artifact() -> Tuple[bool, str]:
    """Missing artifact fails."""
    artifact = {}
    evidence = "scripts/workflow/deps.py:420"
    passes, reason = fvr._verify_unknown_already_done("fix:D1", evidence, artifact)
    if passes:
        return (False, "Expected failure")
    return (True, "")


def case_verify_unknown_already_done_fails_available_false() -> Tuple[bool, str]:
    """available:false artifact fails."""
    artifact = {
        "format": 1,
        "available": False,
        "reason": "git failure",
    }
    evidence = "scripts/workflow/deps.py:420"
    passes, reason = fvr._verify_unknown_already_done("fix:D1", evidence, artifact)
    if passes:
        return (False, "Expected failure")
    if reason != "unavailable":
        return (False, f"Expected unavailable, got {reason}")
    return (True, "")


def case_resolve_file_token_exact_match() -> Tuple[bool, str]:
    """Test 1: Exact match resolves."""
    changed_files = {"scripts/workflow/deps.py", "tests/test.py"}
    status, resolved, candidates = fvr._resolve_file_token("scripts/workflow/deps.py", changed_files)
    if status != "resolved":
        return (False, f"Expected resolved, got {status}")
    if resolved != "scripts/workflow/deps.py":
        return (False, f"Expected scripts/workflow/deps.py, got {resolved}")
    return (True, "")


def case_resolve_file_token_basename_match() -> Tuple[bool, str]:
    """Test 1: Basename match resolves to full path."""
    changed_files = {"scripts/workflow/deps.py", "tests/test.py"}
    status, resolved, candidates = fvr._resolve_file_token("deps.py", changed_files)
    if status != "resolved":
        return (False, f"Expected resolved, got {status}")
    if resolved != "scripts/workflow/deps.py":
        return (False, f"Expected scripts/workflow/deps.py, got {resolved}")
    return (True, "")


def case_resolve_file_token_ambiguous() -> Tuple[bool, str]:
    """Test 2: Multiple matches are ambiguous."""
    changed_files = {"scripts/deps.py", "tests/deps.py"}
    status, resolved, candidates = fvr._resolve_file_token("deps.py", changed_files)
    if status != "ambiguous":
        return (False, f"Expected ambiguous, got {status}")
    if len(candidates) != 2:
        return (False, f"Expected 2 candidates, got {len(candidates)}")
    return (True, "")


def case_resolve_file_token_no_match() -> Tuple[bool, str]:
    """Test 3: No match returns none."""
    changed_files = {"scripts/workflow/deps.py"}
    status, resolved, candidates = fvr._resolve_file_token("missing.py", changed_files)
    if status != "none":
        return (False, f"Expected none, got {status}")
    return (True, "")


def case_resolve_file_token_no_suffix_match_partial() -> Tuple[bool, str]:
    """Test 5: Basename does not match partial component."""
    changed_files = {"scripts/xdeps.py"}
    status, resolved, candidates = fvr._resolve_file_token("deps.py", changed_files)
    if status != "none":
        return (False, f"deps.py should not match xdeps.py: got {status}")
    return (True, "")


def case_resolve_file_token_never_resolve_globs() -> Tuple[bool, str]:
    """Test 5: Glob tokens never resolve."""
    changed_files = {"scripts/workflow/deps.py"}
    for glob_token in ["*.py", "deps*.py", "[abc]deps.py"]:
        status, _, _ = fvr._resolve_file_token(glob_token, changed_files)
        if status != "none":
            return (False, f"Glob token {glob_token} should not resolve, got {status}")
    return (True, "")


def case_classify_doing_it_rows_basename_fixture() -> Tuple[bool, str]:
    """Test 1: Basename fixture reads covered."""
    rows = [
        fvr.DoingItRow(
            unit_id="D1",
            file_cell="deps.py:416-435",
        ),
    ]
    changed_files = {"scripts/workflow/deps.py"}
    base_line_counts = {"scripts/workflow/deps.py": 536}
    results = fvr._classify_doing_it_rows(rows, changed_files, base_line_counts)
    if results["D1"].status != "covered":
        return (False, f"Got {results['D1']}")
    return (True, "")


def case_classify_doing_it_rows_ambiguous_basename() -> Tuple[bool, str]:
    """Test 2: Ambiguous basename reads unknown."""
    rows = [
        fvr.DoingItRow(
            unit_id="D1",
            file_cell="deps.py:416-435",
        ),
    ]
    changed_files = {"scripts/deps.py", "tests/deps.py"}
    results = fvr._classify_doing_it_rows(rows, changed_files)
    if results["D1"].status != "unknown":
        return (False, f"Expected unknown, got {results['D1'].status}")
    if "ambiguous" not in results["D1"].evidence.lower():
        return (False, f"Expected ambiguous in evidence, got {results['D1'].evidence}")
    return (True, "")


def case_classify_doing_it_rows_bound_check_past_end() -> Tuple[bool, str]:
    """Test 6: Range past end of file reads unknown."""
    rows = [
        fvr.DoingItRow(
            unit_id="D1",
            file_cell="deps.py:1023-1028",
        ),
    ]
    changed_files = {"scripts/workflow/deps.py"}
    base_line_counts = {"scripts/workflow/deps.py": 536}
    results = fvr._classify_doing_it_rows(rows, changed_files, base_line_counts)
    if results["D1"].status != "unknown":
        return (False, f"Expected unknown, got {results['D1'].status}")
    if "past end" not in results["D1"].evidence:
        return (False, f"Expected 'past end' in evidence, got {results['D1'].evidence}")
    return (True, "")


def case_doing_it_path_tokens_parses_line_range() -> Tuple[bool, str]:
    """Test that _doing_it_path_tokens parses and preserves line ranges."""
    changed_files = {"scripts/workflow/deps.py"}
    tokens = fvr._doing_it_path_tokens("deps.py:416-435", changed_files)
    if len(tokens) != 1:
        return (False, f"Expected 1 token, got {len(tokens)}")
    if tokens[0].line_range != (416, 435):
        return (False, f"Expected (416, 435), got {tokens[0].line_range}")
    return (True, "")


def case_doing_it_path_tokens_single_line() -> Tuple[bool, str]:
    """Test that _doing_it_path_tokens handles single line numbers."""
    changed_files = {"scripts/workflow/deps.py"}
    tokens = fvr._doing_it_path_tokens("deps.py:420", changed_files)
    if len(tokens) != 1:
        return (False, f"Expected 1 token, got {len(tokens)}")
    if tokens[0].line_range != (420, 420):
        return (False, f"Expected (420, 420), got {tokens[0].line_range}")
    return (True, "")


CASES: List[Tuple[str, Callable[[], Tuple[bool, str]]]] = [
    ("passes with both anchors", case_verify_unknown_already_done_passes_with_both_anchors),
    ("fails on no range overlap", case_verify_unknown_already_done_fails_no_range_overlap),
    ("fails on prose-only evidence", case_verify_unknown_already_done_fails_no_anchor),
    ("fails on missing artifact", case_verify_unknown_already_done_fails_missing_artifact),
    ("fails on available:false", case_verify_unknown_already_done_fails_available_false),
    ("exact match resolves", case_resolve_file_token_exact_match),
    ("basename match resolves", case_resolve_file_token_basename_match),
    ("ambiguous match detected", case_resolve_file_token_ambiguous),
    ("no match returns none", case_resolve_file_token_no_match),
    ("partial component no match", case_resolve_file_token_no_suffix_match_partial),
    ("globs never resolve", case_resolve_file_token_never_resolve_globs),
    ("basename fixture reads covered", case_classify_doing_it_rows_basename_fixture),
    ("ambiguous basename unknown", case_classify_doing_it_rows_ambiguous_basename),
    ("bound check past end", case_classify_doing_it_rows_bound_check_past_end),
    ("parses line range", case_doing_it_path_tokens_parses_line_range),
    ("single line number", case_doing_it_path_tokens_single_line),
]


def main() -> None:
    h = Harness("Evidence artifact tests (#435)")
    for name, fn in CASES:
        try:
            passed, detail = fn()
        except Exception as e:
            passed, detail = False, f"raised {type(e).__name__}: {e}"
        h.test_result(name, passed, detail)
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
