#!/usr/bin/env python3
"""
Spec-blind tests for the #383 slice 1 outstanding gate in scripts/flow-verify-receipt.py
(--outstanding-gate --flow-dir F --receipt steps/07-ship.outstanding.md).

Covers the assessor/human disposition rules, answer-file binding, drop reasons, the taken-over
path, and the legacy (pre-amendment) marker path. Every case runs the real script as a subprocess.

Run with: python3 tests/test_flow_verify_receipt_outstanding_gate.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _test_harness import Harness  # noqa: E402
from _assess_fixtures import (  # noqa: E402
    BASE_IMPL, DECISION_OPTIONS, DROP_REASON, ISSUE_URL, S3, S4, S5, TS,
    assess_check, assess_line, disp, gate, make_flow, make_home, write_answers, write_assess, write_marker,
)

# Assessor option labels must not equal the fixed choice labels (rule 6), so the shared fixture labels are used.
OPTS = DECISION_OPTIONS


def combined(out, err):
    return (out or "") + (err or "")


def base(td, *, implement=BASE_IMPL, log=None):
    """Flow with S3 (uncovered) and S4 (declared) outstanding, a valid assess receipt, and answers
    q1 (S3) and q2 (S4) both labelled 'Open an issue'."""
    home = make_home(td)
    flow = make_flow(td, implement=implement, log=log)
    write_assess(flow, [assess_line(S3, kind="decision", options=OPTS), assess_line(S4, kind="decision", options=OPTS)])
    write_answers(flow, 1, [("q1", S3, "Open an issue", None), ("q2", S4, "Open an issue", None)])
    return home, flow


def good_lines(**overrides):
    s3 = overrides.get("s3", disp(S3, "filed-as-issue", option="Open an issue", route="issue", url=ISSUE_URL))
    s4 = overrides.get("s4", disp(S4, "filed-as-issue", option="Open an issue", route="issue", url=ISSUE_URL))
    return [s3, s4]


def legacy_flow(td, log=None):
    home = make_home(td)
    flow = make_flow(td, implement=BASE_IMPL, log=log)
    (flow / "steps" / "07-ship.md").write_text("Shipped\n<!-- step-end -->\n")
    return home, flow


def main():
    h = Harness("FLOW-VERIFY-RECEIPT OUTSTANDING GATE TESTS (#383 slice 1)")

    print("[Gate: presence and sentinel]")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement={"outstanding": [], "rows": ["Step 1 | covered | in diff"]})
        rc, out, err = gate(flow, home)
        h.test_result("empty outstanding set passes with no marker", rc == 0, f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home, flow = base(td)
        rc, out, err = gate(flow, home)
        h.test_result("non-empty set with no disposition marker fails 'no disposition marker'",
                      rc != 0 and "no disposition marker" in combined(out, err),
                      f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home, flow = base(td)
        write_marker(flow, good_lines())
        rc, out, err = gate(flow, home)
        h.test_result("valid assessor-format marker with human dispositions passes", rc == 0,
                      f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home, flow = base(td)
        write_marker(flow, [disp(S3, "filed-as-issue", option="Open an issue", route="issue", url=ISSUE_URL)])
        rc, out, err = gate(flow, home)
        text = combined(out, err)
        h.test_result("lying sentinel with an undispositioned item fails and names implement:Step 4",
                      rc != 0 and "undispositioned" in text.lower() and S4 in text, f"rc={rc} out:\n{text}")

    with tempfile.TemporaryDirectory() as td:
        home, flow = base(td)
        write_marker(flow, [disp(S3, "filed-as-issue", option="Open an issue", route="issue", url=ISSUE_URL)],
                     sentinel=False)
        rc, out, err = gate(flow, home)
        h.test_result("pending marker without sentinel fails 'marker incomplete'",
                      rc != 0 and "marker incomplete" in combined(out, err), f"rc={rc} out:\n{out}\nerr:\n{err}")
        h.test_result("pending marker prints UNDISPOSITIONED: implement:Step 4 on stdout",
                      [ln for ln in out.splitlines() if ln.startswith("UNDISPOSITIONED:")]
                      == [f"UNDISPOSITIONED: {S4}"], f"out:\n{out}")

    print()
    print("[Gate: assessor and human dispositions]")

    with tempfile.TemporaryDirectory() as td:
        home, flow = base(td)
        write_answers(flow, 1, [("q1", S3, "Drop", DROP_REASON), ("q2", S4, "Open an issue", None)])
        write_marker(flow, [
            disp(S3, "dropped", by="assessor-verified", answer_ref="none", option="Drop", reason=DROP_REASON),
            good_lines()[1],
        ])
        rc, out, err = gate(flow, home)
        h.test_result("assessor-verified drop fails (drops need a human answer)",
                      rc != 0 and "drop without human answer" in combined(out, err),
                      f"rc={rc} out:\n{out}\nerr:\n{err}")

    for label, reason in (("n/a", "n/a"), ("too short", "drop it")):
        with tempfile.TemporaryDirectory() as td:
            home, flow = base(td)
            write_answers(flow, 1, [("q1", S3, "Drop", reason), ("q2", S4, "Open an issue", None)])
            write_marker(flow, [
                disp(S3, "dropped", option="Drop", reason=reason),
                good_lines()[1],
            ])
            rc, out, err = gate(flow, home)
            h.test_result(f"drop with reason {label!r} fails",
                          rc != 0 and "drop reason too thin" in combined(out, err),
                          f"rc={rc} out:\n{out}\nerr:\n{err}")

    for n_chars, want_ok in ((14, False), (15, True)):
        reason = "x" * (n_chars - 1) + "y"
        with tempfile.TemporaryDirectory() as td:
            home, flow = base(td)
            write_answers(flow, 1, [("q1", S3, "Drop", reason), ("q2", S4, "Open an issue", None)])
            write_marker(flow, [
                disp(S3, "dropped", option="Drop", reason=reason),
                good_lines()[1],
            ])
            rc, out, err = gate(flow, home)
            h.test_result(f"drop reason of exactly {n_chars} chars {'passes' if want_ok else 'fails'}",
                          (rc == 0) == want_ok, f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home, flow = base(td)
        write_answers(flow, 1, [("q1", S3, "Drop", DROP_REASON), ("q2", S4, "Open an issue", None)])
        write_marker(flow, [
            disp(S3, "dropped", option="Drop", reason=DROP_REASON),
            good_lines()[1],
        ])
        rc, out, err = gate(flow, home)
        h.test_result("drop with a reason matching the answer notes passes", rc == 0,
                      f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home, flow = base(td)
        write_answers(flow, 1, [("q1", S3, "Drop", DROP_REASON), ("q2", S4, "Open an issue", None)])
        write_marker(flow, [
            disp(S3, "dropped", option="Drop", reason="Something else entirely here"),
            good_lines()[1],
        ])
        rc, out, err = gate(flow, home)
        h.test_result("drop reason differing from the answer fails 'drop reason differs from answer'",
                      rc != 0 and "drop reason differs from answer" in combined(out, err),
                      f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home, flow = base(td)
        write_marker(flow, [
            disp(S3, "filed-as-issue", option="Open an issue", route="issue", answer_ref="answers/assess-99.json", url=ISSUE_URL),
            good_lines()[1],
        ])
        rc, out, err = gate(flow, home)
        h.test_result("answer_ref naming a missing file fails",
                      rc != 0 and "answer file not found" in combined(out, err),
                      f"rc={rc} out:\n{out}\nerr:\n{err}")

    # Test grammar-valid ref that escapes via symlink (F17: reach the containment check)
    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        write_assess(flow, [assess_line(S3, kind="decision", options=OPTS), assess_line(S4, kind="decision", options=OPTS)])
        write_answers(flow, 1, [("q1", S3, "Open an issue", None), ("q2", S4, "Open an issue", None)])
        outside = Path(td) / "outside"
        outside.mkdir(parents=True, exist_ok=True)
        (outside / "assess-02.json").write_text(json.dumps({"step": "assess", "seq": 2, "answers": {"q1": "Open an issue"}}))
        # Create a symlink within answers/ that points outside FLOW_DIR
        symlink_path = flow / "answers" / "assess-02.json"
        symlink_path.symlink_to(outside / "assess-02.json")
        write_marker(flow, [
            disp(S3, "filed-as-issue", option="Open an issue", route="issue", answer_ref="answers/assess-02.json", url=ISSUE_URL),
            disp(S4, "filed-as-issue", option="Open an issue", route="issue", url=ISSUE_URL),
        ])
        rc, out, err = gate(flow, home)
        h.test_result("answer_ref outside FLOW_DIR (symlink escape) fails",
                      rc != 0 and "answer file outside FLOW_DIR" in combined(out, err),
                      f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home, flow = base(td)
        (flow / "answers" / "assess-01.json").write_text("{not json")
        write_marker(flow, good_lines())
        rc, out, err = gate(flow, home)
        h.test_result("invalid answers JSON fails",
                      rc != 0 and "cannot read answers" in combined(out, err),
                      f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home, flow = base(td)
        qf = flow / "questions" / "assess-01.json"
        data = json.loads(qf.read_text())
        for q in data["questions"]:
            q.pop("item", None)
        qf.write_text(json.dumps(data))
        write_marker(flow, good_lines())
        rc, out, err = gate(flow, home)
        h.test_result("question without an item field fails",
                      rc != 0 and "has no item field" in combined(out, err),
                      f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home, flow = base(td)
        write_answers(flow, 1, [("q2", S4, "Open an issue", None)])
        write_marker(flow, good_lines())
        rc, out, err = gate(flow, home)
        h.test_result("answer file not covering the item fails 'answer does not cover'",
                      rc != 0 and "answer does not cover" in combined(out, err), f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home, flow = base(td)
        write_answers(flow, 1, [("q1", S3, "Pause to finish by hand", None), ("q2", S4, "Open an issue", None)])
        write_marker(flow, good_lines())
        rc, out, err = gate(flow, home)
        h.test_result("disposition option differing from the answer label fails 'answer mismatch'",
                      rc != 0 and "answer mismatch" in combined(out, err), f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home, flow = base(td)
        write_answers(flow, 1, [("q1", S3, "Improvise", None), ("q2", S4, "Open an issue", None)])
        write_marker(flow, [
            disp(S3, "asked-and-answered", option="Improvise", route="issue", url=ISSUE_URL),
            good_lines()[1],
        ])
        rc, out, err = gate(flow, home)
        h.test_result("option label that is not one of the assess options fails 'not an issue-routed assessor option'",
                      rc != 0 and "not an issue-routed assessor option" in combined(out, err),
                      f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home, flow = base(td)
        write_answers(flow, 1, [("q1", S3, "File a follow-up", None), ("q2", S4, "Open an issue", None)])
        write_marker(flow, [
            disp(S3, "asked-and-answered", option="File a follow-up", route="issue", url=ISSUE_URL),
            good_lines()[1],
        ])
        rc, out, err = gate(flow, home)
        h.test_result("assessor option 'File a follow-up' routed issue with a URL passes", rc == 0,
                      f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home, flow = base(td)
        write_marker(flow, [
            disp(S3, "filed-as-issue", option="Open an issue", route="issue", url="none"),
            good_lines()[1],
        ])
        rc, out, err = gate(flow, home)
        h.test_result("filed-as-issue without a URL fails",
                      rc != 0 and "needs an issue URL" in combined(out, err),
                      f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        write_assess(flow, [
            assess_line(S3, kind="decision", options=OPTS, leave="no"),
            assess_line(S4, kind="decision", options=OPTS),
        ])
        write_answers(flow, 1, [("q1", S3, "Leave as-is", None), ("q2", S4, "Open an issue", None)])
        write_marker(flow, [
            disp(S3, "asked-and-answered", option="Leave as-is", route="leave"),
            good_lines()[1],
        ])
        rc, out, err = gate(flow, home)
        h.test_result("leave-as-is on an item with leave_as_is_safe=no fails", rc != 0,
                      f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        write_assess(flow, [
            assess_line(S3, status="already-done", evidence="commit 91373c8 adds the handler at src/api.py:12"),
            assess_line(S4, kind="decision", options=OPTS),
        ])
        write_answers(flow, 1, [("q1", S3, "Pause to finish by hand", None), ("q2", S4, "Open an issue", None)])
        write_marker(flow, [
            disp(S3, "asked-and-answered", option="Pause to finish by hand", route="pause"),
            good_lines()[1],
        ])
        rc, out, err = gate(flow, home)
        h.test_result("pause route passes when the item is already-done", rc == 0,
                      f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home, flow = base(td)
        write_answers(flow, 1, [("q1", S3, "Pause to finish by hand", None), ("q2", S4, "Open an issue", None)])
        write_marker(flow, [
            disp(S3, "asked-and-answered", option="Pause to finish by hand", route="pause"),
            good_lines()[1],
        ])
        rc, out, err = gate(flow, home)
        h.test_result("pause route on an unfinished item fails 'paused, not yet finished'",
                      rc != 0 and "paused, not yet finished" in combined(out, err),
                      f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement={"outstanding": [], "rows": [
            "Step 1 | covered | in diff", "Step 5 | unknown | could not derive coverage"]})
        write_assess(flow, [assess_line(S5, kind="decision", options=OPTS)])
        write_answers(flow, 1, [("q1", S5, "Open an issue", None)])
        write_marker(flow, [disp(S5, "finished", option="Open an issue", route="issue", url=ISSUE_URL)])
        rc, out, err = gate(flow, home)
        h.test_result("finished on a decision item fails (unknown coverage, unfinished decision)", rc != 0,
                      f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        write_assess(flow, [
            assess_line(S3, status="already-done", evidence="commit 91373c8 adds the handler at src/api.py:12"),
            assess_line(S4, kind="decision", options=OPTS),
        ])
        write_answers(flow, 1, [("q1", S3, "Confirm already done", None), ("q2", S4, "Open an issue", None)])
        write_marker(flow, [
            disp(S3, "finished", by="assessor-verified", answer_ref="none"),
            good_lines()[1],
        ])
        rc, out, err = gate(flow, home)
        h.test_result("assessor-verified finished on an uncovered row fails 'needs a human confirmation'",
                      rc != 0 and "needs a human confirmation" in combined(out, err),
                      f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        write_assess(flow, [
            assess_line(S3, status="already-done", evidence="human confirmed the route exists"),
            assess_line(S4, kind="decision", options=OPTS),
        ])
        write_answers(flow, 1, [("q1", S3, "Confirm already done", None), ("q2", S4, "Open an issue", None)])
        write_marker(flow, [
            disp(S3, "finished", by="human", option="Confirm already done"),
            good_lines()[1],
        ])
        rc, out, err = gate(flow, home)
        h.test_result("human 'Confirm already done' on an uncovered row passes", rc == 0,
                      f"rc={rc} out:\n{out}\nerr:\n{err}")

    print()
    print("[Gate: taken-over implement]")

    taken_log = [f"{TS} implement taken-over", f"{TS} assess taken-over"]

    def taken_flow(td, log=taken_log):
        home = make_home(td)
        flow = make_flow(td, implement=None, log=log)
        write_answers(flow, 1, [("q1", "implement:coverage-unavailable", "Open an issue", None)])
        return home, flow

    with tempfile.TemporaryDirectory() as td:
        home, flow = taken_flow(td)
        write_marker(flow, [disp("implement:coverage-unavailable", "filed-as-issue", option="Open an issue", route="issue", url=ISSUE_URL)],
                     assessment="taken-over")
        rc, out, err = gate(flow, home)
        h.test_result("taken-over baseline with a filed issue passes", rc == 0,
                      f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home, flow = taken_flow(td)
        write_marker(flow, [disp("implement:coverage-unavailable", "finished", option="Open an issue", route="issue", url=ISSUE_URL)],
                     assessment="taken-over")
        rc, out, err = gate(flow, home)
        h.test_result("taken-over marker with a finished line fails", rc != 0, f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home, flow = taken_flow(td, log=[f"{TS} implement taken-over"])
        write_marker(flow, [disp("implement:coverage-unavailable", "filed-as-issue", option="Open an issue", route="issue", url=ISSUE_URL)],
                     assessment="taken-over")
        rc, out, err = gate(flow, home)
        h.test_result("taken-over marker without an 'assess taken-over' log line fails", rc != 0,
                      f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home, flow = taken_flow(td)
        write_answers(flow, 1, [("q1", "implement:coverage-unavailable", "Leave as-is", None)])
        write_marker(flow, [disp("implement:coverage-unavailable", "asked-and-answered", option="Leave as-is", route="leave")],
                     assessment="taken-over")
        rc, out, err = gate(flow, home)
        h.test_result("taken-over marker with leave-as-is fails", rc != 0, f"rc={rc} out:\n{out}\nerr:\n{err}")

    # Opposite-direction tests for taken-over combinations (F37)
    with tempfile.TemporaryDirectory() as td:
        home, flow = taken_flow(td)
        write_marker(flow, [disp("implement:coverage-unavailable", "filed-as-issue", option="Open an issue", route="issue", url=ISSUE_URL)],
                     assessment="assessor")
        rc, out, err = gate(flow, home)
        h.test_result("assessor marker with a filed issue on taken-over item fails", rc != 0, f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home, flow = taken_flow(td, log=[f"{TS} implement taken-over", f"{TS} assess ok"])
        write_marker(flow, [disp("implement:coverage-unavailable", "filed-as-issue", option="Open an issue", route="issue", url=ISSUE_URL)],
                     assessment="taken-over")
        rc, out, err = gate(flow, home)
        h.test_result("taken-over marker with 'assess ok' instead of 'assess taken-over' log line fails", rc != 0,
                      f"rc={rc} out:\n{out}\nerr:\n{err}")

    print()
    print("[Gate: legacy pre-amendment markers]")

    legacy_ok = ["- Step 3: Open an issue " + ISSUE_URL, "- Step 4: Drop: superseded"]

    with tempfile.TemporaryDirectory() as td:
        home, flow = legacy_flow(td)
        write_marker(flow, legacy_ok, fmt=False)
        rc, out, err = gate(flow, home)
        h.test_result("legacy pre-amendment marker passes and reports OUTSTANDING_GATE: legacy",
                      rc == 0 and "OUTSTANDING_GATE: legacy" in out, f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home, flow = legacy_flow(td)
        write_marker(flow, legacy_ok, fmt=True)
        rc, out, err = gate(flow, home)
        h.test_result("format-2 header carrying unparseable legacy lines fails", rc != 0,
                      f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home, flow = legacy_flow(td)
        (flow / "state.json").write_text('{"outstanding_format": 2}')
        write_marker(flow, legacy_ok, fmt=False)
        rc, out, err = gate(flow, home)
        h.test_result("legacy marker in a flow with outstanding_format fails 'legacy marker not allowed'",
                      rc != 0 and "legacy marker not allowed" in combined(out, err), f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home, flow = legacy_flow(td)
        write_marker(flow, ["- Step 3: Open an issue " + ISSUE_URL], fmt=False)
        rc, out, err = gate(flow, home)
        h.test_result("legacy marker missing one id fails 'legacy marker does not cover'",
                      rc != 0 and "legacy marker does not cover" in combined(out, err), f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home, flow = legacy_flow(td)
        write_marker(flow, legacy_ok, fmt=False, sentinel=False)
        rc, out, err = gate(flow, home)
        h.test_result("legacy marker without the outstanding-end sentinel fails", rc != 0,
                      f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home, flow = legacy_flow(td, log=[f"{TS} implement ok", f"{TS} fix skipped", f"{TS} assess ok"])
        write_marker(flow, legacy_ok, fmt=False)
        rc, out, err = gate(flow, home)
        h.test_result("legacy marker with an assess log line fails", rc != 0, f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home, flow = legacy_flow(td)
        write_assess(flow, [assess_line(S3, kind="decision", options=OPTS), assess_line(S4, kind="decision", options=OPTS)])
        write_marker(flow, legacy_ok, fmt=False)
        rc, out, err = gate(flow, home)
        h.test_result("legacy marker with a 07-assess.md receipt present fails", rc != 0,
                      f"rc={rc} out:\n{out}\nerr:\n{err}")

    print()
    print("[Gate: assess receipt must pass its check]")

    with tempfile.TemporaryDirectory() as td:
        home, flow = base(td)
        write_assess(flow, [assess_line(S3, kind="chore", options=OPTS), assess_line(S4, kind="decision", options=OPTS)])
        write_marker(flow, good_lines())
        rc, out, err = gate(flow, home)
        h.test_result("gate fails when the assess receipt itself fails its check", rc != 0,
                      f"rc={rc} out:\n{out}\nerr:\n{err}")
        rc2, out2, err2 = assess_check(flow, home)
        h.test_result("the same bad assess receipt also fails the --step assess check", rc2 != 0,
                      f"rc={rc2} out:\n{out2}\nerr:\n{err2}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
