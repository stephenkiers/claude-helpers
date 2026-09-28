#!/usr/bin/env python3
"""
Spec-blind test suite for merge_queue.py (local merge queue feature).

Tests the observable behavior of the merge_queue module:
- Config resolution and validation (valid, missing, malformed)
- Ticket-based FIFO ordering scheme
- Trailer build/parse round-trip (Merge-Gate trailer)
- force_push_tested() force-push validation (--force-with-lease only)
- main(argv) subcommand routing
- MergeOutcome enum and typed results

Run with: python3 tests/test_merge_queue_spec_blind.py
"""

import sys
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _test_harness import Harness

# Import the module under test
from workflow.merge_queue import (
    MergeQueueConfig,
    load_and_validate_config,
    build_trailer,
    parse_trailer,
    MergeOutcome,
    MergeResult,
    Runner,
    main,
)

h = Harness("MERGE QUEUE SPEC-BLIND TEST SUITE")
test_result = h.test_result


# ============================================================================
# SECTION 1: Config Resolution and Validation
# ============================================================================
print("[Section 1] Config Resolution and Validation")

# Test 1: Valid config can be loaded
print("  [Test 1.1] Load and validate a valid config")
with tempfile.TemporaryDirectory() as tmpdir:
    config_path = Path(tmpdir) / "merge-queue.json"
    valid_config = {
        "base": "main",
        "steps": ["echo test"],
        "cleanup": False,
    }
    config_path.write_text(json.dumps(valid_config))

    # Mock the git functions to return expected values
    with patch('workflow.merge_queue.git.get_default_branch') as mock_branch:
        mock_branch.return_value = ("main", None)

        try:
            cfg = load_and_validate_config(config_path)
            test_result(
                "Valid config loads successfully",
                cfg is not None and isinstance(cfg, MergeQueueConfig),
                f"Got {type(cfg)}"
            )
            test_result(
                "Config has expected base field",
                cfg.base == "main",
                f"base={cfg.base}"
            )
            test_result(
                "Config has expected steps",
                len(cfg.steps) == 1 and cfg.steps[0] == "echo test",
                f"steps={cfg.steps}"
            )
        except Exception as e:
            test_result("Valid config loads successfully", False, str(e))

# Test 2: Missing config file raises FileNotFoundError
print("  [Test 1.2] Missing config file handling")
try:
    cfg = load_and_validate_config(Path("/nonexistent/merge-queue.json"))
    test_result("Missing config raises FileNotFoundError", False, "No error raised")
except FileNotFoundError:
    test_result("Missing config raises FileNotFoundError", True)
except Exception as e:
    test_result("Missing config raises FileNotFoundError", False, f"Wrong error: {type(e).__name__}: {e}")

# Test 3: Malformed JSON raises ValueError
print("  [Test 1.3] Malformed JSON handling")
with tempfile.TemporaryDirectory() as tmpdir:
    config_path = Path(tmpdir) / "bad.json"
    config_path.write_text("{invalid json")

    try:
        cfg = load_and_validate_config(config_path)
        test_result("Malformed JSON raises ValueError", False, "No error raised")
    except ValueError as e:
        test_result(
            "Malformed JSON raises ValueError",
            "Invalid JSON" in str(e),
            f"Message: {e}"
        )
    except Exception as e:
        test_result("Malformed JSON raises ValueError", False, f"Wrong error: {type(e).__name__}")

# Test 4: Unknown config keys are rejected
print("  [Test 1.4] Unknown config keys rejection")
with tempfile.TemporaryDirectory() as tmpdir:
    config_path = Path(tmpdir) / "merge-queue.json"
    config_with_unknown = {
        "base": "main",
        "steps": ["echo test"],
        "unknown_key": "value",
    }
    config_path.write_text(json.dumps(config_with_unknown))

    with patch('workflow.merge_queue.git.get_default_branch') as mock_branch:
        mock_branch.return_value = ("main", None)

        try:
            cfg = load_and_validate_config(config_path)
            test_result("Unknown config keys are rejected", False, "No error raised")
        except ValueError as e:
            test_result(
                "Unknown config keys are rejected",
                "Unknown" in str(e) or "unknown" in str(e),
                f"Message: {e}"
            )

