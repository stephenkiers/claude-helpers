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
    print("[Section 8] plan_cleanup propagates a get_head_sha failure instead of swallowing it")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        wt_dir = tmppath / "worktree"
        wt_dir.mkdir()
        (wt_dir / ".claude").mkdir()

        with mock.patch("workflow.git.get_current_branch") as mock_branch:
            with mock.patch("workflow.git.get_head_sha") as mock_sha:
                with mock.patch("workflow.git.pr_view_json") as mock_pr:
                    mock_branch.return_value = "feature"
                    mock_pr.return_value = {"state": "MERGED", "number": 42}
                    mock_sha.side_effect = RuntimeError("git failed")

                    plan, err = plan_cleanup(str(wt_dir))

                    test_result(
                        "plan_cleanup returns an Unknown error instead of a plan with a None SHA",
                        plan is None and isinstance(err, Unknown)
                    )

    print()
    print("[Section 9] CleanupPlan.from_dict filters unexpected keys on nested children")

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
            "CleanupPlan.from_dict tolerates an unexpected key in a nested child dict",
            restored.stacked_children[0].branch == "feature/child"
        )
    except TypeError as e:
        test_result(
            "CleanupPlan.from_dict tolerates an unexpected key in a nested child dict",
            False,
            f"raised TypeError: {e}"
        )

    print()
    print("[Section 10] plan_cleanup sets repo_cache_read_failed on an unreadable repo-cache.json")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        wt_dir = tmppath / "worktree"
        wt_dir.mkdir()
        claude_dir = wt_dir / ".claude"
        claude_dir.mkdir()
        (claude_dir / "repo-cache.json").write_text("{invalid json")

        with mock.patch("workflow.git.get_current_branch") as mock_branch:
            with mock.patch("workflow.git.get_head_sha") as mock_sha:
                with mock.patch("workflow.git.pr_view_json") as mock_pr:
                    mock_branch.return_value = "feature"
                    mock_pr.return_value = {"state": "MERGED", "number": 42}
                    mock_sha.return_value = "abc123"

                    plan, err = plan_cleanup(str(wt_dir))

                    test_result(
                        "plan_cleanup flags repo_cache_read_failed for a corrupt (not missing) cache file",
                        plan is not None and plan.repo_cache_read_failed is True
                    )

    print()
    print("[Section 11] apply_cleanup's mutation-adjacent failure paths")

    def _base_plan(**overrides):
        defaults = dict(
            target_worktree=str(wt_dir),
            current_branch="feature",
            pr_state="MERGED",
            expected_head_sha="abc123",
            cache_hash=None,
        )
        defaults.update(overrides)
        return CleanupPlan(**defaults)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        wt_dir = tmppath / "worktree"
        wt_dir.mkdir()

        # 11a: a failed pull_ff_only flips validation_passed, not just a message.
        plan_json = json.dumps(_base_plan().to_dict())
        with mock.patch("workflow.git.get_current_branch") as mock_branch:
            with mock.patch("workflow.git.get_head_sha") as mock_sha:
                with mock.patch("workflow.git.pull_ff_only") as mock_pull:
                    with mock.patch("workflow.git.delete_branch") as mock_delete:
                        with mock.patch("workflow.git.remove_worktree") as mock_remove:
                            mock_branch.return_value = "feature"
                            mock_sha.return_value = "abc123"
                            mock_pull.return_value = (False, Unknown("Merge conflict detected"))
                            mock_delete.return_value = (True, None)
                            mock_remove.return_value = (True, None)

                            result, err = apply_cleanup(plan_json)

                            test_result(
                                "apply_cleanup flips validation_passed=False after a failed pull_ff_only",
                                result.validation_passed is False
                            )

        # 11b: force-retry fires only on a dirty-tree removal failure.
        plan_json = json.dumps(_base_plan().to_dict())
        with mock.patch("workflow.git.get_current_branch") as mock_branch:
            with mock.patch("workflow.git.get_head_sha") as mock_sha:
                with mock.patch("workflow.git.pull_ff_only") as mock_pull:
                    with mock.patch("workflow.git.delete_branch") as mock_delete:
                        with mock.patch("workflow.git.remove_worktree") as mock_remove:
                            mock_branch.return_value = "feature"
                            mock_sha.return_value = "abc123"
                            mock_pull.return_value = (True, None)
                            mock_delete.return_value = (True, None)
                            mock_remove.side_effect = [
                                (False, Unknown("error: working tree is dirty")),
                                (True, None),
                            ]

                            result, err = apply_cleanup(plan_json)

                            test_result(
                                "apply_cleanup retries with force on a dirty-tree removal failure",
                                mock_remove.call_count == 2 and result.worktree_removed is True
                            )

        # 11c: a non-dirty-tree removal failure does NOT trigger the force retry.
        plan_json = json.dumps(_base_plan().to_dict())
        with mock.patch("workflow.git.get_current_branch") as mock_branch:
            with mock.patch("workflow.git.get_head_sha") as mock_sha:
                with mock.patch("workflow.git.pull_ff_only") as mock_pull:
                    with mock.patch("workflow.git.delete_branch") as mock_delete:
                        with mock.patch("workflow.git.remove_worktree") as mock_remove:
                            mock_branch.return_value = "feature"
                            mock_sha.return_value = "abc123"
                            mock_pull.return_value = (True, None)
                            mock_delete.return_value = (True, None)
                            mock_remove.return_value = (False, Unknown("Permission denied"))

                            result, err = apply_cleanup(plan_json)

                            test_result(
                                "apply_cleanup does not force-retry a non-dirty-tree removal failure",
                                mock_remove.call_count == 1 and result.worktree_removed is False
                            )

        # 11d: the forced retry's own exception is recorded, not swallowed.
        plan_json = json.dumps(_base_plan().to_dict())
        with mock.patch("workflow.git.get_current_branch") as mock_branch:
            with mock.patch("workflow.git.get_head_sha") as mock_sha:
                with mock.patch("workflow.git.pull_ff_only") as mock_pull:
                    with mock.patch("workflow.git.delete_branch") as mock_delete:
                        with mock.patch("workflow.git.remove_worktree") as mock_remove:
                            mock_branch.return_value = "feature"
                            mock_sha.return_value = "abc123"
                            mock_pull.return_value = (True, None)
                            mock_delete.return_value = (True, None)
                            mock_remove.side_effect = [
                                (False, Unknown("error: working tree is dirty")),
                                RuntimeError("disk full"),
                            ]

                            result, err = apply_cleanup(plan_json)

                            test_result(
                                "apply_cleanup records the forced retry's own exception",
                                any("disk full" in msg for msg in result.validation_failures)
                            )

        # 11e: HEAD SHA re-check before mutation aborts the apply.
        plan_json = json.dumps(_base_plan(check_commands=["echo test"]).to_dict())
        with mock.patch("workflow.git.get_current_branch") as mock_branch:
            with mock.patch("workflow.git.get_head_sha") as mock_sha:
                with mock.patch("workflow.git.pull_ff_only") as mock_pull:
                    with mock.patch("workflow.git.delete_branch") as mock_delete:
                        with mock.patch("workflow.git.remove_worktree") as mock_remove:
                            with mock.patch("workflow.checks.execute_check") as mock_check:
                                from workflow.checks import CheckResult
                                mock_branch.return_value = "feature"
                                mock_pull.return_value = (True, None)
                                mock_sha.side_effect = ["abc123", "def456"]
                                mock_delete.return_value = (True, None)
                                mock_remove.return_value = (True, None)
                                mock_check.return_value = CheckResult(success=True, returncode=0, stdout="", stderr="")

                                result, err = apply_cleanup(plan_json)

                                test_result(
                                    "apply_cleanup aborts before mutation when HEAD SHA changed mid-apply",
                                    result.success is False
                                    and "HEAD SHA changed" in str(result.error)
                                    and result.worktree_removed is False
                                    and result.branch_deleted is False
                                )

    print()
    h.summarize_and_exit()
