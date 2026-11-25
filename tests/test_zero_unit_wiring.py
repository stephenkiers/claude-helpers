#!/usr/bin/env python3
"""Wiring tests for zero-unit runs (#526): the paths that were first left unconnected.

Covers: implement-ledger render/check with --split-basis zero-units, the verifier's zero-unit
plan cross-check (invoked with --plan, typed reason prefix, fix step not wrongly failed), the
test-correct-zero-unit status and exemption tokens in the docs, the fidelity-audit carve-out,
and the render call sites binding the receipt's SPLIT_BASIS.

Run with: python3 tests/test_zero_unit_wiring.py
"""

import importlib.util
import re
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _test_harness import Harness, REPO_ROOT
import test_implement_ledger as tl

_spec = importlib.util.spec_from_file_location(
    "flow_verify_receipt_zu", REPO_ROOT / "scripts" / "flow-verify-receipt.py"
)
fvr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fvr)

EXPECTED_PREFIX = "zero-unit-plan-incomplete"
GOOD_PLAN = (
    "## Implementation Steps\n\n"
    "### Tests (written spec-blind in Round 2)\n\n"
    "- covers the behaviour\n\n"
    "Targets: `scripts/unit-sizing.py`\n\n"
    "## Other\n"
)


def _doc(rel):
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _zero_unit_run(sb, key):
    run = tl.Run(sb, key)
    run.init()
    run.append(tl.full_rows(run, cls="full", skip={"round1": "no-round1-units"}))
    return run


def test_ledger_render_and_check_with_zero_units():
    sb = tl.Sandbox()
    try:
        run = _zero_unit_run(sb, "run-zu-wire")
        bad = sb.run(["check", "--run-key", run.rk])
        good = sb.run(["check", "--run-key", run.rk, "--split-basis", "zero-units"])
        rend_bad = sb.run(["render", "--run-key", run.rk])
        rend_good = sb.run(["render", "--run-key", run.rk, "--split-basis", "zero-units"])
        out_bad = tl._out(rend_bad)
        out_good = tl._out(rend_good)
        ok = (bad.returncode != 0 and good.returncode == 0
              and "not permitted" in out_bad and "not permitted" not in out_good)
        return ok, f"bad={bad.returncode} good={good.returncode} render_good={out_good[:300]!r}"
    finally:
        sb.close()


def test_verifier_plan_check_prefix_and_pass():
    with tempfile.TemporaryDirectory() as td:
        good = Path(td) / "good.md"
        good.write_text(GOOD_PLAN)
        no_tests = Path(td) / "nt.md"
        no_tests.write_text("## Implementation Steps\n\n## Other\n")
        no_targets = Path(td) / "ntg.md"
        no_targets.write_text(GOOD_PLAN.replace("Targets: `scripts/unit-sizing.py`\n", ""))
        nontest = Path(td) / "nontest.md"
        nontest.write_text(
            "## Implementation Steps\n\n### Step 1: x\n- **Files**: `src/app.py`\n\n"
            + GOOD_PLAN.split("\n\n", 1)[1]
        )
        results = {
            "good": fvr.check_zero_unit_plan(good, REPO_ROOT),
            "no_tests": fvr.check_zero_unit_plan(no_tests, REPO_ROOT),
            "no_targets": fvr.check_zero_unit_plan(no_targets, REPO_ROOT),
            "nontest": fvr.check_zero_unit_plan(nontest, REPO_ROOT),
        }
    ok = (results["good"] is None
          and all((results[k] or "").startswith(EXPECTED_PREFIX) for k in ("no_tests", "no_targets", "nontest")))
    return ok, repr(results)


def test_verifier_shares_targets_rules_with_plan_test_paths():
    """The verifier and plan-test-paths.py reject the same bad Targets (one shared implementation)."""
    bad_targets = ["/etc/passwd", "../outside.py", "scripts", "scripts/does-not-exist-526.py", "tests/test_x.py"]
    seen = {}
    with tempfile.TemporaryDirectory() as td:
        for i, target in enumerate(bad_targets):
            plan = Path(td) / f"p{i}.md"
            plan.write_text(GOOD_PLAN.replace("`scripts/unit-sizing.py`", f"`{target}`"))
            seen[target] = fvr.check_zero_unit_plan(plan, REPO_ROOT)
        nobt = Path(td) / "nobt.md"
        nobt.write_text(GOOD_PLAN.replace("`scripts/unit-sizing.py`", "scripts/unit-sizing.py"))
        seen["no-backticks"] = fvr.check_zero_unit_plan(nobt, REPO_ROOT)
    ok = all((v or "").startswith(EXPECTED_PREFIX) for v in seen.values())
    return ok, repr(seen)


def test_zero_units_constant_has_one_definition():
    scripts = REPO_ROOT / "scripts"
    defs = [f.name for f in scripts.glob("*.py") if re.search(r'^ZERO_UNITS_BASIS\s*=\s*"zero-units"', f.read_text(encoding="utf-8"), re.M)]
    return defs == ["zero_units.py"], repr(defs)


