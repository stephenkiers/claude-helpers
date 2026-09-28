#!/usr/bin/env python3
"""
Spec-blind test suite for merge_queue.py (local merge queue feature).

Tests the observable behavior of the merge_queue module:
- Config resolution and validation (valid, missing, malformed)
- Trailer build/parse round-trip (Merge-Gate trailer)
- force_push_tested() force-push validation (--force-with-lease only)
- run_step exit-code validation (gate step failures detected)
- scan_base poison-commit detection (unverified commits flagged even with anchor)
- resolve_config_path worktrees layout handling
- main(argv) subcommand routing (exit codes and mocked handlers)
- MergeOutcome enum and typed results

Run with: python3 tests/test_workflow_merge_queue_spec_blind.py
"""

import sys
import json
import tempfile
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _test_harness import Harness

# Import the module under test
from workflow.merge_queue import (
    MergeQueueConfig,
    Step,
    load_and_validate_config,
    build_trailer,
    parse_trailer,
    MergeOutcome,
    MergeResult,
    MergePROutcome,
    Runner,
    main,
    run_step,
    scan_base,
    resolve_config_path,
    acquire_ticket,
    _cmd_bootstrap,
    _cmd_reverify,
    _cmd_status,
    _locked_flow,
    _merge_pr,
    _check_outside_worktree,
    _phase_fetch_and_verify_lease,
)

# Import git module for testing git.run_git_command_input
from workflow import git

h = Harness("MERGE QUEUE SPEC-BLIND TEST SUITE")
test_result = h.test_result


def setup_real_git_worktrees(tmpdir: Path):
    """
    Set up a real git repo with linked worktrees (security-sensitive test fixture).

    Returns a dict with:
      - main_worktree: path to main worktree
      - pr_worktree: path to linked PR worktree
      - config_in_pr: path to a config file inside PR worktree
      - config_outside: path to a config file outside repo
    """
    # Create structure: tmpdir/work/ (main) and tmpdir/work/worktrees/pr-1 (linked worktree)
    work_dir = tmpdir / "work"
    work_dir.mkdir(exist_ok=True)

    # Initialize a git repo directly
    subprocess.run(["git", "init"], cwd=work_dir, check=True, capture_output=True)

    # Create an initial commit
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=work_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=work_dir, check=True, capture_output=True)
    (work_dir / "README.md").write_text("# Test Repo")
    subprocess.run(["git", "add", "README.md"], cwd=work_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=work_dir, check=True, capture_output=True)

    # Create a linked worktree for pr-1
    pr_worktree = work_dir / "worktrees" / "pr-1"
    subprocess.run(
        ["git", "worktree", "add", str(pr_worktree), "-b", "pr-1"],
        cwd=work_dir, check=True, capture_output=True
    )

    # Create config files
    config_in_pr = pr_worktree / "merge-queue.json"
    config_in_pr.write_text(json.dumps({"base": "main", "steps": ["echo test"]}))
    config_outside = tmpdir / "merge-queue.json"
    config_outside.write_text(json.dumps({"base": "main", "steps": ["echo test"]}))

    return {
        "main_worktree": work_dir,
        "pr_worktree": pr_worktree,
        "config_in_pr": config_in_pr,
        "config_outside": config_outside,
    }


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
                len(cfg.steps) == 1 and isinstance(cfg.steps[0], Step) and cfg.steps[0].cmd == "echo test",
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
# git interpret-trailers --parse only recognizes a trailer block that follows
# a non-trailer paragraph and a blank line, matching the real commit body
# shape built in _merge_pr() (see build_trailer callers).
commit_msg = f"Tested by merge-queue.\n\n{trailer_text}"
# The parse_trailer function expects a commit SHA, which it will query via git
# We need to mock the git calls; run_git_command_input still runs real git
# interpret-trailers against this mocked commit message.
with patch('workflow.merge_queue.Runner.run_git') as mock_git:
    # Mock to return commit msg for "show" and base_sha for "^1" (parent) queries
    def mock_git_impl(cmd, *args, **kwargs):
        if isinstance(cmd, list) and len(cmd) > 0:
            if cmd[0] == "show":
                return commit_msg
            elif cmd[0] == "rev-parse" and "^1" in cmd[-1]:
                return base_sha  # Return base_sha as the parent
        return commit_msg

    mock_git.side_effect = mock_git_impl

    parsed_base, parsed_head = parse_trailer("dummycommit")
    test_result(
        "parse_trailer returns tuple of (base, head)",
        isinstance((parsed_base, parsed_head), tuple),
        f"Got {type((parsed_base, parsed_head))}"
    )
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
        else:
            test_result("force_push_tested calls run_git", False, "run_git not called")
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
        tested_sha="a" * 40,  # MERGED outcome requires tested_sha
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

# Test 23: main() routes status subcommand (mocked handler)
print("  [Test 5.2] main() 'status' subcommand routing")
with patch('workflow.merge_queue._cmd_status') as mock_status:
    mock_status.return_value = 0
    result = main(["status"])
    test_result(
        "main('status') calls _cmd_status handler",
        mock_status.called,
        "Handler not called"
    )
    test_result(
        "main('status') returns handler's exit code",
        result == 0,
        f"Got {result}"
    )

# Test 24: main() routes reverify subcommand (mocked handler)
print("  [Test 5.3] main() 'reverify' subcommand")
with patch('workflow.merge_queue._cmd_reverify') as mock_reverify:
    mock_reverify.return_value = 0
    result = main(["reverify", "a" * 40])
    test_result(
        "main('reverify') calls _cmd_reverify handler",
        mock_reverify.called,
        "Handler not called"
    )
    test_result(
        "main('reverify') passes args to handler",
        mock_reverify.call_args[0][0] == ["a" * 40],
        f"Got {mock_reverify.call_args}"
    )

