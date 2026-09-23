#!/usr/bin/env python3
"""
Spec-blind test suite for scripts/reviewer-selection-audit.py.

Covers the plan's Testing Strategy for issue #202 (auditable useWhen tuning loop):
  (a) attendance counted per-repo and panel-wide
  (b) severity weighting 8/4/2/1 applies to CONFIRMED findings only
  (c) simulate: a run flipping include -> exclude surfaces its CONFIRMED Critical finding
  (d) simulate: two reviewers narrowed together flip the same run out -> compounding line
  (e) a run with no structural-gate marker counts as router-no and increments the
      missing-marker count
  (f) a candidate index whose only change is useWhen prose triggers the prose-only
      fallback (repo-stratified sample), never a trigger-delta simulation

Builds synthetic ~/.claude/reviews-shaped fixtures under a tempdir; never touches the
real corpus. Uses real reviewer slugs (uncle-bob, tara-typesafe, vera-verifier) so the
harness's own default_current_index_path() lookups resolve against the repo's real
reviewers/index.yaml, exactly as issue #202's plan validation did.

Run with: python3 tests/test_reviewer_selection_audit_harness.py
"""

import importlib.util
import io
import json
import re
import sys
import tempfile
from contextlib import redirect_stdout
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


def _load_current_index_or_fail(module, h, test_name):
    """
    Load the repo's real reviewers/index.yaml, surfacing a load_reviewer_index()
    ValueError (e.g. a reviewer entry missing 'contexts', per ADR-0003.2.2's hard-error
    rule) as a normal test failure instead of an unhandled crash. Returns None on
    failure so callers can bail out of the rest of their precondition checks.
    """
    try:
        return module.load_reviewer_index(module.default_current_index_path())
    except ValueError as e:
        h.test_result(test_name, False, f"load_reviewer_index() raised ValueError: {e}")
        return None


def _make_run(root, repo, run_id, panel_rows, gate_markers="", findings_md="", diff_md=""):
    """
    Build one synthetic review directory: root/repo/run_id/{tagged-sections.md,final-report.md,diff-index.md}.

    panel_rows: list of (display_name, selected) tuples for the Panel Decision table.
    gate_markers: raw HTML-comment structural-gate lines to append after the table.
    findings_md: raw '## Findings by Severity' body (or "" to omit the section).
    diff_md: raw diff-index.md content (or "" for a minimal placeholder).
    """
    run_dir = root / repo / run_id
    run_dir.mkdir(parents=True)

    table_rows = "\n".join(f"| {name} | {selected} | reason |" for name, selected in panel_rows)
    tagged = (
        "## Panel Decision\n\n"
        "| Reviewer | Selected | Reason |\n"
        "| --- | --- | --- |\n"
        f"{table_rows}\n"
    )
    if gate_markers:
        tagged += f"\n{gate_markers}\n"
    (run_dir / "tagged-sections.md").write_text(tagged)

    report = "## Findings by Severity\n\n" + findings_md if findings_md else "## Findings by Severity\n\n(none)\n"
    (run_dir / "final-report.md").write_text(report)

    (run_dir / "diff-index.md").write_text(diff_md or "## Files\n foo.py | 1 +\n\n## Hunks\n+++ b/foo.py\n")

    return run_dir


def _finding(heading, severity_letter, idx, reviewer, confirmed, extra=""):
    # "CONFIRMED" is matched as a raw substring by the harness, so the negative
    # marker must not contain it as a substring (e.g. "unconfirmed" would false-match).
    marker = "CONFIRMED" if confirmed else "DOWNGRADED"
    return (
        f"#### {severity_letter}{idx}. {heading}\n"
        f"- **Reviewer**: {reviewer}\n"
        f"- **Context Re-evaluation**: {marker}\n"
        f"{extra}\n"
    )


