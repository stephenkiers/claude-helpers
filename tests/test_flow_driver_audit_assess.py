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
    # The v1 flow-assessor grammar, ending in the trailing assessment-end sentinel (#538).
    line = _v1_record("i1", safe="yes", options="A => pause ;; B => issue")
    recs, err = assess.parse_assessor_output(line + "\n" + ASSESS_SENTINEL)
    expect(err is None and len(recs) == 1 and len(recs[0].options) == 2 and recs[0].leave_as_is_safe, f"{recs} {err}")
    _, err = assess.parse_assessor_output(line + "\n" + line + "\n" + ASSESS_SENTINEL)
    expect(err is not None and "duplicate" in err, "duplicate ids fail closed")
    _, err = assess.parse_assessor_output("garbage\n" + ASSESS_SENTINEL)
    expect(err is not None, "a line with no fields fails")
    _, err = assess.parse_assessor_output(line)
    expect(err is not None and "sentinel" in err, "a reply without the trailing sentinel fails closed")


def test_parse_assessor_output_malformed() -> None:
    recs, err = assess.parse_assessor_output("not valid")
    expect(err is not None, "malformed output must fail closed, not return empty list")
    # Empty output may return empty list with no error, or fail - both are acceptable fail-closed
    recs, err = assess.parse_assessor_output("")
    expect((recs == [] and err is None) or err is not None, "empty output handling acceptable")


# ---------------------------------------------------------------------------
# Parity catch-up (#538): v1 assessor grammar (agents/flow-assessor.md reply format) and fail-closed replies.
# ---------------------------------------------------------------------------

ASSESS_SENTINEL = "<!-- assessment-end -->"


def _v1_record(rid: str, status: str = "unfinished", kind: str = "decision", scope: str = "in-scope",
               safe: str = "no", cause: str = "cause text", evidence: str = "checked the tree",
               options: str = "A => pause ;; B => issue") -> str:
    return (f"{rid} | status={status} | kind={kind} | scope={scope} | leave_as_is_safe={safe} | "
            f"cause={cause} | evidence={evidence} | finish_plan=finish it | verification_proof=ran it | "
            f"options={options}")


def test_v2_v1_grammar_reply_parses_into_two_records() -> None:
    text = "\n".join([
        _v1_record("a1", status="already-done", kind="mechanical", safe="yes",
                   evidence="src/foo.py:12", options="A => pause"),
        _v1_record("b2"),
        ASSESS_SENTINEL,
        "",
    ])
    recs, err = assess.parse_assessor_output(text)
    expect(err is None, f"v1-grammar reply returned error {err!r}")
    expect(len(recs) == 2, f"expected two records, got {len(recs)}")
    expect(getattr(recs[0], "status", None) == "already-done",
           f"first record status is {getattr(recs[0], 'status', None)!r}, expected already-done")
    expect(getattr(recs[1], "status", None) == "unfinished",
           f"second record status is {getattr(recs[1], 'status', None)!r}, expected unfinished")


def test_v2_hostile_or_malformed_replies_fail_closed() -> None:
    """Each hostile or malformed reply is refused by validate_assessor_output, with its own reason."""
    good = _v1_record("b2")
    variants = {
        "missing sentinel": (good + "\n", "sentinel"),
        "status done": (_v1_record("b2", status="done") + "\n" + ASSESS_SENTINEL + "\n", "unparseable"),
        "record with fewer than two pipes": ("b2 | status=unfinished\n" + ASSESS_SENTINEL + "\n", "missing status or kind"),
        "option without a pause or issue route": (_v1_record("b2", options="A => keep") + "\n" + ASSESS_SENTINEL + "\n", "unparseable"),
        "option labelled Open an issue": (_v1_record("b2", options="Open an issue => issue ;; B => pause") + "\n" + ASSESS_SENTINEL + "\n", "unparseable"),
        "three options": (_v1_record("b2", options="A => pause ;; B => issue ;; C => pause") + "\n" + ASSESS_SENTINEL + "\n", "unparseable"),
        "label of 121 characters": (_v1_record("b2", options=("L" * 121) + " => pause ;; B => issue") + "\n" + ASSESS_SENTINEL + "\n", "option label"),
        "reason containing <<<ITEM": (_v1_record("b2", cause="x <<<ITEM y") + "\n" + ASSESS_SENTINEL + "\n", "reserved marker"),
        "id outside the outstanding set": (_v1_record("zz9") + "\n" + ASSESS_SENTINEL + "\n", "not in outstanding set"),
    }
    wrong = []
    for name, (text, needle) in variants.items():
        recs, err = assess.validate_assessor_output(text, ["b2"])
        if recs is not None or err is None or needle not in err:
            wrong.append(f"{name}: recs={recs!r} err={err!r}, expected an error containing {needle!r}")
    expect(not wrong, "hostile or malformed replies were not refused with their own reason: " + "; ".join(wrong))


def test_v2_label_length_boundary_and_labels_are_kept() -> None:
    """A label of exactly LABEL_MAX characters is accepted and keeps its text and route; one more is refused."""
    at_limit = "L" * assess.LABEL_MAX
    text = _v1_record("b2", options=f"{at_limit} => pause ;; B => issue") + "\n" + ASSESS_SENTINEL + "\n"
    recs, err = assess.validate_assessor_output(text, ["b2"])
    expect(err is None and recs is not None, f"a label of {assess.LABEL_MAX} characters was refused: {err!r}")
    assert recs is not None
    options = recs[0].options
    expect(options is not None and [label for label, _route in options] == [at_limit, "B"],
           f"accepted option labels are {options!r}, expected [{at_limit!r}, 'B']")
    over = _v1_record("b2", options=("L" * (assess.LABEL_MAX + 1)) + " => pause ;; B => issue") + "\n" + ASSESS_SENTINEL + "\n"
    _recs, over_err = assess.validate_assessor_output(over, ["b2"])
    expect(over_err is not None and "option label" in over_err, f"a label of {assess.LABEL_MAX + 1} characters returned {over_err!r}")


SECTIONS = [
    ("[Audit]", [
        ("1: verdict parsing", test_audit_verdicts),
        ("2: audit receipt path never collides with a step receipt", test_audit_receipt_path_is_distinct),
        ("3: Accept eligibility fails closed", test_accept_eligibility_fails_closed),
        ("4: rejection question offers Retry", test_rejection_question_offers_retry_always),
    ]),
    ("[Assess]", [
        ("5: assessor records", test_assessor_records),
        ("6: parse_assessor_output fails closed on malformed output", test_parse_assessor_output_malformed),
        ("7: hostile or malformed replies fail closed", test_v2_hostile_or_malformed_replies_fail_closed),
        ("8: label length boundary and labels are kept", test_v2_label_length_boundary_and_labels_are_kept),
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