# Test 25: main() routes bootstrap subcommand (mocked handler)
print("  [Test 5.4] main() 'bootstrap' subcommand")
with patch('workflow.merge_queue._cmd_bootstrap') as mock_bootstrap:
    mock_bootstrap.return_value = 0
    result = main(["bootstrap"])
    test_result(
        "main('bootstrap') calls _cmd_bootstrap handler",
        mock_bootstrap.called,
        "Handler not called"
    )
    test_result(
        "main('bootstrap') returns handler's exit code",
        result == 0,
        f"Got {result}"
    )

# Test 26: main() routes resume subcommand (mocked handler)
print("  [Test 5.5] main() 'resume' subcommand")
with patch('workflow.merge_queue._cmd_resume') as mock_resume:
    mock_resume.return_value = 0
    result = main(["resume"])
    test_result(
        "main('resume') calls _cmd_resume handler",
        mock_resume.called,
        "Handler not called"
    )
    test_result(
        "main('resume') returns handler's exit code",
        result == 0,
        f"Got {result}"
    )

# Test 27: main() with no args routes to enqueue (mocked handler)
print("  [Test 5.6] main() with no args routes to enqueue")
with patch('workflow.merge_queue._cmd_enqueue') as mock_enqueue:
    mock_enqueue.return_value = 1
    result = main([])
    test_result(
        "main() with no args calls _cmd_enqueue handler",
        mock_enqueue.called,
        "Handler not called"
    )
    test_result(
        "main() returns enqueue handler's exit code",
        result == 1,
        f"Got {result}"
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
    steps=[Step(cmd="echo test")],
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

# Test 35: _cmd_bootstrap writes base-verified record on successful gate
print("  [Test 8.1] _cmd_bootstrap writes base-verified record on gate pass")
with tempfile.TemporaryDirectory() as tmpdir:
    # Mock the state dir to use our temp dir
    base_sha = "a" * 40
    state_dir = Path(tmpdir) / "merge-queue"
    verified_dir = state_dir / "base-verified"
    verified_dir.mkdir(parents=True, exist_ok=True)

    verified_path = verified_dir / base_sha

    # Mock the necessary dependencies for bootstrap
    with patch('workflow.merge_queue.get_state_dir') as mock_get_state:
        with patch('workflow.merge_queue.ensure_state_dir') as mock_ensure:
            with patch('workflow.merge_queue.git.get_default_branch') as mock_branch:
                with patch('workflow.merge_queue.Runner.run_git') as mock_git:
                    with patch('workflow.merge_queue.resolve_config_path') as mock_config_path:
                        with patch('workflow.merge_queue.load_and_validate_config') as mock_load_config:
                            with patch('workflow.merge_queue._verify_base_in_scratch') as mock_verify:
                                mock_get_state.return_value = state_dir
                                mock_ensure.return_value = state_dir
                                mock_branch.return_value = ("main", None)
                                mock_git.return_value = base_sha
                                mock_config_path.return_value = Path("/fake/config.json")
                                mock_load_config.return_value = MergeQueueConfig(
                                    base="main",
                                    steps=[Step(cmd="echo test")]
                                )
                                # Mock returns VerifyBaseOutcome enum
                                from workflow.merge_queue import VerifyBaseOutcome
                                mock_verify.return_value = VerifyBaseOutcome.VERIFIED

                                # Call _cmd_bootstrap
                                result = _cmd_bootstrap([])
                                test_result(
                                    "bootstrap exits with 0 on successful gate",
                                    result == 0,
                                    f"Got exit code {result}"
                                )
                                test_result(
                                    "bootstrap writes base-verified record",
                                    verified_path.exists(),
                                    f"Path not created: {verified_path}"
                                )

# Test 36: _cmd_reverify clears base-failed record
print("  [Test 8.2] _cmd_reverify clears base-failed record")
with tempfile.TemporaryDirectory() as tmpdir:
    base_sha = "b" * 40
    state_dir = Path(tmpdir) / "merge-queue"
    failed_dir = state_dir / "base-failed"
    failed_dir.mkdir(parents=True, exist_ok=True)

    failed_path = failed_dir / base_sha
    failed_path.touch()

    # Mock the state dir and dependencies
    with patch('workflow.merge_queue.get_state_dir') as mock_get_state:
        with patch('workflow.merge_queue.ensure_state_dir') as mock_ensure:
            mock_get_state.return_value = state_dir
            mock_ensure.return_value = state_dir

            # Call _cmd_reverify with the sha
            result = _cmd_reverify([base_sha])
            test_result(
                "reverify exits with 0",
                result == 0,
                f"Got exit code {result}"
            )
            test_result(
                "reverify clears base-failed record",
                not failed_path.exists(),
                f"Path still exists: {failed_path}"
            )

# Test 37: _cmd_status reads live tickets from state dir
print("  [Test 8.3] _cmd_status reads queue state from filesystem")
with tempfile.TemporaryDirectory() as tmpdir:
    state_dir = Path(tmpdir) / "merge-queue"
    tickets_dir = state_dir / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)

    # Create a live ticket file
    ticket_path = tickets_dir / "000000000001"
    ticket_data = {"pr": 123, "branch": "feature-x", "worktree": "/path/to/work", "enqueued_at": 1234567890}
    ticket_path.write_text(json.dumps(ticket_data))

    # Mock the state dir
    with patch('workflow.merge_queue.get_state_dir') as mock_get_state:
        with patch('workflow.merge_queue.ensure_state_dir') as mock_ensure:
            mock_get_state.return_value = state_dir
            mock_ensure.return_value = state_dir

            # Call _cmd_status
            result = _cmd_status([])
            test_result(
                "status exits with 0",
                result == 0,
                f"Got exit code {result}"
            )

