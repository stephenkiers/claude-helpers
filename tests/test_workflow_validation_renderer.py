#!/usr/bin/env python3
"""
Test suite for validation verdict rendering and parsing.

Run with: python3 tests/test_workflow_validation_renderer.py
"""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.validation import ValidationVerdict
from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("WORKFLOW VALIDATION RENDERER TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Parse None gives FAIL")

    verdict = ValidationVerdict.parse(None)
    test_result(
        "parse(None) gives FAIL",
        verdict == ValidationVerdict.FAIL
    )

    print()
    print("[Section 2] Parse uppercase 'PASS'")

    verdict = ValidationVerdict.parse("PASS")
    test_result(
        "parse('PASS') is accepted (may be case-insensitive)",
        verdict in (ValidationVerdict.PASS, ValidationVerdict.FAIL)
    )

    print()
    print("[Section 3] Parse lowercase 'pass' gives PASS")

    verdict = ValidationVerdict.parse("pass")
    test_result(
        "parse('pass') gives PASS",
        verdict == ValidationVerdict.PASS
    )

    print()
    print("[Section 4] Parse lowercase 'fail' gives FAIL")

    verdict = ValidationVerdict.parse("fail")
    test_result(
        "parse('fail') gives FAIL",
        verdict == ValidationVerdict.FAIL
    )

    print()
    print("[Section 5] Parse lowercase 'inconclusive' gives INCONCLUSIVE")

    verdict = ValidationVerdict.parse("inconclusive")
    test_result(
        "parse('inconclusive') gives INCONCLUSIVE",
        verdict == ValidationVerdict.INCONCLUSIVE
    )

    print()
    print("[Section 6] Parse lowercase 'skipped' gives SKIPPED")

    verdict = ValidationVerdict.parse("skipped")
    test_result(
        "parse('skipped') gives SKIPPED",
        verdict == ValidationVerdict.SKIPPED
    )

    print()
    print("[Section 7] Parse integer 1 gives FAIL")

    verdict = ValidationVerdict.parse(1)
    test_result(
        "parse(1) gives FAIL",
        verdict == ValidationVerdict.FAIL
    )

    print()
    print("[Section 8] Parse list gives FAIL")

    verdict = ValidationVerdict.parse(["pass"])
    test_result(
        "parse(['pass']) gives FAIL",
        verdict == ValidationVerdict.FAIL
    )

    print()
    print("[Section 9] Parse empty string gives FAIL")

    verdict = ValidationVerdict.parse("")
    test_result(
        "parse('') gives FAIL",
        verdict == ValidationVerdict.FAIL
    )

    print()
    print("[Section 10] Parse unknown string gives FAIL")

    verdict = ValidationVerdict.parse("unknown_verdict")
    test_result(
        "parse('unknown_verdict') gives FAIL",
        verdict == ValidationVerdict.FAIL
    )

    print()
    print("[Section 11] Verdict enum values are lowercase strings")

    test_result(
        "PASS.value is 'pass'",
        ValidationVerdict.PASS.value == "pass"
    )
    test_result(
        "FAIL.value is 'fail'",
        ValidationVerdict.FAIL.value == "fail"
    )
    test_result(
        "INCONCLUSIVE.value is 'inconclusive'",
        ValidationVerdict.INCONCLUSIVE.value == "inconclusive"
    )
    test_result(
        "SKIPPED.value is 'skipped'",
        ValidationVerdict.SKIPPED.value == "skipped"
    )

    print()
    print("[Section 12] CleanupResult default is INCONCLUSIVE")

    from workflow.cleanup import CleanupResult

    result = CleanupResult(success=True)
    test_result(
        "CleanupResult(success=True) default validation is INCONCLUSIVE",
        result.validation == ValidationVerdict.INCONCLUSIVE
    )

    print()
    print("[Section 13] CleanupResult.to_dict() includes validation field")

    result = CleanupResult(success=True, validation=ValidationVerdict.PASS, validation_reason="all checks passed")
    result_dict = result.to_dict()
    test_result(
        "to_dict() includes validation field",
        "validation" in result_dict
    )
    test_result(
        "validation field contains string value",
        result_dict["validation"] == "pass"
    )

    print()
    print("[Section 14] Derived validation_passed is true for PASS and SKIPPED")

    result = CleanupResult(success=True, validation=ValidationVerdict.PASS)
    result_dict = result.to_dict()
    test_result(
        "validation_passed is true for PASS",
        result_dict.get("validation_passed") is True
    )

    result = CleanupResult(success=True, validation=ValidationVerdict.SKIPPED)
    result_dict = result.to_dict()
    test_result(
        "validation_passed is true for SKIPPED",
        result_dict.get("validation_passed") is True
    )

    print()
    print("[Section 15] Derived validation_passed is false for FAIL and INCONCLUSIVE")

    result = CleanupResult(success=True, validation=ValidationVerdict.FAIL)
    result_dict = result.to_dict()
    test_result(
        "validation_passed is false for FAIL",
        result_dict.get("validation_passed") is False
    )

    result = CleanupResult(success=True, validation=ValidationVerdict.INCONCLUSIVE)
    result_dict = result.to_dict()
    test_result(
        "validation_passed is false for INCONCLUSIVE",
        result_dict.get("validation_passed") is False
    )

    print()
    print("[Section 16] json.dumps(to_dict()) succeeds for all verdicts")

    for verdict in [ValidationVerdict.PASS, ValidationVerdict.FAIL, ValidationVerdict.INCONCLUSIVE, ValidationVerdict.SKIPPED]:
        result = CleanupResult(success=True, validation=verdict, validation_reason="test")
        try:
            json_str = json.dumps(result.to_dict())
            test_result(
                f"json.dumps() succeeds for {verdict.value}",
                isinstance(json_str, str)
            )
        except Exception:
            test_result(
                f"json.dumps() succeeds for {verdict.value}",
                False
            )

    print()
    print("[Section 17] validation_reason is preserved in to_dict()")

    result = CleanupResult(success=True, validation=ValidationVerdict.INCONCLUSIVE, validation_reason="could not fast-forward main")
    result_dict = result.to_dict()
    test_result(
        "validation_reason is in to_dict()",
        "validation_reason" in result_dict
    )
    test_result(
        "validation_reason value is preserved",
        result_dict["validation_reason"] == "could not fast-forward main"
    )

    print()
    print("[Section 18] queue_tested_steps is preserved in to_dict()")

    result = CleanupResult(success=True, validation=ValidationVerdict.SKIPPED, queue_tested_steps=["check", "test"])
    result_dict = result.to_dict()
    test_result(
        "queue_tested_steps is in to_dict()",
        "queue_tested_steps" in result_dict
    )
    test_result(
        "queue_tested_steps value is preserved",
        result_dict["queue_tested_steps"] == ["check", "test"]
    )

    h.summarize_and_exit()
