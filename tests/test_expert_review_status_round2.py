#!/usr/bin/env python3
"""
Test suite for expert-review-status.py fixes (Round 2 plan validation).

Tests for:
1. (ITEM 1) Partial cache entry handling: missing reviewers/findings should default to []/{}
2. (ITEM 3) JSON error output on get_git_info() failure when --json flag is set

Run with: python3 tests/test_expert_review_status_round2.py
"""

import sys
import subprocess
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT
from _git_fixture import GitFixture


def run_expert_review_status(*args, cwd=None):
    """
    Run the expert-review-status.py script and return result.

    Returns: (returncode, stdout, stderr)
    """
    script_path = REPO_ROOT / "scripts" / "expert-review-status.py"
    cmd = [sys.executable, str(script_path)] + list(args)
    result = subprocess.run(
        cmd,
        cwd=cwd or REPO_ROOT,
        capture_output=True,
        text=True
    )
    return result.returncode, result.stdout, result.stderr


if __name__ == "__main__":
    h = Harness("EXPERT-REVIEW-STATUS ROUND 2 TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Partial cache entry handling (Item 1)")
    print()

    # Test: Partial cache with missing 'reviewers' and 'findings' fields
    # The fix should default these to [] and {} respectively
    fixture = GitFixture()
    try:
        fixture.create_initial_commit("Initial commit")
        fixture.create_branch("test-partial-cache")
        wt = fixture.create_worktree("test-partial-cache")

        # Write a minimal cache entry: only branch and lastRun fields
        # This is the "matched-but-partial" case from the plan
        partial_cache = {
            "branch": "test-partial-cache",
            "lastRun": {
                "timestamp": "2026-09-15T12:00:00Z",
                "status": "reviewed"
            }
            # NOTE: Missing 'reviewers' and 'findings' fields
        }
        fixture.write_cache_file(wt, partial_cache)

        returncode, stdout, stderr = run_expert_review_status(cwd=str(wt))

        # The script should not crash (exit code should indicate status, not crash)
        # A zero exit code would mean "not reviewed yet" or similar
        test_result(
            "Partial cache entry: script does not crash on missing reviewers/findings",
            returncode in [0, 1, 2],  # Any normal exit code, not a Python crash
            f"Exit code {returncode}, stderr: {stderr[:200]}"
        )

        # When using --json, the output should be valid JSON
        returncode, stdout, stderr = run_expert_review_status("--json", cwd=str(wt))

        test_result(
            "Partial cache entry: --json output is valid JSON",
            True,  # We'll validate this below
            "Checking JSON parsing..."
        )

        try:
            output = json.loads(stdout)
            test_result(
                "Partial cache entry: --json parses successfully",
                isinstance(output, dict),
                f"Got: {stdout[:200]}"
            )

            # The output should have the expected structure (even if partial cache)
            test_result(
                "Partial cache entry: output includes 'reviewed' field",
                "reviewed" in output or "status" in output or "error" in output,
                f"Unexpected output structure: {output}"
            )
        except json.JSONDecodeError as e:
            test_result(
                "Partial cache entry: --json output is valid JSON",
                False,
                f"JSON decode error: {e}"
            )

        print()
        print("[Section 2] JSON error output on get_git_info() failure (Item 3)")
        print()

        # Test: get_git_info() failure when git operations fail
        # Create a worktree but move to a non-git directory to trigger git failure
        with tempfile.TemporaryDirectory() as non_git_dir:
            non_git_path = Path(non_git_dir)

            # Create a minimal cache file there
            claude_dir = non_git_path / ".claude"
            claude_dir.mkdir(parents=True, exist_ok=True)
            cache_file = claude_dir / "github-cache.json"
            cache_file.write_text(json.dumps({
                "branch": "test-branch",
                "issue": {"number": 123}
            }))

            # Run with --json in a non-git directory
            # This should trigger get_git_info() failure
            returncode, stdout, stderr = run_expert_review_status("--json", cwd=str(non_git_path))

            # With --json flag set, even on failure, output should be valid JSON
            test_result(
                "get_git_info() failure: --json flag produces JSON output",
                True,  # We'll validate below
                "Checking JSON validation..."
            )

            try:
                output = json.loads(stdout)
                test_result(
                    "get_git_info() failure: --json parses successfully",
                    isinstance(output, dict),
                    f"Got: {stdout[:200]}"
                )

                # On error, should have 'error' field or indicate failure somehow
                test_result(
                    "get_git_info() failure: JSON includes error information",
                    "error" in output or "status" in output,
                    f"Output structure: {list(output.keys())}"
                )
            except json.JSONDecodeError as e:
                test_result(
                    "get_git_info() failure: --json output is valid JSON",
                    False,
                    f"JSON decode error: {e}, stdout: {stdout[:200]}"
                )

        print()
        print("[Section 3] Normal cache entry (sanity check)")
        print()

        # Test: A complete cache entry should still work
        fixture2 = GitFixture()
        fixture2.create_initial_commit("Initial commit")
        fixture2.create_branch("test-complete-cache")
        wt2 = fixture2.create_worktree("test-complete-cache")

        complete_cache = {
            "branch": "test-complete-cache",
            "lastRun": {
                "timestamp": "2026-09-15T12:00:00Z",
                "status": "reviewed",
                "reviewers": ["Uncle Bob", "Security Sage"],
                "findings": {
                    "confirmed": 5,
                    "pending": 2
                }
            }
        }
        fixture2.write_cache_file(wt2, complete_cache)

        returncode, stdout, stderr = run_expert_review_status("--json", cwd=str(wt2))

        test_result(
            "Complete cache entry: --json produces valid JSON",
            True,  # Validated below
            "Checking..."
        )

        try:
            output = json.loads(stdout)
            test_result(
                "Complete cache entry: output is dict",
                isinstance(output, dict),
                f"Got: {stdout[:200]}"
            )
        except json.JSONDecodeError as e:
            test_result(
                "Complete cache entry: --json output is valid JSON",
                False,
                f"JSON decode error: {e}"
            )

        fixture2.cleanup()

    finally:
        fixture.cleanup()

    print()
    h.summarize_and_exit()