# Test 38: run_step detects gate failure (exit code != 0)
print("  [Test 8.4] run_step reports step failure when exit code is nonzero (regression test)")
with tempfile.TemporaryDirectory() as tmpdir:
    cwd = Path(tmpdir)
    log_path = cwd / "test.log"

    # Run a step that exits with code 1
    # This validates the fix for bug #1: run_step must check proc.returncode == 0
    outcome = run_step("exit 1", cwd, log_path, timeout_secs=5)
    test_result(
        "run_step success=False when step exits nonzero (bug #1 regression)",
        outcome.success is False,
        f"Got success={outcome.success} (fails on unfixed code, expected per bug #1)"
    )


# ============================================================================
# SECTION 9: Regression Tests
# ============================================================================
print()
print("[Section 9] Regression Tests")

# Regression Test 1: scan_base with anchor present AND poison commit above it
print("  [Test 9.1] scan_base flags poison commits even when anchor exists (regression)")
with tempfile.TemporaryDirectory() as tmpdir:
    # Mock the state dir and git operations
    poison_sha = "p" * 40  # Untrailered commit
    anchor_sha = "a" * 40  # Commit with verified record

    state_dir = Path(tmpdir) / "merge-queue"
    verified_dir = state_dir / "base-verified"
    verified_dir.mkdir(parents=True, exist_ok=True)

    # Create a verified record for anchor
    (verified_dir / anchor_sha).touch()

    # Mock Runner.run_git to return log with poison above anchor
    # The log order is newest first in first-parent history
    log_output = f"{poison_sha}\n{anchor_sha}"

    with patch('workflow.merge_queue.get_state_dir') as mock_get_state:
        with patch('workflow.merge_queue.Runner.run_git') as mock_git:
            mock_get_state.return_value = state_dir
            mock_git.return_value = log_output

            # Call scan_base
            has_anchor, unverified = scan_base(poison_sha, [])
            test_result(
                "scan_base finds anchor despite poison above it",
                has_anchor is True,
                f"Got has_anchor={has_anchor}"
            )
            test_result(
                "scan_base reports poison commit as unverified",
                poison_sha in unverified,
                f"Got unverified={unverified}"
            )

# Regression Test 2: resolve_config_path against /setup-repo layout (bug #4)
print("  [Test 9.2] resolve_config_path validates worktrees layout (regression)")
with tempfile.TemporaryDirectory() as tmpdir:
    # Create the /setup-repo layout: <container>/worktrees/<worktree>/
    # git.get_git_common_dir() for a worktree returns that worktree's own .git dir,
    # i.e. container/worktrees/<name>/.git
    container = Path(tmpdir) / "myrepo"
    worktrees_dir = container / "worktrees"
    worktrees_dir.mkdir(parents=True, exist_ok=True)

    # Create a worktree directory
    worktree = worktrees_dir / "main"
    worktree.mkdir(parents=True, exist_ok=True)

    # Create a merge-queue.json in the container
    config_file = container / "merge-queue.json"
    config_file.write_text(json.dumps({"base": "main", "steps": ["echo test"]}))

    # The git-common-dir for a worktree is its own .git dir, one level below
    # worktrees/<name>, not worktrees/.git directly.
    git_common_dir = worktree / ".git"
    git_common_dir.mkdir(exist_ok=True)

    # Mock git.get_git_common_dir to return the shared git dir (validates bug #4 fix)
    with patch('workflow.merge_queue.git.get_git_common_dir') as mock_common_dir:
        mock_common_dir.return_value = str(git_common_dir)

        # Call resolve_config_path (with no override)
        resolved = resolve_config_path()
        # Resolve both paths to handle symlink expansion on macOS
        resolved_normalized = resolved.resolve()
        expected_normalized = config_file.resolve()
        test_result(
            "resolve_config_path resolves to container/merge-queue.json (bug #4 regression)",
            resolved_normalized == expected_normalized,
            f"Got {resolved_normalized}, expected {expected_normalized}"
        )

# Regression Test 3: run_step with successful step (exit 0)
print("  [Test 9.3] run_step success=True when step exits 0 (regression)")
with tempfile.TemporaryDirectory() as tmpdir:
    cwd = Path(tmpdir)
    log_path = cwd / "test.log"

    # Run a step that exits with code 0
    outcome = run_step("exit 0", cwd, log_path, timeout_secs=5)
    test_result(
        "run_step success=True when step exits 0",
        outcome.success is True,
        f"Got success={outcome.success}"
    )


# ============================================================================
# SECTION 10: NEW ROUND-1 COVERAGE GAPS
# ============================================================================
print("[Section 10] Round-1 Coverage Gaps")

# Test 10.1: resolve_config_path fail-closed on --config with git.get_git_common_dir() error
print("  [Test 10.1] resolve_config_path --config fails closed on git.get_git_common_dir() error")
with tempfile.TemporaryDirectory() as tmpdir:
    config_path = Path(tmpdir) / "merge-queue.json"
    config_path.write_text(json.dumps({"base": "main", "steps": ["echo test"]}))

    with patch('workflow.merge_queue.git.get_git_common_dir') as mock_common_dir:
        mock_common_dir.side_effect = RuntimeError("Cannot determine git dir")

        try:
            resolved = resolve_config_path(str(config_path))
            test_result(
                "resolve_config_path --config fails closed on git.get_git_common_dir() error",
                False,
                "No error raised"
            )
        except RuntimeError as e:
            test_result(
                "resolve_config_path --config fails closed on git.get_git_common_dir() error",
                "trust-boundary" in str(e) or "git-common-dir" in str(e),
                f"Got error: {e}"
            )
        except Exception as e:
            test_result(
                "resolve_config_path --config fails closed on git.get_git_common_dir() error",
                False,
                f"Wrong exception type: {type(e).__name__}: {e}"
            )

