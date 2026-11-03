#!/usr/bin/env python3
"""
Spec-blind edge-case tests for scripts/flow-verify-receipt.py (#435).

Covers two spec clauses:
- Fix-mode coverage: a bare-basename File cell resolves to a changed path only when exactly one
  changed path ends with that name on a whole path-component boundary; zero or several matches stay
  unresolved/ambiguous (never guessed).
- _verify_unknown_already_done accepts an "unknown" item as already-done only when BOTH the row's
  cited line range overlaps an old-side diff hunk within 3 lines of slack AND a cited anchor is
  script-verified present; otherwise it rejects.

Run with: python3 tests/test_flow_verify_receipt_spec_blind_435.py
"""

import importlib.util
import sys
from pathlib import Path
from typing import Callable, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "flow-verify-receipt.py"

_spec = importlib.util.spec_from_file_location("flow_verify_receipt", SCRIPT)
fvr = importlib.util.module_from_spec(_spec)
sys.modules["flow_verify_receipt"] = fvr
_spec.loader.exec_module(fvr)


def _artifact(
    hunk_start: int = 100,
    hunk_count: int = 5,
    row_start: int = 416,
    row_end: int = 435,
    commits: List[str] = None,
    changed: str = "scripts/workflow/deps.py",
) -> Dict:
    """Build a frozen evidence artifact with one hunk and one row for fix:D1."""
    return {
        "format": 1,
        "available": True,
        "base": "base_sha",
        "tip": "tip_sha",
        "changed_files": [changed],
        "hunks": {changed: [[hunk_start, hunk_count]]},
        "base_line_counts": {changed: 536},
        "tip_line_counts": {changed: 541},
        "commits": commits if commits is not None else ["abc1234def5678"],
        "rows": {
            "fix:D1": [{"path": changed, "start": row_start, "end": row_end}],
        },
    }


# ---------------------------------------------------------------------------
# Basename resolution (fix-mode coverage)
# ---------------------------------------------------------------------------


def case_basename_partial_component_does_not_match() -> Tuple[bool, str]:
    """eps.py is a string suffix of deps.py but not a whole path component: must not resolve."""
    changed_files = {"scripts/workflow/deps.py"}
    status, resolved, candidates = fvr._resolve_file_token("eps.py", changed_files)
    if status != "none" or resolved is not None or candidates:
        return (False, f"eps.py should be none, got {status} {resolved} {candidates}")
    return (True, "")


def case_basename_partial_component_cell_is_unknown() -> Tuple[bool, str]:
    """A Doing-it cell naming eps.py yields no evidence, not deps.py coverage."""
    rows = [fvr.DoingItRow(unit_id="D1", file_cell="eps.py:10-20")]
    changed_files = {"scripts/workflow/deps.py"}
    results = fvr._classify_doing_it_rows(rows, changed_files, {"scripts/workflow/deps.py": 536})
    if results["D1"].status != "unknown":
        return (False, f"Expected unknown, got {results['D1'].status}: {results['D1'].evidence}")
    if "no extractable evidence" not in results["D1"].evidence:
        return (False, f"Expected no extractable evidence, got {results['D1'].evidence}")
    return (True, "")


def case_basename_partial_component_alongside_real_match() -> Tuple[bool, str]:
    """deps.py resolves to scripts/deps.py; the lookalike scripts/xdeps.py does not make it ambiguous."""
    changed_files = {"scripts/deps.py", "scripts/xdeps.py"}
    status, resolved, candidates = fvr._resolve_file_token("deps.py", changed_files)
    if status != "resolved" or resolved != "scripts/deps.py":
        return (False, f"Expected resolved scripts/deps.py, got {status} {resolved} {candidates}")
    return (True, "")


def case_basename_nested_same_name_is_ambiguous() -> Tuple[bool, str]:
    """Two nested paths sharing a basename: ambiguous, never guessed."""
    changed_files = {"a/x/deps.py", "b/y/deps.py"}
    status, resolved, candidates = fvr._resolve_file_token("deps.py", changed_files)
    if status != "ambiguous":
        return (False, f"Expected ambiguous, got {status}")
    if resolved is not None:
        return (False, f"Ambiguous token must not resolve, got {resolved}")
    if sorted(candidates) != ["a/x/deps.py", "b/y/deps.py"]:
        return (False, f"Expected both candidates, got {candidates}")
    return (True, "")


