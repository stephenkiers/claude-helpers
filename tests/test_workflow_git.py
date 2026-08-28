#!/usr/bin/env python3
"""
Test suite for git subprocess wrapper (git.py).

Covers: basic git module functionality and error handling via Unknown type.

Run with: python3 tests/test_workflow_git.py
"""

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
        mock_run.return_value = "{}"
        git_module.pr_view_json("some-branch", ["headRefName", "state"])
        called_args, _ = mock_run.call_args
        test_result(
            "pr_view_json comma-joins multi-field --json",
            called_args[0] == ["pr", "view", "--json", "headRefName,state", "--", "some-branch"],
            f"got {called_args[0]}"
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
    h.summarize_and_exit()
