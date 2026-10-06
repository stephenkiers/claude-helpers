#!/usr/bin/env python3
"""
Test suite for queue-proof tree equality skip logic.

Run with: python3 tests/test_workflow_validation_queue_proof.py
"""

import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.validation import queue_proof, Proved, CannotProve
from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("WORKFLOW VALIDATION QUEUE PROOF TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Missing PR number returns CannotProve")

    with tempfile.TemporaryDirectory() as tmpdir:
        main_wt = Path(tmpdir) / "main"
        main_wt.mkdir()

        result = queue_proof(None, 1000.0, main_wt)
        test_result(
            "queue_proof(None, started_at, wt) returns CannotProve",
            isinstance(result, CannotProve)
        )

    print()
    print("[Section 2] Missing queue_started_at returns CannotProve")

    with tempfile.TemporaryDirectory() as tmpdir:
        main_wt = Path(tmpdir) / "main"
        main_wt.mkdir()

        result = queue_proof(123, None, main_wt)
        test_result(
            "queue_proof(pr, None, wt) returns CannotProve",
            isinstance(result, CannotProve)
        )

    print()
    print("[Section 3] Both None returns CannotProve")

    with tempfile.TemporaryDirectory() as tmpdir:
        main_wt = Path(tmpdir) / "main"
        main_wt.mkdir()

        result = queue_proof(None, None, main_wt)
        test_result(
            "queue_proof(None, None, wt) returns CannotProve",
            isinstance(result, CannotProve)
        )

    print()
    print("[Section 4] Missing result file returns CannotProve")

    with tempfile.TemporaryDirectory() as tmpdir:
        main_wt = Path(tmpdir) / "main"
        main_wt.mkdir()

        # Mock read_pr_result to return None (file not found)
        with mock.patch("workflow.validation.read_pr_result") as mock_read:
            mock_read.return_value = None

            result = queue_proof(123, 1000.0, main_wt)
            test_result(
                "queue_proof with missing result file returns CannotProve",
                isinstance(result, CannotProve)
            )

    print()
    print("[Section 5] PR number mismatch returns CannotProve")

    with tempfile.TemporaryDirectory() as tmpdir:
        main_wt = Path(tmpdir) / "main"
        main_wt.mkdir()

        from workflow.merge_queue import MergeResult, MergeOutcome

        # Create a MergeResult with different PR number
        merge_result = MergeResult(
            pr=999,  # Different from requested 123
            outcome=MergeOutcome.MERGED,
            timestamp=1000.0,
            tested_sha="a" * 40,
            merge_base="b" * 40,
        )

        with mock.patch("workflow.validation.read_pr_result") as mock_read:
            mock_read.return_value = (merge_result, 1000.0)

            result = queue_proof(123, 1000.0, main_wt)
            test_result(
                "queue_proof with PR number mismatch returns CannotProve",
                isinstance(result, CannotProve)
            )

    print()
    print("[Section 6] Non-MERGED outcome returns CannotProve")

    with tempfile.TemporaryDirectory() as tmpdir:
        main_wt = Path(tmpdir) / "main"
        main_wt.mkdir()

        from workflow.merge_queue import MergeResult, MergeOutcome

        merge_result = MergeResult(
            pr=123,
            outcome=MergeOutcome.QUEUED,  # Not MERGED
            timestamp=1000.0,
            tested_sha="a" * 40,
            merge_base="b" * 40,
        )

        with mock.patch("workflow.validation.read_pr_result") as mock_read:
            mock_read.return_value = (merge_result, 1000.0)

            result = queue_proof(123, 1000.0, main_wt)
            test_result(
                "queue_proof with non-MERGED outcome returns CannotProve",
                isinstance(result, CannotProve)
            )

    print()
    print("[Section 7] Stale timestamp returns CannotProve")

    with tempfile.TemporaryDirectory() as tmpdir:
        main_wt = Path(tmpdir) / "main"
        main_wt.mkdir()

        from workflow.merge_queue import MergeResult, MergeOutcome

        merge_result = MergeResult(
            pr=123,
            outcome=MergeOutcome.MERGED,
            timestamp=500.0,  # Before queue_started_at (1000.0)
            tested_sha="a" * 40,
            merge_base="b" * 40,
        )

        with mock.patch("workflow.validation.read_pr_result") as mock_read:
            mock_read.return_value = (merge_result, 500.0)

            result = queue_proof(123, 1000.0, main_wt)
            test_result(
                "queue_proof with stale timestamp returns CannotProve",
                isinstance(result, CannotProve)
            )

    print()
    print("[Section 8] Invalid SHA (not 40-hex) returns CannotProve")

    with tempfile.TemporaryDirectory() as tmpdir:
        main_wt = Path(tmpdir) / "main"
        main_wt.mkdir()

        from workflow.merge_queue import MergeResult, MergeOutcome

        merge_result = MergeResult(
            pr=123,
            outcome=MergeOutcome.MERGED,
            timestamp=1500.0,
            tested_sha="not-a-valid-sha",  # Invalid
            merge_base="b" * 40,
        )

        with mock.patch("workflow.validation.read_pr_result") as mock_read:
            mock_read.return_value = (merge_result, 1500.0)

            result = queue_proof(123, 1000.0, main_wt)
            test_result(
                "queue_proof with invalid SHA returns CannotProve",
                isinstance(result, CannotProve)
            )

    print()
    print("[Section 9] Tree_of returns None returns CannotProve")

    with tempfile.TemporaryDirectory() as tmpdir:
        main_wt = Path(tmpdir) / "main"
        main_wt.mkdir()

        from workflow.merge_queue import MergeResult, MergeOutcome

        merge_result = MergeResult(
            pr=123,
            outcome=MergeOutcome.MERGED,
            timestamp=1500.0,
            tested_sha="a" * 40,
            merge_base="b" * 40,
        )

        with mock.patch("workflow.validation.read_pr_result") as mock_read:
            with mock.patch("workflow.validation.tree_of") as mock_tree:
                mock_read.return_value = (merge_result, 1500.0)
                mock_tree.return_value = None  # probe failed

                result = queue_proof(123, 1000.0, main_wt)
                test_result(
                    "queue_proof with tree_of returning None returns CannotProve",
                    isinstance(result, CannotProve)
                )

    print()
    print("[Section 10] Differing trees returns CannotProve")

    with tempfile.TemporaryDirectory() as tmpdir:
        main_wt = Path(tmpdir) / "main"
        main_wt.mkdir()

        from workflow.merge_queue import MergeResult, MergeOutcome

        merge_result = MergeResult(
            pr=123,
            outcome=MergeOutcome.MERGED,
            timestamp=1500.0,
            tested_sha="a" * 40,
            merge_base="b" * 40,
        )

        with mock.patch("workflow.validation.read_pr_result") as mock_read:
            with mock.patch("workflow.validation.tree_of") as mock_tree:
                mock_read.return_value = (merge_result, 1500.0)
                # First call returns tested tree, second returns HEAD tree
                mock_tree.side_effect = ["tree_a", "tree_b"]

                result = queue_proof(123, 1000.0, main_wt)
                test_result(
                    "queue_proof with differing trees returns CannotProve",
                    isinstance(result, CannotProve)
                )

    print()
    print("[Section 11] Exception in queue_proof returns CannotProve")

    with tempfile.TemporaryDirectory() as tmpdir:
        main_wt = Path(tmpdir) / "main"
        main_wt.mkdir()

        with mock.patch("workflow.validation.read_pr_result") as mock_read:
            mock_read.side_effect = RuntimeError("Unexpected error")

            result = queue_proof(123, 1000.0, main_wt)
            test_result(
                "queue_proof with unexpected exception returns CannotProve",
                isinstance(result, CannotProve)
            )

    print()
    print("[Section 12] Equal trees and MERGED outcome gives Proved")

    with tempfile.TemporaryDirectory() as tmpdir:
        main_wt = Path(tmpdir) / "main"
        main_wt.mkdir()

        from workflow.merge_queue import MergeResult, MergeOutcome

        merge_result = MergeResult(
            pr=123,
            outcome=MergeOutcome.MERGED,
            timestamp=1500.0,
            tested_sha="a" * 40,
            merge_base="b" * 40,
        )

        with mock.patch("workflow.validation.read_pr_result") as mock_read:
            with mock.patch("workflow.validation.tree_of") as mock_tree:
                with mock.patch("workflow.validation.resolve_config_path") as mock_config:
                    with mock.patch("workflow.validation.load_and_validate_config") as mock_load:
                        mock_read.return_value = (merge_result, 1500.0)
                        # Both calls return same tree
                        mock_tree.return_value = "same_tree_sha"
                        mock_config.return_value = Path("/fake/queue.json")
                        mock_load.return_value = {"steps": ["lint", "test"]}

                        result = queue_proof(123, 1000.0, main_wt)
                        test_result(
                            "queue_proof with equal trees gives Proved",
                            isinstance(result, Proved)
                        )
                        if isinstance(result, Proved):
                            test_result(
                                "Proved contains tree SHA",
                                result.tree == "same_tree_sha"
                            )
                            test_result(
                                "Proved contains queue steps",
                                result.steps == ["lint", "test"]
                            )

    h.summarize_and_exit()