def case_basename_three_way_ambiguous_token_flags_ambiguity() -> Tuple[bool, str]:
    """Three same-basename matches: the Doing-it token is flagged ambiguous with no resolved path."""
    changed_files = {"a/deps.py", "b/c/deps.py", "d/e/f/deps.py"}
    tokens = fvr._doing_it_path_tokens("deps.py:1-5", changed_files)
    if len(tokens) != 1:
        return (False, f"Expected 1 token, got {len(tokens)}")
    if not tokens[0].is_ambiguous or tokens[0].resolved_path is not None:
        return (False, f"Expected ambiguous unresolved token, got {tokens[0]}")
    return (True, "")


def case_basename_three_way_ambiguous_row_is_unknown() -> Tuple[bool, str]:
    """A three-way ambiguous basename cell classifies the row as unknown, not covered."""
    rows = [fvr.DoingItRow(unit_id="D1", file_cell="deps.py:1-5")]
    changed_files = {"a/deps.py", "b/c/deps.py", "d/e/f/deps.py"}
    results = fvr._classify_doing_it_rows(rows, changed_files)
    if results["D1"].status != "unknown":
        return (False, f"Expected unknown, got {results['D1'].status}")
    if "ambiguous" not in results["D1"].evidence.lower():
        return (False, f"Expected ambiguous evidence, got {results['D1'].evidence}")
    return (True, "")


def case_basename_single_nested_match_resolves() -> Tuple[bool, str]:
    """A basename with exactly one nested changed match resolves to that full path."""
    changed_files = {"scripts/workflow/deps.py", "README.md"}
    status, resolved, candidates = fvr._resolve_file_token("deps.py", changed_files)
    if status != "resolved" or resolved != "scripts/workflow/deps.py":
        return (False, f"Expected resolved scripts/workflow/deps.py, got {status} {resolved}")
    if candidates != ["scripts/workflow/deps.py"]:
        return (False, f"Expected single candidate, got {candidates}")
    return (True, "")


def case_basename_zero_matches_cell_is_unknown() -> Tuple[bool, str]:
    """A bare basename with zero changed matches is dropped: row reads unknown, not uncovered or covered."""
    rows = [fvr.DoingItRow(unit_id="D1", file_cell="missing.py:1-5")]
    changed_files = {"scripts/workflow/deps.py"}
    results = fvr._classify_doing_it_rows(rows, changed_files)
    if results["D1"].status != "unknown":
        return (False, f"Expected unknown, got {results['D1'].status}")
    return (True, "")


def case_basename_resolved_cell_covers_row() -> Tuple[bool, str]:
    """A bare basename with exactly one nested changed match covers the row via the resolved path."""
    rows = [fvr.DoingItRow(unit_id="D1", file_cell="deps.py:416-435")]
    changed_files = {"scripts/workflow/deps.py", "README.md"}
    results = fvr._classify_doing_it_rows(rows, changed_files, {"scripts/workflow/deps.py": 536})
    if results["D1"].status != "covered":
        return (False, f"Expected covered, got {results['D1'].status}: {results['D1'].evidence}")
    if "scripts/workflow/deps.py" not in results["D1"].evidence:
        return (False, f"Expected resolved path in evidence, got {results['D1'].evidence}")
    return (True, "")


# ---------------------------------------------------------------------------
# _verify_unknown_already_done: slack boundary
# ---------------------------------------------------------------------------


def case_slack_exact_three_before_hunk_passes() -> Tuple[bool, str]:
    """Row ends exactly 3 lines before the hunk start: distance 3 is within slack, passes."""
    # hunk starts at 100; row end 97 is distance 3 before it.
    artifact = _artifact(hunk_start=100, hunk_count=5, row_start=90, row_end=97)
    passes, reason = fvr._verify_unknown_already_done("fix:D1", "scripts/workflow/deps.py:95", artifact)
    if not passes:
        return (False, f"Expected pass at slack distance 3, got reason {reason}")
    return (True, "")


