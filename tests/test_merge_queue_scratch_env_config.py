#!/usr/bin/env python3
"""
Test suite for scratch environment configuration in merge queue.

Tests the observable behavior of scratch environment features:
- Config validation for scratch_env_strip (list of non-empty strings)
- Config validation for scratch_env (dict with non-empty string keys and string values)
- _scratch_step_env helper with custom strip lists
- _scratch_step_env with scratch_env overrides and additions
- _scratch_step_env with empty strip list
- run_step with explicit minimal env (verbatim usage)
- MergeQueueConfig.to_dict() and from_dict() round-trip of new fields

Run with: python3 tests/test_merge_queue_scratch_env_config.py
"""

import sys
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _test_harness import Harness
from workflow.merge_queue import (
    MergeQueueConfig,
    validate_config_data,
    _scratch_step_env,
    run_step,
)


if __name__ == "__main__":
    h = Harness("MERGE QUEUE SCRATCH ENV CONFIG TEST SUITE")
    test_result = h.test_result

    # ================================================================
    # SECTION 1: scratch_env_strip validation
    # ================================================================
    print("[Section 1] Config validation for scratch_env_strip")

    # Test 1.1: scratch_env_strip with good values (list of non-empty strings)
    print("  [Test 1.1] scratch_env_strip with valid list of non-empty strings")
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "merge-queue.json"
        config_data = {
            "base": "main",
            "steps": ["echo test"],
            "scratch_env_strip": ["VAR1", "VAR2", "VAR3"],
        }
        config_path.write_text(json.dumps(config_data))

        with patch("workflow.merge_queue.git.get_default_branch") as mock_branch:
            mock_branch.return_value = ("main", None)
            try:
                cfg = validate_config_data(config_data)
                test_result(
                    "scratch_env_strip accepted with valid list",
                    cfg.scratch_env_strip == ["VAR1", "VAR2", "VAR3"],
                    f"scratch_env_strip={cfg.scratch_env_strip}",
                )
            except Exception as e:
                test_result(
                    "scratch_env_strip accepted with valid list",
                    False,
                    f"Unexpected error: {e}",
                )

    # Test 1.2: scratch_env_strip with non-list value
    print("  [Test 1.2] scratch_env_strip with non-list value is rejected")
    with tempfile.TemporaryDirectory() as tmpdir:
        config_data = {
            "base": "main",
            "steps": ["echo test"],
            "scratch_env_strip": "not_a_list",
        }

        with patch("workflow.merge_queue.git.get_default_branch") as mock_branch:
            mock_branch.return_value = ("main", None)
            try:
                cfg = validate_config_data(config_data)
                test_result(
                    "scratch_env_strip non-list rejected",
                    False,
                    "Expected ValueError but config was accepted",
                )
            except ValueError as e:
                test_result(
                    "scratch_env_strip non-list rejected",
                    "scratch_env_strip must be a list of non-empty strings" in str(e),
                    f"Error message: {e}",
                )
            except Exception as e:
                test_result(
                    "scratch_env_strip non-list rejected",
                    False,
                    f"Wrong exception type: {type(e).__name__}: {e}",
                )

    # Test 1.3: scratch_env_strip with empty string entry
    print("  [Test 1.3] scratch_env_strip with empty string entry is rejected")
    with tempfile.TemporaryDirectory() as tmpdir:
        config_data = {
            "base": "main",
            "steps": ["echo test"],
            "scratch_env_strip": ["VAR1", "", "VAR3"],
        }

        with patch("workflow.merge_queue.git.get_default_branch") as mock_branch:
            mock_branch.return_value = ("main", None)
            try:
                cfg = validate_config_data(config_data)
                test_result(
                    "scratch_env_strip empty entry rejected",
                    False,
                    "Expected ValueError but config was accepted",
                )
            except ValueError as e:
                test_result(
                    "scratch_env_strip empty entry rejected",
                    "scratch_env_strip must be a list of non-empty strings" in str(e),
                    f"Error message: {e}",
                )
            except Exception as e:
                test_result(
                    "scratch_env_strip empty entry rejected",
                    False,
                    f"Wrong exception type: {type(e).__name__}: {e}",
                )

    # Test 1.4: scratch_env_strip absent uses default
    print("  [Test 1.4] scratch_env_strip absent uses default")
    with tempfile.TemporaryDirectory() as tmpdir:
        config_data = {
            "base": "main",
            "steps": ["echo test"],
        }

        with patch("workflow.merge_queue.git.get_default_branch") as mock_branch:
            mock_branch.return_value = ("main", None)
            try:
                cfg = validate_config_data(config_data)
                test_result(
                    "scratch_env_strip default applied",
                    cfg.scratch_env_strip == ["COMPOSE_PROJECT_NAME", "DATABASE_URL"],
                    f"scratch_env_strip={cfg.scratch_env_strip}",
                )
            except Exception as e:
                test_result(
                    "scratch_env_strip default applied",
                    False,
                    f"Unexpected error: {e}",
                )

    # ================================================================
    # SECTION 2: scratch_env validation
    # ================================================================
    print()
    print("[Section 2] Config validation for scratch_env")

    # Test 2.1: scratch_env with good values (dict of string keys to string values)
    print("  [Test 2.1] scratch_env with valid dict")
    with tempfile.TemporaryDirectory() as tmpdir:
        config_data = {
            "base": "main",
            "steps": ["echo test"],
            "scratch_env": {"CUSTOM_VAR": "value1", "ANOTHER_VAR": "value2"},
        }

        with patch("workflow.merge_queue.git.get_default_branch") as mock_branch:
            mock_branch.return_value = ("main", None)
            try:
                cfg = validate_config_data(config_data)
                test_result(
                    "scratch_env accepted with valid dict",
                    cfg.scratch_env == {"CUSTOM_VAR": "value1", "ANOTHER_VAR": "value2"},
                    f"scratch_env={cfg.scratch_env}",
                )
            except Exception as e:
                test_result(
                    "scratch_env accepted with valid dict",
                    False,
                    f"Unexpected error: {e}",
                )

    # Test 2.2: scratch_env with non-dict value
    print("  [Test 2.2] scratch_env with non-dict value is rejected")
    with tempfile.TemporaryDirectory() as tmpdir:
        config_data = {
            "base": "main",
            "steps": ["echo test"],
            "scratch_env": ["not", "a", "dict"],
        }

        with patch("workflow.merge_queue.git.get_default_branch") as mock_branch:
            mock_branch.return_value = ("main", None)
            try:
                cfg = validate_config_data(config_data)
                test_result(
                    "scratch_env non-dict rejected",
                    False,
                    "Expected ValueError but config was accepted",
                )
            except ValueError as e:
                test_result(
                    "scratch_env non-dict rejected",
                    "scratch_env must be an object mapping variable names to string values" in str(e),
                    f"Error message: {e}",
                )
            except Exception as e:
                test_result(
                    "scratch_env non-dict rejected",
                    False,
                    f"Wrong exception type: {type(e).__name__}: {e}",
                )

    # Test 2.3: scratch_env with non-string value
    print("  [Test 2.3] scratch_env with non-string value is rejected")
    with tempfile.TemporaryDirectory() as tmpdir:
        config_data = {
            "base": "main",
            "steps": ["echo test"],
            "scratch_env": {"VAR1": "string", "VAR2": 42},
        }

        with patch("workflow.merge_queue.git.get_default_branch") as mock_branch:
            mock_branch.return_value = ("main", None)
            try:
                cfg = validate_config_data(config_data)
                test_result(
                    "scratch_env non-string value rejected",
                    False,
                    "Expected ValueError but config was accepted",
                )
            except ValueError as e:
                test_result(
                    "scratch_env non-string value rejected",
                    "scratch_env must be an object mapping variable names to string values" in str(e),
                    f"Error message: {e}",
                )
            except Exception as e:
                test_result(
                    "scratch_env non-string value rejected",
                    False,
                    f"Wrong exception type: {type(e).__name__}: {e}",
                )

    # Test 2.4: scratch_env with empty key
    print("  [Test 2.4] scratch_env with empty key is rejected")
    with tempfile.TemporaryDirectory() as tmpdir:
        config_data = {
            "base": "main",
            "steps": ["echo test"],
            "scratch_env": {"": "value", "VAR2": "value2"},
        }

        with patch("workflow.merge_queue.git.get_default_branch") as mock_branch:
            mock_branch.return_value = ("main", None)
            try:
                cfg = validate_config_data(config_data)
                test_result(
                    "scratch_env empty key rejected",
                    False,
                    "Expected ValueError but config was accepted",
                )
            except ValueError as e:
                # Empty keys are rejected as part of scratch_env validation
                test_result(
                    "scratch_env empty key rejected",
                    "scratch_env must be an object mapping variable names to string values" in str(e),
                    f"Error message: {e}",
                )
            except Exception as e:
                test_result(
                    "scratch_env empty key rejected",
                    False,
                    f"Wrong exception type: {type(e).__name__}: {e}",
                )

    # Test 2.5: scratch_env absent uses default (empty dict)
    print("  [Test 2.5] scratch_env absent uses default")
    with tempfile.TemporaryDirectory() as tmpdir:
        config_data = {
            "base": "main",
            "steps": ["echo test"],
        }

        with patch("workflow.merge_queue.git.get_default_branch") as mock_branch:
            mock_branch.return_value = ("main", None)
            try:
                cfg = validate_config_data(config_data)
                test_result(
                    "scratch_env default applied",
                    cfg.scratch_env == {},
                    f"scratch_env={cfg.scratch_env}",
                )
            except Exception as e:
                test_result(
                    "scratch_env default applied",
                    False,
                    f"Unexpected error: {e}",
                )

    # ================================================================
    # SECTION 3: Unknown keys still rejected
    # ================================================================
    print()
    print("[Section 3] Unknown config keys still rejected")

    # Test 3.1: Unknown keys are rejected even with valid scratch_env_strip/scratch_env
    print("  [Test 3.1] Unknown keys rejected with new fields present")
    with tempfile.TemporaryDirectory() as tmpdir:
        config_data = {
            "base": "main",
            "steps": ["echo test"],
            "scratch_env_strip": ["VAR1"],
            "scratch_env": {"VAR2": "val"},
            "unknown_field": "should cause error",
        }

        with patch("workflow.merge_queue.git.get_default_branch") as mock_branch:
            mock_branch.return_value = ("main", None)
            try:
                cfg = validate_config_data(config_data)
                test_result(
                    "Unknown key rejected",
                    False,
                    "Expected ValueError but config was accepted",
                )
            except ValueError as e:
                test_result(
                    "Unknown key rejected",
                    "unknown" in str(e).lower() or "Unknown" in str(e),
                    f"Error message: {e}",
                )
            except Exception as e:
                test_result(
                    "Unknown key rejected",
                    False,
                    f"Wrong exception type: {type(e).__name__}: {e}",
                )

    # ================================================================
    # SECTION 4: _scratch_step_env with custom strip list
    # ================================================================
    print()
    print("[Section 4] _scratch_step_env helper function")

    # Test 4.1: _scratch_step_env with custom strip list strips those names, not defaults
    print("  [Test 4.1] _scratch_step_env with custom strip list")
    with tempfile.TemporaryDirectory() as tmpdir:
        test_env = {
            "COMPOSE_PROJECT_NAME": "default_compose",
            "DATABASE_URL": "default_db",
            "CUSTOM_STRIP": "custom_value",
            "KEPT_VAR": "kept_value",
        }
        with patch.dict(os.environ, test_env):
            config = MergeQueueConfig(
                base="main",
                steps=[],
                scratch_env_strip=["CUSTOM_STRIP"],
            )
            result_env = _scratch_step_env(config)
            test_result(
                "_scratch_step_env strips custom list",
                "CUSTOM_STRIP" not in result_env and "KEPT_VAR" in result_env,
                f"Keys in result: {list(result_env.keys())[:10]}",
            )
            test_result(
                "_scratch_step_env keeps default-stripped vars when not in custom list",
                "COMPOSE_PROJECT_NAME" in result_env and "DATABASE_URL" in result_env,
                f"COMPOSE_PROJECT_NAME in result: {'COMPOSE_PROJECT_NAME' in result_env}",
            )

    # Test 4.2: _scratch_step_env with empty strip list (nothing removed)
    print("  [Test 4.2] _scratch_step_env with empty strip list")
    with tempfile.TemporaryDirectory() as tmpdir:
        test_env = {
            "COMPOSE_PROJECT_NAME": "compose_value",
            "DATABASE_URL": "db_value",
            "OTHER_VAR": "other_value",
        }
        with patch.dict(os.environ, test_env):
            config = MergeQueueConfig(
                base="main",
                steps=[],
                scratch_env_strip=[],
            )
            result_env = _scratch_step_env(config)
            test_result(
                "_scratch_step_env with empty strip list preserves all",
                "COMPOSE_PROJECT_NAME" in result_env
                and "DATABASE_URL" in result_env
                and "OTHER_VAR" in result_env,
                "Missing vars in result",
            )

    # Test 4.3: _scratch_step_env with scratch_env overriding inherited value
    print("  [Test 4.3] _scratch_step_env with scratch_env overriding inherited value")
    with tempfile.TemporaryDirectory() as tmpdir:
        test_env = {
            "COMPOSE_PROJECT_NAME": "original_value",
            "OTHER": "keep_this",
        }
        with patch.dict(os.environ, test_env):
            config = MergeQueueConfig(
                base="main",
                steps=[],
                scratch_env_strip=["COMPOSE_PROJECT_NAME"],
                scratch_env={"COMPOSE_PROJECT_NAME": "overridden_value"},
            )
            result_env = _scratch_step_env(config)
            test_result(
                "_scratch_step_env scratch_env overrides after strip",
                result_env.get("COMPOSE_PROJECT_NAME") == "overridden_value",
                f"COMPOSE_PROJECT_NAME={result_env.get('COMPOSE_PROJECT_NAME')}",
            )
            test_result(
                "_scratch_step_env keeps other vars",
                result_env.get("OTHER") == "keep_this",
                f"OTHER={result_env.get('OTHER')}",
            )

    # Test 4.4: _scratch_step_env with scratch_env adding new variable
    print("  [Test 4.4] _scratch_step_env with scratch_env adding new variable")
    with tempfile.TemporaryDirectory() as tmpdir:
        test_env = {
            "COMPOSE_PROJECT_NAME": "original",
            "EXISTING": "existing_value",
        }
        with patch.dict(os.environ, test_env):
            config = MergeQueueConfig(
                base="main",
                steps=[],
                scratch_env_strip=["COMPOSE_PROJECT_NAME"],
                scratch_env={"NEW_VAR": "new_value"},
            )
            result_env = _scratch_step_env(config)
            test_result(
                "_scratch_step_env adds scratch_env variable",
                result_env.get("NEW_VAR") == "new_value",
                f"NEW_VAR={result_env.get('NEW_VAR')}",
            )
            test_result(
                "_scratch_step_env strips COMPOSE_PROJECT_NAME",
                "COMPOSE_PROJECT_NAME" not in result_env,
                f"COMPOSE_PROJECT_NAME in result: {'COMPOSE_PROJECT_NAME' in result_env}",
            )
            test_result(
                "_scratch_step_env keeps existing vars",
                result_env.get("EXISTING") == "existing_value",
                f"EXISTING={result_env.get('EXISTING')}",
            )

    # ================================================================
    # SECTION 5: run_step with explicit minimal env
    # ================================================================
    print()
    print("[Section 5] run_step with explicit minimal env")

    # Test 5.1: run_step with explicit minimal env uses it verbatim
    print("  [Test 5.1] run_step with minimal explicit env uses it verbatim")
    with tempfile.TemporaryDirectory() as tmp:
        # Set up an environment with a variable that should NOT be visible
        with patch.dict(os.environ, {"NOT_IN_ENV": "should_not_appear"}):
            log = Path(tmp) / "step.log"
            # Pass only PATH in the env (needed for /bin/echo to work)
            step_env = {"PATH": "/bin:/usr/bin"}

            outcome = run_step('/bin/echo "$NOT_IN_ENV|$PATH"', Path(tmp), log, env=step_env)
            log_content = log.read_text().strip()
            test_result(
                "run_step with explicit env succeeded",
                outcome.success,
                f"error={outcome.error}, log={log_content}",
            )
            # NOT_IN_ENV should be empty in the output, but PATH should work
            test_result(
                "run_step uses explicit env verbatim (var not in env is empty)",
                log_content == "|/bin:/usr/bin",
                f"log content: {log_content!r}",
            )

    # ================================================================
    # SECTION 6: from_dict/to_dict round-trip
    # ================================================================
    print()
    print("[Section 6] MergeQueueConfig from_dict/to_dict round-trip")

    # Test 6.1: to_dict serializes scratch_env_strip and scratch_env
    print("  [Test 6.1] to_dict serializes new fields")
    cfg = MergeQueueConfig(
        base="main",
        steps=[],
        scratch_env_strip=["VAR1", "VAR2"],
        scratch_env={"CUSTOM": "value"},
    )
    cfg_dict = cfg.to_dict()
    test_result(
        "to_dict includes scratch_env_strip",
        "scratch_env_strip" in cfg_dict and cfg_dict["scratch_env_strip"] == ["VAR1", "VAR2"],
        f"scratch_env_strip in dict: {'scratch_env_strip' in cfg_dict}",
    )
    test_result(
        "to_dict includes scratch_env",
        "scratch_env" in cfg_dict and cfg_dict["scratch_env"] == {"CUSTOM": "value"},
        f"scratch_env in dict: {'scratch_env' in cfg_dict}",
    )

    # Test 6.2: from_dict deserializes scratch_env_strip and scratch_env
    print("  [Test 6.2] from_dict deserializes new fields")
    input_dict = {
        "base": "main",
        "steps": ["echo test"],
        "scratch_env_strip": ["STRIP1", "STRIP2"],
        "scratch_env": {"KEY1": "val1", "KEY2": "val2"},
    }
    with patch("workflow.merge_queue.git.get_default_branch") as mock_branch:
        mock_branch.return_value = ("main", None)
        cfg = MergeQueueConfig.from_dict(input_dict)
        test_result(
            "from_dict restores scratch_env_strip",
            cfg.scratch_env_strip == ["STRIP1", "STRIP2"],
            f"scratch_env_strip={cfg.scratch_env_strip}",
        )
        test_result(
            "from_dict restores scratch_env",
            cfg.scratch_env == {"KEY1": "val1", "KEY2": "val2"},
            f"scratch_env={cfg.scratch_env}",
        )

    # Test 6.3: Round-trip (to_dict then from_dict)
    print("  [Test 6.3] Round-trip (to_dict then from_dict)")
    original_cfg = MergeQueueConfig(
        base="main",
        steps=[],
        scratch_env_strip=["A", "B"],
        scratch_env={"X": "y"},
    )
    dict_form = original_cfg.to_dict()
    with patch("workflow.merge_queue.git.get_default_branch") as mock_branch:
        mock_branch.return_value = ("main", None)
        restored_cfg = MergeQueueConfig.from_dict(dict_form)
        test_result(
            "Round-trip: scratch_env_strip preserved",
            restored_cfg.scratch_env_strip == original_cfg.scratch_env_strip,
            f"Original: {original_cfg.scratch_env_strip}, Restored: {restored_cfg.scratch_env_strip}",
        )
        test_result(
            "Round-trip: scratch_env preserved",
            restored_cfg.scratch_env == original_cfg.scratch_env,
            f"Original: {original_cfg.scratch_env}, Restored: {restored_cfg.scratch_env}",
        )

    h.summarize_and_exit()
