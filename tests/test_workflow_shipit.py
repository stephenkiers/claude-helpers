#!/usr/bin/env python3
"""
Test suite for shipit planning and application.

Tests: plan round-trips, freshness triple validation, push failure handling,
PR create vs edit selection, cache file reading, and the branch-trust rules:
whole-cache invalidation in plan_shipit, the edit-path head probe in
apply_shipit (OPEN / head mismatch / lookup failure / MERGED / CLOSED), and the
cache write-back that stamps branch and replaces stale sections.

Run with: python3 tests/test_workflow_shipit.py
"""

import sys
import json
import contextlib
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.shipit import plan_shipit, apply_shipit, ShipitPlan, ShipitResult
from workflow.safety import Unknown
from _test_harness import Harness


URL_68 = "https://github.com/owner/repo/pull/68"
URL_123 = "https://github.com/owner/repo/pull/123"
ISSUE_55 = {
    "number": 55,
    "url": "https://github.com/owner/repo/issues/55",
    "title": "Fix the thing",
    "body": "Issue body",
    "state": "OPEN",
}


def _make_plan(**overrides):
    """Build a ShipitPlan with the defaults the existing sections use."""
    fields = dict(
        branch="feature/test",
        expected_head_sha="abc123",
        cache_hash=None,
        commit_message_path="/tmp/msg.txt",
        pr_body_path=None,
        pr_title="Test PR",
        pr_number=None,
        pr_exists=False,
        stack=None,
        base_branch=None,
        plan_hash="plan123",
    )
    fields.update(overrides)
    return ShipitPlan(**fields)


def _cache_path(root):
    return root / ".claude" / "github-cache.json"


def _write_cache(root, data=None, raw=None):
    path = _cache_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw if raw is not None else json.dumps(data))
    return path


def _read_cache(root):
    return json.loads(_cache_path(root).read_text())


@contextlib.contextmanager
def _plan_env(branch, head="abc123"):
    """Patch the git reads and every gh call plan_shipit could reach; yield the gh mocks."""
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch("workflow.git.get_current_branch", return_value=branch))
        stack.enter_context(mock.patch("workflow.git.get_head_sha", return_value=head))
        gh = {
            "run_gh_command": stack.enter_context(mock.patch("workflow.git.run_gh_command")),
            "pr_view_json": stack.enter_context(mock.patch("workflow.git.pr_view_json")),
            "pr_create": stack.enter_context(mock.patch("workflow.git.pr_create")),
            "pr_edit": stack.enter_context(mock.patch("workflow.git.pr_edit")),
        }
        yield gh


def _plan_from_cache(root, data=None, raw=None, branch="feature/test"):
    """Optionally write a cache, run plan_shipit under mocks, return (plan, gh mocks)."""
    msg = root / "msg.txt"
    msg.write_text("Test commit")
    if data is not None or raw is not None:
        _write_cache(root, data=data, raw=raw)
    with _plan_env(branch) as gh:
        plan, _err = plan_shipit(str(msg), cwd=root)
    return plan, gh


@contextlib.contextmanager
def _apply_env(branch, view_result=None, create_result=(None, None)):
    """Patch every git and gh call apply_shipit can make; yield the mocks by name."""
    names = (
        "get_current_branch", "get_head_sha", "stage_all", "commit_with_message_file",
        "push_upstream", "pr_create", "pr_edit", "pr_view_json",
    )
    with contextlib.ExitStack() as stack:
        mocks = {name: stack.enter_context(mock.patch(f"workflow.git.{name}")) for name in names}
        mocks["get_current_branch"].return_value = branch
        mocks["get_head_sha"].return_value = "abc123"
        mocks["stage_all"].return_value = (True, None)
        mocks["commit_with_message_file"].return_value = (True, None)
        mocks["push_upstream"].return_value = (True, None)
        mocks["pr_create"].return_value = create_result
        mocks["pr_edit"].return_value = (True, None)
        mocks["pr_view_json"].return_value = view_result if view_result is not None else {}
        yield mocks


