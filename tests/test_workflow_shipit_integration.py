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
import contextlib
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.shipit import apply_shipit, plan_shipit, ShipitPlan
from workflow.safety import Unknown
from _test_harness import Harness
from _git_fixture import GitFixture


URL_68 = "https://github.com/owner/repo/pull/68"
URL_69 = "https://github.com/owner/repo/pull/69"


@contextlib.contextmanager
def _remote_and_gh_mocks(create_url=None):
    """Mock only the network and mutating calls; git reads in plan/apply stay real."""
    names = ("stage_all", "commit_with_message_file", "push_upstream", "pr_create", "pr_edit", "pr_view_json")
    with contextlib.ExitStack() as stack:
        mocks = {name: stack.enter_context(mock.patch(f"workflow.git.{name}")) for name in names}
        mocks["stage_all"].return_value = (True, None)
        mocks["commit_with_message_file"].return_value = (True, None)
        mocks["push_upstream"].return_value = (True, None)
        mocks["pr_create"].return_value = (create_url, None)
        mocks["pr_edit"].return_value = (True, None)
        mocks["pr_view_json"].return_value = {}
        yield mocks


def _read_cache(wt_path):
    """Return the worktree's cache dict, or {} when no cache file exists (so assertions fail, not crash)."""
    cache_file = wt_path / ".claude" / "github-cache.json"
    if not cache_file.exists():
        return {}
    return json.loads(cache_file.read_text())


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
    print("[Section 5] Repro (issue #213): a recycled worktree never edits the previous branch's PR")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)

        fixture.create_initial_commit("initial")
        fixture.create_branch("feature/a")
        wt_path = fixture.create_worktree("feature/a")

        msg_file = wt_path / "COMMIT_MSG.txt"
        msg_file.write_text("Ship A")
        body_file = wt_path / "PR_BODY.md"
        body_file.write_text("Body for A")

        os.chdir(wt_path)

        # Step 1: ship branch A. No cache yet, so the plan creates PR 68 and the write-back records it.
        plan_a, plan_err = plan_shipit(str(msg_file), cwd=wt_path)
        plan_a_json = plan_a.to_dict()
        plan_a_json.update(pr_body_path=str(body_file), pr_title="Ship A")
        with _remote_and_gh_mocks(create_url=URL_68) as gh_a:
            result_a, _ = apply_shipit(json.dumps(plan_a_json), cwd=wt_path)

        test_result(
            "repro step 1: shipping A creates PR 68",
            gh_a["pr_create"].call_count == 1 and result_a.pr_url == URL_68,
            f"plan_err={plan_err}, error={result_a.error}"
        )
        test_result(
            "repro step 1: shipping A writes branch feature/a and pr 68 to the cache",
            _read_cache(wt_path).get("branch") == "feature/a"
            and _read_cache(wt_path).get("pr") == {"number": 68, "url": URL_68, "state": "OPEN"},
            f"cache={_read_cache(wt_path)}"
        )

        # Step 2: recycle the same worktree onto branch B. The cache file stays behind.
        fixture._run_git(["checkout", "-b", "feature/b"], cwd=wt_path)

        # Step 3: plan and apply branch B. The stale cache must be ignored and PR 68 never edited.
        msg_file.write_text("Ship B")
        body_file.write_text("Body for B")
        plan_b, plan_b_err = plan_shipit(str(msg_file), cwd=wt_path)

        test_result(
            "repro step 3: plan for recycled B reports cache_stale=branch_mismatch:feature/a",
            plan_b is not None and plan_b.cache_stale == "branch_mismatch:feature/a",
            f"cache_stale={plan_b.cache_stale if plan_b else 'N/A'}, error={plan_b_err}"
        )
        test_result(
            "repro step 3: plan for recycled B does not claim PR 68",
            plan_b is not None and plan_b.pr_exists is False and plan_b.pr_number is None,
            f"pr_exists={plan_b.pr_exists if plan_b else 'N/A'}"
        )

        plan_b_json = plan_b.to_dict()
        plan_b_json.update(pr_body_path=str(body_file), pr_title="Ship B")
        with _remote_and_gh_mocks(create_url=URL_69) as gh_b:
            result_b, _ = apply_shipit(json.dumps(plan_b_json), cwd=wt_path)

        test_result(
            "repro step 3: apply for B calls pr_create for B",
            gh_b["pr_create"].call_count == 1,
            f"error={result_b.error}"
        )
        test_result(
            "repro step 3: apply for B never calls pr_edit with PR 68",
            not any(c.kwargs.get("pr_number") == 68 for c in gh_b["pr_edit"].call_args_list)
            and not gh_b["pr_edit"].called,
            f"pr_edit calls={gh_b['pr_edit'].call_args_list}"
        )
        test_result(
            "repro step 3: apply for B never probes PR 68",
            not any(c.args[:1] == ("68",) for c in gh_b["pr_view_json"].call_args_list),
            f"pr_view_json calls={gh_b['pr_view_json'].call_args_list}"
        )
        test_result(
            "repro step 3: cache is rewritten for B with pr 69",
            _read_cache(wt_path).get("branch") == "feature/b"
            and _read_cache(wt_path).get("pr") == {"number": 69, "url": URL_69, "state": "OPEN"},
            f"cache={_read_cache(wt_path)}"
        )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    h.summarize_and_exit()
