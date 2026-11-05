#!/usr/bin/env python3
"""
Evidence artifact tests for scripts/flow-verify-receipt.py (#435).

Tests for _verify_unknown_already_done, the evidence artifact structure, and coverage-mode
--evidence-out and --coverage-out support.

Run with: python3 tests/test_flow_verify_receipt_fix_evidence.py
"""

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "flow-verify-receipt.py"

_spec = importlib.util.spec_from_file_location("flow_verify_receipt", SCRIPT)
fvr = importlib.util.module_from_spec(_spec)
sys.modules["flow_verify_receipt"] = fvr
_spec.loader.exec_module(fvr)

# The persisted fix coverage table that every artifact binds to (coverage_sha256). Its content is not
# read by _verify_unknown_already_done; only its bytes matter for the sha binding.
_COV_TMP = tempfile.TemporaryDirectory()
COVERAGE_FILE = Path(_COV_TMP.name) / "05-fix.coverage.txt"
COVERAGE_FILE.write_text("UNITS: 1\nD1 | unknown | fixture", encoding="utf-8")
COVERAGE_SHA = hashlib.sha256(COVERAGE_FILE.read_bytes()).hexdigest()


def _verify(unit_id: str, evidence: str, artifact: Dict,
            coverage_file: Optional[Path] = COVERAGE_FILE) -> Tuple[bool, str]:
    """Call _verify_unknown_already_done with the bound coverage file."""
    return fvr._verify_unknown_already_done(unit_id, evidence, artifact, coverage_file)


