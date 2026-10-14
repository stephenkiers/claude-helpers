#!/usr/bin/env python3
"""
Test suite for worktree-parent containment, the swallow contract of
_all_worktree_paths, and removal of the orphaned detect_second_worktree.

Covers:
- realpath-based ~/.claude containment (symlinked main worktree and sibling entries)
- WorktreeParentError as the fail-closed signal from detect_worktree_parent
- _all_worktree_paths documents and implements its swallow-everything contract
- detect_second_worktree is gone from the workflow package

Run with: python3 tests/test_workflow_worktrees_containment.py
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import workflow.worktrees as worktrees_module
import workflow.git as git_module
from _test_harness import Harness


def _porcelain(*entries):
    """Build a `git worktree list --porcelain` blob from (path, branch) pairs."""
    chunks = []
    for path, branch in entries:
        chunks.append(f"worktree {path}\nbranch refs/heads/{branch}\n")
    return "\n".join(chunks)


def _detect_with(porcelain_text, cwd=None):
    """Run detect_worktree_parent against canned porcelain output."""
    with mock.patch.object(git_module, "get_worktree_list_porcelain", lambda cwd=None: porcelain_text):
        return worktrees_module.detect_worktree_parent(cwd)


if __name__ == "__main__":
    h = Harness("WORKTREES CONTAINMENT TEST SUITE")
    test_result = h.test_result

    original_home = os.environ.get("HOME")
    tmp_root = Path(tempfile.mkdtemp())
    try:
        # Fake HOME with a real ~/.claude tree the containment check must protect.
        home = tmp_root / "home"
        unsafe_main = home / ".claude" / "thing"
        unsafe_main.mkdir(parents=True)
        os.environ["HOME"] = str(home)

        proj = tmp_root / "proj"
        (proj / "worktrees" / "main").mkdir(parents=True)
        (proj / "siblings" / "7-y").mkdir(parents=True)

        print("[Section 1] Canonical layout and fail-closed signal")

        canonical = _detect_with(_porcelain(
            (str(proj / "worktrees" / "main"), "main"),
        ))
        test_result(
            "canonical layout: parent named 'worktrees' is returned",
            canonical == str(proj / "worktrees"),
        )

        raised = False
        try:
            _detect_with(_porcelain((str(unsafe_main), "main")))
        except worktrees_module.WorktreeParentError:
            raised = True
        test_result(
            "main worktree under ~/.claude raises WorktreeParentError (not a silent fallback)",
            raised,
        )
        test_result(
            "WorktreeParentError is a RuntimeError subclass (callers can catch it by base type)",
            issubclass(worktrees_module.WorktreeParentError, RuntimeError),
        )

        print()
        print("[Section 2] realpath containment: symlinked main worktree")

        # Lexically outside ~/.claude, but resolves into it. Only a realpath check catches this.
        main_link = tmp_root / "main-link"
        main_link.symlink_to(unsafe_main)
        raised = False
        try:
            _detect_with(_porcelain((str(main_link), "main")))
        except worktrees_module.WorktreeParentError:
            raised = True
        test_result(
            "main worktree symlinked into ~/.claude raises WorktreeParentError",
            raised,
        )

        print()
        print("[Section 3] realpath containment: symlinked sibling candidate")

        # Sibling candidate whose real location is under ~/.claude must be skipped.
        # Its lexical parent (tmp_root) is outside ~/.claude, so only realpath rejects it.
        sibling_link = tmp_root / "sibling-link"
        sibling_link.symlink_to(unsafe_main)
        # Main must be non-canonical here, otherwise step 1 (canonical 'worktrees' parent)
        # short-circuits and the sibling heuristic is never reached.
        result = _detect_with(_porcelain(
            (str(proj / "main"), "main"),
            (str(sibling_link), "feat/1-x"),
            (str(proj / "siblings" / "7-y"), "fix/7-y"),
        ))
        test_result(
            "symlinked sibling resolving into ~/.claude is skipped; next usable sibling wins",
            result == str(proj / "siblings"),
        )

        print()
        print("[Section 4] Real ~/.claude entries stay excluded (control)")

        result = _detect_with(_porcelain(
            (str(proj / "main"), "main"),
            (str(unsafe_main), "scratch"),
            (str(proj / "siblings" / "7-y"), "fix/7-y"),
        ))
        test_result(
            "non-symlinked ~/.claude sibling is skipped, next usable sibling wins",
            result == str(proj / "siblings"),
        )

        print()
        print("[Section 5] _all_worktree_paths swallow contract")

        with mock.patch.object(git_module, "get_worktree_list_porcelain",
                               side_effect=RuntimeError("git exploded")):
            try:
                paths = worktrees_module._all_worktree_paths(tmp_root)
                no_raise = True
            except Exception:
                paths = None
                no_raise = False
        test_result(
            "_all_worktree_paths does not raise when git listing fails",
            no_raise,
        )
        test_result(
            "_all_worktree_paths returns an empty list when git listing fails",
            paths == [],
        )

        print()
        print("[Section 6] detect_second_worktree is removed")

        test_result(
            "workflow.worktrees no longer exposes detect_second_worktree",
            not hasattr(worktrees_module, "detect_second_worktree"),
        )

        scripts_dir = Path(__file__).parent.parent / "scripts"
        needle = "detect_second" + "_worktree"
        offenders = []
        for py in scripts_dir.rglob("*.py"):
            if needle in py.read_text(encoding="utf-8", errors="replace"):
                offenders.append(str(py.relative_to(scripts_dir)))
        test_result(
            "no module under scripts/ still references detect_second_worktree",
            offenders == [],
            f"offenders: {offenders}",
        )
    finally:
        if original_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = original_home

    print()
    h.summarize_and_exit()
