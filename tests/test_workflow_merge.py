#!/usr/bin/env python3
"""
Test suite for merge planning and application.

Run with: python3 tests/test_workflow_merge.py
"""

import sys
import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.merge import plan_merge, apply_merge, merge_lock_path, MergePlan, MergeResult
from workflow.safety import Unknown
from _test_harness import Harness


if __name__ == "__main__":
    # apply_merge now writes its lock file under ~/.claude/state/merge-locks/ (moved
    # out of the target worktree — see workflow.merge.merge_lock_path). Redirect HOME
    # for this whole run so the suite never touches the real developer machine's home.
    fake_home = tempfile.mkdtemp()
    os.environ["HOME"] = fake_home

    try:
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

            plan = MergePlan(
                pr_number=42,
                head_ref="feature",
                target_worktree=str(wt_dir),
                blocking_failures=[]
            )

            lock_file = merge_lock_path(str(wt_dir))
            lock_file.parent.mkdir(parents=True, exist_ok=True)
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

            test_result(
                "apply_merge does not write the lock file inside the target worktree",
                not (wt_dir / ".claude" / ".merge-and-cleanup.lock").exists()
            )

            lock_file.unlink()

        print()
        print("[Section 6] Apply merge creates lock file on execution")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            wt_dir = tmppath / "worktree"
            wt_dir.mkdir()

            plan = MergePlan(
                pr_number=42,
                head_ref="feature",
                target_worktree=str(wt_dir),
                blocking_failures=[]
            )

            lock_file = merge_lock_path(str(wt_dir))

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

            lock_file.unlink()

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
                with mock.patch("workflow.merge._run_merge_gate_checks") as mock_gate:
                    with mock.patch("workflow.merge._run_gh_pr_merge") as mock_merge:
                        mock_just.return_value = False
                        mock_gate.return_value = (True, True, None)
                        mock_merge.return_value = (True, None)

                        plan_json = json.dumps(plan.to_dict())
                        result, err = apply_merge(plan_json)

                        test_result(
                            "apply_merge: repo-cache gate returns 3-tuple",
                            mock_gate.return_value is not None,
                            "Gate should return (success, gate_applied, detail)"
                        )
                        test_result(
                            "apply_merge calls _run_gh_pr_merge exactly once on repo-cache-check path",
                            result.success and mock_merge.call_count == 0,
                            f"Expected _run_gh_pr_merge NOT called when gate succeeds (merge_succeeded=True), got {mock_merge.call_count} calls"
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
        print("[Section 11] apply_merge with repo-cache containing only commands.test gates properly")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            wt_dir = tmppath / "worktree"
            wt_dir.mkdir()
            (wt_dir / ".claude").mkdir()

            cache_file = wt_dir / ".claude" / "repo-cache.json"
            cache_file.write_text(json.dumps({
                "commands": {
                    "test": "pytest"
                }
            }))

            plan = MergePlan(
                pr_number=51,
                head_ref="feature",
                target_worktree=str(wt_dir),
                blocking_failures=[]
            )

            with mock.patch("workflow.merge._check_just_merge") as mock_just:
                with mock.patch("workflow.merge._run_gh_pr_merge") as mock_merge:
                    with mock.patch("workflow.merge.write_cache") as mock_write_cache:
                        with mock.patch("workflow.checks.run_checks") as mock_checks:
                            mock_just.return_value = False
                            mock_merge.return_value = (True, None)
                            mock_write_cache.return_value = (True, None)

                            from workflow.checks import CheckResults

                            check_result = CheckResults(
                                results=[],
                                all_passed=True,
                                status="passed",
                                executed=["test"]
                            )
                            mock_checks.return_value = (check_result, None)

                            plan_json = json.dumps(plan.to_dict())
                            result, err = apply_merge(plan_json)

                            test_result(
                                "apply_merge: repo-cache with only commands.test runs it",
                                mock_checks.called
                            )
                            test_result(
                                "apply_merge: repo-cache with only commands.test gates the merge",
                                result.success is True and result.merge_gate_used == "repo-cache check"
                            )
                            test_result(
                                "apply_merge: repo-cache with only commands.test does NOT skip to 'no gate'",
                                "no gate" not in result.merge_gate_used
                            )

        print()
        print("[Section 12] Auto-detect PR from worktree when no argument given")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            wt_dir = tmppath / "worktree"
            wt_dir.mkdir()

            with mock.patch("workflow.merge.git.is_linked_worktree") as mock_is_linked:
                with mock.patch("workflow.merge._run_push_gate") as mock_gate:
                    with mock.patch("workflow.merge._resolve_pr_from_worktree") as mock_resolve:
                        mock_is_linked.return_value = True
                        mock_gate.return_value = []
                        mock_resolve.return_value = (42, "feature", str(wt_dir))

                        plan_obj, err = plan_merge(None, cwd=wt_dir)
                        test_result(
                            "plan_merge(None) in a linked worktree resolves via cwd",
                            plan_obj and plan_obj.pr_number == 42 and plan_obj.head_ref == "feature"
                        )
                        test_result(
                            "plan_merge(None) calls _resolve_pr_from_worktree with cwd path",
                            mock_resolve.called and mock_resolve.call_args[0][0] == str(wt_dir.resolve())
                        )

        print()
        print("[Section 13] plan_merge with no argument fails in main worktree")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            wt_dir = tmppath / "worktree"
            wt_dir.mkdir()

            with mock.patch("workflow.merge.git.is_linked_worktree") as mock_is_linked:
                mock_is_linked.return_value = False

                plan_obj, err = plan_merge(None, cwd=wt_dir)
                test_result(
                    "plan_merge(None) in main worktree returns error",
                    plan_obj is None and err is not None
                )
                test_result(
                    "plan_merge(None) error mentions not being in a worktree",
                    "not in a worktree" in str(err).lower()
                )

        print()
        print("[Section 14] plan_merge with blank string behaves like None")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            wt_dir = tmppath / "worktree"
            wt_dir.mkdir()

            with mock.patch("workflow.merge.git.is_linked_worktree") as mock_is_linked:
                with mock.patch("workflow.merge._run_push_gate") as mock_gate:
                    with mock.patch("workflow.merge._resolve_pr_from_worktree") as mock_resolve:
                        mock_is_linked.return_value = True
                        mock_gate.return_value = []
                        mock_resolve.return_value = (42, "feature", str(wt_dir))

                        plan_obj, err = plan_merge("", cwd=wt_dir)
                        test_result(
                            "plan_merge('') in a linked worktree resolves via cwd (regression test)",
                            plan_obj and plan_obj.pr_number == 42
                        )
                        test_result(
                            "plan_merge('') doesn't use Path('').exists() branch",
                            mock_resolve.called,
                            "Should use is_linked_worktree, not Path('').exists()"
                        )

        print()
        h.summarize_and_exit()
    finally:
        shutil.rmtree(fake_home, ignore_errors=True)
