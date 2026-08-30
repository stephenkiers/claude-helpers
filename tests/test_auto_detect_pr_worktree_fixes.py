#!/usr/bin/env python3
"""
Spec-blind tests for PR auto-detection and worktree classification fixes.

Covers the three findings from issue #120:
- Finding 1 (HIGH): is_linked_worktree misclassifies main worktree when cwd is nested
- Finding 2 (MEDIUM) + Finding 5 (LOW): Error message attribution in plan_merge
- Finding 4 (LOW): Error message wording for "not in a linked worktree"

Run with: python3 tests/test_auto_detect_pr_worktree_fixes.py
"""

import sys
import os
import json
import tempfile
import shutil
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import workflow.git as git_module
from workflow.merge import plan_merge
from workflow.safety import Unknown
from _test_harness import Harness
from _git_fixture import GitFixture


if __name__ == "__main__":
    h = Harness("AUTO-DETECT PR/WORKTREE FIXES TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Finding 1 (HIGH): is_linked_worktree with nested cwd in main worktree")

    # The bug: when cwd is a *subdirectory* of the main worktree (not the root),
    # relative/absolute path comparison could misclassify it as linked.
    # Fix ensures both git-dir and git-common-dir are normalized to resolved absolute paths.

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        # Create the initial repo and a linked worktree for comparison
        os.chdir(fixture.repo_root)
        fixture.create_initial_commit("initial")
        fixture.create_branch("feature/test")
        wt_path = fixture.create_worktree("feature/test")

        # Test 1a: Subdirectory of main worktree should NOT be classified as linked
        subdir = fixture.main_worktree / "src" / "nested" / "deep"
        subdir.mkdir(parents=True, exist_ok=True)
        is_linked = git_module.is_linked_worktree(subdir)
        test_result(
            "is_linked_worktree(nested-subdir-of-main) returns False",
            is_linked is False,
            f"got {is_linked} when calling from {subdir}. This is the bug from Finding 1."
        )

        # Test 1b: Even when we chdir into the nested subdir, is_linked_worktree(None) should return False
        old_cwd_inner = os.getcwd()
        try:
            os.chdir(subdir)
            is_linked = git_module.is_linked_worktree(None)
            test_result(
                "is_linked_worktree(None) from nested-subdir-of-main returns False",
                is_linked is False,
                f"got {is_linked} when cwd is {os.getcwd()}. This is the nested-cwd bug."
            )
        finally:
            os.chdir(old_cwd_inner)

        # Test 1c: Sanity check - linked worktree should still return True
        is_linked = git_module.is_linked_worktree(wt_path)
        test_result(
            "is_linked_worktree(linked-worktree) still returns True",
            is_linked is True,
            f"got {is_linked}. Regression: fix broke linked worktree detection."
        )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 2] Finding 4 (LOW): Error message wording for 'not in a linked worktree'")

    # The fix ensures the error message says "linked worktree" to clarify that
    # the main worktree doesn't qualify (e.g., "No argument provided and not in a linked worktree...")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        wt_dir = tmppath / "worktree"
        wt_dir.mkdir()

        with mock.patch("workflow.merge.git.is_linked_worktree") as mock_is_linked:
            mock_is_linked.return_value = False

            plan_obj, err = plan_merge(None, cwd=wt_dir)
            test_result(
                "plan_merge(None) in main worktree returns error",
                plan_obj is None and err is not None,
                f"Expected error, got plan={plan_obj}, err={err}"
            )
            test_result(
                "Error message says 'linked worktree' not just 'worktree'",
                "linked worktree" in str(err).lower(),
                f"got: {err}. The message must clarify it's a *linked* worktree, not just any worktree."
            )

    print()
    print("[Section 3] Finding 2/5 (MEDIUM+LOW): Error message attribution for PR resolution")

    # The bug: when PR resolution fails, the error message was reused incorrectly.
    # Fix: extract shared helper _unresolved_pr_message parameterized by whether
    # target is "current branch" (auto-detect) or explicit worktree's checked-out branch.

    print()
    print("[Section 3a] Auto-detect case: error references 'current branch'")

    # When plan_merge(None) is called from a linked worktree and PR resolution fails,
    # the error should mention "current branch has no associated PR" or similar.
    # (It's the invoking shell's current branch, so "current" is appropriate.)

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)
        fixture.create_initial_commit("initial")
        fixture.create_branch("test-branch")
        wt_path = fixture.create_worktree("test-branch")

        with mock.patch("workflow.merge.git.is_linked_worktree") as mock_is_linked:
            with mock.patch("workflow.merge._run_push_gate") as mock_gate:
                with mock.patch("workflow.merge._resolve_pr_from_worktree") as mock_resolve:
                    # Simulate PR resolution failure in auto-detect case (None argument)
                    mock_is_linked.return_value = True
                    mock_gate.return_value = []
                    mock_resolve.return_value = (None, None, Unknown("No open PR found for this branch"))

                    plan_obj, err = plan_merge(None, cwd=wt_path)

                    test_result(
                        "plan_merge(None) with PR resolution failure returns error",
                        plan_obj is None and err is not None,
                        f"Expected error, got plan={plan_obj}, err={err}"
                    )

                    # The error should reference "current branch" since it's the auto-detect case
                    # (the invoking shell's current branch in the linked worktree)
                    error_str = str(err).lower()
                    has_current_ref = "current branch" in error_str or "this branch" in error_str
                    test_result(
                        "Auto-detect case: error references 'current branch' or 'this branch'",
                        has_current_ref,
                        f"got: {err}. In the auto-detect (no-arg) case, should reference current/this branch."
                    )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 3b] Explicit path case: error should NOT say 'current branch'")

    # When plan_merge(explicit_path) is called with an explicit worktree path
    # (not None/auto-detect) and PR resolution fails, the error should NOT say
    # "current branch" because it's not the invoking shell's current branch.
    # Instead, it should reference the target worktree's branch.

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)
        fixture.create_initial_commit("initial")
        fixture.create_branch("explicit-test")
        wt_path = fixture.create_worktree("explicit-test")

        with mock.patch("workflow.merge._run_push_gate") as mock_gate:
            with mock.patch("workflow.merge._resolve_pr_from_worktree") as mock_resolve:
                # Simulate PR resolution failure with explicit path
                mock_gate.return_value = []
                mock_resolve.return_value = (None, None, Unknown("No open PR found for this branch"))

                # Call with explicit worktree path (string), not None
                plan_obj, err = plan_merge(str(wt_path))

                test_result(
                    "plan_merge(explicit_path) with PR resolution failure returns error",
                    plan_obj is None and err is not None,
                    f"Expected error, got plan={plan_obj}, err={err}"
                )

                error_str = str(err).lower()
                # In explicit path case, the error should NOT reference "current branch"
                # (it's a different context - not the invoking shell's branch)
                # It may reference the branch name, worktree path, or other details instead.
                has_current_ref = "current branch" in error_str
                test_result(
                    "Explicit path case: error does NOT say 'current branch'",
                    not has_current_ref,
                    f"got: {err}. In explicit-path case, should NOT reference 'current branch' - "
                    f"it's the target worktree's branch, not the invoking shell's current branch."
                )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 4] Regression: Linked worktree detection still works correctly")

    # Ensure the fixes didn't break normal linked worktree detection

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)
        fixture.create_initial_commit("initial")
        fixture.create_branch("regression-test")
        wt_path = fixture.create_worktree("regression-test")

        # Test that we can still detect a linked worktree correctly
        is_linked = git_module.is_linked_worktree(wt_path)
        test_result(
            "Linked worktree still detected correctly after fix",
            is_linked is True,
            f"got {is_linked}"
        )

        # Test from inside the linked worktree
        old_cwd_inner = os.getcwd()
        try:
            os.chdir(wt_path)
            is_linked = git_module.is_linked_worktree(None)
            test_result(
                "is_linked_worktree(None) from linked worktree still works",
                is_linked is True,
                f"got {is_linked}"
            )
        finally:
            os.chdir(old_cwd_inner)

        # Test that main worktree is still correctly identified as NOT linked
        is_linked = git_module.is_linked_worktree(fixture.main_worktree)
        test_result(
            "Main worktree still correctly identified as NOT linked",
            is_linked is False,
            f"got {is_linked}"
        )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    h.summarize_and_exit()
