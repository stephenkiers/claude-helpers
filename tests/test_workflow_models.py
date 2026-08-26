#!/usr/bin/env python3
"""
Test suite for workflow models (schemas, validation, serialization).

Run with: python3 tests/test_workflow_models.py
"""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.models import (
    IssueInfo, StackInfo, GitHubCacheData, LocalIssueEntry, IssuesCacheData,
    RepoCacheData, validate_github_cache, validate_issues_cache, validate_repo_cache,
    GITHUB_CACHE_SCHEMA_VERSION, ISSUES_CACHE_SCHEMA_VERSION, REPO_CACHE_SCHEMA_VERSION
)
from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("WORKFLOW MODELS TEST SUITE")
    test_result = h.test_result

    print("[Section 1] GitHub cache schema and serialization")

    issue = IssueInfo(number=81, url="https://...", title="Test", body="Body")
    stack = StackInfo(is_stacked=True, parent_branch="main", parent_pr=42)
    cache = GitHubCacheData(branch="feature", issue=issue, stack=stack)

    cache_dict = cache.to_dict()
    test_result(
        "GitHubCacheData.to_dict() includes schema_version",
        cache_dict["schema_version"] == GITHUB_CACHE_SCHEMA_VERSION
    )
    test_result(
        "GitHubCacheData.to_dict() includes branch",
        cache_dict["branch"] == "feature"
    )
    test_result(
        "GitHubCacheData.to_dict() serializes nested issue",
        cache_dict["issue"]["number"] == 81
    )
    test_result(
        "GitHubCacheData.to_dict() serializes stack info",
        cache_dict["stack"]["is_stacked"] is True and cache_dict["stack"]["parent_pr"] == 42
    )

    reconstructed = GitHubCacheData.from_dict(cache_dict)
    test_result(
        "GitHubCacheData.from_dict() reconstructs branch",
        reconstructed.branch == "feature"
    )
    test_result(
        "GitHubCacheData.from_dict() reconstructs issue",
        reconstructed.issue and reconstructed.issue.number == 81
    )
    test_result(
        "GitHubCacheData.from_dict() reconstructs stack",
        reconstructed.stack.is_stacked is True
    )

    print()
    print("[Section 2] GitHub cache validation")

    test_result(
        "validate_github_cache() accepts valid cache",
        validate_github_cache(cache_dict)
    )

    bad_version = dict(cache_dict)
    bad_version["schema_version"] = "2.0"
    test_result(
        "validate_github_cache() rejects wrong schema_version",
        not validate_github_cache(bad_version)
    )

    bad_branch = dict(cache_dict)
    bad_branch["branch"] = 123
    test_result(
        "validate_github_cache() rejects non-string branch",
        not validate_github_cache(bad_branch)
    )

    test_result(
        "validate_github_cache() rejects non-dict input",
        not validate_github_cache("not a dict")
    )

    print()
    print("[Section 3] Issues cache schema and serialization")

    issues = IssuesCacheData(next_id=2)
    issues.issues[1] = LocalIssueEntry(id=1, title="First", body="Desc", status="open")

    issues_dict = issues.to_dict()
    test_result(
        "IssuesCacheData.to_dict() includes next_id",
        issues_dict["next_id"] == 2
    )
    test_result(
        "IssuesCacheData.to_dict() serializes issues",
        "1" in issues_dict["issues"]
    )

    reconstructed_issues = IssuesCacheData.from_dict(issues_dict)
    test_result(
        "IssuesCacheData.from_dict() reconstructs next_id",
        reconstructed_issues.next_id == 2
    )
    test_result(
        "IssuesCacheData.from_dict() reconstructs entries",
        1 in reconstructed_issues.issues and reconstructed_issues.issues[1].title == "First"
    )

    print()
    print("[Section 4] Issues cache validation")

    test_result(
        "validate_issues_cache() accepts valid cache",
        validate_issues_cache(issues_dict)
    )

    bad_next_id = dict(issues_dict)
    bad_next_id["next_id"] = 0
    test_result(
        "validate_issues_cache() rejects next_id < 1",
        not validate_issues_cache(bad_next_id)
    )

    bad_issues = dict(issues_dict)
    bad_issues["issues"] = "not a dict"
    test_result(
        "validate_issues_cache() rejects non-dict issues",
        not validate_issues_cache(bad_issues)
    )

    print()
    print("[Section 5] Repo cache schema")

    repo = RepoCacheData(repo_path="/path/to/repo", worktree_parent="/path/worktrees")
    repo_dict = repo.to_dict()
    test_result(
        "RepoCacheData.to_dict() includes schema_version",
        repo_dict["schema_version"] == REPO_CACHE_SCHEMA_VERSION
    )
    test_result(
        "RepoCacheData.to_dict() includes repo_path",
        repo_dict["repo_path"] == "/path/to/repo"
    )

    reconstructed_repo = RepoCacheData.from_dict(repo_dict)
    test_result(
        "RepoCacheData.from_dict() reconstructs repo_path",
        reconstructed_repo.repo_path == "/path/to/repo"
    )

    test_result(
        "validate_repo_cache() accepts valid cache",
        validate_repo_cache(repo_dict)
    )

    print()
    h.summarize_and_exit()
