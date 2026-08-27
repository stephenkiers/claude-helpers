#!/usr/bin/env python3
"""
Test suite for cache reading, validation, and hashing.

Run with: python3 tests/test_workflow_cache.py
"""

import sys
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.cache import (
    read_github_cache, read_issues_cache, hash_file_content,
    hash_github_cache_file, hash_issues_cache_file
)
from workflow.models import GitHubCacheData, IssueInfo, StackInfo
from workflow.safety import Unknown
from _test_harness import Harness


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
    h.summarize_and_exit()
