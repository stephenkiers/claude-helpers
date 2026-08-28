#!/usr/bin/env python3
"""
Test suite for cleanup planning and application.

Run with: python3 tests/test_workflow_cleanup.py
"""

import sys
import json
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.cleanup import plan_cleanup, apply_cleanup, CleanupPlan, CleanupResult
from workflow.safety import Unknown
from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("WORKFLOW CLEANUP TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Cleanup plan dataclass round-trip")

    plan = CleanupPlan(
        target_worktree="/tmp/wt",
        current_branch="feature/test",
        pr_state="MERGED",
        pr_number=42,
        expected_head_sha="abc123",
        cache_hash="def456",
        check_commands=["cargo test", "cargo clippy"]
    )

    plan_dict = plan.to_dict()
    test_result(
        "CleanupPlan.to_dict() produces valid dict",
        isinstance(plan_dict, dict) and plan_dict["target_worktree"] == "/tmp/wt"
    )

    restored = CleanupPlan.from_dict(plan_dict)
    test_result(
        "CleanupPlan.from_dict() restores all fields",
        restored.current_branch == "feature/test" and restored.pr_number == 42
    )

    print()
    print("[Section 2] Cleanup result dataclass")

    result = CleanupResult(
        success=True,
        worktree_removed=True,
        branch_deleted=True,
        validation_passed=True
    )

    result_dict = result.to_dict()
    test_result(
        "CleanupResult.to_dict() includes success flag",
        result_dict["success"] is True
    )

    result_with_error = CleanupResult(
        success=False,
        error=Unknown("test error")
    )
    result_dict_with_error = result_with_error.to_dict()
    test_result(
        "CleanupResult.to_dict() serializes error",
        "error" in result_dict_with_error and "test error" in result_dict_with_error["error"]
    )

    print()
    print("[Section 3] Plan hash changes with state changes")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        wt_dir = tmppath / "worktree"
        wt_dir.mkdir()
        claude_dir = wt_dir / ".claude"
        claude_dir.mkdir()

        with mock.patch("workflow.git.get_current_branch") as mock_branch:
            with mock.patch("workflow.git.pr_view_json") as mock_pr:
                with mock.patch("workflow.git.get_head_sha") as mock_sha:
                    mock_branch.return_value = "feature"
                    mock_pr.return_value = {"state": "MERGED", "number": 42}
                    mock_sha.return_value = "abc123def456"

                    plan1, err1 = plan_cleanup(str(wt_dir))
                    hash1 = plan1.plan_hash if plan1 else None

                    mock_pr.return_value = {"state": "OPEN", "number": 42}
                    plan2, err2 = plan_cleanup(str(wt_dir))
                    hash2 = plan2.plan_hash if plan2 else None

                    test_result(
                        "Plan hash changes when PR state changes",
                        hash1 and hash2 and hash1 != hash2
                    )

    print()
    print("[Section 4] Apply cleanup validates freshness")

    plan = CleanupPlan(
        target_worktree="/tmp/nonexistent",
        current_branch="feature",
        pr_state="MERGED",
        expected_head_sha="abc123",
        cache_hash="def456"
    )
    plan_json = json.dumps(plan.to_dict())

    result, err = apply_cleanup(plan_json)
    test_result(
        "apply_cleanup rejects stale plan (missing worktree)",
        not result.success and result.error is not None
    )

    print()
    print("[Section 5] Apply cleanup selects correct branch deletion flag")

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
                with mock.patch("workflow.git.delete_branch") as mock_delete:
                    with mock.patch("workflow.git.remove_worktree") as mock_remove:
                        mock_branch.return_value = "feature"
                        mock_sha.return_value = "abc123"
                        mock_delete.return_value = (True, None)
                        mock_remove.return_value = (True, None)

                        result, err = apply_cleanup(plan_json)

                        called_args, called_kwargs = mock_delete.call_args
                        force_flag = called_kwargs.get("force", False)
                        test_result(
                            "apply_cleanup uses -D (force=True) when PR_STATE == MERGED",
                            force_flag is True
                        )

        plan.pr_state = "OPEN"
        plan_json = json.dumps(plan.to_dict())

        with mock.patch("workflow.git.get_current_branch") as mock_branch:
            with mock.patch("workflow.git.get_head_sha") as mock_sha:
                with mock.patch("workflow.git.delete_branch") as mock_delete:
                    with mock.patch("workflow.git.remove_worktree") as mock_remove:
                        mock_branch.return_value = "feature"
                        mock_sha.return_value = "abc123"
                        mock_delete.return_value = (True, None)
                        mock_remove.return_value = (True, None)

                        result, err = apply_cleanup(plan_json)

                        called_args, called_kwargs = mock_delete.call_args
                        force_flag = called_kwargs.get("force", False)
                        test_result(
                            "apply_cleanup uses -d (force=False) when PR_STATE != MERGED",
                            force_flag is False
                        )

    print()
    print("[Section 6] Validation failure does not prevent worktree removal")

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
            check_commands=["false"]
        )
        plan_json = json.dumps(plan.to_dict())

        with mock.patch("workflow.git.get_current_branch") as mock_branch:
            with mock.patch("workflow.git.get_head_sha") as mock_sha:
                with mock.patch("workflow.git.delete_branch") as mock_delete:
                    with mock.patch("workflow.git.remove_worktree") as mock_remove:
                        with mock.patch("workflow.git.pull_ff_only") as mock_pull:
                            mock_branch.return_value = "feature"
                            mock_sha.return_value = "abc123"
                            mock_delete.return_value = (True, None)
                            mock_remove.return_value = (True, None)
                            mock_pull.return_value = (True, None)

                            result, err = apply_cleanup(plan_json)

                            test_result(
                                "apply_cleanup removes worktree even with validation failures",
                                result.worktree_removed is True
                            )
                            test_result(
                                "apply_cleanup records validation failure",
                                result.validation_passed is False and len(result.validation_failures) > 0
                            )

    print()
    print("[Section 7] Stacked children detection")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        main_wt = tmppath / "main"
        main_wt.mkdir()
        child_wt = tmppath / "child"
        child_wt.mkdir()

        main_wt_resolve = str(main_wt)
        child_wt_resolve = str(child_wt)

        (child_wt / ".claude").mkdir(parents=True)
        child_cache = child_wt / ".claude" / "github-cache.json"
        child_cache.write_text(json.dumps({
            "schema_version": "1.0",
            "branch": "feature/child",
            "stack": {
                "isStacked": True,
                "parentBranch": "feature/parent",
                "parentPr": 42
            }
        }))

        with mock.patch("workflow.git.get_current_branch") as mock_branch:
            with mock.patch("workflow.git.get_worktree_list_porcelain") as mock_porcelain:
                with mock.patch("workflow.git.pr_list_json") as mock_pr_list:
                    with mock.patch("workflow.worktrees.detect_worktree_parent") as mock_parent:
                        mock_branch.return_value = "feature/parent"
                        mock_parent.return_value = str(tmppath)
                        mock_porcelain.return_value = f"worktree {child_wt_resolve}\nbranch refs/heads/feature/child\n"
                        mock_pr_list.return_value = []

                        from workflow.cleanup import _detect_stacked_children
                        children, gh_failed, detection_incomplete = _detect_stacked_children(
                            "feature/parent",
                            Path(main_wt)
                        )

                        test_result(
                            "Cache-detected child found",
                            len(children) == 1 and children[0].branch == "feature/child"
                        )

        with mock.patch("workflow.git.get_worktree_list_porcelain") as mock_porcelain:
            with mock.patch("workflow.git.pr_list_json") as mock_pr_list:
                with mock.patch("workflow.worktrees.detect_worktree_parent") as mock_parent:
                    mock_parent.return_value = str(tmppath)
                    mock_porcelain.return_value = ""
                    mock_pr_list.return_value = [
                        {"headRefName": "feature/gh-child", "number": 99}
                    ]

                    from workflow.cleanup import _detect_stacked_children
                    children, gh_failed, detection_incomplete = _detect_stacked_children(
                        "feature/parent",
                        Path(main_wt)
                    )

                    test_result(
                        "gh pr list-detected child found",
                        len(children) == 1 and children[0].branch == "feature/gh-child"
                    )

        with mock.patch("workflow.git.get_worktree_list_porcelain") as mock_porcelain:
            with mock.patch("workflow.git.pr_list_json") as mock_pr_list:
                with mock.patch("workflow.worktrees.detect_worktree_parent") as mock_parent:
                    mock_parent.return_value = str(tmppath)
                    mock_porcelain.return_value = f"worktree {child_wt_resolve}\nbranch refs/heads/feature/child\n"
                    mock_pr_list.side_effect = Exception("Network error")

                    from workflow.cleanup import _detect_stacked_children
                    children, gh_failed, detection_incomplete = _detect_stacked_children(
                        "feature/parent",
                        Path(main_wt)
                    )

                    test_result(
                        "gh lookup failure is flagged",
                        gh_failed is True
                    )

    print()
    h.summarize_and_exit()