def test_attendance_per_repo_and_panel_wide(h):
    """(a) attendance is counted correctly per-repo and panel-wide."""
    module = _load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_run(root, "repo-a", "run1", [("Uncle Bob", "Yes")])
        _make_run(root, "repo-a", "run2", [("Uncle Bob", "No")])
        _make_run(root, "repo-b", "run3", [("Uncle Bob", "Yes")])

        buf = io.StringIO()
        with redirect_stdout(buf):
            module.cmd_attendance(str(root))
        out = buf.getvalue()

        panel_match = re.search(r"^uncle-bob\s+(\d+)\s*/\s*(\d+)", out, re.MULTILINE)
        h.test_result(
            "panel-wide: uncle-bob 2/3 selected",
            bool(panel_match) and panel_match.group(1) == "2" and panel_match.group(2) == "3",
            f"got groups={panel_match.groups() if panel_match else None}",
        )

        repo_a_match = re.search(r"repo-a:.*?uncle-bob\s+(\d+)\s*/\s*(\d+)", out, re.DOTALL)
        h.test_result(
            "repo-a: uncle-bob 1/2 selected",
            bool(repo_a_match) and repo_a_match.group(1) == "1" and repo_a_match.group(2) == "2",
            f"got groups={repo_a_match.groups() if repo_a_match else None}",
        )

        repo_b_match = re.search(r"repo-b:.*?uncle-bob\s+(\d+)\s*/\s*(\d+)", out, re.DOTALL)
        h.test_result(
            "repo-b: uncle-bob 1/1 selected",
            bool(repo_b_match) and repo_b_match.group(1) == "1" and repo_b_match.group(2) == "1",
            f"got groups={repo_b_match.groups() if repo_b_match else None}",
        )


def test_severity_weighting_confirmed_only(h):
    """(b) severity weighting 8/4/2/1 applies to CONFIRMED findings only."""
    module = _load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        findings = (
            "### Critical\n\n"
            + _finding("A confirmed critical bug", "C", 1, "Uncle Bob", confirmed=True)
            + "\n"
            + _finding("An unconfirmed critical claim", "C", 2, "Uncle Bob", confirmed=False)
            + "\n### High\n\n"
            + _finding("A confirmed high bug", "H", 1, "Uncle Bob", confirmed=True)
            + "\n### Medium\n\n"
            + _finding("A confirmed medium bug", "M", 1, "Uncle Bob", confirmed=True)
            + "\n### Low\n\n"
            + _finding("A confirmed low nit", "L", 1, "Uncle Bob", confirmed=True)
            + "\n"
        )
        _make_run(root, "repo-c", "run1", [("Uncle Bob", "Yes")], findings_md=findings)

        buf = io.StringIO()
        with redirect_stdout(buf):
            module.cmd_yield(str(root))
        out = buf.getvalue()

        m = re.search(r"^uncle-bob\s+([\d.]+)\s+([\d.]+)\s+(\d+)", out, re.MULTILINE)
        h.test_result(
            "yield found for uncle-bob",
            bool(m),
            f"no match in output:\n{out}",
        )
        if m:
            avg_yield, total_yield, run_count = m.groups()
            # 8 (confirmed critical) + 4 (confirmed high) + 2 (confirmed medium) + 1 (confirmed low) = 15;
            # the unconfirmed critical (would-be +8) must NOT be counted.
            h.test_result(
                "total yield = 15.00 (unconfirmed critical excluded)",
                total_yield == "15.00",
                f"got total_yield={total_yield}",
            )
            h.test_result(
                "avg yield = 15.00 over 1 attended run",
                avg_yield == "15.00" and run_count == "1",
                f"got avg={avg_yield} run_count={run_count}",
            )


