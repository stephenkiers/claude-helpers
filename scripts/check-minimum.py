#!/usr/bin/env python3
"""
Minimum check selector for this repo: lint + typecheck + tests for changed files.

Maps changed file paths to test files (grep tests/ for module/path names).
Falls back to full test suite for unmappable paths, with logging.

Imports the same canonical_changed_files, resolve_check_base functions as validate().
Exports CHECK_BASE and CHECK_CHANGED_FILES (in a temp file), reads them if set.
"""

import sys
import os
import subprocess
from pathlib import Path

# Add parent to path so we can import workflow
sys.path.insert(0, str(Path(__file__).parent))

from workflow.checks import resolve_check_base, canonical_changed_files  # type: ignore[import-not-found]


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
    tests_dir = Path("tests")
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


def main() -> int:
    """Run minimum check."""
    check_base, changed_files = get_check_base_and_files()

    # 1. Run lint
    print("[minimum] Running: just lint")
    result = subprocess.run(["just", "lint"], cwd=".")
    if result.returncode != 0:
        print("[minimum] lint failed")
        return 1

    # 2. Run typecheck
    print("[minimum] Running: just typecheck")
    result = subprocess.run(["just", "typecheck"], cwd=".")
    if result.returncode != 0:
        print("[minimum] typecheck failed")
        return 1

    # 3. Map files to tests
    test_modules, unmapped = map_files_to_tests(changed_files)

    if unmapped:
        print(f"[minimum] Unmappable paths, falling back to full test suite: {unmapped}")
        # Run all tests
        print("[minimum] Running: python3 tests/run_all.py")
        result = subprocess.run(["python3", "tests/run_all.py"], cwd=".")
        return result.returncode

    if not test_modules:
        # No tests mapped
        print("[minimum] No test files mapped, skipping tests")
        return 0

    print(f"[minimum] Running {len(test_modules)} test modules: {', '.join(test_modules)}")
    # Run mapped tests
    test_args = [f"tests/{m}" for m in test_modules]
    result = subprocess.run(["python3", "-m", "pytest", "-xvs"] + test_args, cwd=".")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
