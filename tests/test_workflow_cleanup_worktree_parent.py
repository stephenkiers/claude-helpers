#!/usr/bin/env python3
"""
Test suite for how plan_cleanup surfaces a WorktreeParentError.

plan_cleanup must not swallow a fail-closed worktree-parent refusal: it returns
no plan and an Unknown error that carries the refusal reason.

Run with: python3 tests/test_workflow_cleanup_worktree_parent.py
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import workflow.worktrees as worktrees_module
import workflow.git as git_module
from workflow.cleanup import plan_cleanup
from workflow.safety import Unknown
from _test_harness import Harness


def _porcelain(*entries):
    """Build a `git worktree list --porcelain` blob from (path, branch) pairs."""
    return "\n".join(f"worktree {p}\nbranch refs/heads/{b}\n" for p, b in entries)


def _plan_with(target, porcelain_text=None, detect_error=None):
    """
    Run plan_cleanup for `target` with git/gh probes stubbed.

    Either `porcelain_text` feeds the real detection, or `detect_error` makes
    detect_worktree_parent raise that error directly.
    """
    patches = [
        mock.patch.object(git_module, "get_current_branch", lambda cwd=None: "feat/1-x"),
        mock.patch.object(git_module, "get_head_sha", lambda cwd=None: "abc123"),
        mock.patch.object(git_module, "pr_view_json", lambda *a, **k: {"state": "MERGED", "number": 42}),
        mock.patch.object(git_module, "pr_list_json", lambda *a, **k: []),
    ]
    if porcelain_text is not None:
        patches.append(mock.patch.object(
            git_module, "get_worktree_list_porcelain", lambda cwd=None: porcelain_text))
    if detect_error is not None:
        patches.append(mock.patch.object(
            worktrees_module, "detect_worktree_parent", side_effect=detect_error))
    for p in patches:
        p.start()
    try:
        return plan_cleanup(str(target), cwd=target)
    finally:
        for p in reversed(patches):
            p.stop()


if __name__ == "__main__":
    h = Harness("WORKFLOW CLEANUP WORKTREE-PARENT TEST SUITE")
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
        target = proj / "worktrees" / "feat-1-x"
        (target / ".claude").mkdir(parents=True)
        canonical_main.mkdir(parents=True)

        print("[Section 1] Happy path: canonical layout plans normally")

        plan, err = _plan_with(
            target,
            porcelain_text=_porcelain((canonical_main, "main"), (target, "feat/1-x")),
        )
        test_result(
            "canonical layout yields a plan with no error",
            plan is not None and err is None,
        )

        print()
        print("[Section 2] Fail-closed refusal from real detection is surfaced, not swallowed")

        # No usable sibling in the list, so detection falls back to <main>/worktrees,
        # which lands under ~/.claude and must be refused.
        plan, err = _plan_with(
            target,
            porcelain_text=_porcelain((unsafe_main, "main")),
        )
        test_result(
            "unsafe main worktree yields no plan",
            plan is None,
        )
        test_result(
            "unsafe main worktree yields an Unknown error",
            isinstance(err, Unknown),
        )
        test_result(
            "Unknown error carries the WorktreeParentError refusal reason",
            isinstance(err, Unknown) and "refusing unsafe WORKTREE_PARENT" in str(err),
            f"err={err!r}",
        )

        print()
        print("[Section 3] Fail-closed refusal raised by detect_worktree_parent is surfaced")

        plan, err = _plan_with(
            target,
            detect_error=worktrees_module.WorktreeParentError("sentinel refusal"),
        )
        test_result(
            "WorktreeParentError from detection yields no plan",
            plan is None,
        )
        test_result(
            "WorktreeParentError from detection yields an Unknown error naming the refusal",
            isinstance(err, Unknown) and "sentinel refusal" in str(err),
            f"err={err!r}",
        )
    finally:
        if original_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = original_home

    print()
    h.summarize_and_exit()