def test_simulate_include_to_exclude_surfaces_critical(h):
    """(c) a run flipping include -> exclude surfaces its CONFIRMED Critical finding."""
    module = _load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        findings = "### Critical\n\n" + _finding(
            "A real bug only uncle-bob caught", "C", 1, "Uncle Bob", confirmed=True
        )
        diff = "## Files\n foo.py | 3 +\n\n## Hunks\n+++ b/foo.py\n@@ -1,3 +1,3 @@\n+needs cleanup here\n"
        _make_run(root, "repo-d", "run1", [("Uncle Bob", "Yes")], findings_md=findings, diff_md=diff)

        current = _load_current_index_or_fail(
            module, h, "precondition: real index has uncle-bob with 'cleanup' trigger"
        )
        if current is None:
            return
        h.test_result(
            "precondition: real index has uncle-bob with 'cleanup' trigger",
            "uncle-bob" in current and "cleanup" in current["uncle-bob"]["triggers"],
            f"uncle-bob entry={current.get('uncle-bob')}",
        )

        candidate_path = root / "candidate-index.yaml"
        candidate_path.write_text(
            "reviewers:\n"
            "  - name: Uncle Bob\n"
            "    file: uncle-bob.yaml\n"
            "    priority: high\n"
            "    contexts: {review: secondary, plan: primary}\n"
            "    useWhen: narrowed for test\n"
            "    triggers: [nonmatching-trigger-xyz]\n"
        )

        buf = io.StringIO()
        with redirect_stdout(buf):
            module.cmd_simulate(str(root), str(candidate_path), reviewer_filter="uncle-bob")
        out = buf.getvalue()

        h.test_result(
            "run1 listed under include -> exclude",
            "run1" in out.split("=== Runs flipping exclude -> include ===")[0],
        )
        h.test_result(
            "CONFIRMED Critical finding surfaced with uncle-bob attribution",
            "CONFIRMED Critical C1" in out and "Uncle Bob" in out,
            f"output:\n{out}",
        )


def test_simulate_compounding_two_reviewers(h):
    """(d) two reviewers narrowed together flip the same run out -> compounding line."""
    module = _load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        diff = (
            "## Files\n foo.py | 3 +\n\n## Hunks\n+++ b/foo.py\n"
            "@@ -1,3 +1,3 @@\n+needs cleanup and a schema update\n"
        )
        _make_run(
            root, "repo-e", "run1",
            [("Uncle Bob", "Yes"), ("Tara TypeSafe", "Yes")],
            diff_md=diff,
        )

        current = _load_current_index_or_fail(
            module, h, "precondition: real index has tara-typesafe with 'schema' trigger"
        )
        if current is None:
            return
        h.test_result(
            "precondition: real index has tara-typesafe with 'schema' trigger",
            "tara-typesafe" in current and "schema" in current["tara-typesafe"]["triggers"],
            f"tara-typesafe entry={current.get('tara-typesafe')}",
        )

        # changed_reviewers() diffs the FULL current index against the candidate, so a
        # candidate that only lists the two targeted reviewers would make every other
        # reviewer look "changed" too (missing == different triggers). Copy the real
        # index.yaml in full and narrow only the two targets within it, as issue #202's
        # own manual validation did.
        real_index_text = (REPO_ROOT / "reviewers" / "index.yaml").read_text()
        candidate_text = real_index_text.replace(
            "triggers: [open, close, drop, dispose, new, create, acquire, release, "
            "shutdown, stop, terminate, timeout, deadline, spawn, thread, task, finally, cleanup]",
            "triggers: [nonmatching-trigger-xyz]",
        ).replace(
            'triggers: [interface, type, struct, trait, any, unknown, as, "as any", unsafe, '
            "Zod, schema, invariant, assert, debug_assert, must, never, always, validate]",
            "triggers: [nonmatching-trigger-abc]",
        )
        h.test_result(
            "precondition: both replacements actually matched the real index text",
            candidate_text != real_index_text
            and "nonmatching-trigger-xyz" in candidate_text
            and "nonmatching-trigger-abc" in candidate_text,
        )
        candidate_path = root / "candidate-index.yaml"
        candidate_path.write_text(candidate_text)

        buf = io.StringIO()
        with redirect_stdout(buf):
            module.cmd_simulate(str(root), str(candidate_path))
        out = buf.getvalue()

        h.test_result(
            "compounding section reports run1 for both reviewers",
            "Compounding narrowing" in out
            and "tara-typesafe" in out.split("Compounding narrowing")[1].split("\n")[0]
            and "uncle-bob" in out.split("Compounding narrowing")[1].split("\n")[0]
            and "run1" in out.split("Compounding narrowing")[1],
            f"output:\n{out}",
        )


