#!/usr/bin/env python3
"""
Test suite for merge queue guard (detect_merge_queue, queue_guard, plan_merge refusal, apply_merge gate).

Tests the observable behavior of the merge queue guard:
- detect_merge_queue() state detection (configured, absent, unknown)
- queue_guard() base-scoped refusal logic
- plan_merge() early refusal and exit code 3
- apply_merge() authoritative gate (re-checks live)

Run with: python3 tests/test_merge_queue_guard.py
"""

import sys
import json
import os
import shutil
import tempfile
import subprocess
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.merge import (
    detect_merge_queue,
    queue_guard,
    plan_merge,
    apply_merge,
    MergePlan,
    QueueDetection,
    QueueGuardDecision,
)
from workflow.merge_queue import resolve_config_path, LayoutMismatchError
from _test_harness import Harness


if __name__ == "__main__":
    # Redirect HOME so merge locks go to a temp dir
    fake_home = tempfile.mkdtemp()
    os.environ["HOME"] = fake_home

    # Clear queue config env var to avoid interference
    os.environ.pop("MERGE_QUEUE_CONFIG", None)

    try:
        h = Harness("MERGE QUEUE GUARD TEST SUITE")
        test_result = h.test_result

        # ================================================================
        # SECTION 1: detect_merge_queue states
        # ================================================================
        print("[Section 1] detect_merge_queue state detection")

        # Test 1: worktrees/ layout with default config present
        print("  [Test 1] worktrees/ layout, config present → configured (default source)")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            container = tmpdir / "repo"
            container.mkdir()
            config_path = container / "merge-queue.json"
            config_path.write_text(json.dumps({"base": "main", "steps": ["echo test"]}))

            os.environ.pop("MERGE_QUEUE_CONFIG", None)
            # Mock resolve_config_path to return the config
            with mock.patch("workflow.merge_queue.resolve_config_path") as mock_resolve:
                mock_resolve.return_value = config_path
                detection = detect_merge_queue(container)

                test_result(
                    "detect_merge_queue: configured (default)",
                    detection.state == "configured" and detection.source == "default",
                    f"state={detection.state}, source={detection.source}"
                )

        # Test 2: worktrees/ layout, config absent
        print("  [Test 2] worktrees/ layout, config absent → absent")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            container = tmpdir / "repo"
            container.mkdir()

            os.environ.pop("MERGE_QUEUE_CONFIG", None)
            # Mock resolve_config_path to return None
            with mock.patch("workflow.merge_queue.resolve_config_path") as mock_resolve:
                mock_resolve.return_value = container / "merge-queue.json"
                detection = detect_merge_queue(container)
                test_result(
                    "detect_merge_queue: absent (no config)",
                    detection.state == "absent",
                    f"state={detection.state}"
                )

        # Test 3: MERGE_QUEUE_CONFIG env set to existing file outside repo
        print("  [Test 3] MERGE_QUEUE_CONFIG set to file outside repo → configured (env source)")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            outside_config = tmpdir / "outside-config.json"
            outside_config.write_text(json.dumps({"base": "main", "steps": ["echo test"]}))

            os.environ["MERGE_QUEUE_CONFIG"] = str(outside_config)
            with mock.patch("workflow.merge_queue.resolve_config_path") as mock_resolve:
                mock_resolve.return_value = outside_config
                detection = detect_merge_queue(tmpdir)

                test_result(
                    "detect_merge_queue: configured (env)",
                    detection.state == "configured" and detection.source == "env",
                    f"state={detection.state}, source={detection.source}"
                )

        # Test 4: MERGE_QUEUE_CONFIG set to nonexistent path while default exists
        print("  [Test 4] MERGE_QUEUE_CONFIG missing while default exists → unknown")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            os.environ["MERGE_QUEUE_CONFIG"] = str(tmpdir / "missing.json")
            with mock.patch("workflow.merge_queue.resolve_config_path") as mock_resolve:
                mock_resolve.side_effect = RuntimeError("MERGE_QUEUE_CONFIG is set to /missing/path but no file exists there")
                detection = detect_merge_queue(tmpdir)

                test_result(
                    "detect_merge_queue: unknown (env set but missing)",
                    detection.state == "unknown" and "MERGE_QUEUE_CONFIG" in detection.reason,
                    f"state={detection.state}, reason={detection.reason}"
                )

        # Test 5: --config pointing inside repo (trust-boundary rejection)
        print("  [Test 5] --config inside repo → unknown (trust-boundary)")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            in_repo_config = tmpdir / "internal-queue.json"
            in_repo_config.write_text(json.dumps({"base": "main", "steps": ["echo test"]}))

            os.environ.pop("MERGE_QUEUE_CONFIG", None)
            with mock.patch("workflow.merge_queue.resolve_config_path") as mock_resolve:
                mock_resolve.side_effect = RuntimeError("--config path is inside the repository; must be outside")
                detection = detect_merge_queue(tmpdir, config_flag=str(in_repo_config))

                test_result(
                    "detect_merge_queue: unknown (trust boundary)",
                    detection.state == "unknown" and ("inside" in detection.reason.lower() or "repository" in detection.reason.lower()),
                    f"state={detection.state}, reason={detection.reason}"
                )

        # Test 6: Flat clone (no worktrees/ ancestor), env unset
        print("  [Test 6] Flat clone, no override → absent (layout-not-queue-capable)")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            flat_repo = tmpdir / "flat_repo"
            flat_repo.mkdir()
            os.environ.pop("MERGE_QUEUE_CONFIG", None)

            with mock.patch("workflow.merge_queue.resolve_config_path") as mock_resolve:
                mock_resolve.side_effect = LayoutMismatchError("layout not queue-capable")
                detection = detect_merge_queue(flat_repo)
                test_result(
                    "detect_merge_queue: absent (layout not queue-capable)",
                    detection.state == "absent" and "layout" in detection.reason.lower(),
                    f"state={detection.state}, reason={detection.reason}"
                )

        # Test 7: get_git_common_dir raises → unknown
        print("  [Test 7] get_git_common_dir raises → unknown")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            os.environ.pop("MERGE_QUEUE_CONFIG", None)

            with mock.patch("workflow.merge_queue.resolve_config_path") as mock_resolve:
                mock_resolve.side_effect = RuntimeError("git error")
                detection = detect_merge_queue(tmpdir)
                test_result(
                    "detect_merge_queue: unknown (resolver exception)",
                    detection.state == "unknown",
                    f"state={detection.state}, reason={detection.reason}"
                )

        # Test 8: Config path is a directory → unknown
        print("  [Test 8] Config path is a directory → unknown")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            config_dir = tmpdir / "merge-queue.json"
            config_dir.mkdir()

            os.environ.pop("MERGE_QUEUE_CONFIG", None)
            # When resolver tries to read directory, it fails
            with mock.patch("workflow.merge_queue.resolve_config_path") as mock_resolve:
                mock_resolve.return_value = config_dir
                detection = detect_merge_queue(tmpdir)
                test_result(
                    "detect_merge_queue: unknown (config is directory)",
                    detection.state == "unknown",
                    f"state={detection.state}, reason={detection.reason}"
                )

        # Test 9: Unexpected exception inside detector → unknown, never absent
        print("  [Test 9] Unexpected exception → unknown, never absent")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            os.environ.pop("MERGE_QUEUE_CONFIG", None)

            with mock.patch("workflow.merge_queue.resolve_config_path") as mock_resolve:
                mock_resolve.side_effect = Exception("Unexpected!")
                detection = detect_merge_queue(tmpdir)
                test_result(
                    "detect_merge_queue: unknown on unexpected exception",
                    detection.state == "unknown" and detection.state != "absent",
                    f"state={detection.state}"
                )

        # Test 9b: exception after the resolver succeeds (stat on the resolved path) → unknown
        print("  [Test 9b] Stat of resolved path raises → unknown, never absent")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            os.environ.pop("MERGE_QUEUE_CONFIG", None)
            broken_path = mock.MagicMock()
            broken_path.exists.side_effect = RuntimeError("stat exploded")

            with mock.patch("workflow.merge_queue.resolve_config_path") as mock_resolve:
                mock_resolve.return_value = broken_path
                detection = detect_merge_queue(tmpdir)
                test_result(
                    "detect_merge_queue: unknown when post-resolve check raises",
                    detection.state == "unknown",
                    f"state={detection.state}, reason={detection.reason}"
                )

        # ================================================================
        # SECTION 2: queue_guard base scoping
        # ================================================================
        print()
        print("[Section 2] queue_guard base-scoped refusal")

        # Test 11: configured + PR base == config base → refuse
        print("  [Test 11] configured + base matches → refuse")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            pr_wt = tmpdir / "pr_wt"
            pr_wt.mkdir()

            cfg = tmpdir / "merge-queue.json"
            cfg.write_text(json.dumps({"base": "main", "steps": ["echo test"]}))

            with mock.patch("workflow.merge.detect_merge_queue") as mock_detect:
                mock_detect.return_value = QueueDetection(state="configured", path=str(cfg), source="default")
                with mock.patch("workflow.merge.git.pr_view_json") as mock_pr:
                    mock_pr.return_value = {"baseRefName": "main"}

                    result = queue_guard(123, pr_wt)
                    test_result(
                        "queue_guard: refuse when base matches",
                        result.decision == "refuse" and result.queue_base == "main"
                        and "/queued-merge 123" in result.message,
                        f"decision={result.decision}, queue_base={result.queue_base}"
                    )

        # Test 12: configured + PR base != config base (stacked) → proceed
        print("  [Test 12] configured + stacked PR (base != config base) → proceed")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            pr_wt = tmpdir / "pr_wt"
            pr_wt.mkdir()

            cfg = tmpdir / "merge-queue.json"
            cfg.write_text(json.dumps({"base": "main", "steps": ["echo test"]}))

            with mock.patch("workflow.merge.detect_merge_queue") as mock_detect:
                mock_detect.return_value = QueueDetection(state="configured", path=str(cfg), source="default")
                with mock.patch("workflow.merge.git.pr_view_json") as mock_pr:
                    mock_pr.return_value = {"baseRefName": "feature/parent"}

                    result = queue_guard(456, pr_wt)
                    test_result(
                        "queue_guard: proceed for stacked PR",
                        result.decision == "proceed",
                        f"decision={result.decision}"
                    )

        # Test 13: absent state → proceed without calling pr_view_json
        print("  [Test 13] absent → proceed (no PR lookup)")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            flat_repo = tmpdir / "flat_repo"
            flat_repo.mkdir()

            with mock.patch("workflow.merge.detect_merge_queue") as mock_detect:
                mock_detect.return_value = QueueDetection(state="absent")
                with mock.patch("workflow.merge.git.pr_view_json") as mock_pr:
                    result = queue_guard(789, flat_repo)
                    test_result(
                        "queue_guard: proceed + no pr_view_json call when absent",
                        result.decision == "proceed" and not mock_pr.called,
                        f"decision={result.decision}, pr_view_json called={mock_pr.called}"
                    )

        # Test 14: unknown state + PR base == default branch → refuse
        print("  [Test 14] unknown + default branch match → refuse")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            pr_wt = tmpdir / "pr_wt"
            pr_wt.mkdir()

            with mock.patch("workflow.merge.detect_merge_queue") as mock_detect:
                mock_detect.return_value = QueueDetection(state="unknown", reason="test error")
                with mock.patch("workflow.merge.git.pr_view_json") as mock_pr:
                    mock_pr.return_value = {"baseRefName": "main"}
                    with mock.patch("workflow.merge.git.get_default_branch") as mock_branch:
                        mock_branch.return_value = ("main", None)

                        result = queue_guard(999, pr_wt)
                        test_result(
                            "queue_guard: unknown + matching default → refuse",
                            result.decision == "refuse",
                            f"decision={result.decision}"
                        )

        # Test 15: pr_view_json returns empty dict → refuse (fail closed)
        print("  [Test 15] pr_view_json returns {} → refuse (fail closed)")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            pr_wt = tmpdir / "pr_wt"
            pr_wt.mkdir()

            with mock.patch("workflow.merge.detect_merge_queue") as mock_detect:
                cfg = tmpdir / "merge-queue.json"
                cfg.write_text(json.dumps({"base": "main", "steps": ["echo test"]}))
                mock_detect.return_value = QueueDetection(state="configured", path=str(cfg), source="default")
                with mock.patch("workflow.merge.git.pr_view_json") as mock_pr:
                    mock_pr.return_value = {}

                    result = queue_guard(555, pr_wt)
                    test_result(
                        "queue_guard: refuse when PR base cannot be determined",
                        result.decision == "refuse" and result.pr_base is None and "PR base" in result.message,
                        f"decision={result.decision}, message={result.message}"
                    )

        # ================================================================
        # SECTION 3: plan_merge with queue guard
        # ================================================================
        print()
        print("[Section 3] plan_merge early refusal and exit code 3")

        # Test 16: queue guard refuses → plan.queue.decision == refuse, no push gate
        print("  [Test 16] queue refuse → plan has queue.decision=refuse, no push gate")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            pr_wt = tmpdir / "pr_wt"
            pr_wt.mkdir()

            with mock.patch("workflow.merge.queue_guard") as mock_guard:
                mock_guard.return_value = QueueGuardDecision(
                    decision="refuse",
                    detection=QueueDetection(state="configured"),
                    pr_base="main",
                    queue_base="main",
                    message="Use /queued-merge"
                )
                with mock.patch("workflow.merge._resolve_pr_from_worktree") as mock_resolve:
                    mock_resolve.return_value = (42, "feature", str(pr_wt))
                    with mock.patch("workflow.merge._run_push_gate") as mock_gate:
                        plan, err = plan_merge(str(pr_wt))

                        test_result(
                            "plan_merge: queue.decision=refuse on guard refusal",
                            plan and plan.queue.get("decision") == "refuse" and not mock_gate.called,
                            f"decision={plan.queue.get('decision') if plan else None}, push_gate called={mock_gate.called}"
                        )

        # Test 17: plan_merge respects --cwd parameter
        print("  [Test 17] plan_merge --cwd resolves from different directory")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            pr_wt = tmpdir / "pr_wt"
            pr_wt.mkdir()
            unrelated = tmpdir / "unrelated"
            unrelated.mkdir()

            with mock.patch("workflow.merge.queue_guard") as mock_guard:
                mock_guard.return_value = QueueGuardDecision(
                    decision="proceed",
                    detection=QueueDetection(state="absent"),
                    pr_base="other",
                    queue_base=None,
                    message=""
                )
                with mock.patch("workflow.merge._resolve_pr_from_worktree") as mock_resolve:
                    mock_resolve.return_value = (43, "feature", str(pr_wt))
                    with mock.patch("workflow.merge._run_push_gate") as mock_gate:
                        mock_gate.return_value = []

                        plan, err = plan_merge(str(pr_wt), cwd=unrelated)
                        # Check that target_worktree is set correctly
                        test_result(
                            "plan_merge: respects cwd parameter",
                            plan is not None and str(plan.target_worktree) == str(pr_wt.resolve()),
                            f"target_worktree={plan.target_worktree if plan else None}"
                        )

        # ================================================================
        # SECTION 4: apply_merge authoritative gate
        # ================================================================
        print()
        print("[Section 4] apply_merge authoritative gate re-checks queue live")

        # Test 18: edited plan.queue.decision (proceed) while queue refuses → refuse at apply
        print("  [Test 18] edited plan.queue → refused at apply, no merge gates run")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            pr_wt = tmpdir / "pr_wt"
            pr_wt.mkdir()

            plan = MergePlan(
                pr_number=100,
                head_ref="feature",
                target_worktree=str(pr_wt),
                blocking_failures=[],
                queue={"decision": "proceed"}  # Edited to lie
            )

            plan_json = json.dumps(plan.to_dict())

            with mock.patch("workflow.merge.queue_guard") as mock_guard:
                mock_guard.return_value = QueueGuardDecision(
                    decision="refuse",
                    detection=QueueDetection(state="configured"),
                    pr_base="main",
                    queue_base="main",
                    message="Use /queued-merge"
                )
                with mock.patch("workflow.merge._check_just_merge") as mock_just:
                    with mock.patch("workflow.merge._run_merge_gate_checks") as mock_gate_checks:
                        with mock.patch("workflow.merge._run_gh_pr_merge") as mock_merge:
                            merge_result, err = apply_merge(plan_json, cwd=None)

                            test_result(
                                "apply_merge: refuses despite edited plan.queue",
                                not merge_result.pr_merged and err is not None
                                and "/queued-merge" in str(merge_result.error)
                                and not (mock_just.called or mock_gate_checks.called or mock_merge.called),
                                f"pr_merged={merge_result.pr_merged}, error={merge_result.error}, "
                                f"just={mock_just.called}, gate={mock_gate_checks.called}, merge={mock_merge.called}"
                            )

        # Test 20: gh pr merge runs in the target worktree even when the caller's cwd is elsewhere
        print("  [Test 20] apply_merge runs _run_gh_pr_merge in the target worktree, not the caller's cwd")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            flat_repo = tmpdir / "flat_repo"
            flat_repo.mkdir()
            unrelated = tmpdir / "unrelated"
            unrelated.mkdir()

            plan = MergePlan(
                pr_number=102,
                head_ref="feature",
                target_worktree=str(flat_repo),
                blocking_failures=[],
                queue={"decision": "proceed"}
            )

            plan_json = json.dumps(plan.to_dict())

            with mock.patch("workflow.merge.queue_guard") as mock_guard:
                mock_guard.return_value = QueueGuardDecision(
                    decision="proceed",
                    detection=QueueDetection(state="absent"),
                    pr_base="main",
                    queue_base=None,
                    message=""
                )
                with mock.patch("workflow.merge._check_just_merge") as mock_just:
                    with mock.patch("workflow.merge._run_merge_gate_checks") as mock_gate_checks:
                        with mock.patch("workflow.merge._run_gh_pr_merge") as mock_merge:
                            mock_just.return_value = False
                            mock_gate_checks.return_value = (True, False, "")
                            mock_merge.return_value = (True, "gh pr merge")

                            result = apply_merge(plan_json, cwd=unrelated)

                            # Verify that _run_gh_pr_merge was called with target worktree's cwd
                            if mock_merge.called:
                                call_args = mock_merge.call_args
                                # Get the cwd argument (could be positional or keyword)
                                cwd_arg = None
                                if len(call_args[0]) > 1:
                                    cwd_arg = call_args[0][1]
                                else:
                                    cwd_arg = call_args[1].get("cwd")

                                matches = (cwd_arg == Path(flat_repo)) or (str(cwd_arg) == str(flat_repo))
                                test_result(
                                    "apply_merge: _run_gh_pr_merge gets target worktree cwd",
                                    matches,
                                    f"cwd={cwd_arg}"
                                )
                            else:
                                test_result(
                                    "apply_merge: _run_gh_pr_merge called",
                                    False,
                                    "Not called"
                                )

        # ================================================================
        # SECTION 5: Regression and docs
        # ================================================================
        print()
        print("[Section 5] Regression tests")

        # Test 21: real worktrees/ layout, resolved from a cwd that is not the process cwd
        print("  [Test 21] real worktrees/ layout: resolver + detector honor cwd, not process cwd")

        def _git(*args, cwd):
            subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir).resolve()
            container = tmpdir / "repo"
            main_wt = container / "worktrees" / "main"
            main_wt.mkdir(parents=True)
            _git("init", "-q", "-b", "main", cwd=main_wt)
            _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init", cwd=main_wt)
            feature_wt = container / "worktrees" / "feature"
            _git("worktree", "add", "-q", "-b", "feature", str(feature_wt), cwd=main_wt)
            os.environ.pop("MERGE_QUEUE_CONFIG", None)

            # Process cwd stays in this repo's checkout (a different git repo entirely)
            detection = detect_merge_queue(feature_wt)
            test_result(
                "detect_merge_queue (real repo): absent without config",
                detection.state == "absent",
                f"state={detection.state}, reason={detection.reason}"
            )

            (container / "merge-queue.json").write_text(json.dumps({"base": "main", "steps": ["echo test"]}))
            resolved = resolve_config_path(cwd=feature_wt)
            detection = detect_merge_queue(feature_wt)
            test_result(
                "resolve_config_path (real repo): container config from linked worktree cwd",
                Path(resolved).resolve() == (container / "merge-queue.json").resolve(),
                f"resolved={resolved}"
            )
            test_result(
                "detect_merge_queue (real repo): configured from linked worktree cwd",
                detection.state == "configured" and detection.source == "default",
                f"state={detection.state}, source={detection.source}"
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            flat_repo = Path(tmpdir).resolve() / "flat"
            flat_repo.mkdir()
            _git("init", "-q", cwd=flat_repo)
            os.environ.pop("MERGE_QUEUE_CONFIG", None)
            raised = None
            try:
                resolve_config_path(cwd=flat_repo)
            except LayoutMismatchError as e:
                raised = e
            detection = detect_merge_queue(flat_repo)
            test_result(
                "resolve_config_path (real repo): flat clone raises LayoutMismatchError",
                raised is not None,
                f"raised={raised!r}"
            )
            test_result(
                "detect_merge_queue (real repo): flat clone → absent (layout-not-queue-capable)",
                detection.state == "absent" and detection.reason == "layout-not-queue-capable",
                f"state={detection.state}, reason={detection.reason}"
            )

        # Bare-repo layout: <container>/.bare + <container>/.git gitfile + <container>/worktrees/*
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir).resolve()
            container = tmpdir / "repo"
            seed = tmpdir / "seed"
            seed.mkdir()
            _git("init", "-q", "-b", "main", cwd=seed)
            _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init", cwd=seed)
            container.mkdir()
            _git("clone", "-q", "--bare", str(seed), str(container / ".bare"), cwd=tmpdir)
            (container / ".git").write_text("gitdir: ./.bare\n")
            feature_wt = container / "worktrees" / "feature"
            _git("worktree", "add", "-q", "-b", "feature", str(feature_wt), "main", cwd=container)
            os.environ.pop("MERGE_QUEUE_CONFIG", None)

            resolved = resolve_config_path(cwd=feature_wt)
            test_result(
                "resolve_config_path (bare layout): container config from linked worktree cwd",
                Path(resolved).resolve() == (container / "merge-queue.json").resolve(),
                f"resolved={resolved}"
            )

        # Test 22: LayoutMismatchError is defined and raised
        print("  [Test 22] LayoutMismatchError exists as a RuntimeError subclass")
        try:
            raise LayoutMismatchError("test")
        except RuntimeError:
            test_result("LayoutMismatchError: is RuntimeError", True)
        except Exception as e:
            test_result("LayoutMismatchError: is RuntimeError", False, f"Wrong type: {type(e)}")

        h.summarize_and_exit()

    finally:
        # Cleanup
        if os.path.exists(fake_home):
            shutil.rmtree(fake_home, ignore_errors=True)
        os.environ.pop("MERGE_QUEUE_CONFIG", None)