def case_verify_unknown_already_done_passes_with_both_anchors() -> Tuple[bool, str]:
    """Rule 5 positive: a fix:D1 unknown with B overlap and evidence anchor → passes."""
    artifact = {
        "format": 1,
        "available": True,
        "base": "base_sha",
        "tip": "tip_sha",
        "coverage_sha256": COVERAGE_SHA,
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
    passes, reason = _verify("fix:D1", evidence, artifact)
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
        "coverage_sha256": COVERAGE_SHA,
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
    passes, reason = _verify("fix:D1", evidence, artifact)
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
        "coverage_sha256": COVERAGE_SHA,
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
    passes, reason = _verify("fix:D1", evidence, artifact)
    if passes:
        return (False, "Expected failure")
    if reason != "anchor":
        return (False, f"Expected anchor, got {reason}")
    return (True, "")


def case_verify_unknown_already_done_fails_missing_artifact() -> Tuple[bool, str]:
    """Missing artifact fails."""
    artifact: Dict = {}
    evidence = "scripts/workflow/deps.py:420"
    passes, reason = _verify("fix:D1", evidence, artifact)
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
    passes, reason = _verify("fix:D1", evidence, artifact)
    if passes:
        return (False, "Expected failure")
    if reason != "unavailable":
        return (False, f"Expected unavailable, got {reason}")
    return (True, "")


# ---------------------------------------------------------------------------
# Coverage binding (coverage_sha256 against the persisted coverage file)
# ---------------------------------------------------------------------------


def _passing_artifact() -> Dict:
    """A structurally valid artifact that would pass with a correct binding."""
    return {
        "format": 1,
        "available": True,
        "coverage_sha256": COVERAGE_SHA,
        "changed_files": ["scripts/workflow/deps.py"],
        "hunks": {"scripts/workflow/deps.py": [[416, 5]]},
        "base_line_counts": {"scripts/workflow/deps.py": 536},
        "tip_line_counts": {"scripts/workflow/deps.py": 541},
        "commits": [],
        "rows": {"fix:D1": [{"path": "scripts/workflow/deps.py", "start": 416, "end": 435}]},
    }


def case_binding_control_passes() -> Tuple[bool, str]:
    """Control: the valid artifact with its bound coverage file passes."""
    passes, reason = _verify("fix:D1", "scripts/workflow/deps.py:420", _passing_artifact())
    if not passes:
        return (False, f"control should pass, got {reason}")
    return (True, "")


def case_binding_missing_sha_is_unavailable() -> Tuple[bool, str]:
    """An artifact without coverage_sha256 cannot be bound to a coverage table: unavailable."""
    artifact = _passing_artifact()
    del artifact["coverage_sha256"]
    passes, reason = _verify("fix:D1", "scripts/workflow/deps.py:420", artifact)
    if passes or reason != "unavailable":
        return (False, f"Expected (False, unavailable), got ({passes}, {reason})")
    return (True, "")


def case_binding_empty_sha_is_unavailable() -> Tuple[bool, str]:
    """coverage_mode writes "" when no --coverage-out was given: not a binding, unavailable."""
    artifact = _passing_artifact()
    artifact["coverage_sha256"] = ""
    passes, reason = _verify("fix:D1", "scripts/workflow/deps.py:420", artifact)
    if passes or reason != "unavailable":
        return (False, f"Expected (False, unavailable), got ({passes}, {reason})")
    return (True, "")


def case_binding_sha_mismatch_is_unavailable() -> Tuple[bool, str]:
    """The artifact names a different coverage table than the one on disk: unavailable."""
    artifact = _passing_artifact()
    artifact["coverage_sha256"] = hashlib.sha256(b"some other table").hexdigest()
    passes, reason = _verify("fix:D1", "scripts/workflow/deps.py:420", artifact)
    if passes or reason != "unavailable":
        return (False, f"Expected (False, unavailable), got ({passes}, {reason})")
    return (True, "")


def case_binding_no_coverage_file_is_unavailable() -> Tuple[bool, str]:
    """With no coverage file reachable the binding cannot be checked: unavailable, even with a sha."""
    passes, reason = _verify("fix:D1", "scripts/workflow/deps.py:420", _passing_artifact(), coverage_file=None)
    if passes or reason != "unavailable":
        return (False, f"Expected (False, unavailable), got ({passes}, {reason})")
    return (True, "")


def case_binding_missing_coverage_path_is_unavailable() -> Tuple[bool, str]:
    """A coverage path that does not exist cannot be hashed: unavailable."""
    missing = Path(_COV_TMP.name) / "does-not-exist.txt"
    passes, reason = _verify("fix:D1", "scripts/workflow/deps.py:420", _passing_artifact(), coverage_file=missing)
    if passes or reason != "unavailable":
        return (False, f"Expected (False, unavailable), got ({passes}, {reason})")
    return (True, "")


# ---------------------------------------------------------------------------
# Malformed artifacts: unavailable, never an exception
# ---------------------------------------------------------------------------


def _malformed_cases() -> List[Tuple[str, Dict]]:
    """Each artifact is bound (valid sha) but has one malformed field."""
    cases: List[Tuple[str, Dict]] = []

    def with_change(label: str, **changes: object) -> None:
        artifact = _passing_artifact()
        artifact.update(changes)
        cases.append((label, artifact))

    with_change("rows is a list", rows=[])
    with_change("row entry is not a list", rows={"fix:D1": {"path": "scripts/workflow/deps.py"}})
    with_change("row item is not a dict", rows={"fix:D1": ["scripts/workflow/deps.py:420"]})
    with_change("start is a string", rows={"fix:D1": [{"path": "scripts/workflow/deps.py", "start": "416", "end": 435}]})
    with_change("end is a bool", rows={"fix:D1": [{"path": "scripts/workflow/deps.py", "start": 416, "end": True}]})
    with_change("path is not a string", rows={"fix:D1": [{"path": 7, "start": 416, "end": 435}]})
    with_change("hunks is a list", hunks=[[416, 5]])
    with_change("hunk is not a pair", hunks={"scripts/workflow/deps.py": [[416]]})
    with_change("hunk holds a float", hunks={"scripts/workflow/deps.py": [[416.0, 5]]})
    with_change("changed_files is a string", changed_files="scripts/workflow/deps.py")
    with_change("base_line_counts is a list", base_line_counts=[536])
    with_change("tip_line_counts value is a string", tip_line_counts={"scripts/workflow/deps.py": "541"})
    with_change("commits holds an int", commits=[123])
    return cases


def case_malformed_artifacts_are_unavailable() -> Tuple[bool, str]:
    """Every malformed field returns (False, unavailable) and never raises."""
    for label, artifact in _malformed_cases():
        try:
            passes, reason = _verify("fix:D1", "scripts/workflow/deps.py:420", artifact)
        except Exception as e:  # any raise is a failure
            return (False, f"{label}: raised {type(e).__name__}: {e}")
        if passes or reason != "unavailable":
            return (False, f"{label}: expected (False, unavailable), got ({passes}, {reason})")
    return (True, "")


# ---------------------------------------------------------------------------
# File-token resolution
# ---------------------------------------------------------------------------


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


def case_resolve_file_token_bare_root_and_nested_is_ambiguous() -> Tuple[bool, str]:
    """A bare token equal to a root changed path and also a nested match is ambiguous, not the root file."""
    changed_files = {"deps.py", "scripts/workflow/deps.py"}
    status, resolved, candidates = fvr._resolve_file_token("deps.py", changed_files)
    if status != "ambiguous" or resolved is not None:
        return (False, f"Expected ambiguous with no resolved path, got {status} {resolved}")
    if sorted(candidates) != ["deps.py", "scripts/workflow/deps.py"]:
        return (False, f"Expected both candidates, got {candidates}")
    return (True, "")


def case_resolve_file_token_bare_single_exact_resolves() -> Tuple[bool, str]:
    """A bare token that is the only changed path with that name resolves to it."""
    changed_files = {"deps.py", "tests/other.py"}
    status, resolved, _ = fvr._resolve_file_token("deps.py", changed_files)
    if status != "resolved" or resolved != "deps.py":
        return (False, f"Expected resolved deps.py, got {status} {resolved}")
    return (True, "")


def case_resolve_file_token_qualified_exact_match_wins() -> Tuple[bool, str]:
    """A path-qualified token prefers its exact match over a longer suffix match."""
    changed_files = {"scripts/deps.py", "x/scripts/deps.py"}
    status, resolved, _ = fvr._resolve_file_token("scripts/deps.py", changed_files)
    if status != "resolved" or resolved != "scripts/deps.py":
        return (False, f"Expected exact scripts/deps.py, got {status} {resolved}")
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


# ---------------------------------------------------------------------------
# Line-range parsing: ASCII digits only, no raise, line 0 and inverted ranges are no range
# ---------------------------------------------------------------------------


def case_line_range_superscript_digits_are_no_range() -> Tuple[bool, str]:
    """Superscript digits pass str.isdigit() but are not ASCII: no range, no exception."""
    changed_files = {"scripts/workflow/deps.py"}
    for cell in ["deps.py:²", "deps.py:1¹-2²"]:
        try:
            tokens = fvr._doing_it_path_tokens(cell, changed_files)
        except Exception as e:
            return (False, f"{cell!r} raised {type(e).__name__}: {e}")
        if len(tokens) != 1 or tokens[0].line_range is not None:
            return (False, f"{cell!r}: expected one token with no range, got {tokens}")
    return (True, "")


def case_line_range_huge_number_is_no_range() -> Tuple[bool, str]:
    """A 5000-digit number is not a line number: no range, no exception."""
    changed_files = {"scripts/workflow/deps.py"}
    huge = "9" * 5000
    for cell in [f"deps.py:{huge}", f"deps.py:1-{huge}", f"deps.py:{huge}-{huge}"]:
        try:
            tokens = fvr._doing_it_path_tokens(cell, changed_files)
        except Exception as e:
            return (False, f"raised {type(e).__name__}: {e}")
        if len(tokens) != 1 or tokens[0].line_range is not None:
            return (False, f"expected one token with no range, got {tokens}")
    return (True, "")


def case_line_range_zero_and_inverted_are_no_range() -> Tuple[bool, str]:
    """Line 0 and start > end are not ranges."""
    changed_files = {"scripts/workflow/deps.py"}
    for cell in ["deps.py:0", "deps.py:0-5", "deps.py:9-3", "deps.py:0-0"]:
        tokens = fvr._doing_it_path_tokens(cell, changed_files)
        if len(tokens) != 1 or tokens[0].line_range is not None:
            return (False, f"{cell!r}: expected no range, got {tokens}")
    return (True, "")


def case_line_range_valid_forms_still_parse() -> Tuple[bool, str]:
    """Plain and dashed ranges, including the 9-digit limit, still parse."""
    changed_files = {"scripts/workflow/deps.py"}
    want = {
        "deps.py:7": (7, 7),
        "deps.py:3-7": (3, 7),
        "deps.py:5-5": (5, 5),
        "deps.py:999999999": (999999999, 999999999),
    }
    for cell, expected in want.items():
        tokens = fvr._doing_it_path_tokens(cell, changed_files)
        if len(tokens) != 1 or tokens[0].line_range != expected:
            return (False, f"{cell!r}: expected {expected}, got {tokens}")
    return (True, "")


# ---------------------------------------------------------------------------
# Integration: real git repo through coverage_mode (unknown row ranges reach the artifact)
# ---------------------------------------------------------------------------

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")

ACTION_PLAN = """# Action plan

## Doing it: 2

| Item | File |
|------|------|
| one | deps.py:10-12 |
| two | scripts/workflow/deps.py |

## Deferred: 0
"""


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                            env=GIT_ENV, check=True)
    return result.stdout.strip()