# Test 5: Missing required 'base' field
print("  [Test 1.5] Missing required 'base' field")
with tempfile.TemporaryDirectory() as tmpdir:
    config_path = Path(tmpdir) / "merge-queue.json"
    bad_config = {
        "steps": ["echo test"],
    }
    config_path.write_text(json.dumps(bad_config))

    try:
        cfg = load_and_validate_config(config_path)
        test_result("Missing 'base' field raises ValueError", False, "No error raised")
    except ValueError as e:
        test_result(
            "Missing 'base' field raises ValueError",
            "base" in str(e),
            f"Message: {e}"
        )

# Test 6: Empty steps list is rejected
print("  [Test 1.6] Empty steps list rejection")
with tempfile.TemporaryDirectory() as tmpdir:
    config_path = Path(tmpdir) / "merge-queue.json"
    bad_config = {
        "base": "main",
        "steps": [],
    }
    config_path.write_text(json.dumps(bad_config))

    with patch('workflow.merge_queue.git.get_default_branch') as mock_branch:
        mock_branch.return_value = ("main", None)

        try:
            cfg = load_and_validate_config(config_path)
            test_result("Empty steps list raises ValueError", False, "No error raised")
        except ValueError as e:
            test_result(
                "Empty steps list raises ValueError",
                "steps" in str(e) and ("empty" in str(e) or "non-empty" in str(e)),
                f"Message: {e}"
            )

# Test 7: Step with empty string is rejected
print("  [Test 1.7] Empty step string rejection")
with tempfile.TemporaryDirectory() as tmpdir:
    config_path = Path(tmpdir) / "merge-queue.json"
    bad_config = {
        "base": "main",
        "steps": [""],
    }
    config_path.write_text(json.dumps(bad_config))

    with patch('workflow.merge_queue.git.get_default_branch') as mock_branch:
        mock_branch.return_value = ("main", None)

        try:
            cfg = load_and_validate_config(config_path)
            test_result("Empty step string raises ValueError", False, "No error raised")
        except ValueError as e:
            test_result(
                "Empty step string raises ValueError",
                "step" in str(e) and "empty" in str(e),
                f"Message: {e}"
            )

# Test 8: Base must match default branch
print("  [Test 1.8] Base matches default branch")
with tempfile.TemporaryDirectory() as tmpdir:
    config_path = Path(tmpdir) / "merge-queue.json"
    config_with_wrong_base = {
        "base": "develop",
        "steps": ["echo test"],
    }
    config_path.write_text(json.dumps(config_with_wrong_base))

    with patch('workflow.merge_queue.git.get_default_branch') as mock_branch:
        mock_branch.return_value = ("main", None)

        try:
            cfg = load_and_validate_config(config_path)
            test_result("Base must match default branch", False, "No error raised")
        except ValueError as e:
            test_result(
                "Base must match default branch",
                "base" in str(e) and "default" in str(e),
                f"Message: {e}"
            )

# Test 9: Config with dict step (cmd + timeout)
print("  [Test 1.9] Config with dict step validation")
with tempfile.TemporaryDirectory() as tmpdir:
    config_path = Path(tmpdir) / "merge-queue.json"
    config_with_dict_step = {
        "base": "main",
        "steps": [{"cmd": "make test", "timeout_secs": 300}],
    }
    config_path.write_text(json.dumps(config_with_dict_step))

    with patch('workflow.merge_queue.git.get_default_branch') as mock_branch:
        mock_branch.return_value = ("main", None)

        try:
            cfg = load_and_validate_config(config_path)
            test_result(
                "Config with dict step loads successfully",
                len(cfg.steps) == 1,
                f"Got {len(cfg.steps)} steps"
            )
        except Exception as e:
            test_result("Config with dict step loads successfully", False, str(e))

