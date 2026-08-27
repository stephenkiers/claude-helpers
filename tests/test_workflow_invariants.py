#!/usr/bin/env python3
"""
Test suite for workflow invariants: fail-closed-to-unknown principle.

Key invariant: every function in worktrees.py/stack.py/project.py/cache.py that hits
a state it can't resolve must return an Unknown marker, never a silent default guess.

Run with: python3 tests/test_workflow_invariants.py
"""

import sys
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.safety import Unknown
from workflow.cache import read_github_cache, read_issues_cache
from workflow.stack import is_stacked
from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("WORKFLOW INVARIANTS TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Unknown type exists and behaves as expected")

    unk = Unknown("test reason")
    test_result(
        "Unknown has reason attribute",
        hasattr(unk, "reason")
    )
    test_result(
        "Unknown reason is captured",
        unk.reason == "test reason"
    )
    test_result(
        "Unknown has string representation",
        len(str(unk)) > 0
    )

    print()
    print("[Section 2] Cache reading returns Unknown on errors (not silent failures)")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)

        nonexistent = tmppath / "missing-github-cache.json"
        result, error = read_github_cache(nonexistent)
        test_result(
            "read_github_cache returns Unknown for missing file",
            result is None and isinstance(error, Unknown)
        )
        test_result(
            "Unknown error has reason attribute",
            isinstance(error, Unknown) and hasattr(error, "reason")
        )

        bad_json_file = tmppath / "bad.json"
        bad_json_file.write_text("{invalid json")
        result, error = read_github_cache(bad_json_file)
        test_result(
            "read_github_cache returns Unknown for malformed JSON",
            result is None and isinstance(error, Unknown)
        )

        invalid_schema_file = tmppath / "bad-schema.json"
        invalid_schema_file.write_text(json.dumps({"schema_version": "999"}))
        result, error = read_github_cache(invalid_schema_file)
        test_result(
            "read_github_cache returns Unknown for invalid schema_version",
            result is None and isinstance(error, Unknown)
        )

        nonexistent_issues = tmppath / "missing-issues.json"
        result, error = read_issues_cache(nonexistent_issues)
        test_result(
            "read_issues_cache returns Unknown for missing file",
            result is None and isinstance(error, Unknown)
        )

        bad_issues_json = tmppath / "bad-issues.json"
        bad_issues_json.write_text("{bad")
        result, error = read_issues_cache(bad_issues_json)
        test_result(
            "read_issues_cache returns Unknown for malformed JSON",
            result is None and isinstance(error, Unknown)
        )

    print()
    print("[Section 3] is_stacked returns False when cache missing, never guesses")

    with patch("workflow.stack.git.get_current_branch") as mock_branch:
        with patch("workflow.stack.git.get_default_branch") as mock_default:
            with patch("workflow.stack.git.get_worktree_list_porcelain") as mock_worktree:
                mock_branch.return_value = "feature"
                mock_default.return_value = ("main", None)
                mock_worktree.return_value = ""

                is_stack, parent, pr, _ = is_stacked(cache_path=Path("/nonexistent/cache"))
                test_result(
                    "is_stacked falls back to False when no cache",
                    is_stack is False
                )
                test_result(
                    "is_stacked parent is None when not stacked",
                    parent is None
                )
                test_result(
                    "is_stacked pr is None when not stacked",
                    pr is None
                )

    print()
    print("[Section 4] Cache validation rejects malformed shapes")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        cache_file = tmppath / "cache.json"

        missing_schema = {"branch": "feature"}
        cache_file.write_text(json.dumps(missing_schema))
        result, error = read_github_cache(cache_file)
        test_result(
            "read_github_cache rejects missing schema_version",
            result is None and isinstance(error, Unknown)
        )

        wrong_schema = {"schema_version": "9.9.9", "branch": "feature"}
        cache_file.write_text(json.dumps(wrong_schema))
        result, error = read_github_cache(cache_file)
        test_result(
            "read_github_cache rejects wrong schema_version",
            result is None and isinstance(error, Unknown)
        )

        non_string_branch = {
            "schema_version": "1.0",
            "branch": 123,
            "issue": None,
            "stack": {"is_stacked": False}
        }
        cache_file.write_text(json.dumps(non_string_branch))
        result, error = read_github_cache(cache_file)
        test_result(
            "read_github_cache rejects non-string branch",
            result is None and isinstance(error, Unknown)
        )

    print()
    print("[Section 5] Cache validation strictly checks types")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        issues_file = tmppath / "issues.json"

        not_dict = {"schema_version": "1.0"}
        issues_file.write_text(json.dumps(not_dict))
        result, error = read_issues_cache(issues_file)
        test_result(
            "read_issues_cache rejects missing next_id",
            result is None and isinstance(error, Unknown)
        )

        wrong_type_next_id = {
            "schema_version": "1.0",
            "next_id": "not_a_number",
            "issues": {}
        }
        issues_file.write_text(json.dumps(wrong_type_next_id))
        result, error = read_issues_cache(issues_file)
        test_result(
            "read_issues_cache rejects non-numeric next_id",
            result is None and isinstance(error, Unknown)
        )

        invalid_next_id = {
            "schema_version": "1.0",
            "next_id": 0,
            "issues": {}
        }
        issues_file.write_text(json.dumps(invalid_next_id))
        result, error = read_issues_cache(issues_file)
        test_result(
            "read_issues_cache rejects next_id < 1",
            result is None and isinstance(error, Unknown)
        )

    print()
    h.summarize_and_exit()
