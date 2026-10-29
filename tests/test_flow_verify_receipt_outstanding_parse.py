#!/usr/bin/env python3
"""
Spec-blind tests for the #383 outstanding line parsing requirement (F8).

Malformed OUTSTANDING lines that fail grammar checks must raise an error, not silently skip.
Valid OUTSTANDING grammar: "OUTSTANDING: <item> | <provenance> | <coverage> | <reason>"
Invalid lines (missing fields, wrong format) must fail closed.
Also tests unterminated marker lines (F26): trailing partial marker line means incomplete marker, not crash.

Every case runs the real script as a subprocess against a FLOW_DIR fixture.

Run with: python3 tests/test_flow_verify_receipt_outstanding_parse.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _test_harness import Harness  # noqa: E402
from _assess_fixtures import (  # noqa: E402
    BASE_IMPL, DECISION_OPTIONS, ISSUE_URL, S3, S4, assess_line, disp, gate, make_flow, make_home, write_answers, write_assess,
)


def combined(out, err):
    return (out or "") + (err or "")


def main():
    h = Harness("FLOW-VERIFY-RECEIPT OUTSTANDING PARSE TESTS (F8, F26)")

    print("[Outstanding line parsing: malformed lines raise error]")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        write_assess(flow, [assess_line(S3, kind="decision", options=DECISION_OPTIONS), assess_line(S4, kind="decision", options=DECISION_OPTIONS)])
        write_answers(flow, 1, [("q1", S3, "Open an issue", None), ("q2", S4, "Open an issue", None)])
        # Write marker with malformed OUTSTANDING line (missing fields)
        path = flow / "steps" / "07-ship.outstanding.md"
        path.write_text(
            "format: 2\nassessment: assessor\n"
            "<!-- outstanding-pending -->\n"
            "implement:Step 3 | filed-as-issue\n"  # Missing fields: coverage, reason
            "implement:Step 4 | filed-as-issue | option=Open an issue | route=issue | url=" + ISSUE_URL + "\n"
            "<!-- outstanding-end -->\n"
        )
        rc, out, err = gate(flow, home)
        text = combined(out, err)
        h.test_result("OUTSTANDING line with missing fields raises error",
                      rc != 0 and "malformed disposition line" in text,
                      f"rc={rc} out:\n{text}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        write_assess(flow, [assess_line(S3, kind="decision", options=DECISION_OPTIONS), assess_line(S4, kind="decision", options=DECISION_OPTIONS)])
        write_answers(flow, 1, [("q1", S3, "Open an issue", None), ("q2", S4, "Open an issue", None)])
        # Write marker with line that looks like OUTSTANDING but is invalid
        path = flow / "steps" / "07-ship.outstanding.md"
        path.write_text(
            "format: 2\nassessment: assessor\n"
            "<!-- outstanding-pending -->\n"
            "OUTSTANDING: some text that is not formatted right\n"  # Looks like OUTSTANDING but invalid
            "implement:Step 3 | filed-as-issue | option=Open an issue | route=issue | url=" + ISSUE_URL + "\n"
            "implement:Step 4 | filed-as-issue | option=Open an issue | route=issue | url=" + ISSUE_URL + "\n"
            "<!-- outstanding-end -->\n"
        )
        rc, out, err = gate(flow, home)
        text = combined(out, err)
        h.test_result("Line with OUTSTANDING: prefix but invalid format raises error",
                      rc != 0 and "malformed disposition line" in text,
                      f"rc={rc} out:\n{text}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        write_assess(flow, [assess_line(S3, kind="decision", options=DECISION_OPTIONS), assess_line(S4, kind="decision", options=DECISION_OPTIONS)])
        write_answers(flow, 1, [("q1", S3, "Open an issue", None), ("q2", S4, "Open an issue", None)])
        # Write marker with an empty OUTSTANDING line
        path = flow / "steps" / "07-ship.outstanding.md"
        path.write_text(
            "format: 2\nassessment: assessor\n"
            "<!-- outstanding-pending -->\n"
            "OUTSTANDING: \n"  # Empty OUTSTANDING line
            "implement:Step 3 | filed-as-issue | option=Open an issue | route=issue | url=" + ISSUE_URL + "\n"
            "implement:Step 4 | filed-as-issue | option=Open an issue | route=issue | url=" + ISSUE_URL + "\n"
            "<!-- outstanding-end -->\n"
        )
        rc, out, err = gate(flow, home)
        text = combined(out, err)
        h.test_result("Empty OUTSTANDING line raises error",
                      rc != 0 and "malformed disposition line" in text,
                      f"rc={rc} out:\n{text}")

    print()
    print("[Unterminated marker line: incomplete marker handling]")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        write_assess(flow, [assess_line(S3, kind="decision", options=DECISION_OPTIONS), assess_line(S4, kind="decision", options=DECISION_OPTIONS)])
        write_answers(flow, 1, [("q1", S3, "Open an issue", None), ("q2", S4, "Open an issue", None)])
        # Write marker without closing sentinel (incomplete)
        path = flow / "steps" / "07-ship.outstanding.md"
        path.write_text(
            "format: 2\nassessment: assessor\n"
            "<!-- outstanding-pending -->\n"
            "implement:Step 3 | filed-as-issue | option=Open an issue | route=issue | url=" + ISSUE_URL + "\n"
            "implement:Step 4 | filed-as-issue | option=Open an issue | route=issue | url=" + ISSUE_URL + "\n"
            # No "<!-- outstanding-end -->" - incomplete marker
        )
        rc, out, err = gate(flow, home)
        text = combined(out, err)
        h.test_result("Unterminated marker (no sentinel) fails",
                      rc != 0 and "malformed disposition line" in text,
                      f"rc={rc} out:\n{text}")

    with tempfile.TemporaryDirectory() as td:
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        write_assess(flow, [assess_line(S3, kind="decision", options=DECISION_OPTIONS), assess_line(S4, kind="decision", options=DECISION_OPTIONS)])
        write_answers(flow, 1, [("q1", S3, "Open an issue", None), ("q2", S4, "Open an issue", None)])
        # Write marker with partial/truncated last line
        path = flow / "steps" / "07-ship.outstanding.md"
        path.write_text(
            "format: 2\nassessment: assessor\n"
            "<!-- outstanding-pending -->\n"
            "implement:Step 3 | filed-as-issue | option=Open an issue | route=issue | url=" + ISSUE_URL + "\n"
            "implement:Step 4 | filed-as-issue | option=Open an issue | route=issue | url=" + ISSUE_URL + "\n"
            "<!-- outstanding-en"  # Truncated sentinel - incomplete marker
        )
        rc, out, err = gate(flow, home)
        text = combined(out, err)
        h.test_result("Truncated sentinel line means incomplete marker, not parse crash",
                      rc != 0 and "malformed disposition line" in text,
                      f"rc={rc} out:\n{text}")

    print("[Content after the sentinel (D26)]")

    def sentinel_flow(td, extra):
        home = make_home(td)
        flow = make_flow(td, implement=BASE_IMPL)
        write_assess(flow, [assess_line(S3, kind="decision", options=DECISION_OPTIONS), assess_line(S4, kind="decision", options=DECISION_OPTIONS)])
        write_answers(flow, 1, [("q1", S3, "Open an issue", None), ("q2", S4, "Open an issue", None)])
        path = flow / "steps" / "07-ship.outstanding.md"
        path.write_text(
            "format: 2\nassessment: assessor\n<!-- outstanding-pending -->\n"
            + disp(S3, "filed-as-issue", option="Open an issue", route="issue", url=ISSUE_URL) + "\n"
            + disp(S4, "filed-as-issue", option="Open an issue", route="issue", url=ISSUE_URL) + "\n"
            + "<!-- outstanding-end -->\n" + extra
        )
        return home, flow

    with tempfile.TemporaryDirectory() as td:
        home, flow = sentinel_flow(td, "")
        baseline_rc, _, _ = gate(flow, home)
    with tempfile.TemporaryDirectory() as td:
        home, flow = sentinel_flow(td, "some trailing note that is not a disposition\n")
        rc, out, err = gate(flow, home)
        h.test_result("a non-disposition junk line after the sentinel leaves the result unchanged",
                      baseline_rc == 0 and rc == baseline_rc, f"baseline={baseline_rc} rc={rc} out:\n{combined(out, err)}")
    with tempfile.TemporaryDirectory() as td:
        home, flow = sentinel_flow(td, disp("implement:Step 5", "dropped") + "\n")
        rc, out, err = gate(flow, home)
        h.test_result("a disposition-shaped line after the sentinel errors 'content after the sentinel'",
                      rc != 0 and "content after the sentinel" in combined(out, err), f"rc={rc} out:\n{combined(out, err)}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
