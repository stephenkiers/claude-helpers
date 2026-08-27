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

    allowed, reason = check_mutation_allowed(["worktree", "remove", "/tmp/wt"])
    test_result(
        "check_mutation_allowed: allowed worktree remove",
        allowed and reason is None
    )

    allowed, reason = check_mutation_allowed(["worktree", "remove", "--force", "/tmp/wt"])
    test_result(
        "check_mutation_allowed: allowed worktree remove --force",
        allowed and reason is None
    )

    allowed, reason = check_mutation_allowed(["branch", "-d", "feature"])
    test_result(
        "check_mutation_allowed: allowed branch -d",
        allowed and reason is None
    )

    allowed, reason = check_mutation_allowed(["branch", "-D", "feature"])
    test_result(
        "check_mutation_allowed: allowed branch -D",
        allowed and reason is None
    )

    allowed, reason = check_mutation_allowed(["pull", "--ff-only", "origin", "main"])
    test_result(
        "check_mutation_allowed: allowed pull --ff-only",
        allowed and reason is None
    )

    allowed, reason = check_mutation_allowed(["worktree", "remove", "--force", "--verbose", "/tmp/wt"])
    test_result(
        "check_mutation_allowed: rejects extra flag",
        not allowed and reason is not None
    )

    allowed, reason = check_mutation_allowed(["push", "origin", "main"])
    test_result(
        "check_mutation_allowed: rejects unlisted subcommand",
        not allowed and "not in mutation allowlist" in reason
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
    h.summarize_and_exit()
