#!/usr/bin/env python3
"""
Test suite for plan_merge() queue routing decision (PR #232).

Tests the new queue.decision="route" path in plan_merge():
- Queue routing when conditions are met (PR base == queue base)
- Push gate runs before early return for queue-routed PRs
- blocking_failures surface in plan.queue for queue-routed route

Run with: python3 tests/test_queue_routing_plan_merge.py
"""

import sys
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.merge import (
    plan_merge,
    queue_guard,
    QueueDetection,
    QueueGuardDecision,
)
from _test_harness import Harness


if __name__ == "__main__":
    # Redirect HOME so merge locks go to a temp dir
    fake_home = tempfile.mkdtemp()
    os.environ["HOME"] = fake_home

    # Clear queue config env var
    os.environ.pop("MERGE_QUEUE_CONFIG", None)

    try:
        h = Harness("QUEUE ROUTING PLAN_MERGE TEST SUITE (PR #232)")
        test_result = h.test_result

        print()
        print("[Section 1] Queue routing decision path")

        # ================================================================
        # Test 1: queue_guard refuses when PR targets queue base
        # ================================================================
        print("  [Test 1] queue_guard decision='refuse' when PR base == queue base")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            pr_wt = tmpdir / "pr_wt"
            pr_wt.mkdir()

            with mock.patch("workflow.merge.queue_guard") as mock_guard:
                mock_guard.return_value = QueueGuardDecision(
                    decision="refuse",  # Queue-routed PRs get 'refuse'; routing happens in shell layer
                    detection=QueueDetection(state="configured"),
                    pr_base="main",
                    queue_base="main",
                    message="Use /queued-merge"
                )
                with mock.patch("workflow.merge._resolve_pr_from_worktree") as mock_resolve:
                    mock_resolve.return_value = (42, "feature", str(pr_wt))
                    with mock.patch("workflow.merge._run_push_gate") as mock_gate:
                        mock_gate.return_value = []  # No blocking failures
                        plan, err = plan_merge(str(pr_wt))

                        test_result(
                            "plan_merge: queue.decision='refuse' when targeting queue base",
                            plan and plan.queue.get("decision") == "refuse",
                            f"decision={plan.queue.get('decision') if plan else None}"
                        )

        print()

        # ================================================================
        # Test 2: Push gate runs before early return for refused PRs
        # ================================================================
        print("  [Test 2] Push gate called before early return for queue-bound PR")
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
                        mock_gate.return_value = []  # No blocking failures
                        plan, err = plan_merge(str(pr_wt))

                        test_result(
                            "push gate called for queue-routed PR",
                            mock_gate.called,
                            f"push_gate.called={mock_gate.called} (should be True for queue route)"
                        )

        print()

        # ================================================================
        # Test 3: Push gate failures surface in plan for refused PRs
        # ================================================================
        print("  [Test 3] Push gate blocking_failures captured in plan for queue-bound PR")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            pr_wt = tmpdir / "pr_wt"
            pr_wt.mkdir()

            expected_failures = ["uncommitted changes", "unpushed commits"]

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
                        # Simulate push gate failures
                        mock_gate.return_value = expected_failures
                        plan, err = plan_merge(str(pr_wt))

                        # Check either plan.blocking_failures or plan.queue.blocking_failures
                        blocking_failures = plan.blocking_failures if plan else []
                        queue_blocking = plan.queue.get("blocking_failures", []) if plan else []
                        captured = blocking_failures or queue_blocking

                        has_failures = len(captured) > 0
                        test_result(
                            "push gate failures captured in plan for queue-routed PR",
                            has_failures,
                            f"blocking_failures={captured}, push_gate returned={expected_failures}"
                        )

        print()

        # ================================================================
        # Test 4: Push gate is called BEFORE queue-guard check
        # ================================================================
        print("  [Test 4] Push gate runs before queue-guard decision")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            pr_wt = tmpdir / "pr_wt"
            pr_wt.mkdir()

            call_order = []

            def mock_gate_fn(*args, **kwargs):
                call_order.append("push_gate")
                return []

            def mock_guard_fn(*args, **kwargs):
                call_order.append("queue_guard")
                return QueueGuardDecision(
                    decision="refuse",
                    detection=QueueDetection(state="configured"),
                    pr_base="main",
                    queue_base="main",
                    message="Use /queued-merge"
                )

            with mock.patch("workflow.merge.queue_guard", side_effect=mock_guard_fn):
                with mock.patch("workflow.merge._resolve_pr_from_worktree") as mock_resolve:
                    mock_resolve.return_value = (42, "feature", str(pr_wt))
                    with mock.patch("workflow.merge._run_push_gate", side_effect=mock_gate_fn):
                        plan, err = plan_merge(str(pr_wt))

                        # Verify push gate was called
                        push_gate_called = "push_gate" in call_order
                        test_result(
                            "push gate was invoked",
                            push_gate_called,
                            f"call_order={call_order}"
                        )

        print()

        # ================================================================
        # Test 5: Queue refusal returns early with plan containing refuse decision
        # ================================================================
        print("  [Test 5] plan_merge returns plan with queue.decision='refuse'")
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
                        mock_gate.return_value = []

                        plan, err = plan_merge(str(pr_wt))

                        has_plan = plan is not None
                        has_queue_decision = plan and plan.queue.get("decision") == "refuse" if has_plan else False
                        test_result(
                            "plan_merge returns plan with queue refuse decision",
                            has_plan and has_queue_decision,
                            f"plan={plan}, queue.decision={plan.queue.get('decision') if plan else None}"
                        )

        print()

        # ================================================================
        # Test 6: Refuse decision still runs push gate first
        # ================================================================
        print("  [Test 6] Push gate also runs for queue.decision='refuse'")
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
                        mock_gate.return_value = []

                        plan, err = plan_merge(str(pr_wt))

                        test_result(
                            "push gate called for queue refuse too",
                            mock_gate.called,
                            f"push_gate.called={mock_gate.called}"
                        )

        print()

        h.summarize_and_exit()

    finally:
        import shutil
        shutil.rmtree(fake_home, ignore_errors=True)
