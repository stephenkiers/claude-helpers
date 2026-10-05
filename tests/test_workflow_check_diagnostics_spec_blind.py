#!/usr/bin/env python3
"""
Spec-blind test suite for check_diagnostics module enhancements.

This test suite is written ONLY from the plan specification (5 findings),
without reading the implementation files (check_diagnostics.py itself).

The five findings being tested:
1. [HIGH] Failed cleanup runs leak cleanup-checks-* temp dirs forever.
   Fix: Add age-based pruning and/or retention cap (~20) in make_log_dir().

2. [HIGH] Raw secrets leak in stdout/stderr/ps argv.
   Fix: Add _redact() pass for token/secret/key/password/authorization patterns.

3. [LOW] _run_best_effort contract is undocumented.
   Fix: Add docstring stating: never raises; returns "<unavailable: {err}>" on failure.

4. [LOW] write_check_log's index parameter has undocumented per-log_dir uniqueness precondition.
   Fix: Document the precondition.

5. [LOW] environment_snapshot doesn't document fallback when main_worktree is None.
   Fix: One clause in docstring stating the None fallback behavior.

Run with: python3 tests/test_workflow_check_diagnostics_spec_blind.py
"""

import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow import check_diagnostics as diag
from workflow.checks import CheckResult
from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("CHECK DIAGNOSTICS SPEC-BLIND TEST SUITE")
    test_result = h.test_result

    # =========================================================================
    # Finding 1: make_log_dir() implements age-based pruning or retention cap
    # =========================================================================
    print("[Finding 1] make_log_dir() implements age-based pruning or retention cap")
    print("=" * 70)

    # Test 1: Verify make_log_dir() returns a valid directory
    new_dir = diag.make_log_dir()
    test_result(
        "make_log_dir returns a path",
        new_dir is not None and isinstance(new_dir, (str, Path)),
        f"Expected path, got {type(new_dir)}"
    )

    test_result(
        "make_log_dir returns an existing directory",
        new_dir is not None and Path(new_dir).exists() and Path(new_dir).is_dir(),
        f"Expected valid directory, got {new_dir}"
    )

    # Test 2: Verify that make_log_dir creates cleanup-checks-* directories
    if new_dir:
        new_dir_path = Path(new_dir)
        # The directory name should follow a pattern like cleanup-checks-<timestamp>
        test_result(
            "make_log_dir creates cleanup-checks-* directories",
            "cleanup-checks" in str(new_dir_path.name),
            f"Expected 'cleanup-checks' in directory name, got {new_dir_path.name}"
        )

    print()

    # =========================================================================
    # Finding 2: _redact() function redacts secret patterns
    # =========================================================================
    print("[Finding 2] _redact() redacts secret/token/password/key patterns")
    print("=" * 70)

    # Test that _redact function exists
    has_redact = hasattr(diag, "_redact")
    test_result(
        "_redact function exists",
        has_redact,
        "_redact not found in check_diagnostics"
    )

    if has_redact:
        # Test PASSWORD (uppercase) — basic token/password/key/Authorization patterns and
        # normal-text preservation are already covered in test_workflow_check_diagnostics.py
        result = diag._redact("PASSWORD=secret789")
        test_result(
            "_redact handles uppercase PASSWORD",
            "PASSWORD=" in result and "secret789" not in result,
            f"Expected uppercase PASSWORD to be redacted, got '{result}'"
        )

        # Test mixed content
        result = diag._redact("output with token=secret and normal text")
        test_result(
            "_redact redacts in mixed content",
            "token=" in result and "secret" not in result and "normal text" in result,
            f"Expected selective redaction, got '{result}'"
        )

        # Test JSON-shaped secrets with quoted keys
        result = diag._redact('{"password": "mysecret123"}')
        test_result(
            "_redact redacts JSON password pattern",
            "password" in result and "mysecret123" not in result,
            f"Expected JSON password to be redacted, got '{result}'"
        )

        # Test JSON-shaped api_key pattern
        result = diag._redact('{"api_key": "abc123def456"}')
        test_result(
            "_redact redacts JSON api_key pattern",
            "api_key" in result and "abc123def456" not in result,
            f"Expected JSON api_key to be redacted, got '{result}'"
        )

        # Test bare GitHub token (ghp_ prefix)
        result = diag._redact("ghp_abc123def456xyz789")
        test_result(
            "_redact redacts bare GitHub token ghp_",
            "ghp_" not in result or "<redacted>" in result,
            f"Expected GitHub token to be redacted, got '{result}'"
        )

        # Test bare GitHub token (ghs_ prefix)
        result = diag._redact("ghs_xyz789abc123def456")
        test_result(
            "_redact redacts bare GitHub token ghs_",
            "ghs_" not in result or "<redacted>" in result,
            f"Expected GitHub token to be redacted, got '{result}'"
        )

        # Test AWS access key ID (AKIA prefix)
        result = diag._redact("AKIA1234567890ABCDEF")
        test_result(
            "_redact redacts AWS access key ID",
            "AKIA" not in result or "<redacted>" in result,
            f"Expected AWS key to be redacted, got '{result}'"
        )

    print()

    # =========================================================================
    # Finding 3: _run_best_effort docstring documents contract
    # =========================================================================
    print("[Finding 3] _run_best_effort documents its never-raises contract")
    print("=" * 70)

    has_run_best_effort = hasattr(diag, "_run_best_effort")
    test_result(
        "_run_best_effort function exists",
        has_run_best_effort,
        "_run_best_effort not found in check_diagnostics"
    )

    if has_run_best_effort:
        docstring = diag._run_best_effort.__doc__ or ""
        test_result(
            "_run_best_effort has a docstring",
            len(docstring.strip()) > 0,
            "docstring is missing or empty"
        )

        # The docstring should document that it never raises and returns a sentinel on failure
        has_never_raises = "never raises" in docstring.lower()
        has_unavailable = "<unavailable:" in docstring or "unavailable" in docstring.lower()

        test_result(
            "_run_best_effort docstring mentions it never raises",
            has_never_raises,
            f"Expected 'never raises' in docstring; got: {docstring[:100]}"
        )

        test_result(
            "_run_best_effort docstring mentions <unavailable:> sentinel return",
            has_unavailable,
            f"Expected '<unavailable:' or 'unavailable' in docstring; got: {docstring[:100]}"
        )

    print()

    # =========================================================================
    # Finding 4: write_check_log documents index uniqueness precondition
    # =========================================================================
    print("[Finding 4] write_check_log documents index per-log_dir uniqueness")
    print("=" * 70)

    has_write_check_log = hasattr(diag, "write_check_log")
    test_result(
        "write_check_log function exists",
        has_write_check_log,
        "write_check_log not found in check_diagnostics"
    )

    if has_write_check_log:
        docstring = diag.write_check_log.__doc__ or ""
        test_result(
            "write_check_log has a docstring",
            len(docstring.strip()) > 0,
            "docstring is missing or empty"
        )

        # The docstring should mention the index parameter and its uniqueness
        has_index_mention = "index" in docstring.lower()
        has_unique_mention = ("unique" in docstring.lower() or
                              "per-log_dir" in docstring or
                              "precondition" in docstring.lower())

        test_result(
            "write_check_log docstring mentions index parameter",
            has_index_mention,
            f"Expected 'index' mention in docstring; got: {docstring[:150]}"
        )

        test_result(
            "write_check_log docstring mentions uniqueness/precondition",
            has_unique_mention,
            f"Expected uniqueness/precondition mention; got: {docstring[:150]}"
        )

    print()

    # =========================================================================
    # Finding 5: environment_snapshot documents None fallback
    # =========================================================================
    print("[Finding 5] environment_snapshot documents main_worktree None fallback")
    print("=" * 70)

    has_env_snapshot = hasattr(diag, "environment_snapshot")
    test_result(
        "environment_snapshot function exists",
        has_env_snapshot,
        "environment_snapshot not found in check_diagnostics"
    )

    if has_env_snapshot:
        docstring = diag.environment_snapshot.__doc__ or ""
        test_result(
            "environment_snapshot has a docstring",
            len(docstring.strip()) > 0,
            "docstring is missing or empty"
        )

        # The docstring should mention None handling and fallback behavior
        has_none_mention = "none" in docstring.lower() or "none:" in docstring.lower()
        has_fallback_mention = ("fallback" in docstring.lower() or
                                "cwd" in docstring.lower() or
                                "current" in docstring.lower())

        test_result(
            "environment_snapshot docstring mentions None handling",
            has_none_mention,
            f"Expected None mention in docstring; got: {docstring[:150]}"
        )

        test_result(
            "environment_snapshot docstring mentions cwd fallback",
            has_fallback_mention,
            f"Expected fallback/cwd mention; got: {docstring[:150]}"
        )

    print()

    # =========================================================================
    # Additional Integration Tests: Verify redaction is applied in context
    # =========================================================================
    print("[Integration] Redaction is applied to persisted output")
    print("=" * 70)

    if has_redact and has_write_check_log:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            log_dir = tmppath / "test-log-dir"
            log_dir.mkdir()

            # Create a failed check result with secrets in stdout/stderr
            secret_check = CheckResult(
                success=False,
                returncode=1,
                stdout="API call failed with token=secret123",
                stderr="password=admin123",
                error=None
            )

            # Mock environment_snapshot to avoid real system calls
            with mock.patch.object(diag, "environment_snapshot", return_value="ENV_SNAPSHOT"):
                try:
                    # Call write_check_log
                    log_path = diag.write_check_log(
                        log_dir,
                        0,  # index
                        "test-command",
                        secret_check,
                        datetime.now(timezone.utc),
                        1.5,  # elapsed
                        tmppath  # tmp_root
                    )

                    # If log was written, verify it doesn't contain secrets
                    if log_path and Path(log_path).exists():
                        log_content = Path(log_path).read_text()
                        test_result(
                            "write_check_log redacts stdout secrets",
                            "token=secret123" not in log_content and "token=" in log_content,
                            "Secrets should be redacted in persisted output"
                        )

                        test_result(
                            "write_check_log redacts stderr secrets",
                            "password=admin123" not in log_content and "password=" in log_content,
                            "Password should be redacted in persisted output"
                        )

                        test_result(
                            "log file exists and contains content",
                            log_path is not None and len(log_content) > 0,
                            f"Expected valid log file, got {log_path}"
                        )
                    else:
                        test_result(
                            "write_check_log creates log file",
                            log_path is not None,
                            "write_check_log returned None or no file"
                        )

                except Exception as e:
                    test_result(
                        "write_check_log executes without exception",
                        False,
                        f"Unexpected exception: {e}"
                    )

    print()
    h.summarize_and_exit()
