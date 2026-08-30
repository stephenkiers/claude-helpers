#!/usr/bin/env python3
"""
Test suite for git subprocess wrapper (git.py).

Covers: basic git module functionality and error handling via Unknown type.

Run with: python3 tests/test_workflow_git.py
"""

import subprocess
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import workflow.git as git_module
from workflow.safety import Unknown
from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("WORKFLOW GIT TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Git module provides core functions")

    test_result(
        "git module imports successfully",
        git_module is not None
    )

    functions = [
        "get_current_branch",
        "get_default_branch",
        "get_worktree_list_porcelain"
    ]

    for func_name in functions:
        has_func = hasattr(git_module, func_name)
        test_result(
            f"git module has {func_name}",
            has_func
        )

    print()
    print("[Section 2] Git functions callable")

    if hasattr(git_module, "get_current_branch"):
        func = getattr(git_module, "get_current_branch")
        test_result(
            "get_current_branch is callable",
            callable(func)
        )

    if hasattr(git_module, "get_default_branch"):
        func = getattr(git_module, "get_default_branch")
        test_result(
            "get_default_branch is callable",
            callable(func)
        )

    if hasattr(git_module, "get_worktree_list_porcelain"):
        func = getattr(git_module, "get_worktree_list_porcelain")
        test_result(
            "get_worktree_list_porcelain is callable",
            callable(func)
        )

    print()
    print("[Section 3] Unknown type available for error handling")

    unk = Unknown("test error")
    test_result(
        "Unknown type can be instantiated",
        isinstance(unk, Unknown)
    )
    test_result(
        "Unknown has reason",
        hasattr(unk, "reason") and unk.reason == "test error"
    )

    print()
    print("[Section 4] --json argv is comma-joined, not list-spliced")

    with mock.patch("workflow.git.run_gh_command") as mock_run:
        mock_run.return_value = "{}"
        git_module.repo_view_json(["name", "owner"])
        called_args, _ = mock_run.call_args
        test_result(
            "repo_view_json comma-joins multi-field --json",
            called_args[0] == ["repo", "view", "--json", "name,owner"],
            f"got {called_args[0]}"
        )

    with mock.patch("workflow.git.run_gh_command") as mock_run:
        mock_run.return_value = '{"headRefName": "feature/x", "state": "OPEN"}'
        result = git_module.pr_view_json("some-branch", ["headRefName", "state"])
        called_args, _ = mock_run.call_args
        test_result(
            "pr_view_json comma-joins multi-field --json",
            called_args[0] == ["pr", "view", "--json", "headRefName,state", "--", "some-branch"],
            f"got {called_args[0]}"
        )
        test_result(
            "pr_view_json returns parsed JSON on success",
            result == {"headRefName": "feature/x", "state": "OPEN"}
        )

    with mock.patch("workflow.git.run_gh_command") as mock_run:
        mock_run.return_value = "[]"
        git_module.pr_list_json("main", ["number", "title"])
        called_args, _ = mock_run.call_args
        test_result(
            "pr_list_json comma-joins multi-field --json",
            called_args[0] == ["pr", "list", "--base", "main", "--state", "open", "--json", "number,title"],
            f"got {called_args[0]}"
        )

    print()
    print("[Section: GitCommandError carries stderr]")

    # Regression: subprocess.CalledProcessError.__str__ reports only the exit status.
    # Wrapping it as f"...: {e}" therefore dropped git's own diagnostic, which silently
    # disabled every caller that branches on *why* a command failed (notably
    # cleanup.py's dirty-tree force-retry, which grepped for "modified or untracked").
    err = git_module.GitCommandError(
        128, ["git", "worktree", "remove", "--", "/x"], output="", stderr="fatal: contains modified or untracked files, use --force to delete it\n"
    )
    h.test_result(
        "GitCommandError.__str__ includes stderr",
        "modified or untracked" in str(err),
        f"got {str(err)!r}"
    )
    h.test_result(
        "GitCommandError.__str__ still includes the exit status",
        "exit status 128" in str(err),
        f"got {str(err)!r}"
    )
    h.test_result(
        "GitCommandError is a CalledProcessError (existing handlers keep catching it)",
        issubclass(git_module.GitCommandError, subprocess.CalledProcessError)
    )

    empty = git_module.GitCommandError(1, ["git", "x"], output="", stderr="")
    h.test_result(
        "GitCommandError with no stderr does not append a trailing separator",
        not str(empty).endswith(": "),
        f"got {str(empty)!r}"
    )

    # End-to-end through the real subprocess path: the reason surfaced to a caller must
    # contain git's message, not just the exit code. This is the assertion that would
    # have caught the original bug.
    ok, unknown = git_module.remove_worktree(Path("/nonexistent-worktree-xyz"))
    h.test_result(
        "remove_worktree surfaces git's stderr in the Unknown reason",
        ok is False and unknown is not None and "not a working tree" in unknown.reason,
        f"got {unknown.reason if unknown else None!r}"
    )

    print()
    print("[Section: is_linked_worktree with real git operations]")

    # Import GitFixture for real git testing
    import os as os_module
    from _git_fixture import GitFixture

    # Test 1: is_linked_worktree returns False for main worktree
    fixture = GitFixture()
    old_cwd = os_module.getcwd()
    try:
        os_module.chdir(fixture.repo_root)

        fixture.create_initial_commit("initial")
        fixture.create_branch("feature/test")

        # Test main worktree returns False
        is_linked = git_module.is_linked_worktree(fixture.main_worktree)
        test_result(
            "is_linked_worktree returns False for main worktree",
            is_linked is False,
            f"got {is_linked}"
        )

        # Test 2: is_linked_worktree returns True for linked worktree
        wt_path = fixture.create_worktree("feature/test")
        is_linked = git_module.is_linked_worktree(wt_path)
        test_result(
            "is_linked_worktree returns True for linked worktree",
            is_linked is True,
            f"got {is_linked}"
        )

        # Test 3: is_linked_worktree with explicit Path object
        is_linked = git_module.is_linked_worktree(Path(wt_path))
        test_result(
            "is_linked_worktree works with Path objects",
            is_linked is True,
            f"got {is_linked}"
        )

        # Test 4: is_linked_worktree with None defaults to cwd
        old_cwd_inner = os_module.getcwd()
        try:
            os_module.chdir(wt_path)
            is_linked = git_module.is_linked_worktree(None)
            test_result(
                "is_linked_worktree(None) uses current working directory",
                is_linked is True,
                f"got {is_linked}"
            )
        finally:
            os_module.chdir(old_cwd_inner)

        # Test 5: is_linked_worktree with main worktree as cwd when None
        old_cwd_inner = os_module.getcwd()
        try:
            os_module.chdir(fixture.main_worktree)
            is_linked = git_module.is_linked_worktree(None)
            test_result(
                "is_linked_worktree(None) from main worktree returns False",
                is_linked is False,
                f"got {is_linked}"
            )
        finally:
            os_module.chdir(old_cwd_inner)

    finally:
        os_module.chdir(old_cwd)
        fixture.cleanup()

    print()
    h.summarize_and_exit()
