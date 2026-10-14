#!/usr/bin/env python3
"""
Test suite for flow round 2 specification: checkTimeoutSecs round-tripping.

Tests that RepoCacheData correctly round-trips the checkTimeoutSecs field through
from_dict -> to_dict -> from_dict, with proper key mapping (camelCase JSON key,
snake_case Python field) and invalid value handling.

Run with: python3 tests/test_flow_round2_spec.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.models import RepoCacheData, validate_repo_cache
from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("FLOW ROUND 2 SPECIFICATION TEST SUITE")
    test_result = h.test_result

    print("[Section 1] RepoCacheData.checkTimeoutSecs key mapping and round-tripping")

    # Test 1.1: Valid positive integer value round-trips through camelCase JSON key
    repo = RepoCacheData(
        repo_path="/path/to/repo",
        worktree_parent="/path/worktrees",
        check_timeout_secs=120
    )
    repo_dict = repo.to_dict()
    test_result(
        "to_dict() maps snake_case check_timeout_secs to camelCase checkTimeoutSecs",
        "checkTimeoutSecs" in repo_dict and repo_dict["checkTimeoutSecs"] == 120
    )
    test_result(
        "to_dict() does not include snake_case check_timeout_secs in output",
        "check_timeout_secs" not in repo_dict
    )

    # Test 1.2: Round-trip through from_dict preserves value
    reconstructed = RepoCacheData.from_dict(repo_dict)
    test_result(
        "from_dict() reads camelCase checkTimeoutSecs and stores in snake_case field",
        reconstructed.check_timeout_secs == 120
    )

    # Test 1.3: Full round-trip (to_dict -> from_dict -> to_dict)
    final_dict = reconstructed.to_dict()
    test_result(
        "Full round-trip preserves checkTimeoutSecs value",
        final_dict.get("checkTimeoutSecs") == 120
    )
    test_result(
        "Full round-trip maintains camelCase key in JSON",
        "checkTimeoutSecs" in final_dict
    )

    print()
    print("[Section 2] Invalid checkTimeoutSecs values become None")

    # Test 2.1: bool is invalid (even though bool is int subclass in Python)
    invalid_bool_dict = {
        "repo_path": "/path",
        "worktree_parent": "/path/wt",
        "commands": {},
        "checkTimeoutSecs": True
    }
    repo_bool = RepoCacheData.from_dict(invalid_bool_dict)
    test_result(
        "from_dict() rejects bool value for checkTimeoutSecs (becomes None)",
        repo_bool.check_timeout_secs is None
    )

    bool_dict_false = {
        "repo_path": "/path",
        "worktree_parent": "/path/wt",
        "commands": {},
        "checkTimeoutSecs": False
    }
    repo_bool_false = RepoCacheData.from_dict(bool_dict_false)
    test_result(
        "from_dict() rejects False for checkTimeoutSecs (becomes None)",
        repo_bool_false.check_timeout_secs is None
    )

    # Test 2.2: Zero is invalid (non-positive)
    invalid_zero_dict = {
        "repo_path": "/path",
        "worktree_parent": "/path/wt",
        "commands": {},
        "checkTimeoutSecs": 0
    }
    repo_zero = RepoCacheData.from_dict(invalid_zero_dict)
    test_result(
        "from_dict() rejects 0 for checkTimeoutSecs (becomes None)",
        repo_zero.check_timeout_secs is None
    )

    # Test 2.3: Negative values are invalid
    invalid_negative_dict = {
        "repo_path": "/path",
        "worktree_parent": "/path/wt",
        "commands": {},
        "checkTimeoutSecs": -10
    }
    repo_negative = RepoCacheData.from_dict(invalid_negative_dict)
    test_result(
        "from_dict() rejects negative values for checkTimeoutSecs (becomes None)",
        repo_negative.check_timeout_secs is None
    )

    # Test 2.4: String values are invalid
    invalid_string_dict = {
        "repo_path": "/path",
        "worktree_parent": "/path/wt",
        "commands": {},
        "checkTimeoutSecs": "120"
    }
    repo_string = RepoCacheData.from_dict(invalid_string_dict)
    test_result(
        "from_dict() rejects string value for checkTimeoutSecs (becomes None)",
        repo_string.check_timeout_secs is None
    )

    # Test 2.5: Float values are invalid
    invalid_float_dict = {
        "repo_path": "/path",
        "worktree_parent": "/path/wt",
        "commands": {},
        "checkTimeoutSecs": 120.5
    }
    repo_float = RepoCacheData.from_dict(invalid_float_dict)
    test_result(
        "from_dict() rejects float value for checkTimeoutSecs (becomes None)",
        repo_float.check_timeout_secs is None
    )

    # Test 2.6: List values are invalid
    invalid_list_dict = {
        "repo_path": "/path",
        "worktree_parent": "/path/wt",
        "commands": {},
        "checkTimeoutSecs": [120]
    }
    repo_list = RepoCacheData.from_dict(invalid_list_dict)
    test_result(
        "from_dict() rejects list value for checkTimeoutSecs (becomes None)",
        repo_list.check_timeout_secs is None
    )

    print()
    print("[Section 3] Valid positive integer values are preserved")

    # Test 3.1: Small positive integer
    small_int_dict = {
        "repo_path": "/path",
        "worktree_parent": "/path/wt",
        "commands": {},
        "checkTimeoutSecs": 1
    }
    repo_small = RepoCacheData.from_dict(small_int_dict)
    test_result(
        "from_dict() preserves small positive value (1)",
        repo_small.check_timeout_secs == 1
    )

    # Test 3.2: Default timeout value
    default_timeout_dict = {
        "repo_path": "/path",
        "worktree_parent": "/path/wt",
        "commands": {},
        "checkTimeoutSecs": 300
    }
    repo_default = RepoCacheData.from_dict(default_timeout_dict)
    test_result(
        "from_dict() preserves default timeout (300)",
        repo_default.check_timeout_secs == 300
    )

    # Test 3.3: Large positive integer
    large_int_dict = {
        "repo_path": "/path",
        "worktree_parent": "/path/wt",
        "commands": {},
        "checkTimeoutSecs": 3600
    }
    repo_large = RepoCacheData.from_dict(large_int_dict)
    test_result(
        "from_dict() preserves large positive value (3600)",
        repo_large.check_timeout_secs == 3600
    )

    print()
    print("[Section 4] Missing checkTimeoutSecs is handled correctly")

    # Test 4.1: Missing key defaults to None
    missing_key_dict = {
        "repo_path": "/path",
        "worktree_parent": "/path/wt",
        "commands": {}
    }
    repo_missing = RepoCacheData.from_dict(missing_key_dict)
    test_result(
        "from_dict() missing checkTimeoutSecs becomes None",
        repo_missing.check_timeout_secs is None
    )

    # Test 4.2: Explicit None value is handled
    explicit_none_dict = {
        "repo_path": "/path",
        "worktree_parent": "/path/wt",
        "commands": {},
        "checkTimeoutSecs": None
    }
    repo_explicit_none = RepoCacheData.from_dict(explicit_none_dict)
    test_result(
        "from_dict() explicit None for checkTimeoutSecs stays None",
        repo_explicit_none.check_timeout_secs is None
    )

    print()
    print("[Section 5] to_dict() omits checkTimeoutSecs when None")

    # Test 5.1: None value is omitted from to_dict output
    repo_none = RepoCacheData(
        repo_path="/path",
        worktree_parent="/path/wt",
        check_timeout_secs=None
    )
    none_dict = repo_none.to_dict()
    test_result(
        "to_dict() omits checkTimeoutSecs key when value is None",
        "checkTimeoutSecs" not in none_dict
    )

    print()
    print("[Section 6] Real shipit-shaped payload with checkTimeoutSecs")

    # Test 6.1: Real shipit cache format with valid checkTimeoutSecs
    shipit_with_timeout = {
        "version": 1,
        "repo_path": "/Users/me/Repositories/my-project",
        "worktree_parent": "/Users/me/Repositories/my-project/worktrees",
        "commands": {
            "test": "pytest",
            "lint": "ruff check .",
            "typecheck": "mypy"
        },
        "checkTimeoutSecs": 600
    }
    repo_shipit = RepoCacheData.from_dict(shipit_with_timeout)
    test_result(
        "from_dict() parses shipit format with checkTimeoutSecs",
        repo_shipit.check_timeout_secs == 600
    )

    shipit_dict_out = repo_shipit.to_dict()
    test_result(
        "to_dict() produces camelCase checkTimeoutSecs for shipit format",
        shipit_dict_out.get("checkTimeoutSecs") == 600
    )

    # Test 6.2: Real shipit cache without checkTimeoutSecs (backward compat)
    shipit_no_timeout = {
        "version": 1,
        "repo_path": "/Users/me/Repositories/my-project",
        "worktree_parent": "/Users/me/Repositories/my-project/worktrees",
        "commands": {
            "test": "pytest"
        }
    }
    repo_shipit_no_timeout = RepoCacheData.from_dict(shipit_no_timeout)
    test_result(
        "from_dict() handles shipit format without checkTimeoutSecs",
        repo_shipit_no_timeout.check_timeout_secs is None
    )

    print()
    print("[Section 7] validation allows valid checkTimeoutSecs")

    valid_with_timeout = {
        "schema_version": "1.0",
        "repo_path": "/path",
        "worktree_parent": "/path/wt",
        "commands": {"test": "pytest"},
        "checkTimeoutSecs": 120
    }
    test_result(
        "validate_repo_cache() accepts valid cache with checkTimeoutSecs",
        validate_repo_cache(valid_with_timeout)
    )

    # Test with invalid checkTimeoutSecs (validation happens in from_dict, not validate_repo_cache)
    # But the validator should still accept the structure itself
    invalid_timeout_structure = {
        "schema_version": "1.0",
        "repo_path": "/path",
        "worktree_parent": "/path/wt",
        "commands": {"test": "pytest"},
        "checkTimeoutSecs": "not-an-int"
    }
    test_result(
        "validate_repo_cache() accepts structure (invalid values handled in from_dict)",
        validate_repo_cache(invalid_timeout_structure)
    )

    print()
    h.summarize_and_exit()