def test_missing_structural_gate_marker_counts_as_router_no(h):
    """(e) a run with no structural-gate marker counts as router-no, and increments the missing-marker count."""
    module = _load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # No gate_markers passed at all -> no structural-gate HTML comments in tagged-sections.md.
        _make_run(root, "repo-f", "run1", [("Uncle Bob", "No")])

        buf = io.StringIO()
        with redirect_stdout(buf):
            module.cmd_attendance(str(root))
        out = buf.getvalue()

        h.test_result(
            "missing-marker count is 1",
            "Runs without structural-gate markers: 1" in out,
            f"output:\n{out}",
        )

        m = re.search(r"^uncle-bob\s+(\d+)\s*/\d+\s+(\d+)\s*/\d+\s+(\d+)\s*/\d+", out, re.MULTILINE)
        h.test_result(
            "uncle-bob counted as router-no (not structural-gate-no)",
            bool(m) and m.group(1) == "0" and m.group(2) == "1" and m.group(3) == "0",
            f"got groups={m.groups() if m else None}\noutput:\n{out}",
        )


def test_prose_only_useWhen_change_triggers_fallback(h):
    """(f) a useWhen-only candidate change triggers the prose-only fallback, never a trigger simulation."""
    module = _load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for i in range(3):
            _make_run(root, "repo-g", f"run{i}", [("Vera Verifier", "Yes")])

        current = _load_current_index_or_fail(module, h, "precondition: real index has vera-verifier")
        if current is None:
            return
        h.test_result(
            "precondition: real index has vera-verifier",
            "vera-verifier" in current,
            f"index keys sample={list(current.keys())[:5]}",
        )
        vera_triggers = current.get("vera-verifier", {}).get("triggers", [])

        candidate_path = root / "candidate-index.yaml"
        # Quote each trigger: some real triggers are globs like "*.test.*", and a bare
        # leading "*" in YAML flow-sequence context is parsed as an alias reference.
        triggers_yaml = ", ".join(json.dumps(t) for t in vera_triggers)
        candidate_path.write_text(
            "reviewers:\n"
            "  - name: Vera Verifier\n"
            "    file: vera-verifier.yaml\n"
            "    priority: high\n"
            "    contexts: {review: primary, plan: primary}\n"
            "    useWhen: a brand-new prose description, triggers unchanged\n"
            f"    triggers: [{triggers_yaml}]\n"
        )

        buf = io.StringIO()
        with redirect_stdout(buf):
            module.cmd_simulate(str(root), str(candidate_path), reviewer_filter="vera-verifier")
        out = buf.getvalue()

        h.test_result(
            "prose-only fallback message printed",
            "No trigger delta for 'vera-verifier'" in out,
            f"output:\n{out}",
        )
        h.test_result(
            "trigger-delta simulation never runs for a prose-only change",
            "Simulating trigger-delta reviewers" not in out,
            f"output:\n{out}",
        )
        h.test_result(
            "sample run directories are listed",
            "run0" in out and "run1" in out and "run2" in out,
            f"output:\n{out}",
        )


def test_trigger_matches_multi_star_globs(h):
    """(h) filename globs with more than one '*' (or a '*' not before '.') match touched files."""
    mod = _load_module()
    diff = "## Hunks\n+++ b/src/foo.test.ts\n+++ b/crates/x/lock_test.rs\n+++ b/Tests/Bar.swift\n"

    cases = [
        ("*.test.*", True, "matches foo.test.ts"),
        ("*_test.rs", True, "matches lock_test.rs"),
        ("*/tests/*", False, "no lowercase tests/ dir touched"),
        ("Tests/*", True, "matches Tests/Bar.swift"),
        ("*.spec.*", False, "no spec file touched"),
    ]
    for trigger, expected, why in cases:
        h.test_result(
            f"trigger_matches({trigger!r}) is {expected} ({why})",
            mod.trigger_matches(trigger, diff) is expected,
        )

    h.test_result(
        "non-glob trigger still substring-matches case-insensitively",
        mod.trigger_matches("BAR.swift", diff) is True,
    )


def main():
    h = Harness("REVIEWER SELECTION AUDIT HARNESS TEST SUITE")

    test_attendance_per_repo_and_panel_wide(h)
    test_severity_weighting_confirmed_only(h)
    test_simulate_include_to_exclude_surfaces_critical(h)
    test_simulate_compounding_two_reviewers(h)
    test_missing_structural_gate_marker_counts_as_router_no(h)
    test_prose_only_useWhen_change_triggers_fallback(h)
    test_trigger_matches_multi_star_globs(h)

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
