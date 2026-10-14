#!/usr/bin/env python3
"""
Test suite for how resolve_queue_path reports a WorktreeParentError.

resolve_queue_path returns None only for "cannot resolve the repo". A
fail-closed worktree-parent refusal is a different condition and must
propagate to the caller as WorktreeParentError, not be turned into None.

Run with: python3 tests/test_verify_queue_worktree_parent.py
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import workflow.worktrees as worktrees_module
import workflow.git as git_module
import workflow.verify_queue as verify_queue
from _test_harness import Harness


def _porcelain(*entries):
    """Build a `git worktree list --porcelain` blob from (path, branch) pairs."""
    return "\n".join(f"worktree {p}\nbranch refs/heads/{b}\n" for p, b in entries)


if __name__ == "__main__":
    h = Harness("VERIFY QUEUE WORKTREE-PARENT TEST SUITE")
    test_result = h.test_result

    original_home = os.environ.get("HOME")
    tmp_root = Path(tempfile.mkdtemp())
    try:
        home = tmp_root / "home"
        unsafe_main = home / ".claude" / "thing"
        unsafe_main.mkdir(parents=True)
        os.environ["HOME"] = str(home)

        proj = tmp_root / "proj"
        canonical_main = proj / "worktrees" / "main"
        cwd = proj / "worktrees" / "feat-1-x"
        canonical_main.mkdir(parents=True)
        cwd.mkdir(parents=True)

        print("[Section 1] Happy path: canonical layout resolves the queue file")

        with mock.patch.object(git_module, "get_worktree_list_porcelain",
                               lambda cwd=None: _porcelain((canonical_main, "main"), (cwd, "feat/1-x"))):
            path = verify_queue.resolve_queue_path(cwd)
        test_result(
            "canonical layout resolves to a Path",
            isinstance(path, Path),
            f"got {path!r}",
        )
        test_result(
            "queue path is verify-queue.jsonl inside the canonical worktree parent",
            path is not None and path == proj / "worktrees" / "verify-queue.jsonl",
            f"got {path!r}",
        )

        print()
        print("[Section 2] Fail-closed refusal propagates as WorktreeParentError")

        # No usable sibling in the list, so detection falls back to <main>/worktrees under ~/.claude.
        raised = None
        with mock.patch.object(git_module, "get_worktree_list_porcelain",
                               lambda cwd=None: _porcelain((unsafe_main, "main"))):
            try:
                result = verify_queue.resolve_queue_path(cwd)
            except worktrees_module.WorktreeParentError as exc:
                raised = exc
                result = "<raised>"
        test_result(
            "unsafe main worktree raises WorktreeParentError from resolve_queue_path",
            raised is not None,
            f"returned {result!r}",
        )
        test_result(
            "refusal is not converted into a None queue path",
            result != None,  # noqa: E711 - explicit sentinel check, not a truthiness test
        )

        print()
        print("[Section 3] Detection-level refusal propagates unchanged")

        raised = None
        with mock.patch.object(worktrees_module, "detect_worktree_parent",
                               side_effect=worktrees_module.WorktreeParentError("sentinel refusal")):
            try:
                verify_queue.resolve_queue_path(cwd)
            except worktrees_module.WorktreeParentError as exc:
                raised = exc
        test_result(
            "WorktreeParentError from detect_worktree_parent propagates out of resolve_queue_path",
            raised is not None and "sentinel refusal" in str(raised),
        )
    finally:
        if original_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = original_home

    print()
    h.summarize_and_exit()
