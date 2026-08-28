#!/usr/bin/env python3
"""
Test suite for toolchain detection.

Run with: python3 tests/test_workflow_checks.py
"""

import sys
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.checks import detect_toolchains, detect_checks, execute_check
from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("WORKFLOW CHECKS TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Toolchain detection")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)

        toolchains = detect_toolchains(repo_root)
        test_result(
            "detect_toolchains() returns a dict",
            isinstance(toolchains, dict)
        )
        test_result(
            "detect_toolchains() includes typescript",
            "typescript" in toolchains
        )
        test_result(
            "detect_toolchains() detects missing TypeScript",
            toolchains["typescript"] is False
        )

        (repo_root / "tsconfig.json").touch()
        toolchains = detect_toolchains(repo_root)
        test_result(
            "detect_toolchains() detects TypeScript",
            toolchains["typescript"] is True
        )

        (repo_root / "package.json").touch()
        toolchains = detect_toolchains(repo_root)
        test_result(
            "detect_toolchains() detects Node.js",
            toolchains["node"] is True
        )

        (repo_root / "Cargo.toml").touch()
        toolchains = detect_toolchains(repo_root)
        test_result(
            "detect_toolchains() detects Rust",
            toolchains["rust"] is True
        )

    print()
    print("[Section 2] ESLint detection")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)

        toolchains = detect_toolchains(repo_root)
        test_result(
            "detect_toolchains() detects missing ESLint",
            toolchains["eslint"] is False
        )

        (repo_root / ".eslintrc.json").touch()
        toolchains = detect_toolchains(repo_root)
        test_result(
            "detect_toolchains() detects ESLint config",
            toolchains["eslint"] is True
        )

    print()
    print("[Section 3] Check detection with package.json scripts")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)

        pkg_json = repo_root / "package.json"
        pkg_data = {
            "name": "test-pkg",
            "scripts": {
                "test": "jest",
                "lint": "eslint .",
                "type-check": "tsc --noEmit"
            }
        }
        pkg_json.write_text(json.dumps(pkg_data))

        checks = detect_checks(repo_root)
        test_result(
            "detect_checks() returns a dict",
            isinstance(checks, dict)
        )
        test_result(
            "detect_checks() detects test script",
            "npm_test" in checks
        )
        test_result(
            "detect_checks() detects lint script",
            "npm_lint" in checks
        )
        test_result(
            "detect_checks() detects type-check script",
            "npm_typecheck" in checks
        )

    print()
    print("[Section 4] Check detection without package.json")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)

        checks = detect_checks(repo_root)
        test_result(
            "detect_checks() handles missing package.json",
            isinstance(checks, dict)
        )

    print()
    print("[Section 5] Check detection with Rust")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        (repo_root / "Cargo.toml").touch()

        checks = detect_checks(repo_root)
        test_result(
            "detect_checks() detects cargo test",
            "cargo_test" in checks
        )

    print()
    print("[Section 6] Check execution (Fix 11)")

    result = execute_check("true", cwd=None)
    test_result(
        "execute_check: succeeds on passing command",
        result.success and result.returncode == 0
    )

    result = execute_check("false", cwd=None)
    test_result(
        "execute_check: fails on failing command",
        not result.success and result.returncode != 0
    )

    result = execute_check("echo 'test output' >&2 && false", cwd=None)
    test_result(
        "execute_check: captures stderr on failure",
        not result.success and "test output" in result.stderr
    )

    result = execute_check("sleep 5", cwd=None, timeout=1)
    test_result(
        "execute_check: times out after specified timeout",
        not result.success and "timed out" in str(result.error).lower()
    )

    print()
    h.summarize_and_exit()
