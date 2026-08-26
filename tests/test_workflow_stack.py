#!/usr/bin/env python3
"""
Test suite for stack detection (ADR-0011 logic).

Run with: python3 tests/test_workflow_stack.py
"""

import sys
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.stack import is_stacked, detect_layout
from workflow.models import GitHubCacheData, StackInfo
from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("WORKFLOW STACK TEST SUITE")
    test_result = h.test_result

    print("[Section 1] is_stacked with cache")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        cache_file = tmppath / "github-cache.json"

        stacked_cache = GitHubCacheData(
            branch="feature",
            stack=StackInfo(is_stacked=True, parent_branch="main", parent_pr=42)
        )
        cache_file.write_text(json.dumps(stacked_cache.to_dict()))

        is_stack, parent, pr = is_stacked(cache_path=cache_file)
        test_result(
            "is_stacked() reads cache.stack.is_stacked=True",
            is_stack is True
        )
        test_result(
            "is_stacked() reads cache.stack.parent_branch",
            parent == "main"
        )
        test_result(
            "is_stacked() reads cache.stack.parent_pr",
            pr == 42
        )

        not_stacked_cache = GitHubCacheData(
            branch="feature",
            stack=StackInfo(is_stacked=False)
        )
        cache_file.write_text(json.dumps(not_stacked_cache.to_dict()))

        is_stack, parent, pr = is_stacked(cache_path=cache_file)
        test_result(
            "is_stacked() respects cache.stack.is_stacked=False",
            is_stack is False
        )
        test_result(
            "is_stacked() returns None parent when not stacked",
            parent is None
        )

    print()
    print("[Section 2] is_stacked returns default when no cache")

    with patch("workflow.stack.git.get_current_branch") as mock_branch:
        with patch("workflow.stack.git.get_default_branch") as mock_default:
            with patch("workflow.stack.git.get_worktree_list_porcelain") as mock_worktree:
                mock_branch.return_value = "feature"
                mock_default.return_value = "main"
                mock_worktree.return_value = ""

                is_stack, parent, pr = is_stacked(cache_path=Path("/nonexistent"))
                test_result(
                    "is_stacked() returns False when no cache",
                    is_stack is False
                )

    print()
    print("[Section 3] detect_layout returns unknown for empty state")

    with patch("workflow.stack.git.get_current_branch") as mock_branch:
        with patch("workflow.stack.git.get_default_branch") as mock_default:
            with patch("workflow.stack.git.get_worktree_list_porcelain") as mock_worktree:
                with patch("workflow.stack.is_stacked") as mock_is_stacked:
                    mock_branch.return_value = "feature"
                    mock_default.return_value = "main"
                    mock_worktree.return_value = ""
                    mock_is_stacked.return_value = (False, None, None)

                    layout = detect_layout()
                    test_result(
                        "detect_layout() returns unknown when no worktrees",
                        layout == "unknown"
                    )

    print()
    print("[Section 4] detect_layout single-driver detection")

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
                                "detect_layout() returns single-driver when stacked and no sibling",
                                layout == "single-driver"
                            )

    print()
    h.summarize_and_exit()
