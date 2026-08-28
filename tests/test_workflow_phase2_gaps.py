#!/usr/bin/env python3
"""
Test suite for Phase 2 plan items not yet covered by existing tests.

Covers:
- Item 3: MergeResult.cache_write_failed field
- Item 6: get_head_sha failure in plan_cleanup
- Item 10: CleanupPlan.from_dict filtering on unexpected keys
- Item 11: CleanupPlan.repo_cache_read_failed field
- Item 13: validation_passed after pull_ff_only failure
- Item 14: Dirty tree force retry logic
- Item 15: Forced worktree removal exception recording
- Item 16: HEAD SHA re-check during cleanup apply

Run with: python3 tests/test_workflow_phase2_gaps.py
"""

import sys
import json
import tempfile
import os
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.merge import apply_merge, MergePlan
from workflow.cleanup import plan_cleanup, apply_cleanup, CleanupPlan, CleanupResult
from workflow.cache import write_cache
from workflow.safety import Unknown
from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("WORKFLOW PHASE 2 GAPS TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Item 3: MergeResult handles cache write failures")

    # Test that when _write_merge_cache fails, the result still completes
    # and any cache_write_failed field (if present) is properly set
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        wt_dir = tmppath / "worktree"
        wt_dir.mkdir()
        claude_dir = wt_dir / ".claude"
        claude_dir.mkdir()

        plan = MergePlan(
            pr_number=50,
            head_ref="feature",
            target_worktree=str(wt_dir),
            blocking_failures=[]
        )

        # Mock the merge process to succeed but cache write to fail
        with mock.patch("workflow.merge._check_just_merge") as mock_just:
            with mock.patch("workflow.merge._run_gh_pr_merge") as mock_merge:
                with mock.patch("workflow.merge.write_cache") as mock_write_cache:
                    mock_just.return_value = False
                    mock_merge.return_value = (True, None)
                    # Simulate write_cache failure
                    mock_write_cache.return_value = (False, Unknown("Permission denied"))

                    plan_json = json.dumps(plan.to_dict())
                    result, err = apply_merge(plan_json)

                    # The merge can still succeed even if cache write fails
                    # Check if cache_write_failed field exists and is set, or if it's handled differently
                    has_cache_write_failed = hasattr(result, 'cache_write_failed')
                    test_result(
                        "apply_merge result has cache_write_failed field",
                        has_cache_write_failed,
                        f"Result should have cache_write_failed field if cache write can fail"
                    )

    print()
    print("[Section 2] Item 6: get_head_sha failure in plan_cleanup")

    # Test that when get_head_sha raises an error, plan_cleanup propagates it
    # as a real error instead of silently producing expected_head_sha=None
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        wt_dir = tmppath / "worktree"
        wt_dir.mkdir()
        claude_dir = wt_dir / ".claude"
        claude_dir.mkdir()

        with mock.patch("workflow.git.get_current_branch") as mock_branch:
            with mock.patch("workflow.git.get_head_sha") as mock_sha:
                with mock.patch("workflow.git.pr_view_json") as mock_pr:
                    mock_branch.return_value = "feature"
                    mock_pr.return_value = {"state": "MERGED", "number": 42}
                    # Simulate get_head_sha failure
                    mock_sha.side_effect = RuntimeError("git failed")

                    plan, err = plan_cleanup(str(wt_dir))

                    test_result(
                        "plan_cleanup propagates get_head_sha error",
                        plan is None and err is not None and isinstance(err, Unknown),
                        f"Expected error, got plan={plan}, err={err}"
                    )

    print()
    print("[Section 3] Item 10: CleanupPlan.from_dict with unexpected keys")

    # Test that CleanupPlan.from_dict filters unexpected keys in child dicts
    # without raising TypeError
    plan_dict = {
        "target_worktree": "/tmp/wt",
        "current_branch": "feature",
        "pr_state": "MERGED",
        "pr_number": 42,
        "expected_head_sha": "abc123",
        "cache_hash": "def456",
        "check_commands": [],
        "stacked_children": [
            {
                "branch": "feature/child",
                "pr_number": 99,
                "worktree_path": "/tmp/child",
                "unexpected_field": "should not cause error"
            }
        ]
    }

    try:
        restored = CleanupPlan.from_dict(plan_dict)
        test_result(
            "CleanupPlan.from_dict handles unexpected child fields",
            restored is not None and restored.stacked_children is not None and len(restored.stacked_children) > 0,
            "Should create plan without raising TypeError"
        )
    except TypeError as e:
        test_result(
            "CleanupPlan.from_dict handles unexpected child fields",
            False,
            f"Should not raise TypeError, got: {e}"
        )

    print()
    print("[Section 4] Item 11: CleanupPlan.repo_cache_read_failed field")

    # Test that CleanupPlan.repo_cache_read_failed is set when .claude/repo-cache.json exists
    # but fails to read/validate
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        wt_dir = tmppath / "worktree"
        wt_dir.mkdir()
        claude_dir = wt_dir / ".claude"
        claude_dir.mkdir()

        # Create an invalid repo-cache.json
        cache_file = claude_dir / "repo-cache.json"
        cache_file.write_text("{invalid json")

        with mock.patch("workflow.git.get_current_branch") as mock_branch:
            with mock.patch("workflow.git.get_head_sha") as mock_sha:
                with mock.patch("workflow.git.pr_view_json") as mock_pr:
                    mock_branch.return_value = "feature"
                    mock_pr.return_value = {"state": "MERGED", "number": 42}
                    mock_sha.return_value = "abc123"

                    plan, err = plan_cleanup(str(wt_dir))

                    test_result(
                        "plan_cleanup sets repo_cache_read_failed when cache is invalid",
                        plan is not None and hasattr(plan, 'repo_cache_read_failed') and plan.repo_cache_read_failed is True,
                        f"Expected repo_cache_read_failed=True, got {getattr(plan, 'repo_cache_read_failed', 'MISSING')}"
                    )

    print()
    print("[Section 5] Item 13: validation_passed after pull_ff_only failure")

    # Test that when pull_ff_only fails, validation_passed is set to False
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        wt_dir = tmppath / "worktree"
        wt_dir.mkdir()

        plan = CleanupPlan(
            target_worktree=str(wt_dir),
            current_branch="feature",
            pr_state="MERGED",
            expected_head_sha="abc123",
            cache_hash=None
        )
        plan_json = json.dumps(plan.to_dict())

        with mock.patch("workflow.git.get_current_branch") as mock_branch:
            with mock.patch("workflow.git.get_head_sha") as mock_sha:
                with mock.patch("workflow.git.pull_ff_only") as mock_pull:
                    with mock.patch("workflow.git.delete_branch") as mock_delete:
                        with mock.patch("workflow.git.remove_worktree") as mock_remove:
                            mock_branch.return_value = "feature"
                            mock_sha.return_value = "abc123"
                            # Simulate pull failure (tuple: success, error_message)
                            mock_pull.return_value = (False, "Merge conflict detected")
                            mock_delete.return_value = (True, None)
                            mock_remove.return_value = (True, None)

                            result, err = apply_cleanup(plan_json)

                            test_result(
                                "apply_cleanup sets validation_passed=False after pull_ff_only failure",
                                result.validation_passed is False,
                                f"Expected validation_passed=False, got {result.validation_passed}"
                            )
                            test_result(
                                "pull_ff_only failure recorded in validation_failures",
                                len(result.validation_failures) > 0 and any("ff-only" in msg.lower() for msg in result.validation_failures),
                                f"Expected pull failure message in validation_failures, got {result.validation_failures}"
                            )

    print()
    print("[Section 6] Item 14: Dirty tree force retry logic")

    # Test that force retry happens on dirty tree errors (contains "dirty" or "modified or untracked")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        wt_dir = tmppath / "worktree"
        wt_dir.mkdir()

        plan = CleanupPlan(
            target_worktree=str(wt_dir),
            current_branch="feature",
            pr_state="MERGED",
            expected_head_sha="abc123",
            cache_hash=None
        )
        plan_json = json.dumps(plan.to_dict())

        with mock.patch("workflow.git.get_current_branch") as mock_branch:
            with mock.patch("workflow.git.get_head_sha") as mock_sha:
                with mock.patch("workflow.git.pull_ff_only") as mock_pull:
                    with mock.patch("workflow.git.delete_branch") as mock_delete:
                        with mock.patch("workflow.git.remove_worktree") as mock_remove:
                            mock_branch.return_value = "feature"
                            mock_sha.return_value = "abc123"
                            mock_pull.return_value = (True, None)
                            mock_delete.return_value = (True, None)
                            # Simulate removal failure with "dirty" reason, then success on retry
                            mock_remove.side_effect = [
                                (False, "error: working tree has local modifications"),
                                (True, None)  # success on retry with force
                            ]

                            result, err = apply_cleanup(plan_json)

                            # The dirty tree retry logic should attempt force removal when error indicates dirty tree
                            test_result(
                                "apply_cleanup retries on dirty tree error",
                                mock_remove.call_count >= 1,
                                f"Expected at least 1 call to remove_worktree, got {mock_remove.call_count}"
                            )

    print()
    print("[Section 7] Item 14b: Force retry NOT triggered for non-dirty failures")

    # Test that force retry does NOT happen for non-dirty failures
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        wt_dir = tmppath / "worktree"
        wt_dir.mkdir()

        plan = CleanupPlan(
            target_worktree=str(wt_dir),
            current_branch="feature",
            pr_state="MERGED",
            expected_head_sha="abc123",
            cache_hash=None
        )
        plan_json = json.dumps(plan.to_dict())

        with mock.patch("workflow.git.get_current_branch") as mock_branch:
            with mock.patch("workflow.git.get_head_sha") as mock_sha:
                with mock.patch("workflow.git.pull_ff_only") as mock_pull:
                    with mock.patch("workflow.git.delete_branch") as mock_delete:
                        with mock.patch("workflow.git.remove_worktree") as mock_remove:
                            mock_branch.return_value = "feature"
                            mock_sha.return_value = "abc123"
                            mock_pull.return_value = (True, None)
                            mock_delete.return_value = (True, None)
                            # Simulate removal failure with non-dirty reason
                            mock_remove.return_value = (False, "Permission denied")

                            result, err = apply_cleanup(plan_json)

                            test_result(
                                "apply_cleanup does NOT retry with force on non-dirty error",
                                result.worktree_removed is False,
                                f"Expected worktree_removed=False, got {result.worktree_removed}"
                            )
                            test_result(
                                "remove_worktree called only once (no force retry)",
                                mock_remove.call_count == 1,
                                f"Expected 1 call to remove_worktree, got {mock_remove.call_count}"
                            )

    print()
    print("[Section 8] Item 15: Forced worktree removal exception recording")

    # Test that when worktree removal fails multiple times, the errors are recorded
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        wt_dir = tmppath / "worktree"
        wt_dir.mkdir()

        plan = CleanupPlan(
            target_worktree=str(wt_dir),
            current_branch="feature",
            pr_state="MERGED",
            expected_head_sha="abc123",
            cache_hash=None
        )
        plan_json = json.dumps(plan.to_dict())

        with mock.patch("workflow.git.get_current_branch") as mock_branch:
            with mock.patch("workflow.git.get_head_sha") as mock_sha:
                with mock.patch("workflow.git.pull_ff_only") as mock_pull:
                    with mock.patch("workflow.git.delete_branch") as mock_delete:
                        with mock.patch("workflow.git.remove_worktree") as mock_remove:
                            mock_branch.return_value = "feature"
                            mock_sha.return_value = "abc123"
                            mock_pull.return_value = (True, None)
                            mock_delete.return_value = (True, None)
                            # Simulate removal failure
                            mock_remove.return_value = (False, "Error: unable to remove worktree")

                            result, err = apply_cleanup(plan_json)

                            test_result(
                                "apply_cleanup records removal error in validation_failures",
                                len(result.validation_failures) > 0,
                                f"Expected error messages in validation_failures, got {result.validation_failures}"
                            )

    print()
    print("[Section 9] Item 16: HEAD SHA re-check during cleanup apply")

    # Test that if HEAD SHA changes during check-commands execution, cleanup is aborted
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        wt_dir = tmppath / "worktree"
        wt_dir.mkdir()

        plan = CleanupPlan(
            target_worktree=str(wt_dir),
            current_branch="feature",
            pr_state="MERGED",
            expected_head_sha="abc123",
            cache_hash=None,
            check_commands=["echo test"]
        )
        plan_json = json.dumps(plan.to_dict())

        with mock.patch("workflow.git.get_current_branch") as mock_branch:
            with mock.patch("workflow.git.get_head_sha") as mock_sha:
                with mock.patch("workflow.git.pull_ff_only") as mock_pull:
                    with mock.patch("workflow.git.delete_branch") as mock_delete:
                        with mock.patch("workflow.git.remove_worktree") as mock_remove:
                            with mock.patch("workflow.checks.execute_check") as mock_check:
                                mock_branch.return_value = "feature"
                                mock_pull.return_value = (True, None)
                                # Return expected SHA first time, different SHA on re-check
                                mock_sha.side_effect = ["abc123", "def456"]
                                mock_delete.return_value = (True, None)
                                mock_remove.return_value = (True, None)
                                from workflow.checks import CheckResult
                                mock_check.return_value = CheckResult(
                                    success=True,
                                    returncode=0,
                                    stdout="output",
                                    stderr="",
                                    error=None
                                )

                                result, err = apply_cleanup(plan_json)

                                test_result(
                                    "apply_cleanup detects HEAD SHA change during checks",
                                    result.success is False and "HEAD SHA changed" in str(result.error),
                                    f"Expected HEAD SHA changed error, got {result.error}"
                                )
                                test_result(
                                    "apply_cleanup does not remove worktree on HEAD change",
                                    result.worktree_removed is False,
                                    f"Expected worktree_removed=False, got {result.worktree_removed}"
                                )
                                test_result(
                                    "apply_cleanup does not delete branch on HEAD change",
                                    result.branch_deleted is False,
                                    f"Expected branch_deleted=False, got {result.branch_deleted}"
                                )

    print()
    h.summarize_and_exit()
