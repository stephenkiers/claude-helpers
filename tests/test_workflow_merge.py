#!/usr/bin/env python3
"""
Test suite for merge planning and application.

Run with: python3 tests/test_workflow_merge.py
"""

import sys
import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.merge import plan_merge, apply_merge, merge_lock_path, MergePlan, MergeResult, _get_merge_apply_timeout, DEFAULT_MERGE_APPLY_TIMEOUT_SECS
from workflow.safety import Unknown
from workflow.merge import QueueDetection, QueueGuardDecision
from workflow.merge import _resolve_pr_from_worktree
from _test_harness import Harness


if __name__ == "__main__":
    # apply_merge now writes its lock file under ~/.claude/state/merge-locks/ (moved
    # out of the target worktree — see workflow.merge.merge_lock_path). Redirect HOME
    # for this whole run so the suite never touches the real developer machine's home.
    fake_home = tempfile.mkdtemp()
    os.environ["HOME"] = fake_home

    # These suites predate the merge-queue guard and use non-git fake worktrees, which the
    # guard (correctly) reads as "unknown" and refuses. Pin it to "no queue" for the whole
    # run; the guard itself is covered by its own suite.
    mock.patch(
        "workflow.merge.queue_guard",
        return_value=QueueGuardDecision(decision="proceed", detection=QueueDetection(state="absent")),
    ).start()

    try:
        h = Harness("WORKFLOW MERGE TEST SUITE")
        test_result = h.test_result

        print("[Section 1] Merge plan dataclass round-trip")

        plan = MergePlan(
            pr_number=42,
            head_ref="feature/test",
            target_worktree="/tmp/wt",
            blocking_failures=[]
        )

        plan_dict = plan.to_dict()
        test_result(
            "MergePlan.to_dict() produces valid dict",
            isinstance(plan_dict, dict) and plan_dict["pr_number"] == 42
        )

        restored = MergePlan.from_dict(plan_dict)
        test_result(
            "MergePlan.from_dict() restores all fields",
            restored.head_ref == "feature/test" and restored.pr_number == 42
        )

        print()
        print("[Section 2] Merge result dataclass")

        result = MergeResult(
            success=True,
            pr_merged=True,
            merge_gate_used="just merge"
        )

        result_dict = result.to_dict()
        test_result(
            "MergeResult.to_dict() includes success flag",
            result_dict["success"] is True and result_dict["merge_gate_used"] == "just merge"
        )

        print()
        print("[Section 3] Push gate detects failures correctly")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            wt_dir = tmppath / "worktree"
            wt_dir.mkdir()

            with mock.patch("workflow.merge._run_push_gate") as mock_gate:
                with mock.patch("workflow.merge._resolve_pr_from_worktree") as mock_resolve:
                    mock_gate.return_value = []
                    mock_resolve.return_value = (42, "feature", str(wt_dir))
                    plan_obj, err = plan_merge(str(wt_dir))
                    test_result(
                        "plan_merge succeeds when push gate passes",
                        plan_obj and plan_obj.blocking_failures == []
                    )

            with mock.patch("workflow.merge._run_push_gate") as mock_gate:
                with mock.patch("workflow.merge._resolve_pr_from_worktree") as mock_resolve:
                    mock_gate.return_value = ["Uncommitted changes", "Unpushed commits"]
                    mock_resolve.return_value = (42, "feature", str(wt_dir))
                    plan_obj, err = plan_merge(str(wt_dir))
                    test_result(
                        "plan_merge captures push gate failures",
                        plan_obj and len(plan_obj.blocking_failures) == 2
                    )

        print()
        print("[Section 4] Apply merge rejects stale plans")

        plan = MergePlan(
            pr_number=42,
            head_ref="feature",
            target_worktree="/tmp/nonexistent",
            blocking_failures=["test failure"]
        )
        plan_json = json.dumps(plan.to_dict())

        result, err = apply_merge(plan_json)
        test_result(
            "apply_merge rejects plan with blocking_failures",
            not result.success and result.error is not None
        )

        print()
        print("[Section 5] Apply merge lock file prevents concurrent runs")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            wt_dir = tmppath / "worktree"
            wt_dir.mkdir()

            plan = MergePlan(
                pr_number=42,
                head_ref="feature",
                target_worktree=str(wt_dir),
                blocking_failures=[]
            )

            lock_file = merge_lock_path(str(wt_dir))
            lock_file.parent.mkdir(parents=True, exist_ok=True)
            lock_file.write_text("locked")

            plan_json = json.dumps(plan.to_dict())
            result, err = apply_merge(plan_json)

            test_result(
                "apply_merge detects existing lock file",
                not result.success and "lock" in str(result.error).lower()
            )

            test_result(
                "apply_merge does not clear lock file",
                lock_file.exists()
            )

            test_result(
                "apply_merge does not write the lock file inside the target worktree",
                not (wt_dir / ".claude" / ".merge-and-cleanup.lock").exists()
            )

            lock_file.unlink()

        print()
        print("[Section 6] Apply merge creates lock file on execution")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            wt_dir = tmppath / "worktree"
            wt_dir.mkdir()

            plan = MergePlan(
                pr_number=42,
                head_ref="feature",
                target_worktree=str(wt_dir),
                blocking_failures=[]
            )

            lock_file = merge_lock_path(str(wt_dir))

            with mock.patch("workflow.merge._check_just_merge") as mock_just:
                with mock.patch("workflow.merge._run_gh_pr_merge") as mock_merge:
                    mock_just.return_value = False
                    mock_merge.return_value = (True, None)

                    plan_json = json.dumps(plan.to_dict())
                    result, err = apply_merge(plan_json)

                    test_result(
                        "apply_merge creates lock file before merge",
                        lock_file.exists()
                    )

                    test_result(
                        "apply_merge does not remove lock file after success",
                        result.success and lock_file.exists()
                    )

            lock_file.unlink()

        print()
        print("[Section 7] Apply merge uses unpushed commits check correctly")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            wt_dir = tmppath / "worktree"
            wt_dir.mkdir()

            with mock.patch("workflow.git.rev_list_count") as mock_count:
                with mock.patch("workflow.merge._run_push_gate") as mock_gate:
                    with mock.patch("workflow.merge._resolve_pr_from_worktree") as mock_resolve:
                        mock_resolve.return_value = (42, "feature", str(wt_dir))
                        mock_count.return_value = -1
                        mock_gate.return_value = ["Could not determine unpushed commit count"]

                        plan_obj, err = plan_merge(str(wt_dir))

                        test_result(
                            "plan_merge treats -1 (rev_list error) as failure, not 0",
                            plan_obj and "Could not determine" in str(plan_obj.blocking_failures)
                        )

        print()
        print("[Section 8] Apply merge calls gh pr merge after repo-cache gate passes")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            wt_dir = tmppath / "worktree"
            wt_dir.mkdir()
            claude_dir = wt_dir / ".claude"
            claude_dir.mkdir()

            plan = MergePlan(
                pr_number=47,
                head_ref="feature",
                target_worktree=str(wt_dir),
                blocking_failures=[]
            )

            with mock.patch("workflow.merge._check_just_merge") as mock_just:
                with mock.patch("workflow.merge._run_merge_gate_checks") as mock_gate:
                    with mock.patch("workflow.merge._run_gh_pr_merge") as mock_merge:
                        mock_just.return_value = False
                        mock_gate.return_value = (True, True, None)
                        mock_merge.return_value = (True, None)

                        plan_json = json.dumps(plan.to_dict())
                        result, err = apply_merge(plan_json)

                        test_result(
                            "apply_merge: repo-cache gate returns 3-tuple",
                            mock_gate.return_value is not None,
                            "Gate should return (success, gate_applied, detail)"
                        )
                        test_result(
                            "apply_merge calls _run_gh_pr_merge exactly once on repo-cache-check path",
                            result.success and mock_merge.call_count == 1 and result.merge_gate_used == "repo-cache check",
                            f"Expected _run_gh_pr_merge called once after gate passes, got {mock_merge.call_count} calls"
                        )

        print()
        print("[Section 9] Apply merge diagnostic details (Fix 3)")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            wt_dir = tmppath / "worktree"
            wt_dir.mkdir()
            claude_dir = wt_dir / ".claude"
            claude_dir.mkdir()

            plan = MergePlan(
                pr_number=48,
                head_ref="feature",
                target_worktree=str(wt_dir),
                blocking_failures=[]
            )

            with mock.patch("workflow.merge._check_just_merge") as mock_just:
                with mock.patch("workflow.merge._run_just_merge") as mock_run_just:
                    mock_just.return_value = True
                    mock_run_just.return_value = (False, "just merge error detail")

                    plan_json = json.dumps(plan.to_dict())
                    result, err = apply_merge(plan_json)

                    test_result(
                        "apply_merge includes diagnostic detail in error message",
                        result.error and "just merge error detail" in str(result.error)
                    )

        print()
        print("[Section 10] apply_merge records a failed cache write without failing the merge")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            wt_dir = tmppath / "worktree"
            wt_dir.mkdir()
            (wt_dir / ".claude").mkdir()

            plan = MergePlan(
                pr_number=50,
                head_ref="feature",
                target_worktree=str(wt_dir),
                blocking_failures=[]
            )

            with mock.patch("workflow.merge._check_just_merge") as mock_just:
                with mock.patch("workflow.merge._run_gh_pr_merge") as mock_merge:
                    with mock.patch("workflow.merge.write_cache") as mock_write_cache:
                        mock_just.return_value = False
                        mock_merge.return_value = (True, None)
                        mock_write_cache.return_value = (False, Unknown("Permission denied"))

                        plan_json = json.dumps(plan.to_dict())
                        result, err = apply_merge(plan_json)

                        test_result(
                            "apply_merge still succeeds when the cache write fails",
                            result.success is True
                        )
                        test_result(
                            "apply_merge records the cache write failure detail",
                            result.cache_write_failed is not None and "Permission denied" in result.cache_write_failed
                        )

        print()
        print("[Section 11] apply_merge with repo-cache containing only commands.test gates properly")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            wt_dir = tmppath / "worktree"
            wt_dir.mkdir()
            (wt_dir / ".claude").mkdir()

            cache_file = wt_dir / ".claude" / "repo-cache.json"
            cache_file.write_text(json.dumps({
                "commands": {
                    "test": "pytest"
                }
            }))

            plan = MergePlan(
                pr_number=51,
                head_ref="feature",
                target_worktree=str(wt_dir),
                blocking_failures=[]
            )

            with mock.patch("workflow.merge._check_just_merge") as mock_just:
                with mock.patch("workflow.merge._run_gh_pr_merge") as mock_merge:
                    with mock.patch("workflow.merge.write_cache") as mock_write_cache:
                        with mock.patch("workflow.checks.run_checks") as mock_checks:
                            mock_just.return_value = False
                            mock_merge.return_value = (True, None)
                            mock_write_cache.return_value = (True, None)

                            from workflow.checks import CheckResults

                            check_result = CheckResults(
                                results=[],
                                all_passed=True,
                                status="passed",
                                executed=["test"]
                            )
                            mock_checks.return_value = (check_result, None)

                            plan_json = json.dumps(plan.to_dict())
                            result, err = apply_merge(plan_json)

                            test_result(
                                "apply_merge: repo-cache with only commands.test runs it",
                                mock_checks.called
                            )
                            test_result(
                                "apply_merge: repo-cache with only commands.test gates the merge",
                                result.success is True and result.merge_gate_used == "repo-cache check"
                            )
                            test_result(
                                "apply_merge: repo-cache with only commands.test does NOT skip to 'no gate'",
                                "no gate" not in result.merge_gate_used
                            )

        print()
        print("[Section 12] Auto-detect PR from worktree when no argument given")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            wt_dir = tmppath / "worktree"
            wt_dir.mkdir()

            with mock.patch("workflow.merge.git.is_linked_worktree") as mock_is_linked:
                with mock.patch("workflow.merge._run_push_gate") as mock_gate:
                    with mock.patch("workflow.merge._resolve_pr_from_worktree") as mock_resolve:
                        mock_is_linked.return_value = True
                        mock_gate.return_value = []
                        mock_resolve.return_value = (42, "feature", str(wt_dir))

                        plan_obj, err = plan_merge(None, cwd=wt_dir)
                        test_result(
                            "plan_merge(None) in a linked worktree resolves via cwd",
                            plan_obj and plan_obj.pr_number == 42 and plan_obj.head_ref == "feature"
                        )
                        test_result(
                            "plan_merge(None) calls _resolve_pr_from_worktree with cwd path",
                            mock_resolve.called and mock_resolve.call_args[0][0] == str(wt_dir.resolve())
                        )

        print()
        print("[Section 13] plan_merge with no argument fails in main worktree")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            wt_dir = tmppath / "worktree"
            wt_dir.mkdir()

            with mock.patch("workflow.merge.git.is_linked_worktree") as mock_is_linked:
                mock_is_linked.return_value = False

                plan_obj, err = plan_merge(None, cwd=wt_dir)
                test_result(
                    "plan_merge(None) in main worktree returns error",
                    plan_obj is None and err is not None
                )
                test_result(
                    "plan_merge(None) error mentions not being in a linked worktree",
                    "not in a linked worktree" in str(err).lower()
                )

        print()
        print("[Section 14] plan_merge with blank string behaves like None")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            wt_dir = tmppath / "worktree"
            wt_dir.mkdir()

            with mock.patch("workflow.merge.git.is_linked_worktree") as mock_is_linked:
                with mock.patch("workflow.merge._run_push_gate") as mock_gate:
                    with mock.patch("workflow.merge._resolve_pr_from_worktree") as mock_resolve:
                        mock_is_linked.return_value = True
                        mock_gate.return_value = []
                        mock_resolve.return_value = (42, "feature", str(wt_dir))

                        plan_obj, err = plan_merge("", cwd=wt_dir)
                        test_result(
                            "plan_merge('') in a linked worktree resolves via cwd (regression test)",
                            plan_obj and plan_obj.pr_number == 42
                        )
                        test_result(
                            "plan_merge('') doesn't use Path('').exists() branch",
                            mock_resolve.called,
                            "Should use is_linked_worktree, not Path('').exists()"
                        )

        print()
        print("[Section 15] plan_merge with whitespace-only argument behaves like None")

        for ws in ["   ", "\t", "  \t  \n  "]:
            with tempfile.TemporaryDirectory() as tmpdir:
                tmppath = Path(tmpdir)
                wt_dir = tmppath / "worktree"
                wt_dir.mkdir()

                with mock.patch("workflow.merge.git.is_linked_worktree") as mock_is_linked:
                    with mock.patch("workflow.merge._run_push_gate") as mock_gate:
                        with mock.patch("workflow.merge._resolve_pr_from_worktree") as mock_resolve:
                            mock_is_linked.return_value = True
                            mock_gate.return_value = []
                            mock_resolve.return_value = (42, "feature", str(wt_dir))

                            plan_obj, err = plan_merge(ws, cwd=wt_dir)
                            test_result(
                                f"plan_merge({ws!r}) with whitespace resolves via cwd",
                                plan_obj and plan_obj.pr_number == 42,
                                "Whitespace-only string should behave like None"
                            )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            wt_dir = tmppath / "worktree"
            wt_dir.mkdir()

            with mock.patch("workflow.merge.git.is_linked_worktree") as mock_is_linked:
                mock_is_linked.return_value = False

                # Test whitespace-only fails in main worktree
                plan_obj, err = plan_merge("   ", cwd=wt_dir)
                test_result(
                    "plan_merge('   ') in main worktree returns error",
                    plan_obj is None and err is not None,
                    "Whitespace in non-linked worktree should error"
                )

        print()
        print("[Section 16] _get_merge_apply_timeout handles invalid env var gracefully (regression test)")

        # Test that invalid env var doesn't crash
        old_env = os.environ.get("MERGE_APPLY_TIMEOUT_SECS")
        try:
            os.environ["MERGE_APPLY_TIMEOUT_SECS"] = "not-a-number"
            timeout = _get_merge_apply_timeout()
            test_result(
                "_get_merge_apply_timeout on invalid input returns default",
                timeout == DEFAULT_MERGE_APPLY_TIMEOUT_SECS,
                f"Expected {DEFAULT_MERGE_APPLY_TIMEOUT_SECS}, got {timeout}"
            )
        finally:
            if old_env is not None:
                os.environ["MERGE_APPLY_TIMEOUT_SECS"] = old_env
            else:
                os.environ.pop("MERGE_APPLY_TIMEOUT_SECS", None)

        # Test that valid integer works
        old_env = os.environ.get("MERGE_APPLY_TIMEOUT_SECS")
        try:
            os.environ["MERGE_APPLY_TIMEOUT_SECS"] = "600"
            timeout = _get_merge_apply_timeout()
            test_result(
                "_get_merge_apply_timeout with valid integer returns that value",
                timeout == 600,
                f"Expected 600, got {timeout}"
            )
        finally:
            if old_env is not None:
                os.environ["MERGE_APPLY_TIMEOUT_SECS"] = old_env
            else:
                os.environ.pop("MERGE_APPLY_TIMEOUT_SECS", None)

        # Test that negative values fall back to default
        old_env = os.environ.get("MERGE_APPLY_TIMEOUT_SECS")
        try:
            os.environ["MERGE_APPLY_TIMEOUT_SECS"] = "-100"
            timeout = _get_merge_apply_timeout()
            test_result(
                "_get_merge_apply_timeout on negative value returns default",
                timeout == DEFAULT_MERGE_APPLY_TIMEOUT_SECS,
                f"Expected {DEFAULT_MERGE_APPLY_TIMEOUT_SECS}, got {timeout}"
            )
        finally:
            if old_env is not None:
                os.environ["MERGE_APPLY_TIMEOUT_SECS"] = old_env
            else:
                os.environ.pop("MERGE_APPLY_TIMEOUT_SECS", None)

        print()
        print("[Section 17] PR-resolution error message attribution (auto-detect vs explicit path)")

        # Auto-detect case (no argument): the target IS the invoking shell's
        # current branch, so the error should say "current branch".
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            wt_dir = tmppath / "worktree"
            wt_dir.mkdir()

            with mock.patch("workflow.merge.git.is_linked_worktree") as mock_is_linked:
                with mock.patch("workflow.merge._run_push_gate") as mock_gate:
                    with mock.patch("workflow.merge._resolve_pr_from_worktree") as mock_resolve:
                        mock_is_linked.return_value = True
                        mock_gate.return_value = []
                        mock_resolve.return_value = (None, None, str(wt_dir))

                        plan_obj, err = plan_merge(None, cwd=wt_dir)
                        test_result(
                            "plan_merge(None) with unresolved PR returns error",
                            plan_obj is None and err is not None
                        )
                        test_result(
                            "Auto-detect case: error references 'current branch'",
                            "current branch" in str(err).lower(),
                            f"got: {err}"
                        )

        # Explicit-path case: the target is NOT necessarily the invoking
        # shell's current branch, so the error must NOT say "current branch".
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            wt_dir = tmppath / "worktree"
            wt_dir.mkdir()

            with mock.patch("workflow.merge._resolve_pr_from_worktree") as mock_resolve:
                mock_resolve.return_value = (None, None, str(wt_dir))

                plan_obj, err = plan_merge(str(wt_dir))
                test_result(
                    "plan_merge(explicit path) with unresolved PR returns error",
                    plan_obj is None and err is not None
                )
                test_result(
                    "Explicit-path case: error does NOT reference 'current branch'",
                    "current branch" not in str(err).lower(),
                    f"got: {err}"
                )

        print()
        print("[Section 18] _resolve_pr_from_worktree trusts the cached PR number only after a head probe")

        def _is_probe(call):
            """True when a pr_view_json call is the cached-number head probe."""
            return call.args[:2] == ("68", ["headRefName", "state"])

        def _resolve_with(cache_data, head_ref, probe_result, by_branch=(9, "OPEN")):
            """Run _resolve_pr_from_worktree on a fake worktree; return (result, pr_view mock, wt path)."""
            with tempfile.TemporaryDirectory() as tmpdir:
                wt_dir = Path(tmpdir) / "worktree"
                (wt_dir / ".claude").mkdir(parents=True)
                if cache_data is not None:
                    (wt_dir / ".claude" / "github-cache.json").write_text(json.dumps(cache_data))

                def fake_view(branch_or_number, json_args, cwd=None):
                    if json_args == ["headRefName", "state"]:
                        return probe_result
                    return {"number": by_branch[0], "state": by_branch[1]}

                with mock.patch("workflow.git.get_current_branch", return_value=head_ref):
                    with mock.patch("workflow.git.pr_view_json", side_effect=fake_view) as mock_view:
                        result = _resolve_pr_from_worktree(str(wt_dir))
                return result, mock_view, str(wt_dir)

        track_shaped_pr = {"number": 68, "url": "https://github.com/owner/repo/pull/68"}
        verified = {"headRefName": "feature/x", "state": "OPEN"}

        # 18a: cache names another branch -> never probed, by-branch result returned.
        result, mock_view, wt = _resolve_with(
            {"branch": "other-branch", "pr": track_shaped_pr}, "feature/x", verified
        )
        test_result(
            "resolve: cache for another branch is never probed by cached number",
            not any(_is_probe(c) for c in mock_view.call_args_list),
            f"calls={mock_view.call_args_list}"
        )
        test_result(
            "resolve: cache for another branch falls through to the by-branch number",
            result[0] == 9 and result[1] == "feature/x",
            f"result={result!r}"
        )

        # 18b: matching cache, probe reports a different head -> by-branch result returned.
        result, mock_view, wt = _resolve_with(
            {"branch": "feature/x", "pr": track_shaped_pr},
            "feature/x",
            {"headRefName": "other-branch", "state": "OPEN"},
        )
        test_result(
            "resolve: matching cache with head mismatch falls back to the by-branch number",
            result[0] == 9,
            f"result={result!r}"
        )

        # 18c: matching cache, verified head -> cached number returned.
        result, mock_view, wt = _resolve_with({"branch": "feature/x", "pr": track_shaped_pr}, "feature/x", verified)
        test_result(
            "resolve: matching cache with verified head returns the cached number",
            result[0] == 68,
            f"result={result!r}"
        )
        test_result(
            "resolve: matching cache with verified head returns the verified branch as head_ref",
            result[1] == "feature/x" and result[2] == wt,
            f"result={result!r}"
        )
        test_result(
            "resolve: matching cache with verified head probes the cached number once",
            sum(1 for c in mock_view.call_args_list if _is_probe(c)) == 1,
            f"calls={mock_view.call_args_list}"
        )

        # 18d: /track-shaped cache (pr, no schema_version) is trusted and probed.
        result, mock_view, wt = _resolve_with(
            {"branch": "feature/x", "issue": {"number": 55, "url": "u", "title": "t", "body": "b", "state": "OPEN"},
             "pr": track_shaped_pr},
            "feature/x",
            verified,
        )
        test_result(
            "resolve: /track-shaped cache (no schema_version) is probed and its number returned",
            result[0] == 68 and any(_is_probe(c) for c in mock_view.call_args_list),
            f"result={result!r}"
        )

        # 18e: probe returns {} -> falls through to the by-branch path.
        result, mock_view, wt = _resolve_with({"branch": "feature/x", "pr": track_shaped_pr}, "feature/x", {})
        test_result(
            "resolve: probe returning {} falls through to the by-branch number",
            result[0] == 9,
            f"result={result!r}"
        )

        # 18f: no cache file -> no probe, by-branch result returned.
        result, mock_view, wt = _resolve_with(None, "feature/x", verified)
        test_result(
            "resolve: no cache file never probes the cached number",
            not any(_is_probe(c) for c in mock_view.call_args_list) and result[0] == 9,
            f"result={result!r} calls={mock_view.call_args_list}"
        )

        print()
        h.summarize_and_exit()
    finally:
        shutil.rmtree(fake_home, ignore_errors=True)
