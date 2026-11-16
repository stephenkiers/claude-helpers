#!/usr/bin/env python3
"""
Completeness check for docs/v2-inventory/skeleton-findings.md (Sub-issue S, plan Step 10).

The findings file records what the walking-skeleton measurement runs showed. This test checks shape only,
never values: it cannot tell a good measurement from a bad one, but it makes a placeholder fail the build.

Covered here:
1. The findings file exists.
2. The provenance header and the S-D1 coverage disclaimer are present.
3. Every required row ID has exactly one table row.
4. Each row's last cell holds either `Evidence: <non-empty pointer>` or `NOT MEASURED — <non-empty reason>`.
5. No PENDING / TBD / TODO placeholder token remains anywhere in the file.
6. Synthetic fixtures prove the row check rejects a missing row, an empty reason and a row with neither form.

Run with: python3 tests/test_skeleton_findings.py
"""

import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT


FINDINGS = REPO_ROOT / "docs" / "v2-inventory" / "skeleton-findings.md"

REQUIRED_ROW_IDS = (
    "AC1-SPAWN",
    "AC1-RESUME",
    "AC1-WAIT",
    "AC1-ASK",
    "AC1-PARK",
    "AC2-K1-interrupt",
    "AC2-K1-kill",
    "AC2-K2-interrupt",
    "AC2-K2-kill",
    "AC3",
    "AC4a",
    "AC4b",
    "PHANTOM-LIVE",
    "ORPHAN-OVERLAP",
    "SPAWN-XCHECK",
    "RESUME-a",
    "RESUME-b",
    "RESUME-c",
    "COMPACTION",
    "LATE-RECEIPT",
)

PLACEHOLDER_RE = re.compile(r"\b(PENDING|TBD|TODO)\b")
NOT_MEASURED_RE = re.compile(r"^NOT MEASURED — (\S.*)$")
EVIDENCE_RE = re.compile(r"^Evidence: (\S.*)$")


def parse_rows(text: str) -> Dict[str, List[List[str]]]:
    """Map each first-cell ID of a markdown table row to the list of its rows' cell lists."""
    rows: Dict[str, List[List[str]]] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        rows.setdefault(cells[0].strip("`"), []).append(cells)
    return rows


def row_problems(text: str, required: Tuple[str, ...] = REQUIRED_ROW_IDS) -> List[str]:
    problems: List[str] = []
    rows = parse_rows(text)
    for row_id in required:
        found = rows.get(row_id, [])
        if len(found) != 1:
            problems.append(f"{row_id}: expected exactly one row, found {len(found)}")
            continue
        last = found[0][-1]
        if len(found[0]) < 3:
            problems.append(f"{row_id}: row needs at least an ID, a result and an evidence/reason cell")
        elif not (NOT_MEASURED_RE.match(last) or EVIDENCE_RE.match(last)):
            problems.append(f"{row_id}: last cell must be 'Evidence: <pointer>' or 'NOT MEASURED — <reason>'")
    return problems


def test_file_exists():
    return FINDINGS.is_file(), f"{FINDINGS} missing"


def test_provenance_header():
    text = FINDINGS.read_text(encoding="utf-8")
    needed = ("## Provenance", "skeleton-runbook.md", "claude --version", "temporary symlinks")
    missing = [n for n in needed if n not in text]
    return not missing, f"provenance header missing: {missing}"


def test_s_d1_disclaimer():
    text = FINDINGS.read_text(encoding="utf-8")
    needed = ("## Coverage disclaimer (S-D1)", "general-purpose", "Layer 2")
    missing = [n for n in needed if n not in text]
    return not missing, f"S-D1 disclaimer missing: {missing}"


def test_cleanup_note():
    text = FINDINGS.read_text(encoding="utf-8")
    return "S-D5" in text and "manual" in text.lower(), "S-D5 manual-cleanup note missing"


def test_every_row_present_with_evidence_or_reason():
    problems = row_problems(FINDINGS.read_text(encoding="utf-8"))
    return not problems, "; ".join(problems)


def test_no_placeholder_tokens():
    hits = [
        f"line {i}: {m.group(1)}"
        for i, line in enumerate(FINDINGS.read_text(encoding="utf-8").splitlines(), 1)
        for m in PLACEHOLDER_RE.finditer(line)
    ]
    return not hits, "; ".join(hits)


def _good_table(ids: Tuple[str, ...]) -> str:
    lines = ["| ID | Result | Evidence |", "| --- | --- | --- |"]
    lines += [f"| {i} | r | Evidence: flow x |" for i in ids]
    return "\n".join(lines)


def test_fixture_complete_table_passes():
    return row_problems(_good_table(("A", "B")), ("A", "B")) == [], "complete table should pass"


def test_fixture_missing_row_fails():
    return len(row_problems(_good_table(("A",)), ("A", "B"))) == 1, "missing row B should be one problem"


def test_fixture_duplicate_row_fails():
    text = _good_table(("A", "A"))
    return len(row_problems(text, ("A",))) == 1, "duplicate row should fail"


def test_fixture_empty_reason_fails():
    text = "| A | r | NOT MEASURED —  |"
    return len(row_problems(text, ("A",))) == 1, "empty reason should fail"


def test_fixture_reason_passes():
    text = "| A | r | NOT MEASURED — requires a human |"
    return row_problems(text, ("A",)) == [], "reason should pass"


def test_fixture_neither_form_fails():
    text = "| A | r | looked fine |"
    return len(row_problems(text, ("A",))) == 1, "row with neither form should fail"


def test_fixture_empty_evidence_fails():
    text = "| A | r | Evidence:  |"
    return len(row_problems(text, ("A",))) == 1, "empty evidence pointer should fail"


def _check(h, label, fn):
    """Run one test function, recording a crash as a failure instead of aborting the suite."""
    try:
        passed, msg = fn()
    except Exception as exc:  # report, do not abort the suite
        passed, msg = False, f"{type(exc).__name__}: {exc}"
    h.test_result(label, passed, msg)


if __name__ == "__main__":
    h = Harness("SKELETON FINDINGS COMPLETENESS TEST SUITE")

    print("[Section 1] the real findings file")
    _check(h, "skeleton-findings.md exists", test_file_exists)
    if FINDINGS.is_file():
        _check(h, "provenance header present", test_provenance_header)
        _check(h, "S-D1 coverage disclaimer present", test_s_d1_disclaimer)
        _check(h, "S-D5 manual-cleanup note present", test_cleanup_note)
        _check(h, "every row ID has one row with an evidence pointer or NOT MEASURED reason",
               test_every_row_present_with_evidence_or_reason)
        _check(h, "no PENDING/TBD/TODO placeholder token", test_no_placeholder_tokens)
    print()

    print("[Section 2] synthetic fixtures")
    _check(h, "complete table passes", test_fixture_complete_table_passes)
    _check(h, "missing row fails", test_fixture_missing_row_fails)
    _check(h, "duplicate row fails", test_fixture_duplicate_row_fails)
    _check(h, "empty NOT MEASURED reason fails", test_fixture_empty_reason_fails)
    _check(h, "NOT MEASURED with a reason passes", test_fixture_reason_passes)
    _check(h, "row with neither form fails", test_fixture_neither_form_fails)
    _check(h, "empty evidence pointer fails", test_fixture_empty_evidence_fails)
    print()

    h.summarize_and_exit()
