#!/usr/bin/env python3
"""
Tests for scripts/flow_driver/audit.py and assess.py: verdict parsing, Accept eligibility, assessor record parsing.

Run with: python3 tests/test_flow_driver_audit_assess.py
"""

import dataclasses
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from _test_harness import Harness  # noqa: E402
from test_flow_driver_gates import make_state  # noqa: E402

from scripts.flow_driver import assess, audit  # noqa: E402
from scripts.flow_driver.state import ParkedQuestion  # noqa: E402


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check(h: Harness, name: str, fn: Callable[[], None]) -> None:
    try:
        fn()
    except AssertionError as exc:
        h.test_result(name, False, str(exc) or "assertion failed")
    except Exception as exc:
        h.test_result(name, False, f"{type(exc).__name__}: {exc}")
    else:
        h.test_result(name, True)


def test_audit_verdicts() -> None:
    expect(isinstance(audit.parse_audit_verdict("pass"), audit.AuditPass), "pass")
    rej = audit.parse_audit_verdict("reject: reduced run (audit): x")
    expect(isinstance(rej, audit.AuditReject) and rej.reason.startswith("reduced run"), str(rej))
    for bad in ("", "reject:", "PASS maybe", "ok"):
        expect(isinstance(audit.parse_audit_verdict(bad), audit.AuditError), f"{bad!r} must be an error")


def test_audit_receipt_path_is_distinct() -> None:
    p = audit.audit_receipt_path(Path("/f"), "plan", 2)
    expect(p == Path("/f/steps/audit-plan-a2.md"), str(p))


def rejected_state(**kw: object):  # type: ignore[no-untyped-def]
    parked = ParkedQuestion(step="plan", attempt=1, nn=1, kind="audit", item=None)
    base = dict(
        status="failed", step="plan", stop_reason={"plan": "audit-rejected: reduced run"},
        stop_source={"plan": "audit"}, parked=parked,
    )
    base.update(kw)
    return make_state(**base)


def test_accept_eligibility_fails_closed() -> None:
    expect(audit.lifecycle_check_audit_eligible(rejected_state(), "plan"), "baseline must be eligible")
    s = rejected_state()
    expect(not audit.lifecycle_check_audit_eligible(dataclasses.replace(s, status="paused"), "plan"), "status")
    expect(not audit.lifecycle_check_audit_eligible(dataclasses.replace(s, stop_reason={"plan": "step-failed: x"}), "plan"), "code")
    expect(not audit.lifecycle_check_audit_eligible(dataclasses.replace(s, stop_source={"plan": "receipt"}), "plan"), "source")
    expect(not audit.lifecycle_check_audit_eligible(dataclasses.replace(s, parked=None), "plan"), "parked")
    expect(not audit.lifecycle_check_audit_eligible(s, "review"), "other step")


def test_rejection_question_offers_retry_always() -> None:
    q = audit.build_audit_rejection_question("whatever", None, None)
    expect(q["options"] == ["Retry"], f"no allowlist means Retry only, got {q['options']}")


def test_assessor_records() -> None:
    line = "i1 | status=open | kind=fix | scope=isolated | leave_as_is_safe=true | cause=c | options=a, b"
    recs, err = assess.parse_assessor_output(line)
    expect(err is None and len(recs) == 1 and recs[0].options == ["a", "b"] and recs[0].leave_as_is_safe, f"{recs} {err}")
    _, err = assess.parse_assessor_output(line + "\n" + line)
    expect(err is not None and "duplicate" in err, "duplicate ids fail closed")
    _, err = assess.parse_assessor_output("garbage")
    expect(err is not None, "a line with no fields fails")


SECTIONS = [
    ("[Audit]", [
        ("1: verdict parsing", test_audit_verdicts),
        ("2: audit receipt path never collides with a step receipt", test_audit_receipt_path_is_distinct),
        ("3: Accept eligibility fails closed", test_accept_eligibility_fails_closed),
        ("4: rejection question offers Retry", test_rejection_question_offers_retry_always),
    ]),
    ("[Assess]", [
        ("5: assessor records", test_assessor_records),
    ]),
]


def main() -> None:
    h = Harness("FLOW DRIVER AUDIT AND ASSESS")
    for title, checks in SECTIONS:
        print(title)
        for name, fn in checks:
            check(h, name, fn)
        print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
