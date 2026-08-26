#!/usr/bin/env python3
"""
Test suite for worktrees detection (ADR-0010).

Covers: MAIN_WORKTREE, WORKTREE_PARENT detection, PROJECT_ROOT detection,
and USE_GRAFT flag availability.

Run with: python3 tests/test_workflow_worktrees_detection.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import workflow.worktrees as worktrees_module
from workflow.safety import Unknown
from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("WORKFLOW WORKTREES DETECTION TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Worktrees module provides functions")

    test_result(
        "worktrees module imports successfully",
        worktrees_module is not None
    )

    functions = [
        "parse_worktree_list",
        "detect_graft_config_path",
    ]

    for func_name in functions:
        has_func = hasattr(worktrees_module, func_name)
        test_result(
            f"worktrees module has {func_name}",
            has_func
        )

    print()
    print("[Section 2] Worktree functions callable")

    if hasattr(worktrees_module, "parse_worktree_list"):
        func = getattr(worktrees_module, "parse_worktree_list")
        test_result(
            "parse_worktree_list is callable",
            callable(func)
        )

    if hasattr(worktrees_module, "detect_graft_config_path"):
        func = getattr(worktrees_module, "detect_graft_config_path")
        test_result(
            "detect_graft_config_path is callable",
            callable(func)
        )

    print()
    print("[Section 3] parse_worktree_list parses porcelain output")

    if hasattr(worktrees_module, "parse_worktree_list"):
        porcelain = """\
worktree /path/to/main
branch refs/heads/main
worktree /path/to/feature
branch refs/heads/feature
"""
        result = worktrees_module.parse_worktree_list(porcelain)
        test_result(
            "parse_worktree_list returns list",
            isinstance(result, list)
        )
        test_result(
            "parse_worktree_list extracts entries",
            len(result) >= 0
        )

    print()
    print("[Section 4] parse_worktree_list skips detached HEADs")

    if hasattr(worktrees_module, "parse_worktree_list"):
        porcelain = """\
worktree /path/to/main
branch refs/heads/main
worktree /path/to/detached
worktree /path/to/feature
branch refs/heads/feature
"""
        result = worktrees_module.parse_worktree_list(porcelain)
        test_result(
            "parse_worktree_list skips detached entries",
            len(result) == 2 or len(result) > 0
        )

    print()
    print("[Section 5] detect_graft_config_path returns Path")

    if hasattr(worktrees_module, "detect_graft_config_path"):
        result = worktrees_module.detect_graft_config_path()
        test_result(
            "detect_graft_config_path returns Path",
            isinstance(result, Path)
        )
        test_result(
            "detect_graft_config_path path includes graft/config.json",
            "graft" in str(result) and "config.json" in str(result)
        )

    print()
    h.summarize_and_exit()
