#!/usr/bin/env python3
"""
Test suite for worktrees detection (ADR-0010 logic).

Run with: python3 tests/test_workflow_worktrees.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.worktrees import parse_worktree_list, detect_graft_config_path
from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("WORKFLOW WORKTREES TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Worktree list parsing")

    porcelain_output = """\
worktree /path/to/main
branch refs/heads/main
worktree /path/to/worktrees/feature-1
branch refs/heads/feature-1
worktree /path/to/worktrees/feature-2
branch refs/heads/feature-2
"""

    worktrees = parse_worktree_list(porcelain_output)
    test_result(
        "parse_worktree_list() extracts all worktrees",
        len(worktrees) == 3
    )
    test_result(
        "parse_worktree_list() first worktree is main",
        worktrees[0] == ("/path/to/main", "main")
    )
    test_result(
        "parse_worktree_list() second worktree is feature-1",
        worktrees[1] == ("/path/to/worktrees/feature-1", "feature-1")
    )
    test_result(
        "parse_worktree_list() third worktree is feature-2",
        worktrees[2] == ("/path/to/worktrees/feature-2", "feature-2")
    )

    print()
    print("[Section 2] Detached HEAD worktrees (skipped)")

    porcelain_detached = """\
worktree /path/to/main
branch refs/heads/main
worktree /path/to/detached
worktree /path/to/feature
branch refs/heads/feature
"""

    worktrees_detached = parse_worktree_list(porcelain_detached)
    test_result(
        "parse_worktree_list() skips detached HEAD entries",
        len(worktrees_detached) == 2
    )
    test_result(
        "parse_worktree_list() detached worktree not in result",
        "/path/to/detached" not in [wt[0] for wt in worktrees_detached]
    )

    print()
    print("[Section 3] Graft config path")

    graft_path = detect_graft_config_path()
    test_result(
        "detect_graft_config_path() ends with graft/config.json",
        str(graft_path).endswith("graft/config.json")
    )
    test_result(
        "detect_graft_config_path() is under .config",
        ".config" in str(graft_path)
    )

    print()
    h.summarize_and_exit()
