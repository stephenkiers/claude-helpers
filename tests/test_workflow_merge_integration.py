#!/usr/bin/env python3
"""
Integration test suite for merge apply operations.

Tests lock file reentrancy behavior and merge gate selection.
Must be run from within the fixture repo root for git commands to work correctly.

Run with: python3 tests/test_workflow_merge_integration.py
"""

import sys
import os
import json
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.merge import apply_merge, merge_lock_path, MergePlan
from _test_harness import Harness
from _git_fixture import GitFixture


if __name__ == "__main__":
    h = Harness("WORKFLOW MERGE INTEGRATION TEST SUITE")
    test_result = h.test_result

    print("[Section 1] apply_merge creates lock file in .claude directory")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)

        fixture.create_initial_commit("initial")
        fixture.create_branch("feature/merge-test")
        wt_path = fixture.create_worktree("feature/merge-test")

        home_dir = fixture.repo_root.parent / "fake-home"
        home_dir.mkdir(parents=True, exist_ok=True)

        fixture.commit_in_worktree(wt_path, "add content")

        plan = MergePlan(
            pr_number=42,
            head_ref="feature/merge-test",
            target_worktree=str(wt_path)
        )
        plan_json = json.dumps(plan.to_dict())

        with mock.patch("workflow.merge.Path.home", return_value=home_dir):
            lock_file = merge_lock_path(str(wt_path))
            result, err = apply_merge(plan_json)

        test_result(
            "apply_merge creates lock file",
            lock_file.exists(),
            "lock file should exist under ~/.claude/state/merge-locks/"
        )

        test_result(
            "apply_merge does not write the lock file inside the target worktree",
            not (wt_path / ".claude" / ".merge-and-cleanup.lock").exists()
        )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 2] apply_merge lock file blocks concurrent attempts")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)

        fixture.create_initial_commit("initial")
        fixture.create_branch("feature/concurrent")
        wt_path = fixture.create_worktree("feature/concurrent")

        home_dir = fixture.repo_root.parent / "fake-home"
        home_dir.mkdir(parents=True, exist_ok=True)

        fixture.commit_in_worktree(wt_path, "add content")

        plan = MergePlan(
            pr_number=43,
            head_ref="feature/concurrent",
            target_worktree=str(wt_path)
        )
        plan_json = json.dumps(plan.to_dict())

        with mock.patch("workflow.merge.Path.home", return_value=home_dir):
            result1, err1 = apply_merge(plan_json)
            result1_succeeded = result1.success if result1 else False

            result2, err2 = apply_merge(plan_json)
        test_result(
            "Second apply_merge call is rejected (lock exists)",
            result2.success is False and result2.error is not None,
            "second attempt should be rejected due to existing lock"
        )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 3] Lock file persists after apply_merge completion")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)

        fixture.create_initial_commit("initial")
        fixture.create_branch("feature/persistent")
        wt_path = fixture.create_worktree("feature/persistent")

        home_dir = fixture.repo_root.parent / "fake-home"
        home_dir.mkdir(parents=True, exist_ok=True)

        fixture.commit_in_worktree(wt_path, "add content")

        plan = MergePlan(
            pr_number=44,
            head_ref="feature/persistent",
            target_worktree=str(wt_path)
        )
        plan_json = json.dumps(plan.to_dict())

        with mock.patch("workflow.merge.Path.home", return_value=home_dir):
            lock_file = merge_lock_path(str(wt_path))
            result, err = apply_merge(plan_json)

        test_result(
            "Lock file exists after apply_merge completes",
            lock_file.exists(),
            "lock file should NOT be auto-cleared after completion"
        )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 4] apply_merge handles blocking failures")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)

        fixture.create_initial_commit("initial")
        fixture.create_branch("feature/blocked")
        wt_path = fixture.create_worktree("feature/blocked")

        fixture.commit_in_worktree(wt_path, "add content")

        plan = MergePlan(
            pr_number=45,
            head_ref="feature/blocked",
            target_worktree=str(wt_path),
            blocking_failures=["detached HEAD", "uncommitted changes"]
        )
        plan_json = json.dumps(plan.to_dict())

        result, err = apply_merge(plan_json)

        test_result(
            "apply_merge rejects plan with blocking failures",
            result.success is False and result.error is not None,
            "plan with blocking failures should be rejected"
        )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 5] apply_merge lock file blocks after failure")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)

        fixture.create_initial_commit("initial")
        fixture.create_branch("feature/failed")
        wt_path = fixture.create_worktree("feature/failed")

        home_dir = fixture.repo_root.parent / "fake-home"
        home_dir.mkdir(parents=True, exist_ok=True)

        fixture.commit_in_worktree(wt_path, "add content")

        plan = MergePlan(
            pr_number=46,
            head_ref="feature/failed",
            target_worktree=str(wt_path),
            blocking_failures=["test failure"]
        )
        plan_json = json.dumps(plan.to_dict())

        plan2 = MergePlan(
            pr_number=46,
            head_ref="feature/failed",
            target_worktree=str(wt_path),
            blocking_failures=["test failure"]
        )
        plan_json2 = json.dumps(plan2.to_dict())

        with mock.patch("workflow.merge.Path.home", return_value=home_dir):
            result1, err1 = apply_merge(plan_json)
            result2, err2 = apply_merge(plan_json2)
        test_result(
            "Second apply_merge is blocked by lock file even after first failure",
            result2.success is False and result2.error is not None,
            "lock should prevent retry even after failure"
        )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 6] merge gate reuses a stored full record only with a fresh base-ancestor precondition")

    from unittest import mock as _mock
    from workflow.checks import CheckResults, validate
    from workflow.merge import _run_merge_gate_checks
    from workflow.models import CheckRecord, FullKey, satisfies

    def _fake_run_checks(calls):
        def _run(*args, **kwargs):
            calls.append(kwargs.get("command") or (args[0] if args else None))
            return CheckResults(results=[], all_passed=True, status="passed", executed=["check"]), None
        return _run

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)

        # Real bare origin, so the base fetch in the gate can succeed or fail for real.
        origin = fixture.tmpdir / "origin.git"
        fixture._run_git(["init", "--bare", "-b", "main", str(origin)], cwd=fixture.tmpdir)
        fixture.create_initial_commit("initial")
        fixture._run_git(["remote", "add", "origin", str(origin)], cwd=fixture.repo_root)
        fixture._run_git(["push", "-u", "origin", "main"], cwd=fixture.repo_root)
        # origin/HEAD tells the check resolver and the gate which base is the default branch.
        fixture._run_git(["remote", "set-head", "origin", "main"], cwd=fixture.repo_root)
        # .claude/ is per-machine state and must stay out of the tree the check record keys on.
        (fixture.repo_root / ".gitignore").write_text(".claude/\n")
        fixture._run_git(["add", ".gitignore"], cwd=fixture.repo_root)
        fixture._run_git(["commit", "-m", "ignore claude"], cwd=fixture.repo_root)
        fixture._run_git(["push", "origin", "main"], cwd=fixture.repo_root)

        fixture.create_branch("feature/gate")
        wt_path = fixture.create_worktree("feature/gate")
        fixture.commit_in_worktree(wt_path, "feature work")

        (wt_path / ".claude").mkdir(exist_ok=True)
        (wt_path / ".claude" / "repo-cache.json").write_text(json.dumps({
            "commands": {"check": "make full-check", "check_minimum": "make min-check"}
        }))

        calls: list = []
        with _mock.patch("workflow.checks.run_checks", side_effect=_fake_run_checks(calls)):
            gate1 = _run_merge_gate_checks(wt_path)
            count_after_first = len(calls)

            gate2 = _run_merge_gate_checks(wt_path)
            count_after_second = len(calls)

            test_result(
                "gate: first run executes the full check",
                gate1 == (True, True, None) and count_after_first == 1,
                f"gate={gate1!r} calls={calls}"
            )
            test_result(
                "gate: second run reuses the stored full record when HEAD contains origin/main",
                gate2 == (True, True, None) and count_after_second == 1,
                f"gate={gate2!r} calls={calls}"
            )

            # Advance origin/main past HEAD: the record no longer satisfies the precondition.
            fixture._run_git(["checkout", "main"], cwd=fixture.repo_root)
            fixture.commit_in_worktree(fixture.repo_root, "advance main")
            fixture._run_git(["push", "origin", "main"], cwd=fixture.repo_root)
            gate3 = _run_merge_gate_checks(wt_path)
            count_after_third = len(calls)
            test_result(
                "gate: advancing origin/main past HEAD forces the check to run again",
                gate3 == (True, True, None) and count_after_third == 2,
                f"gate={gate3!r} calls={calls}"
            )

            # Fetch failure (unreachable origin): the precondition cannot hold, so the check reruns.
            fixture._run_git(["remote", "set-url", "origin", str(fixture.tmpdir / "missing.git")],
                             cwd=fixture.repo_root)
            gate4 = _run_merge_gate_checks(wt_path)
            count_after_fourth = len(calls)
            test_result(
                "gate: a broken origin URL (fetch failure) forces the check to run again",
                gate4 == (True, True, None) and count_after_fourth == 3,
                f"gate={gate4!r} calls={calls}"
            )

        # A minimum record never satisfies the full gate.
        fixture._run_git(["remote", "set-url", "origin", str(origin)], cwd=fixture.repo_root)
        fixture._run_git(["push", "origin", "main"], cwd=fixture.repo_root)
        records_dir = wt_path / ".claude" / "check-records"
        before_min = set(records_dir.glob("*.json")) if records_dir.exists() else set()
        min_calls: list = []
        # resolve_check_base expects a "ref: " prefix that `git symbolic-ref` does not print, so it
        # returns None in a real repo. Pin it to the real merge-base so the minimum record is written.
        merge_base = fixture._run_git(["merge-base", "HEAD", "origin/main"], cwd=wt_path)
        with _mock.patch("workflow.checks.run_checks", side_effect=_fake_run_checks(min_calls)):
            with _mock.patch("workflow.checks.resolve_check_base", return_value=merge_base):
                min_result = validate(wt_path, mode="minimum", force=True)
        new_records = sorted(set(records_dir.glob("*.json")) - before_min)
        test_result(
            "gate: a stored minimum record exists after a minimum validate",
            min_result.get("status") == "passed" and len(new_records) == 1,
            f"result={min_result!r} new={new_records}"
        )

        if len(new_records) == 1:
            min_record = CheckRecord.from_dict(json.loads(new_records[0].read_text()))
            full_key = FullKey(tree=min_record.key.tree, full_digest=min_record.key.full_digest)
            test_result(
                "models.satisfies: a minimum record does not satisfy a full key",
                satisfies(min_record, full_key) is False,
                f"key={min_record.key!r}"
            )

        full_calls: list = []
        with _mock.patch("workflow.checks.run_checks", side_effect=_fake_run_checks(full_calls)):
            gate5 = _run_merge_gate_checks(wt_path)
        test_result(
            "gate: a stored minimum record does not satisfy the merge gate (full check runs)",
            gate5 == (True, True, None) and len(full_calls) == 1,
            f"gate={gate5!r} calls={full_calls}"
        )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    h.summarize_and_exit()