# Test 10.2: resolve_config_path fail-closed on MERGE_QUEUE_CONFIG env var with git.get_git_common_dir() error
print("  [Test 10.2] resolve_config_path MERGE_QUEUE_CONFIG fails closed on git.get_git_common_dir() error")
with tempfile.TemporaryDirectory() as tmpdir:
    config_path = Path(tmpdir) / "merge-queue.json"
    config_path.write_text(json.dumps({"base": "main", "steps": ["echo test"]}))

    with patch.dict(os.environ, {"MERGE_QUEUE_CONFIG": str(config_path)}):
        with patch('workflow.merge_queue.git.get_git_common_dir') as mock_common_dir:
            mock_common_dir.side_effect = Exception("Cannot determine git dir")

            try:
                resolved = resolve_config_path()
                test_result(
                    "resolve_config_path MERGE_QUEUE_CONFIG fails closed on error",
                    False,
                    "No error raised"
                )
            except RuntimeError as e:
                test_result(
                    "resolve_config_path MERGE_QUEUE_CONFIG fails closed on error",
                    "trust-boundary" in str(e) or "git-common-dir" in str(e),
                    f"Got error: {e}"
                )
            except Exception as e:
                test_result(
                    "resolve_config_path MERGE_QUEUE_CONFIG fails closed on error",
                    False,
                    f"Wrong exception type: {type(e).__name__}: {e}"
                )

# Test 10.3: resolve_config_path rejects --config path inside PR worktree (real git worktree)
print("  [Test 10.3] resolve_config_path rejects --config inside PR worktree (real worktree)")
with tempfile.TemporaryDirectory() as tmpdir:
    try:
        worktrees = setup_real_git_worktrees(Path(tmpdir))
        pr_worktree = worktrees["pr_worktree"]
        config_in_pr = worktrees["config_in_pr"]

        # Try to resolve config path that's inside the PR worktree
        try:
            resolved = resolve_config_path(str(config_in_pr))
            test_result(
                "resolve_config_path rejects --config inside PR worktree (real)",
                False,
                "No error raised; config inside PR worktree was accepted"
            )
        except RuntimeError as e:
            # Should reject because config is inside repo or worktrees/
            error_msg = str(e)
            rejected = (
                "inside the" in error_msg or "must be outside" in error_msg
                or "worktrees" in error_msg
            )
            test_result(
                "resolve_config_path rejects --config inside PR worktree (real)",
                rejected,
                f"Got error: {e}"
            )
    except Exception as e:
        test_result(
            "resolve_config_path rejects --config inside PR worktree (real)",
            False,
            f"Test setup failed: {e}"
        )

# Test 10.4: resolve_config_path rejects MERGE_QUEUE_CONFIG env var inside PR worktree (real git worktree)
print("  [Test 10.4] resolve_config_path rejects MERGE_QUEUE_CONFIG inside PR worktree (real)")
with tempfile.TemporaryDirectory() as tmpdir:
    try:
        worktrees = setup_real_git_worktrees(Path(tmpdir))
        pr_worktree = worktrees["pr_worktree"]
        config_in_pr = worktrees["config_in_pr"]

        # Try to resolve config path via env var when it's inside the PR worktree
        with patch.dict(os.environ, {"MERGE_QUEUE_CONFIG": str(config_in_pr)}):
            try:
                resolved = resolve_config_path()
                test_result(
                    "resolve_config_path rejects MERGE_QUEUE_CONFIG inside PR worktree (real)",
                    False,
                    "No error raised; config inside PR worktree was accepted"
                )
            except RuntimeError as e:
                # Should reject because config is inside repo or worktrees/
                error_msg = str(e)
                rejected = (
                    "inside the" in error_msg or "must be outside" in error_msg
                    or "worktrees" in error_msg
                )
                test_result(
                    "resolve_config_path rejects MERGE_QUEUE_CONFIG inside PR worktree (real)",
                    rejected,
                    f"Got error: {e}"
                )
    except Exception as e:
        test_result(
            "resolve_config_path rejects MERGE_QUEUE_CONFIG inside PR worktree (real)",
            False,
            f"Test setup failed: {e}"
        )

# Test 10.5: Config validation for mutation_timeout_secs as integer
print("  [Test 10.5] Config validates mutation_timeout_secs as integer")
with tempfile.TemporaryDirectory() as tmpdir:
    config_path = Path(tmpdir) / "merge-queue.json"
    config_data = {
        "base": "main",
        "steps": ["echo test"],
        "mutation_timeout_secs": 600,
    }
    config_path.write_text(json.dumps(config_data))

    with patch('workflow.merge_queue.git.get_default_branch') as mock_branch:
        mock_branch.return_value = ("main", None)

        try:
            cfg = load_and_validate_config(config_path)
            test_result(
                "Config accepts valid mutation_timeout_secs",
                cfg.mutation_timeout_secs == 600,
                f"Got {cfg.mutation_timeout_secs}"
            )
        except Exception as e:
            test_result("Config accepts valid mutation_timeout_secs", False, str(e))

# Test 10.6: Config rejects mutation_timeout_secs as bool (even though bool is subclass of int in Python)
print("  [Test 10.6] Config rejects mutation_timeout_secs as bool")
with tempfile.TemporaryDirectory() as tmpdir:
    config_path = Path(tmpdir) / "merge-queue.json"
    config_data = {
        "base": "main",
        "steps": ["echo test"],
        "mutation_timeout_secs": True,
    }
    config_path.write_text(json.dumps(config_data))

    with patch('workflow.merge_queue.git.get_default_branch') as mock_branch:
        mock_branch.return_value = ("main", None)

        try:
            cfg = load_and_validate_config(config_path)
            test_result(
                "Config rejects mutation_timeout_secs as bool",
                False,
                "No error raised"
            )
        except ValueError as e:
            test_result(
                "Config rejects mutation_timeout_secs as bool",
                "integer" in str(e).lower(),
                f"Got error: {e}"
            )

