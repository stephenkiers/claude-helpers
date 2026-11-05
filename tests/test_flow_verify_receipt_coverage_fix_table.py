#!/usr/bin/env python3
"""
Fix-mode coverage table tests for scripts/flow-verify-receipt.py (#409).

The #358 run wrote an all-`unknown` fix coverage table because the Doing-it rows were never matched to
their File cells. The fixture under tests/fixtures/coverage-fix-358/ is that run's action plan (Doing-it
section, verbatim), fix receipt and observed coverage file. These cases pin the shared table-row parser
and the File-cell classification:
- (a) named rows classify as covered / uncovered / unknown; shared-file rows stay unknown with a reason
- (b) seam: coverage_mode stdout -> _parse_coverage_file -> derive_outstanding_set, with asserted counts
- (c) unknown rows, shared-file rows included, count as outstanding
- (d) an `unknown-declared:` line is not represented in the outstanding set (documents current behaviour)
- (e) parity: the row parser's count equals the observed 18 rows
- (f) per-token classification on the real cell strings
- (g) a table with no File header yields all unknown

The diff list is stubbed (`_git_diff_names`), so no git repo is needed.

Run with: python3 tests/test_flow_verify_receipt_coverage_fix_table.py
"""

import importlib.util
import io
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "flow-verify-receipt.py"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "coverage-fix-358"
PLAN = FIXTURE / "claude-action-plan-doing-it.md"
RECEIPT = FIXTURE / "05-fix.md"
OBSERVED = FIXTURE / "05-fix.coverage.txt"
ALL_UNITS = [f"D{n}" for n in range(1, 19)]

_spec = importlib.util.spec_from_file_location("flow_verify_receipt", SCRIPT)
fvr = importlib.util.module_from_spec(_spec)
sys.modules["flow_verify_receipt"] = fvr
_spec.loader.exec_module(fvr)


def run_fix_coverage(plan_path: Path, diff: Set[str]) -> str:
    """Run coverage_mode(step='fix') in-process with the diff list stubbed; return its stdout."""
    original = fvr._git_diff_names
    original_resolve = fvr._resolve_commit
    fvr._git_diff_names = lambda repo, base, branch: set(diff)
    # The branch is resolved to a tip SHA before the diff; the fixture repo does not exist, so stub that step too
    fvr._resolve_commit = lambda repo, ref: "0" * 40
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            try:
                fvr.coverage_mode("fix", str(plan_path), str(RECEIPT), "/unused-repo", "HEAD", "base")
            except SystemExit as e:
                if e.code not in (0, None):
                    raise
    finally:
        fvr._git_diff_names = original
        fvr._resolve_commit = original_resolve
    return buf.getvalue()


def parse_rows(stdout: str) -> Dict[str, Tuple[str, str]]:
    """Map unit id -> (status, evidence) from coverage_mode stdout."""
    rows: Dict[str, Tuple[str, str]] = {}
    for line in stdout.splitlines():
        parts = line.split(" | ", 2)
        if len(parts) == 3 and parts[0].startswith("D"):
            rows[parts[0]] = (parts[1], parts[2])
    return rows


def expect_rows(rows: Dict[str, Tuple[str, str]], expected: Dict[str, Tuple[str, str]]) -> Tuple[bool, str]:
    """Compare the named rows against expected (status, evidence) pairs; report every mismatch."""
    mismatches = []
    for unit, want in expected.items():
        got = rows.get(unit)
        if got != want:
            mismatches.append(f"{unit}: want {want!r}, got {got!r}")
    return (not mismatches, "; ".join(mismatches))


def seam_outstanding(stdout: str) -> Tuple[Dict[str, str], Optional[str]]:
    """coverage_mode stdout -> persisted coverage file -> derive_outstanding_set; return ({id: coverage}, error)."""
    parsed_kind, parsed_rows, parse_err = fvr._parse_coverage_file(stdout)
    if parsed_kind != "table":
        return ({}, f"coverage file did not parse as a table: {parse_err}")
    if len(parsed_rows) != len(ALL_UNITS):
        return ({}, f"expected {len(ALL_UNITS)} parsed rows, got {len(parsed_rows)}")
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp)
        (flow_dir / "steps").mkdir()
        (flow_dir / "log.md").write_text("2026-10-30T12:00:00Z fix ok\n", encoding="utf-8")
        (flow_dir / "steps" / "05-fix.md").write_text(RECEIPT.read_text(encoding="utf-8"), encoding="utf-8")
        (flow_dir / "steps" / "05-fix.coverage.txt").write_text(stdout, encoding="utf-8")
        items, err = fvr.derive_outstanding_set(flow_dir)
    return ({item.id: item.coverage for item in items}, err)


