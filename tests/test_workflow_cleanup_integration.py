#!/usr/bin/env python3
"""
Integration test suite for cleanup apply operations.

Uses real git fixtures to test worktree removal, branch deletion, and cache updates.
Must be run from within the fixture repo root for git commands to work correctly.

Run with: python3 tests/test_workflow_cleanup_integration.py
"""

import sys
import os
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.cleanup import apply_cleanup, CleanupPlan
from _test_harness import Harness
from _git_fixture import GitFixture


if __name__ == "__main__":
    h = Harness("WORKFLOW CLEANUP INTEGRATION TEST SUITE")
    test_result = h.test_result

    print("[Section 1] apply_cleanup removes worktree when plan is fresh")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)

        fixture.create_initial_commit("initial")
        fixture.create_branch("feature/test")
        wt_path = fixture.create_worktree("feature/test")

        feature_sha = fixture.get_head_sha(cwd=wt_path)

        plan = CleanupPlan(
            target_worktree=str(wt_path),
            current_branch="feature/test",
            pr_state="MERGED",
            pr_number=42,
            expected_head_sha=feature_sha,
            cache_hash=None,
            check_commands=[]
        )
        plan_json = json.dumps(plan.to_dict())

        result, err = apply_cleanup(plan_json)

        test_result(
            "apply_cleanup succeeds with fresh plan",
            result.success is True,
            f"error: {err}"
        )
        test_result(
            "apply_cleanup removes worktree",
            not fixture.worktree_exists(wt_path),
            "worktree should be removed from git worktree list"
        )
        test_result(
            "worktree_removed flag is set",
            result.worktree_removed is True
        )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 2] apply_cleanup deletes branch with -D when PR state is MERGED")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)

        fixture.create_initial_commit("initial")
        fixture.create_branch("feature/merged")
        wt_path = fixture.create_worktree("feature/merged")

        feature_sha = fixture.get_head_sha(cwd=wt_path)

        plan = CleanupPlan(
            target_worktree=str(wt_path),
            current_branch="feature/merged",
            pr_state="MERGED",
            pr_number=42,
            expected_head_sha=feature_sha,
            cache_hash=None,
            check_commands=[]
        )
        plan_json = json.dumps(plan.to_dict())

        result, err = apply_cleanup(plan_json)

        test_result(
            "apply_cleanup succeeds with MERGED PR state",
            result.success is True,
            f"error: {err}"
        )
        test_result(
            "branch_deleted flag is set",
            result.branch_deleted is True
        )
        test_result(
            "branch no longer exists after cleanup",
            not fixture.branch_exists("feature/merged"),
            "branch should be deleted from git branch list"
        )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 3] apply_cleanup deletes branch with -d when PR state is OPEN")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)

        fixture.create_initial_commit("initial")
        fixture.create_branch("feature/open")
        wt_path = fixture.create_worktree("feature/open")

        feature_sha = fixture.get_head_sha(cwd=wt_path)

        plan = CleanupPlan(
            target_worktree=str(wt_path),
            current_branch="feature/open",
            pr_state="OPEN",
            pr_number=43,
            expected_head_sha=feature_sha,
            cache_hash=None,
            check_commands=[]
        )
        plan_json = json.dumps(plan.to_dict())

        result, err = apply_cleanup(plan_json)

        test_result(
            "apply_cleanup succeeds with OPEN PR state",
            result.success is True,
            f"error: {err}"
        )
        test_result(
            "branch is still deleted (just with -d flag)",
            not fixture.branch_exists("feature/open"),
            "branch should be deleted even with safe -d flag"
        )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 4] apply_cleanup rejects stale plan (HEAD SHA mismatch)")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)

        fixture.create_initial_commit("initial")
        fixture.create_branch("feature/stale")
        wt_path = fixture.create_worktree("feature/stale")

        feature_sha = fixture.get_head_sha(cwd=wt_path)

        plan = CleanupPlan(
            target_worktree=str(wt_path),
            current_branch="feature/stale",
            pr_state="MERGED",
            pr_number=44,
            expected_head_sha="deadbeefdeadbeef",
            cache_hash=None,
            check_commands=[]
        )
        plan_json = json.dumps(plan.to_dict())

        result, err = apply_cleanup(plan_json)

        test_result(
            "apply_cleanup rejects stale plan",
            result.success is False and result.error is not None,
            "stale plan should be rejected"
        )
        test_result(
            "branch is NOT deleted on stale plan",
            fixture.branch_exists("feature/stale"),
            "branch should still exist after rejected plan"
        )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 5] apply_cleanup succeeds when cache_hash is None")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)

        fixture.create_initial_commit("initial")
        fixture.create_branch("feature/no-cache")
        wt_path = fixture.create_worktree("feature/no-cache")

        feature_sha = fixture.get_head_sha(cwd=wt_path)

        plan = CleanupPlan(
            target_worktree=str(wt_path),
            current_branch="feature/no-cache",
            pr_state="MERGED",
            pr_number=45,
            expected_head_sha=feature_sha,
            cache_hash=None,
            check_commands=[]
        )
        plan_json = json.dumps(plan.to_dict())

        result, err = apply_cleanup(plan_json)

        test_result(
            "apply_cleanup succeeds with cache_hash=None",
            result.success is True,
            f"error: {err}"
        )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 6] apply_cleanup handles missing worktree gracefully")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)

        fixture.create_initial_commit("initial")
        fixture.create_branch("feature/missing")

        nonexistent_wt = fixture.worktree_parent / "feature-missing"

        plan = CleanupPlan(
            target_worktree=str(nonexistent_wt),
            current_branch="feature/missing",
            pr_state="MERGED",
            pr_number=46,
            expected_head_sha="abc123",
            cache_hash=None,
            check_commands=[]
        )
        plan_json = json.dumps(plan.to_dict())

        result, err = apply_cleanup(plan_json)

        test_result(
            "apply_cleanup fails for missing worktree",
            result.success is False and result.error is not None,
            "missing worktree should cause error"
        )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    h.summarize_and_exit()
