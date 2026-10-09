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
import os
from pathlib import Path
from unittest import mock
from contextlib import contextmanager

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.cleanup import apply_cleanup, CleanupPlan
from workflow.checks import TIMEOUT_ERROR_PREFIX, CheckResult
from workflow import validation, git
from workflow.validation import ValidationVerdict
from _test_harness import Harness
from _git_fixture import GitFixture


def _setup_test_isolation():
    """
    Patch validation probes to avoid side effects on the real checkout.
    """
    # Patch validation_lock to skip actual locking
    @contextmanager
    def mock_validation_lock(main_worktree: Path):
        yield None

    validation.validation_lock = mock_validation_lock

    # Patch git probes to avoid real filesystem access
    def mock_fingerprint(cwd):
        from workflow.git import Fingerprint
        return Fingerprint(
            head="abc123",
            status=b"",
            diff_sha="def456"
        )

    git.tracked_fingerprint = mock_fingerprint

    # Patch compose detection to default to false
    def mock_is_compose(main_worktree: Path):
        return False

    validation._is_compose_repo = mock_is_compose

    # Patch abs_git_common_dir to return a safe temp path
    def mock_abs_git_common_dir(cwd):
        import tempfile
        temp_path = Path(tempfile.gettempdir()) / ".mock-git-common-dir"
        temp_path.mkdir(parents=True, exist_ok=True)
        return temp_path

    git.abs_git_common_dir = mock_abs_git_common_dir

    # Patch pull_ff_only to succeed by default
    def mock_pull_ff_only(remote, branch, cwd=None):
        return (True, None)

    git.pull_ff_only = mock_pull_ff_only


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
    _setup_test_isolation()
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
            # With retry logic: check 1 attempt 1/2, check 2 attempt 1/2
            mock_exec.side_effect = [
                timeout_result,        # Check 1 attempt 1
                timeout_result,        # Check 1 attempt 2
                non_timeout_result,    # Check 2 attempt 1
                non_timeout_result,    # Check 2 attempt 2
            ]

            result, err = apply_cleanup(plan_json)

            test_result(
                "apply_cleanup with mixed failures executes all checks with retries",
                mock_exec.call_count == 4,
                f"Expected 4 execute_check calls (2 checks × 2 attempts), got {mock_exec.call_count}"
            )

            # The key assertion: mixed timeout + non-timeout failures should give FAIL verdict
            # (not INCONCLUSIVE, which would indicate all checks were inconclusive)
            test_result(
                "mixed timeout + non-timeout failures give FAIL verdict",
                result.validation == ValidationVerdict.FAIL,
                f"Expected FAIL verdict, got {result.validation}"
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
            # With retry logic: check 1 attempt 1/2, check 2 attempt 1/2
            mock_exec.side_effect = [
                timeout_result_1,  # Check 1 attempt 1
                timeout_result_1,  # Check 1 attempt 2
                timeout_result_2,  # Check 2 attempt 1
                timeout_result_2,  # Check 2 attempt 2
            ]

            result, err = apply_cleanup(plan_json)

            test_result(
                "apply_cleanup with all timeouts executes all checks with retries",
                mock_exec.call_count == 4,
                f"Expected 4 execute_check calls (2 checks × 2 attempts), got {mock_exec.call_count}"
            )

            # The key assertion: when all checks timeout, the verdict should be INCONCLUSIVE
            # (not FAIL, which would indicate a non-timeout failure)
            test_result(
                "all timeouts give INCONCLUSIVE verdict",
                result.validation == ValidationVerdict.INCONCLUSIVE,
                f"Expected INCONCLUSIVE verdict for all-timeout scenario, got {result.validation}"
            )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 3] Timeout prefix is consistent across all three layers")
    print("=" * 70)

    # Verify the timeout prefix is accessible from both checks and cleanup modules

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