def test_zero_unit_preconditions_precede_test_changes_ask():
    text = _doc("commands/implement-with-haiku.md")
    marker = "Zero-unit preconditions run here, before any drafting or asking"
    start = text.index("**Implement step: existing-test-edit and existing-test-removal items")
    ask = text.index("e. **Ask.**", start)
    first = text.find(marker, start)
    return text.count(marker) == 2 and 0 < first < ask, f"count={text.count(marker)} first={first} ask={ask}"


def test_receipt_check_runs_for_implement_only():
    receipt = "Decision: OK\nSPLIT_BASIS: zero-units\n"
    with tempfile.TemporaryDirectory() as td:
        bad = Path(td) / "bad.md"
        bad.write_text("## Implementation Steps\n\n## Other\n")
        impl = fvr._check_zero_unit_receipt(receipt, "OK", bad, step="implement")
        fix = fvr._check_zero_unit_receipt(receipt, "OK", bad, step="fix")
        other = fvr._check_zero_unit_receipt("Decision: OK\nSPLIT_BASIS: parallel\n", "OK", bad, step="implement")
    ok = (impl or "").startswith(EXPECTED_PREFIX) and fix is None and other is None
    return ok, f"implement={impl!r} fix={fix!r} other={other!r}"


def test_expert_flow_passes_plan_for_implement():
    text = _doc("commands/expert-flow.md")
    return ('pass `--plan "<plan_path>"` for implement only' in text, "rule 8 verifier --plan wiring")


def test_render_sites_bind_receipt_split_basis():
    text = _doc("commands/expert-flow.md")
    sites = [m for m in re.findall(r"implement-ledger\.py\" render [^`]*`", text)]
    ok = len(sites) >= 2 and all('--split-basis "<SPLIT_BASIS>"' in s for s in sites)
    return ok, f"sites={sites}"


def test_stop_code_lists_agree_on_prefix():
    steps = _doc("scripts/flow_driver/steps.py")
    receipts = _doc("scripts/flow_driver/receipts.py")
    baseline = _doc("scripts/flow-stop-baseline.py")
    ok = all(EXPECTED_PREFIX in t for t in (steps, receipts, baseline))
    return ok, "zero-unit-plan-incomplete in steps, receipts, baseline"


def test_zero_unit_status_and_exemption_tokens():
    iwh = _doc("commands/implement-with-haiku.md")
    ref = _doc("prompts/test-cleanup-reference.md")
    statuses = iwh.split("**Statuses.**", 1)[1][:900]
    skip = iwh.split("A failing check whose only red tests are", 1)[1][:400]
    intro = iwh.split("Round 4 runs in a fixed order.", 1)[1][:900]
    ok = ("test-correct-zero-unit" in statuses and "test-correct-zero-unit" in skip
          and "test-correct-zero-unit" in intro and ref.count("test-correct-zero-unit") >= 2)
    return ok, "status list, round 4 intro, fix-loop skip, reference exemptions"


def test_audit_carveout_in_auditor_criteria_and_both_steps():
    text = _doc("commands/expert-flow.md")
    carve = "zero-unit `implement` or `fix` run" in text
    criteria = text.split("implement, fix: the implement-ledger output below", 1)[1][:1800]
    return carve and "SPLIT_BASIS: zero-units" in criteria and "not a reduced run" in criteria, "carve-out"


def test_inventory_row_45_names_this_test():
    row = [ln for ln in _doc("docs/v2-inventory/rules.md").splitlines() if ln.startswith("| 45 | Amendment (2026-11-23): zero-unit")]
    return len(row) == 1 and "tests/test_zero_unit_wiring.py" in row[0], repr(row)[:200]


TESTS = [
    ("ledger render/check: zero-units basis needed and honoured", test_ledger_render_and_check_with_zero_units),
    ("verifier plan cross-check returns typed prefix on every failure", test_verifier_plan_check_prefix_and_pass),
    ("verifier and plan-test-paths share the Targets rules", test_verifier_shares_targets_rules_with_plan_test_paths),
    ("ZERO_UNITS_BASIS has one definition", test_zero_units_constant_has_one_definition),
    ("zero-unit preconditions precede the Test changes ask", test_zero_unit_preconditions_precede_test_changes_ask),
    ("receipt cross-check runs for implement, never fails fix or other bases", test_receipt_check_runs_for_implement_only),
    ("expert-flow passes --plan for implement", test_expert_flow_passes_plan_for_implement),
    ("expert-flow render sites bind the receipt SPLIT_BASIS", test_render_sites_bind_receipt_split_basis),
    ("stop-code lists carry zero-unit-plan-incomplete", test_stop_code_lists_agree_on_prefix),
    ("test-correct-zero-unit in status list and exemptions", test_zero_unit_status_and_exemption_tokens),
    ("audit carve-out covers implement, fix and auditor criteria", test_audit_carveout_in_auditor_criteria_and_both_steps),
    ("inventory row 45 names its pinning test", test_inventory_row_45_names_this_test),
]

if __name__ == "__main__":
    h = Harness("ZERO-UNIT WIRING (#526)")
    for name, fn in TESTS:
        try:
            passed, msg = fn()
        except Exception:
            passed, msg = False, f"exception: {traceback.format_exc(limit=3)}"
        h.test_result(name, passed, msg if not passed else "")
    h.summarize_and_exit()
