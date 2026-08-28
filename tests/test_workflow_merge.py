#!/usr/bin/env python3
"""
Test suite for merge planning and application.

Run with: python3 tests/test_workflow_merge.py
"""

import sys
import json
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.merge import plan_merge, apply_merge, MergePlan, MergeResult
from workflow.safety import Unknown
from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("WORKFLOW MERGE TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Merge plan dataclass round-trip")

    plan = MergePlan(
        pr_number=42,
        head_ref="feature/test",
        target_worktree="/tmp/wt",
        blocking_failures=[]
    )

    plan_dict = plan.to_dict()
    test_result(
        "MergePlan.to_dict() produces valid dict",
        isinstance(plan_dict, dict) and plan_dict["pr_number"] == 42
    )

    restored = MergePlan.from_dict(plan_dict)
    test_result(
        "MergePlan.from_dict() restores all fields",
        restored.head_ref == "feature/test" and restored.pr_number == 42
    )

    print()
    print("[Section 2] Merge result dataclass")

    result = MergeResult(
        success=True,
        pr_merged=True,
        merge_gate_used="just merge"
    )

    result_dict = result.to_dict()
    test_result(
        "MergeResult.to_dict() includes success flag",
        result_dict["success"] is True and result_dict["merge_gate_used"] == "just merge"
    )

    print()
    print("[Section 3] Push gate detects failures correctly")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        wt_dir = tmppath / "worktree"
        wt_dir.mkdir()

        with mock.patch("workflow.merge._run_push_gate") as mock_gate:
            with mock.patch("workflow.merge._resolve_pr_from_worktree") as mock_resolve:
                mock_gate.return_value = []
                mock_resolve.return_value = (42, "feature", str(wt_dir))
                plan_obj, err = plan_merge(str(wt_dir))
                test_result(
                    "plan_merge succeeds when push gate passes",
                    plan_obj and plan_obj.blocking_failures == []
                )

        with mock.patch("workflow.merge._run_push_gate") as mock_gate:
            with mock.patch("workflow.merge._resolve_pr_from_worktree") as mock_resolve:
                mock_gate.return_value = ["Uncommitted changes", "Unpushed commits"]
                mock_resolve.return_value = (42, "feature", str(wt_dir))
                plan_obj, err = plan_merge(str(wt_dir))
                test_result(
                    "plan_merge captures push gate failures",
                    plan_obj and len(plan_obj.blocking_failures) == 2
                )

    print()
    print("[Section 4] Apply merge rejects stale plans")

    plan = MergePlan(
        pr_number=42,
        head_ref="feature",
        target_worktree="/tmp/nonexistent",
        blocking_failures=["test failure"]
    )
    plan_json = json.dumps(plan.to_dict())

    result, err = apply_merge(plan_json)
    test_result(
        "apply_merge rejects plan with blocking_failures",
        not result.success and result.error is not None
    )

    print()
    print("[Section 5] Apply merge lock file prevents concurrent runs")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        wt_dir = tmppath / "worktree"
        wt_dir.mkdir()
        claude_dir = wt_dir / ".claude"
        claude_dir.mkdir()

        plan = MergePlan(
            pr_number=42,
            head_ref="feature",
            target_worktree=str(wt_dir),
            blocking_failures=[]
        )

        lock_file = claude_dir / ".merge-and-cleanup.lock"
        lock_file.write_text("locked")

        plan_json = json.dumps(plan.to_dict())
        result, err = apply_merge(plan_json)

        test_result(
            "apply_merge detects existing lock file",
            not result.success and "lock" in str(result.error).lower()
        )

        test_result(
            "apply_merge does not clear lock file",
            lock_file.exists()
        )

    print()
    print("[Section 6] Apply merge creates lock file on execution")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        wt_dir = tmppath / "worktree"
        wt_dir.mkdir()
        claude_dir = wt_dir / ".claude"
        claude_dir.mkdir()

        plan = MergePlan(
            pr_number=42,
            head_ref="feature",
            target_worktree=str(wt_dir),
            blocking_failures=[]
        )

        lock_file = claude_dir / ".merge-and-cleanup.lock"

        with mock.patch("workflow.merge._check_just_merge") as mock_just:
            with mock.patch("workflow.merge._run_gh_pr_merge") as mock_merge:
                mock_just.return_value = False
                mock_merge.return_value = (True, None)

                plan_json = json.dumps(plan.to_dict())
                result, err = apply_merge(plan_json)

                test_result(
                    "apply_merge creates lock file before merge",
                    lock_file.exists()
                )

                test_result(
                    "apply_merge does not remove lock file after success",
                    result.success and lock_file.exists()
                )

    print()
    print("[Section 7] Apply merge uses unpushed commits check correctly")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        wt_dir = tmppath / "worktree"
        wt_dir.mkdir()

        with mock.patch("workflow.git.rev_list_count") as mock_count:
            with mock.patch("workflow.merge._run_push_gate") as mock_gate:
                with mock.patch("workflow.merge._resolve_pr_from_worktree") as mock_resolve:
                    mock_resolve.return_value = (42, "feature", str(wt_dir))
                    mock_count.return_value = -1
                    mock_gate.return_value = ["Could not determine unpushed commit count"]

                    plan_obj, err = plan_merge(str(wt_dir))

                    test_result(
                        "plan_merge treats -1 (rev_list error) as failure, not 0",
                        plan_obj and "Could not determine" in str(plan_obj.blocking_failures)
                    )

    print()
    print("[Section 8] Apply merge calls _run_gh_pr_merge exactly once on repo-cache-check path (Fix 1)")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        wt_dir = tmppath / "worktree"
        wt_dir.mkdir()
        claude_dir = wt_dir / ".claude"
        claude_dir.mkdir()

        plan = MergePlan(
            pr_number=47,
            head_ref="feature",
            target_worktree=str(wt_dir),
            blocking_failures=[]
        )

        with mock.patch("workflow.merge._check_just_merge") as mock_just:
            with mock.patch("workflow.merge._get_repo_cache_check_cmd") as mock_get_cmd:
                with mock.patch("workflow.merge._run_check_command") as mock_check:
                    with mock.patch("workflow.merge._run_gh_pr_merge") as mock_merge:
                        mock_just.return_value = False
                        mock_get_cmd.return_value = ("test-cmd", None)
                        mock_check.return_value = (True, None)
                        mock_merge.return_value = (True, None)

                        plan_json = json.dumps(plan.to_dict())
                        result, err = apply_merge(plan_json)

                        test_result(
                            "apply_merge calls _run_gh_pr_merge exactly once on repo-cache-check path",
                            result.success and mock_merge.call_count == 1,
                            f"Expected 1 call to _run_gh_pr_merge when check succeeds, got {mock_merge.call_count}"
                        )

    print()
    print("[Section 9] Apply merge diagnostic details (Fix 3)")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        wt_dir = tmppath / "worktree"
        wt_dir.mkdir()
        claude_dir = wt_dir / ".claude"
        claude_dir.mkdir()

        plan = MergePlan(
            pr_number=48,
            head_ref="feature",
            target_worktree=str(wt_dir),
            blocking_failures=[]
        )

        with mock.patch("workflow.merge._check_just_merge") as mock_just:
            with mock.patch("workflow.merge._run_just_merge") as mock_run_just:
                mock_just.return_value = True
                mock_run_just.return_value = (False, "just merge error detail")

                plan_json = json.dumps(plan.to_dict())
                result, err = apply_merge(plan_json)

                test_result(
                    "apply_merge includes diagnostic detail in error message",
                    result.error and "just merge error detail" in str(result.error)
                )

    print()
    print("[Section 10] apply_merge records a failed cache write without failing the merge")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        wt_dir = tmppath / "worktree"
        wt_dir.mkdir()
        (wt_dir / ".claude").mkdir()

        plan = MergePlan(
            pr_number=50,
            head_ref="feature",
            target_worktree=str(wt_dir),
            blocking_failures=[]
        )

        with mock.patch("workflow.merge._check_just_merge") as mock_just:
            with mock.patch("workflow.merge._run_gh_pr_merge") as mock_merge:
                with mock.patch("workflow.merge.write_cache") as mock_write_cache:
                    mock_just.return_value = False
                    mock_merge.return_value = (True, None)
                    mock_write_cache.return_value = (False, Unknown("Permission denied"))

                    plan_json = json.dumps(plan.to_dict())
                    result, err = apply_merge(plan_json)

                    test_result(
                        "apply_merge still succeeds when the cache write fails",
                        result.success is True
                    )
                    test_result(
                        "apply_merge records the cache write failure detail",
                        result.cache_write_failed is not None and "Permission denied" in result.cache_write_failed
                    )

    print()
    h.summarize_and_exit()
