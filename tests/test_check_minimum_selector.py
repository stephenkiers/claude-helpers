#!/usr/bin/env python3
"""
Check minimum selector (scripts/check-minimum.py): file-to-test mapping, base/file precedence,
and main() routing (#462 Part A).

Subprocess calls and the changed-file source are mocked, so nothing here runs lint, typecheck or
the test suite. Real git-backed canonical_changed_files behaviour is tested in
test_workflow_checks.py.

Run with: python3 tests/test_check_minimum_selector.py
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_harness import Harness

REPO = Path(__file__).resolve().parent.parent


def _load_selector():
    spec = importlib.util.spec_from_file_location("check_minimum_selector", REPO / "scripts" / "check-minimum.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


selector = _load_selector()


class _Done:
    def __init__(self, returncode=0):
        self.returncode = returncode


def _with_env(**changes):
    """Context manager: set/unset environment variables, restoring them on exit."""
    class _Ctx:
        def __enter__(self):
            self.saved = {k: os.environ.get(k) for k in changes}
            for k, v in changes.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        def __exit__(self, *exc):
            for k, v in self.saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            return False

    return _Ctx()


def main():
    h = Harness("check minimum selector")
    test_result = h.test_result

    print("[Section 1] map_files_to_tests: mapping and unmapped fallback")
    prev_cwd = os.getcwd()
    os.chdir(REPO)  # map_files_to_tests globs tests/ relative to the cwd
    try:
        modules, unmapped = selector.map_files_to_tests(["scripts/workflow/checks.py"])
        test_result(
            "scripts/workflow/checks.py maps to tests/test_workflow_checks*.py",
            "test_workflow_checks.py" in modules and unmapped == [],
            f"modules={modules} unmapped={unmapped}",
        )

        modules, unmapped = selector.map_files_to_tests(["tests/test_workflow_checks_cli.py"])
        test_result(
            "a changed tests/test_*.py file maps to itself",
            modules == ["test_workflow_checks_cli.py"] and unmapped == [],
            f"modules={modules} unmapped={unmapped}",
        )

        modules, unmapped = selector.map_files_to_tests(["scripts/no-such-module-462.py"])
        test_result(
            "a scripts/*.py file with no matching test is unmapped (falls back to the full suite)",
            modules == [] and unmapped == ["scripts/no-such-module-462.py"],
            f"modules={modules} unmapped={unmapped}",
        )

        modules, unmapped = selector.map_files_to_tests(["commands/merge-and-cleanup.md"])
        test_result(
            "a non-Python file is unmapped",
            modules == [] and unmapped == ["commands/merge-and-cleanup.md"],
            f"modules={modules} unmapped={unmapped}",
        )

        modules, unmapped = selector.map_files_to_tests(
            ["scripts/workflow/checks.py", "scripts/no-such-module-462.py"]
        )
        test_result(
            "a mixed list keeps mapped modules and reports the unmapped path",
            "test_workflow_checks.py" in modules and unmapped == ["scripts/no-such-module-462.py"],
            f"modules={modules} unmapped={unmapped}",
        )
    finally:
        os.chdir(prev_cwd)

    print("[Section 2] get_check_base_and_files: CHECK_CHANGED_FILES, CHECK_BASE and computed precedence")
    real_resolve = selector.resolve_check_base
    real_canonical = selector.canonical_changed_files
    try:
        selector.resolve_check_base = lambda worktree: "computed-base"
        selector.canonical_changed_files = lambda worktree, base: [f"from-{base}"]

        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "check-env.txt"
            env_file.write_text("EXPORTED-BASE\nscripts/a.py\0tests/test_b.py\0")
            with _with_env(CHECK_CHANGED_FILES=str(env_file), CHECK_BASE="ignored-when-file-set"):
                base, files = selector.get_check_base_and_files()
        test_result(
            "CHECK_CHANGED_FILES: base is the first line and files are the NUL-separated remainder",
            base == "EXPORTED-BASE" and files == ["scripts/a.py", "tests/test_b.py"],
            f"base={base!r} files={files!r}",
        )

        with _with_env(CHECK_CHANGED_FILES=None, CHECK_BASE="exported-base-only"):
            base, files = selector.get_check_base_and_files()
        test_result(
            "CHECK_BASE alone is honoured (not replaced by a recomputed merge-base)",
            base == "exported-base-only" and files == ["from-exported-base-only"],
            f"base={base!r} files={files!r}",
        )

        with _with_env(CHECK_CHANGED_FILES=None, CHECK_BASE=None):
            base, files = selector.get_check_base_and_files()
        test_result(
            "with no override env, base and files are computed from git",
            base == "computed-base" and files == ["from-computed-base"],
            f"base={base!r} files={files!r}",
        )

        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist.txt"
            with _with_env(CHECK_CHANGED_FILES=str(missing), CHECK_BASE=None):
                base, files = selector.get_check_base_and_files()
        test_result(
            "a CHECK_CHANGED_FILES path that does not exist falls back to computing",
            base == "computed-base" and files == ["from-computed-base"],
            f"base={base!r} files={files!r}",
        )
    finally:
        selector.resolve_check_base = real_resolve
        selector.canonical_changed_files = real_canonical

    print("[Section 3] main(): lint, typecheck, then mapped tests or the full suite")

    def _run_main(files, lint_rc=0, typecheck_rc=0):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            if argv == ["just", "lint"]:
                return _Done(lint_rc)
            if argv == ["just", "typecheck"]:
                return _Done(typecheck_rc)
            return _Done(0)

        prev = os.getcwd()
        os.chdir(REPO)
        try:
            with mock.patch.object(selector, "get_check_base_and_files", return_value=("b", files)), \
                    mock.patch.object(selector.subprocess, "run", side_effect=fake_run):
                rc = selector.main()
        finally:
            os.chdir(prev)
        return rc, calls

    rc, calls = _run_main(["scripts/no-such-module-462.py"])
    test_result(
        "an unmapped changed path runs lint, typecheck, then the full suite",
        rc == 0 and calls == [["just", "lint"], ["just", "typecheck"], ["python3", "tests/run_all.py"]],
        f"rc={rc} calls={calls}",
    )

    rc, calls = _run_main(["tests/test_workflow_checks_cli.py"])
    test_result(
        "mapped changes run only the mapped test modules under pytest",
        rc == 0 and calls[-1] == ["python3", "-m", "pytest", "-xvs", "tests/test_workflow_checks_cli.py"],
        f"rc={rc} calls={calls}",
    )

    rc, calls = _run_main([])
    test_result(
        "no changed files: lint and typecheck run, no test run, exit 0",
        rc == 0 and calls == [["just", "lint"], ["just", "typecheck"]],
        f"rc={rc} calls={calls}",
    )

    rc, calls = _run_main(["tests/test_workflow_checks_cli.py"], lint_rc=1)
    test_result(
        "a lint failure stops before typecheck and tests, exit 1",
        rc == 1 and calls == [["just", "lint"]],
        f"rc={rc} calls={calls}",
    )

    rc, calls = _run_main(["tests/test_workflow_checks_cli.py"], typecheck_rc=1)
    test_result(
        "a typecheck failure stops before tests, exit 1",
        rc == 1 and calls == [["just", "lint"], ["just", "typecheck"]],
        f"rc={rc} calls={calls}",
    )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