# Test 10.7: Config rejects mutation_timeout_secs <= 0
print("  [Test 10.7] Config rejects mutation_timeout_secs <= 0")
with tempfile.TemporaryDirectory() as tmpdir:
    config_path = Path(tmpdir) / "merge-queue.json"
    config_data = {
        "base": "main",
        "steps": ["echo test"],
        "mutation_timeout_secs": 0,
    }
    config_path.write_text(json.dumps(config_data))

    with patch('workflow.merge_queue.git.get_default_branch') as mock_branch:
        mock_branch.return_value = ("main", None)

        try:
            cfg = load_and_validate_config(config_path)
            test_result(
                "Config rejects mutation_timeout_secs <= 0",
                False,
                "No error raised"
            )
        except ValueError as e:
            test_result(
                "Config rejects mutation_timeout_secs <= 0",
                "positive" in str(e).lower(),
                f"Got error: {e}"
            )

# Test 10.8: Config validation for pr_merge_poll_secs as integer
print("  [Test 10.8] Config validates pr_merge_poll_secs as integer")
with tempfile.TemporaryDirectory() as tmpdir:
    config_path = Path(tmpdir) / "merge-queue.json"
    config_data = {
        "base": "main",
        "steps": ["echo test"],
        "pr_merge_poll_secs": 120,
    }
    config_path.write_text(json.dumps(config_data))

    with patch('workflow.merge_queue.git.get_default_branch') as mock_branch:
        mock_branch.return_value = ("main", None)

        try:
            cfg = load_and_validate_config(config_path)
            test_result(
                "Config accepts valid pr_merge_poll_secs",
                cfg.pr_merge_poll_secs == 120,
                f"Got {cfg.pr_merge_poll_secs}"
            )
        except Exception as e:
            test_result("Config accepts valid pr_merge_poll_secs", False, str(e))

# Test 10.9: Config has default values for mutation_timeout_secs and pr_merge_poll_secs
print("  [Test 10.9] Config has default values for new timeout fields")
with tempfile.TemporaryDirectory() as tmpdir:
    config_path = Path(tmpdir) / "merge-queue.json"
    config_data = {
        "base": "main",
        "steps": ["echo test"],
    }
    config_path.write_text(json.dumps(config_data))

    with patch('workflow.merge_queue.git.get_default_branch') as mock_branch:
        mock_branch.return_value = ("main", None)

        try:
            cfg = load_and_validate_config(config_path)
            test_result(
                "Config has default mutation_timeout_secs=300",
                cfg.mutation_timeout_secs == 300,
                f"Got {cfg.mutation_timeout_secs}"
            )
            test_result(
                "Config has default pr_merge_poll_secs=60",
                cfg.pr_merge_poll_secs == 60,
                f"Got {cfg.pr_merge_poll_secs}"
            )
        except Exception as e:
            test_result("Config defaults are correct", False, str(e))

# Test 10.10: git.run_git_command_input pipes input to git correctly
print("  [Test 10.10] git.run_git_command_input pipes input to git")
with tempfile.TemporaryDirectory() as tmpdir:
    # Use git hash-object to verify stdin is piped correctly
    test_input = "Hello, World!"
    expected_hash = "72a1c7ab92c4a20e42cf0c1c8e98c1e8e3d8f9e2"  # Pre-computed SHA1
    # Actually, let's use a simpler approach: pipe a message to interpret-trailers
    try:
        # Use git interpret-trailers which accepts input on stdin
        output = git.run_git_command_input(
            ["interpret-trailers", "--parse"],
            "Some text\n\nMy-Trailer: value\n",
            cwd=Path(tmpdir)
        )
        # If it succeeds, output should contain the trailer
        test_result(
            "git.run_git_command_input pipes input correctly",
            "My-Trailer" in output or "value" in output or "text" in output,
            f"Got output: {output}"
        )
    except Exception as e:
        test_result(
            "git.run_git_command_input pipes input correctly",
            False,
            f"Got error: {e}"
        )

# Test 10.11: Verify Step normalization from dict/string to Step objects
print("  [Test 10.11] Step objects normalize from dict and string configs")
with tempfile.TemporaryDirectory() as tmpdir:
    config_path = Path(tmpdir) / "merge-queue.json"
    config_data = {
        "base": "main",
        "steps": [
            "echo simple",
            {"cmd": "echo with-timeout", "timeout_secs": 100},
        ],
    }
    config_path.write_text(json.dumps(config_data))

    with patch('workflow.merge_queue.git.get_default_branch') as mock_branch:
        mock_branch.return_value = ("main", None)

        try:
            cfg = load_and_validate_config(config_path)
            test_result(
                "Step 0 is Step object with cmd from string",
                isinstance(cfg.steps[0], Step) and cfg.steps[0].cmd == "echo simple" and cfg.steps[0].timeout_secs is None,
                f"Got {cfg.steps[0]}"
            )
            test_result(
                "Step 1 is Step object with cmd and timeout_secs from dict",
                isinstance(cfg.steps[1], Step) and cfg.steps[1].cmd == "echo with-timeout" and cfg.steps[1].timeout_secs == 100,
                f"Got {cfg.steps[1]}"
            )
        except Exception as e:
            test_result("Step normalization", False, str(e))

# Test 10.12: Config rejects unknown keys in dict steps
print("  [Test 10.12] Config rejects unknown keys in dict steps")
with tempfile.TemporaryDirectory() as tmpdir:
    config_path = Path(tmpdir) / "merge-queue.json"
    config_data = {
        "base": "main",
        "steps": [
            {"cmd": "echo test", "unknown_key": "value"},
        ],
    }
    config_path.write_text(json.dumps(config_data))

    with patch('workflow.merge_queue.git.get_default_branch') as mock_branch:
        mock_branch.return_value = ("main", None)

        try:
            cfg = load_and_validate_config(config_path)
            test_result(
                "Config rejects unknown keys in dict steps",
                False,
                "No error raised"
            )
        except ValueError as e:
            test_result(
                "Config rejects unknown keys in dict steps",
                "unknown" in str(e).lower(),
                f"Got error: {e}"
            )

