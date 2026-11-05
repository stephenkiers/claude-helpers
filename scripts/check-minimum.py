#!/usr/bin/env python3
"""
Minimum check selector for this repo: lint + typecheck + tests for changed files.

Maps changed file paths to test files (matching tests/ names by module/path).
Falls back to the full test suite when the base is unresolved or a changed path is unmappable.

Imports the same canonical_changed_files, resolve_check_base functions as validate().
Reads CHECK_BASE and CHECK_CHANGED_FILES (exported by validate) when set.

Sandboxed invariant: the selection (build_plan, reached through --dry-run) reads nothing outside
the working tree except the base and the changed-file list it is given. It does not read HOME,
the user's config, or any other environment variable beyond CHECK_BASE and CHECK_CHANGED_FILES.
"""

import argparse
import json
import sys
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add parent to path so we can import workflow
sys.path.insert(0, str(Path(__file__).parent))

from workflow.checks import resolve_check_base, canonical_changed_files  # type: ignore[import-not-found]

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"


def get_check_base_and_files() -> tuple:
    """Get base and changed files from env or compute them."""
    worktree = Path.cwd()

    # Check for override env vars
    check_base = os.environ.get("CHECK_BASE")
    check_files_env = os.environ.get("CHECK_CHANGED_FILES")

    if check_files_env:
        try:
            check_files_path = Path(check_files_env)
            if check_files_path.exists():
                content = check_files_path.read_text()
                lines = content.split("\n")
                check_base = lines[0] if lines else ""
                files = content[len(check_base)+1:].split("\0") if len(lines) > 1 else []
                files = [f for f in files if f]
                return check_base, files
        except Exception:
            pass

    # Compute from scratch; an exported CHECK_BASE takes precedence over the computed merge-base
    base = check_base or resolve_check_base(worktree)
    files = canonical_changed_files(worktree, base)
    return base, files


def map_files_to_tests(files: list) -> tuple:
    """Map changed file paths to test files. Return (test_modules, unmapped_files)."""
    tests_dir = TESTS_DIR
    test_files = set()
    unmapped = []

    for file_path in files:
        # For Python files in scripts/workflow, map to tests/test_workflow_*.py
        if file_path.startswith("scripts/workflow/") and file_path.endswith(".py"):
            base_name = Path(file_path).stem
            # Try to find matching test file
            matching = list(tests_dir.glob(f"test_workflow_{base_name}*.py"))
            if matching:
                for m in matching:
                    test_files.add(m.name)
            else:
                unmapped.append(file_path)
        elif file_path.startswith("scripts/") and file_path.endswith(".py"):
            base_name = Path(file_path).stem
            matching = list(tests_dir.glob(f"test_{base_name}*.py"))
            if matching:
                for m in matching:
                    test_files.add(m.name)
            else:
                unmapped.append(file_path)
        elif file_path.startswith("tests/"):
            # Test file changed, run it
            test_name = Path(file_path).name
            if test_name.endswith(".py") and test_name.startswith("test_"):
                test_files.add(test_name)
        else:
            unmapped.append(file_path)

    return sorted(test_files), unmapped


def build_plan(check_base: Optional[str], changed_files: List[str]) -> Dict[str, Any]:
    """
    Pure selection: which test modules to run, or the full suite and why. Runs nothing.

    An unresolved base (None or empty) means the changed set is unknown, so the full suite runs.
    An unmappable changed path also runs the full suite.
    """
    if not check_base:
        return {
            "check_base": check_base or "",
            "changed_files": list(changed_files),
            "tests": [],
            "full_suite": True,
            "fallback_reason": "base unresolved",
        }
    tests, unmapped = map_files_to_tests(changed_files)
    if unmapped:
        return {
            "check_base": check_base,
            "changed_files": list(changed_files),
            "tests": [],
            "full_suite": True,
            "fallback_reason": f"unmappable paths: {unmapped}",
        }
    return {
        "check_base": check_base,
        "changed_files": list(changed_files),
        "tests": tests,
        "full_suite": False,
        "fallback_reason": None,
    }


def _run(argv: List[str]) -> Any:
    return subprocess.run(argv, cwd=str(REPO_ROOT))


def main(argv: Optional[List[str]] = None) -> int:
    """Run minimum check. With --dry-run, print the selection as JSON and run no gates."""
    parser = argparse.ArgumentParser(description="Minimum check selector")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the selected plan as JSON and exit without running any gate")
    args = parser.parse_args(argv)

    check_base, changed_files = get_check_base_and_files()
    plan = build_plan(check_base, changed_files)

    if args.dry_run:
        print(json.dumps(plan, sort_keys=True))
        return 0

    # 1. Run lint
    print("[minimum] Running: just lint")
    result = _run(["just", "lint"])
    if result.returncode != 0:
        print("[minimum] lint failed")
        return 1

    # 2. Run typecheck
    print("[minimum] Running: just typecheck")
    result = _run(["just", "typecheck"])
    if result.returncode != 0:
        print("[minimum] typecheck failed")
        return 1

    # 3. Run the selected tests
    if plan["full_suite"]:
        print(f"[minimum] {plan['fallback_reason']}, falling back to full test suite")
        print("[minimum] Running: python3 tests/run_all.py")
        result = _run(["python3", "tests/run_all.py"])
        return result.returncode

    test_modules = plan["tests"]
    if not test_modules:
        # No tests mapped
        print("[minimum] No test files mapped, skipping tests")
        return 0

    print(f"[minimum] Running {len(test_modules)} test modules: {', '.join(test_modules)}")
    # Each suite is a standalone script (repo convention; pytest is not a dependency)
    rc = 0
    for m in test_modules:
        print(f"[minimum] Running: python3 tests/{m}")
        result = _run(["python3", f"tests/{m}"])
        if result.returncode != 0:
            rc = result.returncode
            break
    return rc


if __name__ == "__main__":
    sys.exit(main())