def _make_fix_fixture(tmp: Path) -> Tuple[Path, str, Path, Path]:
    """Repo with scripts/workflow/deps.py changed at lines 10-12; returns (repo, base_sha, plan, flow_dir)."""
    repo = tmp / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    target = repo / "scripts" / "workflow" / "deps.py"
    target.parent.mkdir(parents=True)
    target.write_text("".join(f"line {n}\n" for n in range(1, 31)), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    lines = [f"line {n}\n" for n in range(1, 31)]
    for n in (10, 11, 12):
        lines[n - 1] = f"changed {n}\n"
    target.write_text("".join(lines), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")

    plan = tmp / "claude-action-plan.md"
    plan.write_text(ACTION_PLAN, encoding="utf-8")
    flow_dir = tmp / "flow"
    (flow_dir / "steps").mkdir(parents=True)
    return repo, base, plan, flow_dir


def _run_coverage(repo: Path, base: str, plan: Path, receipt: Path,
                  coverage_out: Path, evidence_out: Path) -> None:
    """Run coverage_mode(fix) in-process; capture stdout and swallow its exit."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            fvr.coverage_mode("fix", str(plan), str(receipt), str(repo), "HEAD", base,
                              coverage_out=coverage_out, evidence_out=evidence_out)
        except SystemExit as e:
            if e.code not in (0, None):
                raise


def case_unknown_row_range_reaches_artifact_and_verifies() -> Tuple[bool, str]:
    """A shared-file (unknown) row keeps its resolved range in the artifact, and verifies end to end."""
    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        repo, base, plan, flow_dir = _make_fix_fixture(tmp)
        receipt = flow_dir / "steps" / "05-fix.md"
        receipt.write_text("", encoding="utf-8")
        coverage_out = flow_dir / "steps" / "05-fix.coverage.txt"
        evidence_out = flow_dir / "steps" / "05-fix.evidence.json"
        _run_coverage(repo, base, plan, receipt, coverage_out, evidence_out)

        coverage_text = coverage_out.read_text(encoding="utf-8")
        if "D1 | unknown |" not in coverage_text:
            return (False, f"D1 should be unknown (shared file), coverage was:\n{coverage_text}")
        artifact = json.loads(evidence_out.read_text(encoding="utf-8"))
        if artifact.get("available") is False:
            return (False, f"artifact unavailable: {artifact}")
        rows = artifact.get("rows", {})
        want = [{"path": "scripts/workflow/deps.py", "start": 10, "end": 12}]
        if rows.get("fix:D1") != want:
            return (False, f"unknown row fix:D1 should carry its range {want}, rows were {rows}")
        if "fix:D2" in rows:
            return (False, f"row with no range must not appear in rows: {rows}")
        passes, reason = fvr._verify_unknown_already_done(
            "fix:D1", "scripts/workflow/deps.py:11", artifact, coverage_out)
        if not passes:
            return (False, f"unknown row should verify with overlap and anchor, got {reason}")
        passes, reason = fvr._verify_unknown_already_done(
            "fix:D1", "scripts/workflow/deps.py:40", artifact, coverage_out)
        if passes or reason != "anchor":
            return (False, f"anchor outside the row range must fail, got ({passes}, {reason})")
    return (True, "")


def case_uncovered_first_row_does_not_leak_ranges() -> Tuple[bool, str]:
    """A not-in-diff first row writes both files, and its ranges are not attributed to the later rows."""
    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        repo, base, plan, flow_dir = _make_fix_fixture(tmp)
        plan.write_text(
            "# Action plan\n\n## Doing it: 2\n\n| Item | File |\n|------|------|\n"
            "| one | other/missing.py:2-3 |\n| two | deps.py:10-12 |\n\n## Deferred: 0\n",
            encoding="utf-8")
        receipt = flow_dir / "steps" / "05-fix.md"
        receipt.write_text("", encoding="utf-8")
        coverage_out = flow_dir / "steps" / "05-fix.coverage.txt"
        evidence_out = flow_dir / "steps" / "05-fix.evidence.json"
        _run_coverage(repo, base, plan, receipt, coverage_out, evidence_out)
        if not evidence_out.exists():
            return (False, "evidence artifact was not written")
        rows = json.loads(evidence_out.read_text(encoding="utf-8")).get("rows", {})
        if "fix:D1" in rows:
            return (False, f"uncovered first row must not carry ranges: {rows}")
        want = [{"path": "scripts/workflow/deps.py", "start": 10, "end": 12}]
        if rows.get("fix:D2") != want:
            return (False, f"fix:D2 should carry only its own range {want}, rows were {rows}")
    return (True, "")


def case_match_outstanding_issue_runs_without_step_or_receipt() -> Tuple[bool, str]:
    """The documented invocation (no --step/--receipt) works, and a match inside a truncated page is still found."""
    args = ["--match-outstanding-issue", "--flow-id", "F1", "--item-id", "fix:D1", "--limit", "1"]
    hit = json.dumps([{"url": "https://example.invalid/issues/1", "body": "flow-outstanding: F1 fix:D1"}])
    proc = _run_script_stdin(args, hit)
    if proc.returncode != 0 or proc.stdout.strip() != "https://example.invalid/issues/1":
        return (False, f"match in a full page should print the URL and exit 0: rc={proc.returncode} "
                       f"out={proc.stdout!r} err={proc.stderr!r}")
    miss = json.dumps([{"url": "u1", "body": "x"}])
    proc = _run_script_stdin(args, miss)
    if proc.returncode != 3:
        return (False, f"no match in a full page should exit 3, got rc={proc.returncode} err={proc.stderr!r}")
    return (True, "")


def case_range_end_at_tip_line_count_is_in_bounds() -> Tuple[bool, str]:
    """A range ending exactly on the last line of the file is in bounds; one line past it is not."""
    base = {"hunks": {"a.py": [[8, 2]]}, "tip_line_counts": {"a.py": 10}}
    at_end = {**base, "rows": {"fix:D1": [{"path": "a.py", "start": 8, "end": 10}]}}
    past_end = {**base, "rows": {"fix:D1": [{"path": "a.py", "start": 8, "end": 11}]}}
    got = fvr._check_range_overlap("fix:D1", at_end)
    if got.failure is not None:
        return (False, f"range ending at the tip line count should overlap, got failure {got.failure!r}")
    got = fvr._check_range_overlap("fix:D1", past_end)
    if got.failure != "range-overlap":
        return (False, f"range one past the tip line count should fail range-overlap, got {got.failure!r}")
    return (True, "")


def _run_script(args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)


def case_coverage_out_outside_steps_is_refused() -> Tuple[bool, str]:
    """With --flow-dir, --coverage-out and --evidence-out must live under <flow_dir>/steps/."""
    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        repo, base, plan, flow_dir = _make_fix_fixture(tmp)
        receipt = flow_dir / "steps" / "05-fix.md"
        receipt.write_text("", encoding="utf-8")
        common = ["--step", "fix", "--coverage", "--plan", str(plan), "--receipt", str(receipt),
                  "--repo", str(repo), "--branch", "HEAD", "--base", base, "--flow-dir", str(flow_dir)]

        outside = tmp / "elsewhere" / "coverage.txt"
        outside.parent.mkdir()
        proc = _run_script(common + ["--coverage-out", str(outside)])
        if proc.returncode == 0 or outside.exists():
            return (False, f"coverage-out outside steps/ should be refused: rc={proc.returncode}")

        escape = flow_dir / "steps" / ".." / "escaped-evidence.json"
        proc = _run_script(common + ["--evidence-out", str(escape)])
        if proc.returncode == 0 or (flow_dir / "escaped-evidence.json").exists():
            return (False, f"path escaping steps/ via .. should be refused: rc={proc.returncode}")

        proc = _run_script(common + ["--coverage-out", str(flow_dir / "steps" / "05-fix.coverage.txt"),
                                     "--evidence-out", str(flow_dir / "steps" / "05-fix.evidence.json")])
        if proc.returncode != 0 or not (flow_dir / "steps" / "05-fix.evidence.json").exists():
            return (False, f"paths under steps/ should be accepted: rc={proc.returncode} {proc.stderr}")
    return (True, "")


EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def case_tip_is_full_sha_of_resolved_branch() -> Tuple[bool, str]:
    """D7: the artifact's tip is the 40-hex SHA the branch resolves to, not the branch name."""
    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        repo, base, plan, flow_dir = _make_fix_fixture(tmp)
        receipt = flow_dir / "steps" / "05-fix.md"
        receipt.write_text("", encoding="utf-8")
        coverage_out = flow_dir / "steps" / "05-fix.coverage.txt"
        evidence_out = flow_dir / "steps" / "05-fix.evidence.json"
        _run_coverage(repo, base, plan, receipt, coverage_out, evidence_out)
        artifact = json.loads(evidence_out.read_text(encoding="utf-8"))
        head = _git(repo, "rev-parse", "HEAD")
        tip = artifact.get("tip")
        if tip != head:
            return (False, f"tip should be the resolved SHA {head}, got {tip!r}")
        if not (isinstance(tip, str) and len(tip) == 40 and all(c in "0123456789abcdef" for c in tip)):
            return (False, f"tip should be a 40-hex SHA, got {tip!r}")
    return (True, "")


def case_non_ancestor_base_is_unavailable() -> Tuple[bool, str]:
    """D15: a base that is not an ancestor of the tip (an orphan commit) yields an unavailable artifact."""
    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        repo, _base, plan, flow_dir = _make_fix_fixture(tmp)
        orphan = _git(repo, "commit-tree", EMPTY_TREE, "-m", "orphan base")
        receipt = flow_dir / "steps" / "05-fix.md"
        receipt.write_text("", encoding="utf-8")
        coverage_out = flow_dir / "steps" / "05-fix.coverage.txt"
        evidence_out = flow_dir / "steps" / "05-fix.evidence.json"
        _run_coverage(repo, orphan, plan, receipt, coverage_out, evidence_out)
        artifact = json.loads(evidence_out.read_text(encoding="utf-8"))
        if artifact.get("available") is not False:
            return (False, f"non-ancestor base should be unavailable, got {artifact}")
        if artifact.get("reason") != "base not ancestor of tip":
            return (False, f"expected reason 'base not ancestor of tip', got {artifact.get('reason')!r}")
    return (True, "")


def case_failing_hunk_diff_gives_range_overlap() -> Tuple[bool, str]:
    """A per-file `git diff -U0` that fails leaves no hunks for that file, so a row there is range-overlap."""
    real_git_out = fvr._git_out

    def failing_hunk_diff(repo: str, args: List[str]) -> Tuple[int, str]:
        if args[:1] == ["diff"] and "-U0" in args:
            return (1, "")
        return real_git_out(repo, args)

    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        repo, base, plan, flow_dir = _make_fix_fixture(tmp)
        receipt = flow_dir / "steps" / "05-fix.md"
        receipt.write_text("", encoding="utf-8")
        coverage_out = flow_dir / "steps" / "05-fix.coverage.txt"
        evidence_out = flow_dir / "steps" / "05-fix.evidence.json"
        fvr._git_out = failing_hunk_diff
        try:
            _run_coverage(repo, base, plan, receipt, coverage_out, evidence_out)
        finally:
            fvr._git_out = real_git_out
        artifact = json.loads(evidence_out.read_text(encoding="utf-8"))
        if artifact.get("hunks") != {}:
            return (False, f"failed hunk diff should leave no hunks, got {artifact.get('hunks')}")
        passes, reason = fvr._verify_unknown_already_done(
            "fix:D1", "scripts/workflow/deps.py:11", artifact, coverage_out)
        if passes or reason != "range-overlap":
            return (False, f"expected (False, range-overlap), got ({passes}, {reason})")
    return (True, "")


def _run_script_stdin(args: List[str], stdin_text: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], input=stdin_text, capture_output=True, text=True)


def _match_args(receipt: Path, item_id: str, limit: str) -> List[str]:
    return ["--step", "fix", "--receipt", str(receipt), "--match-outstanding-issue",
            "--flow-id", "F1", "--item-id", item_id, "--limit", limit]


def case_match_outstanding_issue_exit_codes() -> Tuple[bool, str]:
    """D13: match prints the URL and exits 0; a full page (count reaching the limit) exits 3; bad input exits 2."""
    with tempfile.TemporaryDirectory() as tmp_s:
        receipt = Path(tmp_s) / "receipt.md"
        receipt.write_text("", encoding="utf-8")
        hit = json.dumps([{"url": "https://example.invalid/issues/1", "body": "flow-outstanding: F1 fix:D1"}])
        proc = _run_script_stdin(_match_args(receipt, "fix:D1", "5"), hit)
        if proc.returncode != 0 or proc.stdout.strip() != "https://example.invalid/issues/1":
            return (False, f"match should print the URL and exit 0: rc={proc.returncode} out={proc.stdout!r}")
        proc = _run_script_stdin(_match_args(receipt, "fix:D2", "5"), hit)
        if proc.returncode != 0 or proc.stdout.strip():
            return (False, f"no match should exit 0 with no output: rc={proc.returncode} out={proc.stdout!r}")
        two = json.dumps([{"url": "u1", "body": "x"}, {"url": "u2", "body": "y"}])
        proc = _run_script_stdin(_match_args(receipt, "fix:D1", "2"), two)
        if proc.returncode != 3:
            return (False, f"a page that reaches the limit should exit 3, got rc={proc.returncode}")
        proc = _run_script_stdin(_match_args(receipt, "fix:D1", "5"), "not json")
        if proc.returncode != 2:
            return (False, f"unparseable JSON should exit 2, got rc={proc.returncode}")
        proc = _run_script_stdin(_match_args(receipt, "fix:D1|x", "5"), "[]")
        if proc.returncode != 2:
            return (False, f"forbidden '|' in the unit should exit 2, got rc={proc.returncode}")
    return (True, "")


def case_assess_anchors_reports_unavailable_without_artifact() -> Tuple[bool, str]:
    """D13: --assess-anchors with an unknown fix row and no evidence artifact prints unavailable and exits 0."""
    from _assess_fixtures import TS, make_flow
    with tempfile.TemporaryDirectory() as tmp_s:
        flow = make_flow(tmp_s, implement={"rows": ["Step 1 | covered | in diff"]},
                         fix={"rows": ["D1 | unknown | shared file"]},
                         log=[f"{TS} implement ok", f"{TS} fix ok"])
        proc = _run_script(["--assess-anchors", "--flow-dir", str(flow)])
        if proc.returncode != 0:
            return (False, f"--assess-anchors should exit 0, got rc={proc.returncode}: {proc.stderr}")
        want = "fix:D1 | unavailable | artifact missing or unreadable"
        if want not in proc.stdout:
            return (False, f"expected {want!r} in output, got {proc.stdout!r}")
    return (True, "")


def _anchors_flow(tmp: Path) -> Path:
    """A flow dir whose fix step carries a real evidence artifact bound to its coverage table (D1 overlaps)."""
    from _assess_fixtures import TS, make_flow
    repo, base, plan, fixture_dir = _make_fix_fixture(tmp)
    receipt = fixture_dir / "steps" / "05-fix.md"
    receipt.write_text("", encoding="utf-8")
    cov = fixture_dir / "steps" / "05-fix.coverage.txt"
    ev = fixture_dir / "steps" / "05-fix.evidence.json"
    _run_coverage(repo, base, plan, receipt, cov, ev)
    flow = make_flow(tmp / "flow-home", implement={"rows": ["Step 1 | covered | in diff"]},
                     fix={"rows_raw": cov.read_text(encoding="utf-8")},
                     log=[f"{TS} implement ok", f"{TS} fix ok"])
    (flow / "steps" / "05-fix.evidence.json").write_bytes(ev.read_bytes())
    return flow


def case_assess_anchors_prints_overlap_status() -> Tuple[bool, str]:
    """F1: --assess-anchors prints the `overlap` status for a row whose range overlaps a hunk with no anchor."""
    with tempfile.TemporaryDirectory() as tmp_s:
        flow = _anchors_flow(Path(tmp_s))
        proc = _run_script(["--assess-anchors", "--flow-dir", str(flow)])
        if proc.returncode != 0:
            return (False, f"--assess-anchors should exit 0, got rc={proc.returncode}: {proc.stderr}")
        if not any(ln.startswith("fix:D1 | overlap | ") for ln in proc.stdout.splitlines()):
            return (False, f"fix:D1 overlaps its hunk and should print an overlap status, got {proc.stdout!r}")
    return (True, "")


def case_assess_anchors_corrupt_artifact_is_unavailable() -> Tuple[bool, str]:
    """F18: an unparseable evidence artifact is reported as unavailable; --assess-anchors still exits 0."""
    with tempfile.TemporaryDirectory() as tmp_s:
        flow = _anchors_flow(Path(tmp_s))
        (flow / "steps" / "05-fix.evidence.json").write_text("{not json", encoding="utf-8")
        proc = _run_script(["--assess-anchors", "--flow-dir", str(flow)])
        if proc.returncode != 0:
            return (False, f"corrupt artifact should not hard-stop --assess-anchors: rc={proc.returncode}: {proc.stderr}")
        if not any(ln.startswith("fix:D1 | unavailable | ") for ln in proc.stdout.splitlines()):
            return (False, f"expected fix:D1 to be unavailable, got {proc.stdout!r}")
    return (True, "")


def case_zero_start_and_inverted_row_ranges_are_unavailable() -> Tuple[bool, str]:
    """F23: a row with start < 1 or end < start is a malformed artifact: (False, unavailable), never a raise."""
    bad_rows = [
        ("start 0", [{"path": "scripts/workflow/deps.py", "start": 0, "end": 420}]),
        ("end before start", [{"path": "scripts/workflow/deps.py", "start": 420, "end": 416}]),
    ]
    for label, rows in bad_rows:
        artifact = _passing_artifact()
        artifact["rows"] = {"fix:D1": rows}
        try:
            passes, reason = _verify("fix:D1", "scripts/workflow/deps.py:418", artifact)
        except Exception as e:
            return (False, f"{label}: raised {type(e).__name__}: {e}")
        if passes or reason != "unavailable":
            return (False, f"{label}: expected (False, unavailable), got ({passes}, {reason})")
    return (True, "")


def case_evidence_and_coverage_out_require_flow_dir() -> Tuple[bool, str]:
    """F19: --coverage-out and --evidence-out without --flow-dir exit 2 and write nothing."""
    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        repo, base, plan, flow_dir = _make_fix_fixture(tmp)
        receipt = flow_dir / "steps" / "05-fix.md"
        receipt.write_text("", encoding="utf-8")
        for flag, name in (("--coverage-out", "cov.txt"), ("--evidence-out", "ev.json")):
            out = tmp / name
            args = ["--step", "fix", "--coverage", "--plan", str(plan), "--receipt", str(receipt),
                    "--repo", str(repo), "--branch", "HEAD", "--base", base, flag, str(out)]
            proc = _run_script(args)
            if proc.returncode != 2 or out.exists():
                return (False, f"{flag} without --flow-dir should exit 2 and write nothing: "
                               f"rc={proc.returncode} exists={out.exists()}")
    return (True, "")


def case_failed_tip_show_marks_artifact_unavailable() -> Tuple[bool, str]:
    """F7: a non-zero `git show` while reading the tip's line counts leaves the artifact unavailable."""
    real_git_out = fvr._git_out

    def failing_show(repo: str, args: List[str]) -> Tuple[int, str]:
        if "show" in args:
            return (128, "")
        return real_git_out(repo, args)

    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        repo, base, plan, flow_dir = _make_fix_fixture(tmp)
        receipt = flow_dir / "steps" / "05-fix.md"
        receipt.write_text("", encoding="utf-8")
        coverage_out = flow_dir / "steps" / "05-fix.coverage.txt"
        evidence_out = flow_dir / "steps" / "05-fix.evidence.json"
        fvr._git_out = failing_show
        try:
            _run_coverage(repo, base, plan, receipt, coverage_out, evidence_out)
        finally:
            fvr._git_out = real_git_out
        artifact = json.loads(evidence_out.read_text(encoding="utf-8"))
        if artifact.get("available") is not False:
            return (False, f"a failing git show should leave the artifact unavailable, got {artifact}")
    return (True, "")


CASES: List[Tuple[str, Callable[[], Tuple[bool, str]]]] = [
    ("passes with both anchors", case_verify_unknown_already_done_passes_with_both_anchors),
    ("fails on no range overlap", case_verify_unknown_already_done_fails_no_range_overlap),
    ("fails on prose-only evidence", case_verify_unknown_already_done_fails_no_anchor),
    ("fails on missing artifact", case_verify_unknown_already_done_fails_missing_artifact),
    ("fails on available:false", case_verify_unknown_already_done_fails_available_false),
    ("binding control passes", case_binding_control_passes),
    ("binding: missing coverage_sha256 unavailable", case_binding_missing_sha_is_unavailable),
    ("binding: empty coverage_sha256 unavailable", case_binding_empty_sha_is_unavailable),
    ("binding: sha mismatch unavailable", case_binding_sha_mismatch_is_unavailable),
    ("binding: no coverage file unavailable", case_binding_no_coverage_file_is_unavailable),
    ("binding: missing coverage path unavailable", case_binding_missing_coverage_path_is_unavailable),
    ("malformed artifact fields are unavailable", case_malformed_artifacts_are_unavailable),
    ("exact match resolves", case_resolve_file_token_exact_match),
    ("basename match resolves", case_resolve_file_token_basename_match),
    ("ambiguous match detected", case_resolve_file_token_ambiguous),
    ("bare root+nested token is ambiguous", case_resolve_file_token_bare_root_and_nested_is_ambiguous),
    ("bare single exact token resolves", case_resolve_file_token_bare_single_exact_resolves),
    ("qualified exact match wins", case_resolve_file_token_qualified_exact_match_wins),
    ("no match returns none", case_resolve_file_token_no_match),
    ("partial component no match", case_resolve_file_token_no_suffix_match_partial),
    ("globs never resolve", case_resolve_file_token_never_resolve_globs),
    ("basename fixture reads covered", case_classify_doing_it_rows_basename_fixture),
    ("ambiguous basename unknown", case_classify_doing_it_rows_ambiguous_basename),
    ("bound check past end", case_classify_doing_it_rows_bound_check_past_end),
    ("parses line range", case_doing_it_path_tokens_parses_line_range),
    ("single line number", case_doing_it_path_tokens_single_line),
    ("superscript digits are no range", case_line_range_superscript_digits_are_no_range),
    ("huge line number is no range", case_line_range_huge_number_is_no_range),
    ("line 0 and inverted ranges are no range", case_line_range_zero_and_inverted_are_no_range),
    ("valid range forms still parse", case_line_range_valid_forms_still_parse),
    ("unknown row range reaches artifact and verifies", case_unknown_row_range_reaches_artifact_and_verifies),
    ("coverage/evidence out outside steps refused", case_coverage_out_outside_steps_is_refused),
    ("D7 tip is the resolved 40-hex SHA", case_tip_is_full_sha_of_resolved_branch),
    ("D15 non-ancestor base is unavailable", case_non_ancestor_base_is_unavailable),
    ("failing hunk diff gives range-overlap", case_failing_hunk_diff_gives_range_overlap),
    ("D13 match-outstanding-issue exit codes", case_match_outstanding_issue_exit_codes),
    ("D13 assess-anchors unavailable without artifact", case_assess_anchors_reports_unavailable_without_artifact),
    ("F1 assess-anchors prints overlap status", case_assess_anchors_prints_overlap_status),
    ("F18 corrupt artifact is unavailable in assess-anchors", case_assess_anchors_corrupt_artifact_is_unavailable),
    ("F23 start 0 and inverted row ranges are unavailable", case_zero_start_and_inverted_row_ranges_are_unavailable),
    ("F19 coverage/evidence out require flow-dir", case_evidence_and_coverage_out_require_flow_dir),
    ("F7 failing tip git show is unavailable", case_failed_tip_show_marks_artifact_unavailable),
    ("range end at the tip line count is in bounds", case_range_end_at_tip_line_count_is_in_bounds),
    ("uncovered first row does not leak ranges", case_uncovered_first_row_does_not_leak_ranges),
    ("match-outstanding-issue runs without step or receipt", case_match_outstanding_issue_runs_without_step_or_receipt),
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
