#!/usr/bin/env python3
"""
Test suite for validation check result classification.

Run with: python3 tests/test_workflow_validation_classifier.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.validation import classify, AttemptOutcome
from workflow.checks import CheckResult, TIMEOUT_ERROR_PREFIX
from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("WORKFLOW VALIDATION CLASSIFIER TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Success gives PASS")

    result = CheckResult(
        success=True,
        returncode=0,
        error=None,
        stdout="ok",
        stderr=""
    )
    outcome, reason = classify(result)
    test_result(
        "Success (returncode=0) gives PASS",
        outcome == AttemptOutcome.PASS
    )

    print()
    print("[Section 2] Timeout error gives INCONCLUSIVE")

    result = CheckResult(
        success=False,
        returncode=None,
        error=f"{TIMEOUT_ERROR_PREFIX}check timed out after 60s",
        stdout="",
        stderr=""
    )
    outcome, reason = classify(result)
    test_result(
        "Timeout error gives INCONCLUSIVE",
        outcome == AttemptOutcome.INCONCLUSIVE
    )
    test_result(
        "Timeout reason mentions check timed out",
        "timed out" in reason.lower()
    )

    print()
    print("[Section 3] Spawn exception gives INCONCLUSIVE")

    result = CheckResult(
        success=False,
        returncode=None,
        error="[Errno 2] No such file or directory",
        stdout="",
        stderr=""
    )
    outcome, reason = classify(result)
    test_result(
        "Spawn exception (returncode=None, error set) gives INCONCLUSIVE",
        outcome == AttemptOutcome.INCONCLUSIVE
    )
    test_result(
        "Spawn reason mentions could not start",
        "could not start" in reason.lower()
    )

    print()
    print("[Section 4] Exit code 126 gives INCONCLUSIVE (blind spot test)")

    result = CheckResult(
        success=False,
        returncode=126,
        error=None,
        stdout="",
        stderr=""
    )
    outcome, reason = classify(result)
    test_result(
        "Exit code 126 gives INCONCLUSIVE",
        outcome == AttemptOutcome.INCONCLUSIVE
    )
    test_result(
        "Blind spot documented in reason",
        "126" in reason or "permission" in reason.lower()
    )

    print()
    print("[Section 5] Exit code 127 gives INCONCLUSIVE (blind spot test)")

    result = CheckResult(
        success=False,
        returncode=127,
        error=None,
        stdout="",
        stderr="unknown_command: command not found"
    )
    outcome, reason = classify(result)
    test_result(
        "Exit code 127 gives INCONCLUSIVE",
        outcome == AttemptOutcome.INCONCLUSIVE
    )

    print()
    print("[Section 6] Docker daemon down pattern gives INCONCLUSIVE")

    result = CheckResult(
        success=False,
        returncode=1,
        error=None,
        stdout="",
        stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
    )
    outcome, reason = classify(result)
    test_result(
        "Docker daemon pattern gives INCONCLUSIVE",
        outcome == AttemptOutcome.INCONCLUSIVE
    )

    print()
    print("[Section 7] Port in use pattern gives INCONCLUSIVE")

    result = CheckResult(
        success=False,
        returncode=1,
        error=None,
        stdout="",
        stderr="Error: listen EADDRINUSE: address already in use :::3000"
    )
    outcome, reason = classify(result)
    test_result(
        "Port in use pattern gives INCONCLUSIVE",
        outcome == AttemptOutcome.INCONCLUSIVE
    )

    print()
    print("[Section 8] node_modules error pattern gives INCONCLUSIVE")

    result = CheckResult(
        success=False,
        returncode=1,
        error=None,
        stdout="",
        stderr="npm ERR! code ELSPROBLEMS\nnpm ERR! Optional dependencies failed"
    )
    outcome, reason = classify(result)
    test_result(
        "npm dependencies error gives INCONCLUSIVE",
        outcome == AttemptOutcome.INCONCLUSIVE
    )

    print()
    print("[Section 9] CHECK-GUARD-ABORT at line start gives INCONCLUSIVE")

    result = CheckResult(
        success=False,
        returncode=1,
        error=None,
        stdout="Starting check\nCHECK-GUARD-ABORT: database not ready",
        stderr=""
    )
    outcome, reason = classify(result)
    test_result(
        "CHECK-GUARD-ABORT marker at line start gives INCONCLUSIVE",
        outcome == AttemptOutcome.INCONCLUSIVE
    )
    test_result(
        "Reason mentions guard abort",
        "guard" in reason.lower() or "abort" in reason.lower()
    )

    print()
    print("[Section 10] CHECK-GUARD-ABORT not at line start gives FAIL")

    result = CheckResult(
        success=False,
        returncode=1,
        error=None,
        stdout="Output: CHECK-GUARD-ABORT: false positive",
        stderr=""
    )
    outcome, reason = classify(result)
    test_result(
        "CHECK-GUARD-ABORT not at line start gives FAIL",
        outcome == AttemptOutcome.FAIL
    )

    print()
    print("[Section 11] Negative returncode gives FAIL")

    result = CheckResult(
        success=False,
        returncode=-15,  # SIGTERM
        error=None,
        stdout="",
        stderr=""
    )
    outcome, reason = classify(result)
    test_result(
        "Negative returncode (signal) gives FAIL",
        outcome == AttemptOutcome.FAIL
    )

    print()
    print("[Section 12] Unknown nonzero exit gives FAIL")

    result = CheckResult(
        success=False,
        returncode=42,
        error=None,
        stdout="Some error",
        stderr="Other error"
    )
    outcome, reason = classify(result)
    test_result(
        "Unknown nonzero exit gives FAIL",
        outcome == AttemptOutcome.FAIL
    )

    print()
    print("[Section 13] Docker error pattern (different format) gives INCONCLUSIVE")

    result = CheckResult(
        success=False,
        returncode=1,
        error=None,
        stdout="",
        stderr="error during connect: this error may indicate the docker daemon is not running"
    )
    outcome, reason = classify(result)
    test_result(
        "Docker 'error during connect' pattern gives INCONCLUSIVE",
        outcome == AttemptOutcome.INCONCLUSIVE
    )

    print()
    print("[Section 14] 'Cannot find module' pattern gives INCONCLUSIVE")

    result = CheckResult(
        success=False,
        returncode=1,
        error=None,
        stdout="",
        stderr="Error: Cannot find module 'some-package'"
    )
    outcome, reason = classify(result)
    test_result(
        "Cannot find module pattern gives INCONCLUSIVE",
        outcome == AttemptOutcome.INCONCLUSIVE
    )

    print()
    print("[Section 15] Empty output with nonzero exit gives FAIL")

    result = CheckResult(
        success=False,
        returncode=1,
        error=None,
        stdout="",
        stderr=""
    )
    outcome, reason = classify(result)
    test_result(
        "Unknown failure (no pattern match) gives FAIL",
        outcome == AttemptOutcome.FAIL
    )

    h.summarize_and_exit()
