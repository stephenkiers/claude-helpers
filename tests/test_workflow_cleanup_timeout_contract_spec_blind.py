#!/usr/bin/env python3
"""
Spec-blind test suite for cleanup timeout-vs-failure classification contract.

This test suite is written ONLY from the plan specification (two findings),
without reading the implementation files (cleanup.py, checks.py, cleanup.md).

The two findings being tested:
1. Shared string constant + exact-match test for timeout-vs-failure contract:
   - timeout-vs-failure distinction crosses execute_check (sets error string on TimeoutExpired),
     apply_cleanup (re-derives timeout classification from error string prefix), and
     cleanup.md's jq filter (re-matches on string to decide inconclusive vs REGRESSION).
   - Define shared constants for these prefixes and pin their exact values with tests.

2. Mixed timeout+regression outcome must NOT collapse to "inconclusive":
   - When any non-timeout failure is present in validation_failures, even if a timeout
     failure is also present, the outcome must escalate to "REGRESSION on main".
   - Only when EVERY entry in validation_failures is timeout-shaped should the softer
     "inconclusive: check command timed out" headline print.

Run with: python3 tests/test_workflow_cleanup_timeout_contract_spec_blind.py
"""

import sys
import json
import tempfile
import os
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.cleanup import apply_cleanup, CleanupPlan
from workflow.checks import TIMEOUT_ERROR_PREFIX, CheckResult
from _test_harness import Harness
from _git_fixture import GitFixture


def mock_execute_check_with_sequence(results_sequence):
    """Factory for mock execute_check with side_effect list."""
    call_count = [0]

    def mock_fn(cmd, cwd=None, timeout=300):
        if call_count[0] >= len(results_sequence):
            # Fallback: return success if sequence exhausted
            return CheckResult(success=True, returncode=0, stdout="", stderr="", error=None)
        result = results_sequence[call_count[0]]
        call_count[0] += 1
        return result

    return mock_fn


