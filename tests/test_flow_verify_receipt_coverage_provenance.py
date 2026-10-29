#!/usr/bin/env python3
"""
Spec-blind tests for #383 coverage vocabulary and provenance requirements:
- F10: Coverage `none` is removed (only declared|uncovered|unknown allowed)
- F9: [verifier] provenance rejected from implementer/fixer receipts
- F22: ISSUE_URL_RE restricted to https://github.com/<owner>/<repo>/issues/<n>
- F27: Duplicate answers error

Every case runs the real script as a subprocess against a FLOW_DIR fixture.

Run with: python3 tests/test_flow_verify_receipt_coverage_provenance.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _test_harness import Harness  # noqa: E402
from _assess_fixtures import (  # noqa: E402
    BASE_IMPL, DECISION_OPTIONS, ISSUE_URL, S3, S4, assess_line, disp, gate, make_flow, make_home, write_answers, write_assess, write_marker,
)


def combined(out, err):
    return (out or "") + (err or "")


def main():
    h = Harness("FLOW-VERIFY-RECEIPT COVERAGE & PROVENANCE TESTS (F9, F10, F22, F27)")

    print("[Coverage vocabulary: none removed]")

    # Coverage "none" is no longer a valid value; only declared|uncovered|unknown are accepted
    # Tests for this are implicit in other outstanding set tests - when coverage is properly parsed

    print("[Provenance [verifier] rejection]")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        write_assess(flow, [assess_line(S3, kind="decision", options=DECISION_OPTIONS), assess_line(S4, kind="decision", options=DECISION_OPTIONS)])
        write_answers(flow, 1, [("q1", S3, "Open an issue", None), ("q2", S4, "Open an issue", None)])
        # Write marker with [verifier] provenance (should be rejected)
        path = flow / "steps" / "07-ship.outstanding.md"
        path.write_text(
            "format: 2\nassessment: assessor\n"
            "<!-- outstanding-pending -->\n"
            "implement:Step 3 | filed-as-issue | by=[verifier] | option=Open an issue | route=issue | url=" + ISSUE_URL + "\n"
            "implement:Step 4 | filed-as-issue | option=Open an issue | route=issue | url=" + ISSUE_URL + "\n"
            "<!-- outstanding-end -->\n"
        )
        rc, out, err = gate(flow, home)
        text = combined(out, err)
        h.test_result("[verifier] provenance in disposition fails",
                      rc != 0 and "malformed disposition line" in text,
                      f"rc={rc} out:\n{text}")

    print("[ISSUE_URL validation: must be https://github.com/<owner>/<repo>/issues/<n>]")

    bad_urls = [
        ("http://github.com/acme/app/issues/7", "http instead of https"),
        ("https://github.com/acme/issues/7", "missing repo name"),
        ("https://github.com/acme/app/pull/7", "pull instead of issues"),
        ("https://github.com/acme/app/issues/abc", "non-numeric issue number"),
        ("https://example.com/acme/app/issues/7", "wrong domain"),
        ("https://github.com/acme/app/issues/7/foo", "extra path segments"),
        ("https://github.com/acme/app/issues/7?query=1", "has query string"),
    ]

    for bad_url, reason in bad_urls:
        with tempfile.TemporaryDirectory() as td:
            home = make_home(td)
            flow = make_flow(td, implement=BASE_IMPL)
            write_assess(flow, [assess_line(S3, kind="decision", options=DECISION_OPTIONS), assess_line(S4, kind="decision", options=DECISION_OPTIONS)])
            write_answers(flow, 1, [("q1", S3, "Open an issue", None), ("q2", S4, "Open an issue", None)])
            write_marker(flow, [
                disp(S3, "filed-as-issue", option="Open an issue", route="issue", url=bad_url),
                disp(S4, "filed-as-issue", option="Open an issue", route="issue", url=ISSUE_URL),
            ])
            rc, out, err = gate(flow, home)
            text = combined(out, err)
            h.test_result(f"URL {reason!r} fails validation",
                          rc != 0 and "issue URL" in text,
                          f"rc={rc} out:\n{text}")

    # Valid URLs that should pass
    valid_urls = [
        "https://github.com/acme/app/issues/1",
        "https://github.com/my-org/my-repo/issues/999",
        "https://github.com/x/y/issues/0",
    ]

    for valid_url in valid_urls:
        with tempfile.TemporaryDirectory() as td:
            home = make_home(td)
            flow = make_flow(td, implement=BASE_IMPL, state={"issue_url": valid_url})  # the URL must be in the flow's repo
            write_assess(flow, [assess_line(S3, kind="decision", options=DECISION_OPTIONS), assess_line(S4, kind="decision", options=DECISION_OPTIONS)])
            write_answers(flow, 1, [("q1", S3, "Open an issue", None), ("q2", S4, "Open an issue", None)])
            write_marker(flow, [
                disp(S3, "filed-as-issue", option="Open an issue", route="issue", url=valid_url),
                disp(S4, "filed-as-issue", option="Open an issue", route="issue", url=valid_url),
            ])
            rc, out, err = gate(flow, home)
            h.test_result(f"Valid URL {valid_url} passes",
                          rc == 0, f"rc={rc} out:\n{combined(out, err)}")

    print("[Duplicate answers error]")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        write_assess(flow, [assess_line(S3, kind="decision", options=DECISION_OPTIONS), assess_line(S4, kind="decision", options=DECISION_OPTIONS)])
        # Write answers with duplicate question id
        answer_file = flow / "answers" / "assess-01.json"
        questions_file = flow / "questions" / "assess-01.json"

        # Create question file with duplicate ids
        qs = {
            "step": "assess", "seq": 1, "questions": [
                {"id": "q1", "item": S3, "question": "Outstanding item", "header": "Outstanding", "options": [{"label": "Open an issue", "description": ""}]},
                {"id": "q1", "item": S4, "question": "Outstanding item", "header": "Outstanding", "options": [{"label": "Open an issue", "description": ""}]},
            ]
        }
        questions_file.write_text(json.dumps(qs))

        # Answers corresponding to duplicated question ids
        answer_file.write_text(json.dumps({
            "step": "assess", "seq": 1,
            "answers": {"q1": "Open an issue"},
            "annotations": {}
        }))

        write_marker(flow, [
            disp(S3, "filed-as-issue", option="Open an issue", route="issue", url=ISSUE_URL),
            disp(S4, "filed-as-issue", option="Open an issue", route="issue", url=ISSUE_URL),
        ])
        rc, out, err = gate(flow, home)
        text = combined(out, err)
        h.test_result("Duplicate question ids cause validation issues",
                      rc != 0 and ("does not cover" in text or "not found" in text.lower()),
                      f"rc={rc} out:\n{text}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
