#!/usr/bin/env python3
"""
Test suite for mypy static type checking support in scripts/workflow/.

This test suite verifies that:
1. .gitignore contains a .mypy_cache/ entry and git correctly ignores files inside it
2. just typecheck's preflight check: when mypy is not importable, it prints an actionable
   error message and exits non-zero (not a raw "command not found")
3. mypy.ini config sanity: python_version = 3.8, disallow_untyped_defs = True,
   check_untyped_defs = True, files = scripts/workflow, cache_dir = .mypy_cache

Run with: python3 tests/test_workflow_mypy_support.py
"""

import os
import subprocess
import sys
from configparser import ConfigParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT


def test_gitignore_contains_mypy_cache_entry():
    """Test that .gitignore contains .mypy_cache/ entry."""
    gitignore_path = REPO_ROOT / ".gitignore"
    if not gitignore_path.exists():
        return False, "no .gitignore found"

    content = gitignore_path.read_text()
    if ".mypy_cache/" not in content:
        return False, ".gitignore does not contain '.mypy_cache/' entry"

    return True, ""


def test_git_ignores_mypy_cache_files():
    """Test that git check-ignore correctly marks files in .mypy_cache as ignored."""
    # Use a temporary directory so we don't pollute the actual repo
    test_file_path = ".mypy_cache/test_file.txt"

    result = subprocess.run(
        ["git", "check-ignore", test_file_path],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    # git check-ignore returns 0 if the path is ignored, non-zero if not ignored
    if result.returncode != 0:
        return False, f"git check-ignore did not mark {test_file_path} as ignored"

    return True, ""


def test_mypy_ini_exists():
    """Test that mypy.ini exists."""
    mypy_ini_path = REPO_ROOT / "mypy.ini"
    if not mypy_ini_path.exists():
        return False, "mypy.ini not found"

    return True, ""


def test_mypy_ini_python_version():
    """Test that mypy.ini has python_version = 3.8."""
    mypy_ini_path = REPO_ROOT / "mypy.ini"
    config = ConfigParser()
    config.read(mypy_ini_path)

    if not config.has_option("mypy", "python_version"):
        return False, "mypy.ini does not have python_version setting"

    version = config.get("mypy", "python_version")
    if version != "3.8":
        return False, f"python_version is {version}, expected 3.8"

    return True, ""


def test_mypy_ini_disallow_untyped_defs():
    """Test that mypy.ini has disallow_untyped_defs = True."""
    mypy_ini_path = REPO_ROOT / "mypy.ini"
    config = ConfigParser()
    config.read(mypy_ini_path)

    if not config.has_option("mypy", "disallow_untyped_defs"):
        return False, "mypy.ini does not have disallow_untyped_defs setting"

    value = config.get("mypy", "disallow_untyped_defs")
    if value.lower() != "true":
        return False, f"disallow_untyped_defs is {value}, expected True"

    return True, ""


def test_mypy_ini_check_untyped_defs():
    """Test that mypy.ini has check_untyped_defs = True."""
    mypy_ini_path = REPO_ROOT / "mypy.ini"
    config = ConfigParser()
    config.read(mypy_ini_path)

    if not config.has_option("mypy", "check_untyped_defs"):
        return False, "mypy.ini does not have check_untyped_defs setting"

    value = config.get("mypy", "check_untyped_defs")
    if value.lower() != "true":
        return False, f"check_untyped_defs is {value}, expected True"

    return True, ""


def test_mypy_ini_files_setting():
    """Test that mypy.ini has files = scripts/workflow."""
    mypy_ini_path = REPO_ROOT / "mypy.ini"
    config = ConfigParser()
    config.read(mypy_ini_path)

    if not config.has_option("mypy", "files"):
        return False, "mypy.ini does not have files setting"

    files = config.get("mypy", "files")
    if files != "scripts/workflow":
        return False, f"files is {files}, expected scripts/workflow"

    return True, ""


def test_mypy_ini_cache_dir():
    """Test that mypy.ini has cache_dir = .mypy_cache."""
    mypy_ini_path = REPO_ROOT / "mypy.ini"
    config = ConfigParser()
    config.read(mypy_ini_path)

    if not config.has_option("mypy", "cache_dir"):
        return False, "mypy.ini does not have cache_dir setting"

    cache_dir = config.get("mypy", "cache_dir")
    if cache_dir != ".mypy_cache":
        return False, f"cache_dir is {cache_dir}, expected .mypy_cache"

    return True, ""


def test_just_typecheck_preflight_check():
    """
    Test that 'just typecheck' prints an actionable error and exits non-zero
    when mypy is not importable, instead of a raw "command not found".

    Forces mypy to be unimportable by disabling user site-packages
    (PYTHONNOUSERSITE=1) rather than masking PATH, since python3 itself
    must stay on PATH for the preflight's `python3 -c "import mypy"` to run.
    """
    just_check = subprocess.run(["which", "just"], capture_output=True, text=True)
    if just_check.returncode != 0:
        return True, "(just not in PATH, test skipped)"

    env = dict(os.environ)
    env["PYTHONNOUSERSITE"] = "1"
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        ["just", "typecheck"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )

    output = result.stdout + result.stderr

    if "No module named 'mypy'" not in output and "ModuleNotFoundError" not in output:
        # mypy is still importable even with user site-packages disabled
        # (e.g. installed into the system/venv site-packages) - can't
        # exercise the failure path in this environment.
        return True, "(mypy still importable with PYTHONNOUSERSITE=1, test skipped)"

    if result.returncode == 0:
        return False, "typecheck exited 0 despite mypy being unimportable"

    if "command not found" in output:
        return False, f"got raw 'command not found' instead of the actionable message: {output[:200]}"

    if "mypy not found" not in output or "pip install" not in output or "requirements-dev.txt" not in output:
        return False, f"preflight message is not actionable: {output[:200]}"

    return True, ""


def test_requirements_dev_has_mypy():
    """Test that requirements-dev.txt includes mypy."""
    req_path = REPO_ROOT / "requirements-dev.txt"
    if not req_path.exists():
        return False, "requirements-dev.txt not found"

    content = req_path.read_text()
    if "mypy" not in content:
        return False, "requirements-dev.txt does not mention mypy"

    # Check for the specific version pinning
    if "mypy==" not in content:
        return (
            False,
            "mypy version is not pinned with == (should be mypy==X.Y.Z)",
        )

    return True, ""


if __name__ == "__main__":
    h = Harness("WORKFLOW MYPY SUPPORT TEST SUITE")
    test_result = h.test_result

    print("[Section 1] .gitignore configuration")
    passed, msg = test_gitignore_contains_mypy_cache_entry()
    test_result(".gitignore contains .mypy_cache/ entry", passed, msg)

    passed, msg = test_git_ignores_mypy_cache_files()
    test_result("git correctly ignores .mypy_cache/ files", passed, msg)

    print()
    print("[Section 2] mypy.ini configuration sanity")
    passed, msg = test_mypy_ini_exists()
    test_result("mypy.ini exists", passed, msg)

    passed, msg = test_mypy_ini_python_version()
    test_result("mypy.ini has python_version = 3.8", passed, msg)

    passed, msg = test_mypy_ini_disallow_untyped_defs()
    test_result("mypy.ini has disallow_untyped_defs = True", passed, msg)

    passed, msg = test_mypy_ini_check_untyped_defs()
    test_result("mypy.ini has check_untyped_defs = True", passed, msg)

    passed, msg = test_mypy_ini_files_setting()
    test_result("mypy.ini has files = scripts/workflow", passed, msg)

    passed, msg = test_mypy_ini_cache_dir()
    test_result("mypy.ini has cache_dir = .mypy_cache", passed, msg)

    print()
    print("[Section 3] just typecheck preflight check")
    passed, msg = test_requirements_dev_has_mypy()
    test_result("requirements-dev.txt includes pinned mypy version", passed, msg)

    passed, msg = test_just_typecheck_preflight_check()
    test_result(
        "just typecheck prints actionable error when mypy not available", passed, msg
    )

    print()
    h.summarize_and_exit()
