#!/usr/bin/env python3
"""
Test suite for check ordering and execution.

Run with: python3 tests/test_workflow_checks.py
"""

import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.checks import (
    execute_check, build_check_order, resolve_check_command, resolve_check_timeout
)
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
        seconds, timeout_source = resolve_check_timeout(wt, main, None)
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
        seconds, timeout_source = resolve_check_timeout(wt, main, main / ".claude" / "repo-cache.json")
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
            seconds, timeout_source = resolve_check_timeout(wt, main, None)
        test_result(
            "(8) nothing sets checkTimeoutSecs: 300 default",
            seconds == 300,
            f"seconds={seconds!r} source={timeout_source!r}",
        )

        # (8b) SHIPIT_CHECK_TIMEOUT_SECS overrides every file, including a worktree cache value
        _write(wt / ".claude" / "repo-cache.json", json.dumps({"checkTimeoutSecs": 900}))
        with _env_unset("SHIPIT_CHECK_TIMEOUT_SECS", "42"):
            seconds, timeout_source = resolve_check_timeout(wt, main, None)
        test_result(
            "(8) SHIPIT_CHECK_TIMEOUT_SECS=42 overrides the cache's 900",
            seconds == 42,
            f"seconds={seconds!r} source={timeout_source!r}",
        )

    print()
    h.summarize_and_exit()
