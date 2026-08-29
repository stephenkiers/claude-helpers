#!/usr/bin/env python3
"""
Test suite for mutation allowlist and funnel.

Run with: python3 tests/test_workflow_mutations.py
"""

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.mutations import check_mutation_allowed, _matches_shape
from workflow.git import remove_worktree, delete_branch, pull_ff_only
from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("WORKFLOW MUTATIONS TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Shape matching")

    test_result(
        "_matches_shape: exact literal match",
        _matches_shape(["remove", "path"], ("remove", "<path>"))
    )

    test_result(
        "_matches_shape: rejects wrong literal",
        not _matches_shape(["remove-all", "path"], ("remove", "<path>"))
    )

    test_result(
        "_matches_shape: rejects wrong arg count",
        not _matches_shape(["remove"], ("remove", "<path>"))
    )

    test_result(
        "_matches_shape: rejects extra args",
        not _matches_shape(["remove", "path", "extra"], ("remove", "<path>"))
    )

    test_result(
        "_matches_shape: accepts placeholder",
        _matches_shape(["remove", "/tmp/wt"], ("remove", "<path>"))
    )

    test_result(
        "_matches_shape: rejects empty placeholder",
        not _matches_shape(["remove", ""], ("remove", "<path>"))
    )

    print()
    print("[Section 2] Allowlist checks")

    allowed, reason = check_mutation_allowed(["worktree", "remove", "--", "/tmp/wt"])
    test_result(
        "check_mutation_allowed: allowed worktree remove",
        allowed and reason is None
    )

    allowed, reason = check_mutation_allowed(["worktree", "remove", "--force", "--", "/tmp/wt"])
    test_result(
        "check_mutation_allowed: allowed worktree remove --force",
        allowed and reason is None
    )

    allowed, reason = check_mutation_allowed(["branch", "-d", "--", "feature"])
    test_result(
        "check_mutation_allowed: allowed branch -d",
        allowed and reason is None
    )

    allowed, reason = check_mutation_allowed(["branch", "-D", "--", "feature"])
    test_result(
        "check_mutation_allowed: allowed branch -D",
        allowed and reason is None
    )

    allowed, reason = check_mutation_allowed(["pull", "--ff-only", "--", "origin", "main"])
    test_result(
        "check_mutation_allowed: allowed pull --ff-only",
        allowed and reason is None
    )

    allowed, reason = check_mutation_allowed(["worktree", "remove", "--force", "--verbose", "/tmp/wt"])
    test_result(
        "check_mutation_allowed: rejects extra flag",
        not allowed and reason is not None
    )

    allowed, reason = check_mutation_allowed(["fetch", "origin", "main"])
    test_result(
        "check_mutation_allowed: rejects unlisted subcommand",
        not allowed and "not in mutation allowlist" in reason
    )

    allowed, reason = check_mutation_allowed(["push", "origin", "main"])
    test_result(
        "check_mutation_allowed: rejects push with wrong shape (missing -u flag)",
        not allowed and "does not match any allowed pattern" in reason
    )

    allowed, reason = check_mutation_allowed(["reset", "--hard", "HEAD"])
    test_result(
        "check_mutation_allowed: rejects dangerous reset",
        not allowed and "not in mutation allowlist" in reason
    )

    allowed, reason = check_mutation_allowed([])
    test_result(
        "check_mutation_allowed: rejects empty args",
        not allowed and reason is not None
    )

    # Fix 10: reject placeholders starting with -
    test_result(
        "_matches_shape: rejects placeholder starting with -",
        not _matches_shape(["remove", "--evil"], ("remove", "<path>"))
    )

    allowed, reason = check_mutation_allowed(["worktree", "remove", "--", "--sneaky"])
    test_result(
        "check_mutation_allowed: rejects operand starting with - in placeholder",
        not allowed and reason is not None
    )

    # Fix 13: pr merge allowlist
    allowed, reason = check_mutation_allowed(["pr", "merge", "--squash", "123"])
    test_result(
        "check_mutation_allowed: allowed pr merge --squash",
        allowed and reason is None
    )

    allowed, reason = check_mutation_allowed(["pr", "merge", "--squash", "--evil"])
    test_result(
        "check_mutation_allowed: rejects pr merge with placeholder starting with -",
        not allowed and reason is not None
    )

    print()
    print("[Section 3] Mutation functions with funnel")

    with mock.patch("workflow.git.run_git_command") as mock_run:
        mock_run.return_value = ""
        success, err = remove_worktree(Path("/tmp/wt"))
        test_result(
            "remove_worktree: calls funnel and succeeds",
            success and err is None and mock_run.called
        )

    with mock.patch("workflow.git.run_git_command") as mock_run:
        def raise_error(*args, **kwargs):
            raise RuntimeError("git failed")
        mock_run.side_effect = raise_error
        success, err = remove_worktree(Path("/tmp/wt"))
        test_result(
            "remove_worktree: returns error on git failure",
            not success and err is not None
        )

    with mock.patch("workflow.mutations.check_mutation_allowed") as mock_check:
        mock_check.return_value = (False, "not allowed")
        success, err = remove_worktree(Path("/tmp/wt"))
        test_result(
            "remove_worktree: respects funnel rejection",
            not success and "not allowed" in str(err)
        )

    with mock.patch("workflow.git.run_git_command") as mock_run:
        mock_run.return_value = ""
        success, err = delete_branch("feature", force=True)
        test_result(
            "delete_branch: calls funnel with -D when force=True",
            success and err is None and mock_run.called
        )

    with mock.patch("workflow.git.run_git_command") as mock_run:
        mock_run.return_value = ""
        success, err = delete_branch("feature", force=False)
        test_result(
            "delete_branch: calls funnel with -d when force=False",
            success and err is None and mock_run.called
        )

    with mock.patch("workflow.git.run_git_command") as mock_run:
        mock_run.return_value = ""
        success, err = pull_ff_only("origin", "main")
        test_result(
            "pull_ff_only: calls funnel",
            success and err is None and mock_run.called
        )

    print()
    print("[Section 4] New shipit-related allowlist shapes")

    # git add -A
    allowed, reason = check_mutation_allowed(["add", "-A"])
    test_result(
        "check_mutation_allowed: allowed git add -A",
        allowed and reason is None,
        f"got allowed={allowed}, reason={reason}"
    )

    allowed, reason = check_mutation_allowed(["add", "-A", "extra_arg"])
    test_result(
        "check_mutation_allowed: rejects add with extra args",
        not allowed and reason is not None
    )

    allowed, reason = check_mutation_allowed(["add", "file.txt"])
    test_result(
        "check_mutation_allowed: rejects add with wrong shape (not -A)",
        not allowed and "does not match any allowed pattern" in reason
    )

    # git commit -F <path>
    allowed, reason = check_mutation_allowed(["commit", "-F", "--", "/tmp/msg.txt"])
    test_result(
        "check_mutation_allowed: allowed git commit -F with -- separator",
        allowed and reason is None,
        f"got allowed={allowed}, reason={reason}"
    )

    allowed, reason = check_mutation_allowed(["commit", "-F", "--", ""])
    test_result(
        "check_mutation_allowed: rejects commit -F with empty path",
        not allowed and reason is not None
    )

    allowed, reason = check_mutation_allowed(["commit", "-m", "message"])
    test_result(
        "check_mutation_allowed: rejects commit -m (not allowed, only -F)",
        not allowed and "does not match any allowed pattern" in reason
    )

    # git push -u <remote> <branch>
    allowed, reason = check_mutation_allowed(["push", "-u", "origin", "feature"])
    test_result(
        "check_mutation_allowed: allowed git push -u origin branch",
        allowed and reason is None,
        f"got allowed={allowed}, reason={reason}"
    )

    # CRITICAL: reject --force variants
    allowed, reason = check_mutation_allowed(["push", "-u", "--force", "origin", "feature"])
    test_result(
        "check_mutation_allowed: rejects push with --force",
        not allowed and reason is not None,
        f"--force should NOT be allowed, got allowed={allowed}"
    )

    allowed, reason = check_mutation_allowed(["push", "--force", "-u", "origin", "feature"])
    test_result(
        "check_mutation_allowed: rejects push with --force (different position)",
        not allowed and reason is not None
    )

    allowed, reason = check_mutation_allowed(["push", "-u", "--force-with-lease", "origin", "feature"])
    test_result(
        "check_mutation_allowed: rejects push with --force-with-lease",
        not allowed and reason is not None,
        f"--force-with-lease should NOT be allowed"
    )

    allowed, reason = check_mutation_allowed(["push", "-f", "origin", "feature"])
    test_result(
        "check_mutation_allowed: rejects push with -f (short form of --force)",
        not allowed and reason is not None
    )

    allowed, reason = check_mutation_allowed(["push", "origin", "feature"])
    test_result(
        "check_mutation_allowed: rejects push without -u flag",
        not allowed and "does not match any allowed pattern" in reason
    )

    # pr create --title <title> --body-file <path>
    allowed, reason = check_mutation_allowed(["pr", "create", "--title", "Test PR", "--body-file", "/tmp/body.md"])
    test_result(
        "check_mutation_allowed: allowed pr create with title and body-file",
        allowed and reason is None,
        f"got allowed={allowed}, reason={reason}"
    )

    # pr create --title <title> --base <branch> --body-file <path> (stacked)
    allowed, reason = check_mutation_allowed(["pr", "create", "--title", "Test PR", "--base", "parent", "--body-file", "/tmp/body.md"])
    test_result(
        "check_mutation_allowed: allowed pr create with title, base, and body-file (stacked)",
        allowed and reason is None,
        f"got allowed={allowed}, reason={reason}"
    )

    allowed, reason = check_mutation_allowed(["pr", "create", "--title", "", "--body-file", "/tmp/body.md"])
    test_result(
        "check_mutation_allowed: rejects pr create with empty title",
        not allowed and reason is not None
    )

    allowed, reason = check_mutation_allowed(["pr", "create", "--title", "Test", "--body-file", ""])
    test_result(
        "check_mutation_allowed: rejects pr create with empty body-file path",
        not allowed and reason is not None
    )

    # pr edit <pr_number> --title <title> --body-file <path>
    allowed, reason = check_mutation_allowed(["pr", "edit", "123", "--title", "Updated PR", "--body-file", "/tmp/body.md"])
    test_result(
        "check_mutation_allowed: allowed pr edit",
        allowed and reason is None,
        f"got allowed={allowed}, reason={reason}"
    )

    allowed, reason = check_mutation_allowed(["pr", "edit", "--title", "Test", "--body-file", "/tmp/body.md"])
    test_result(
        "check_mutation_allowed: rejects pr edit without pr_number",
        not allowed and reason is not None
    )

    # Test that pr edit handles arbitrary pr_number placeholder (doesn't validate numeric)
    allowed, reason = check_mutation_allowed(["pr", "edit", "any_value", "--title", "Test", "--body-file", "/tmp/body.md"])
    test_result(
        "check_mutation_allowed: allows pr edit with arbitrary pr_number",
        allowed and reason is None
    )

    print("[Section 5] Mutation allowlist for worktree add")

    from workflow.mutations import check_mutation_allowed

    allowed, reason = check_mutation_allowed(["worktree", "add", "-b", "feature/123-foo", "--", "/tmp/wt"])
    test_result(
        "check_mutation_allowed: worktree add -b branch -- path",
        allowed and reason is None
    )

    allowed, reason = check_mutation_allowed(["worktree", "add", "-b", "feature/123-foo", "--", "/tmp/wt", "main"])
    test_result(
        "check_mutation_allowed: worktree add -b branch -- path base",
        allowed and reason is None
    )

    allowed, reason = check_mutation_allowed(["worktree", "add", "-b", "feature/123-foo", "/tmp/wt"])
    test_result(
        "check_mutation_allowed: rejects worktree add without -- separator",
        not allowed and reason is not None
    )

    allowed, reason = check_mutation_allowed(["worktree", "add", "-b", "", "--", "/tmp/wt"])
    test_result(
        "check_mutation_allowed: rejects worktree add with empty branch",
        not allowed and reason is not None
    )

    allowed, reason = check_mutation_allowed(["worktree", "add", "-b", "feature/123-foo", "--", ""])
    test_result(
        "check_mutation_allowed: rejects worktree add with empty path",
        not allowed and reason is not None
    )

    print()

    print("[Section 6] Mutation allowlist for issue commands")

    allowed, reason = check_mutation_allowed(["issue", "create", "--title", "New Issue", "--body-file", "/tmp/body.md"])
    test_result(
        "check_mutation_allowed: issue create with title and body-file",
        allowed and reason is None
    )

    allowed, reason = check_mutation_allowed(["issue", "create", "--title", "New Issue", "--label", "bug", "--body-file", "/tmp/body.md"])
    test_result(
        "check_mutation_allowed: issue create with label (before body-file)",
        allowed and reason is None
    )

    allowed, reason = check_mutation_allowed(["issue", "create", "--title", "New Issue", "--assignee", "user", "--body-file", "/tmp/body.md"])
    test_result(
        "check_mutation_allowed: issue create with assignee (before body-file)",
        allowed and reason is None
    )

    allowed, reason = check_mutation_allowed(["issue", "comment", "42", "--body-file", "/tmp/comment.md"])
    test_result(
        "check_mutation_allowed: issue comment",
        allowed and reason is None
    )

    allowed, reason = check_mutation_allowed(["issue", "edit", "42", "--body-file", "/tmp/body.md"])
    test_result(
        "check_mutation_allowed: issue edit",
        allowed and reason is None
    )

    print()

    print()
    h.summarize_and_exit()
