#!/usr/bin/env python3
"""
Test suite for worktrees detection (ADR-0010).

Covers: MAIN_WORKTREE, WORKTREE_PARENT detection, PROJECT_ROOT detection.

Run with: python3 tests/test_workflow_worktrees_detection.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import workflow.worktrees as worktrees_module
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
            "parse_worktree_list extracts both branched entries",
            len(result) == 2
        )
        test_result(
            "parse_worktree_list preserves worktree path and branch pairs",
            result == [("/path/to/main", "main"), ("/path/to/feature", "feature")]
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
            result == [("/path/to/main", "main"), ("/path/to/feature", "feature")]
        )

    print()
    print("[Section 5] detect_worktree_parent guards against a poisoned second worktree")

    if hasattr(worktrees_module, "detect_worktree_parent"):
        import workflow.git as git_module

        original_get_porcelain = git_module.get_worktree_list_porcelain

        def _fake_porcelain(porcelain_text):
            def _impl(cwd=None):
                return porcelain_text
            return _impl

        # Poisoned: second worktree nested under a type-prefixed folder.
        poisoned_porcelain = """\
worktree /repo/main
branch refs/heads/main
worktree /repo/worktrees/feature/166-foo
branch refs/heads/feature/166-foo
"""
        git_module.get_worktree_list_porcelain = _fake_porcelain(poisoned_porcelain)
        try:
            result = worktrees_module.detect_worktree_parent()
        finally:
            git_module.get_worktree_list_porcelain = original_get_porcelain
        test_result(
            "detect_worktree_parent falls back to '<main>/worktrees' when second worktree is nested under a type prefix",
            result == "/repo/main/worktrees"
        )

        # Legacy flat layout: second worktree's parent basename is not a type prefix — trust it as-is.
        legacy_flat_porcelain = """\
worktree /repo/main
branch refs/heads/main
worktree /repo/siblings/42-bar
branch refs/heads/feature/42-bar
"""
        git_module.get_worktree_list_porcelain = _fake_porcelain(legacy_flat_porcelain)
        try:
            result = worktrees_module.detect_worktree_parent()
        finally:
            git_module.get_worktree_list_porcelain = original_get_porcelain
        test_result(
            "detect_worktree_parent still trusts a legacy flat (non-poisoned) second-worktree parent",
            result == "/repo/siblings"
        )

        # Main worktree already under worktrees/, no second worktree: step 2.
        single_under_worktrees_porcelain = """\
worktree /repo/worktrees/main
branch refs/heads/main
"""
        git_module.get_worktree_list_porcelain = _fake_porcelain(single_under_worktrees_porcelain)
        try:
            result = worktrees_module.detect_worktree_parent()
        finally:
            git_module.get_worktree_list_porcelain = original_get_porcelain
        test_result(
            "detect_worktree_parent returns existing worktrees/ dir when main already lives there (step 2)",
            result == "/repo/worktrees"
        )

        # Single worktree, not under worktrees/: step 3, create worktrees/ as sibling.
        single_bare_porcelain = """\
worktree /repo/main
branch refs/heads/main
"""
        git_module.get_worktree_list_porcelain = _fake_porcelain(single_bare_porcelain)
        try:
            result = worktrees_module.detect_worktree_parent()
        finally:
            git_module.get_worktree_list_porcelain = original_get_porcelain
        test_result(
            "detect_worktree_parent falls back to '<main>/worktrees' for a bare top-level clone (rule 3)",
            result == "/repo/main/worktrees"
        )

    print()
    print("[Section 6] detect_worktree_parent: canonical layout, ~/.claude skip, nested skip, fail-closed")

    import os
    import workflow.git as git_module6

    original6 = git_module6.get_worktree_list_porcelain
    home = os.environ.get("HOME") or os.path.expanduser("~")

    def _detect(porcelain_text):
        git_module6.get_worktree_list_porcelain = lambda cwd=None: porcelain_text
        try:
            return worktrees_module.detect_worktree_parent()
        finally:
            git_module6.get_worktree_list_porcelain = original6

    scratch = f"{home}/.claude/merge-queue-scratch/abc123/scratch"

    # Canonical layout wins even with a scratch entry listed second.
    r = _detect(f"""\
worktree /repo/worktrees/main
branch refs/heads/main

worktree {scratch}
HEAD 0000
detached

worktree /elsewhere/other/42-x
branch refs/heads/feature/42-x
""")
    test_result("canonical layout wins even when a scratch entry is second", r == "/repo/worktrees")

    # Non-canonical main: ~/.claude scratch second entry is skipped, next candidate used.
    r = _detect(f"""\
worktree /repo/main
branch refs/heads/main

worktree {scratch}
HEAD 0000
detached

worktree /repo/siblings/42-x
branch refs/heads/feature/42-x
""")
    test_result("~/.claude/merge-queue-scratch second entry is skipped", r == "/repo/siblings")

    # .claude/worktrees under HOME is skipped too; only scratch present -> fallback.
    r = _detect(f"""\
worktree /repo/main
branch refs/heads/main

worktree {home}/.claude/worktrees/agent-1
branch refs/heads/agent-1
""")
    test_result("only ~/.claude entries -> falls back to <main>/worktrees", r == "/repo/main/worktrees")

    # Nested-under-main entry is skipped.
    r = _detect("""\
worktree /repo/main
branch refs/heads/main

worktree /repo/main/worktrees/1638-x
branch refs/heads/feature/1638-x

worktree /repo/siblings/7-y
branch refs/heads/fix/7-y
""")
    test_result("entry nested under main is skipped", r == "/repo/siblings")

    # Type-prefixed parent skipped.
    r = _detect("""\
worktree /repo/main
branch refs/heads/main

worktree /repo/other/feature/9-z
branch refs/heads/feature/9-z

worktree /repo/siblings/7-y
branch refs/heads/fix/7-y
""")
    test_result("type-prefixed parent entry is skipped", r == "/repo/siblings")

    # Plain-clone fallback.
    r = _detect("""\
worktree /repo
branch refs/heads/main
""")
    test_result("plain top-level clone falls back to <main>/worktrees", r == "/repo/worktrees")

    # Fail closed: main under ~/.claude -> fallback lands under ~/.claude.
    raised = False
    try:
        _detect(f"""\
worktree {home}/.claude/thing
branch refs/heads/main
""")
    except worktrees_module.WorktreeParentError:
        raised = True
    test_result("fail-closed: parent under ~/.claude raises WorktreeParentError", raised)

    print()
    h.summarize_and_exit()