# (a) named rows, one diff per scenario ---------------------------------------------------------

def case_a_unique_file_in_diff() -> Tuple[bool, str]:
    rows = parse_rows(run_fix_coverage(PLAN, {"CLAUDE.md"}))
    return expect_rows(rows, {
        # Row 7 names CLAUDE.md, which no other row names: covered through the bare name.
        "D7": ("covered", "files in diff: CLAUDE.md"),
        # Shared flow-bash-guard rows, not in the diff: uncovered.
        "D1": ("uncovered", "not in diff: scripts/flow-bash-guard.py"),
        "D2": ("uncovered", "not in diff: scripts/flow-bash-guard.py"),
        "D3": ("uncovered", "not in diff: scripts/flow-bash-guard.py"),
        "D5": ("uncovered", "not in diff: scripts/flow-bash-guard.py"),
        "D6": ("uncovered", "not in diff: scripts/flow-bash-guard.py"),
        "D16": ("uncovered", "not in diff: scripts/flow-bash-guard.py"),
        "D4": ("uncovered", "not in diff: tests/test_flow_hook_guards.py"),
        "D8": ("uncovered", "not in diff: agents/flow-review.md"),
        # Row 10: the `~1350` and `~1648` fragments are dropped, so only the path is reported.
        "D10": ("uncovered", "not in diff: commands/expert-flow.md"),
    })


def case_a_shared_flow_bash_guard_in_diff() -> Tuple[bool, str]:
    rows = parse_rows(run_fix_coverage(PLAN, {"scripts/flow-bash-guard.py"}))
    return expect_rows(rows, {
        "D1": ("unknown", "file shared with D2, D3, D5, D6, D16"),
        "D2": ("unknown", "file shared with D1, D3, D5, D6, D16"),
        "D3": ("unknown", "file shared with D1, D2, D5, D6, D16"),
        "D5": ("unknown", "file shared with D1, D2, D3, D6, D16"),
        "D6": ("unknown", "file shared with D1, D2, D3, D5, D16"),
        "D16": ("unknown", "file shared with D1, D2, D3, D5, D6"),
        "D4": ("uncovered", "not in diff: tests/test_flow_hook_guards.py"),
    })


def case_a_shared_test_and_docs_in_diff() -> Tuple[bool, str]:
    rows = parse_rows(run_fix_coverage(PLAN, {"tests/test_flow_hook_guards.py", "commands/expert-flow.md"}))
    return expect_rows(rows, {
        "D4": ("unknown", "file shared with D9"),
        "D9": ("unknown", "file shared with D4"),
        "D10": ("unknown", "file shared with D11, D12"),
        "D11": ("unknown", "file shared with D10, D12"),
        "D12": ("unknown", "file shared with D10, D11"),
    })


def case_a_shared_agent_doc_and_unique_guard() -> Tuple[bool, str]:
    rows = parse_rows(run_fix_coverage(PLAN, {"agents/flow-review.md", "scripts/flow-agent-guard.py"}))
    return expect_rows(rows, {
        "D7": ("unknown", "file shared with D8"),
        "D8": ("unknown", "file shared with D7"),
        "D15": ("covered", "files in diff: scripts/flow-agent-guard.py"),
    })


# (b), (c) seam round-trip and outstanding counts -----------------------------------------------

SEAM_DIFF = {"CLAUDE.md", "scripts/flow-agent-guard.py", "scripts/flow-bash-guard.py"}


def case_b_seam_baseline_all_unknown() -> Tuple[bool, str]:
    observed = OBSERVED.read_text(encoding="utf-8")
    outstanding, err = seam_outstanding(observed)
    if err is not None:
        return (False, f"derive error: {err}")
    want = {f"fix:{unit}": "unknown" for unit in ALL_UNITS}
    if outstanding != want:
        return (False, f"baseline outstanding set: want 18 unknown items, got {len(outstanding)}: {outstanding}")
    return (True, "")


