#!/usr/bin/env python3
"""
Check minimum selector (scripts/check-minimum.py): file-to-test mapping, base/file precedence,
and main() routing (#462 Part A).

Gate subprocess calls (just lint, just typecheck, test runs) and the changed-file source are mocked,
so nothing here runs lint, typecheck or the test suite. The --dry-run seam is exercised for real
(it runs no gate), including one sandboxed invocation in a throwaway git repo. Real git-backed
canonical_changed_files behaviour is tested in test_workflow_checks.py.

Run with: python3 tests/test_check_minimum_selector.py
"""
import importlib.util
import json
import os
import subprocess
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

        modules, unmapped = selector.map_files_to_tests(["scripts/workflow/checks.py"])
        test_result(
            "a changed module also selects tests that reference it under another name (grep mapping)",
            "test_check_receipt.py" in modules and "test_workflow_merge.py" in modules,
            f"modules={modules}",
        )

        modules, unmapped = selector.map_files_to_tests(["tests/_test_harness.py"])
        test_result(
            "a non-test helper under tests/ is unmapped (falls back to the full suite)",
            modules == [] and unmapped == ["tests/_test_harness.py"],
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
        "mapped changes run only the mapped test modules, each as a standalone script",
        rc == 0 and calls[-1] == ["python3", "tests/test_workflow_checks_cli.py"],
        f"rc={rc} calls={calls}",
    )

    print("[Section 3b] build_plan, base-unresolved full suite, and the --dry-run seam")
    plan = selector.build_plan("some-base", ["tests/test_workflow_checks_cli.py"])
    test_result(
        "build_plan with a resolved base and mapped files selects only those modules",
        plan["tests"] == ["test_workflow_checks_cli.py"] and plan["full_suite"] is False
        and plan["fallback_reason"] is None,
        f"plan={plan}",
    )

    plan = selector.build_plan("some-base", ["scripts/no-such-module-462.py"])
    test_result(
        "build_plan with an unmappable path selects the full suite and says why",
        plan["full_suite"] is True and plan["tests"] == [] and "unmappable" in (plan["fallback_reason"] or ""),
        f"plan={plan}",
    )

    plan = selector.build_plan(None, ["tests/test_workflow_checks_cli.py"])
    test_result(
        "build_plan with an unresolved base selects the full suite (changed set unknown)",
        plan["full_suite"] is True and plan["fallback_reason"] == "base unresolved",
        f"plan={plan}",
    )

    calls_unresolved = []

    def _fake_unresolved(argv, **kwargs):
        calls_unresolved.append(list(argv))
        return _Done(0)

    prev = os.getcwd()
    os.chdir(REPO)
    try:
        with mock.patch.object(selector, "get_check_base_and_files", return_value=(None, [])), \
                mock.patch.object(selector.subprocess, "run", side_effect=_fake_unresolved):
            rc_unresolved = selector.main([])
    finally:
        os.chdir(prev)
    test_result(
        "an unresolved base runs lint, typecheck and the full suite (not the no-tests skip)",
        rc_unresolved == 0 and calls_unresolved[-1] == ["python3", "tests/run_all.py"],
        f"rc={rc_unresolved} calls={calls_unresolved}",
    )

    import contextlib
    import io

    dry_calls = []

    def _fail_if_run(argv, **kwargs):
        dry_calls.append(list(argv))
        raise AssertionError(f"--dry-run must not run gates: {argv}")

    buf = io.StringIO()
    with mock.patch.object(selector, "get_check_base_and_files",
                           return_value=("b", ["tests/test_workflow_checks_cli.py"])), \
            mock.patch.object(selector.subprocess, "run", side_effect=_fail_if_run), \
            contextlib.redirect_stdout(buf):
        rc_dry = selector.main(["--dry-run"])
    try:
        dry_plan = json.loads(buf.getvalue())
    except ValueError:
        dry_plan = {}
    test_result(
        "--dry-run prints the plan as JSON, exits 0, and runs no gate",
        rc_dry == 0 and dry_calls == [] and dry_plan.get("tests") == ["test_workflow_checks_cli.py"],
        f"rc={rc_dry} calls={dry_calls} plan={dry_plan}",
    )

    print("[Section 3c] sandboxed selection: the outer environment does not change the plan")
    with tempfile.TemporaryDirectory() as tmp:
        origin = Path(tmp) / "origin.git"
        work = Path(tmp) / "work"
        git_id = ["-c", "user.email=t@example.invalid", "-c", "user.name=t"]

        def _g(cwd, *args):
            return subprocess.run(["git", *git_id, *args], cwd=str(cwd), capture_output=True, text=True, check=True)

        subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-q", str(work)], check=True, capture_output=True)
        _g(work, "checkout", "-q", "-b", "main")
        (work / "README.md").write_text("base\n")
        _g(work, "add", "README.md")
        _g(work, "commit", "-q", "-m", "base")
        _g(work, "remote", "add", "origin", str(origin))
        _g(work, "push", "-q", "origin", "main")
        _g(work, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        _g(work, "checkout", "-q", "-b", "feature")
        (work / "tests").mkdir()
        (work / "tests" / "test_workflow_checks_cli.py").write_text("# feature\n")
        _g(work, "add", "tests/test_workflow_checks_cli.py")
        _g(work, "commit", "-q", "-m", "feature")

        def _dry_run(env):
            proc = subprocess.run(
                [sys.executable, str(REPO / "scripts" / "check-minimum.py"), "--dry-run"],
                cwd=str(work), env=env, capture_output=True, text=True,
            )
            return proc.returncode, proc.stdout

        clean_env = {"PATH": os.environ.get("PATH", ""), "HOME": tmp}
        noisy_env = {k: v for k, v in os.environ.items() if k not in ("CHECK_BASE", "CHECK_CHANGED_FILES")}
        noisy_env["HOME"] = tmp

        rc_a, out_a = _dry_run(clean_env)
        rc_b, out_b = _dry_run(noisy_env)
        plan_a = json.loads(out_a) if rc_a == 0 and out_a.strip() else {}
        test_result(
            "the selection is identical under a clean sandbox and a noisy environment (no HOME or config leakage)",
            rc_a == 0 and rc_b == 0 and out_a == out_b and plan_a.get("tests") == ["test_workflow_checks_cli.py"],
            f"rc_a={rc_a} rc_b={rc_b} out_a={out_a!r} out_b={out_b!r}",
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

    print("[Section 3d] selector and validate agree: the selector's list is the canonical list for the worktree")
    from workflow.checks import resolve_check_base, canonical_changed_files
    with tempfile.TemporaryDirectory() as tmp:
        origin = Path(tmp) / "origin.git"
        work = Path(tmp) / "work"
        git_id = ["-c", "user.email=t@example.invalid", "-c", "user.name=t"]

        def _g(cwd, *args):
            return subprocess.run(["git", *git_id, *args], cwd=str(cwd), capture_output=True, text=True, check=True)

        subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-q", str(work)], check=True, capture_output=True)
        _g(work, "checkout", "-q", "-b", "main")
        (work / "README.md").write_text("base\n")
        _g(work, "add", "README.md")
        _g(work, "commit", "-q", "-m", "base")
        _g(work, "remote", "add", "origin", str(origin))
        _g(work, "push", "-q", "origin", "main")
        _g(work, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        _g(work, "checkout", "-q", "-b", "feature")
        (work / "committed.txt").write_text("c\n")
        _g(work, "add", "committed.txt")
        _g(work, "commit", "-q", "-m", "feature")
        (work / "staged.txt").write_text("s\n")
        _g(work, "add", "staged.txt")
        (work / "untracked.txt").write_text("u\n")

        prev_cwd = os.getcwd()
        try:
            os.chdir(str(work))
            with _with_env(CHECK_BASE=None, CHECK_CHANGED_FILES=None):
                sel_base, sel_files = selector.get_check_base_and_files()
        finally:
            os.chdir(prev_cwd)
        canon_base = resolve_check_base(work)
        canon_files = canonical_changed_files(work, canon_base) if canon_base else None
        test_result(
            "selector base and changed-file list equal resolve_check_base and canonical_changed_files for the same worktree",
            sel_base == canon_base and sel_files == canon_files
            and "committed.txt" in sel_files and "staged.txt" in sel_files and "untracked.txt" in sel_files,
            f"selector=({sel_base!r}, {sel_files!r}) canonical=({canon_base!r}, {canon_files!r})",
        )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