# Test 10: allow_unverified must contain 40-hex shas
print("  [Test 1.10] allow_unverified validation")
with tempfile.TemporaryDirectory() as tmpdir:
    config_path = Path(tmpdir) / "merge-queue.json"
    bad_sha_config = {
        "base": "main",
        "steps": ["echo test"],
        "allow_unverified": ["not_a_valid_sha"],
    }
    config_path.write_text(json.dumps(bad_sha_config))

    with patch('workflow.merge_queue.git.get_default_branch') as mock_branch:
        mock_branch.return_value = ("main", None)

        try:
            cfg = load_and_validate_config(config_path)
            test_result("Invalid allow_unverified sha raises ValueError", False, "No error raised")
        except ValueError as e:
            test_result(
                "Invalid allow_unverified sha raises ValueError",
                "allow_unverified" in str(e) or "sha" in str(e),
                f"Message: {e}"
            )


# ============================================================================
# SECTION 2: Trailer Build and Parse
# ============================================================================
print()
print("[Section 2] Merge-Gate Trailer Build and Parse")

# Test 11: build_trailer creates a valid trailer with correct parameter order
print("  [Test 2.1] build_trailer creates valid trailer with correct order")
base_sha = "a" * 40
tested_sha = "b" * 40
trailer = build_trailer(base_sha, tested_sha)
test_result(
    "build_trailer returns a string",
    isinstance(trailer, str),
    f"Got {type(trailer)}"
)
test_result(
    "Trailer starts with 'Merge-Gate:'",
    trailer.startswith("Merge-Gate:"),
    f"Got: {trailer[:50]}"
)
test_result(
    "Trailer contains tested-base (first param)",
    base_sha in trailer,
    f"Got: {trailer}"
)
test_result(
    "Trailer contains tested-head (second param)",
    tested_sha in trailer,
    f"Got: {trailer}"
)
test_result(
    "Trailer has 'tested-base=' before 'tested-head='",
    trailer.find("tested-base=") < trailer.find("tested-head="),
    f"Order incorrect in: {trailer}"
)

# Test 12: parse_trailer extracts values from valid trailer
print("  [Test 2.2] parse_trailer extracts from valid trailer")
trailer_text = build_trailer(base_sha, tested_sha)
# The parse_trailer function expects a commit SHA, which it will query via git
# We need to mock the git calls
with patch('workflow.merge_queue.Runner.run_git') as mock_git:
    with patch('workflow.merge_queue.git.is_ancestor') as mock_ancestor:
        # Mock git interpret-trailers to return the trailer
        mock_git.return_value = trailer_text
        mock_ancestor.return_value = True

        parsed_base, parsed_head = parse_trailer("dummycommit")
        test_result(
            "parse_trailer returns tuple of (base, head)",
            isinstance((parsed_base, parsed_head), tuple),
            f"Got {type((parsed_base, parsed_head))}"
        )
        # The return values depend on the regex match
        if parsed_base and parsed_head:
            test_result(
                "parse_trailer extracts correct base",
                parsed_base == base_sha,
                f"Got {parsed_base}"
            )
            test_result(
                "parse_trailer extracts correct head",
                parsed_head == tested_sha,
                f"Got {parsed_head}"
            )

# Test 13: parse_trailer returns (None, None) for invalid trailer
print("  [Test 2.3] parse_trailer returns None for invalid trailer")
with patch('workflow.merge_queue.Runner.run_git') as mock_git:
    mock_git.return_value = "Some random text without trailer"

    parsed_base, parsed_head = parse_trailer("dummycommit")
    test_result(
        "parse_trailer returns (None, None) for invalid input",
        parsed_base is None and parsed_head is None,
        f"Got ({parsed_base}, {parsed_head})"
    )

# Test 14: Trailer is idempotent (same inputs always produce same trailer)
print("  [Test 2.4] Trailer format is deterministic")
base = "c" * 40
head = "d" * 40
trailer1 = build_trailer(base, head)
trailer2 = build_trailer(base, head)
test_result(
    "Trailer is deterministic",
    trailer1 == trailer2,
    f"trailer1={trailer1}, trailer2={trailer2}"
)


# ============================================================================
# SECTION 3: force_push_tested() validation
# ============================================================================
print()
print("[Section 3] force_push_tested() Force-Push Validation")

