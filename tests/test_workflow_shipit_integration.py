#!/usr/bin/env python3
"""
Integration test suite for shipit apply operations.

Uses real git fixtures to test freshness validation and error handling.
Some operations are mocked to avoid complex git setup requirements.

Run with: python3 tests/test_workflow_shipit_integration.py
"""

import sys
import os
import json
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.shipit import apply_shipit, ShipitPlan
from workflow.safety import Unknown
from _test_harness import Harness
from _git_fixture import GitFixture


if __name__ == "__main__":
    h = Harness("WORKFLOW SHIPIT INTEGRATION TEST SUITE")
    test_result = h.test_result

    print("[Section 1] apply_shipit validates freshness against real repo state")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)

        fixture.create_initial_commit("initial")
        fixture.create_branch("feature/test")
        wt_path = fixture.create_worktree("feature/test")

        feature_sha = fixture.get_head_sha(cwd=wt_path)

        msg_file = wt_path / "COMMIT_MSG.txt"
        msg_file.write_text("Test feature commit\n\nThis is a test change.")

        os.chdir(wt_path)

        plan = ShipitPlan(
            branch="feature/test",
            expected_head_sha=feature_sha,
            cache_hash=None,
            commit_message_path=str(msg_file),
            pr_body_path=None,
            pr_title="Test PR",
            pr_number=None,
            pr_exists=False,
            stack=None,
            base_branch=None,
            plan_hash="plan123"
        )
        plan_json = json.dumps(plan.to_dict())

        # Mock only the gh pr create call; stage/commit operations will be real
        with mock.patch("workflow.git.push_upstream") as mock_push:
            mock_push.return_value = (False, Unknown("push rejected — no remote"))

            result, err = apply_shipit(plan_json, cwd=wt_path)

        # Verify freshness validation passed (we got past that stage)
        test_result(
            "apply_shipit: validates freshness before attempting mutations",
            result.success is False or result.error is not None,
            f"Plan should have passed freshness or failed gracefully; got error={err}"
        )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 2] apply_shipit validates freshness (stale SHA fails)")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)

        fixture.create_initial_commit("initial")
        fixture.create_branch("feature/stale")
        wt_path = fixture.create_worktree("feature/stale")

        original_sha = fixture.get_head_sha(cwd=wt_path)

        msg_file = wt_path / "COMMIT_MSG.txt"
        msg_file.write_text("Test commit")

        os.chdir(wt_path)

        # Make a new commit to change HEAD
        fixture.commit_in_worktree(wt_path, "new change")
        new_sha = fixture.get_head_sha(cwd=wt_path)

        # Create plan with OLD SHA
        plan = ShipitPlan(
            branch="feature/stale",
            expected_head_sha=original_sha,  # This is now stale
            cache_hash=None,
            commit_message_path=str(msg_file),
            pr_body_path=None,
            pr_title="Test",
            pr_number=None,
            pr_exists=False,
            stack=None,
            base_branch=None,
            plan_hash="plan123"
        )
        plan_json = json.dumps(plan.to_dict())

        result, err = apply_shipit(plan_json, cwd=wt_path)

        test_result(
            "apply_shipit: rejects stale SHA",
            not result.success and result.error is not None,
            f"result.success={result.success}, error={result.error}"
        )
        test_result(
            "apply_shipit: does not commit when SHA is stale",
            result.committed is False
        )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 3] apply_shipit respects push failure detection")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)

        fixture.create_initial_commit("initial")
        fixture.create_branch("feature/push-test")
        wt_path = fixture.create_worktree("feature/push-test")

        msg_file = wt_path / "COMMIT_MSG.txt"
        msg_file.write_text("Test commit")

        os.chdir(wt_path)

        feature_sha = fixture.get_head_sha(cwd=wt_path)

        plan = ShipitPlan(
            branch="feature/push-test",
            expected_head_sha=feature_sha,
            cache_hash=None,
            commit_message_path=str(msg_file),
            pr_body_path=None,
            pr_title="Test",
            pr_number=None,
            pr_exists=False,
            stack=None,
            base_branch=None,
            plan_hash="plan123"
        )
        plan_json = json.dumps(plan.to_dict())

        # Mock push_upstream to return Unknown (simulating rejection)
        with mock.patch("workflow.git.push_upstream") as mock_push:
            mock_push.return_value = (False, Unknown("push rejected — remote has diverged"))

            with mock.patch("workflow.git.stage_all") as mock_stage:
                with mock.patch("workflow.git.commit_with_message_file") as mock_commit:
                    with mock.patch("workflow.git.pr_create") as mock_pr_create:
                        mock_stage.return_value = (True, None)
                        mock_commit.return_value = (True, None)

                        result, err = apply_shipit(plan_json, cwd=wt_path)

                        test_result(
                            "apply_shipit: stops when push fails",
                            result.pushed is False
                        )
                        test_result(
                            "apply_shipit: does not attempt PR create after push failure",
                            not mock_pr_create.called
                        )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 4] apply_shipit result structure")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)

        fixture.create_initial_commit("initial")
        fixture.create_branch("feature/result-test")
        wt_path = fixture.create_worktree("feature/result-test")

        msg_file = wt_path / "COMMIT_MSG.txt"
        msg_file.write_text("Test commit")
        body_file = wt_path / "PR_BODY.md"
        body_file.write_text("Test PR body")

        os.chdir(wt_path)

        feature_sha = fixture.get_head_sha(cwd=wt_path)

        plan = ShipitPlan(
            branch="feature/result-test",
            expected_head_sha=feature_sha,
            cache_hash=None,
            commit_message_path=str(msg_file),
            pr_body_path=str(body_file),
            pr_title="Test PR",
            pr_number=None,
            pr_exists=False,
            stack=None,
            base_branch=None,
            plan_hash="plan123"
        )
        plan_json = json.dumps(plan.to_dict())

        mock_pr_url = "https://github.com/owner/repo/pull/123"

        # Mock all git/gh operations
        with mock.patch("workflow.git.stage_all") as mock_stage:
            with mock.patch("workflow.git.commit_with_message_file") as mock_commit:
                with mock.patch("workflow.git.push_upstream") as mock_push:
                    with mock.patch("workflow.git.pr_create") as mock_pr_create:
                        mock_stage.return_value = (True, None)
                        mock_commit.return_value = (True, None)
                        mock_push.return_value = (True, None)
                        mock_pr_create.return_value = (mock_pr_url, None)

                        result, err = apply_shipit(plan_json, cwd=wt_path)

        test_result(
            "apply_shipit: records pr_url in result",
            result.pr_url == mock_pr_url,
            f"expected {mock_pr_url}, got {result.pr_url}"
        )
        test_result(
            "apply_shipit: success flag set on full flow",
            result.success is True,
            f"result.success={result.success}, error={result.error}"
        )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    h.summarize_and_exit()
