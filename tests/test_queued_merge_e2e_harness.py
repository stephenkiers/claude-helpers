#!/usr/bin/env python3
"""
End-to-end harness test for /queued-merge command.

Exercises key Phase 1 fixes:
- H1: enqueue token fed to argparse
- H2: outcome casing mismatch
"""

import sys
import json
import tempfile
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _test_harness import Harness
from workflow.merge_queue import main

h = Harness("QUEUED-MERGE END-TO-END HARNESS TEST")
test_result = h.test_result


# ============================================================================
# Test 1: main() with "enqueue" subcommand dispatches correctly (H1)
# ============================================================================
print("[Test 1] H1: enqueue token fed to argparse — dispatch works")
try:
    # The fix for H1 ensures that when "enqueue" is passed as argv[0],
    # it gets stripped before passing to argparse. This test verifies
    # that we don't get a SystemExit from argparse trying to parse "enqueue"
    # as an unknown argument.
    with patch('workflow.merge_queue.git.get_current_branch') as mock_branch:
        mock_branch.return_value = "feature-branch"

        # Call main with "enqueue" as first arg
        result = main(["enqueue", "--pr", "999", "--config", "/nonexistent.json"])

        # The fix allows us to get past argparse to config loading.
        # We expect exit code 1 (config loading) or 3 (validation)
        test_result(
            "enqueue subcommand routes to _cmd_enqueue without argparse error",
            result in (1, 3),
            f"Got exit code {result}"
        )
except SystemExit as e:
    test_result("enqueue subcommand routes to _cmd_enqueue without argparse error", False, f"SystemExit from argparse: {e}")
except Exception as e:
    test_result("enqueue subcommand routes to _cmd_enqueue without argparse error", False, str(e))


# ============================================================================
# Test 2: result.json outcome casing (H2)
# ============================================================================
print("[Test 2] H2: outcome casing — lowercase enum values in JSON")
try:
    from workflow.merge_queue import MergeResult, MergeOutcome, write_result_json

    with tempfile.TemporaryDirectory() as tmpdir:
        # Set up a minimal git repo for this test
        subprocess.run(["git", "init"], cwd=tmpdir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmpdir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmpdir, check=True, capture_output=True)

        # Create initial commit so we have a SHA to use
        test_file = Path(tmpdir) / "test.txt"
        test_file.write_text("test")
        subprocess.run(["git", "add", "test.txt"], cwd=tmpdir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "test"], cwd=tmpdir, check=True, capture_output=True)

        # Get the commit SHA
        result_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            check=True
        ).stdout.strip()

        # Change to the repo dir so write_result_json can find .git
        old_cwd = os.getcwd()
        os.chdir(tmpdir)

        # Create and write a result with MERGED outcome
        result = MergeResult(
            outcome=MergeOutcome.MERGED,
            pr=1,
            branch="feature-branch",
            worktree=tmpdir,
            orig_head=result_sha,
            tested_sha=result_sha,
        )
        write_result_json(result, Path(tmpdir))

        # Read back and verify lowercase casing
        result_json_path = Path(tmpdir) / ".git" / "merge-queue" / "result.json"
        result_json = json.loads(result_json_path.read_text())

        # H2 fix: outcome should be lowercase "merged", not uppercase "MERGED"
        test_result(
            "result.json outcome is lowercase 'merged'",
            result_json.get("outcome") == "merged",
            f"Got '{result_json.get('outcome')}'"
        )

        os.chdir(old_cwd)
except Exception as e:
    test_result("result.json outcome is lowercase 'merged'", False, str(e))


# ============================================================================
# Test 3: MergeOutcome enum values are lowercase
# ============================================================================
print("[Test 3] H2: MergeOutcome enum — all values are lowercase")
try:
    from workflow.merge_queue import MergeOutcome

    expected_values = {
        "MERGED": "merged",
        "KICKBACK": "kickback",
        "PUSHED_NOT_MERGED": "pushed_not_merged",
        "REFUSED": "refused",
        "INTERNAL_ERROR": "internal_error",
    }

    all_correct = True
    for enum_name, expected_value in expected_values.items():
        actual_value = getattr(MergeOutcome, enum_name).value
        if actual_value != expected_value:
            all_correct = False
            break

    test_result(
        "All MergeOutcome enum values are lowercase",
        all_correct,
        "Some enum values are not lowercase"
    )
except Exception as e:
    test_result("All MergeOutcome enum values are lowercase", False, str(e))


# ============================================================================
# Summary
# ============================================================================
print()
h.summarize_and_exit()
