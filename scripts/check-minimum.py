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
import re
import sys
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"

# Import by the repo-rooted name (scripts.workflow.checks), the same module name every other caller uses,
# so there is one module object for checks.py rather than a second one named workflow.checks.
sys.path.insert(0, str(REPO_ROOT))

from scripts.workflow.checks import resolve_check_base, canonical_changed_files  # noqa: E402


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
    computed = canonical_changed_files(worktree, base)
    if computed is None:
        # The changed set is unknown: an empty base makes build_plan fall back to the full suite.
        return "", []
    return base, computed


def _reference_patterns(file_path: str) -> List["re.Pattern[str]"]:
    """Patterns that mean a test references the module at `file_path` (path or module-name mentions)."""
    path = Path(file_path)
    stem = path.stem
    patterns = [re.escape(file_path)]
    if file_path.startswith("scripts/workflow/"):
        name = re.escape(stem)
        patterns.append(rf"\bworkflow\.{name}\b")
        patterns.append(rf"\bfrom\s+(?:scripts\.)?workflow\s+import\s+[^\n]*\b{name}\b")
    else:
        patterns.append(rf"(?<![\w./-]){re.escape(path.name)}")
        if stem.isidentifier():
            patterns.append(rf"^\s*(?:from|import)\s+{re.escape(stem)}\b")
    return [re.compile(p, re.MULTILINE) for p in patterns]


def map_files_to_tests(files: list) -> tuple:
    """
    Map changed file paths to test files. Return (test_modules, unmapped_files).

    A changed scripts/*.py file selects every tests/test_*.py that names it (by path, file name or
    import) plus the test files named after it. A changed tests/test_*.py file selects itself. Anything
    else, including a non-test file under tests/, is unmapped, which sends the caller to the full suite.
    """
    tests_dir = TESTS_DIR
    test_files: Set[str] = set()
    unmapped: List[str] = []
    test_texts: Dict[str, str] = {}
    for tf in sorted(tests_dir.glob("test_*.py")):
        try:
            test_texts[tf.name] = tf.read_text(errors="replace")
        except OSError:
            continue

    for file_path in files:
        if file_path.startswith("scripts/") and file_path.endswith(".py"):
            base_name = Path(file_path).stem
            if file_path.startswith("scripts/workflow/"):
                matching = {m.name for m in tests_dir.glob(f"test_workflow_{base_name}*.py")}
            else:
                matching = {m.name for m in tests_dir.glob(f"test_{base_name}*.py")}
            # Only a file that exists can be referenced; a deleted or fake path is not grepped (the
            # grep would otherwise match its own mention in a test's data) and stays unmapped.
            if (REPO_ROOT / file_path).is_file():
                patterns = _reference_patterns(file_path)
                for name, text in test_texts.items():
                    if any(p.search(text) for p in patterns):
                        matching.add(name)
            if matching:
                test_files.update(matching)
            else:
                unmapped.append(file_path)
        elif file_path.startswith("tests/"):
            test_name = Path(file_path).name
            if "/" not in file_path[len("tests/"):] and test_name.endswith(".py") and test_name.startswith("test_"):
                test_files.add(test_name)
            else:
                # A helper, runner or fixture under tests/ can affect any suite
                unmapped.append(file_path)
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