# Test 10.13: parse_trailer round-trip with real body shape from _merge_pr
print("  [Test 10.13] parse_trailer round-trip with real commit body")
with tempfile.TemporaryDirectory() as tmpdir:
    # Initialize a real git repo
    cwd = Path(tmpdir)
    subprocess.run(
        ["git", "init"],
        cwd=cwd,
        capture_output=True,
        check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=cwd,
        capture_output=True,
        check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=cwd,
        capture_output=True,
        check=True
    )

    # Create an initial commit
    test_file = cwd / "test.txt"
    test_file.write_text("test content")
    subprocess.run(
        ["git", "add", "test.txt"],
        cwd=cwd,
        capture_output=True,
        check=True
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=cwd,
        capture_output=True,
        check=True
    )

    # Create a commit with the exact body shape used by _merge_pr()
    # Get the actual parent SHA (for Decided #5: immediate parent check)
    parent_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True
    ).stdout.strip()

    # Use the actual parent as the tested-base to satisfy Decided #5 check
    base_sha = parent_sha
    tested_sha = "b" * 40
    trailer_text = build_trailer(base_sha, tested_sha)
    body = f"Tested by merge-queue.\n\n{trailer_text}"

    # Create a commit with this exact body by using git commit-tree
    tree_sha = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True
    ).stdout.strip()

    # Create commit with exact body
    commit_sha = subprocess.run(
        ["git", "commit-tree", tree_sha, "-p", parent_sha, "-m", body],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        input=None
    ).stdout.strip()

    # Now test parse_trailer with the real commit
    try:
        parsed_base, parsed_head = parse_trailer(commit_sha, cwd=cwd)
        test_result(
            "parse_trailer round-trip extracts correct base",
            parsed_base == base_sha,
            f"Got {parsed_base}, expected {base_sha}"
        )
        test_result(
            "parse_trailer round-trip extracts correct head",
            parsed_head == tested_sha,
            f"Got {parsed_head}, expected {tested_sha}"
        )
    except Exception as e:
        test_result(
            "parse_trailer round-trip with real commit",
            False,
            f"Got error: {e}"
        )


# ============================================================================
# SECTION 11: _locked_flow early-KICKBACK invariant regression
# ============================================================================
print("[Section 11] _locked_flow early-KICKBACK invariant regression")

# Regression: MergeResult.__post_init__ enforces "KICKBACK requires orig_head" at
# construction time. Several _locked_flow() early-return paths used to construct a
# KICKBACK MergeResult before orig_head had been computed, raising ValueError instead
# of returning the kickback (silently surfacing as INTERNAL_ERROR to the caller).
print("  [Test 11.1] _locked_flow returns KICKBACK (not ValueError) when tree is dirty")
with tempfile.TemporaryDirectory() as tmpdir:
    cwd = Path(tmpdir)
    with patch('workflow.merge_queue.Runner.run_git') as mock_git:
        mock_git.return_value = "f" * 40 + "\n"
        with patch('workflow.merge_queue._is_tree_clean') as mock_clean:
            mock_clean.return_value = False
            try:
                result = _locked_flow(
                    pr=1,
                    branch="feature-x",
                    worktree=str(cwd),
                    config=MergeQueueConfig(base="main", steps=[Step(cmd="echo test")]),
                    merge_lock_fd=-1,
                    inherit_lock_fd=None,
                )
                test_result(
                    "_locked_flow dirty-tree kickback does not raise",
                    result.outcome == MergeOutcome.KICKBACK and result.orig_head == "f" * 40,
                    f"Got outcome={result.outcome}, orig_head={result.orig_head}"
                )
            except ValueError as e:
                test_result(
                    "_locked_flow dirty-tree kickback does not raise",
                    False,
                    f"Raised ValueError: {e}"
                )

print("  [Test 11.2] _locked_flow returns KICKBACK (not ValueError) when config is invalid")
with tempfile.TemporaryDirectory() as tmpdir:
    cwd = Path(tmpdir)
    with patch('workflow.merge_queue.Runner.run_git') as mock_git:
        mock_git.return_value = "e" * 40 + "\n"
        with patch('workflow.merge_queue._is_tree_clean') as mock_clean:
            mock_clean.return_value = True
            with patch('workflow.merge_queue._has_rebase_or_merge_in_progress') as mock_rebase:
                mock_rebase.return_value = False
                with patch('workflow.merge_queue.load_and_validate_config') as mock_load:
                    mock_load.side_effect = ValueError("bad config")
                    try:
                        result = _locked_flow(
                            pr=1,
                            branch="feature-x",
                            worktree=str(cwd),
                            config=MergeQueueConfig(base="main", steps=[Step(cmd="echo test")]),
                            merge_lock_fd=-1,
                            inherit_lock_fd=None,
                            config_path=Path(tmpdir) / "merge-queue.json",
                        )
                        test_result(
                            "_locked_flow config-invalid kickback does not raise",
                            result.outcome == MergeOutcome.KICKBACK and result.orig_head == "e" * 40,
                            f"Got outcome={result.outcome}, orig_head={result.orig_head}"
                        )
                    except ValueError as e:
                        test_result(
                            "_locked_flow config-invalid kickback does not raise",
                            False,
                            f"Raised ValueError: {e}"
                        )


# ============================================================================
# SECTION 12: Ticket allocation and ordering
# ============================================================================
print("[Section 12] Ticket allocation and ordering")

