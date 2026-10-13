#!/usr/bin/env python3
"""
Test suite for cache reading, validation, and hashing.

Run with: python3 tests/test_workflow_cache.py
"""

import sys
import json
import tempfile
import os
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.cache import (
    read_github_cache, read_issues_cache, hash_file_content,
    hash_github_cache_file, hash_issues_cache_file, write_cache,
    read_local_tracker, write_local_tracker,
    trusted_github_cache, verify_pr_head
)
from workflow.models import GitHubCacheData, IssueInfo, StackInfo, LocalTrackerData, LocalTrackerEntry
from workflow.safety import Unknown
from _test_harness import Harness
from unittest import mock


if __name__ == "__main__":
    h = Harness("WORKFLOW CACHE TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Hash functions")

    content = "test content"
    hash1 = hash_file_content(content)
    hash2 = hash_file_content(content)
    test_result(
        "hash_file_content() produces consistent hash",
        hash1 == hash2
    )
    test_result(
        "hash_file_content() produces 64-char hex (SHA256)",
        len(hash1) == 64 and all(c in "0123456789abcdef" for c in hash1)
    )

    content2 = "different content"
    hash3 = hash_file_content(content2)
    test_result(
        "hash_file_content() produces different hash for different content",
        hash1 != hash3
    )

    hash_lower = hash_file_content("content")
    hash_upper = hash_file_content("CONTENT")
    test_result(
        "hash_file_content() is case-sensitive",
        hash_lower != hash_upper
    )

    hash_empty = hash_file_content("")
    test_result(
        "hash_file_content() handles empty string",
        len(hash_empty) == 64
    )

    print()
    print("[Section 2] Read github-cache.json")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)

        cache_data = GitHubCacheData(
            branch="feature",
            issue=IssueInfo(number=1, url="http://...", title="Test", body="Body"),
            stack=StackInfo(is_stacked=False)
        )
        cache_file = tmppath / "github-cache.json"
        cache_file.write_text(json.dumps(cache_data.to_dict()))

        result, error = read_github_cache(cache_file)
        test_result(
            "read_github_cache() parses valid file",
            result is not None and error is None
        )
        test_result(
            "read_github_cache() extracts branch",
            result and result.branch == "feature"
        )

        missing_file = tmppath / "missing.json"
        result, error = read_github_cache(missing_file)
        test_result(
            "read_github_cache() handles missing file gracefully",
            result is None and isinstance(error, Unknown)
        )

        bad_json_file = tmppath / "bad.json"
        bad_json_file.write_text("{bad json")
        result, error = read_github_cache(bad_json_file)
        test_result(
            "read_github_cache() handles malformed JSON",
            result is None and isinstance(error, Unknown)
        )

    print()
    print("[Section 3] Read issues.json")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)

        issues_file = tmppath / "issues.json"
        issues_data = {
            "schema_version": "1.0",
            "next_id": 2,
            "issues": {
                "1": {"id": 1, "title": "First", "body": "", "status": "open"}
            }
        }
        issues_file.write_text(json.dumps(issues_data))

        result, error = read_issues_cache(issues_file)
        test_result(
            "read_issues_cache() parses valid file",
            result is not None and error is None
        )
        test_result(
            "read_issues_cache() extracts next_id",
            result and result.next_id == 2
        )

        missing_file = tmppath / "missing.json"
        result, error = read_issues_cache(missing_file)
        test_result(
            "read_issues_cache() handles missing file gracefully",
            result is None and isinstance(error, Unknown)
        )

    print()
    print("[Section 4] File hashing")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        test_file = tmppath / "test.json"
        test_file.write_text('{"key": "value"}')

        file_hash = hash_github_cache_file(test_file)
        test_result(
            "hash_github_cache_file() returns valid hash",
            file_hash and len(file_hash) == 64
        )

        missing_hash = hash_github_cache_file(tmppath / "nonexistent.json")
        test_result(
            "hash_github_cache_file() returns None for missing file",
            missing_hash is None
        )

        issues_hash = hash_issues_cache_file(test_file)
        test_result(
            "hash_issues_cache_file() returns valid hash",
            issues_hash and len(issues_hash) == 64
        )

    print()
    print("[Section 5] Cache round-trip preserves nested fields")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        cache_file = tmppath / "github-cache.json"

        original = GitHubCacheData(
            branch="feature",
            issue=IssueInfo(number=99, url="http://gh", title="Issue", body="Body"),
            stack=StackInfo(is_stacked=True, parent_branch="main", parent_pr=42)
        )
        cache_file.write_text(json.dumps(original.to_dict()))

        read_data, error = read_github_cache(cache_file)
        test_result(
            "read_github_cache() preserves issue number through round-trip",
            read_data and read_data.issue and read_data.issue.number == 99
        )
        test_result(
            "read_github_cache() preserves stack info through round-trip",
            read_data and read_data.stack and read_data.stack.parent_branch == "main"
                and read_data.stack.parent_pr == 42
        )

    print()
    print("[Section 6] Write cache with locking")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        cache_file = tmppath / "test.json"

        data = {"key": "value", "nested": {"a": 1}}
        success, error = write_cache(cache_file, data)
        test_result(
            "write_cache() succeeds for new file",
            success and error is None
        )
        test_result(
            "write_cache() creates file with correct content",
            cache_file.exists() and json.loads(cache_file.read_text()) == data
        )

        data2 = {"key": "updated"}
        success, error = write_cache(cache_file, data2)
        test_result(
            "write_cache() overwrites existing file",
            success and json.loads(cache_file.read_text()) == data2
        )

        lock_file = cache_file.with_suffix(cache_file.suffix + ".lock")
        try:
            lock_file.touch()
            success, error = write_cache(cache_file, {"new": "data"})
            test_result(
                "write_cache() returns False when lock file exists",
                not success and isinstance(error, Unknown) and "locked" in str(error)
            )
        finally:
            lock_file.unlink(missing_ok=True)

        cache_file_updated = cache_file.read_text()
        test_result(
            "write_cache() does not clobber original when lock exists",
            json.loads(cache_file_updated) == data2
        )

        tmpfile_pattern = f".{cache_file.name}."
        leftover_tmpfiles = list(tmppath.glob(f"{tmpfile_pattern}*"))
        test_result(
            "write_cache() cleans up temp files on success",
            len([f for f in leftover_tmpfiles if f.suffix == ".tmp"]) == 0
        )

    print()
    print("[Section 7] Lock staleness detection")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        cache_file = tmppath / "test.json"
        lock_file = cache_file.with_suffix(cache_file.suffix + ".lock")

        # Test 1: Recent lock file blocks write
        lock_file.write_text(f"{os.getpid()} {time.time()}")
        success, error = write_cache(cache_file, {"key": "value"})
        test_result(
            "write_cache() is blocked by recent lock file",
            not success and isinstance(error, Unknown) and "locked" in str(error)
        )

        # Clean up for next test
        lock_file.unlink(missing_ok=True)

    print()
    print("[Section 8] Stale lock file recovery")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        cache_file = tmppath / "test.json"
        lock_file = cache_file.with_suffix(cache_file.suffix + ".lock")

        # Create a stale lock file (older than LOCK_STALE_SECONDS)
        import time as time_module
        stale_time = time_module.time() - 700  # 700 seconds ago (> 600 second threshold)
        lock_file.write_text(f"{os.getpid()} {stale_time}")

        data = {"key": "value"}
        success, error = write_cache(cache_file, data)
        test_result(
            "write_cache() succeeds by removing stale lock",
            success and error is None
        )
        test_result(
            "write_cache() creates file after removing stale lock",
            cache_file.exists() and json.loads(cache_file.read_text()) == data
        )
        test_result(
            "write_cache() removes stale lock file",
            not lock_file.exists()
        )

    print("[Section 9] read/write_local_tracker integration")

    tmpdir = Path(tempfile.mkdtemp())
    try:
        tracker_path = tmpdir / "issues.json"

        # Test read missing file
        data, err = read_local_tracker(tracker_path)
        test_result(
            "read_local_tracker: missing file returns Unknown",
            data is None and isinstance(err, Unknown)
        )

        # Test write and read back
        tracker = LocalTrackerData(entries=[
            LocalTrackerEntry(id=1, title="Task", status="todo", plan="plans/1.md")
        ])
        success, err = write_local_tracker(tracker_path, tracker)
        test_result(
            "write_local_tracker: succeeds",
            success and err is None
        )

        data, err = read_local_tracker(tracker_path)
        test_result(
            "read_local_tracker: reads back written data",
            data is not None and len(data.entries) == 1 and data.entries[0].id == 1
        )

    finally:
        import shutil
        shutil.rmtree(tmpdir)

    print()

    print()
    print("[Section 20] trusted_github_cache: whole-cache branch trust")

    ISSUE_55 = {"number": 55, "url": "https://github.com/o/r/issues/55", "title": "t", "body": "b", "state": "OPEN"}

    def _trust(content, branch="feature/x"):
        """Write content (dict -> JSON, str -> raw) to a temp cache file and run the helper."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "github-cache.json"
            path.write_text(content if isinstance(content, str) else json.dumps(content))
            return trusted_github_cache(path, branch)

    with tempfile.TemporaryDirectory() as tmpdir:
        missing_result = trusted_github_cache(Path(tmpdir) / "absent.json", "feature/x")
    test_result(
        "trusted_github_cache: missing file -> (None, None, 'missing', False)",
        missing_result == (None, None, "missing", False),
        f"got {missing_result!r}"
    )

    test_result(
        "trusted_github_cache: non-JSON -> invalid, not stack-claiming",
        _trust("{not json") == (None, None, "invalid", False)
    )
    test_result(
        "trusted_github_cache: JSON array -> invalid, not stack-claiming",
        _trust("[1, 2]") == (None, None, "invalid", False)
    )
    test_result(
        "trusted_github_cache: branch absent (pr present) -> invalid",
        _trust({"pr": {"number": 68, "url": "u"}}) == (None, None, "invalid", False)
    )
    test_result(
        "trusted_github_cache: empty branch -> invalid",
        _trust({"branch": "", "pr": {"number": 68, "url": "u"}}) == (None, None, "invalid", False)
    )
    test_result(
        "trusted_github_cache: non-str branch -> invalid",
        _trust({"branch": 5}) == (None, None, "invalid", False)
    )
    test_result(
        "trusted_github_cache: section that fails to construct -> invalid",
        _trust({"branch": "feature/x", "issue": {"bogus_key": 1}}) == (None, None, "invalid", False)
    )
    test_result(
        "trusted_github_cache: review-only cache -> invalid, not stack-claiming",
        _trust({"review": {"branch": "feature-x", "commit": "abc"}}) == (None, None, "invalid", False)
    )
    test_result(
        "trusted_github_cache: branchless cache with isStacked=true -> invalid, stack-claiming",
        _trust({"stack": {"isStacked": True, "parentBranch": "p", "parentPr": 1}}) == (None, None, "invalid", True)
    )

    test_result(
        "trusted_github_cache: branch mismatch -> branch_mismatch:<cached>, not stack-claiming",
        _trust({"branch": "old-branch", "pr": {"number": 68, "url": "u"}}) == (None, None, "branch_mismatch:old-branch", False)
    )
    test_result(
        "trusted_github_cache: branch mismatch with isStacked=true -> stack-claiming",
        _trust({"branch": "old-branch", "stack": {"isStacked": True, "parentBranch": "p", "parentPr": 1}})
        == (None, None, "branch_mismatch:old-branch", True)
    )
    test_result(
        "trusted_github_cache: branch mismatch with isStacked=false -> not stack-claiming",
        _trust({"branch": "old-branch", "stack": {"isStacked": False, "parentBranch": "p", "parentPr": 1}})
        == (None, None, "branch_mismatch:old-branch", False)
    )

    trusted, raw_pr, stale_reason, claimed = _trust({
        "branch": "feature/x",
        "issue": ISSUE_55,
        "localPlan": {"path": "plans/x.md"},
        "pr": {"number": 68, "url": "https://github.com/o/r/pull/68"},
    })
    test_result(
        "trusted_github_cache: /track-shaped cache (no schema_version) is trusted",
        stale_reason is None and claimed is False and isinstance(trusted, GitHubCacheData),
        f"stale_reason={stale_reason!r} claimed={claimed!r}"
    )
    test_result(
        "trusted_github_cache: trusted cache carries the branch",
        trusted is not None and trusted.branch == "feature/x",
        f"branch={trusted.branch if trusted else 'N/A'}"
    )
    test_result(
        "trusted_github_cache: trusted cache carries the issue",
        trusted is not None and trusted.issue is not None and trusted.issue.number == 55
    )
    test_result(
        "trusted_github_cache: pr section is read from the same parsed dict",
        raw_pr == {"number": 68, "url": "https://github.com/o/r/pull/68"},
        f"raw_pr={raw_pr!r}"
    )

    trusted, raw_pr, stale_reason, claimed = _trust({
        "branch": "feature/x",
        "stack": {"isStacked": True, "parentBranch": "feature/parent", "parentPr": 10},
    })
    test_result(
        "trusted_github_cache: trusted stacked cache populates the stack and claims nothing",
        stale_reason is None and claimed is False
        and trusted is not None and trusted.stack is not None
        and trusted.stack.parent_branch == "feature/parent",
        f"stale_reason={stale_reason!r} claimed={claimed!r}"
    )

    trusted, raw_pr, stale_reason, claimed = _trust({"branch": "old-branch", "pr": {"number": 68, "url": "u"}})
    test_result(
        "trusted_github_cache: stale verdict returns no data and no raw pr",
        trusted is None and raw_pr is None and stale_reason is not None
    )

    print()
    print("[Section 21] verify_pr_head: live head probe, binary verdict")

    def _verify(view_return, branch="feature/x", pr_number=68):
        """Run verify_pr_head with git.pr_view_json mocked; return (state, err, mock)."""
        with mock.patch("workflow.git.pr_view_json") as mock_view:
            mock_view.return_value = view_return
            state, err = verify_pr_head(pr_number, branch, Path("."))
        return state, err, mock_view

    state, err, mock_view = _verify({"headRefName": "feature/x", "state": "OPEN"})
    test_result(
        "verify_pr_head: head matches and OPEN -> ('OPEN', None)",
        state == "OPEN" and err is None,
        f"state={state!r} err={err!r}"
    )
    test_result(
        "verify_pr_head: asks git.pr_view_json for headRefName and state of the cached number",
        mock_view.call_count == 1 and mock_view.call_args.args[:2] == ("68", ["headRefName", "state"]),
        f"calls={mock_view.call_args_list}"
    )

    state, err, _ = _verify({"headRefName": "feature/x", "state": "MERGED"})
    test_result(
        "verify_pr_head: head matches and MERGED -> ('MERGED', None) (caller decides policy)",
        state == "MERGED" and err is None,
        f"state={state!r} err={err!r}"
    )

    state, err, _ = _verify({})
    test_result(
        "verify_pr_head: {} -> (None, Unknown lookup-failed), never 'no PR'",
        state is None and isinstance(err, Unknown)
        and err.reason == "could not verify PR #68 belongs to feature/x: lookup failed",
        f"state={state!r} err={err!r}"
    )

    state, err, _ = _verify({"headRefName": "feature/x"})
    test_result(
        "verify_pr_head: response missing state -> lookup-failed Unknown",
        state is None and isinstance(err, Unknown)
        and err.reason == "could not verify PR #68 belongs to feature/x: lookup failed",
        f"state={state!r} err={err!r}"
    )

    state, err, _ = _verify({"state": "OPEN"})
    test_result(
        "verify_pr_head: response missing headRefName -> lookup-failed Unknown",
        state is None and isinstance(err, Unknown)
        and err.reason == "could not verify PR #68 belongs to feature/x: lookup failed",
        f"state={state!r} err={err!r}"
    )

    state, err, _ = _verify({"headRefName": 5, "state": "OPEN"})
    test_result(
        "verify_pr_head: non-str headRefName -> lookup-failed Unknown",
        state is None and isinstance(err, Unknown)
        and err.reason == "could not verify PR #68 belongs to feature/x: lookup failed",
        f"state={state!r} err={err!r}"
    )

    state, err, _ = _verify({"headRefName": "feature/x", "state": None})
    test_result(
        "verify_pr_head: non-str state -> lookup-failed Unknown",
        state is None and isinstance(err, Unknown)
        and err.reason == "could not verify PR #68 belongs to feature/x: lookup failed",
        f"state={state!r} err={err!r}"
    )

    state, err, _ = _verify({"headRefName": "other-branch", "state": "OPEN"})
    test_result(
        "verify_pr_head: head mismatch -> Unknown naming cached PR and actual head",
        state is None and isinstance(err, Unknown)
        and err.reason == "cached PR #68 belongs to other-branch, not feature/x",
        f"state={state!r} err={err!r}"
    )

    print()
    h.summarize_and_exit()
