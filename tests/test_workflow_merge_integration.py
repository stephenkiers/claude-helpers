#!/usr/bin/env python3
"""
Integration test suite for merge apply operations.

Tests lock file reentrancy behavior and merge gate selection.
Must be run from within the fixture repo root for git commands to work correctly.

Run with: python3 tests/test_workflow_merge_integration.py
"""

import sys
import os
import json
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.merge import apply_merge, merge_lock_path, MergePlan, MergeResult
from _test_harness import Harness
from _git_fixture import GitFixture


if __name__ == "__main__":
    h = Harness("WORKFLOW MERGE INTEGRATION TEST SUITE")
    test_result = h.test_result

    print("[Section 1] apply_merge creates lock file in .claude directory")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)

        fixture.create_initial_commit("initial")
        fixture.create_branch("feature/merge-test")
        wt_path = fixture.create_worktree("feature/merge-test")

        home_dir = fixture.repo_root.parent / "fake-home"
        home_dir.mkdir(parents=True, exist_ok=True)

        fixture.commit_in_worktree(wt_path, "add content")

        plan = MergePlan(
            pr_number=42,
            head_ref="feature/merge-test",
            target_worktree=str(wt_path)
        )
        plan_json = json.dumps(plan.to_dict())

        with mock.patch("workflow.merge.Path.home", return_value=home_dir):
            lock_file = merge_lock_path(str(wt_path))
            result, err = apply_merge(plan_json)

        test_result(
            "apply_merge creates lock file",
            lock_file.exists(),
            "lock file should exist under ~/.claude/state/merge-locks/"
        )

        test_result(
            "apply_merge does not write the lock file inside the target worktree",
            not (wt_path / ".claude" / ".merge-and-cleanup.lock").exists()
        )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 2] apply_merge lock file blocks concurrent attempts")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)

        fixture.create_initial_commit("initial")
        fixture.create_branch("feature/concurrent")
        wt_path = fixture.create_worktree("feature/concurrent")

        home_dir = fixture.repo_root.parent / "fake-home"
        home_dir.mkdir(parents=True, exist_ok=True)

        fixture.commit_in_worktree(wt_path, "add content")

        plan = MergePlan(
            pr_number=43,
            head_ref="feature/concurrent",
            target_worktree=str(wt_path)
        )
        plan_json = json.dumps(plan.to_dict())

        with mock.patch("workflow.merge.Path.home", return_value=home_dir):
            result1, err1 = apply_merge(plan_json)
            result1_succeeded = result1.success if result1 else False

            result2, err2 = apply_merge(plan_json)
        test_result(
            "Second apply_merge call is rejected (lock exists)",
            result2.success is False and result2.error is not None,
            "second attempt should be rejected due to existing lock"
        )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 3] Lock file persists after apply_merge completion")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)

        fixture.create_initial_commit("initial")
        fixture.create_branch("feature/persistent")
        wt_path = fixture.create_worktree("feature/persistent")

        home_dir = fixture.repo_root.parent / "fake-home"
        home_dir.mkdir(parents=True, exist_ok=True)

        fixture.commit_in_worktree(wt_path, "add content")

        plan = MergePlan(
            pr_number=44,
            head_ref="feature/persistent",
            target_worktree=str(wt_path)
        )
        plan_json = json.dumps(plan.to_dict())

        with mock.patch("workflow.merge.Path.home", return_value=home_dir):
            lock_file = merge_lock_path(str(wt_path))
            result, err = apply_merge(plan_json)

        test_result(
            "Lock file exists after apply_merge completes",
            lock_file.exists(),
            "lock file should NOT be auto-cleared after completion"
        )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 4] apply_merge handles blocking failures")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)

        fixture.create_initial_commit("initial")
        fixture.create_branch("feature/blocked")
        wt_path = fixture.create_worktree("feature/blocked")

        fixture.commit_in_worktree(wt_path, "add content")

        plan = MergePlan(
            pr_number=45,
            head_ref="feature/blocked",
            target_worktree=str(wt_path),
            blocking_failures=["detached HEAD", "uncommitted changes"]
        )
        plan_json = json.dumps(plan.to_dict())

        result, err = apply_merge(plan_json)

        test_result(
            "apply_merge rejects plan with blocking failures",
            result.success is False and result.error is not None,
            "plan with blocking failures should be rejected"
        )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 5] apply_merge lock file blocks after failure")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)

        fixture.create_initial_commit("initial")
        fixture.create_branch("feature/failed")
        wt_path = fixture.create_worktree("feature/failed")

        home_dir = fixture.repo_root.parent / "fake-home"
        home_dir.mkdir(parents=True, exist_ok=True)

        fixture.commit_in_worktree(wt_path, "add content")

        plan = MergePlan(
            pr_number=46,
            head_ref="feature/failed",
            target_worktree=str(wt_path),
            blocking_failures=["test failure"]
        )
        plan_json = json.dumps(plan.to_dict())

        plan2 = MergePlan(
            pr_number=46,
            head_ref="feature/failed",
            target_worktree=str(wt_path),
            blocking_failures=["test failure"]
        )
        plan_json2 = json.dumps(plan2.to_dict())

        with mock.patch("workflow.merge.Path.home", return_value=home_dir):
            result1, err1 = apply_merge(plan_json)
            result2, err2 = apply_merge(plan_json2)
        test_result(
            "Second apply_merge is blocked by lock file even after first failure",
            result2.success is False and result2.error is not None,
            "lock should prevent retry even after failure"
        )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    h.summarize_and_exit()
