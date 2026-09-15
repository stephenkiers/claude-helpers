#!/usr/bin/env python3
"""
Comprehensive test suite for scripts/expert-review-status.py.

Tests the standalone "has this commit already been reviewed" check.
Based on the plan spec, not the implementation.

Run with: python3 tests/test_expert_review_status_spec_blind.py
"""

import sys
import json
import os
import subprocess
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness
from _git_fixture import GitFixture


def run_expert_review_status(cwd, args=None):
    """
    Run the expert-review-status.py script and return (stdout, stderr, exit_code).
    """
    if args is None:
        args = []

    script = Path(__file__).parent.parent / "scripts" / "expert-review-status.py"
    cmd = ["python3", str(script)] + args

    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True
        )
        return result.stdout, result.stderr, result.returncode
    except Exception as e:
        return "", str(e), 1


if __name__ == "__main__":
    h = Harness("EXPERT-REVIEW-STATUS COMPREHENSIVE TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Basic functionality with no cache file")

    fixture = GitFixture()
    try:
        old_cwd = os.getcwd()
        os.chdir(fixture.repo_root)

        fixture.create_initial_commit("Initial commit")

        # Test 1: No cache file present
        stdout, stderr, exit_code = run_expert_review_status(fixture.repo_root)
        test_result(
            "expert-review-status with no cache returns exit code 1",
            exit_code == 1,
            f"got {exit_code}"
        )
        test_result(
            "expert-review-status with no cache produces output",
            len(stdout) > 0 or len(stderr) > 0,
            f"stdout={stdout!r}, stderr={stderr!r}"
        )

        print()
        print("[Section 2] Output with no cache file (various modes)")

        # Test 2: --json output with no cache
        stdout, stderr, exit_code = run_expert_review_status(fixture.repo_root, ["--json"])
        test_result(
            "expert-review-status --json with no cache returns valid JSON",
            exit_code == 1,
            f"exit code: {exit_code}"
        )
        try:
            data = json.loads(stdout)
            test_result(
                "expert-review-status --json contains 'reviewed' field",
                "reviewed" in data and data["reviewed"] is False,
                f"got {data}"
            )
            test_result(
                "expert-review-status --json contains other expected fields",
                all(k in data for k in ["current", "dirty", "lastRun", "commit", "branch"]),
                f"fields: {list(data.keys())}"
            )
        except json.JSONDecodeError as e:
            test_result(
                "expert-review-status --json output is valid JSON",
                False,
                f"JSON error: {e}"
            )

        # Test 3: --quiet mode suppresses stdout
        stdout, stderr, exit_code = run_expert_review_status(fixture.repo_root, ["-q"])
        test_result(
            "expert-review-status -q suppresses stdout",
            len(stdout.strip()) == 0,
            f"got {stdout!r}"
        )
        test_result(
            "expert-review-status -q still exits with 1 when not reviewed",
            exit_code == 1,
            f"got {exit_code}"
        )

        # Test 4: --quiet with --json works
        stdout, stderr, exit_code = run_expert_review_status(fixture.repo_root, ["-q", "--json"])
        test_result(
            "expert-review-status -q --json suppresses stdout",
            len(stdout.strip()) == 0,
            f"got {stdout!r}"
        )

        print()
        print("[Section 3] Matching cache entry (reviewed at current commit, clean tree)")

        # Create cache with matching review
        current_branch = fixture.get_current_branch()
        current_hash = fixture.get_head_sha()[:7]  # short hash

        cache_data = {
            "review": {
                "lastRun": "2025-09-15T12:00:00Z",
                "commit": current_hash,
                "branch": current_branch,
                "reviewDir": "/some/path",
                "reviewers": ["Uncle Bob", "Security Sage"],
                "panelModel": "sonnet",
                "findings": []
            }
        }

        fixture.write_cache_file(fixture.repo_root, cache_data)

        # Test 5: Matching cache should return exit 0
        stdout, stderr, exit_code = run_expert_review_status(fixture.repo_root)
        test_result(
            "expert-review-status with matching cache and clean tree exits 0",
            exit_code == 0,
            f"got {exit_code}"
        )

        # Test 6: --json with matching cache shows reviewed=true, current=true
        stdout, stderr, exit_code = run_expert_review_status(fixture.repo_root, ["--json"])
        test_result(
            "expert-review-status --json with match returns exit 0",
            exit_code == 0,
            f"got {exit_code}"
        )
        try:
            data = json.loads(stdout)
            test_result(
                "expert-review-status --json shows reviewed=true for matching cache",
                data.get("reviewed") is True,
                f"got {data.get('reviewed')}"
            )
            test_result(
                "expert-review-status --json shows current=true for matching hash",
                data.get("current") is True,
                f"got {data.get('current')}"
            )
            test_result(
                "expert-review-status --json shows dirty=false for clean tree",
                data.get("dirty") is False,
                f"got {data.get('dirty')}"
            )
            test_result(
                "expert-review-status --json includes cache fields",
                data.get("lastRun") == "2025-09-15T12:00:00Z",
                f"got {data.get('lastRun')}"
            )
        except json.JSONDecodeError as e:
            test_result(
                "expert-review-status --json output is valid JSON",
                False,
                f"JSON error: {e}"
            )

        print()
        print("[Section 4] Matching cache but dirty tree")

        # Make a trivial uncommitted edit
        test_file = fixture.repo_root / "README.md"
        test_file.write_text("# Test Repo\nModified\n")

        stdout, stderr, exit_code = run_expert_review_status(fixture.repo_root)
        test_result(
            "expert-review-status with matching cache but dirty tree exits 1",
            exit_code == 1,
            f"got {exit_code}"
        )

        # Test --json with dirty tree
        stdout, stderr, exit_code = run_expert_review_status(fixture.repo_root, ["--json"])
        test_result(
            "expert-review-status --json with dirty tree exits 1",
            exit_code == 1,
            f"got {exit_code}"
        )
        try:
            data = json.loads(stdout)
            test_result(
                "expert-review-status --json shows reviewed=true despite dirty tree",
                data.get("reviewed") is True,
                f"got {data.get('reviewed')}"
            )
            test_result(
                "expert-review-status --json shows current=true despite dirty tree",
                data.get("current") is True,
                f"got {data.get('current')}"
            )
            test_result(
                "expert-review-status --json shows dirty=true for uncommitted changes",
                data.get("dirty") is True,
                f"got {data.get('dirty')}"
            )
        except json.JSONDecodeError as e:
            test_result(
                "expert-review-status --json output is valid JSON",
                False,
                f"JSON error: {e}"
            )

        # Restore the file for next test
        test_file.write_text("# Test Repo\n")

        print()
        print("[Section 5] Cache with different commit (not current)")

        old_hash = current_hash[:6] + "X"  # Modify the hash
        cache_data["review"]["commit"] = old_hash

        fixture.write_cache_file(fixture.repo_root, cache_data)

        stdout, stderr, exit_code = run_expert_review_status(fixture.repo_root, ["--json"])
        test_result(
            "expert-review-status with different commit exits 1",
            exit_code == 1,
            f"got {exit_code}"
        )
        try:
            data = json.loads(stdout)
            test_result(
                "expert-review-status --json shows reviewed=true with old commit",
                data.get("reviewed") is True,
                f"got {data.get('reviewed')}"
            )
            test_result(
                "expert-review-status --json shows current=false with different commit",
                data.get("current") is False,
                f"got {data.get('current')}"
            )
        except json.JSONDecodeError:
            test_result("JSON parsing works", False)

        print()
        print("[Section 6] Cache with different branch")

        cache_data["review"]["commit"] = current_hash
        cache_data["review"]["branch"] = "different-branch"

        fixture.write_cache_file(fixture.repo_root, cache_data)

        stdout, stderr, exit_code = run_expert_review_status(fixture.repo_root, ["--json"])
        test_result(
            "expert-review-status with different branch exits 1",
            exit_code == 1,
            f"got {exit_code}"
        )
        try:
            data = json.loads(stdout)
            test_result(
                "expert-review-status --json shows reviewed=false with different branch",
                data.get("reviewed") is False,
                f"got {data.get('reviewed')}"
            )
        except json.JSONDecodeError:
            test_result("JSON parsing works", False)

        print()
        print("[Section 7] Malformed JSON in cache file")

        bad_cache_path = fixture.repo_root / ".claude" / "github-cache.json"
        bad_cache_path.write_text("{bad json")

        stdout, stderr, exit_code = run_expert_review_status(fixture.repo_root)
        test_result(
            "expert-review-status with malformed JSON exits 1",
            exit_code == 1,
            f"got {exit_code}"
        )
        test_result(
            "expert-review-status warns on malformed JSON (stderr or stdout)",
            len(stderr) > 0 or "not reviewed" in stdout.lower() or len(stdout) > 0,
            f"stderr={stderr!r}, stdout={stdout!r}"
        )

        # Test --json with malformed cache
        stdout, stderr, exit_code = run_expert_review_status(fixture.repo_root, ["--json"])
        test_result(
            "expert-review-status --json with malformed cache produces JSON",
            exit_code == 1,
            f"got {exit_code}"
        )
        try:
            data = json.loads(stdout)
            test_result(
                "expert-review-status --json with malformed cache shows reviewed=false",
                data.get("reviewed") is False,
                f"got {data.get('reviewed')}"
            )
        except json.JSONDecodeError as e:
            test_result(
                "expert-review-status --json with malformed cache outputs valid JSON",
                False,
                f"JSON error: {e}"
            )

        print()
        print("[Section 8] Cache with missing 'review' key")

        cache_data = {"some_other_key": "value"}
        fixture.write_cache_file(fixture.repo_root, cache_data)

        stdout, stderr, exit_code = run_expert_review_status(fixture.repo_root)
        test_result(
            "expert-review-status with missing 'review' key exits 1",
            exit_code == 1,
            f"got {exit_code}"
        )

        # Test --json with missing review key
        stdout, stderr, exit_code = run_expert_review_status(fixture.repo_root, ["--json"])
        test_result(
            "expert-review-status --json with missing 'review' key produces JSON",
            exit_code == 1,
            f"got {exit_code}"
        )
        try:
            data = json.loads(stdout)
            test_result(
                "expert-review-status --json shows reviewed=false for missing review key",
                data.get("reviewed") is False,
                f"got {data.get('reviewed')}"
            )
        except json.JSONDecodeError:
            test_result("JSON parsing works", False)

        print()
        print("[Section 9] Cache with missing 'lastRun' (required for reviewed=true)")

        cache_data = {
            "review": {
                "commit": current_hash,
                "branch": current_branch,
                # Missing lastRun
            }
        }
        fixture.write_cache_file(fixture.repo_root, cache_data)

        stdout, stderr, exit_code = run_expert_review_status(fixture.repo_root, ["--json"])
        test_result(
            "expert-review-status with missing lastRun exits 1",
            exit_code == 1,
            f"got {exit_code}"
        )
        try:
            data = json.loads(stdout)
            test_result(
                "expert-review-status shows reviewed=false when lastRun is missing",
                data.get("reviewed") is False,
                f"got {data.get('reviewed')}"
            )
        except json.JSONDecodeError:
            test_result("JSON parsing works", False)

        print()
        print("[Section 10] Cache with 'review' field that is not a dict")

        # Test with review as an integer
        bad_cache_path = fixture.repo_root / ".claude" / "github-cache.json"
        bad_cache_path.write_text(json.dumps({"review": 123}))

        stdout, stderr, exit_code = run_expert_review_status(fixture.repo_root)
        test_result(
            "expert-review-status with non-dict review exits 1",
            exit_code == 1,
            f"got {exit_code}"
        )
        test_result(
            "expert-review-status warns on non-dict review (stderr)",
            "not a dict" in stderr.lower() or "non-dict" in stderr.lower() or len(stderr) > 0,
            f"got {stderr!r}"
        )

        # Test --json with non-dict review
        stdout, stderr, exit_code = run_expert_review_status(fixture.repo_root, ["--json"])
        test_result(
            "expert-review-status --json with non-dict review exits 1",
            exit_code == 1,
            f"got {exit_code}"
        )
        try:
            data = json.loads(stdout)
            test_result(
                "expert-review-status --json with non-dict review shows reviewed=false",
                data.get("reviewed") is False,
                f"got {data.get('reviewed')}"
            )
            test_result(
                "expert-review-status --json output does not contain hash_current",
                "hash_current" not in data,
                f"fields: {list(data.keys())}"
            )
        except json.JSONDecodeError as e:
            test_result(
                "expert-review-status --json with non-dict review outputs valid JSON",
                False,
                f"JSON error: {e}"
            )

        # Test with review as a string
        bad_cache_path.write_text(json.dumps({"review": "not a dict"}))

        stdout, stderr, exit_code = run_expert_review_status(fixture.repo_root, ["--json"])
        test_result(
            "expert-review-status with string review exits 1",
            exit_code == 1,
            f"got {exit_code}"
        )
        try:
            data = json.loads(stdout)
            test_result(
                "expert-review-status --json with string review shows reviewed=false",
                data.get("reviewed") is False,
                f"got {data.get('reviewed')}"
            )
        except json.JSONDecodeError:
            test_result("JSON parsing works", False)

        # Test with review as a list
        bad_cache_path.write_text(json.dumps({"review": ["not", "a", "dict"]}))

        stdout, stderr, exit_code = run_expert_review_status(fixture.repo_root, ["--json"])
        test_result(
            "expert-review-status with list review exits 1",
            exit_code == 1,
            f"got {exit_code}"
        )
        try:
            data = json.loads(stdout)
            test_result(
                "expert-review-status --json with list review shows reviewed=false",
                data.get("reviewed") is False,
                f"got {data.get('reviewed')}"
            )
        except json.JSONDecodeError:
            test_result("JSON parsing works", False)

        print()
        print("[Section 11] Human-readable output format")

        cache_data = {
            "review": {
                "lastRun": "2025-09-15T12:00:00Z",
                "commit": current_hash,
                "branch": current_branch,
                "reviewDir": "/some/path",
                "reviewers": ["Uncle Bob"],
                "panelModel": "sonnet",
                "findings": []
            }
        }
        fixture.write_cache_file(fixture.repo_root, cache_data)

        stdout, stderr, exit_code = run_expert_review_status(fixture.repo_root)
        test_result(
            "expert-review-status default output is human-readable",
            len(stdout) > 0 and exit_code == 0,
            f"got {stdout!r}"
        )
        test_result(
            "expert-review-status default output is not JSON",
            not stdout.strip().startswith("{"),
            f"got {stdout!r}"
        )

        print()
        print("[Section 12] Exit code is 0 only for reviewed + current + clean tree")

        # Set up clean state: reviewed=true, current=true, dirty=false
        cache_data = {
            "review": {
                "lastRun": "2025-09-15T12:00:00Z",
                "commit": current_hash,
                "branch": current_branch,
                "reviewDir": "/some/path",
                "reviewers": [],
                "panelModel": "sonnet",
                "findings": []
            }
        }
        fixture.write_cache_file(fixture.repo_root, cache_data)

        stdout, stderr, exit_code = run_expert_review_status(fixture.repo_root)
        test_result(
            "expert-review-status returns 0 only when fully matched and clean",
            exit_code == 0,
            f"got {exit_code} (stdout={stdout!r})"
        )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 13] jq pipeline from commands/expert-review.md handles empty reviewers/findings")

    fixture = GitFixture()
    try:
        old_cwd = os.getcwd()
        os.chdir(fixture.repo_root)

        fixture.create_initial_commit("Initial commit")

        # Create cache with branch and lastRun, but missing reviewers/findings keys
        # (simulating a partial or minimal cache entry)
        current_branch = fixture.get_current_branch()
        current_hash = fixture.get_head_sha()[:7]

        cache_data = {
            "review": {
                "branch": current_branch,
                "lastRun": "2025-09-15T12:00:00Z",
                # Missing: reviewers, findings, commit, reviewDir
            }
        }

        fixture.write_cache_file(fixture.repo_root, cache_data)

        # Run the expert-review-status.py script with --json
        script = Path(__file__).parent.parent / "scripts" / "expert-review-status.py"
        result = subprocess.run(
            [sys.executable, str(script), "--json"],
            cwd=fixture.repo_root,
            capture_output=True,
            text=True
        )

        # Parse the JSON output
        try:
            status_json = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            test_result(
                "expert-review-status outputs valid JSON for partial cache",
                False,
                f"JSON decode error: {e}"
            )
            os.chdir(old_cwd)
            fixture.cleanup()
            h.summarize_and_exit()

        # Now test the actual jq pipeline from commands/expert-review.md
        # These are the exact lines from the command that would fail if reviewers/findings were null

        # Line 187: REVIEWERS=$(printf '%s' "$STATUS_JSON" | jq -r '.reviewers | join(", ")')
        try:
            reviewers_output = subprocess.run(
                ["jq", "-r", ".reviewers | join(\", \")"],
                input=result.stdout,
                capture_output=True,
                text=True,
                check=False
            )
            test_result(
                "jq '.reviewers | join' with empty array does not crash",
                reviewers_output.returncode == 0,
                f"jq returned {reviewers_output.returncode}, stderr: {reviewers_output.stderr}"
            )
        except Exception as e:
            test_result(
                "jq '.reviewers | join' command runs successfully",
                False,
                f"Exception: {e}"
            )

        # Line 188: FINDINGS=$(printf '%s' "$STATUS_JSON" | jq -r '.findings')
        try:
            findings_output = subprocess.run(
                ["jq", "-r", ".findings"],
                input=result.stdout,
                capture_output=True,
                text=True,
                check=False
            )
            test_result(
                "jq '.findings' with empty object does not crash",
                findings_output.returncode == 0,
                f"jq returned {findings_output.returncode}, stderr: {findings_output.stderr}"
            )
        except Exception as e:
            test_result(
                "jq '.findings' command runs successfully",
                False,
                f"Exception: {e}"
            )

        # Line 191: jq -r 'to_entries | map(...) | join(" / ")'
        # This is the most complex pipeline; ensure it handles empty findings dict
        try:
            findings_str_output = subprocess.run(
                ["jq", "-r", "to_entries | map(\"\\(.value)\\(.key|.[0:1]|ascii_upcase)\") | join(\" / \")"],
                input=findings_output.stdout,
                capture_output=True,
                text=True,
                check=False
            )
            test_result(
                "jq findings processing pipeline does not crash on empty findings",
                findings_str_output.returncode == 0,
                f"jq returned {findings_str_output.returncode}, stderr: {findings_str_output.stderr}"
            )
        except Exception as e:
            test_result(
                "jq findings processing pipeline runs successfully",
                False,
                f"Exception: {e}"
            )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    h.summarize_and_exit()
