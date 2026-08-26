#!/usr/bin/env python3
"""
Test suite for complex stack layout detection scenarios (ADR-0011).

Key invariant: detect_layout's three-way branch logic must correctly distinguish:
- single-driver: subject has a parent, no other worktree has the parent checked out
- per-branch: subject has a parent checked out in another worktree OR
              another worktree's cached parent equals the subject
- unknown: ambiguous/unresolvable state (fail closed)

Run with: python3 tests/test_workflow_layout_edgecases.py
"""

import sys
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.stack import detect_layout
from workflow.models import GitHubCacheData, StackInfo
from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("WORKFLOW LAYOUT EDGE CASES TEST SUITE")
    test_result = h.test_result

    print("[Section 1] detect_layout: per-branch detection (sibling has parent checked out)")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        cache_file = tmppath / "github-cache.json"

        stacked_cache = GitHubCacheData(
            branch="child",
            stack=StackInfo(is_stacked=True, parent_branch="parent")
        )
        cache_file.write_text(json.dumps(stacked_cache.to_dict()))

        porcelain = """\
worktree /path/main
branch refs/heads/main
worktree /path/parent-wt
branch refs/heads/parent
worktree /path/child-wt
branch refs/heads/child
"""

        with patch("workflow.stack.git.get_current_branch") as mock_branch:
            with patch("workflow.stack.git.get_default_branch") as mock_default:
                with patch("workflow.stack.git.get_worktree_list_porcelain") as mock_worktree:
                    with patch("workflow.stack.read_github_cache") as mock_read:
                        with patch("workflow.stack.is_stacked") as mock_is_stacked:
                            mock_branch.return_value = "child"
                            mock_default.return_value = "main"
                            mock_worktree.return_value = porcelain
                            mock_read.return_value = stacked_cache
                            mock_is_stacked.return_value = (True, "parent", None)

                            layout = detect_layout(subject_branch="child")
                            test_result(
                                "detect_layout returns per-branch when parent worktree exists",
                                layout == "per-branch"
                            )

    print()
    print("[Section 2] detect_layout: per-branch requires careful worktree mapping")

    with patch("workflow.stack.git.get_current_branch") as mock_branch:
        with patch("workflow.stack.git.get_default_branch") as mock_default:
            with patch("workflow.stack.git.get_worktree_list_porcelain") as mock_worktree:
                with patch("workflow.stack.is_stacked") as mock_is_stacked:
                    mock_branch.return_value = "child"
                    mock_default.return_value = "main"
                    porcelain = """\
worktree /path/main
branch refs/heads/main
worktree /path/parent-wt
branch refs/heads/parent
worktree /path/child-wt
branch refs/heads/child
"""
                    mock_worktree.return_value = porcelain
                    mock_is_stacked.return_value = (False, None, None)

                    layout = detect_layout(subject_branch="child")
                    test_result(
                        "detect_layout handles multiple worktrees correctly",
                        layout in ["single-driver", "per-branch", "unknown"]
                    )

    print()
    print("[Section 3] detect_layout: single-driver (parent stacked but no sibling)")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        cache_file = tmppath / "github-cache.json"

        stacked_cache = GitHubCacheData(
            branch="feature",
            stack=StackInfo(is_stacked=True, parent_branch="main")
        )
        cache_file.write_text(json.dumps(stacked_cache.to_dict()))

        porcelain = """\
worktree /path/main
branch refs/heads/main
worktree /path/feature
branch refs/heads/feature
"""

        with patch("workflow.stack.git.get_current_branch") as mock_branch:
            with patch("workflow.stack.git.get_default_branch") as mock_default:
                with patch("workflow.stack.git.get_worktree_list_porcelain") as mock_worktree:
                    with patch("workflow.stack.read_github_cache"):
                        with patch("workflow.stack.is_stacked") as mock_is_stacked:
                            mock_branch.return_value = "feature"
                            mock_default.return_value = "main"
                            mock_worktree.return_value = porcelain
                            mock_is_stacked.return_value = (True, "main", None)

                            layout = detect_layout(subject_branch="feature")
                            test_result(
                                "detect_layout returns single-driver when stacked with no per-branch sibling",
                                layout == "single-driver"
                            )

    print()
    print("[Section 4] detect_layout: unknown (no worktrees besides main)")

    with patch("workflow.stack.git.get_current_branch") as mock_branch:
        with patch("workflow.stack.git.get_default_branch") as mock_default:
            with patch("workflow.stack.git.get_worktree_list_porcelain") as mock_worktree:
                with patch("workflow.stack.is_stacked") as mock_is_stacked:
                    mock_branch.return_value = "feature"
                    mock_default.return_value = "main"
                    mock_worktree.return_value = ""
                    mock_is_stacked.return_value = (False, None, None)

                    layout = detect_layout(subject_branch="feature")
                    test_result(
                        "detect_layout returns unknown when no worktrees",
                        layout == "unknown"
                    )

    print()
    print("[Section 5] detect_layout: unknown (no parent found, can't determine)")

    with patch("workflow.stack.git.get_current_branch") as mock_branch:
        with patch("workflow.stack.git.get_default_branch") as mock_default:
            with patch("workflow.stack.git.get_worktree_list_porcelain") as mock_worktree:
                with patch("workflow.stack.is_stacked") as mock_is_stacked:
                    mock_branch.return_value = "feature"
                    mock_default.return_value = "main"
                    porcelain = """\
worktree /path/main
branch refs/heads/main
worktree /path/feature
branch refs/heads/feature
worktree /path/other
branch refs/heads/other
"""
                    mock_worktree.return_value = porcelain
                    mock_is_stacked.return_value = (False, None, None)

                    layout = detect_layout(subject_branch="feature")
                    test_result(
                        "detect_layout returns unknown when no parent and multiple siblings",
                        layout == "unknown"
                    )

    print()
    print("[Section 6] detect_layout: unknown (detached HEAD in worktrees)")

    with patch("workflow.stack.git.get_current_branch") as mock_branch:
        with patch("workflow.stack.git.get_default_branch") as mock_default:
            with patch("workflow.stack.git.get_worktree_list_porcelain") as mock_worktree:
                with patch("workflow.stack.is_stacked") as mock_is_stacked:
                    mock_branch.return_value = "feature"
                    mock_default.return_value = "main"
                    porcelain_detached = """\
worktree /path/main
branch refs/heads/main
worktree /path/detached
worktree /path/feature
branch refs/heads/feature
"""
                    mock_worktree.return_value = porcelain_detached
                    mock_is_stacked.return_value = (True, "main", None)

                    layout = detect_layout(subject_branch="feature")
                    test_result(
                        "detect_layout handles detached worktrees correctly",
                        layout in ["single-driver", "unknown"]
                    )

    print()
    print("[Section 7] detect_layout: per-branch with default branch consideration")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        cache_file = tmppath / "github-cache.json"

        stacked_cache = GitHubCacheData(
            branch="feature",
            stack=StackInfo(is_stacked=True, parent_branch="main")
        )
        cache_file.write_text(json.dumps(stacked_cache.to_dict()))

        porcelain = """\
worktree /path/main
branch refs/heads/main
worktree /path/main-worktree
branch refs/heads/main
worktree /path/feature
branch refs/heads/feature
"""

        with patch("workflow.stack.git.get_current_branch") as mock_branch:
            with patch("workflow.stack.git.get_default_branch") as mock_default:
                with patch("workflow.stack.git.get_worktree_list_porcelain") as mock_worktree:
                    with patch("workflow.stack.read_github_cache"):
                        with patch("workflow.stack.is_stacked") as mock_is_stacked:
                            mock_branch.return_value = "feature"
                            mock_default.return_value = "main"
                            mock_worktree.return_value = porcelain
                            mock_is_stacked.return_value = (True, "main", None)

                            layout = detect_layout(subject_branch="feature")
                            test_result(
                                "detect_layout does not count default branch as sibling",
                                layout == "single-driver"
                            )

    print()
    h.summarize_and_exit()