def case_slack_exact_three_after_hunk_passes() -> Tuple[bool, str]:
    """Row starts exactly 3 lines after the hunk end: distance 3 is within slack, passes."""
    # hunk 100..105; row start 108 is distance 3 past hunk end.
    artifact = _artifact(hunk_start=100, hunk_count=5, row_start=108, row_end=110)
    passes, reason = fvr._verify_unknown_already_done("fix:D1", "scripts/workflow/deps.py:109", artifact)
    if not passes:
        return (False, f"Expected pass at slack distance 3 after hunk, got reason {reason}")
    return (True, "")


def case_slack_four_before_hunk_fails() -> Tuple[bool, str]:
    """Row ends 4 lines before the hunk start: beyond slack, fails range-overlap."""
    # hunk starts at 100; row end 96 is distance 4.
    artifact = _artifact(hunk_start=100, hunk_count=5, row_start=90, row_end=96)
    passes, reason = fvr._verify_unknown_already_done("fix:D1", "scripts/workflow/deps.py:95", artifact)
    if passes:
        return (False, "Expected failure at slack distance 4")
    if reason != "range-overlap":
        return (False, f"Expected range-overlap, got {reason}")
    return (True, "")


def case_slack_four_after_hunk_fails() -> Tuple[bool, str]:
    """Row starts 4 lines after the hunk end: beyond slack, fails range-overlap."""
    # hunk 100..105; row start 109 is distance 4 past hunk end.
    artifact = _artifact(hunk_start=100, hunk_count=5, row_start=109, row_end=112)
    passes, reason = fvr._verify_unknown_already_done("fix:D1", "scripts/workflow/deps.py:110", artifact)
    if passes:
        return (False, "Expected failure at slack distance 4 after hunk")
    if reason != "range-overlap":
        return (False, f"Expected range-overlap, got {reason}")
    return (True, "")


# ---------------------------------------------------------------------------
# _verify_unknown_already_done: both anchors required
# ---------------------------------------------------------------------------


def case_anchor_present_no_overlap_fails_with_sha() -> Tuple[bool, str]:
    """A valid commit SHA anchor does not rescue a row with no hunk overlap."""
    artifact = _artifact(hunk_start=100, hunk_count=5, row_start=300, row_end=310,
                         commits=["abc1234def5678"])
    passes, reason = fvr._verify_unknown_already_done("fix:D1", "see commit abc1234", artifact)
    if passes:
        return (False, "Expected failure: anchor present but no overlap")
    if reason != "range-overlap":
        return (False, f"Expected range-overlap, got {reason}")
    return (True, "")


def case_overlap_but_no_anchor_fails() -> Tuple[bool, str]:
    """Overlapping row with a path cited that is not in the diff: anchor missing, fails."""
    artifact = _artifact(hunk_start=416, hunk_count=5, row_start=416, row_end=435)
    passes, reason = fvr._verify_unknown_already_done("fix:D1", "other/file.py:420", artifact)
    if passes:
        return (False, "Expected failure: overlap but anchor absent")
    if reason != "anchor":
        return (False, f"Expected anchor, got {reason}")
    return (True, "")


def case_overlap_but_anchor_line_past_tip_fails() -> Tuple[bool, str]:
    """Anchor cites a line past the tip end of file: not script-verified, fails."""
    artifact = _artifact(hunk_start=416, hunk_count=5, row_start=416, row_end=435)
    passes, reason = fvr._verify_unknown_already_done("fix:D1", "scripts/workflow/deps.py:999", artifact)
    if passes:
        return (False, "Expected failure: anchor past tip end")
    if reason != "anchor":
        return (False, f"Expected anchor, got {reason}")
    return (True, "")


