#!/usr/bin/env python3
"""
Test suite for check ordering and execution.

Run with: python3 tests/test_workflow_checks.py
"""

import contextlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import workflow.checks as checks_module
from workflow.checks import (
    execute_check, build_check_order, resolve_check_command, resolve_check_timeout,
    resolve_check_detail, detect_package_json_check, suggest_from_claude_md,
    ensure_check, record_check_answer, OPT_OUT_SOURCE, NO_CHECK_CONFIGURED_MESSAGE,
    worktree_tree, canonical_changed_files, resolve_check_base,
)
from workflow.deps import LOCKFILE_MANAGERS
from _test_harness import Harness


@contextlib.contextmanager
def _env_unset(name, value=None):
    """Set (or unset when value is None) an environment variable for the duration of the block."""
    saved = os.environ.pop(name, None)
    if value is not None:
        os.environ[name] = value
    try:
        yield
    finally:
        os.environ.pop(name, None)
        if saved is not None:
            os.environ[name] = saved


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), env={**os.environ, **_GIT_ENV},
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _clone_fixture(root):
    """Bare origin with three tracked files on main, cloned as `work` on branch `feature`."""
    seed = root / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _write(seed / ".gitignore", "ignored.log\n")
    _write(seed / "keep.txt", "keep\n")
    _write(seed / "old.txt", "old\n")
    _write(seed / "gone.txt", "gone\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "init")
    origin = root / "origin.git"
    _git(root, "clone", "-q", "--bare", str(seed), str(origin))
    work = root / "work"
    _git(root, "clone", "-q", str(origin), str(work))
    _git(work, "checkout", "-q", "-b", "feature")
    return work


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


if __name__ == "__main__":
    h = Harness("WORKFLOW CHECKS TEST SUITE")
    test_result = h.test_result

    print("[Section 1] build_check_order with standard commands")

    commands = {
        "format": "prettier --write .",
        "check": "custom-check",
        "lint": "eslint .",
        "typecheck": "tsc --noEmit",
        "test": "jest",
        "build": "npm run build"
    }
    planned, skipped = build_check_order(commands)

    test_result(
        "build_check_order: returns tuple",
        isinstance(planned, list) and isinstance(skipped, list)
    )
    test_result(
        "build_check_order: format first",
        len(planned) > 0 and planned[0] == "format"
    )
    test_result(
        "build_check_order: check present",
        "check" in planned
    )
    test_result(
        "build_check_order: lint suppressed by check",
        "lint" not in planned and any(s.command_type == "lint" and s.reason == "superseded_by_check" for s in skipped)
    )
    test_result(
        "build_check_order: typecheck suppressed by check",
        "typecheck" not in planned and any(s.command_type == "typecheck" and s.reason == "superseded_by_check" for s in skipped)
    )
    test_result(
        "build_check_order: test and build still run",
        "test" in planned and "build" in planned
    )

    print()
    print("[Section 2] build_check_order with null commands")

    commands = {
        "format": None,
        "check": None,
        "lint": "eslint .",
        "test": "jest"
    }
    planned, skipped = build_check_order(commands)

    test_result(
        "build_check_order: skips null format",
        any(s.command_type == "format" and s.reason == "null_command" for s in skipped)
    )
    test_result(
        "build_check_order: includes lint when no check",
        "lint" in planned
    )
    test_result(
        "build_check_order: includes test",
        "test" in planned
    )

    print()
    print("[Section 3] build_check_order preserves CHECK_ORDER")

    commands = {
        "format": "fmt",
        "lint": "lint",
        "typecheck": "type",
        "test": "test",
        "build": "build"
    }
    planned, skipped = build_check_order(commands)

    test_result(
        "build_check_order: format before lint",
        planned.index("format") < planned.index("lint")
    )
    test_result(
        "build_check_order: test before build",
        planned.index("test") < planned.index("build")
    )

    print()
    print("[Section 4] Check execution (Fix 11)")

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
    print("[Section 5] build_check_order with extras and install")

    commands = {
        "format": "fmt",
        "install": "npm install",
        "lint": "lint",
        "test": "test",
        "custom": "custom-check",
        "build": "build"
    }
    planned, skipped = build_check_order(commands)

    test_result(
        "build_check_order: skips install (not_a_check)",
        "install" not in planned and any(s.command_type == "install" and s.reason == "not_a_check" for s in skipped)
    )
    test_result(
        "build_check_order: includes custom command",
        "custom" in planned
    )
    test_result(
        "build_check_order: custom between test and build",
        planned.index("test") < planned.index("custom") < planned.index("build")
    )

    print()
    print("[Section 6] resolve_check_command: checkOptOut (#338)")

    with tempfile.TemporaryDirectory() as tmp, _env_unset("SHIPIT_CHECK_TIMEOUT_SECS"):
        root = Path(tmp)
        wt, main = root / "wt", root / "main"
        wt.mkdir()
        main.mkdir()

        # (1) opt-out in the worktree project.yaml, nothing configured
        _write(wt / ".claude" / "project.yaml", 'checkOptOut: "docs-only repo"\n')
        command, source, failure = resolve_check_command(wt, main)
        test_result(
            "(1) worktree checkOptOut with no command: command None, source 'opt-out'",
            command is None and source == "opt-out",
            f"command={command!r} source={source!r}",
        )
        test_result(
            "(1) opt-out failure_reason is 'check opted out: <reason> ...' with the reason",
            failure is not None and failure.startswith("check opted out: docs-only repo"),
            f"failure={failure!r}",
        )
        test_result(
            "(1) opt-out failure_reason carries the implement/fix-only scope note",
            failure is not None and "implement/fix gate only" in failure and "verify and /shipit still require a check" in failure,
            f"failure={failure!r}",
        )

    with tempfile.TemporaryDirectory() as tmp, _env_unset("SHIPIT_CHECK_TIMEOUT_SECS"):
        root = Path(tmp)
        wt, main = root / "wt", root / "main"
        wt.mkdir()
        main.mkdir()

        # (2) opt-out only in the main worktree project.yaml
        _write(main / ".claude" / "project.yaml", 'checkOptOut: "docs-only repo"\n')
        command, source, failure = resolve_check_command(wt, main)
        test_result(
            "(2) main-worktree checkOptOut is honoured when the worktree has none",
            command is None and source == "opt-out" and failure is not None and "docs-only repo" in failure,
            f"command={command!r} source={source!r} failure={failure!r}",
        )

    with tempfile.TemporaryDirectory() as tmp, _env_unset("SHIPIT_CHECK_TIMEOUT_SECS"):
        root = Path(tmp)
        wt, main = root / "wt", root / "main"
        wt.mkdir()
        main.mkdir()

        # (3) a resolved command always wins over checkOptOut
        _write(wt / ".claude" / "project.yaml", 'checkOptOut: "docs-only repo"\n')
        _write(wt / ".claude" / "repo-cache.json", json.dumps({"commands": {"check": "just check"}}))
        command, source, failure = resolve_check_command(wt, main)
        test_result(
            "(3) command configured plus checkOptOut: the command wins",
            command == "just check" and source != "opt-out" and failure is None,
            f"command={command!r} source={source!r} failure={failure!r}",
        )

    with tempfile.TemporaryDirectory() as tmp, _env_unset("SHIPIT_CHECK_TIMEOUT_SECS"):
        root = Path(tmp)
        wt, main = root / "wt", root / "main"
        wt.mkdir()
        main.mkdir()

        # (4) a whitespace-only checkOptOut counts as unset: not-configured failure
        _write(wt / ".claude" / "project.yaml", 'checkOptOut: "   "\n')
        command, source, failure = resolve_check_command(wt, main)
        test_result(
            "(4) whitespace-only checkOptOut is unset: command None, source None, not-configured failure",
            command is None and source is None and failure is not None and "no check command configured" in failure,
            f"command={command!r} source={source!r} failure={failure!r}",
        )

    print()
    print("[Section 7] resolve_check_command: not-configured failure names the file and key (#338)")

    with tempfile.TemporaryDirectory() as tmp, _env_unset("SHIPIT_CHECK_TIMEOUT_SECS"):
        root = Path(tmp)
        wt, main = root / "wt", root / "main"
        wt.mkdir()
        main.mkdir()
        _write(wt / ".claude" / "project.yaml", "name: nothing-here\n")

        command, source, failure = resolve_check_command(wt, main)
        failure = failure or ""
        for token in (".claude/project.yaml", "commands.check", "checkOptOut", "verify and ship"):
            test_result(
                f"(5) not-configured failure_reason contains {token!r}",
                token in failure,
                f"failure={failure[:300]!r}",
            )

    print()
    print("[Section 8] resolve_check_timeout: the timeout follows the command's source (#338)")

    with tempfile.TemporaryDirectory() as tmp, _env_unset("SHIPIT_CHECK_TIMEOUT_SECS"):
        root = Path(tmp)
        wt, main = root / "wt", root / "main"
        wt.mkdir()
        main.mkdir()

        # (6) no worktree cache, a justfile check, main repo-cache sets 900 with no check key
        _write(wt / "justfile", "check:\n\techo ok\n")
        _write(main / ".claude" / "repo-cache.json", json.dumps({"checkTimeoutSecs": 900}))
        command, source, failure = resolve_check_command(wt, main)
        seconds, timeout_source = resolve_check_timeout(wt, main, None, "repo-cache", None)
        test_result(
            "(6) justfile check with no worktree cache resolves 'just check' from the justfile",
            command == "just check" and source == "justfile",
            f"command={command!r} source={source!r}",
        )
        test_result(
            "(6) timeout is the main repo-cache's 900, not the 300 default",
            seconds == 900 and "repo-cache" in timeout_source,
            f"seconds={seconds!r} source={timeout_source!r}",
        )

    with tempfile.TemporaryDirectory() as tmp, _env_unset("SHIPIT_CHECK_TIMEOUT_SECS"):
        root = Path(tmp)
        wt, main = root / "wt", root / "main"
        wt.mkdir()
        main.mkdir()

        # (7) the command comes from the main repo-cache (900); the worktree cache has another value, no check key
        _write(main / ".claude" / "repo-cache.json", json.dumps({"commands": {"check": "just check"}, "checkTimeoutSecs": 900}))
        _write(wt / ".claude" / "repo-cache.json", json.dumps({"checkTimeoutSecs": 60}))
        command, source, failure = resolve_check_command(wt, main)
        seconds, timeout_source = resolve_check_timeout(wt, main, main / ".claude" / "repo-cache.json", "repo-cache", "main")
        test_result(
            "(7) command from the main repo-cache: main timeout (900) wins over the worktree cache (60)",
            command == "just check" and seconds == 900,
            f"command={command!r} seconds={seconds!r} source={timeout_source!r}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        wt, main = root / "wt", root / "main"
        wt.mkdir()
        main.mkdir()

        # (8a) nothing sets a timeout: 300, and the override is absent
        with _env_unset("SHIPIT_CHECK_TIMEOUT_SECS"):
            seconds, timeout_source = resolve_check_timeout(wt, main, None, "repo-cache", None)
        test_result(
            "(8) nothing sets checkTimeoutSecs: 300 default",
            seconds == 300,
            f"seconds={seconds!r} source={timeout_source!r}",
        )

        # (8b) SHIPIT_CHECK_TIMEOUT_SECS overrides every file, including a worktree cache value
        _write(wt / ".claude" / "repo-cache.json", json.dumps({"checkTimeoutSecs": 900}))
        with _env_unset("SHIPIT_CHECK_TIMEOUT_SECS", "42"):
            seconds, timeout_source = resolve_check_timeout(wt, main, None, "repo-cache", None)
        test_result(
            "(8) SHIPIT_CHECK_TIMEOUT_SECS=42 overrides the cache's 900",
            seconds == 42,
            f"seconds={seconds!r} source={timeout_source!r}",
        )

    # (#338 mutation-b) zero and non-integer timeouts are invalid everywhere: fall through to the default
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        wt, main = root / "wt", root / "main"
        wt.mkdir()
        main.mkdir()
        with _env_unset("SHIPIT_CHECK_TIMEOUT_SECS", "0"):
            seconds, timeout_source = resolve_check_timeout(wt, main, None, "repo-cache", None)
        test_result(
            "(8) SHIPIT_CHECK_TIMEOUT_SECS=0 falls through to the 300 default",
            seconds == 300 and timeout_source == "default",
            f"seconds={seconds!r} source={timeout_source!r}",
        )
        with _env_unset("SHIPIT_CHECK_TIMEOUT_SECS", "abc"):
            seconds, timeout_source = resolve_check_timeout(wt, main, None, "repo-cache", None)
        test_result(
            "(8) non-integer SHIPIT_CHECK_TIMEOUT_SECS falls through to the 300 default",
            seconds == 300 and timeout_source == "default",
            f"seconds={seconds!r} source={timeout_source!r}",
        )
        _write(wt / ".claude" / "repo-cache.json", json.dumps({"checkTimeoutSecs": 0}))
        with _env_unset("SHIPIT_CHECK_TIMEOUT_SECS"):
            seconds, timeout_source = resolve_check_timeout(wt, main, None, "repo-cache", None)
        test_result(
            "(8) checkTimeoutSecs 0 in the cache falls through to the 300 default",
            seconds == 300 and timeout_source.startswith("default"),
            f"seconds={seconds!r} source={timeout_source!r}",
        )
        test_result(
            "(8) the ignored cache 0 is named in timeout_source (#338 directive 1)",
            "ignored invalid checkTimeoutSecs in worktree repo-cache.json" in timeout_source,
            f"source={timeout_source!r}",
        )
        _write(wt / ".claude" / "repo-cache.json", json.dumps({"checkTimeoutSecs": 1}))
        with _env_unset("SHIPIT_CHECK_TIMEOUT_SECS"):
            seconds, _src = resolve_check_timeout(wt, main, None, "repo-cache", None)
        test_result("(8) checkTimeoutSecs 1 is the smallest accepted value", seconds == 1, f"seconds={seconds!r}")

    print()
    print("[Section 9] OPT_OUT_SOURCE is exported and reported by an opt-out resolution (from hardening spec)")

    test_result(
        "OPT_OUT_SOURCE is the string 'opt-out'",
        OPT_OUT_SOURCE == "opt-out",
        f"OPT_OUT_SOURCE={OPT_OUT_SOURCE!r}",
    )

    with tempfile.TemporaryDirectory() as tmp, _env_unset("SHIPIT_CHECK_TIMEOUT_SECS"):
        root = Path(tmp)
        wt, main = root / "wt", root / "main"
        wt.mkdir()
        main.mkdir()
        _write(wt / ".claude" / "project.yaml", 'checkOptOut: "docs-only repo"\n')
        command, source, failure = resolve_check_command(wt, main)
        test_result(
            "opt-out resolution: source equals OPT_OUT_SOURCE, command is None, failure reason present",
            source == OPT_OUT_SOURCE and command is None and failure is not None
            and "docs-only repo" in failure,
            f"command={command!r} source={source!r} failure={failure!r}",
        )

    print()
    print("[Section 10] Invalid checkOptOut edges are ignored with the non-empty-string note (from hardening spec)")

    with tempfile.TemporaryDirectory() as tmp, _env_unset("SHIPIT_CHECK_TIMEOUT_SECS"):
        root = Path(tmp)
        wt, main = root / "wt", root / "main"
        wt.mkdir()
        main.mkdir()
        _write(wt / ".claude" / "project.yaml", 'checkOptOut: ""\n')
        command, source, failure = resolve_check_command(wt, main)
        test_result(
            "empty-string checkOptOut is ignored: no opt-out source, no command",
            command is None and source is None and failure is not None
            and "no check command configured" in failure,
            f"command={command!r} source={source!r} failure={failure!r}",
        )
        test_result(
            "empty-string checkOptOut: failure reason says checkOptOut must be a non-empty string",
            failure is not None and "checkOptOut must be a non-empty string" in failure,
            f"failure={failure!r}",
        )

    with tempfile.TemporaryDirectory() as tmp, _env_unset("SHIPIT_CHECK_TIMEOUT_SECS"):
        root = Path(tmp)
        wt, main = root / "wt", root / "main"
        wt.mkdir()
        main.mkdir()
        _write(wt / ".claude" / "project.yaml", "checkOptOut: 42\n")
        command, source, failure = resolve_check_command(wt, main)
        test_result(
            "numeric checkOptOut is ignored: source is not opt-out, failure says non-empty string",
            source != OPT_OUT_SOURCE and command is None
            and failure is not None and "checkOptOut must be a non-empty string" in failure,
            f"command={command!r} source={source!r} failure={failure!r}",
        )

    print()
    print("[Section 11] Invalid project.yaml checkTimeoutSecs edges are named in timeout_source (from hardening spec)")

    with tempfile.TemporaryDirectory() as tmp, _env_unset("SHIPIT_CHECK_TIMEOUT_SECS"):
        root = Path(tmp)
        wt, main = root / "wt", root / "main"
        wt.mkdir()
        main.mkdir()
        _write(wt / ".claude" / "project.yaml", "commands:\n  check: just check\ncheckTimeoutSecs: -5\n")
        command, source, failure = resolve_check_command(wt, main)
        project_yaml = wt / ".claude" / "project.yaml"
        seconds, timeout_source = resolve_check_timeout(wt, main, project_yaml, "project.yaml", "worktree")
        test_result(
            "negative project.yaml checkTimeoutSecs (-5) is ignored; falls back to the 300 default",
            seconds == 300
            and timeout_source == "default (ignored invalid checkTimeoutSecs in worktree project.yaml)",
            f"seconds={seconds!r} timeout_source={timeout_source!r}",
        )
        test_result(
            "negative project.yaml checkTimeoutSecs (-5) is named in timeout_source",
            "ignored invalid checkTimeoutSecs in worktree project.yaml" in timeout_source,
            f"timeout_source={timeout_source!r}",
        )

    with tempfile.TemporaryDirectory() as tmp, _env_unset("SHIPIT_CHECK_TIMEOUT_SECS"):
        root = Path(tmp)
        wt, main = root / "wt", root / "main"
        wt.mkdir()
        main.mkdir()
        _write(wt / ".claude" / "project.yaml", "commands:\n  check: just check\ncheckTimeoutSecs: \"900\"\n")
        command, source, failure = resolve_check_command(wt, main)
        project_yaml = wt / ".claude" / "project.yaml"
        seconds, timeout_source = resolve_check_timeout(wt, main, project_yaml, "project.yaml", "worktree")
        test_result(
            "string project.yaml checkTimeoutSecs (\"900\") is named in timeout_source as ignored",
            "ignored invalid checkTimeoutSecs in worktree project.yaml" in timeout_source and seconds == 300,
            f"seconds={seconds!r} timeout_source={timeout_source!r}",
        )

    print()
    print("[Section 12] No-check-configured message: sources tried on its own line (from hardening spec)")

    with tempfile.TemporaryDirectory() as tmp, _env_unset("SHIPIT_CHECK_TIMEOUT_SECS"):
        root = Path(tmp)
        wt, main = root / "wt", root / "main"
        wt.mkdir()
        main.mkdir()
        command, source, failure = resolve_check_command(wt, main)
        lines = (failure or "").splitlines()
        test_result(
            "nothing configured: failure is the message's lines plus one 'sources tried' line",
            len(lines) == len(NO_CHECK_CONFIGURED_MESSAGE.splitlines()) + 1,
            f"failure={failure!r}",
        )
        test_result(
            "nothing configured: first line carries 'no check command configured'",
            bool(lines) and "no check command configured" in lines[0],
            f"lines={lines!r}",
        )
        tried_lines = [ln for ln in lines[1:] if "sources tried" in ln.lower()]
        test_result(
            "nothing configured: 'sources tried' is on its own line, separate from the first sentence",
            len(tried_lines) == 1 and "no check command configured" not in tried_lines[0],
            f"lines={lines!r}",
        )

    print()
    print("[Section 13] project.yaml checkTimeoutSecs and invalid checkOptOut (#338 directives 1-3)")

    with tempfile.TemporaryDirectory() as tmp, _env_unset("SHIPIT_CHECK_TIMEOUT_SECS"):
        root = Path(tmp)
        wt, main = root / "wt", root / "main"
        wt.mkdir()
        main.mkdir()

        # (9a) a valid project.yaml checkTimeoutSecs is honored for the command it resolved
        _write(wt / ".claude" / "project.yaml", "commands:\n  check: just check\ncheckTimeoutSecs: 900\n")
        command, source, failure = resolve_check_command(wt, main)
        project_yaml = wt / ".claude" / "project.yaml"
        seconds, timeout_source = resolve_check_timeout(wt, main, project_yaml, "project.yaml", "worktree")
        test_result(
            "(9) valid project.yaml checkTimeoutSecs 900 is honored when project.yaml supplied the command",
            command == "just check" and source == "project.yaml" and seconds == 900 and "worktree project.yaml" in timeout_source,
            f"command={command!r} source={source!r} seconds={seconds!r} timeout_source={timeout_source!r}",
        )

    with tempfile.TemporaryDirectory() as tmp, _env_unset("SHIPIT_CHECK_TIMEOUT_SECS"):
        root = Path(tmp)
        wt, main = root / "wt", root / "main"
        wt.mkdir()
        main.mkdir()

        # (9b) a boolean true is not a timeout: falls back to the 300 default and names the ignored file
        _write(wt / ".claude" / "project.yaml", "commands:\n  check: just check\ncheckTimeoutSecs: true\n")
        command, source, failure = resolve_check_command(wt, main)
        project_yaml = wt / ".claude" / "project.yaml"
        seconds, timeout_source = resolve_check_timeout(wt, main, project_yaml, "project.yaml", "worktree")
        test_result(
            "(9) project.yaml checkTimeoutSecs: true is ignored; falls back to the 300 default",
            seconds == 300 and timeout_source.startswith("default"),
            f"seconds={seconds!r} timeout_source={timeout_source!r}",
        )
        test_result(
            "(9) the ignored boolean project.yaml value is named in timeout_source",
            "ignored invalid checkTimeoutSecs in worktree project.yaml" in timeout_source,
            f"timeout_source={timeout_source!r}",
        )

    with tempfile.TemporaryDirectory() as tmp, _env_unset("SHIPIT_CHECK_TIMEOUT_SECS"):
        root = Path(tmp)
        wt, main = root / "wt", root / "main"
        wt.mkdir()
        main.mkdir()

        # (9c) a string "900" is not a timeout: falls back to the 300 default and names the ignored file
        _write(wt / ".claude" / "project.yaml", "commands:\n  check: just check\ncheckTimeoutSecs: \"900\"\n")
        command, source, failure = resolve_check_command(wt, main)
        project_yaml = wt / ".claude" / "project.yaml"
        seconds, timeout_source = resolve_check_timeout(wt, main, project_yaml, "project.yaml", "worktree")
        test_result(
            "(9) project.yaml checkTimeoutSecs: \"900\" (a string) is ignored; falls back to the 300 default",
            seconds == 300 and timeout_source.startswith("default"),
            f"seconds={seconds!r} timeout_source={timeout_source!r}",
        )

    with tempfile.TemporaryDirectory() as tmp, _env_unset("SHIPIT_CHECK_TIMEOUT_SECS"):
        root = Path(tmp)
        wt, main = root / "wt", root / "main"
        wt.mkdir()
        main.mkdir()

        # (9d) an invalid checkOptOut (not a non-empty string) is ignored and the failure says so
        _write(wt / ".claude" / "project.yaml", "checkOptOut: true\n")
        command, source, failure = resolve_check_command(wt, main)
        test_result(
            "(9) invalid checkOptOut: no opt-out, source None, failure names 'checkOptOut must be a non-empty string'",
            command is None and source is None and failure is not None
            and "checkOptOut must be a non-empty string" in failure
            and "no check command configured" in failure,
            f"command={command!r} source={source!r} failure={failure!r}",
        )

    def _put(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    print("[Section 10] Package.json provenance label (#417 Step 2)")

    with tempfile.TemporaryDirectory() as tmpdir:
        wt = Path(tmpdir) / "wt"
        _put(wt / ".claude" / "repo-cache.json", json.dumps({"version": 1, "commands": {"check": "npm run check"}}))
        resolution = resolve_check_detail(wt, None)
        test_result(
            "a repo-cache check without provenance keeps the plain 'repo-cache' label",
            resolution.source == "repo-cache" and resolution.command == "npm run check",
            f"source={resolution.source!r}",
        )
        _put(
            wt / ".claude" / "repo-cache.json",
            json.dumps({"version": 1, "commands": {"check": "npm run check"}, "commandSources": {"check": "package-json"}}),
        )
        # The package-json provenance is re-derived from this worktree, so it must derive the same check.
        _put(wt / "package-lock.json", "{}")
        _put(wt / "package.json", json.dumps({"scripts": {"check": "npm run check"}}))
        resolution = resolve_check_detail(wt, None)
        test_result(
            "a repo-cache check with commandSources.check == package-json is labelled 'repo-cache (package-json)'",
            resolution.source == "repo-cache (package-json)" and resolution.command == "npm run check",
            f"source={resolution.source!r}",
        )

        # Item 9: a package-json cache check is not trusted from the shared cache when this worktree
        # derives no check; the cached value is ignored and the failure names why.
        (wt / "package.json").write_text(json.dumps({"scripts": {"test": "x"}}))
        resolution = resolve_check_detail(wt, None)
        test_result(
            "a package-json cache check is ignored when this worktree's package.json derives no check",
            resolution.command is None and resolution.source is None
            and "package-json check not re-derived" in (resolution.failure_reason or ""),
            f"command={resolution.command!r} source={resolution.source!r}",
        )
        (wt / "package.json").write_text(json.dumps({"scripts": {"check": "pnpm-check-here"}}))
        (wt / "package-lock.json").unlink()
        _put(wt / "pnpm-lock.yaml", "")
        resolution = resolve_check_detail(wt, None)
        test_result(
            "a package-json cache check is re-derived from this worktree, so a different branch's script is used",
            resolution.command == "pnpm run check" and resolution.source == "repo-cache (package-json)",
            f"command={resolution.command!r} source={resolution.source!r}",
        )

    print("[Section 11] detect_package_json_check")

    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        _put(base / "npm" / "package-lock.json", "{}")
        _put(base / "npm" / "package.json", json.dumps({"scripts": {"check": "tsc && jest"}}))
        test_result(
            "npm lockfile with scripts.check detects 'npm run check'",
            detect_package_json_check(base / "npm") == "npm run check",
        )
        _put(base / "pnpm" / "pnpm-lock.yaml", "")
        _put(base / "pnpm" / "package.json", json.dumps({"scripts": {"check": "x"}}))
        test_result(
            "pnpm lockfile with scripts.check detects 'pnpm run check'",
            detect_package_json_check(base / "pnpm") == "pnpm run check",
        )
        _put(base / "noscript" / "package-lock.json", "{}")
        _put(base / "noscript" / "package.json", json.dumps({"scripts": {"test": "x"}}))
        test_result(
            "no scripts.check yields None",
            detect_package_json_check(base / "noscript") is None,
        )
        _put(base / "ambig" / "package-lock.json", "{}")
        _put(base / "ambig" / "yarn.lock", "")
        _put(base / "ambig" / "package.json", json.dumps({"scripts": {"check": "x"}}))
        test_result(
            "two lockfiles yield None (nothing guessed)",
            detect_package_json_check(base / "ambig") is None,
        )
        _put(base / "nolock" / "package.json", json.dumps({"scripts": {"check": "x"}}))
        test_result(
            "no lockfile yields None",
            detect_package_json_check(base / "nolock") is None,
        )

    print("[Section 11b] commands/shipit.md lockfile table equals the detector mapping (#417 Step 3)")

    shipit_text = (Path(__file__).parent.parent / "commands" / "shipit.md").read_text(encoding="utf-8")
    table_rows = {}
    table_found = False
    in_table = False
    for line in shipit_text.splitlines():
        if "Lockfile → package manager" in line:
            in_table = True
            continue
        if not in_table:
            continue
        row = re.match(r"^\|\s*`([^`]+)`\s*\|\s*(\w+)\s*\|\s*$", line)
        if row:
            table_found = True
            table_rows[row.group(1)] = row.group(2)
        elif table_found:
            break
    test_result(
        "commands/shipit.md has a lockfile-to-manager table (rows like | `package-lock.json` | npm |)",
        table_found,
        "heading 'Lockfile → package manager' missing or no rows parsed from commands/shipit.md",
    )
    test_result(
        "the shipit.md lockfile table equals the detector's LOCKFILE_MANAGERS mapping (one rule, not two)",
        table_rows == LOCKFILE_MANAGERS,
        f"doc={table_rows} detector={LOCKFILE_MANAGERS}",
    )

    print("[Section 12] suggest_from_claude_md (display only)")

    with tempfile.TemporaryDirectory() as tmpdir:
        wt = Path(tmpdir)
        _put(wt / "CLAUDE.md", "Before pushing, run `just check` to gate the change.\n")
        test_result(
            "a backticked 'just check' in CLAUDE.md is suggested",
            suggest_from_claude_md(wt) == "just check",
        )
        _put(wt / "CLAUDE.md", "No gate command is documented here.\n")
        test_result(
            "CLAUDE.md with no check command suggests nothing",
            suggest_from_claude_md(wt) is None,
        )

    print("[Section 13] ensure_check")

    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        main = base / "main"
        feat = base / "feat"
        _put(feat / "package-lock.json", "{}")
        _put(feat / "package.json", json.dumps({"scripts": {"check": "run-all"}}))
        result = ensure_check(feat, main)
        cache = json.loads((main / ".claude" / "repo-cache.json").read_text())
        test_result(
            "a derivable check is written to the MAIN repo-cache with provenance (action detected)",
            result.action == "detected" and result.source == "repo-cache (package-json)"
            and result.command == "npm run check"
            and cache["commands"]["check"] == "npm run check"
            and cache["commandSources"]["check"] == "package-json",
            f"result={result} cache={cache}",
        )
        resolution = resolve_check_detail(feat, main)
        test_result(
            "after ensure_check the step resolves through the main repo-cache with the package-json label",
            resolution.command == "npm run check" and resolution.source == "repo-cache (package-json)",
            f"source={resolution.source!r} command={resolution.command!r}",
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        main = base / "main"
        feat = base / "feat"
        _put(feat / "package-lock.json", "{}")
        _put(feat / "package.json", json.dumps({"scripts": {"check": "run-all"}}))
        _put(main / ".claude" / "repo-cache.json", json.dumps({"version": 1, "commands": {"install": "npm ci"}, "checkTimeoutSecs": 60}))
        result = ensure_check(feat, main)
        cache = json.loads((main / ".claude" / "repo-cache.json").read_text())
        test_result(
            "existing main repo-cache keys (install, checkTimeoutSecs) are preserved",
            result.action == "detected" and cache["commands"]["install"] == "npm ci"
            and cache["checkTimeoutSecs"] == 60 and cache["commands"]["check"] == "npm run check",
            f"cache={cache}",
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        main = base / "main"
        feat = base / "feat"
        _put(feat / "package-lock.json", "{}")
        _put(feat / "package.json", json.dumps({"scripts": {"check": "run-all"}}))
        _put(main / ".claude" / "repo-cache.json", "{not json")
        result = ensure_check(feat, main)
        test_result(
            "a corrupt main repo-cache.json is refused and left unchanged (action error)",
            result.action == "error" and (main / ".claude" / "repo-cache.json").read_text() == "{not json",
            f"result={result}",
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        main = base / "main"
        feat = base / "feat"
        _put(feat / "CLAUDE.md", "Run `make check` before merging.\n")
        result = ensure_check(feat, main)
        test_result(
            "nothing derivable is unresolved with an unverified CLAUDE.md suggestion, and nothing is written",
            result.action == "unresolved" and result.source is None and result.suggestion == "make check"
            and not (main / ".claude" / "repo-cache.json").exists(),
            f"result={result}",
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        main = base / "main"
        feat = base / "feat"
        _put(main / ".claude" / "project.yaml", "checkOptOut: no CI in this repo\n")
        result = ensure_check(feat, main)
        test_result(
            "an explicit checkOptOut is reported as opt-out with its reason",
            result.action == "opt-out" and result.source == "opt-out"
            and result.opt_out_reason == "no CI in this repo",
            f"result={result}",
        )

    print("[Section 14] record_check_answer")

    with tempfile.TemporaryDirectory() as tmpdir:
        main = Path(tmpdir) / "main"
        main.mkdir()
        ok, message = record_check_answer(main, command="make verify")
        resolution = resolve_check_detail(main, None)
        test_result(
            "with no project.yaml the answer creates it with commands.check and verifies",
            ok and resolution.command == "make verify" and resolution.source == "project.yaml",
            f"ok={ok} message={message!r}",
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        main = Path(tmpdir) / "main"
        _put(main / ".claude" / "project.yaml", "commands:\n  install: npm ci\n")
        ok, message = record_check_answer(main, command="make verify")
        text = (main / ".claude" / "project.yaml").read_text()
        test_result(
            "a block commands: gets check inserted after the header with the first child's indent",
            ok and text == 'commands:\n  check: "make verify"\n  install: npm ci\n'
            and resolve_check_detail(main, None).command == "make verify",
            f"ok={ok} text={text!r} message={message!r}",
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        main = Path(tmpdir) / "main"
        original = "commands:\n  check: make old\n"
        _put(main / ".claude" / "project.yaml", original)
        ok, message = record_check_answer(main, command="make new")
        test_result(
            "an existing commands.check is refused and the file is unchanged",
            not ok and (main / ".claude" / "project.yaml").read_text() == original,
            f"ok={ok} message={message!r}",
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        main = Path(tmpdir) / "main"
        original = "commands: {install: npm ci}\n"
        _put(main / ".claude" / "project.yaml", original)
        ok, message = record_check_answer(main, command="make verify")
        test_result(
            "flow-style commands is refused and the file is unchanged",
            not ok and (main / ".claude" / "project.yaml").read_text() == original,
            f"ok={ok} message={message!r}",
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        main = Path(tmpdir) / "main"
        _put(main / ".claude" / "project.yaml", "commands:\n  install: x\n")
        ok, message = record_check_answer(main, opt_out="CI is not wired up yet")
        resolution = resolve_check_detail(main, None)
        test_result(
            "--opt-out appends checkOptOut and verifies as an opt-out",
            ok and resolution.source == "opt-out" and resolution.opt_out_reason == "CI is not wired up yet",
            f"ok={ok} message={message!r}",
        )
        ok_again, _ = record_check_answer(main, opt_out="again")
        test_result(
            "a second --opt-out is refused because checkOptOut already exists",
            not ok_again,
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        main = Path(tmpdir) / "main"
        _put(main / ".claude" / "repo-cache.json", json.dumps({"version": 1, "commands": {"check": "from-cache"}}))
        _put(main / ".claude" / "project.yaml", "# keep me\n")
        ok, message = record_check_answer(main, command="new-answer")
        test_result(
            "when re-resolution does not confirm the answer, project.yaml is restored and the YAML is named",
            not ok and "verification failed" in message and "new-answer" in message
            and (main / ".claude" / "project.yaml").read_text() == "# keep me\n",
            f"ok={ok} message={message!r}",
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        main = Path(tmpdir) / "main"
        _put(main / ".claude" / "repo-cache.json", json.dumps({"version": 1, "commands": {"check": "from-cache"}}))
        ok, message = record_check_answer(main, command="new-answer")
        test_result(
            "a rolled-back answer in a file that did not exist before is removed again",
            not ok and not (main / ".claude" / "project.yaml").exists(),
            f"ok={ok} message={message!r}",
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        main = Path(tmpdir) / "main"
        main.mkdir()
        ok_both, _ = record_check_answer(main, command="x", opt_out="y")
        ok_none, _ = record_check_answer(main)
        ok_multi, _ = record_check_answer(main, command="line one\nline two")
        test_result(
            "both or neither answer flag, or a multi-line answer, is refused",
            not ok_both and not ok_none and not ok_multi and not (main / ".claude" / "project.yaml").exists(),
        )

    print("[Section 15] record_check_answer: config lock, opt-out normalization, nested check, CRLF (#417 items 10, 27)")

    with tempfile.TemporaryDirectory() as tmpdir:
        main = Path(tmpdir) / "main"
        _put(main / ".claude" / "project.yaml", "commands:\n  install: npm ci\n")
        ok, message = record_check_answer(main, command="make verify")
        test_result(
            "the config lock file is kept out of .claude/ (only project.yaml is there after a recorded answer)",
            ok and sorted(p.name for p in (main / ".claude").iterdir()) == ["project.yaml"],
            f"ok={ok} message={message!r}",
        )

    saved_fcntl = checks_module.fcntl
    checks_module.fcntl = None
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            main = Path(tmpdir) / "main"
            main.mkdir()
            ok, message = record_check_answer(main, command="make verify")
            resolution = resolve_check_detail(main, None)
            test_result(
                "without fcntl the answer is still recorded and verified (the lock degrades to no lock)",
                ok and resolution.command == "make verify",
                f"ok={ok} message={message!r}",
            )
    finally:
        checks_module.fcntl = saved_fcntl

    with tempfile.TemporaryDirectory() as tmpdir:
        main = Path(tmpdir) / "main"
        _put(main / ".claude" / "project.yaml", "commands:\n  install: npm ci\n")
        ok, message = record_check_answer(main, opt_out="  CI   is\toff  ")
        text = (main / ".claude" / "project.yaml").read_text()
        test_result(
            "--opt-out echoes and writes the whitespace-normalized reason, not the raw value",
            ok and 'checkOptOut: "CI is off"' in message and 'checkOptOut: "CI is off"' in text and "  CI" not in text,
            f"ok={ok} message={message!r} text={text!r}",
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        main = Path(tmpdir) / "main"
        original = "commands:\n  install:\n    check: nested-only\n"
        _put(main / ".claude" / "project.yaml", original)
        ok, message = record_check_answer(main, command="make verify")
        text = (main / ".claude" / "project.yaml").read_text()
        test_result(
            "a deeper-nested key named check is not mistaken for commands.check; the answer is inserted",
            ok and text == 'commands:\n  check: "make verify"\n  install:\n    check: nested-only\n',
            f"ok={ok} text={text!r} message={message!r}",
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        main = Path(tmpdir) / "main"
        (main / ".claude").mkdir(parents=True)
        (main / ".claude" / "project.yaml").write_bytes(b"commands:\r\n  install: npm ci\r\n")
        ok, message = record_check_answer(main, command="make verify")
        data = (main / ".claude" / "project.yaml").read_bytes()
        test_result(
            "a CRLF project.yaml is edited and kept CRLF (no bare LF is introduced)",
            ok and data == b'commands:\r\n  check: "make verify"\r\n  install: npm ci\r\n',
            f"ok={ok} data={data!r} message={message!r}",
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        main = Path(tmpdir) / "main"
        _put(main / ".claude" / "repo-cache.json", json.dumps({"version": 1, "commands": {"check": "from-cache"}}))
        (main / ".claude" / "project.yaml").write_bytes(b"# keep me\r\n")
        ok, message = record_check_answer(main, command="new-answer")
        data = (main / ".claude" / "project.yaml").read_bytes()
        test_result(
            "a CRLF project.yaml that fails verification is restored to its original bytes",
            not ok and data == b"# keep me\r\n",
            f"ok={ok} data={data!r} message={message!r}",
        )

    print("[Section 16] worktree_tree in linked worktrees and failure logging (#462 Part A)")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        work = _clone_fixture(root)
        linked = root / "linked"
        _git(work, "worktree", "add", "-q", "-b", "linked-branch", str(linked))
        clean_tree = worktree_tree(linked)
        test_result(
            "worktree_tree: a clean linked worktree yields HEAD's tree SHA",
            clean_tree is not None and clean_tree == _git(linked, "rev-parse", "HEAD^{tree}"),
            f"tree={clean_tree!r}",
        )
        _write(linked / "untracked.txt", "new\n")
        dirty_tree = worktree_tree(linked)
        test_result(
            "worktree_tree: an untracked non-ignored file changes the tree",
            dirty_tree is not None and dirty_tree != clean_tree,
            f"clean={clean_tree!r} dirty={dirty_tree!r}",
        )
        (linked / "untracked.txt").unlink()
        restored_tree = worktree_tree(linked)
        test_result(
            "worktree_tree: removing the file restores the clean tree SHA",
            restored_tree == clean_tree,
            f"restored={restored_tree!r} clean={clean_tree!r}",
        )
        _write(linked / "ignored.log", "noise\n")
        test_result(
            "worktree_tree: a gitignored file does not change the tree",
            worktree_tree(linked) == clean_tree,
            f"tree={worktree_tree(linked)!r} clean={clean_tree!r}",
        )
        _write(linked / "stage-probe.txt", "probe\n")
        status_before = _git(linked, "status", "--porcelain")
        worktree_tree(linked)
        status_after = _git(linked, "status", "--porcelain")
        test_result(
            "worktree_tree: leaves the real index untouched (status unchanged, nothing staged)",
            status_before == status_after and "A  stage-probe.txt" not in status_after,
            f"before={status_before!r} after={status_after!r}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        not_a_repo = Path(tmp) / "plain"
        not_a_repo.mkdir()
        handler = _ListHandler()
        logging.getLogger().addHandler(handler)
        try:
            tree = worktree_tree(not_a_repo)
        finally:
            logging.getLogger().removeHandler(handler)
        test_result(
            "worktree_tree: a non-git directory returns None and logs the idempotency-disabled reason",
            tree is None and any("idempotency disabled" in m for m in handler.messages),
            f"tree={tree!r} messages={handler.messages!r}",
        )

    print("[Section 16b] resolve_check_base and canonical_changed_files (#462 Part A)")
    with tempfile.TemporaryDirectory() as tmp:
        work = _clone_fixture(Path(tmp))
        origin_main = _git(work, "rev-parse", "origin/main")
        base = resolve_check_base(work)
        test_result(
            "resolve_check_base: resolves to the merge-base with origin/<default> (symbolic-ref form)",
            base == origin_main,
            f"base={base!r} origin/main={origin_main!r}",
        )

        _write(work / "committed.txt", "c\n")
        _git(work, "add", "committed.txt")
        _git(work, "commit", "-q", "-m", "committed change")
        _write(work / "keep.txt", "keep changed\n")
        _git(work, "add", "keep.txt")
        _write(work / "keep.txt", "keep changed again\n")
        _write(work / "old.txt", "old changed\n")
        _write(work / "untracked.txt", "u\n")
        _write(work / "ignored.log", "noise\n")
        _git(work, "mv", "gone.txt", "renamed.txt")

        base = resolve_check_base(work)
        files = canonical_changed_files(work, base)
        expected = sorted(["committed.txt", "keep.txt", "old.txt", "untracked.txt", "gone.txt", "renamed.txt"])
        test_result(
            "canonical_changed_files: unions committed, staged, working, untracked; excludes ignored; both rename sides",
            files == expected,
            f"files={files!r} expected={expected!r}",
        )
        test_result(
            "canonical_changed_files: sorted and unique (keep.txt is staged and modified again)",
            files == sorted(set(files)),
            f"files={files!r}",
        )
        test_result(
            "canonical_changed_files: deterministic across calls on the same state",
            canonical_changed_files(work, base) == files,
            "second call differed",
        )

        no_base = canonical_changed_files(work, None)
        test_result(
            "canonical_changed_files with base=None returns None (unknown), never an empty set that could be hashed",
            no_base is None,
            f"no_base={no_base!r}",
        )

    print("[Section 16c] cache and record paths are git-ignored in this repo (#462 Part A)")
    repo_root = Path(__file__).resolve().parent.parent
    for ignored_path in [
        ".claude/check-records/x.json",
        ".mypy_cache/x",
        ".ruff_cache/x",
        "__pycache__/x.pyc",
    ]:
        # .ruff_cache is named in the root .gitignore, so it is ignored whether or not ruff has run
        # yet (ruff's own in-directory .gitignore only exists after the first run).
        proc = subprocess.run(
            ["git", "check-ignore", "-q", ignored_path], cwd=str(repo_root),
            capture_output=True, text=True,
        )
        test_result(
            f"{ignored_path} is git-ignored (so validate records and caches never dirty the tree)",
            proc.returncode == 0,
            f"rc={proc.returncode} stderr={proc.stderr.strip()!r}",
        )

    print()
    h.summarize_and_exit()