def case_b_seam_scenario_counts_and_values() -> Tuple[bool, str]:
    stdout = run_fix_coverage(PLAN, SEAM_DIFF)
    outstanding, err = seam_outstanding(stdout)
    if err is not None:
        return (False, f"derive error: {err}")
    # Covered rows (D7 via CLAUDE.md, D15 via flow-agent-guard) drop out; the other 16 stay outstanding.
    if len(outstanding) != 16:
        return (False, f"expected 16 outstanding items, got {len(outstanding)}: {sorted(outstanding)}")
    if "fix:D7" in outstanding or "fix:D15" in outstanding:
        return (False, "covered rows D7/D15 must not be outstanding")
    checks = {"fix:D1": "unknown", "fix:D4": "uncovered", "fix:D10": "uncovered"}
    for item_id, want in checks.items():
        if outstanding.get(item_id) != want:
            return (False, f"{item_id}: want {want}, got {outstanding.get(item_id)}")
    return (True, "")


def case_c_unknown_rows_count_as_outstanding() -> Tuple[bool, str]:
    stdout = run_fix_coverage(PLAN, SEAM_DIFF)
    rows = parse_rows(stdout)
    outstanding, err = seam_outstanding(stdout)
    if err is not None:
        return (False, f"derive error: {err}")
    unknown_units = [unit for unit, (status, _) in rows.items() if status == "unknown"]
    missing = [unit for unit in unknown_units if outstanding.get(f"fix:{unit}") != "unknown"]
    if not unknown_units:
        return (False, "scenario produced no unknown rows; the check would be vacuous")
    if missing:
        return (False, f"unknown rows missing from outstanding set: {missing}")
    return (True, "")


# (d) unknown-declared is documented as dropped ---------------------------------------------------

def case_d_unknown_declared_not_outstanding() -> Tuple[bool, str]:
    text = OBSERVED.read_text(encoding="utf-8").rstrip("\n") + "\nunknown-declared: D99\n"
    outstanding, err = seam_outstanding(text)
    if err is not None:
        return (False, f"derive error: {err}")
    if "fix:D99" in outstanding:
        return (False, "unknown-declared line must not become an outstanding item (documented behaviour)")
    if len(outstanding) != 18:
        return (False, f"expected the 18 table rows only, got {len(outstanding)}")
    return (True, "")


# (e) parity -------------------------------------------------------------------------------------

def case_e_parity_with_observed_rows() -> Tuple[bool, str]:
    plan_text = PLAN.read_text(encoding="utf-8")
    enumerated = fvr._enumerate_doing_it_items(plan_text)
    parsed = [row.unit_id for row in fvr._parse_doing_it_rows(plan_text)]
    observed_units = list(parse_rows(OBSERVED.read_text(encoding="utf-8")).keys())
    if enumerated != ALL_UNITS:
        return (False, f"enumerator returned {len(enumerated)} units: {enumerated}")
    if parsed != enumerated:
        return (False, f"row parser {parsed} differs from enumerator {enumerated}")
    if observed_units != enumerated:
        return (False, f"observed coverage units {observed_units} differ from enumerator")
    return (True, "")


# (f) per-token classification on the real cell strings ------------------------------------------

def case_f_token_classification() -> Tuple[bool, str]:
    cases: List[Tuple[str, Set[str], List[str]]] = [
        ("agents/flow-review.md:5-12; CLAUDE.md", {"CLAUDE.md", "agents/flow-review.md"},
         ["agents/flow-review.md", "CLAUDE.md"]),
        ("agents/flow-review.md:5-12; CLAUDE.md", {"agents/flow-review.md"}, ["agents/flow-review.md"]),
        ("tests/test_flow_hook_guards.py:1690-1697, 1758-1777", set(), ["tests/test_flow_hook_guards.py"]),
        ("commands/expert-flow.md:~971, ~1350, ~1648", set(), ["commands/expert-flow.md"]),
        ("scripts/flow-bash-guard.py:~70-80", set(), ["scripts/flow-bash-guard.py"]),
        ("`scripts/flow-agent-guard.py:~625-660`", set(), ["scripts/flow-agent-guard.py"]),
        # Bare basename: a unique whole-component suffix match in the changed set resolves to the changed path
        ("flow-review.md", {"agents/flow-review.md"}, ["agents/flow-review.md"]),
        ("", set(), []),
        # Leading ./ normalization: ./scripts/x.py matches scripts/x.py in diff
        ("./scripts/flow-bash-guard.py", {"scripts/flow-bash-guard.py"}, ["scripts/flow-bash-guard.py"]),
        # Multiple leading ./ stripped repeatedly
        ("././scripts/test.py", {"scripts/test.py"}, ["scripts/test.py"]),
        # ../ not stripped (protected)
        ("../scripts/x.py", set(), ["../scripts/x.py"]),
    ]
    for cell, diff, want in cases:
        # A raw token (no match, kept for uncovered detection) carries its text in .token, not resolved_path
        got = [t.resolved_path if t.status == "resolved" else t.token for t in fvr._doing_it_path_tokens(cell, diff)]
        if got != want:
            return (False, f"cell {cell!r} with diff {sorted(diff)}: want {want}, got {got}")
    plan_rows = fvr._parse_doing_it_rows(PLAN.read_text(encoding="utf-8"))
    row7 = [row for row in plan_rows if row.unit_id == "D7"]
    if not row7 or row7[0].file_cell != "agents/flow-review.md:5-12; CLAUDE.md":
        return (False, f"D7 file cell read from the real table is {row7[0].file_cell if row7 else None!r}")
    return (True, "")


