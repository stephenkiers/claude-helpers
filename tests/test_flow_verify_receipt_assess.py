#!/usr/bin/env python3
"""
Spec-blind tests for the #383 slice 1 assess stage in scripts/flow-verify-receipt.py:

- the read-only outstanding set (--outstanding-set): ids, provenance, coverage, skipped/taken-over/STUB
  and exit-2 rules, runnable without --step/--receipt;
- the --step assess receipt check (check_assess): id-set equality, evidence, mechanical finish fields,
  unknown-coverage rules, decision options and their routes.

Every case runs the real script as a subprocess against a FLOW_DIR fixture built in a temp dir.

Run with: python3 tests/test_flow_verify_receipt_assess.py
"""

import importlib.util
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _test_harness import Harness  # noqa: E402
from _assess_fixtures import (  # noqa: E402
    BASE_IMPL, DECISION_OPTIONS, S3, S4, S5, TS,
    assess_check, assess_line, make_flow, make_home, outstanding_set, parse_outstanding, write_assess,
)


def combined(out, err):
    return (out or "") + (err or "")


def main():
    h = Harness("FLOW-VERIFY-RECEIPT ASSESS STAGE TESTS (#383 slice 1)")

    print("[Outstanding set: --outstanding-set]")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        rc, out, err = outstanding_set(flow, home)
        count, rows = parse_outstanding(out)
        passed = (
            rc == 0 and count == 2
            and rows.get(S3, ("", "", ""))[:2] == ("verifier", "uncovered")
            and rows.get(S4, ("", "", ""))[:2] == ("self-report", "declared")
        )
        h.test_result("outstanding set prints prefixed ids with provenance, coverage, and ITEMS count",
                      passed, f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        rc, out, err = outstanding_set(flow, home)
        passed = rc == 0 and "ITEMS: 2" in out and "required" not in err.lower()
        h.test_result("outstanding set runs without --step or --receipt", passed, f"rc={rc} err={err!r}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(
            td,
            implement={"outstanding": [], "rows": ["Step 1 | covered | in diff"]},
            fix={"outstanding": ["D2 (deferred) [self-report]"], "rows": ["D2 | declared | deferred", "D3 | uncovered | not in diff"]},
            log=[f"{TS} implement ok", f"{TS} fix ok"],
        )
        rc, out, err = outstanding_set(flow, home)
        count, rows = parse_outstanding(out)
        passed = (
            rc == 0 and count == 2
            and rows.get("fix:D2", ("", "", ""))[:2] == ("self-report", "declared")
            and rows.get("fix:D3", ("", "", ""))[:2] == ("verifier", "uncovered")
        )
        h.test_result("fix units are prefixed fix:D<n> and implement covered rows are not items",
                      passed, f"rc={rc} out:\n{out}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=None, log=[f"{TS} implement taken-over"])
        rc, out, err = outstanding_set(flow, home)
        count, rows = parse_outstanding(out)
        row = rows.get("implement:coverage-unavailable")
        passed = rc == 0 and row is not None and row[1] == "unknown"
        h.test_result("taken-over step with no coverage yields implement:coverage-unavailable (unknown)",
                      passed, f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL, fix=None, log=[f"{TS} implement ok", f"{TS} fix skipped"])
        rc, out, err = outstanding_set(flow, home)
        count, rows = parse_outstanding(out)
        passed = rc == 0 and not any(k.startswith("fix:") for k in rows) and count == 2
        h.test_result("skipped fix (newest log line) contributes nothing and exits 0",
                      passed, f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement={"outstanding": [], "rows_raw": "STUB: superseded before audit\n"})
        rc, out, err = outstanding_set(flow, home)
        count, rows = parse_outstanding(out)
        row = rows.get("implement:coverage-unavailable")
        passed = rc == 0 and row is not None and row[1] == "unknown"
        h.test_result("STUB coverage on an ok step yields coverage-unavailable (unknown), not exit 2",
                      passed, f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement={"outstanding": [], "rows": ["Step 3 | uncovered | x"], "receipt": False})
        rc, out, err = outstanding_set(flow, home)
        passed = rc == 2 and "03-implement" in combined(out, err)
        h.test_result("missing receipt for a step the log shows ok exits 2 naming the file",
                      passed, f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement={"outstanding": [], "rows_raw": "this is not a coverage table\n"})
        rc, out, err = outstanding_set(flow, home)
        passed = rc == 2 and "03-implement" in combined(out, err)
        h.test_result("unparseable coverage file on an ok step exits 2 naming the file",
                      passed, f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement={"outstanding": [], "rows": ["Step 1 | covered | in diff"]})
        rc, out, err = outstanding_set(flow, home)
        count, rows = parse_outstanding(out)
        passed = rc == 0 and "ITEMS: 0" in out and count == 0
        h.test_result("nothing outstanding prints ITEMS: 0 and exits 0", passed, f"rc={rc} out:\n{out}")

    print()
    print("[Assess receipt: --step assess]")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        write_assess(flow, [
            assess_line(S3, kind="decision", options=DECISION_OPTIONS),
            assess_line(S4),
        ])
        rc, out, err = assess_check(flow, home)
        h.test_result("a valid assess receipt passes (verifier-provenance item included)",
                      rc == 0, f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        write_assess(flow, [
            assess_line(S3, kind="decision", options=DECISION_OPTIONS),
            assess_line(S4, status="already-done", evidence="commit 91373c8 adds the handler at src/api.py:12"),
        ])
        rc, out, err = assess_check(flow, home)
        h.test_result("an already-done item with cited evidence passes", rc == 0, f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        write_assess(flow, [
            assess_line(S3, kind="decision", options=DECISION_OPTIONS),
            assess_line(S4, kind="chore"),
        ])
        rc, out, err = assess_check(flow, home)
        text = combined(out, err)
        h.test_result("unknown kind value fails and names the offending line",
                      rc != 0 and S4 in text, f"rc={rc} out:\n{text}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        write_assess(flow, [assess_line(S4)])
        rc, out, err = assess_check(flow, home)
        text = combined(out, err)
        h.test_result("one missing id fails as 'missing implement:Step 3'",
                      rc != 0 and f"missing {S3}" in text, f"rc={rc} out:\n{text}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        write_assess(flow, [
            assess_line(S3, kind="decision", options=DECISION_OPTIONS),
            assess_line(S4),
            assess_line("fix:D9"),
        ])
        rc, out, err = assess_check(flow, home)
        text = combined(out, err)
        h.test_result("one extra id fails as 'extra fix:D9'",
                      rc != 0 and "extra fix:D9" in text, f"rc={rc} out:\n{text}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        write_assess(flow, [
            assess_line(S3, kind="decision", options=DECISION_OPTIONS),
            assess_line(S4, finish_plan=""),
        ])
        rc, out, err = assess_check(flow, home)
        h.test_result("unfinished+mechanical with empty finish_plan fails (rule 4)",
                      rc != 0 and "missing finish_plan" in combined(out, err),
                      f"rc={rc} out:\n{combined(out, err)}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        write_assess(flow, [
            assess_line(S3, kind="decision", options=DECISION_OPTIONS),
            assess_line(S4, verification_proof=""),
        ])
        rc, out, err = assess_check(flow, home)
        h.test_result("unfinished+mechanical with empty verification_proof fails (rule 4)",
                      rc != 0 and "missing verification_proof" in combined(out, err),
                      f"rc={rc} out:\n{combined(out, err)}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        write_assess(flow, [
            assess_line(S3, kind="decision", options=DECISION_OPTIONS),
            assess_line(S4, status="already-done", evidence=""),
        ])
        rc, out, err = assess_check(flow, home)
        h.test_result("already-done with empty evidence fails",
                      rc != 0 and "evidence is empty" in combined(out, err),
                      f"rc={rc} out:\n{combined(out, err)}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement={"outstanding": [], "rows": ["Step 1 | covered | in diff", "Step 5 | unknown | could not derive coverage"]},
                         log=[f"{TS} implement ok", f"{TS} fix skipped"])
        write_assess(flow, [assess_line(S5, kind="mechanical")])
        rc, out, err = assess_check(flow, home)
        h.test_result("unknown-coverage item assessed mechanical fails (D2)",
                      rc != 0 and "must be unfinished+decision" in combined(out, err),
                      f"rc={rc} out:\n{combined(out, err)}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement={"outstanding": [], "rows": ["Step 1 | covered | in diff", "Step 5 | unknown | could not derive coverage"]},
                         log=[f"{TS} implement ok", f"{TS} fix skipped"])
        write_assess(flow, [assess_line(S5, status="already-done")])
        rc, out, err = assess_check(flow, home)
        h.test_result("unknown-coverage item assessed already-done fails (D2)",
                      rc != 0 and "must be unfinished+decision" in combined(out, err),
                      f"rc={rc} out:\n{combined(out, err)}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement={"outstanding": [], "rows": ["Step 1 | covered | in diff", "Step 5 | unknown | could not derive coverage"]},
                         log=[f"{TS} implement ok", f"{TS} fix skipped"])
        write_assess(flow, [assess_line(S5, kind="decision", options=DECISION_OPTIONS)])
        rc, out, err = assess_check(flow, home)
        h.test_result("unknown-coverage item assessed unfinished+decision with options passes (D2 always asked)",
                      rc == 0, f"rc={rc} out:\n{combined(out, err)}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        write_assess(flow, [assess_line(S3, kind="decision", options=""), assess_line(S4)])
        rc, out, err = assess_check(flow, home)
        h.test_result("unfinished+decision with no options fails (rule 6)",
                      rc != 0 and "needs 1-2 options" in combined(out, err),
                      f"rc={rc} out:\n{combined(out, err)}")

    bad_options = {
        "option with no route": "Patch the gap now",
        "route other than pause/issue": "Patch the gap now => finish",
        "three options": "A one => pause ;; B two => issue ;; C three => pause",
        "label equal to Drop": "Drop => issue",
        "label equal to Open an issue": "Open an issue => issue",
    }
    for label, options in bad_options.items():
        with tempfile.TemporaryDirectory() as td:
            home = make_home(td)
            flow = make_flow(td, implement=BASE_IMPL)
            write_assess(flow, [assess_line(S3, kind="decision", options=options), assess_line(S4)])
            rc, out, err = assess_check(flow, home)
            h.test_result(f"malformed option set fails ({label})",
                          rc != 0 and ("malformed" in combined(out, err).lower() or S3 in combined(out, err)),
                          f"options={options!r} rc={rc} out:\n{combined(out, err)}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        lines = [
            assess_line(S3, kind="decision", options=DECISION_OPTIONS),
            assess_line(S4),
        ]
        write_assess(flow, lines, end_marker=False)
        rc, out, err = assess_check(flow, home)
        h.test_result("a receipt without <!-- assessment-end --> fails", rc != 0,
                      f"rc={rc} out:\n{combined(out, err)}")

    print("[Literal-derived tuples (D28)]")
    spec = importlib.util.spec_from_file_location(
        "flow_verify_receipt_under_test", Path(__file__).resolve().parent.parent / "scripts" / "flow-verify-receipt.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    expected = {
        "_ASSESS_STATUSES": ("unfinished", "already-done"),
        "_ASSESS_KINDS": ("mechanical", "decision"),
        "_ASSESS_SCOPES": ("in-scope", "separate-ticket"),
        "_LEAVE_SAFE_VALUES": ("yes", "no"),
        "_OPTION_ROUTES": ("pause", "issue"),
        "_DISPOSITIONS": ("finished", "asked-and-answered", "filed-as-issue", "dropped"),
        "_DISPOSITION_BYS": ("human", "assessor-verified"),
        "_DISPOSITION_ROUTES": ("leave", "pause", "issue", "none"),
        "_ASSESSMENT_MODES": ("assessor", "taken-over"),
    }
    for name, want in expected.items():
        got = getattr(mod, name, None)
        h.test_result(f"{name} equals the expected ordered values", got == want, f"got {got!r}, want {want!r}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