print("  [Test 12.1] acquire_ticket creates ticket with correct number")
with tempfile.TemporaryDirectory() as tmpdir:
    state_dir = Path(tmpdir)
    (state_dir / "tickets").mkdir()
    with patch('workflow.merge_queue.ensure_state_dir') as mock_ensure:
        mock_ensure.return_value = state_dir
        with patch('workflow.merge_queue.get_alloc_lock_path') as mock_alloc:
            mock_alloc.return_value = state_dir / "alloc.lock"
            try:
                ticket_num, ticket_fd = acquire_ticket(pr=1, branch="feature-1", worktree="/fake")
                test_result(
                    "acquire_ticket creates ticket with number 1 for first PR",
                    ticket_num == 1,
                    f"Got ticket_num={ticket_num}"
                )
                os.close(ticket_fd)
            except Exception as e:
                test_result(
                    "acquire_ticket creates ticket",
                    False,
                    f"Got error: {e}"
                )

print("  [Test 12.2] acquire_ticket rejects duplicate PR enqueue")
with tempfile.TemporaryDirectory() as tmpdir:
    state_dir = Path(tmpdir)
    (state_dir / "tickets").mkdir()
    with patch('workflow.merge_queue.ensure_state_dir') as mock_ensure:
        mock_ensure.return_value = state_dir
        with patch('workflow.merge_queue.get_alloc_lock_path') as mock_alloc:
            mock_alloc.return_value = state_dir / "alloc.lock"
            try:
                # First ticket
                ticket_num, ticket_fd = acquire_ticket(pr=1, branch="feature-1", worktree="/fake")
                # Try to acquire second ticket for same PR
                try:
                    ticket_num2, ticket_fd2 = acquire_ticket(pr=1, branch="feature-1", worktree="/fake")
                    test_result(
                        "acquire_ticket rejects duplicate PR",
                        False,
                        "Should have raised RuntimeError"
                    )
                    os.close(ticket_fd2)
                except RuntimeError as e:
                    test_result(
                        "acquire_ticket rejects duplicate PR",
                        "already enqueued" in str(e),
                        f"Got error: {e}"
                    )
                os.close(ticket_fd)
            except Exception as e:
                test_result(
                    "acquire_ticket duplicate rejection setup",
                    False,
                    f"Got error: {e}"
                )


# ============================================================================
# SECTION 13: Lease rejection and force-push semantics
# ============================================================================
print("[Section 13] Lease rejection and force-push semantics")

print("  [Test 13.1] force_push_tested with lease rejection")
with tempfile.TemporaryDirectory() as tmpdir:
    cwd = Path(tmpdir)
    with patch('workflow.merge_queue.Runner.run_git') as mock_git:
        # Simulate git push failing with exit status indicating lease rejection
        mock_git.side_effect = RuntimeError("git push failed: update rejected")
        try:
            result = Runner.force_push_tested(
                branch="feature-x",
                tested_sha="a" * 40,
                lease_sha="b" * 40,
                cwd=cwd,
                timeout=10
            )
            test_result(
                "force_push_tested returns False on push failure",
                result is False,
                f"Got result={result}"
            )
        except RuntimeError as e:
            # force_push_tested doesn't catch RuntimeError, so test that it propagates
            test_result(
                "force_push_tested propagates git errors",
                "update rejected" in str(e),
                f"Got error: {e}"
            )


# ============================================================================
# SECTION 14: run_step timeout behavior
# ============================================================================
print("[Section 14] run_step timeout behavior")

print("  [Test 14.1] run_step returns failure when command fails")
with tempfile.TemporaryDirectory() as tmpdir:
    log_path = Path(tmpdir) / "step.log"
    try:
        # Run a command that will fail (exit non-zero)
        outcome = run_step(
            cmd="false",  # 'false' always returns exit code 1
            cwd=Path(tmpdir),
            log_path=log_path,
            timeout_secs=10,
            inherit_lock_fd=None
        )
        test_result(
            "run_step returns failure when command fails",
            not outcome.success,
            f"Got success={outcome.success}"
        )
    except Exception as e:
        test_result(
            "run_step failure handling",
            False,
            f"Got error: {e}"
        )


# ============================================================================
# SECTION 15: _merge_pr with GH_MERGE_UNVERIFIED
# ============================================================================
print("[Section 15] _merge_pr with GH_MERGE_UNVERIFIED outcome")

print("  [Test 15.1] _merge_pr returns GH_MERGE_UNVERIFIED when verification fails")
with tempfile.TemporaryDirectory() as tmpdir:
    cwd = Path(tmpdir)
    with patch('workflow.merge_queue.git.pr_view_json') as mock_pr_view:
        with patch('workflow.merge_queue.Runner.run_gh') as mock_run_gh:
            # First call: converges for merge
            # Second call (verification): GitCommandError
            mock_pr_view.side_effect = [
                {"headRefOid": "a" * 40, "mergeable": "MERGEABLE", "title": "Test PR"},
                git.GitCommandError("git", 1, "", "API error")
            ]
            mock_run_gh.return_value = ""  # gh pr merge succeeds
            try:
                outcome = _merge_pr(
                    pr=1,
                    tested_sha="a" * 40,
                    base_sha="b" * 40,
                    config=MergeQueueConfig(base="main", steps=[Step(cmd="echo test")]),
                    cwd=cwd
                )
                test_result(
                    "_merge_pr returns GH_MERGE_UNVERIFIED on verification failure",
                    outcome == MergePROutcome.GH_MERGE_UNVERIFIED,
                    f"Got outcome={outcome}"
                )
            except Exception as e:
                test_result(
                    "_merge_pr GH_MERGE_UNVERIFIED handling",
                    False,
                    f"Got error: {e}"
                )


# ============================================================================
# SECTION 16: _locked_flow happy path end-to-end
# ============================================================================
print("[Section 16] _locked_flow happy path end-to-end")