def _apply_files(root):
    msg = root / "msg.txt"
    body = root / "body.md"
    msg.write_text("Test commit")
    body.write_text("Body text")
    return msg, body


def _apply_plan_json(msg, body, **overrides):
    """Serialized apply plan for PR 68 (edit by default); overrides replace any field."""
    fields = dict(
        commit_message_path=str(msg),
        pr_body_path=str(body),
        pr_title="Updated PR",
        pr_number=68,
        pr_exists=True,
    )
    fields.update(overrides)
    return json.dumps(_make_plan(**fields).to_dict())


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

        # The edit path probes the PR head before editing; the probe must match the branch.
        with _apply_env("feature/test", view_result={"headRefName": "feature/test", "state": "OPEN"}) as mocks:
            result, err = apply_shipit(plan_json, cwd=tmppath)

        test_result(
            "apply_shipit: calls pr_edit when pr_exists=True",
            mocks["pr_edit"].called,
            f"error: {err}"
        )
        test_result(
            "apply_shipit: does not call pr_create when pr_exists=True",
            not mocks["pr_create"].called
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

        # Create .claude/github-cache.json with pr section, stamped with the current branch
        claude_dir = tmppath / ".claude"
        claude_dir.mkdir()
        cache_file = claude_dir / "github-cache.json"
        cache_data = {
            "branch": "feature/test",
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
    print("[Section 11] ShipitPlan branch-trust fields (issue_number, cache_stale, stale_stack_claimed)")

    plan = _make_plan(issue_number=55, cache_stale="branch_mismatch:old-branch", stale_stack_claimed=True)
    restored = ShipitPlan.from_dict(plan.to_dict())
    test_result(
        "ShipitPlan round-trip keeps issue_number",
        restored.issue_number == 55,
        f"got {restored.issue_number!r}"
    )
    test_result(
        "ShipitPlan round-trip keeps cache_stale",
        restored.cache_stale == "branch_mismatch:old-branch",
        f"got {restored.cache_stale!r}"
    )
    test_result(
        "ShipitPlan round-trip keeps stale_stack_claimed",
        restored.stale_stack_claimed is True
    )

    legacy_dict = {
        k: v for k, v in plan.to_dict().items()
        if k not in ("issue_number", "cache_stale", "stale_stack_claimed")
    }
    legacy = ShipitPlan.from_dict(legacy_dict)
    test_result(
        "ShipitPlan.from_dict() on pre-change JSON defaults issue_number to None",
        legacy.issue_number is None
    )
    test_result(
        "ShipitPlan.from_dict() on pre-change JSON defaults cache_stale to None",
        legacy.cache_stale is None
    )
    test_result(
        "ShipitPlan.from_dict() on pre-change JSON defaults stale_stack_claimed to False",
        legacy.stale_stack_claimed is False
    )

    print()
    print("[Section 12] plan_shipit trusts github-cache.json only when its branch is the current branch")

    with tempfile.TemporaryDirectory() as tmpdir:
        plan, gh = _plan_from_cache(
            Path(tmpdir),
            data={
                "branch": "old-branch",
                "pr": {"number": 68, "url": URL_68},
                "stack": {"isStacked": True, "parentBranch": "feature/parent", "parentPr": 10},
                "issue": ISSUE_55,
            },
            branch="new-branch",
        )
    test_result(
        "plan_shipit: incident repro sets pr_exists=False",
        plan is not None and plan.pr_exists is False,
        f"plan={plan}"
    )
    test_result(
        "plan_shipit: incident repro sets pr_number=None",
        plan is not None and plan.pr_number is None
    )
    test_result(
        "plan_shipit: incident repro drops the cached stack",
        plan is not None and plan.stack is None
    )
    test_result(
        "plan_shipit: incident repro drops the cached issue_number",
        plan is not None and plan.issue_number is None
    )
    test_result(
        "plan_shipit: incident repro reports cache_stale=branch_mismatch:old-branch",
        plan is not None and plan.cache_stale == "branch_mismatch:old-branch",
        f"cache_stale={plan.cache_stale if plan else 'N/A'}"
    )
    test_result(
        "plan_shipit: incident repro flags stale_stack_claimed=True",
        plan is not None and plan.stale_stack_claimed is True
    )
    test_result(
        "plan_shipit: incident repro makes no PR lookup",
        not gh["pr_view_json"].called
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        plan, gh = _plan_from_cache(
            Path(tmpdir),
            data={"pr": {"number": 68, "url": URL_68}},
        )
    test_result(
        "plan_shipit: cache without branch sets cache_stale=invalid",
        plan is not None and plan.cache_stale == "invalid",
        f"cache_stale={plan.cache_stale if plan else 'N/A'}"
    )
    test_result(
        "plan_shipit: cache without branch sets pr_exists=False",
        plan is not None and plan.pr_exists is False
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        plan, gh = _plan_from_cache(Path(tmpdir), raw="{not json")
    test_result(
        "plan_shipit: non-JSON cache sets cache_stale=invalid",
        plan is not None and plan.cache_stale == "invalid",
        f"cache_stale={plan.cache_stale if plan else 'N/A'}"
    )
    test_result(
        "plan_shipit: non-JSON cache sets pr_exists=False",
        plan is not None and plan.pr_exists is False
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        plan, gh = _plan_from_cache(Path(tmpdir), raw=json.dumps([1, 2]))
    test_result(
        "plan_shipit: JSON array cache sets cache_stale=invalid",
        plan is not None and plan.cache_stale == "invalid",
        f"cache_stale={plan.cache_stale if plan else 'N/A'}"
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        plan, gh = _plan_from_cache(
            Path(tmpdir),
            data={"branch": "feature/test", "issue": ISSUE_55, "localPlan": {"path": "plans/x.md"}},
        )
    test_result(
        "plan_shipit: /track-shaped cache (no schema_version) is trusted",
        plan is not None and plan.cache_stale is None,
        f"cache_stale={plan.cache_stale if plan else 'N/A'}"
    )
    test_result(
        "plan_shipit: /track-shaped cache reports issue_number=55",
        plan is not None and plan.issue_number == 55,
        f"issue_number={plan.issue_number if plan else 'N/A'}"
    )
    test_result(
        "plan_shipit: /track-shaped cache with no pr sets pr_exists=False",
        plan is not None and plan.pr_exists is False and plan.pr_number is None
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        plan, gh = _plan_from_cache(
            Path(tmpdir),
            data={"branch": "feature/test", "pr": {"number": 68, "url": URL_68}},
        )
    test_result(
        "plan_shipit: trusted cache with positive int pr.number sets pr_exists=True",
        plan is not None and plan.pr_exists is True,
        f"pr_exists={plan.pr_exists if plan else 'N/A'}"
    )
    test_result(
        "plan_shipit: trusted cache with positive int pr.number sets pr_number=68",
        plan is not None and plan.pr_number == 68,
        f"pr_number={plan.pr_number if plan else 'N/A'}"
    )

    for bad_number in ("68", 0, -1, True):
        with tempfile.TemporaryDirectory() as tmpdir:
            plan, gh = _plan_from_cache(
                Path(tmpdir),
                data={"branch": "feature/test", "pr": {"number": bad_number, "url": URL_68}},
            )
        test_result(
            f"plan_shipit: pr.number={bad_number!r} is treated as absent (pr_exists=False)",
            plan is not None and plan.pr_exists is False,
            f"pr_exists={plan.pr_exists if plan else 'N/A'}"
        )
        test_result(
            f"plan_shipit: pr.number={bad_number!r} is treated as absent (pr_number=None)",
            plan is not None and plan.pr_number is None,
            f"pr_number={plan.pr_number if plan else 'N/A'}"
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        plan, gh = _plan_from_cache(Path(tmpdir))
    test_result(
        "plan_shipit: missing cache sets cache_stale=missing",
        plan is not None and plan.cache_stale == "missing",
        f"cache_stale={plan.cache_stale if plan else 'N/A'}"
    )
    test_result(
        "plan_shipit: missing cache sets stale_stack_claimed=False",
        plan is not None and plan.stale_stack_claimed is False
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        plan, gh = _plan_from_cache(
            Path(tmpdir),
            data={"review": {"branch": "feature-test", "commit": "abc123"}},
        )
    test_result(
        "plan_shipit: review-only cache sets cache_stale=invalid",
        plan is not None and plan.cache_stale == "invalid",
        f"cache_stale={plan.cache_stale if plan else 'N/A'}"
    )
    test_result(
        "plan_shipit: review-only cache sets stale_stack_claimed=False",
        plan is not None and plan.stale_stack_claimed is False
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        plan, gh = _plan_from_cache(
            Path(tmpdir),
            data={"branch": "old-branch", "pr": {"number": 68, "url": URL_68}},
            branch="new-branch",
        )
    test_result(
        "plan_shipit: branch mismatch without a stack sets stale_stack_claimed=False",
        plan is not None and plan.stale_stack_claimed is False
        and plan.cache_stale == "branch_mismatch:old-branch",
        f"cache_stale={plan.cache_stale if plan else 'N/A'}, "
        f"stale_stack_claimed={plan.stale_stack_claimed if plan else 'N/A'}"
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        plan, gh = _plan_from_cache(
            Path(tmpdir),
            data={"branch": "old-branch", "stack": {"isStacked": False, "parentBranch": "x", "parentPr": None}},
            branch="new-branch",
        )
    test_result(
        "plan_shipit: branch mismatch with isStacked=false sets stale_stack_claimed=False",
        plan is not None and plan.stale_stack_claimed is False
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        plan, gh = _plan_from_cache(
            Path(tmpdir),
            data={"branch": "old-branch", "stack": {"isStacked": True, "parentBranch": "feature/parent", "parentPr": 10}},
            branch="new-branch",
        )
    test_result(
        "plan_shipit: branch mismatch with isStacked=true sets stale_stack_claimed=True",
        plan is not None and plan.stale_stack_claimed is True,
        f"stale_stack_claimed={plan.stale_stack_claimed if plan else 'N/A'}"
    )
    test_result(
        "plan_shipit: branch mismatch with isStacked=true drops base_branch",
        plan is not None and plan.base_branch is None,
        f"base_branch={plan.base_branch if plan else 'N/A'}"
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        plan, gh = _plan_from_cache(
            Path(tmpdir),
            data={"stack": {"isStacked": True, "parentBranch": "feature/parent", "parentPr": 10}},
        )
    test_result(
        "plan_shipit: branchless cache with isStacked=true sets stale_stack_claimed=True",
        plan is not None and plan.cache_stale == "invalid" and plan.stale_stack_claimed is True,
        f"cache_stale={plan.cache_stale if plan else 'N/A'}, "
        f"stale_stack_claimed={plan.stale_stack_claimed if plan else 'N/A'}"
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        plan, gh = _plan_from_cache(
            Path(tmpdir),
            data={"branch": "feature/test", "stack": {"isStacked": True, "parentBranch": "feature/parent", "parentPr": 10}},
        )
    test_result(
        "plan_shipit: trusted stacked cache sets cache_stale=None",
        plan is not None and plan.cache_stale is None,
        f"cache_stale={plan.cache_stale if plan else 'N/A'}"
    )
    test_result(
        "plan_shipit: trusted stacked cache sets stale_stack_claimed=False",
        plan is not None and plan.stale_stack_claimed is False
    )
    test_result(
        "plan_shipit: trusted stacked cache populates plan.stack",
        plan is not None and plan.stack is not None
    )
    test_result(
        "plan_shipit: trusted stacked cache sets base_branch to the cached parent",
        plan is not None and plan.base_branch == "feature/parent",
        f"base_branch={plan.base_branch if plan else 'N/A'}"
    )

    print()
    print("[Section 13] plan_shipit is hash-stable, network-free, and accepts /dev/null as message path")

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _write_cache(root, {"branch": "feature/test", "pr": {"number": 68, "url": URL_68}})
        plan_a, gh_a = _plan_from_cache(root)
        plan_b, gh_b = _plan_from_cache(root)
    test_result(
        "plan_shipit: same inputs produce the same plan_hash",
        plan_a is not None and plan_b is not None and plan_a.plan_hash == plan_b.plan_hash,
        f"a={plan_a.plan_hash if plan_a else 'N/A'} b={plan_b.plan_hash if plan_b else 'N/A'}"
    )
    test_result(
        "plan_shipit: planning makes no pr_view_json or gh call",
        not gh_a["pr_view_json"].called and not gh_a["run_gh_command"].called
        and not gh_b["pr_view_json"].called and not gh_b["run_gh_command"].called
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _write_cache(root, {"branch": "feature/test", "issue": ISSUE_55})
        with _plan_env("feature/test") as gh:
            plan, err = plan_shipit("/dev/null", cwd=root)
    test_result(
        "plan_shipit: /dev/null message path plans successfully",
        plan is not None and plan.commit_message_path == "/dev/null",
        f"plan={plan}, error={err}"
    )
    test_result(
        "plan_shipit: /dev/null planning makes no gh call",
        not gh["pr_view_json"].called and not gh["run_gh_command"].called
    )

    print()
    print("[Section 14] apply_shipit edit path: live head probe before pr_edit")

    # 14a: OPEN and head matches -> pr_edit once, no create, cache pr kept, branch/schema stamped.
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        msg, body = _apply_files(root)
        _write_cache(root, {"branch": "feature/test", "issue": ISSUE_55, "pr": {"number": 68, "url": URL_68}})
        plan_json = _apply_plan_json(msg, body)
        with _apply_env("feature/test", view_result={"headRefName": "feature/test", "state": "OPEN"}) as mocks:
            result, err = apply_shipit(plan_json, cwd=root)
        cache_after = _read_cache(root)

    test_result(
        "apply_shipit edit: probes the cached PR number with headRefName and state",
        mocks["pr_view_json"].call_count == 1
        and mocks["pr_view_json"].call_args.args[:2] == ("68", ["headRefName", "state"]),
        f"calls={mocks['pr_view_json'].call_args_list}"
    )
    test_result(
        "apply_shipit edit: OPEN and head matches calls pr_edit exactly once",
        mocks["pr_edit"].call_count == 1 and mocks["pr_edit"].call_args.kwargs.get("pr_number") == 68,
        f"calls={mocks['pr_edit'].call_args_list}"
    )
    test_result(
        "apply_shipit edit: OPEN and head matches never calls pr_create",
        not mocks["pr_create"].called
    )
    test_result(
        "apply_shipit edit: result.pr_url stays unset on the edit path",
        result.pr_url is None,
        f"pr_url={result.pr_url!r}"
    )
    test_result(
        "apply_shipit edit: write-back keeps the existing pr section unchanged",
        cache_after.get("pr") == {"number": 68, "url": URL_68},
        f"pr={cache_after.get('pr')!r}"
    )
    test_result(
        "apply_shipit edit: write-back keeps the cached issue",
        cache_after.get("issue") == ISSUE_55,
        f"issue={cache_after.get('issue')!r}"
    )
    test_result(
        "apply_shipit edit: write-back stamps branch",
        cache_after.get("branch") == "feature/test",
        f"branch={cache_after.get('branch')!r}"
    )
    test_result(
        "apply_shipit edit: write-back stamps schema_version 1.0",
        cache_after.get("schema_version") == "1.0",
        f"schema_version={cache_after.get('schema_version')!r}"
    )

    # 14b: head mismatch -> Unknown, no mutation, pushed stays True, cache untouched.
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        msg, body = _apply_files(root)
        cache_before = _write_cache(root, {"branch": "feature/test", "pr": {"number": 68, "url": URL_68}}).read_bytes()
        plan_json = _apply_plan_json(msg, body)
        with _apply_env("feature/test", view_result={"headRefName": "other-branch", "state": "OPEN"}) as mocks:
            result, err = apply_shipit(plan_json, cwd=root)
        cache_now = _cache_path(root).read_bytes()

    test_result(
        "apply_shipit head mismatch: result.error is Unknown",
        isinstance(result.error, Unknown),
        f"error={result.error!r}"
    )
    test_result(
        "apply_shipit head mismatch: error names the cached PR and its actual head",
        isinstance(result.error, Unknown)
        and result.error.reason.startswith("cached PR #68 belongs to other-branch, not feature/test.")
        and "Branch was pushed" in result.error.reason
        and "the PR was not touched" in result.error.reason
        and "Retry if the lookup failed" in result.error.reason,
        f"reason={getattr(result.error, 'reason', None)!r}"
    )
    test_result(
        "apply_shipit head mismatch: neither pr_edit nor pr_create is called",
        not mocks["pr_edit"].called and not mocks["pr_create"].called
    )
    test_result(
        "apply_shipit head mismatch: committed and pushed stay True",
        result.committed is True and result.pushed is True
    )
    test_result(
        "apply_shipit head mismatch: cache file is not rewritten",
        cache_now == cache_before
    )

    # 14c: lookup returns {} -> Unknown, no mutation.
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        msg, body = _apply_files(root)
        _write_cache(root, {"branch": "feature/test", "pr": {"number": 68, "url": URL_68}})
        plan_json = _apply_plan_json(msg, body)
        with _apply_env("feature/test", view_result={}) as mocks:
            result, err = apply_shipit(plan_json, cwd=root)

    test_result(
        "apply_shipit lookup {}: result.error is the lookup-failed Unknown",
        isinstance(result.error, Unknown)
        and result.error.reason.startswith("could not verify PR #68 belongs to feature/test: lookup failed.")
        and "the PR was not touched" in result.error.reason,
        f"reason={getattr(result.error, 'reason', None)!r}"
    )
    test_result(
        "apply_shipit lookup {}: neither pr_edit nor pr_create is called",
        not mocks["pr_edit"].called and not mocks["pr_create"].called
    )
    test_result(
        "apply_shipit lookup {}: pushed stays True",
        result.pushed is True
    )

    # 14d: response missing state -> same lookup-failed Unknown, no mutation.
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        msg, body = _apply_files(root)
        _write_cache(root, {"branch": "feature/test", "pr": {"number": 68, "url": URL_68}})
        plan_json = _apply_plan_json(msg, body)
        with _apply_env("feature/test", view_result={"headRefName": "feature/test"}) as mocks:
            result, err = apply_shipit(plan_json, cwd=root)

    test_result(
        "apply_shipit lookup missing state: result.error is the lookup-failed Unknown",
        isinstance(result.error, Unknown)
        and result.error.reason.startswith("could not verify PR #68 belongs to feature/test: lookup failed.")
        and "the PR was not touched" in result.error.reason,
        f"reason={getattr(result.error, 'reason', None)!r}"
    )
    test_result(
        "apply_shipit lookup missing state: neither pr_edit nor pr_create is called",
        not mocks["pr_edit"].called and not mocks["pr_create"].called
    )

    # 14e: MERGED and head matches -> create a new PR, never edit, cache takes the new number.
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        msg, body = _apply_files(root)
        _write_cache(root, {"branch": "feature/test", "issue": ISSUE_55, "pr": {"number": 68, "url": URL_68}})
        plan_json = _apply_plan_json(msg, body, base_branch=None)
        with _apply_env(
            "feature/test",
            view_result={"headRefName": "feature/test", "state": "MERGED"},
            create_result=(URL_123, None),
        ) as mocks:
            result, err = apply_shipit(plan_json, cwd=root)
        cache_after = _read_cache(root)

    test_result(
        "apply_shipit MERGED: pr_create is called exactly once",
        mocks["pr_create"].call_count == 1,
        f"calls={mocks['pr_create'].call_args_list}"
    )
    test_result(
        "apply_shipit MERGED: pr_edit is never called",
        not mocks["pr_edit"].called
    )
    test_result(
        "apply_shipit MERGED: result records the new PR as created",
        result.pr_created is True and result.pr_url == URL_123,
        f"pr_created={result.pr_created!r} pr_url={result.pr_url!r}"
    )
    test_result(
        "apply_shipit MERGED: cache pr.number becomes the new PR number",
        cache_after.get("pr") == {"number": 123, "url": URL_123, "state": "OPEN"},
        f"pr={cache_after.get('pr')!r}"
    )

    # 14f: MERGED but the create URL does not parse -> no pr section, no edit, no stale number.
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        msg, body = _apply_files(root)
        _write_cache(root, {"branch": "feature/test", "pr": {"number": 68, "url": URL_68}})
        plan_json = _apply_plan_json(msg, body)
        with _apply_env(
            "feature/test",
            view_result={"headRefName": "feature/test", "state": "MERGED"},
            create_result=("https://github.com/owner/repo/pull/not-a-number", None),
        ) as mocks:
            result, err = apply_shipit(plan_json, cwd=root)
        cache_after = _read_cache(root)
        with _plan_env("feature/test") as gh:
            fresh_plan, _ = plan_shipit(str(msg), cwd=root)

    test_result(
        "apply_shipit MERGED unparseable: pr_create is called once",
        mocks["pr_create"].call_count == 1
    )
    test_result(
        "apply_shipit MERGED unparseable: pr_edit is never called",
        not mocks["pr_edit"].called
    )
    test_result(
        "apply_shipit MERGED unparseable: cache has no pr section (merged number not kept)",
        "pr" not in cache_after,
        f"cache={cache_after}"
    )
    test_result(
        "apply_shipit MERGED unparseable: a fresh plan reports pr_exists=False",
        fresh_plan is not None and fresh_plan.pr_exists is False and fresh_plan.pr_number is None,
        f"fresh_plan={fresh_plan}"
    )

    # 14g: CLOSED -> Unknown, no mutation, cache untouched.
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        msg, body = _apply_files(root)
        cache_before = _write_cache(root, {"branch": "feature/test", "pr": {"number": 68, "url": URL_68}}).read_bytes()
        plan_json = _apply_plan_json(msg, body)
        with _apply_env("feature/test", view_result={"headRefName": "feature/test", "state": "CLOSED"}) as mocks:
            result, err = apply_shipit(plan_json, cwd=root)
        cache_now = _cache_path(root).read_bytes()

    test_result(
        "apply_shipit CLOSED: result.error reason is the CLOSED refusal",
        isinstance(result.error, Unknown)
        and result.error.reason == (
            "cached PR #68 for feature/test is CLOSED; not editing or creating. "
            "Reopen it or clear .claude/github-cache.json pr and re-run"
        ),
        f"reason={getattr(result.error, 'reason', None)!r}"
    )
    test_result(
        "apply_shipit CLOSED: neither pr_edit nor pr_create is called",
        not mocks["pr_edit"].called and not mocks["pr_create"].called
    )
    test_result(
        "apply_shipit CLOSED: cache file is not rewritten",
        cache_now == cache_before
    )

    # 14h: any other non-OPEN, non-MERGED state is also refused without mutation.
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        msg, body = _apply_files(root)
        _write_cache(root, {"branch": "feature/test", "pr": {"number": 68, "url": URL_68}})
        plan_json = _apply_plan_json(msg, body)
        with _apply_env("feature/test", view_result={"headRefName": "feature/test", "state": "DRAFT"}) as mocks:
            result, err = apply_shipit(plan_json, cwd=root)

    test_result(
        "apply_shipit unknown state DRAFT: result.error is Unknown",
        isinstance(result.error, Unknown),
        f"error={result.error!r}"
    )
    test_result(
        "apply_shipit unknown state DRAFT: neither pr_edit nor pr_create is called",
        not mocks["pr_edit"].called and not mocks["pr_create"].called
    )

    # 14i: create path (pr_exists=False) makes no probe call.
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        msg, body = _apply_files(root)
        _write_cache(root, {"branch": "feature/test", "issue": ISSUE_55})
        plan_json = _apply_plan_json(msg, body, pr_number=None, pr_exists=False)
        with _apply_env("feature/test", create_result=(URL_123, None)) as mocks:
            result, err = apply_shipit(plan_json, cwd=root)

    test_result(
        "apply_shipit create path: pr_view_json is not called",
        not mocks["pr_view_json"].called
    )
    test_result(
        "apply_shipit create path: pr_create is called once",
        mocks["pr_create"].call_count == 1
    )

    print()
    print("[Section 15] apply_shipit write-back stamps branch and replaces stale sections")

    # 15a: create from a branch_mismatch cache drops old stack/issue/localPlan/review.
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        msg, body = _apply_files(root)
        _write_cache(root, {
            "branch": "old-branch",
            "pr": {"number": 68, "url": URL_68},
            "stack": {"isStacked": True, "parentBranch": "feature/parent", "parentPr": 10},
            "issue": ISSUE_55,
            "localPlan": {"path": "plans/old.md"},
            "review": {"branch": "old-branch", "commit": "old111"},
        })
        plan = _make_plan(
            branch="feature/new",
            commit_message_path=str(msg),
            pr_body_path=str(body),
            pr_title="New PR",
            pr_exists=False,
            cache_stale="branch_mismatch:old-branch",
            stale_stack_claimed=True,
        )
        with _apply_env("feature/new", create_result=(URL_123, None)) as mocks:
            result, err = apply_shipit(json.dumps(plan.to_dict()), cwd=root)
        cache_after = _read_cache(root)

    test_result(
        "write-back after branch_mismatch create: branch is the plan branch",
        cache_after.get("branch") == "feature/new",
        f"branch={cache_after.get('branch')!r}"
    )
    test_result(
        "write-back after branch_mismatch create: schema_version is 1.0",
        cache_after.get("schema_version") == "1.0"
    )
    test_result(
        "write-back after branch_mismatch create: pr is the new PR as OPEN",
        cache_after.get("pr") == {"number": 123, "url": URL_123, "state": "OPEN"},
        f"pr={cache_after.get('pr')!r}"
    )
    test_result(
        "write-back after branch_mismatch create: old stack, issue, localPlan, review are gone",
        not any(k in cache_after for k in ("stack", "issue", "localPlan", "review")),
        f"keys={sorted(cache_after.keys())}"
    )

    # 15b: create from a review-only (invalid) cache keeps review when review.branch matches.
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        msg, body = _apply_files(root)
        review = {"branch": "feature-x", "commit": "abc123", "lastRun": "2026-10-01"}
        _write_cache(root, {"review": review})
        plan = _make_plan(
            branch="feature/x",
            commit_message_path=str(msg),
            pr_body_path=str(body),
            pr_title="X PR",
            pr_exists=False,
            cache_stale="invalid",
            stale_stack_claimed=False,
        )
        with _apply_env("feature/x", create_result=(URL_123, None)) as mocks:
            result, err = apply_shipit(json.dumps(plan.to_dict()), cwd=root)
        cache_after = _read_cache(root)

    test_result(
        "write-back after review-only create: review for this branch is preserved",
        cache_after.get("review") == review,
        f"review={cache_after.get('review')!r}"
    )
    test_result(
        "write-back after review-only create: branch is stamped",
        cache_after.get("branch") == "feature/x"
    )

    # 15c: a cache first created by shipit is trusted by the next plan_shipit.
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        msg, body = _apply_files(root)
        plan = _make_plan(
            branch="feature/test",
            commit_message_path=str(msg),
            pr_body_path=str(body),
            pr_title="Test PR",
            pr_exists=False,
            cache_stale="missing",
        )
        with _apply_env("feature/test", create_result=(URL_123, None)) as mocks:
            result, err = apply_shipit(json.dumps(plan.to_dict()), cwd=root)
        with _plan_env("feature/test"):
            next_plan, _ = plan_shipit(str(msg), cwd=root)

    test_result(
        "shipit-created cache is trusted on the next plan_shipit (cache_stale=None)",
        next_plan is not None and next_plan.cache_stale is None,
        f"cache_stale={next_plan.cache_stale if next_plan else 'N/A'}"
    )
    test_result(
        "shipit-created cache makes the next plan report pr_exists=True",
        next_plan is not None and next_plan.pr_exists is True and next_plan.pr_number == 123,
        f"pr_exists={next_plan.pr_exists if next_plan else 'N/A'} "
        f"pr_number={next_plan.pr_number if next_plan else 'N/A'}"
    )

    print()
    h.summarize_and_exit()
