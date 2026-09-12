#!/usr/bin/env python3
"""
Test suite for worktrees detection (ADR-0010 logic).

Run with: python3 tests/test_workflow_worktrees.py
"""

import sys
import re
import ast
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.worktrees import parse_worktree_list, _TYPE_PREFIXES
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
    print("[Section 3] Type prefix synchronization across codebase")

    # Extract _TYPE_PREFIXES from worktrees.py
    worktrees_type_prefixes = _TYPE_PREFIXES

    # Extract type keywords from track.py's infer_type function by parsing the source
    track_file = Path(__file__).parent.parent / "scripts" / "workflow" / "track.py"
    track_types = set()
    if track_file.exists():
        try:
            track_source = track_file.read_text()
            tree = ast.parse(track_source)
            # Find the infer_type function and extract the keywords dict
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "infer_type":
                    # Look for the keywords dict assignment within the function
                    for child in ast.walk(node):
                        if isinstance(child, ast.Dict):
                            # Extract all string keys from the dict
                            for key in child.keys:
                                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                                    track_types.add(key.value)
                            break
        except Exception:
            pass

    # Extract type list from prompts/worktree-reference.md line 26
    # Find the line with: printf '%s\n' "feature fix chore" | grep -qw "$SECOND_PARENT_BASENAME"
    prompt_file = Path(__file__).parent.parent / "prompts" / "worktree-reference.md"
    prompt_types = set()
    if prompt_file.exists():
        prompt_content = prompt_file.read_text()
        # Find the line with the printf statement containing type prefixes
        match = re.search(r'printf\s+\'%s\\n\'\s+"([^"]+)"', prompt_content)
        if match:
            types_str = match.group(1)
            prompt_types = set(types_str.split())

    # All three sets should be equal
    test_result(
        "type prefixes: worktrees._TYPE_PREFIXES contains expected types",
        worktrees_type_prefixes == {"feature", "fix", "chore"}
    )

    test_result(
        "type prefixes: track.py infer_type keywords match worktrees._TYPE_PREFIXES",
        track_types == worktrees_type_prefixes
    )

    test_result(
        "type prefixes: prompts/worktree-reference.md types match worktrees._TYPE_PREFIXES",
        prompt_types == worktrees_type_prefixes
    )

    test_result(
        "type prefixes: all three sources are synchronized",
        worktrees_type_prefixes == track_types == prompt_types
    )

    print()
    h.summarize_and_exit()