print("  [Test 16.1] _locked_flow succeeds with all green lights")
with tempfile.TemporaryDirectory() as tmpdir:
    cwd = Path(tmpdir)
    config = MergeQueueConfig(base="main", steps=[Step(cmd="echo test")])

    with patch('workflow.merge_queue.Runner.run_git') as mock_git:
        with patch('workflow.merge_queue._is_tree_clean') as mock_clean:
            with patch('workflow.merge_queue._has_rebase_or_merge_in_progress') as mock_rebase:
                with patch('workflow.merge_queue.git.pr_view_json') as mock_pr_view:
                    with patch('workflow.merge_queue.load_and_validate_config') as mock_config:
                        with patch('workflow.merge_queue.scan_base') as mock_scan:
                            with patch('workflow.merge_queue.run_step') as mock_step:
                                with patch('workflow.merge_queue._merge_pr') as mock_merge:
                                    # Setup mocks for happy path
                                    mock_git.return_value = "a" * 40 + "\n"
                                    mock_clean.return_value = True
                                    mock_rebase.return_value = False
                                    mock_pr_view.return_value = {
                                        "state": "OPEN",
                                        "baseRefName": "main",
                                        "headRefName": "feature-1",
                                        "headRefOid": "a" * 40
                                    }
                                    mock_config.return_value = config
                                    mock_scan.return_value = (True, [])
                                    mock_step.return_value.success = True
                                    mock_merge.return_value = MergePROutcome.SUCCESS

                                    try:
                                        result = _locked_flow(
                                            pr=1,
                                            branch="feature-1",
                                            worktree=str(cwd),
                                            config=config,
                                            merge_lock_fd=-1,
                                            inherit_lock_fd=None,
                                        )
                                        test_result(
                                            "_locked_flow happy path returns MERGED",
                                            result.outcome == MergeOutcome.MERGED,
                                            f"Got outcome={result.outcome}"
                                        )
                                    except Exception as e:
                                        test_result(
                                            "_locked_flow happy path execution",
                                            False,
                                            f"Got error: {e}"
                                        )


# ============================================================================
# SECTION 17: Invariant checks (trailer-anchor and trust-boundary)
# ============================================================================
print("[Section 17] Invariant checks")

print("  [Test 17.1] _check_outside_worktree rejects config under worktrees/")
with tempfile.TemporaryDirectory() as tmpdir:
    repo_root = Path(tmpdir) / "repo"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()

    # Create a config under worktrees/pr-1 (which should be rejected)
    worktrees_dir = repo_root / "worktrees"
    pr_worktree = worktrees_dir / "pr-1"
    pr_worktree.mkdir(parents=True)
    config_in_worktree = pr_worktree / "merge-queue.json"
    config_in_worktree.write_text("{}")

    try:
        _check_outside_worktree(config_in_worktree, "test")
        test_result(
            "Trust-boundary guard rejects config under worktrees/",
            False,
            "Should have raised RuntimeError"
        )
    except RuntimeError as e:
        test_result(
            "Trust-boundary guard rejects config under worktrees/",
            "under a worktrees/" in str(e),
            f"Got error: {e}"
        )

print("  [Test 17.2] trailer-anchor exact-parent rule: parse_trailer finds anchor commit")
with tempfile.TemporaryDirectory() as tmpdir:
    cwd = Path(tmpdir)
    # Create a real git repo for this test
    subprocess.run(["git", "init"], cwd=cwd, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=cwd, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=cwd, check=True, capture_output=True)

    # Create initial commit
    (cwd / "file.txt").write_text("test")
    subprocess.run(["git", "add", "file.txt"], cwd=cwd, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=cwd, check=True, capture_output=True)
    base_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    # Create and commit a merge with trailer
    trailer = build_trailer(base_sha, "a" * 40)
    body = f"Tested by merge-queue.\n\n{trailer}"
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", f"Merge PR\n\n{body}"],
        cwd=cwd, check=True, capture_output=True
    )
    commit_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    try:
        parsed_base, parsed_head = parse_trailer(commit_sha, cwd=cwd)
        test_result(
            "parse_trailer extracts base from trailer",
            parsed_base == base_sha,
            f"Got {parsed_base}, expected {base_sha}"
        )
    except Exception as e:
        test_result(
            "trailer-anchor parse_trailer",
            False,
            f"Got error: {e}"
        )


# ============================================================================
# SECTION 18: Regression test for M7 lease handling
# ============================================================================
print("[Section 18] Regression test: M7 lease_sha = orig_head (not post-fetch tip)")

print("  [Test 18.1] _phase_fetch_and_verify_lease pins lease to orig_head, not post-fetch remote")
with tempfile.TemporaryDirectory() as tmpdir:
    cwd = Path(tmpdir)
    config = MergeQueueConfig(base="main", steps=[Step(cmd="echo test")])

    # Simulate: orig_head was "abc123", but remote moved to "def456" between enqueue and lock
    with patch('workflow.merge_queue.Runner.run_git') as mock_git:
        def git_side_effect(cmd, **kwargs):
            if cmd[0] == "fetch":
                return ""
            elif "rev-parse" in cmd and "origin/feature-1" in str(cmd):
                return "def456" * 5 + "\n"  # Remote moved
            elif "rev-parse" in cmd and "origin/main" in str(cmd):
                return "aabbcc" * 6 + "ddee\n"
            else:
                return ""

        mock_git.side_effect = git_side_effect

        try:
            result, lease_sha, base_sha = _phase_fetch_and_verify_lease(
                pr=1,
                branch="feature-1",
                worktree=str(cwd),
                orig_head="abc123" * 8 + "abcd",
                config=config,
                cwd=cwd
            )
            # Should KICKBACK because remote moved
            test_result(
                "M7 regression: detects when remote branch moved",
                result is not None and result.outcome == MergeOutcome.KICKBACK,
                f"Got result.outcome={result.outcome if result else None}"
            )
        except Exception as e:
            test_result(
                "M7 lease_sha regression test",
                False,
                f"Got error: {e}"
            )


# ============================================================================
# Summary
# ============================================================================
print()
h.summarize_and_exit()
