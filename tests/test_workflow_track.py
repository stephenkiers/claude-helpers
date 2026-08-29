#!/usr/bin/env python3
"""
Test suite for track module: helpers, providers, plan/apply functions.

Run with: python3 tests/test_workflow_track.py
"""

import sys
import os
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.track import (
    slugify, infer_type, infer_labels, build_branch_name,
    plan_track, apply_track, TrackApplyResult,
    ISSUE_NUMBER_PLACEHOLDER, STEP_CREATE_ISSUE
)
from workflow.models import LocalTrackerData, LocalTrackerEntry, IssueInfo
from workflow.cache import read_local_tracker, write_local_tracker
from workflow.providers.local import LocalProvider
from _test_harness import Harness
from _git_fixture import GitFixture


if __name__ == "__main__":
    h = Harness("WORKFLOW TRACK TEST SUITE")
    test_result = h.test_result

    print("[Section 1] slugify helper function")

    test_result(
        "slugify: basic title",
        slugify("Add Auth System") == "add-auth-system"
    )

    test_result(
        "slugify: removes non-alphanumeric",
        slugify("Fix: Bug #123!") == "fix-bug-123"
    )

    test_result(
        "slugify: collapses multiple dashes",
        slugify("Test---Multiple----Dashes") == "test-multiple-dashes"
    )

    test_result(
        "slugify: strips leading/trailing dashes",
        slugify("---Leading and Trailing---") == "leading-and-trailing"
    )

    test_result(
        "slugify: respects max_len parameter",
        len(slugify("This is a very long title that should be truncated", max_len=20)) <= 20
    )

    test_result(
        "slugify: truncate doesn't leave trailing dash",
        not slugify("Test Title", max_len=5).endswith("-")
    )

    test_result(
        "slugify: empty string",
        slugify("") == ""
    )

    print()
    print("[Section 2] infer_type helper function")

    test_result(
        "infer_type: fix keywords in title",
        infer_type("Fix critical bug in auth") == "fix"
    )

    test_result(
        "infer_type: broken keyword",
        infer_type("Something is broken") == "fix"
    )

    test_result(
        "infer_type: crash keyword",
        infer_type("Handle crash on startup") == "fix"
    )

    test_result(
        "infer_type: feature keywords in title",
        infer_type("Add new dashboard") == "feature"
    )

    test_result(
        "infer_type: implement keyword",
        infer_type("Implement search functionality") == "feature"
    )

    test_result(
        "infer_type: create keyword",
        infer_type("Create API endpoint") == "feature"
    )

    test_result(
        "infer_type: chore keywords in title",
        infer_type("Refactor database layer") == "chore"
    )

    test_result(
        "infer_type: cleanup keyword",
        infer_type("Cleanup dead code") == "chore"
    )

    test_result(
        "infer_type: rename keyword",
        infer_type("Rename variables for clarity") == "chore"
    )

    test_result(
        "infer_type: move keyword prioritized by type order",
        # "Move" contains "new" which is a feature keyword, so feature wins first
        infer_type("Move to new location") == "feature"
    )

    test_result(
        "infer_type: falls back to content when title has no match",
        infer_type("Random title", content="Need to fix the database") == "fix"
    )

    test_result(
        "infer_type: type precedence (fix before feature before chore)",
        # fix keywords checked first in iteration order
        infer_type("Add feature", content="Fix bug") == "fix"
    )

    test_result(
        "infer_type: default is feature",
        infer_type("Random title with no keywords") == "feature"
    )

    test_result(
        "infer_type: case insensitive matching",
        infer_type("FIX CRASH") == "fix"
    )

    test_result(
        "infer_type: whole-word match only",
        infer_type("prefix is not a match") == "feature"  # "fix" is in "prefix" but shouldn't match
    )

    print()
    print("[Section 3] infer_labels helper function")

    test_result(
        "infer_labels: bug label from 'fix' keyword",
        "bug" in infer_labels("Fix issue")
    )

    test_result(
        "infer_labels: enhancement label from 'add' keyword",
        "enhancement" in infer_labels("Add feature")
    )

    test_result(
        "infer_labels: documentation label",
        "documentation" in infer_labels("Update README")
    )

    test_result(
        "infer_labels: chore label",
        "chore" in infer_labels("Refactor code")
    )

    test_result(
        "infer_labels: multiple labels",
        len(infer_labels("Add and fix bug documentation")) > 1
    )

    test_result(
        "infer_labels: deduplicates",
        infer_labels("fix fix bug bug").count("bug") == 1
    )

    test_result(
        "infer_labels: empty when no match",
        infer_labels("Random title") == []
    )

    test_result(
        "infer_labels: case insensitive",
        "bug" in infer_labels("BUG FIX")
    )

    test_result(
        "infer_labels: searches content when title has no match",
        "bug" in infer_labels("Random title", content="Fix issue")
    )

    print()
    print("[Section 4] build_branch_name helper function")

    test_result(
        "build_branch_name: basic format",
        build_branch_name("feature", 42, "my-feature") == "feature/42-my-feature"
    )

    test_result(
        "build_branch_name: with placeholder",
        build_branch_name("fix", ISSUE_NUMBER_PLACEHOLDER, "crash-fix") == f"fix/{ISSUE_NUMBER_PLACEHOLDER}-crash-fix"
    )

    test_result(
        "build_branch_name: with string issue number",
        build_branch_name("feature", "PPS-166", "api-endpoint") == "feature/PPS-166-api-endpoint"
    )

    print()
    print("[Section 5] LocalProvider implementation")

    tmpdir = Path(tempfile.mkdtemp())
    try:
        tracker_path = tmpdir / "issues.json"
        plans_dir = tmpdir / "plans"

        provider = LocalProvider(tracker_path=tracker_path, plans_dir=plans_dir)

        test_result(
            "LocalProvider.list_open_issues() returns empty list for missing tracker",
            provider.list_open_issues() == []
        )

        # Create tracker with some entries
        initial_tracker = LocalTrackerData(entries=[
            LocalTrackerEntry(id=1, title="Task 1", status="todo"),
            LocalTrackerEntry(id=2, title="Task 2", status="planned"),
            LocalTrackerEntry(id=3, title="Task 3", status="done"),  # Should be filtered out
        ])
        write_local_tracker(tracker_path, initial_tracker)

        issues = provider.list_open_issues()
        test_result(
            "LocalProvider.list_open_issues() filters by status",
            len(issues) == 2 and all(i.state == "open" for i in issues)
        )

        test_result(
            "LocalProvider.list_open_issues() maps to IssueInfo",
            issues[0].number == 1 and issues[0].title == "Task 1"
        )

        # Test create_issue
        new_issue = provider.create_issue(
            title="New issue",
            body="Issue body",
            labels=["feature"],
            assignee=None
        )

        test_result(
            "LocalProvider.create_issue() allocates new ID",
            new_issue.number == 4
        )

        test_result(
            "LocalProvider.create_issue() returns IssueInfo",
            isinstance(new_issue, IssueInfo) and new_issue.title == "New issue"
        )

        test_result(
            "LocalProvider.create_issue() creates plan file",
            (plans_dir / "4-new-issue.md").exists()
        )

        test_result(
            "LocalProvider.create_issue() writes plan content",
            (plans_dir / "4-new-issue.md").read_text() == "Issue body"
        )

        # Test edit_issue_body
        provider.edit_issue_body(4, "Updated body")
        test_result(
            "LocalProvider.edit_issue_body() updates plan file",
            (plans_dir / "4-new-issue.md").read_text() == "Updated body"
        )

        # Test comment_issue (should be no-op)
        provider.comment_issue(4, "Comment text")
        test_result(
            "LocalProvider.comment_issue() is a no-op (no error)",
            True
        )

        test_result(
            "LocalProvider.repo_identity() returns None",
            provider.repo_identity() is None
        )

        test_result(
            "LocalProvider.current_user() returns None",
            provider.current_user() is None
        )

    finally:
        import shutil
        shutil.rmtree(tmpdir)

    print()
    print("[Section 6] plan_track with GitHub provider (mocked)")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)
        fixture.create_initial_commit("Initial commit")

        # Mock GitHub provider
        class MockGithubProvider:
            def repo_identity(self):
                return ("owner", "repo")

            def current_user(self):
                return "testuser"

            def list_open_issues(self):
                return []

            def create_issue(self, title, body, labels, assignee):
                pass

            def comment_issue(self, number, body):
                pass

            def edit_issue_body(self, number, body):
                pass

        provider = MockGithubProvider()

        plan, err = plan_track(
            provider=provider,
            plan_content="This is the plan",
            title="Add new feature",
            mode="github",
            assignee=None,
            cwd=fixture.repo_root
        )

        test_result(
            "plan_track: succeeds for GitHub mode",
            plan is not None and err is None,
            f"error: {err}"
        )

        test_result(
            "plan_track: sets mode",
            plan.mode == "github"
        )

        test_result(
            "plan_track: computes slug",
            plan.slug == "add-new-feature"
        )

        test_result(
            "plan_track: infers type",
            plan.issue_type == "feature"
        )

        test_result(
            "plan_track: branch uses placeholder",
            ISSUE_NUMBER_PLACEHOLDER in plan.branch
        )

        test_result(
            "plan_track: worktree_path uses placeholder",
            ISSUE_NUMBER_PLACEHOLDER in plan.worktree_path
        )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 7] apply_track result structure")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)
        fixture.create_initial_commit("Initial commit")

        tmpdir = Path(tempfile.mkdtemp())
        tracker_path = tmpdir / "issues.json"
        plans_dir = tmpdir / "plans"

        provider = LocalProvider(tracker_path=tracker_path, plans_dir=plans_dir)

        # Create a plan
        plan, err = plan_track(
            provider=provider,
            plan_content="This is the implementation plan",
            title="Implement feature",
            mode="local",
            assignee=None,
            cwd=fixture.repo_root
        )

        test_result(
            "plan_track: succeeds for local mode",
            plan is not None and err is None
        )

        plan_json = json.dumps(plan.to_dict())

        # Apply the plan
        result, err = apply_track(provider, plan_json, cwd=fixture.repo_root)

        test_result(
            "apply_track: returns TrackApplyResult",
            isinstance(result, TrackApplyResult)
        )

        test_result(
            "apply_track: has issue_number",
            result.issue_number == 1
        )

        test_result(
            "apply_track: has branch",
            result.branch == "feature/1-implement-feature"
        )

        test_result(
            "apply_track: branch does not contain placeholder",
            ISSUE_NUMBER_PLACEHOLDER not in result.branch
        )

        test_result(
            "apply_track: has worktree_path",
            result.worktree_path is not None and result.worktree_path.endswith("1-implement-feature")
        )

        test_result(
            "apply_track: records issue creation in steps_completed",
            STEP_CREATE_ISSUE in result.steps_completed and STEP_CREATE_ISSUE not in result.steps_failed
        )

        # Verify the tracker was updated
        data, err = read_local_tracker(tracker_path)
        test_result(
            "apply_track: tracker file was created/updated",
            data is not None and len(data.entries) > 0
        )

        import shutil
        shutil.rmtree(tmpdir)

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 8] apply_track stale plan detection")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)
        fixture.create_initial_commit("Initial commit")

        tmpdir = Path(tempfile.mkdtemp())
        tracker_path = tmpdir / "issues.json"
        plans_dir = tmpdir / "plans"

        provider = LocalProvider(tracker_path=tracker_path, plans_dir=plans_dir)

        # Create a plan with a specific HEAD SHA
        plan, err = plan_track(
            provider=provider,
            plan_content="Implementation plan",
            title="Test issue",
            mode="local",
            cwd=fixture.repo_root
        )

        # Modify the repository to make the plan stale
        new_file = fixture.repo_root / "test.txt"
        new_file.write_text("test content")
        fixture._run_git(["add", "test.txt"], cwd=fixture.repo_root)
        fixture._run_git(["commit", "-m", "Change repo"], cwd=fixture.repo_root)

        plan_json = json.dumps(plan.to_dict())

        # Try to apply the now-stale plan
        result, err = apply_track(provider, plan_json, cwd=fixture.repo_root)

        test_result(
            "apply_track: rejects stale plan (HEAD SHA mismatch)",
            result.success is False and result.error is not None,
            f"error: {err}"
        )

        import shutil
        shutil.rmtree(tmpdir)

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 9] apply_track reports partial success")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)
        fixture.create_initial_commit("Initial commit")

        tmpdir = Path(tempfile.mkdtemp())
        tracker_path = tmpdir / "issues.json"
        plans_dir = tmpdir / "plans"

        # Create a provider that will fail on worktree creation
        class FailingWorktreeProvider:
            def repo_identity(self):
                return ("owner", "repo")

            def current_user(self):
                return "testuser"

            def list_open_issues(self):
                return []

            def create_issue(self, title, body, labels, assignee):
                return IssueInfo(number=1, url="https://github.com/owner/repo/issues/1",
                                title=title, body=body, state="open")

            def comment_issue(self, number, body):
                pass

            def edit_issue_body(self, number, body):
                pass

        provider = FailingWorktreeProvider()

        # Create a plan
        plan, err = plan_track(
            provider=provider,
            plan_content="Implementation plan",
            title="Test issue",
            mode="github",
            cwd=fixture.repo_root
        )

        # Make the worktree path invalid so creation will fail
        plan.worktree_path = "/invalid/path/that/will/fail"
        plan_json = json.dumps(plan.to_dict())

        # Try to apply the plan with an invalid worktree path
        result, err = apply_track(provider, plan_json, cwd=fixture.repo_root)

        test_result(
            "apply_track: partial success when worktree creation fails",
            result.success is False and STEP_CREATE_ISSUE in result.steps_completed,
            f"steps_completed: {result.steps_completed}"
        )

        test_result(
            "apply_track: issue was created despite worktree failure",
            result.issue_number == 1
        )

        import shutil
        shutil.rmtree(tmpdir)

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    h.summarize_and_exit()
