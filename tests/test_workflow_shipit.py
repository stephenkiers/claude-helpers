#!/usr/bin/env python3
"""
Test suite for shipit planning and application.

Tests: plan round-trips, freshness triple validation, push failure handling,
PR create vs edit selection, and cache file reading.

Run with: python3 tests/test_workflow_shipit.py
"""

import sys
import json
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.shipit import plan_shipit, apply_shipit, ShipitPlan, ShipitResult
from workflow.safety import Unknown
from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("WORKFLOW SHIPIT TEST SUITE")
    test_result = h.test_result

    print("[Section 1] ShipitPlan dataclass round-trip")

    plan = ShipitPlan(
        branch="feature/test",
        expected_head_sha="abc123",
        cache_hash="def456",
        commit_message_path="/tmp/msg.txt",
        pr_body_path="/tmp/body.md",
        pr_title="Test PR",
        pr_number=None,
        pr_exists=False,
        stack=None,
        base_branch=None,
        plan_hash="plan123"
    )

    plan_dict = plan.to_dict()
    test_result(
        "ShipitPlan.to_dict() produces valid dict",
        isinstance(plan_dict, dict) and plan_dict["branch"] == "feature/test"
    )

    restored = ShipitPlan.from_dict(plan_dict)
    test_result(
        "ShipitPlan.from_dict() restores all fields",
        restored.branch == "feature/test" and restored.pr_number is None
    )
    test_result(
        "ShipitPlan round-trip preserves pr_exists flag",
        restored.pr_exists is False
    )

    print()
    print("[Section 2] ShipitResult dataclass")

    result = ShipitResult(
        success=True,
        committed=True,
        pushed=True,
        pr_created=True,
        pr_updated=False,
        pr_url="https://github.com/owner/repo/pull/123",
        error=None
    )

    result_dict = result.to_dict()
    test_result(
        "ShipitResult.to_dict() includes success flag",
        result_dict["success"] is True
    )
    test_result(
        "ShipitResult.to_dict() includes pr_url",
        result_dict["pr_url"] == "https://github.com/owner/repo/pull/123"
    )

    result_with_error = ShipitResult(
        success=False,
        error=Unknown("push rejected")
    )
    result_dict_with_error = result_with_error.to_dict()
    test_result(
        "ShipitResult.to_dict() serializes error",
        "error" in result_dict_with_error and "push rejected" in result_dict_with_error["error"]
    )

    print()
    print("[Section 3] apply_shipit rejects stale plan (branch mismatch)")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        msg_file = tmppath / "msg.txt"
        msg_file.write_text("Test commit")

        plan = ShipitPlan(
            branch="feature/old",
            expected_head_sha="abc123",
            cache_hash="def456",
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

        with mock.patch("workflow.git.get_current_branch") as mock_branch:
            mock_branch.return_value = "feature/new"

            result, err = apply_shipit(plan_json, cwd=tmpdir)

            test_result(
                "apply_shipit rejects stale plan (branch changed)",
                not result.success and result.error is not None
            )
            test_result(
                "apply_shipit does not mutate when plan is stale",
                result.committed is False and result.pushed is False
            )

    print()
    print("[Section 4] apply_shipit rejects stale plan (HEAD SHA mismatch)")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        msg_file = tmppath / "msg.txt"
        msg_file.write_text("Test commit")

        plan = ShipitPlan(
            branch="feature/test",
            expected_head_sha="abc123",
            cache_hash="def456",
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

        with mock.patch("workflow.git.get_current_branch") as mock_branch:
            with mock.patch("workflow.git.get_head_sha") as mock_sha:
                mock_branch.return_value = "feature/test"
                mock_sha.return_value = "different_sha"

                result, err = apply_shipit(plan_json, cwd=tmppath)

                test_result(
                    "apply_shipit rejects stale plan (HEAD SHA changed)",
                    not result.success and result.error is not None
                )

    print()
    print("[Section 5] apply_shipit stops when push returns Unknown")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        msg_file = tmppath / "msg.txt"
        msg_file.write_text("Test commit")

        plan = ShipitPlan(
            branch="feature/test",
            expected_head_sha="abc123",
            cache_hash="def456",
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

        with mock.patch("workflow.git.get_current_branch") as mock_branch:
            with mock.patch("workflow.git.get_head_sha") as mock_sha:
                with mock.patch("workflow.git.stage_all") as mock_stage:
                    with mock.patch("workflow.git.commit_with_message_file") as mock_commit:
                        with mock.patch("workflow.git.push_upstream") as mock_push:
                            with mock.patch("workflow.git.pr_create") as mock_pr_create:
                                mock_branch.return_value = "feature/test"
                                mock_sha.return_value = "abc123"
                                mock_stage.return_value = (True, None)
                                mock_commit.return_value = (True, None)
                                # Push returns Unknown (diverged)
                                mock_push.return_value = (False, Unknown("push rejected — remote has diverged"))

                                result, err = apply_shipit(plan_json, cwd=tmppath)

                                test_result(
                                    "apply_shipit: push failure stops execution",
                                    not result.success and result.pushed is False
                                )
                                test_result(
                                    "apply_shipit: does not attempt PR create on push failure",
                                    not mock_pr_create.called
                                )

    print()
    print("[Section 6] apply_shipit uses pr_create when pr_exists=False")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        msg_file = tmppath / "msg.txt"
        body_file = tmppath / "body.md"
        msg_file.write_text("Test commit")
        body_file.write_text("Test body")

        plan = ShipitPlan(
            branch="feature/test",
            expected_head_sha="abc123",
            cache_hash=None,  # No cache to check
            commit_message_path=str(msg_file),
            pr_body_path=str(body_file),
            pr_title="Test PR",
            pr_number=None,
            pr_exists=False,  # New PR
            stack=None,
            base_branch=None,
            plan_hash="plan123"
        )
        plan_json = json.dumps(plan.to_dict())

        with mock.patch("workflow.git.get_current_branch") as mock_branch:
            with mock.patch("workflow.git.get_head_sha") as mock_sha:
                with mock.patch("workflow.git.stage_all") as mock_stage:
                    with mock.patch("workflow.git.commit_with_message_file") as mock_commit:
                        with mock.patch("workflow.git.push_upstream") as mock_push:
                            with mock.patch("workflow.git.pr_create") as mock_pr_create:
                                with mock.patch("workflow.git.pr_edit") as mock_pr_edit:
                                    mock_branch.return_value = "feature/test"
                                    mock_sha.return_value = "abc123"
                                    mock_stage.return_value = (True, None)
                                    mock_commit.return_value = (True, None)
                                    mock_push.return_value = (True, None)
                                    mock_pr_create.return_value = ("https://github.com/owner/repo/pull/123", None)

                                    result, err = apply_shipit(plan_json, cwd=tmppath)

                                    test_result(
                                        "apply_shipit: calls pr_create when pr_exists=False",
                                        mock_pr_create.called,
                                        f"error: {err}"
                                    )
                                    test_result(
                                        "apply_shipit: does not call pr_edit when pr_exists=False",
                                        not mock_pr_edit.called
                                    )

    print()
    print("[Section 7] apply_shipit uses pr_edit when pr_exists=True")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        msg_file = tmppath / "msg.txt"
        body_file = tmppath / "body.md"
        msg_file.write_text("Test commit")
        body_file.write_text("Updated body")

        plan = ShipitPlan(
            branch="feature/test",
            expected_head_sha="abc123",
            cache_hash=None,  # No cache to check
            commit_message_path=str(msg_file),
            pr_body_path=str(body_file),
            pr_title="Updated PR",
            pr_number=123,
            pr_exists=True,  # PR already exists
            stack=None,
            base_branch=None,
            plan_hash="plan123"
        )
        plan_json = json.dumps(plan.to_dict())

        with mock.patch("workflow.git.get_current_branch") as mock_branch:
            with mock.patch("workflow.git.get_head_sha") as mock_sha:
                with mock.patch("workflow.git.stage_all") as mock_stage:
                    with mock.patch("workflow.git.commit_with_message_file") as mock_commit:
                        with mock.patch("workflow.git.push_upstream") as mock_push:
                            with mock.patch("workflow.git.pr_create") as mock_pr_create:
                                with mock.patch("workflow.git.pr_edit") as mock_pr_edit:
                                    mock_branch.return_value = "feature/test"
                                    mock_sha.return_value = "abc123"
                                    mock_stage.return_value = (True, None)
                                    mock_commit.return_value = (True, None)
                                    mock_push.return_value = (True, None)
                                    mock_pr_edit.return_value = (True, None)

                                    result, err = apply_shipit(plan_json, cwd=tmppath)

                                    test_result(
                                        "apply_shipit: calls pr_edit when pr_exists=True",
                                        mock_pr_edit.called,
                                        f"error: {err}"
                                    )
                                    test_result(
                                        "apply_shipit: does not call pr_create when pr_exists=True",
                                        not mock_pr_create.called
                                    )

    print()
    print("[Section 8] plan_shipit reads existing PR from github-cache.json (no PR case)")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        msg_file = tmppath / "msg.txt"
        msg_file.write_text("Test commit")

        # Create .claude/github-cache.json without pr section
        claude_dir = tmppath / ".claude"
        claude_dir.mkdir()
        cache_file = claude_dir / "github-cache.json"
        cache_file.write_text(json.dumps({"stack": {}}))

        with mock.patch("workflow.git.get_current_branch") as mock_branch:
            with mock.patch("workflow.git.get_head_sha") as mock_sha:
                mock_branch.return_value = "feature/test"
                mock_sha.return_value = "abc123"

                plan, err = plan_shipit(str(msg_file), cwd=tmppath)

                test_result(
                    "plan_shipit: detects no existing PR",
                    plan is not None and plan.pr_exists is False,
                    f"plan={plan}, error={err}"
                )
                test_result(
                    "plan_shipit: sets pr_number to None when no PR",
                    plan.pr_number is None if plan else False
                )

    print()
    print("[Section 9] plan_shipit reads existing PR from github-cache.json (PR exists case)")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        msg_file = tmppath / "msg.txt"
        msg_file.write_text("Test commit")

        # Create .claude/github-cache.json with pr section
        claude_dir = tmppath / ".claude"
        claude_dir.mkdir()
        cache_file = claude_dir / "github-cache.json"
        cache_data = {
            "pr": {
                "number": 42,
                "url": "https://github.com/owner/repo/pull/42"
            }
        }
        cache_file.write_text(json.dumps(cache_data))

        with mock.patch("workflow.git.get_current_branch") as mock_branch:
            with mock.patch("workflow.git.get_head_sha") as mock_sha:
                mock_branch.return_value = "feature/test"
                mock_sha.return_value = "abc123"

                plan, err = plan_shipit(str(msg_file), cwd=tmppath)

                test_result(
                    "plan_shipit: detects existing PR",
                    plan is not None and plan.pr_exists is True,
                    f"plan={plan}, error={err}"
                )
                test_result(
                    "plan_shipit: reads pr_number from cache",
                    plan.pr_number == 42 if plan else False,
                    f"plan.pr_number={plan.pr_number if plan else 'N/A'}"
                )

    print()
    print("[Section 10] ShipitPlan includes freshness triple")

    plan = ShipitPlan(
        branch="feature/test",
        expected_head_sha="abc123",
        cache_hash="def456",
        commit_message_path="/tmp/msg.txt",
        pr_body_path=None,
        pr_title="Test",
        pr_number=None,
        pr_exists=False,
        stack=None,
        base_branch=None,
        plan_hash="plan123"
    )

    test_result(
        "ShipitPlan: has branch (freshness triple element 1)",
        plan.branch is not None
    )
    test_result(
        "ShipitPlan: has expected_head_sha (freshness triple element 2)",
        plan.expected_head_sha is not None
    )
    test_result(
        "ShipitPlan: has cache_hash (freshness triple element 3)",
        plan.cache_hash is not None
    )

    print()
    h.summarize_and_exit()
