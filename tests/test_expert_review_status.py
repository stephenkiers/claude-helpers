#!/usr/bin/env python3
"""
Test suite for expert-review-status.py script.

Run with: python3 tests/test_expert_review_status.py
"""

import sys
import json
import subprocess
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness


def create_temp_git_repo(initial_commit: bool = True) -> Path:
    """
    Create a temporary git repository, optionally with an initial empty commit.
    Returns the repo path.
    """
    tmpdir = Path(tempfile.mkdtemp())
    subprocess.run(
        ["git", "init"],
        cwd=tmpdir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=test@test.local", "-c", "user.name=Test User",
         "commit", "--allow-empty", "-m", "Initial commit"],
        cwd=tmpdir,
        check=True,
        capture_output=True,
    )
    return tmpdir


def run_status_script(repo_path: Path, args: list = None) -> subprocess.CompletedProcess:
    """
    Run the expert-review-status.py script in the given repo directory.
    Returns the CompletedProcess result.
    """
    if args is None:
        args = []
    script_path = Path(__file__).parent.parent / "scripts" / "expert-review-status.py"
    result = subprocess.run(
        [sys.executable, str(script_path)] + args,
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    return result


if __name__ == "__main__":
    h = Harness("EXPERT-REVIEW-STATUS TEST SUITE")
    test_result = h.test_result

    print("[Section 1] No cache file → reviewed: false, exit 1")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo = create_temp_git_repo()
        try:
            result = run_status_script(repo, ["--json"])
            test_result(
                "No cache file returns exit code 1",
                result.returncode == 1
            )

            data = json.loads(result.stdout)
            test_result(
                "No cache file → reviewed: false",
                data.get("reviewed") is False
            )
            test_result(
                "No cache file → current: false",
                data.get("current") is False
            )
            test_result(
                "No cache file → dirty: false (clean repo)",
                data.get("dirty") is False
            )
        finally:
            import shutil
            shutil.rmtree(repo, ignore_errors=True)

    print()
    print("[Section 2] Cache with different branch → reviewed: false, exit 1")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo = create_temp_git_repo()
        try:
            # Create cache file with a different branch
            claude_dir = repo / ".claude"
            claude_dir.mkdir(exist_ok=True)
            cache_file = claude_dir / "github-cache.json"
            cache_data = {
                "schema_version": "1.0",
                "branch": "main",
                "review": {
                    "branch": "different-branch",
                    "commit": "abc1234",
                    "lastRun": "2026-01-01T12:00:00",
                    "reviewDir": "~/.claude/reviews/test/diff-abc1234-20260101T120000",
                    "reviewers": ["uncle-bob", "security-sage"],
                    "panelModel": "sonnet",
                    "findings": {"critical": 0, "high": 1, "medium": 2, "low": 0}
                }
            }
            cache_file.write_text(json.dumps(cache_data))

            result = run_status_script(repo, ["--json"])
            test_result(
                "Different branch returns exit code 1",
                result.returncode == 1
            )

            data = json.loads(result.stdout)
            test_result(
                "Different branch → reviewed: false",
                data.get("reviewed") is False
            )
        finally:
            import shutil
            shutil.rmtree(repo, ignore_errors=True)

    print()
    print("[Section 3] Cache with same branch but stale commit → reviewed: true, current: false, exit 1")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo = create_temp_git_repo()
        try:
            # Get current branch
            branch_result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            )
            current_branch = branch_result.stdout.strip()

            # Create cache with current branch but stale commit
            claude_dir = repo / ".claude"
            claude_dir.mkdir(exist_ok=True)
            cache_file = claude_dir / "github-cache.json"
            cache_data = {
                "schema_version": "1.0",
                "branch": "main",
                "review": {
                    "branch": current_branch,
                    "commit": "stale0000",  # This won't match current HEAD
                    "lastRun": "2026-01-01T12:00:00",
                    "reviewDir": "~/.claude/reviews/test/diff-stale0000-20260101T120000",
                    "reviewers": ["uncle-bob"],
                    "panelModel": "sonnet",
                    "findings": {"critical": 0, "high": 0, "medium": 1, "low": 0}
                }
            }
            cache_file.write_text(json.dumps(cache_data))

            result = run_status_script(repo, ["--json"])
            test_result(
                "Same branch, stale commit returns exit code 1",
                result.returncode == 1
            )

            data = json.loads(result.stdout)
            test_result(
                "Same branch, stale commit → reviewed: true",
                data.get("reviewed") is True
            )
            test_result(
                "Same branch, stale commit → current: false",
                data.get("current") is False
            )
        finally:
            import shutil
            shutil.rmtree(repo, ignore_errors=True)

    print()
    print("[Section 4] Cache matches HEAD exactly, clean tree → reviewed: true, current: true, dirty: false, exit 0")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo = create_temp_git_repo()
        try:
            # Get current branch and commit
            branch_result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            )
            current_branch = branch_result.stdout.strip()

            hash_result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            )
            current_hash = hash_result.stdout.strip()

            # Create cache with exact current state
            claude_dir = repo / ".claude"
            claude_dir.mkdir(exist_ok=True)
            cache_file = claude_dir / "github-cache.json"
            cache_data = {
                "schema_version": "1.0",
                "branch": "main",
                "review": {
                    "branch": current_branch,
                    "commit": current_hash,
                    "lastRun": "2026-01-01T12:00:00",
                    "reviewDir": f"~/.claude/reviews/test/diff-{current_hash}-20260101T120000",
                    "reviewers": ["uncle-bob", "security-sage"],
                    "panelModel": "sonnet",
                    "findings": {"critical": 0, "high": 0, "medium": 0, "low": 1}
                }
            }
            cache_file.write_text(json.dumps(cache_data))

            result = run_status_script(repo, ["--json"])
            test_result(
                "Matching HEAD, clean tree returns exit code 0",
                result.returncode == 0
            )

            data = json.loads(result.stdout)
            test_result(
                "Matching HEAD, clean tree → reviewed: true",
                data.get("reviewed") is True
            )
            test_result(
                "Matching HEAD, clean tree → current: true",
                data.get("current") is True
            )
            test_result(
                "Matching HEAD, clean tree → dirty: false",
                data.get("dirty") is False
            )
        finally:
            import shutil
            shutil.rmtree(repo, ignore_errors=True)

    print()
    print("[Section 5] Matching commit but dirty tree → reviewed: true, current: true, dirty: true, exit 1")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo = create_temp_git_repo()
        try:
            # Create and commit a test file first, so we have a stable commit
            test_file = repo / "test.txt"
            test_file.write_text("initial content")
            subprocess.run(
                ["git", "add", "test.txt"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-c", "user.email=test@test.local", "-c", "user.name=Test User",
                 "commit", "-m", "Add test file"],
                cwd=repo,
                check=True,
                capture_output=True,
            )

            # Now get the current branch and commit (after the test file is committed)
            branch_result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            )
            current_branch = branch_result.stdout.strip()

            hash_result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            )
            current_hash = hash_result.stdout.strip()

            # Create cache with exact current state
            claude_dir = repo / ".claude"
            claude_dir.mkdir(exist_ok=True)
            cache_file = claude_dir / "github-cache.json"
            cache_data = {
                "schema_version": "1.0",
                "branch": "main",
                "review": {
                    "branch": current_branch,
                    "commit": current_hash,
                    "lastRun": "2026-01-01T12:00:00",
                    "reviewDir": f"~/.claude/reviews/test/diff-{current_hash}-20260101T120000",
                    "reviewers": ["uncle-bob"],
                    "panelModel": "sonnet",
                    "findings": {"critical": 0, "high": 0, "medium": 0, "low": 0}
                }
            }
            cache_file.write_text(json.dumps(cache_data))

            # Now modify the tracked file (make repo dirty)
            test_file.write_text("modified content")

            result = run_status_script(repo, ["--json"])
            test_result(
                "Matching commit but dirty tree returns exit code 1",
                result.returncode == 1
            )

            data = json.loads(result.stdout)
            test_result(
                "Dirty tree → reviewed: true (still reviewed)",
                data.get("reviewed") is True
            )
            test_result(
                "Dirty tree → current: true (still current)",
                data.get("current") is True
            )
            test_result(
                "Dirty tree → dirty: true",
                data.get("dirty") is True
            )
        finally:
            import shutil
            shutil.rmtree(repo, ignore_errors=True)

    print()
    print("[Section 6] --quiet flag suppresses output but sets exit code correctly")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo = create_temp_git_repo()
        try:
            # Create cache matching current state
            branch_result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            )
            current_branch = branch_result.stdout.strip()

            hash_result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            )
            current_hash = hash_result.stdout.strip()

            claude_dir = repo / ".claude"
            claude_dir.mkdir(exist_ok=True)
            cache_file = claude_dir / "github-cache.json"
            cache_data = {
                "schema_version": "1.0",
                "review": {
                    "branch": current_branch,
                    "commit": current_hash,
                    "lastRun": "2026-01-01T12:00:00",
                    "reviewDir": f"~/.claude/reviews/test/diff-{current_hash}-20260101T120000",
                    "reviewers": ["uncle-bob"],
                    "panelModel": "sonnet",
                    "findings": {}
                }
            }
            cache_file.write_text(json.dumps(cache_data))

            result = run_status_script(repo, ["--quiet"])
            test_result(
                "--quiet suppresses stdout",
                result.stdout == ""
            )
            test_result(
                "--quiet still sets exit code correctly (0 for match)",
                result.returncode == 0
            )
        finally:
            import shutil
            shutil.rmtree(repo, ignore_errors=True)

    print()
    print("[Section 7] Partial cache entry (missing reviewers/findings) → defaults to [] and {}")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo = create_temp_git_repo()
        try:
            branch_result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            )
            current_branch = branch_result.stdout.strip()

            # Cache entry with only branch + lastRun — no reviewers/findings keys at all
            claude_dir = repo / ".claude"
            claude_dir.mkdir(exist_ok=True)
            cache_file = claude_dir / "github-cache.json"
            cache_data = {
                "schema_version": "1.0",
                "branch": "main",
                "review": {
                    "branch": current_branch,
                    "lastRun": "2026-01-01T12:00:00",
                },
            }
            cache_file.write_text(json.dumps(cache_data))

            result = run_status_script(repo, ["--json"])
            test_result(
                "Partial cache entry does not crash the script",
                result.returncode in (0, 1)
            )

            data = json.loads(result.stdout)
            test_result(
                "Partial cache entry → reviewers defaults to []",
                data.get("reviewers") == []
            )
            test_result(
                "Partial cache entry → findings defaults to {}",
                data.get("findings") == {}
            )

            # Same, but with reviewers/findings explicitly present as JSON null
            cache_data["review"]["reviewers"] = None
            cache_data["review"]["findings"] = None
            cache_file.write_text(json.dumps(cache_data))

            result = run_status_script(repo, ["--json"])
            data = json.loads(result.stdout)
            test_result(
                "Explicit null reviewers → defaults to [] (not None)",
                data.get("reviewers") == []
            )
            test_result(
                "Explicit null findings → defaults to {} (not None)",
                data.get("findings") == {}
            )
        finally:
            import shutil
            shutil.rmtree(repo, ignore_errors=True)

    print()
    print("[Section 8] get_git_info() failure emits a JSON error object when --json is set")

    with tempfile.TemporaryDirectory() as non_git_dir:
        non_git_path = Path(non_git_dir)
        result = run_status_script(non_git_path, ["--json"])
        test_result(
            "Non-git directory with --json returns exit code 1",
            result.returncode == 1
        )
        try:
            data = json.loads(result.stdout)
            test_result(
                "Non-git directory --json output is valid JSON",
                isinstance(data, dict)
            )
            test_result(
                "Non-git directory --json output includes an 'error' key",
                "error" in data
            )
        except json.JSONDecodeError as e:
            test_result(
                "Non-git directory --json output is valid JSON",
                False,
                f"JSON decode error: {e}, stdout: {result.stdout[:200]}"
            )

        # --quiet should suppress the JSON error body but still set exit code 1
        result_quiet = run_status_script(non_git_path, ["--json", "--quiet"])
        test_result(
            "Non-git directory with --json --quiet suppresses stdout",
            result_quiet.stdout == ""
        )
        test_result(
            "Non-git directory with --json --quiet still returns exit code 1",
            result_quiet.returncode == 1
        )

    print()

    h.summarize_and_exit()