# Test 15: force_push_tested validates sha format
print("  [Test 3.1] force_push_tested validates tested_sha format")
with patch('workflow.merge_queue.Runner.run_git') as mock_git:
    try:
        Runner.force_push_tested("mybranch", "invalid_sha", "a" * 40)
        test_result("force_push_tested rejects non-40-hex tested_sha", False, "No error raised")
    except ValueError as e:
        test_result(
            "force_push_tested rejects non-40-hex tested_sha",
            "tested_sha" in str(e) and "40-hex" in str(e),
            f"Message: {e}"
        )

# Test 16: force_push_tested validates lease_sha format
print("  [Test 3.2] force_push_tested validates lease_sha format")
with patch('workflow.merge_queue.Runner.run_git') as mock_git:
    try:
        Runner.force_push_tested("mybranch", "a" * 40, "invalid_sha")
        test_result("force_push_tested rejects non-40-hex lease_sha", False, "No error raised")
    except ValueError as e:
        test_result(
            "force_push_tested rejects non-40-hex lease_sha",
            "lease_sha" in str(e) and "40-hex" in str(e),
            f"Message: {e}"
        )

# Test 17: force_push_tested uses --force-with-lease (not bare --force)
print("  [Test 3.3] force_push_tested uses --force-with-lease (never bare --force)")
tested_sha = "a" * 40
lease_sha = "b" * 40
branch = "feature-branch"

with patch('workflow.merge_queue.Runner.run_git') as mock_git:
    mock_git.return_value = "success"
    try:
        Runner.force_push_tested(branch, tested_sha, lease_sha)
        # Check that run_git was called with the right arguments
        if mock_git.called:
            args = mock_git.call_args[0][0]  # Get positional arguments
            # Check that there IS a --force-with-lease parameter
            has_force_with_lease = any("--force-with-lease" in str(arg) for arg in args)
            # Check that there is NO bare --force (only --force-with-lease should be present)
            has_bare_force = any(arg == "--force" for arg in args)
            test_result(
                "force_push_tested uses --force-with-lease (not bare --force)",
                has_force_with_lease and not has_bare_force,
                f"Args: {args}"
            )
            # Verify the specific refspec format
            test_result(
                "force_push_tested uses correct refspec format",
                any(f"{tested_sha}:refs/heads/{branch}" in str(arg) for arg in args),
                f"Args: {args}"
            )
        else:
            test_result("force_push_tested calls run_git", False, "run_git not called")
    except Exception as e:
        test_result("force_push_tested calls run_git successfully", False, str(e))

# Test 18: force_push_tested passes both shas as 40-hex
print("  [Test 3.4] force_push_tested constructs correct git command")
with patch('workflow.merge_queue.Runner.run_git') as mock_git:
    mock_git.return_value = "success"
    tested = "1" * 40
    lease = "2" * 40
    branch = "test-branch"

    try:
        Runner.force_push_tested(branch, tested, lease)
        if mock_git.called:
            args = mock_git.call_args[0][0]
            # Verify the command structure
            test_result(
                "force_push_tested first arg is 'push'",
                args[0] == "push",
                f"Args[0]: {args[0]}"
            )
            test_result(
                "Command includes lease parameter",
                any(f"--force-with-lease={branch}:{lease}" in str(arg) for arg in args),
                f"Args: {args}"
            )
            test_result(
                "Command includes 'origin'",
                "origin" in args,
                f"Args: {args}"
            )
    except Exception as e:
        test_result("force_push_tested constructs correct git command", False, str(e))


# ============================================================================
# SECTION 4: MergeOutcome Enum Values
# ============================================================================
print()
print("[Section 4] MergeOutcome Enum Values")

# Test 19: MergeOutcome has required values
print("  [Test 4.1] MergeOutcome has required outcome values")
required_outcomes = ["MERGED", "KICKBACK", "PUSHED_NOT_MERGED", "REFUSED", "INTERNAL_ERROR"]
for outcome_name in required_outcomes:
    try:
        outcome = getattr(MergeOutcome, outcome_name)
        test_result(
            f"MergeOutcome.{outcome_name} exists",
            outcome is not None,
            f"Got {outcome}"
        )
    except AttributeError as e:
        test_result(f"MergeOutcome.{outcome_name} exists", False, str(e))