# (g) no File header ------------------------------------------------------------------------------

NO_FILE_HEADER_PLAN = """# Action plan

## Doing it: 2

| Item | Work |
|------|------|
| one | scripts/flow-bash-guard.py: fix it |
| two | tests/test_x.py: add a test |

## Deferred: 0
"""


def case_g_no_file_header_all_unknown() -> Tuple[bool, str]:
    with tempfile.TemporaryDirectory() as tmp:
        plan = Path(tmp) / "plan.md"
        plan.write_text(NO_FILE_HEADER_PLAN, encoding="utf-8")
        rows = parse_rows(run_fix_coverage(plan, {"scripts/flow-bash-guard.py", "tests/test_x.py"}))
    return expect_rows(rows, {
        "D1": ("unknown", "no extractable evidence (File header missing)"),
        "D2": ("unknown", "no extractable evidence (File header missing)"),
    })


# (h) empty cells vs no File header ------------------------------------------------------------------

EMPTY_CELLS_PLAN = """# Action plan

## Doing it: 2

| Item | File |
|------|------|
| one |  |
| two |  |

## Deferred: 0
"""


def case_h_empty_cells_vs_no_header() -> Tuple[bool, str]:
    with tempfile.TemporaryDirectory() as tmp:
        plan = Path(tmp) / "plan.md"
        plan.write_text(EMPTY_CELLS_PLAN, encoding="utf-8")
        rows = parse_rows(run_fix_coverage(plan, {}))
    return expect_rows(rows, {
        "D1": ("unknown", "no extractable evidence (no files in table row)"),
        "D2": ("unknown", "no extractable evidence (no files in table row)"),
    })


CASES: List[Tuple[str, Callable[[], Tuple[bool, str]]]] = [
    ("(a) D7 covered through CLAUDE.md; shared and fragment rows uncovered when not in diff", case_a_unique_file_in_diff),
    ("(a) shared flow-bash-guard rows are unknown with a 'file shared with' reason", case_a_shared_flow_bash_guard_in_diff),
    ("(a) shared tests/test_flow_hook_guards.py and commands/expert-flow.md rows are unknown", case_a_shared_test_and_docs_in_diff),
    ("(a) shared agents/flow-review.md rows are unknown; unique flow-agent-guard row is covered", case_a_shared_agent_doc_and_unique_guard),
    ("(b) baseline seam: the observed all-unknown table yields 18 outstanding unknown items", case_b_seam_baseline_all_unknown),
    ("(b) seam round-trip: covered rows drop out; outstanding count is 16 under the scenario diff", case_b_seam_scenario_counts_and_values),
    ("(c) unknown rows, including shared-file rows, count as outstanding", case_c_unknown_rows_count_as_outstanding),
    ("(d) an unknown-declared line is not an outstanding item (documented behaviour)", case_d_unknown_declared_not_outstanding),
    ("(e) parity: row parser and enumerator both yield the observed 18 units in order", case_e_parity_with_observed_rows),
    ("(f) per-token classification on the real cell strings", case_f_token_classification),
    ("(g) a table with no File header yields all unknown", case_g_no_file_header_all_unknown),
    ("(h) empty File cells are distinct from no File header; both yield unknown but with different evidence", case_h_empty_cells_vs_no_header),
]


def main() -> None:
    h = Harness("Fix-mode coverage table (#409)")
    for name, fn in CASES:
        try:
            passed, detail = fn()
        except Exception as e:  # reproduction on an unfixed tree must report, not crash the suite
            passed, detail = False, f"raised {type(e).__name__}: {e}"
        h.test_result(name, passed, detail)
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
