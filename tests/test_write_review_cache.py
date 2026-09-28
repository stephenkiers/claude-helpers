#!/usr/bin/env python3
"""
Test suite for scripts/write-review-cache.py.

Tests cover:
1. Writer-to-reader round trip (write via script, read via schema readers)
2. Merge preservation of other top-level cache sections
3. Corrupt cache handling (backs up the file)
4. Empty argument rejection
5. Schema validation on read-back

Run with: python3 tests/test_write_review_cache.py
"""

import sys
import json
import subprocess
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT


def run_script(cache_path: Path, args: list) -> subprocess.CompletedProcess:
    """Run write-review-cache.py and return the result."""
    script_path = REPO_ROOT / "scripts" / "write-review-cache.py"
    cmd = [sys.executable, str(script_path), "--cache-path", str(cache_path)] + args
    return subprocess.run(cmd, capture_output=True, text=True)


def read_cache(cache_path: Path) -> dict:
    """Read the cache file as JSON."""
    with open(cache_path, "r") as f:
        return json.load(f)


if __name__ == "__main__":
    h = Harness("WRITE-REVIEW-CACHE TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Basic round-trip: write and read back")

    with tempfile.TemporaryDirectory() as tmpdir:
        cache_path = Path(tmpdir) / "github-cache.json"

        result = run_script(
            cache_path,
            [
                "--commit", "abc1234",
                "--branch", "fix-bug",
                "--review-dir", "/tmp/review-dir",
                "--panel-model", "sonnet",
                "--critical", "1",
                "--high", "2",
                "--medium", "3",
                "--low", "4",
                "--reviewer", "uncle-bob",
                "--reviewer", "security-sage",
            ]
        )

        test_result(
            "Script exits with 0 on valid input",
            result.returncode == 0,
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

        test_result(
            "Cache file created",
            cache_path.exists(),
            f"File does not exist at {cache_path}"
        )

        cache_data = read_cache(cache_path)
        review = cache_data.get("review")

        test_result(
            "Review section exists",
            review is not None,
            f"Cache data: {cache_data}"
        )

        test_result(
            "Commit is correct",
            review.get("commit") == "abc1234",
            f"Got: {review.get('commit')}"
        )

        test_result(
            "Branch is correct",
            review.get("branch") == "fix-bug",
            f"Got: {review.get('branch')}"
        )

        test_result(
            "reviewDir is correct",
            review.get("reviewDir") == "/tmp/review-dir",
            f"Got: {review.get('reviewDir')}"
        )

        test_result(
            "Severity counts are correct",
            review.get("findings") == {"critical": 1, "high": 2, "medium": 3, "low": 4},
            f"Got: {review.get('findings')}"
        )

        test_result(
            "Reviewers are correct",
            review.get("reviewers") == ["uncle-bob", "security-sage"],
            f"Got: {review.get('reviewers')}"
        )

        test_result(
            "lastRun is present",
            "lastRun" in review,
            f"Keys: {review.keys()}"
        )

        test_result(
            "panelModel is correct",
            review.get("panelModel") == "sonnet",
            f"Got: {review.get('panelModel')}"
        )

    print()
    print("[Section 2] Merge preservation: other top-level sections preserved")

    with tempfile.TemporaryDirectory() as tmpdir:
        cache_path = Path(tmpdir) / "github-cache.json"

        # Pre-populate with other data
        initial_cache = {
            "schema_version": "1.0",
            "issue": {"title": "Test issue", "body": "Test body"},
            "branch": "fix-bug",
        }
        cache_path.write_text(json.dumps(initial_cache))

        result = run_script(
            cache_path,
            [
                "--commit", "abc1234",
                "--branch", "fix-bug",
                "--review-dir", "/tmp/review-dir",
                "--critical", "0",
                "--high", "0",
                "--medium", "0",
                "--low", "1",
            ]
        )

        test_result(
            "Merge script returns 0",
            result.returncode == 0,
            f"stderr: {result.stderr}"
        )

        cache_data = read_cache(cache_path)

        test_result(
            "Existing schema_version preserved",
            cache_data.get("schema_version") == "1.0",
            f"Got: {cache_data.get('schema_version')}"
        )

        test_result(
            "Existing issue data preserved",
            cache_data.get("issue") == {"title": "Test issue", "body": "Test body"},
            f"Got: {cache_data.get('issue')}"
        )

        test_result(
            "Review section added",
            "review" in cache_data,
            f"Cache keys: {cache_data.keys()}"
        )

    print()
    print("[Section 3] Corrupt cache handling: backs up unreadable JSON")

    with tempfile.TemporaryDirectory() as tmpdir:
        cache_path = Path(tmpdir) / "github-cache.json"

        # Write invalid JSON
        cache_path.write_text("{ invalid json")

        result = run_script(
            cache_path,
            [
                "--commit", "abc1234",
                "--branch", "fix-bug",
                "--review-dir", "/tmp/review-dir",
                "--critical", "0",
                "--high", "0",
                "--medium", "0",
                "--low", "1",
            ]
        )

        test_result(
            "Script returns 0 on corrupt input (overwrites)",
            result.returncode == 0,
            f"stderr: {result.stderr}"
        )

        backup_path = Path(str(cache_path) + ".bak")
        test_result(
            "Backup file created",
            backup_path.exists(),
            f"Backup not found at {backup_path}"
        )

        # Verify backup contains the corrupt content
        backup_content = backup_path.read_text()
        test_result(
            "Backup contains corrupt content",
            "invalid json" in backup_content,
            f"Backup content: {backup_content}"
        )

        # Verify new cache is valid
        cache_data = read_cache(cache_path)
        test_result(
            "New cache is valid JSON",
            isinstance(cache_data, dict) and "review" in cache_data,
            f"Cache: {cache_data}"
        )

    print()
    print("[Section 4] Empty argument rejection")

    with tempfile.TemporaryDirectory() as tmpdir:
        cache_path = Path(tmpdir) / "github-cache.json"

        # Empty commit
        result = run_script(
            cache_path,
            [
                "--commit", "",
                "--branch", "fix-bug",
                "--review-dir", "/tmp/review-dir",
                "--critical", "0",
                "--high", "0",
                "--medium", "0",
                "--low", "1",
            ]
        )

        test_result(
            "Script rejects empty commit",
            result.returncode == 2,
            f"returncode: {result.returncode}, stderr: {result.stderr}"
        )

        test_result(
            "Error message mentions commit",
            "commit" in result.stderr.lower(),
            f"stderr: {result.stderr}"
        )

        # Empty branch
        result = run_script(
            cache_path,
            [
                "--commit", "abc1234",
                "--branch", "",
                "--review-dir", "/tmp/review-dir",
                "--critical", "0",
                "--high", "0",
                "--medium", "0",
                "--low", "1",
            ]
        )

        test_result(
            "Script rejects empty branch",
            result.returncode == 2,
            f"returncode: {result.returncode}, stderr: {result.stderr}"
        )

        test_result(
            "Error message mentions branch",
            "branch" in result.stderr.lower(),
            f"stderr: {result.stderr}"
        )

    print()
    print("[Section 5] Schema validation: read-back verification catches drift")

    with tempfile.TemporaryDirectory() as tmpdir:
        cache_path = Path(tmpdir) / "github-cache.json"

        result = run_script(
            cache_path,
            [
                "--commit", "abc1234",
                "--branch", "fix-bug",
                "--review-dir", "/tmp/review-dir",
                "--panel-model", "opus",
                "--critical", "1",
                "--high", "0",
                "--medium", "0",
                "--low", "0",
                "--reviewer", "alice",
            ]
        )

        test_result(
            "Valid write returns 0",
            result.returncode == 0,
            f"stderr: {result.stderr}"
        )

        cache_data = read_cache(cache_path)
        review = cache_data.get("review")

        # Verify critical fields for fast-path check
        test_result(
            "Review has required fields for fast-path",
            all(k in review for k in ["lastRun", "commit", "branch", "reviewers", "findings"]),
            f"Keys: {review.keys()}"
        )

        test_result(
            "Reviewers is a list",
            isinstance(review.get("reviewers"), list),
            f"Type: {type(review.get('reviewers'))}"
        )

        test_result(
            "Findings is a dict",
            isinstance(review.get("findings"), dict),
            f"Type: {type(review.get('findings'))}"
        )

    print()
    print("[Section 6] No reviewers edge case: empty reviewer array")

    with tempfile.TemporaryDirectory() as tmpdir:
        cache_path = Path(tmpdir) / "github-cache.json"

        result = run_script(
            cache_path,
            [
                "--commit", "abc1234",
                "--branch", "fix-bug",
                "--review-dir", "/tmp/review-dir",
                "--critical", "0",
                "--high", "0",
                "--medium", "0",
                "--low", "0",
            ]
        )

        test_result(
            "Script handles no reviewers",
            result.returncode == 0,
            f"stderr: {result.stderr}"
        )

        cache_data = read_cache(cache_path)
        review = cache_data.get("review")

        test_result(
            "Reviewers is an empty list",
            review.get("reviewers") == [],
            f"Got: {review.get('reviewers')}"
        )

    h.summarize_and_exit()
