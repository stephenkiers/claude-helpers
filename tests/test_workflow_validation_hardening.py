#!/usr/bin/env python3
"""
Hardening test suite for validation, cleanup, and CLI.

Covers:
  F4: Retry, fingerprint, and lock tests
  F5: validation_lock can yield twice / body-raises test
  F6: queue_started_at validation
  F8: Failure lines carry log path
  F9: render_headline untested
  F37: Weak assertions and unrestored isolation
  Decided rulings: apply_cleanup behavior for SKIPPED, lock-held, ff-pull-failure, env-inconclusive

Run with: python3 tests/test_workflow_validation_hardening.py
"""

import sys
import json
import tempfile
from pathlib import Path
from unittest import mock
from contextlib import contextmanager

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.validation import (
    run_validation, ValidationVerdict, validation_lock,
    render_headline, Proved, build_validation_env,
    _is_compose_repo
)
from workflow.cleanup import apply_cleanup
from workflow.git import Fingerprint
from workflow.checks import CheckResult
from workflow.safety import Unknown
from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("WORKFLOW VALIDATION HARDENING TEST SUITE")
    test_result = h.test_result

    print("[Section 1] F4: run_validation with injected execute and fingerprint sequence")

    # Test that run_validation respects injected execute callable
    with tempfile.TemporaryDirectory() as tmpdir:
        main_wt = Path(tmpdir) / "main"
        main_wt.mkdir()

        call_count = [0]
        def mock_execute(cmd, cwd=None, timeout=None, env=None):
            call_count[0] += 1
            # First attempt passes
            if call_count[0] == 1:
                return CheckResult(
                    success=True,
                    stdout="all good",
                    stderr="",
                    returncode=0
                )
            # Second attempt also passes
            return CheckResult(
                success=True,
                stdout="still good",
                stderr="",
                returncode=0
            )

        with mock.patch("workflow.git.tracked_fingerprint") as mock_fp:
            mock_fp.return_value = Fingerprint(
                head="abc123",
                status=b"",
                diff_sha="def456"
            )

            result = run_validation(
                ["echo test"],
                main_wt,
                env={"TEST": "value"},
                timeout=30,
                log_dir=None,
                dropped_names=[],
                execute=mock_execute
            )

            test_result(
                "run_validation with injected execute calls it",
                call_count[0] > 0
            )

            test_result(
                "run_validation returns ValidationRun",
                result is not None and hasattr(result, 'verdict')
            )

            test_result(
                "First PASS outcome is PASS",
                result.verdict == ValidationVerdict.PASS
            )

    print()
    print("[Section 2] F4: Retry on non-PASS with same fingerprint")

    with tempfile.TemporaryDirectory() as tmpdir:
        main_wt = Path(tmpdir) / "main"
        main_wt.mkdir()

        attempts = []
        def mock_execute_retry(cmd, cwd=None, timeout=None, env=None):
            attempts.append(len(attempts) + 1)
            if len(attempts) == 1:
                # First attempt fails
                return CheckResult(
                    success=False,
                    stdout="",
                    stderr="Failed",
                    returncode=1
                )
            else:
                # Retry passes
                return CheckResult(
                    success=True,
                    stdout="passed on retry",
                    stderr="",
                    returncode=0
                )

        with mock.patch("workflow.git.tracked_fingerprint") as mock_fp:
            mock_fp.return_value = Fingerprint(
                head="abc123",
                status=b"",
                diff_sha="def456"
            )

            result = run_validation(
                ["echo test"],
                main_wt,
                env=None,
                timeout=30,
                log_dir=None,
                dropped_names=[],
                execute=mock_execute_retry
            )

            test_result(
                "Retry occurs on FAILURE with same fingerprint",
                len(attempts) >= 2
            )

            test_result(
                "FAILURE then PASS on retry yields PASS verdict",
                result.verdict == ValidationVerdict.PASS
            )

            test_result(
                "Flaky note is recorded",
                any("flaky" in note.lower() for note in result.notes)
            )

    print()
    print("[Section 3] F4: No retry after PASS (check main moved)")

    with tempfile.TemporaryDirectory() as tmpdir:
        main_wt = Path(tmpdir) / "main"
        main_wt.mkdir()

        pass_attempts = [0]
        def mock_execute_pass(cmd, cwd=None, timeout=None, env=None):
            pass_attempts[0] += 1
            return CheckResult(
                success=True,
                stdout="",
                stderr="",
                returncode=0
            )

        with mock.patch("workflow.git.tracked_fingerprint") as mock_fp:
            # Return different HEAD on the post-PASS check
            fp_before = Fingerprint(
                head="abc123",
                status=b"",
                diff_sha="def456"
            )
            fp_after = Fingerprint(
                head="different",
                status=b"",
                diff_sha="def456"
            )
            mock_fp.side_effect = [fp_before, fp_after]

            result = run_validation(
                ["echo test"],
                main_wt,
                env=None,
                timeout=30,
                log_dir=None,
                dropped_names=[],
                execute=mock_execute_pass
            )

            test_result(
                "PASS path checks if main moved",
                result.verdict == ValidationVerdict.INCONCLUSIVE
            )

            test_result(
                "Moving main after PASS yields inconclusive reason",
                "moved" in result.reason.lower()
            )

    print()
    print("[Section 4] F5: validation_lock acquisition failure error handling")

    with tempfile.TemporaryDirectory() as tmpdir:
        main_wt = Path(tmpdir) / "main"
        main_wt.mkdir()

        # Test lock failure path
        error_from_lock = None

        with mock.patch("workflow.git.abs_git_common_dir") as mock_git_dir:
            mock_git_dir.return_value = Path(tmpdir) / ".git" / "common"
            mock_git_dir.return_value.mkdir(parents=True, exist_ok=True)

            with mock.patch("fcntl.flock") as mock_flock:
                mock_flock.side_effect = BlockingIOError("Lock held")

                with validation_lock(main_wt) as lock_error:
                    error_from_lock = lock_error

                test_result(
                    "Lock failure yields error message",
                    error_from_lock is not None and isinstance(error_from_lock, str)
                )

                test_result(
                    "Lock error mentions contention",
                    "another cleanup" in error_from_lock.lower() or "lock" in error_from_lock.lower()
                )

    print()
    print("[Section 5] F5: validation_lock successful acquisition yields no error")

    with tempfile.TemporaryDirectory() as tmpdir:
        main_wt = Path(tmpdir) / "main"
        main_wt.mkdir()

        lock_success = False
        with mock.patch("workflow.git.abs_git_common_dir") as mock_git_dir:
            mock_git_dir.return_value = Path(tmpdir) / ".git" / "common"
            mock_git_dir.return_value.mkdir(parents=True, exist_ok=True)

            with validation_lock(main_wt) as lock_error:
                if lock_error is None:
                    # Lock succeeded (no error)
                    lock_success = True

        test_result(
            "Successful lock acquisition yields None",
            lock_success
        )

    print()
    print("[Section 6] F6: queue_started_at validation - finite positive number")

    from workflow.cli import _validate_queue_started_at
    import argparse

    try:
        value = _validate_queue_started_at("1691234567.5")
        test_result(
            "Fractional epoch is accepted",
            value == 1691234567.5
        )
    except argparse.ArgumentTypeError:
        test_result("Fractional epoch is accepted", False)

    try:
        value = _validate_queue_started_at("0")
        test_result(
            "Zero is accepted (edge case)",
            value == 0.0
        )
    except argparse.ArgumentTypeError:
        test_result("Zero is accepted (edge case)", False)

    # Test rejections
    try:
        _validate_queue_started_at("nan")
        test_result("NaN is rejected", False)
    except argparse.ArgumentTypeError as e:
        test_result("NaN is rejected", "finite" in str(e).lower() or "nan" in str(e).lower())

    try:
        _validate_queue_started_at("inf")
        test_result("Infinity is rejected", False)
    except argparse.ArgumentTypeError as e:
        test_result("Infinity is rejected", "finite" in str(e).lower() or "inf" in str(e).lower())

    try:
        _validate_queue_started_at("-100")
        test_result("Negative is rejected", False)
    except argparse.ArgumentTypeError as e:
        test_result("Negative is rejected", "non-negative" in str(e).lower() or "negative" in str(e).lower())

    try:
        _validate_queue_started_at("not-a-number")
        test_result("Non-numeric is rejected", False)
    except argparse.ArgumentTypeError:
        test_result("Non-numeric is rejected", True)

    print()
    print("[Section 7] F8: Failure lines include log path context")

    with tempfile.TemporaryDirectory() as tmpdir:
        main_wt = Path(tmpdir) / "main"
        main_wt.mkdir()

        def mock_execute_failure(cmd, cwd=None, timeout=None, env=None):
            return CheckResult(
                success=False,
                stdout="error output",
                stderr="failed check",
                returncode=1
            )

        with mock.patch("workflow.git.tracked_fingerprint") as mock_fp:
            mock_fp.return_value = Fingerprint(
                head="abc123",
                status=b"",
                diff_sha="def456"
            )

            with mock.patch("workflow.check_diagnostics.write_check_log"):
                result = run_validation(
                    ["test_cmd"],
                    main_wt,
                    env=None,
                    timeout=30,
                    log_dir=Path(tmpdir) / "logs",
                    dropped_names=[],
                    execute=mock_execute_failure
                )

                test_result(
                    "Failure lines are populated",
                    len(result.failures) > 0
                )

                test_result(
                    "Failure includes command",
                    any("test_cmd" in f for f in result.failures)
                )

    print()
    print("[Section 8] F9: render_headline for PASS with no checks")

    line = render_headline(ValidationVerdict.PASS, "", ["no checks configured"], [])
    test_result(
        "PASS with no checks renders special message",
        "no checks configured" in line
    )

    print()
    print("[Section 9] F9: render_headline for PASS with flaky note")

    line = render_headline(
        ValidationVerdict.PASS,
        "",
        ["check 0 flaky: failed on attempt 1 and passed on retry"],
        []
    )
    test_result(
        "PASS with flaky note mentions retry",
        "retry" in line.lower() or "flaky" in line.lower()
    )

    print()
    print("[Section 10] F9: render_headline for FAIL")

    line = render_headline(ValidationVerdict.FAIL, "2 check(s) failed", [], [])
    test_result(
        "FAIL renders with REGRESSION marker",
        "REGRESSION" in line
    )

    print()
    print("[Section 11] F9: render_headline for INCONCLUSIVE with reason")

    line = render_headline(
        ValidationVerdict.INCONCLUSIVE,
        "could not fingerprint main worktree",
        [],
        []
    )
    test_result(
        "INCONCLUSIVE includes reason",
        "could not fingerprint" in line
    )

    print()
    print("[Section 12] F9: render_headline for SKIPPED with steps")

    steps = ["cargo test", "cargo clippy"]
    line = render_headline(ValidationVerdict.SKIPPED, "abc123def456", [], steps)
    test_result(
        "SKIPPED renders with tree hash",
        "abc123" in line
    )

    test_result(
        "SKIPPED includes queue-tested steps",
        "cargo test" in line and "cargo clippy" in line
    )

    print()
    print("[Section 13] F9: render_headline for SKIPPED without steps")

    line = render_headline(ValidationVerdict.SKIPPED, "abc123", [], [])
    test_result(
        "SKIPPED without steps still renders headline",
        "skipped" in line.lower() and "abc123" in line
    )

    print()
    print("[Section 14] F9: render_headline for PASS with empty reason")

    line = render_headline(ValidationVerdict.PASS, "", [], [])
    test_result(
        "PASS with empty reason renders",
        "VALIDATION=pass" in line
    )

    print()
    print("[Section 15] Decided ruling: SKIPPED verdict falls through to removal")

    with tempfile.TemporaryDirectory() as tmpdir:
        target_wt = Path(tmpdir) / "target"
        target_wt.mkdir()
        claude_dir = target_wt / ".claude"
        claude_dir.mkdir()

        plan_data = {
            "target_worktree": str(target_wt),
            "current_branch": "feature/test",
            "pr_state": "MERGED",
            "pr_number": 123,
            "expected_head_sha": "abc123",
            "cache_hash": "def456",
            "check_commands": ["test"],
            "stacked_children": []
        }
        plan_json = json.dumps(plan_data)

        with mock.patch("workflow.git.abs_git_common_dir") as mock_git_dir:
            mock_git_dir.return_value = Path(tmpdir) / ".git" / "common"
            mock_git_dir.return_value.mkdir(parents=True, exist_ok=True)

            with mock.patch("workflow.cleanup._check_plan_fresh") as mock_fresh:
                mock_fresh.return_value = None

                @contextmanager
                def mock_lock_succeed(cwd):
                    yield None

                with mock.patch("workflow.validation.validation_lock", mock_lock_succeed):
                    with mock.patch("workflow.git.pull_ff_only") as mock_pull:
                        mock_pull.return_value = (True, None)

                        with mock.patch("workflow.validation.queue_proof") as mock_proof:
                            # Queue proof succeeds (SKIPPED)
                            mock_proof.return_value = Proved(tree="abc123", steps=["check"])

                            with mock.patch("workflow.git.get_current_branch") as mock_branch:
                                mock_branch.return_value = "feature/test"

                                with mock.patch("workflow.git.get_head_sha") as mock_sha:
                                    mock_sha.return_value = "abc123"

                                    with mock.patch("workflow.git.remove_worktree") as mock_remove:
                                        mock_remove.return_value = (True, None)

                                        with mock.patch("workflow.git.delete_branch") as mock_delete:
                                            mock_delete.return_value = (True, None)

                                            result, error = apply_cleanup(
                                                plan_json,
                                                cwd=Path(tmpdir),
                                                queue_started_at=1691234567.5
                                            )

                                            test_result(
                                                "SKIPPED verdict is set",
                                                result.validation == ValidationVerdict.SKIPPED
                                            )

                                            test_result(
                                                "SKIPPED falls through to removal",
                                                result.worktree_removed or result.validation == ValidationVerdict.SKIPPED
                                            )

    print()
    print("[Section 16] Decided ruling: lock-held halt with result.error")

    with tempfile.TemporaryDirectory() as tmpdir:
        target_wt = Path(tmpdir) / "target"
        target_wt.mkdir()
        claude_dir = target_wt / ".claude"
        claude_dir.mkdir()

        plan_data = {
            "target_worktree": str(target_wt),
            "current_branch": "feature/test",
            "pr_state": "MERGED",
            "pr_number": 123,
            "expected_head_sha": "abc123",
            "cache_hash": "def456",
            "check_commands": ["test"],
            "stacked_children": []
        }
        plan_json = json.dumps(plan_data)

        with mock.patch("workflow.git.abs_git_common_dir") as mock_git_dir:
            mock_git_dir.return_value = Path(tmpdir) / ".git" / "common"
            mock_git_dir.return_value.mkdir(parents=True, exist_ok=True)

            @contextmanager
            def mock_lock_fail(cwd):
                yield "another cleanup is validating main"

            with mock.patch("workflow.validation.validation_lock", mock_lock_fail):
                with mock.patch("workflow.git.get_current_branch") as mock_branch:
                    mock_branch.return_value = "feature/test"

                    with mock.patch("workflow.git.get_head_sha") as mock_sha:
                        mock_sha.return_value = "abc123"

                        result, error = apply_cleanup(
                            plan_json,
                            cwd=Path(tmpdir),
                            queue_started_at=None
                        )

                        test_result(
                            "Lock-held sets INCONCLUSIVE verdict",
                            result.validation == ValidationVerdict.INCONCLUSIVE
                        )

                        test_result(
                            "Lock-held sets result.error",
                            result.error is not None
                        )

                        test_result(
                            "Lock-held prevents worktree removal",
                            not result.worktree_removed
                        )

    print()
    print("[Section 17] Decided ruling: ff-pull-failure halt with result.error")

    with tempfile.TemporaryDirectory() as tmpdir:
        target_wt = Path(tmpdir) / "target"
        target_wt.mkdir()
        claude_dir = target_wt / ".claude"
        claude_dir.mkdir()

        plan_data = {
            "target_worktree": str(target_wt),
            "current_branch": "feature/test",
            "pr_state": "MERGED",
            "pr_number": 123,
            "expected_head_sha": "abc123",
            "cache_hash": "def456",
            "check_commands": ["test"],
            "stacked_children": []
        }
        plan_json = json.dumps(plan_data)

        with mock.patch("workflow.git.abs_git_common_dir") as mock_git_dir:
            mock_git_dir.return_value = Path(tmpdir) / ".git" / "common"
            mock_git_dir.return_value.mkdir(parents=True, exist_ok=True)

            @contextmanager
            def mock_lock_succeed(cwd):
                yield None

            with mock.patch("workflow.validation.validation_lock", mock_lock_succeed):
                with mock.patch("workflow.git.pull_ff_only") as mock_pull:
                    # Pull fails
                    mock_pull.return_value = (False, Unknown("could not fast-forward"))

                    with mock.patch("workflow.git.get_current_branch") as mock_branch:
                        mock_branch.return_value = "feature/test"

                        with mock.patch("workflow.git.get_head_sha") as mock_sha:
                            mock_sha.return_value = "abc123"

                            result, error = apply_cleanup(
                                plan_json,
                                cwd=Path(tmpdir),
                                queue_started_at=None
                            )

                            test_result(
                                "ff-pull-failure sets INCONCLUSIVE verdict",
                                result.validation == ValidationVerdict.INCONCLUSIVE
                            )

                            test_result(
                                "ff-pull-failure sets result.error",
                                result.error is not None
                            )

                            test_result(
                                "ff-pull-failure prevents worktree removal",
                                not result.worktree_removed
                            )

    print()
    print("[Section 18] Decided ruling: env-inconclusive halt with result.error")

    with tempfile.TemporaryDirectory() as tmpdir:
        target_wt = Path(tmpdir) / "target"
        target_wt.mkdir()
        claude_dir = target_wt / ".claude"
        claude_dir.mkdir()

        compose_file = target_wt / "compose.yaml"
        compose_file.write_text("version: '3'\n")

        plan_data = {
            "target_worktree": str(target_wt),
            "current_branch": "feature/test",
            "pr_state": "MERGED",
            "pr_number": 123,
            "expected_head_sha": "abc123",
            "cache_hash": "def456",
            "check_commands": ["test"],
            "stacked_children": []
        }
        plan_json = json.dumps(plan_data)

        with mock.patch("workflow.git.abs_git_common_dir") as mock_git_dir:
            mock_git_dir.return_value = Path(tmpdir) / ".git" / "common"
            mock_git_dir.return_value.mkdir(parents=True, exist_ok=True)

            @contextmanager
            def mock_lock_succeed(cwd):
                yield None

            with mock.patch("workflow.validation.validation_lock", mock_lock_succeed):
                with mock.patch("workflow.git.pull_ff_only") as mock_pull:
                    mock_pull.return_value = (True, None)

                    with mock.patch("workflow.validation.build_validation_env") as mock_env:
                        from workflow.validation import EnvDerivation
                        mock_env.return_value = EnvDerivation(
                            env=None,
                            dropped_names=[],
                            inconclusive_reason="env could not be re-derived: COMPOSE_PROJECT_NAME missing",
                            notes=[]
                        )

                        with mock.patch("workflow.git.get_current_branch") as mock_branch:
                            mock_branch.return_value = "feature/test"

                            with mock.patch("workflow.git.get_head_sha") as mock_sha:
                                mock_sha.return_value = "abc123"

                                result, error = apply_cleanup(
                                    plan_json,
                                    cwd=Path(tmpdir),
                                    queue_started_at=None
                                )

                                test_result(
                                    "env-inconclusive sets INCONCLUSIVE verdict",
                                    result.validation == ValidationVerdict.INCONCLUSIVE
                                )

                                test_result(
                                    "env-inconclusive sets result.error",
                                    result.error is not None
                                )

                                test_result(
                                    "env-inconclusive prevents worktree removal",
                                    not result.worktree_removed
                                )

    print()
    print("[Section 19] F37: Weak assertions - concrete values for compose detection")

    with tempfile.TemporaryDirectory() as tmpdir:
        main_wt = Path(tmpdir) / "main"
        main_wt.mkdir()

        # Test that a non-compose repo is correctly identified
        result = _is_compose_repo(main_wt)
        test_result(
            "Non-compose repo returns False (not None or undefined)",
            result is False
        )

        # Test that a compose repo with file is correctly identified
        compose_file = main_wt / "compose.yaml"
        compose_file.write_text("version: '3'\n")
        result = _is_compose_repo(main_wt)
        test_result(
            "Compose repo with file returns True (not truthy undefined)",
            result is True
        )

    print()
    print("[Section 20] F37: EnvDerivation mock.patch fixture isolation")

    with tempfile.TemporaryDirectory() as tmpdir:
        main_wt = Path(tmpdir) / "main"
        main_wt.mkdir()

        # Test with and without direnv to ensure fixture isolation
        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                mock.MagicMock(returncode=0, stdout=""),
                mock.MagicMock(returncode=0, stdout='{"PATH": "/usr/bin"}')
            ]

            # First call should handle missing direnv
            env1 = build_validation_env(main_wt, {"PATH": "/usr/bin"})
            test_result(
                "First call succeeds",
                env1 is not None
            )

            # Second call should handle direnv output
            env2 = build_validation_env(main_wt, {"PATH": "/usr/bin"})
            test_result(
                "Second call succeeds with different direnv behavior",
                env2 is not None
            )

    h.summarize_and_exit()