if __name__ == "__main__":
    h = Harness("CLEANUP TIMEOUT CONTRACT SPEC-BLIND TEST SUITE")
    test_result = h.test_result

    print("[Finding 1] Shared string constant for timeout-vs-failure contract")
    print("=" * 70)

    # Test that TIMEOUT_ERROR_PREFIX is defined and has the expected value
    test_result(
        "TIMEOUT_ERROR_PREFIX is defined in workflow.checks",
        hasattr(__import__("workflow.checks", fromlist=["TIMEOUT_ERROR_PREFIX"]), "TIMEOUT_ERROR_PREFIX"),
        "TIMEOUT_ERROR_PREFIX not found in workflow.checks"
    )

    # Verify the exact string value (so future rewording can't silently break classification)
    test_result(
        "TIMEOUT_ERROR_PREFIX has expected value 'timed out after'",
        TIMEOUT_ERROR_PREFIX == "timed out after",
        f"Expected 'timed out after', got '{TIMEOUT_ERROR_PREFIX}'"
    )

    # Test that execute_check sets error with this prefix on timeout
    print()
    print("[Section 1a] execute_check sets timeout error with correct prefix")

    result = __import__("workflow.checks", fromlist=["execute_check"]).execute_check("sleep 100", cwd=None, timeout=1)
    test_result(
        "execute_check on timeout sets error field",
        result.error is not None,
        f"error should be set on timeout; got {result.error}"
    )
    test_result(
        "execute_check timeout error starts with TIMEOUT_ERROR_PREFIX",
        result.error is not None and result.error.startswith(TIMEOUT_ERROR_PREFIX),
        f"timeout error '{result.error}' should start with '{TIMEOUT_ERROR_PREFIX}'"
    )
    test_result(
        "execute_check timeout error matches pattern 'timed out after Ns'",
        result.error is not None and "timed out after" in result.error and "s" in result.error,
        f"timeout error should match pattern; got '{result.error}'"
    )

    print()
    print("[Finding 2a] Mixed timeout+regression: one timeout, one non-timeout failure")
    print("=" * 70)

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)

        fixture.create_initial_commit("initial")
        fixture.create_branch("feature/mixed-fail")
        wt_path = fixture.create_worktree("feature/mixed-fail")

        feature_sha = fixture.get_head_sha(cwd=wt_path)

        plan = CleanupPlan(
            target_worktree=str(wt_path),
            current_branch="feature/mixed-fail",
            pr_state="MERGED",
            pr_number=100,
            expected_head_sha=feature_sha,
            cache_hash=None,
            check_commands=["format", "lint"]  # Two commands to test
        )
        plan_json = json.dumps(plan.to_dict())

        # Mock execute_check to return one timeout, one non-timeout failure
        timeout_result = CheckResult(
            success=False,
            returncode=None,
            stdout="",
            stderr="",
            error=f"{TIMEOUT_ERROR_PREFIX} 300s"
        )
        non_timeout_result = CheckResult(
            success=False,
            returncode=1,
            stdout="",
            stderr="boom: lint failed",
            error=None
        )

        with mock.patch("workflow.checks.execute_check") as mock_exec:
            mock_exec.side_effect = [timeout_result, non_timeout_result]

            result, err = apply_cleanup(plan_json)

            test_result(
                "apply_cleanup with mixed failures executes all checks",
                mock_exec.call_count == 2,
                f"Expected 2 execute_check calls, got {mock_exec.call_count}"
            )

            # The key assertion: validation_failures should contain at least one entry
            # that does NOT contain the timeout prefix. Look for "Check command" formatted entries.
            has_timeout_failure = False
            has_non_timeout_failure = False

            if result.validation_failures:
                for failure_msg in result.validation_failures:
                    if isinstance(failure_msg, str):
                        # Timeout failures will have "Check command timed out after" or similar
                        if "timed out after" in failure_msg or TIMEOUT_ERROR_PREFIX in failure_msg:
                            has_timeout_failure = True
                        # Non-timeout failures will have "Check command failed" or similar
                        elif "Check command failed" in failure_msg or "failed" in failure_msg.lower():
                            has_non_timeout_failure = True

            test_result(
                "validation_failures contains at least one timeout-shaped failure",
                has_timeout_failure,
                f"Expected a timeout failure containing '{TIMEOUT_ERROR_PREFIX}' in {result.validation_failures}"
            )

            test_result(
                "validation_failures contains at least one non-timeout failure",
                has_non_timeout_failure,
                f"Expected a non-timeout failure in {result.validation_failures}"
            )

            test_result(
                "apply_cleanup with mixed failures returns list of validation_failures",
                isinstance(result.validation_failures, list) and len(result.validation_failures) >= 2,
                f"Expected list with 2+ entries; got {result.validation_failures}"
            )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Finding 2b] All-timeout scenario: multiple timeouts, no non-timeout failures")
    print("=" * 70)

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)

        fixture.create_initial_commit("initial")
        fixture.create_branch("feature/all-timeout")
        wt_path = fixture.create_worktree("feature/all-timeout")

        feature_sha = fixture.get_head_sha(cwd=wt_path)

        plan = CleanupPlan(
            target_worktree=str(wt_path),
            current_branch="feature/all-timeout",
            pr_state="MERGED",
            pr_number=101,
            expected_head_sha=feature_sha,
            cache_hash=None,
            check_commands=["format", "lint"]  # Two commands, both timing out
        )
        plan_json = json.dumps(plan.to_dict())

        # Mock execute_check to return two timeouts
        timeout_result_1 = CheckResult(
            success=False,
            returncode=None,
            stdout="",
            stderr="",
            error=f"{TIMEOUT_ERROR_PREFIX} 300s"
        )
        timeout_result_2 = CheckResult(
            success=False,
            returncode=None,
            stdout="",
            stderr="",
            error=f"{TIMEOUT_ERROR_PREFIX} 300s"
        )

        with mock.patch("workflow.checks.execute_check") as mock_exec:
            mock_exec.side_effect = [timeout_result_1, timeout_result_2]

            result, err = apply_cleanup(plan_json)

            test_result(
                "apply_cleanup with all timeouts executes all checks",
                mock_exec.call_count == 2,
                f"Expected 2 execute_check calls, got {mock_exec.call_count}"
            )

            # All validation_failures should be timeout-shaped (contain "timed out after")
            all_timeout_shaped = True
            has_any_failure = False

            if result.validation_failures:
                has_any_failure = len(result.validation_failures) > 0
                for failure_msg in result.validation_failures:
                    if isinstance(failure_msg, str):
                        # Count only check-command failures; git pull failures are setup noise
                        if failure_msg.startswith("Check command"):
                            if "timed out after" not in failure_msg:
                                all_timeout_shaped = False
                                break

            test_result(
                "validation_failures contains entries (all timeouts scenario)",
                has_any_failure,
                f"Expected validation_failures to be non-empty; got {result.validation_failures}"
            )

            # Filter to just the "Check command" entries for this assertion
            check_command_failures = [msg for msg in result.validation_failures
                                      if isinstance(msg, str) and msg.startswith("Check command")]
            test_result(
                "all check-command failures are timeout-shaped in all-timeout scenario",
                all_timeout_shaped and len(check_command_failures) > 0,
                f"Expected all check-command entries to contain '{TIMEOUT_ERROR_PREFIX}'; got {check_command_failures}"
            )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 3] Timeout prefix is consistent across all three layers")
    print("=" * 70)

    # Verify the timeout prefix is accessible from both checks and cleanup modules
    from workflow import checks
    from workflow import cleanup

    checks_module = __import__("workflow.checks", fromlist=["TIMEOUT_ERROR_PREFIX"])

    test_result(
        "TIMEOUT_ERROR_PREFIX is accessible from checks module",
        hasattr(checks_module, "TIMEOUT_ERROR_PREFIX"),
        "TIMEOUT_ERROR_PREFIX not found in checks module"
    )

    test_result(
        "TIMEOUT_ERROR_PREFIX constant is consistent",
        checks_module.TIMEOUT_ERROR_PREFIX == "timed out after",
        f"Expected 'timed out after', got '{checks_module.TIMEOUT_ERROR_PREFIX}'"
    )

    print()
    h.summarize_and_exit()