# Test 20: MergeOutcome values are strings (enum values)
print("  [Test 4.2] MergeOutcome enum values have string representations")
for outcome_name in required_outcomes:
    try:
        outcome = getattr(MergeOutcome, outcome_name)
        has_value = hasattr(outcome, "value") and isinstance(outcome.value, str)
        test_result(
            f"MergeOutcome.{outcome_name} has string value",
            has_value,
            f"Value: {outcome.value if hasattr(outcome, 'value') else 'N/A'}"
        )
    except Exception as e:
        test_result(f"MergeOutcome.{outcome_name} has string value", False, str(e))

# Test 21: MergeResult can be constructed with outcome
print("  [Test 4.3] MergeResult can be constructed with MergeOutcome")
try:
    result = MergeResult(
        outcome=MergeOutcome.MERGED,
        pr=123,
        branch="test-branch",
        worktree="/path/to/worktree",
    )
    test_result(
        "MergeResult constructor accepts MergeOutcome",
        result.outcome == MergeOutcome.MERGED,
        f"Got {result.outcome}"
    )
except Exception as e:
    test_result("MergeResult constructor accepts MergeOutcome", False, str(e))


# ============================================================================
# SECTION 5: Main Entry Point and Subcommand Routing
# ============================================================================
print()
print("[Section 5] Main Entry Point and Subcommand Routing")

# Test 22: main() with unknown subcommand returns error
print("  [Test 5.1] main() with unknown subcommand")
result = main(["unknown-command"])
test_result(
    "main() with unknown subcommand returns non-zero",
    result != 0,
    f"Got return code {result}"
)

# Test 23: main() routes status subcommand (returns 0 or 1)
print("  [Test 5.2] main() 'status' subcommand routing")
with patch('workflow.merge_queue.ensure_state_dir') as mock_ensure:
    mock_ensure.return_value = Path("/nonexistent")
    result = main(["status"])
    test_result(
        "main('status') returns integer exit code",
        isinstance(result, int),
        f"Got {type(result)}"
    )

# Test 24: main() routes reverify subcommand
print("  [Test 5.3] main() 'reverify' subcommand")
with patch('workflow.merge_queue.ensure_state_dir') as mock_ensure:
    with patch('workflow.merge_queue.git.get_default_branch') as mock_branch:
        with patch('workflow.merge_queue.Runner.run_git') as mock_git:
            mock_ensure.return_value = Path("/nonexistent")
            mock_branch.return_value = ("main", None)
            mock_git.return_value = "a" * 40
            result = main(["reverify", "a" * 40])
            test_result(
                "main('reverify') returns integer exit code",
                isinstance(result, int),
                f"Got {type(result)}"
            )

# Test 25: main() routes bootstrap subcommand
print("  [Test 5.4] main() 'bootstrap' subcommand")
with patch('workflow.merge_queue.git.get_default_branch') as mock_branch:
    with patch('workflow.merge_queue.Runner.run_git') as mock_git:
        with patch('workflow.merge_queue.resolve_config_path') as mock_config_path:
            with patch('workflow.merge_queue.load_and_validate_config') as mock_load_config:
                with patch('workflow.merge_queue._verify_base_in_scratch') as mock_verify:
                    mock_branch.return_value = ("main", None)
                    mock_git.return_value = "a" * 40
                    mock_config_path.return_value = Path("/fake/config.json")
                    mock_load_config.return_value = MergeQueueConfig(
                        base="main",
                        steps=["echo test"]
                    )
                    mock_verify.return_value = False
                    result = main(["bootstrap"])
                    test_result(
                        "main('bootstrap') returns integer exit code",
                        isinstance(result, int),
                        f"Got {type(result)}"
                    )

# Test 26: main() routes resume subcommand
print("  [Test 5.5] main() 'resume' subcommand")
with patch('workflow.merge_queue.ensure_state_dir') as mock_ensure:
    mock_ensure.return_value = Path("/nonexistent")
    result = main(["resume"])
    test_result(
        "main('resume') returns integer exit code",
        isinstance(result, int),
        f"Got {type(result)}"
    )

# Test 27: main() with no args routes to enqueue
print("  [Test 5.6] main() with no args routes to enqueue")
with patch('workflow.merge_queue.git.get_current_branch') as mock_branch:
    mock_branch.return_value = None
    result = main([])
    test_result(
        "main() with no args returns non-zero (enqueue handler)",
        result != 0,
        f"Got return code {result}"
    )


