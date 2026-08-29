#!/usr/bin/env python3
"""
Test suite for merge lock file fixes.

Tests the three gaps mentioned in the code review:
1. Lock cleanup on worktree removal (apply_cleanup)
2. Lock directory permissions (0o700 after apply_merge creates it)
3. Error message discoverability (lock file path in FileExistsError)

Run with: python3 tests/test_merge_lock_fixes.py
"""

import sys
import json
import os
import stat
import shutil
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.merge import apply_merge, merge_lock_path, MergePlan
from workflow.cleanup import apply_cleanup, CleanupPlan, CleanupResult
from workflow.safety import Unknown
from _test_harness import Harness


if __name__ == "__main__":
    fake_home = tempfile.mkdtemp()
    os.environ["HOME"] = fake_home

    try:
        h = Harness("MERGE LOCK FIXES TEST SUITE")
        test_result = h.test_result

        print("[Section 1] Lock directory permissions (Fix 2)")
        print()

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

            with mock.patch("workflow.merge._check_just_merge") as mock_just:
                with mock.patch("workflow.merge._run_gh_pr_merge") as mock_merge:
                    mock_just.return_value = False
                    mock_merge.return_value = (True, None)

                    plan_json = json.dumps(plan.to_dict())
                    result, err = apply_merge(plan_json)

                    lock_file = merge_lock_path(str(wt_dir))
                    lock_dir = lock_file.parent

                    test_result(
                        "Lock directory is created by apply_merge",
                        lock_dir.exists(),
                        f"Lock directory should exist at {lock_dir}"
                    )

                    if lock_dir.exists():
                        dir_mode = stat.S_IMODE(lock_dir.stat().st_mode)
                        test_result(
                            "Lock directory has 0o700 permissions",
                            dir_mode == 0o700,
                            f"Expected 0o700, got {oct(dir_mode)}"
                        )

            if lock_file.exists():
                lock_file.unlink()

        print()
        print("[Section 2] Lock cleanup on worktree removal (Fix 1)")
        print()

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

            lock_file = merge_lock_path(str(wt_dir))
            lock_file.parent.mkdir(parents=True, exist_ok=True)
            lock_file.write_text("locked")

            test_result(
                "Lock file exists before cleanup",
                lock_file.exists(),
                "Lock file should be created for this test"
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
                                    "apply_cleanup succeeds",
                                    result.success is True,
                                    f"Cleanup should succeed, got error: {result.error}"
                                )

                                test_result(
                                    "apply_cleanup removes the worktree",
                                    result.worktree_removed is True,
                                    "worktree_removed should be True"
                                )

                                test_result(
                                    "apply_cleanup removes the lock file after successful worktree removal",
                                    not lock_file.exists(),
                                    f"Lock file should be unlinked after worktree removal at {lock_file}"
                                )

        print()
        print("[Section 3] Lock cleanup is safe when worktree removal fails")
        print()

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

            lock_file = merge_lock_path(str(wt_dir))
            lock_file.parent.mkdir(parents=True, exist_ok=True)
            lock_file.write_text("locked")

            test_result(
                "Lock file exists before cleanup attempt",
                lock_file.exists(),
                "Lock file should be created for this test"
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
                                mock_remove.return_value = (False, Unknown("Permission denied"))
                                mock_pull.return_value = (True, None)

                                result, err = apply_cleanup(plan_json)

                                test_result(
                                    "apply_cleanup fails as expected",
                                    result.success is False,
                                    "Cleanup should fail when worktree removal fails"
                                )

                                test_result(
                                    "apply_cleanup does not remove lock file when worktree_removed is False",
                                    lock_file.exists(),
                                    "Lock file should remain when worktree removal fails"
                                )

            lock_file.unlink()

        print()
        print("[Section 4] Lock cleanup does not crash on missing lock file")
        print()

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

            lock_file = merge_lock_path(str(wt_dir))
            lock_file.parent.mkdir(parents=True, exist_ok=True)

            test_result(
                "Lock file does not exist (intentional for this test)",
                not lock_file.exists(),
                "Lock file should not exist for this safety test"
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
                                    "apply_cleanup succeeds even without lock file",
                                    result.success is True,
                                    "Cleanup should not crash if lock file is missing (missing_ok=True)"
                                )

        print()
        print("[Section 5] FileExistsError message includes lock file path (Fix 3)")
        print()

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

            lock_file = merge_lock_path(str(wt_dir))
            lock_file.parent.mkdir(parents=True, exist_ok=True)
            lock_file.write_text("locked by another process")

            plan_json = json.dumps(plan.to_dict())
            result, err = apply_merge(plan_json)

            test_result(
                "apply_merge detects existing lock file and fails",
                result.success is False and result.error is not None,
                "Should fail when lock file already exists"
            )

            error_message = str(result.error)
            lock_path_str = str(lock_file)

            test_result(
                "Error message includes the lock file path",
                lock_path_str in error_message,
                f"Expected '{lock_path_str}' in error message, got: {error_message}"
            )

            lock_file.unlink()

        print()
        print("[Section 6] FileExistsError with unreadable existing lock still includes path (Fix 3)")
        print()

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

            lock_file = merge_lock_path(str(wt_dir))
            lock_file.parent.mkdir(parents=True, exist_ok=True)
            lock_file.write_text("existing lock")

            with mock.patch("workflow.merge.Path.home", return_value=Path(fake_home)):
                with mock.patch("builtins.open", side_effect=IOError("Permission denied")):
                    plan_json = json.dumps(plan.to_dict())
                    result, err = apply_merge(plan_json)

                    test_result(
                        "apply_merge fails when lock exists and is unreadable",
                        result.success is False,
                        "Should fail due to lock file existence"
                    )

                    error_message = str(result.error)
                    lock_path_str = str(lock_file)

                    test_result(
                        "Error message includes lock file path even when reading fails",
                        lock_path_str in error_message,
                        f"Expected '{lock_path_str}' in error message, got: {error_message}"
                    )

            lock_file.unlink()

        print()
        h.summarize_and_exit()

    finally:
        shutil.rmtree(fake_home, ignore_errors=True)