def case_overlap_but_unknown_sha_fails() -> Tuple[bool, str]:
    """Overlapping row with a SHA that matches no commit in the artifact: fails anchor."""
    artifact = _artifact(hunk_start=416, hunk_count=5, row_start=416, row_end=435,
                         commits=["abc1234def5678"])
    passes, reason = fvr._verify_unknown_already_done("fix:D1", "deadbee", artifact)
    if passes:
        return (False, "Expected failure: unknown SHA")
    if reason != "anchor":
        return (False, f"Expected anchor, got {reason}")
    return (True, "")


def case_overlap_with_ambiguous_basename_anchor_fails() -> Tuple[bool, str]:
    """Overlap plus a basename anchor matching several changed paths: anchor is never guessed, fails."""
    artifact = {
        "format": 1,
        "available": True,
        "base": "base_sha",
        "tip": "tip_sha",
        "changed_files": ["scripts/deps.py", "tests/deps.py"],
        "hunks": {"scripts/deps.py": [[416, 5]]},
        "base_line_counts": {"scripts/deps.py": 536, "tests/deps.py": 100},
        "tip_line_counts": {"scripts/deps.py": 541, "tests/deps.py": 100},
        "commits": [],
        "rows": {
            "fix:D1": [{"path": "scripts/deps.py", "start": 416, "end": 420}],
        },
    }
    passes, reason = fvr._verify_unknown_already_done("fix:D1", "deps.py:418", artifact)
    if passes:
        return (False, "Expected failure: ambiguous basename anchor")
    if reason != "anchor":
        return (False, f"Expected anchor, got {reason}")
    return (True, "")


def case_overlap_with_sha_anchor_passes() -> Tuple[bool, str]:
    """Positive control for the anchor branch: overlap plus a matching SHA prefix passes."""
    artifact = _artifact(hunk_start=416, hunk_count=5, row_start=416, row_end=435,
                         commits=["abc1234def5678"])
    passes, reason = fvr._verify_unknown_already_done("fix:D1", "landed in abc1234", artifact)
    if not passes:
        return (False, f"Expected pass with SHA anchor and overlap, got reason {reason}")
    return (True, "")


CASES: List[Tuple[str, Callable[[], Tuple[bool, str]]]] = [
    ("basename partial component no match", case_basename_partial_component_does_not_match),
    ("basename partial component cell unknown", case_basename_partial_component_cell_is_unknown),
    ("basename partial component alongside real match", case_basename_partial_component_alongside_real_match),
    ("nested same basename ambiguous", case_basename_nested_same_name_is_ambiguous),
    ("three-way ambiguous token flagged", case_basename_three_way_ambiguous_token_flags_ambiguity),
    ("three-way ambiguous row unknown", case_basename_three_way_ambiguous_row_is_unknown),
    ("single nested basename resolves", case_basename_single_nested_match_resolves),
    ("zero-match basename cell unknown", case_basename_zero_matches_cell_is_unknown),
    ("resolved nested basename covers row", case_basename_resolved_cell_covers_row),
    ("slack distance 3 before hunk passes", case_slack_exact_three_before_hunk_passes),
    ("slack distance 3 after hunk passes", case_slack_exact_three_after_hunk_passes),
    ("slack distance 4 before hunk fails", case_slack_four_before_hunk_fails),
    ("slack distance 4 after hunk fails", case_slack_four_after_hunk_fails),
    ("anchor present no overlap fails", case_anchor_present_no_overlap_fails_with_sha),
    ("overlap but no anchor fails", case_overlap_but_no_anchor_fails),
    ("overlap but anchor past tip fails", case_overlap_but_anchor_line_past_tip_fails),
    ("overlap but unknown SHA fails", case_overlap_but_unknown_sha_fails),
    ("overlap with ambiguous basename anchor fails", case_overlap_with_ambiguous_basename_anchor_fails),
    ("overlap with SHA anchor passes", case_overlap_with_sha_anchor_passes),
]


def main() -> None:
    h = Harness("Spec-blind flow-verify-receipt tests (#435)")
    for name, fn in CASES:
        try:
            passed, detail = fn()
        except Exception as e:
            passed, detail = False, f"raised {type(e).__name__}: {e}"
        h.test_result(name, passed, detail)
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