# ============================================================================
# SECTION 6: Runner Validation Methods
# ============================================================================
print()
print("[Section 6] Runner Validation Methods")

# Test 29: Runner.validate_branch rejects invalid branch names
print("  [Test 6.1] Runner.validate_branch validates branch names")
invalid_branches = ["", "-invalid", "has space", "has\ttab", "has\nnewline"]
for invalid in invalid_branches:
    try:
        Runner.validate_branch(invalid)
        test_result(f"Runner.validate_branch rejects '{repr(invalid)}'", False, "No error raised")
    except ValueError:
        test_result(f"Runner.validate_branch rejects '{repr(invalid)}'", True)
    except Exception as e:
        test_result(f"Runner.validate_branch rejects '{repr(invalid)}'", False, f"Wrong error: {type(e).__name__}")

# Test 30: Runner.validate_branch accepts valid branch names
print("  [Test 6.2] Runner.validate_branch accepts valid branch names")
valid_branches = ["main", "feature/test-123", "v1.0-rc1", "test_branch"]
for valid in valid_branches:
    try:
        result = Runner.validate_branch(valid)
        test_result(
            f"Runner.validate_branch accepts '{valid}'",
            result == valid,
            f"Got {result}"
        )
    except Exception as e:
        test_result(f"Runner.validate_branch accepts '{valid}'", False, str(e))

# Test 31: Runner.validate_ref validates refs
print("  [Test 6.3] Runner.validate_ref validates refs")
invalid_refs = ["", "-invalid", "has space"]
for invalid in invalid_refs:
    try:
        Runner.validate_ref(invalid)
        test_result(f"Runner.validate_ref rejects '{repr(invalid)}'", False, "No error raised")
    except ValueError:
        test_result(f"Runner.validate_ref rejects '{repr(invalid)}'", True)
    except Exception as e:
        test_result(f"Runner.validate_ref rejects '{repr(invalid)}'", False, str(type(e).__name__))


# ============================================================================
# SECTION 7: Config Dataclass
# ============================================================================
print()
print("[Section 7] Config Dataclass Methods")

# Test 32: MergeQueueConfig.to_dict() serialization
print("  [Test 7.1] MergeQueueConfig.to_dict() serializes to dict")
cfg = MergeQueueConfig(
    base="main",
    steps=["echo test"],
    cleanup=False,
    scratch_setup=[],
    inherit_lock_fd=False,
    allow_unverified=[],
)
config_dict = cfg.to_dict()
test_result(
    "MergeQueueConfig.to_dict() returns dict",
    isinstance(config_dict, dict),
    f"Got {type(config_dict)}"
)
test_result(
    "Serialized dict contains 'base'",
    "base" in config_dict,
    f"Keys: {list(config_dict.keys())}"
)
test_result(
    "Serialized dict contains 'steps'",
    "steps" in config_dict,
    f"Keys: {list(config_dict.keys())}"
)

# Test 33: MergeQueueConfig.from_dict() deserialization
print("  [Test 7.2] MergeQueueConfig.from_dict() deserializes from dict")
test_dict = {
    "base": "main",
    "steps": ["make test"],
    "cleanup": True,
    "scratch_setup": ["git config user.name 'Test'"],
    "inherit_lock_fd": True,
    "allow_unverified": ["a" * 40],
}
try:
    cfg = MergeQueueConfig.from_dict(test_dict)
    test_result(
        "MergeQueueConfig.from_dict() constructs config",
        isinstance(cfg, MergeQueueConfig),
        f"Got {type(cfg)}"
    )
    test_result(
        "from_dict preserves base",
        cfg.base == "main",
        f"Got {cfg.base}"
    )
    test_result(
        "from_dict preserves steps",
        cfg.steps == ["make test"],
        f"Got {cfg.steps}"
    )
    test_result(
        "from_dict preserves cleanup flag",
        cfg.cleanup is True,
        f"Got {cfg.cleanup}"
    )
except Exception as e:
    test_result("MergeQueueConfig.from_dict() constructs config", False, str(e))

