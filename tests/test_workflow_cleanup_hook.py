#!/usr/bin/env python3
"""
Test suite for the commands.cleanup teardown hook in apply_cleanup.

The hook is read from the MAIN worktree's .claude/project.yaml, runs from the target worktree
before it is removed, and a failing hook keeps the worktree and branch.

Run with: python3 tests/test_workflow_cleanup_hook.py
"""

import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.cleanup import apply_cleanup, CleanupPlan
from workflow import validation, git
from workflow.checks import build_check_order
from _test_harness import Harness
from _git_fixture import GitFixture

git.pull_ff_only = lambda remote, branch, cwd=None: (True, None)


@contextmanager
def _no_lock(main_worktree: Path):
    yield None


validation.validation_lock = _no_lock


def _run(hook_yaml):
    """Apply a cleanup in a fixture; returns (result, err, fixture, wt_path, marker_path)."""
    fixture = GitFixture()
    fixture.create_initial_commit("initial")
    fixture.create_branch("feature/test")
    wt = fixture.create_worktree("feature/test")
    if hook_yaml is not None:
        claude_dir = fixture.repo_root / ".claude"
        claude_dir.mkdir(exist_ok=True)
        (claude_dir / "project.yaml").write_text(hook_yaml)
    plan = CleanupPlan(
        target_worktree=str(wt), current_branch="feature/test", pr_state="MERGED",
        pr_number=1, expected_head_sha=fixture.get_head_sha(cwd=wt), cache_hash=None,
        check_commands=[],
    )
    old = os.getcwd()
    os.chdir(fixture.repo_root)
    try:
        result, err = apply_cleanup(json.dumps(plan.to_dict()))
    finally:
        os.chdir(old)
    return result, err, fixture, wt


if __name__ == "__main__":
    h = Harness("WORKFLOW CLEANUP HOOK TEST SUITE")
    t = h.test_result

    print("[Section 1] cleanup is not treated as a check command")
    order, skipped = build_check_order({"check": "x", "cleanup": "y"})
    t("cleanup is excluded from the check order", order == ["check"], str(order))

    print("[Section 2] hook runs from the target worktree before removal")
    marker_rel = "hook-ran.txt"
    # Writes a file outside the worktree (named by cwd) proving cwd and ordering.
    result, err, fx, wt = _run('commands:\n  cleanup: "pwd > ../hook-cwd.txt && test -d ."\n')
    try:
        cwd_file = wt.parent / "hook-cwd.txt"
        t("hook ran with the target worktree as cwd",
          cwd_file.exists() and Path(cwd_file.read_text().strip()).resolve() == wt.resolve())
        t("cleanup succeeded and removed the worktree",
          result.success is True and not wt.exists(), f"err={err}")
        t("hook is noted", any("commands.cleanup ran" in n for n in result.notes))
    finally:
        fx.cleanup()

    print("[Section 3] a failing hook keeps the worktree and branch")
    result, err, fx, wt = _run('commands:\n  cleanup: "echo boom >&2; exit 3"\n')
    try:
        t("apply reports an error", err is not None and "exit 3" in str(err) and "boom" in str(err), str(err))
        t("worktree still exists", wt.exists())
        t("branch still exists", fx.branch_exists("feature/test"))
        t("failure recorded", any("commands.cleanup failed" in f for f in result.validation_failures))
    finally:
        fx.cleanup()

    print("[Section 4] no hook means unchanged behavior")
    result, err, fx, wt = _run(None)
    try:
        t("cleanup succeeds without a hook", result.success is True and err is None, f"err={err}")
    finally:
        fx.cleanup()

    print("[Section 5] a worktree's own project.yaml is not trusted")
    fixture_result = None
    fx = GitFixture()
    try:
        fx.create_initial_commit("initial")
        fx.create_branch("feature/test")
        wt = fx.create_worktree("feature/test")
        (wt / ".claude").mkdir(exist_ok=True)
        (wt / ".claude" / "project.yaml").write_text('commands:\n  cleanup: "exit 9"\n')
        plan = CleanupPlan(target_worktree=str(wt), current_branch="feature/test", pr_state="MERGED",
                           pr_number=1, expected_head_sha=fx.get_head_sha(cwd=wt), cache_hash=None,
                           check_commands=[])
        old = os.getcwd()
        os.chdir(fx.repo_root)
        try:
            result, err = apply_cleanup(json.dumps(plan.to_dict()))
        finally:
            os.chdir(old)
        t("hook in the worktree's own project.yaml is ignored", result.success is True, f"err={err}")
    finally:
        fx.cleanup()

    print("[Section 6] an invalid hook value blocks removal")
    result, err, fx, wt = _run('commands:\n  cleanup: 5\n')
    try:
        t("non-string hook is an error and keeps the worktree",
          err is not None and wt.exists(), str(err))
    finally:
        fx.cleanup()

    h.summarize_and_exit()
