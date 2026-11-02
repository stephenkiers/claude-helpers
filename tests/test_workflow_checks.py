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
    execute_check, build_check_order, resolve_check_command, resolve_check_timeout,
    resolve_check_detail, detect_package_json_check, suggest_from_claude_md,
    ensure_check, record_check_answer,
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
    print("[Section 9] project.yaml checkTimeoutSecs and invalid checkOptOut (#338 directives 1-3)")

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
        resolution = resolve_check_detail(wt, None)
        test_result(
            "a repo-cache check with commandSources.check == package-json is labelled 'repo-cache (package-json)'",
            resolution.source == "repo-cache (package-json)" and resolution.command == "npm run check",
            f"source={resolution.source!r}",
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

    print()
    h.summarize_and_exit()