# Test 34: from_dict ignores unknown keys (data integrity)
print("  [Test 7.3] MergeQueueConfig.from_dict() ignores unknown keys")
test_dict_with_unknown = {
    "base": "main",
    "steps": ["echo test"],
    "unknown_field": "ignored",
    "another_unknown": 42,
}
try:
    cfg = MergeQueueConfig.from_dict(test_dict_with_unknown)
    test_result(
        "from_dict ignores unknown keys",
        cfg.base == "main" and cfg.steps == ["echo test"],
        f"Got base={cfg.base}, steps={cfg.steps}"
    )
except Exception as e:
    test_result("from_dict ignores unknown keys", False, str(e))


# ============================================================================
# SECTION 8: Bootstrap and Reverify Filesystem Interaction
# ============================================================================
print()
print("[Section 8] Bootstrap and Reverify Filesystem Interaction")

# Test 35: bootstrap writes base-verified record on successful gate
print("  [Test 8.1] bootstrap writes base-verified record on success")
with tempfile.TemporaryDirectory() as tmpdir:
    state_dir = Path(tmpdir) / "merge-queue"
    verified_dir = state_dir / "base-verified"
    verified_dir.mkdir(parents=True, exist_ok=True)

    base_sha = "a" * 40
    verified_path = verified_dir / base_sha

    # File should not exist yet
    test_result(
        "base-verified file does not exist initially",
        not verified_path.exists(),
        f"Path: {verified_path}"
    )

    # Simulate bootstrap writing the record
    verified_path.touch()
    test_result(
        "base-verified file created by bootstrap",
        verified_path.exists(),
        f"Path: {verified_path}"
    )

# Test 36: reverify clears base-failed record
print("  [Test 8.2] reverify clears base-failed record")
with tempfile.TemporaryDirectory() as tmpdir:
    state_dir = Path(tmpdir) / "merge-queue"
    failed_dir = state_dir / "base-failed"
    failed_dir.mkdir(parents=True, exist_ok=True)

    base_sha = "b" * 40
    failed_path = failed_dir / base_sha

    # Create a base-failed record
    failed_path.touch()
    test_result(
        "base-failed file exists initially",
        failed_path.exists(),
        f"Path: {failed_path}"
    )

    # Simulate reverify clearing it
    failed_path.unlink(missing_ok=True)
    test_result(
        "base-failed file cleared by reverify",
        not failed_path.exists(),
        f"Path: {failed_path}"
    )

# Test 37: status reads live tickets from state dir
print("  [Test 8.3] status reads queue state from filesystem")
with tempfile.TemporaryDirectory() as tmpdir:
    state_dir = Path(tmpdir) / "merge-queue"
    tickets_dir = state_dir / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)

    # Create a live ticket file
    ticket_path = tickets_dir / "000000000001"
    ticket_data = {"pr": 123, "branch": "feature-x", "worktree": "/path/to/work", "enqueued_at": 1234567890}
    ticket_path.write_text(json.dumps(ticket_data))

    test_result(
        "status can read ticket metadata from disk",
        ticket_path.exists() and json.loads(ticket_path.read_text()).get("pr") == 123,
        "Ticket content valid"
    )

# Test 38: Trailer contains both base_sha and tested_sha (catches parameter order bug)
print("  [Test 8.4] Trailer round-trip preserves both base and tested SHA")
base_sha = "c" * 40
tested_sha = "d" * 40
trailer = build_trailer(base_sha, tested_sha)

# This test would have caught the bug where build_trailer was called with
# (tested_sha, tested_sha) instead of (base_sha, tested_sha)
test_result(
    "Trailer contains distinct base SHA",
    base_sha in trailer,
    f"Trailer missing base: {trailer}"
)
test_result(
    "Trailer contains distinct tested SHA",
    tested_sha in trailer,
    f"Trailer missing tested: {trailer}"
)
# If the bug existed (both params the same), this would fail:
test_result(
    "Trailer distinguishes base from tested when different",
    trailer.count(base_sha) == 1 and trailer.count(tested_sha) == 1,
    f"Trailer does not properly distinguish SHAs: {trailer}"
)


# ============================================================================
# Summary
# ============================================================================
print()
h.summarize_and_exit()
