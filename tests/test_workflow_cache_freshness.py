#!/usr/bin/env python3
"""
Test suite for cache freshness, hashing, and content validation.

Covers: content hashing consistency, cache staleness detection,
and comprehensive validation of cache schemas.

Run with: python3 tests/test_workflow_cache_freshness.py
"""

import sys
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.cache import (
    hash_file_content, hash_github_cache_file, hash_issues_cache_file,
    read_github_cache, read_issues_cache
)
from workflow.models import (
    GitHubCacheData, IssueInfo, StackInfo, IssuesCacheData, LocalIssueEntry,
    validate_github_cache, validate_issues_cache
)
from workflow.safety import Unknown
from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("WORKFLOW CACHE FRESHNESS TEST SUITE")
    test_result = h.test_result

    print("[Section 1] hash_file_content produces consistent SHA256 hashes")

    content = "test content for hashing"
    hash1 = hash_file_content(content)
    hash2 = hash_file_content(content)

    test_result(
        "hash_file_content produces consistent results",
        hash1 == hash2
    )
    test_result(
        "hash_file_content produces 64-character hex string (SHA256)",
        len(hash1) == 64 and all(c in "0123456789abcdef" for c in hash1)
    )
    test_result(
        "hash_file_content is deterministic",
        hash_file_content(content) == hash1
    )

    print()
    print("[Section 2] hash_file_content differentiates content")

    hash_a = hash_file_content("content A")
    hash_b = hash_file_content("content B")
    hash_a2 = hash_file_content("content A")

    test_result(
        "hash_file_content produces different hashes for different content",
        hash_a != hash_b
    )
    test_result(
        "hash_file_content produces same hash for same content repeated",
        hash_a == hash_a2
    )

    print()
    print("[Section 3] hash_file_content is case-sensitive")

    hash_lower = hash_file_content("content")
    hash_upper = hash_file_content("CONTENT")

    test_result(
        "hash_file_content is case-sensitive",
        hash_lower != hash_upper
    )

    print()
    print("[Section 4] hash_file_content handles empty content")

    hash_empty = hash_file_content("")
    test_result(
        "hash_file_content handles empty string",
        len(hash_empty) == 64
    )

    print()
    print("[Section 5] hash_github_cache_file reads and hashes files")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        cache_file = tmppath / "cache.json"

        cache_data = GitHubCacheData(
            branch="feature",
            issue=IssueInfo(number=1, url="http://...", title="Test", body="Body")
        )
        cache_file.write_text(json.dumps(cache_data.to_dict()))

        file_hash = hash_github_cache_file(cache_file)
        test_result(
            "hash_github_cache_file returns valid SHA256",
            file_hash and len(file_hash) == 64
        )

        file_hash2 = hash_github_cache_file(cache_file)
        test_result(
            "hash_github_cache_file is consistent",
            file_hash == file_hash2
        )

    print()
    print("[Section 6] hash_github_cache_file returns None for missing files")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        missing = tmppath / "nonexistent.json"

        file_hash = hash_github_cache_file(missing)
        test_result(
            "hash_github_cache_file returns None for missing file",
            file_hash is None
        )

    print()
    print("[Section 7] hash_issues_cache_file handles issues.json")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        issues_file = tmppath / "issues.json"

        issues_data = IssuesCacheData(next_id=2)
        issues_data.issues[1] = LocalIssueEntry(
            id=1, title="First", body="Desc", status="open"
        )
        issues_file.write_text(json.dumps(issues_data.to_dict()))

        file_hash = hash_issues_cache_file(issues_file)
        test_result(
            "hash_issues_cache_file returns valid hash",
            file_hash and len(file_hash) == 64
        )

    print()
    print("[Section 8] hash_issues_cache_file returns None for missing files")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        missing = tmppath / "nonexistent.json"

        file_hash = hash_issues_cache_file(missing)
        test_result(
            "hash_issues_cache_file returns None for missing file",
            file_hash is None
        )

    print()
    print("[Section 9] Cache validation round-trip: GitHub cache")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        cache_file = tmppath / "github-cache.json"

        original = GitHubCacheData(
            branch="feature",
            issue=IssueInfo(number=99, url="http://gh", title="Issue", body="Body"),
            stack=StackInfo(is_stacked=True, parent_branch="main", parent_pr=42)
        )
        cache_dict = original.to_dict()
        cache_file.write_text(json.dumps(cache_dict))

        is_valid = validate_github_cache(cache_dict)
        test_result(
            "validate_github_cache accepts fresh data",
            is_valid is True
        )

        read_data, error = read_github_cache(cache_file)
        test_result(
            "read_github_cache successfully parses valid cache",
            read_data is not None and error is None
        )
        test_result(
            "read_github_cache preserves branch",
            read_data and read_data.branch == "feature"
        )
        test_result(
            "read_github_cache preserves issue number",
            read_data and read_data.issue and read_data.issue.number == 99
        )

    print()
    print("[Section 10] Cache validation round-trip: Issues cache")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        issues_file = tmppath / "issues.json"

        original = IssuesCacheData(next_id=5)
        original.issues[1] = LocalIssueEntry(
            id=1, title="Issue 1", body="Desc 1", status="open"
        )
        original.issues[2] = LocalIssueEntry(
            id=2, title="Issue 2", body="Desc 2", status="closed"
        )

        issues_dict = original.to_dict()
        issues_file.write_text(json.dumps(issues_dict))

        is_valid = validate_issues_cache(issues_dict)
        test_result(
            "validate_issues_cache accepts fresh data",
            is_valid is True
        )

        read_data, error = read_issues_cache(issues_file)
        test_result(
            "read_issues_cache successfully parses valid cache",
            read_data is not None and error is None
        )
        test_result(
            "read_issues_cache preserves next_id",
            read_data and read_data.next_id == 5
        )
        test_result(
            "read_issues_cache preserves issue count",
            read_data and len(read_data.issues) == 2
        )

    print()
    h.summarize_and_exit()
