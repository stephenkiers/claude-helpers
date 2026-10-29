#!/usr/bin/env python3
"""
Spec-blind tests for the #383 ledger-gate requirement (F2): script-owned ledger records.

After an answers file is applied, a flow-ledger record must be appended to <flow>/human.jsonl
with kind=answer, step=assess, text carrying answer_ref and sha256.
Outstanding-gate must fail when:
- A by=human disposition's answer_ref has no corresponding ledger record
- The sha256 in the ledger record does not match the answer file bytes
- The ledger file is unreadable/malformed

Every case runs the real script as a subprocess against a FLOW_DIR fixture.

Run with: python3 tests/test_flow_verify_receipt_ledger_gate.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _test_harness import Harness  # noqa: E402
from _assess_fixtures import (  # noqa: E402
    BASE_IMPL, DECISION_OPTIONS, ISSUE_URL, S3, S4, TS,
    assess_line, disp, gate, make_flow, make_home, write_answers, write_assess, write_marker,
)


def combined(out, err):
    return (out or "") + (err or "")


def main():
    h = Harness("FLOW-VERIFY-RECEIPT LEDGER GATE TESTS (F2)")

    print("[Ledger gate: presence and sha256 validation]")

    # Helper: Create answer file without automatic ledger record
    def make_answers_no_ledger(flow, seq, entries):
        questions = []
        answers = {}
        annotations = {}
        for qid, item, label, notes in entries:
            questions.append({
                "id": qid, "item": item, "question": f"Outstanding item {item}", "header": "Outstanding",
                "options": [{"label": label, "description": ""}],
            })
            answers[qid] = label
            if notes is not None:
                annotations[qid] = {"notes": notes}
        (flow / "questions" / f"assess-{seq:02d}.json").write_text(
            json.dumps({"step": "assess", "seq": seq, "questions": questions}))
        (flow / "answers" / f"assess-{seq:02d}.json").write_text(
            json.dumps({"step": "assess", "seq": seq, "answers": answers, "annotations": annotations}))

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        write_assess(flow, [assess_line(S3, kind="decision", options=DECISION_OPTIONS), assess_line(S4, kind="decision", options=DECISION_OPTIONS)])
        # Write answers without ledger record
        make_answers_no_ledger(flow, 1, [("q1", S3, "Open an issue", None), ("q2", S4, "Open an issue", None)])
        write_marker(flow, [
            disp(S3, "filed-as-issue", option="Open an issue", route="issue", url=ISSUE_URL),
            disp(S4, "filed-as-issue", option="Open an issue", route="issue", url=ISSUE_URL),
        ])
        rc, out, err = gate(flow, home)
        text = combined(out, err)
        h.test_result("by=human disposition without ledger record fails",
                      rc != 0 and ("ledger" in text.lower() or "record" in text.lower()),
                      f"rc={rc} out:\n{text}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        write_assess(flow, [assess_line(S3, kind="decision", options=DECISION_OPTIONS), assess_line(S4, kind="decision", options=DECISION_OPTIONS)])
        # Write answers with ledger record
        write_answers(flow, 1, [("q1", S3, "Open an issue", None), ("q2", S4, "Open an issue", None)], ledger=True)
        write_marker(flow, [
            disp(S3, "filed-as-issue", option="Open an issue", route="issue", url=ISSUE_URL),
            disp(S4, "filed-as-issue", option="Open an issue", route="issue", url=ISSUE_URL),
        ])
        rc, out, err = gate(flow, home)
        h.test_result("by=human disposition with valid ledger record passes", rc == 0,
                      f"rc={rc} out:\n{out}\nerr:\n{err}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        write_assess(flow, [assess_line(S3, kind="decision", options=DECISION_OPTIONS), assess_line(S4, kind="decision", options=DECISION_OPTIONS)])
        # Write answers file manually
        make_answers_no_ledger(flow, 1, [("q1", S3, "Open an issue", None), ("q2", S4, "Open an issue", None)])
        answer_file = flow / "answers" / "assess-01.json"

        # Create ledger with wrong sha256
        wrong_sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        ledger_file = flow / "human.jsonl"
        record = {
            "seq": 1,
            "ts": TS,
            "step": "assess",
            "attempt": 1,
            "kind": "answer",
            "text": f"answer_ref: answers/assess-01.json\nsha256: {wrong_sha}",
            "transcript": None,
            "status": "addressed",
            "addressed_by": "steps/07-assess.md",
            "supersedes": None,
        }
        with open(ledger_file, "a") as f:
            f.write(json.dumps(record) + "\n")

        write_marker(flow, [
            disp(S3, "filed-as-issue", option="Open an issue", route="issue", url=ISSUE_URL),
            disp(S4, "filed-as-issue", option="Open an issue", route="issue", url=ISSUE_URL),
        ])
        rc, out, err = gate(flow, home)
        text = combined(out, err)
        h.test_result("ledger record with mismatched sha256 fails",
                      rc != 0 and ("sha256" in text.lower() or "checksum" in text.lower()),
                      f"rc={rc} out:\n{text}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        write_assess(flow, [assess_line(S3, kind="decision", options=DECISION_OPTIONS), assess_line(S4, kind="decision", options=DECISION_OPTIONS)])
        write_answers(flow, 1, [("q1", S3, "Open an issue", None), ("q2", S4, "Open an issue", None)], ledger=True)
        # Corrupt the ledger file
        (flow / "human.jsonl").write_text("{not valid json\n")
        write_marker(flow, [
            disp(S3, "filed-as-issue", option="Open an issue", route="issue", url=ISSUE_URL),
            disp(S4, "filed-as-issue", option="Open an issue", route="issue", url=ISSUE_URL),
        ])
        rc, out, err = gate(flow, home)
        text = combined(out, err)
        h.test_result("unreadable/malformed ledger fails closed",
                      rc != 0 and ("ledger" in text.lower() or "human.jsonl" in text),
                      f"rc={rc} out:\n{text}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        write_assess(flow, [assess_line(S3, kind="decision", options=DECISION_OPTIONS), assess_line(S4, kind="decision", options=DECISION_OPTIONS)])
        write_answers(flow, 1, [("q1", S3, "Open an issue", None), ("q2", S4, "Open an issue", None)], ledger=True)
        # Create a marker with answer_ref pointing to a different file
        write_marker(flow, [
            disp(S3, "filed-as-issue", option="Open an issue", route="issue", answer_ref="answers/assess-02.json", url=ISSUE_URL),
            disp(S4, "filed-as-issue", option="Open an issue", route="issue", url=ISSUE_URL),
        ])
        rc, out, err = gate(flow, home)
        text = combined(out, err)
        h.test_result("answer_ref mismatched with actual file fails (no ledger record)",
                      rc != 0 and ("ledger" in text.lower() or "record" in text.lower() or "assess-02" in text),
                      f"rc={rc} out:\n{text}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
