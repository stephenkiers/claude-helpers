#!/usr/bin/env python3
"""
Test suite for run_checks() orchestration and coverage assertion.

Tests the canonical order, fail-closed behavior, coverage assertion,
and the guarantee that no gate passes without executing something.

Run with: python3 tests/test_workflow_checks_execute.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.checks import (
    run_checks, execute_check, CheckResult, CheckResults, validate, build_check_order,
    NON_CHECK_COMMANDS,
)
from workflow.safety import Unknown
from _test_harness import Harness


# Keys validate() adds on top of CheckResults.to_dict() (#462 Part A key-set pin).
EXTRA_KEYS = frozenset({
    "schema_version", "mode_requested", "mode_executed", "fell_back_to_full",
    "cache", "satisfied_by", "changed_files_source",
})
TO_DICT_KEYS = frozenset({"results", "all_passed", "failed_at", "planned", "executed", "skipped", "status"})

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), env={**os.environ, **_GIT_ENV},
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _fixture_clone(root, cache_commands):
    """Bare origin with one commit on main, cloned as a work tree one commit ahead on `feature`.

    The repo-cache.json is written untracked and gitignored (as in a real repo), so it does not
    change the tree. Returns the work tree path.
    """
    seed = root / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / ".claude").mkdir()
    (seed / ".claude" / ".gitignore").write_text("check-records/\nrepo-cache.json\n")
    (seed / "README.md").write_text("base\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "init")
    origin = root / "origin.git"
    _git(root, "clone", "-q", "--bare", str(seed), str(origin))
    work = root / "work"
    _git(root, "clone", "-q", str(origin), str(work))
    _git(work, "checkout", "-q", "-b", "feature")
    (work / "feature.txt").write_text("one\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "feature")
    (work / ".claude" / "repo-cache.json").write_text(
        json.dumps({"version": 1, "commands": cache_commands}) + "\n"
    )
    return work


def _counter_cmd(path, ok=True):
    """Shell command that records one execution in `path` and passes or fails."""
    return f"echo run >> '{path}' && {'true' if ok else 'false'}"


def _runs(path):
    return path.read_text().count("run") if path.exists() else 0


if __name__ == "__main__":
    h = Harness("WORKFLOW CHECKS EXECUTE TEST SUITE")
    test_result = h.test_result

    print("[Section 1] CheckResults dataclass")

    result = CheckResults(
        results=[],
        all_passed=True,
        failed_at=None,
        planned=[],
        executed=[],
        skipped=[],
        status="passed"
    )
    test_result(
        "CheckResults can be instantiated",
        isinstance(result, CheckResults)
    )
    test_result(
        "CheckResults.all_passed defaults correctly",
        result.all_passed is True
    )
    test_result(
        "CheckResults.status exists",
        hasattr(result, "status") and result.status == "passed"
    )

    print()
    print("[Section 2] run_checks executes in correct order")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        execution_order = []

        def mock_execute_check(command, cwd=None, timeout=300, env=None):
            """Mock execute_check that records order."""
            execution_order.append(command)
            return CheckResult(
                success=True,
                returncode=0,
                stdout="",
                stderr="",
                error=None
            )

        with mock.patch("workflow.checks.execute_check", side_effect=mock_execute_check):
            commands = {
                "format": "prettier --write .",
                "lint": "eslint .",
                "typecheck": "tsc --noEmit",
                "test": "jest",
                "build": "npm run build"
            }

            result, err = run_checks(commands, repo_root=repo_root)

            test_result(
                "run_checks: returns tuple",
                isinstance(result, CheckResults) and (err is None or isinstance(err, Unknown))
            )
            test_result(
                "run_checks: format executes first",
                len(execution_order) > 0 and execution_order[0] == "prettier --write ."
            )
            test_result(
                "run_checks: build executes last",
                len(execution_order) > 0 and execution_order[-1] == "npm run build"
            )
            test_result(
                "run_checks: all non-suppressed commands executed",
                len(result.executed) == 5
            )

    print()
    print("[Section 3] run_checks stops at first failure")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        execution_calls = []

        def mock_execute_fail(command, cwd=None, timeout=300, env=None):
            """Mock that fails at 'lint' and records all calls."""
            execution_calls.append(command)
            success = "lint" not in command
            return CheckResult(
                success=success,
                returncode=0 if success else 1,
                stdout="",
                stderr="failed" if not success else "",
                error=None
            )

        with mock.patch("workflow.checks.execute_check", side_effect=mock_execute_fail):
            commands = {
                "format": "prettier --write .",
                "lint": "eslint .",
                "typecheck": "tsc --noEmit",
                "test": "jest",
                "build": "npm run build"
            }

            result, err = run_checks(commands, repo_root=repo_root)

            test_result(
                "run_checks: stops at first failure",
                not result.all_passed and "build" not in execution_calls
            )
            test_result(
                "run_checks: failed_at is set",
                result.failed_at == "lint"
            )
            test_result(
                "run_checks: status is 'failed'",
                result.status == "failed"
            )
            test_result(
                "run_checks: a legitimate stop-at-first-failure is not a coverage violation (err is None)",
                err is None
            )
            test_result(
                "run_checks: not-yet-reached commands are recorded as skipped(not_reached)",
                {s.command_type for s in result.skipped if s.reason == "not_reached"} == {"typecheck", "test", "build"}
            )

    print()
    print("[Section 3b] run_checks: a cache listing every command type (mostly null) is not a coverage violation")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)

        with mock.patch("workflow.checks.execute_check") as mock_exec:
            mock_exec.return_value = CheckResult(
                success=True,
                returncode=0,
                stdout="",
                stderr="",
                error=None
            )

            # Matches the documented Cache Schema in commands/shipit.md: every command
            # type present, with unused ones explicitly null rather than omitted.
            commands = {
                "install": None,
                "format": None,
                "lint": None,
                "vet": None,
                "typecheck": None,
                "test": "python3 tests/run_all.py",
                "build": None,
                "check": None,
            }

            result, err = run_checks(commands, repo_root=repo_root)

            test_result(
                "run_checks: fully-enumerated null commands do not trigger a coverage violation (err is None)",
                err is None
            )
            test_result(
                "run_checks: fully-enumerated null commands still pass with the single non-null command executed",
                result.all_passed is True and result.executed == ["test"]
            )

    print()
    print("[Section 4] run_checks handles None/absent commands")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        executed_commands = []

        def mock_execute_track(command, cwd=None, timeout=300, env=None):
            """Mock that tracks executed commands."""
            executed_commands.append(command)
            return CheckResult(
                success=True,
                returncode=0,
                stdout="",
                stderr="",
                error=None
            )

        with mock.patch("workflow.checks.execute_check", side_effect=mock_execute_track):
            commands = {
                "format": None,
                "check": None,
                "lint": "eslint .",
                "test": "jest"
            }

            result, err = run_checks(commands, repo_root=repo_root)

            test_result(
                "run_checks: accepts dict with None values",
                result is not None and result.all_passed is not None
            )
            test_result(
                "run_checks: executes only non-None commands",
                len(executed_commands) == 2 and len(result.executed) == 2
            )
            test_result(
                "run_checks: includes lint (not suppressed when no check)",
                "lint" in result.executed
            )

    print()
    print("[Section 5] run_checks with check present suppresses lint/typecheck")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        executed_types = []

        def mock_execute_selective(command, cwd=None, timeout=300, env=None):
            """Track which command types are called."""
            # Parse the command to guess type (simplistic but works for test)
            if "lint" in command:
                executed_types.append("lint")
            elif "typecheck" in command or "tsc" in command:
                executed_types.append("typecheck")
            elif "check" in command or "custom-check" in command:
                executed_types.append("check")
            elif "jest" in command:
                executed_types.append("test")
            elif "build" in command:
                executed_types.append("build")

            return CheckResult(
                success=True,
                returncode=0,
                stdout="",
                stderr="",
                error=None
            )

        with mock.patch("workflow.checks.execute_check", side_effect=mock_execute_selective):
            commands = {
                "format": "prettier --write .",
                "check": "custom-check",
                "lint": "eslint .",
                "typecheck": "tsc --noEmit",
                "test": "jest",
                "build": "npm run build"
            }

            result, err = run_checks(commands, repo_root=repo_root)

            test_result(
                "run_checks: check replaces lint",
                "check" in result.executed and "lint" not in result.executed
            )
            test_result(
                "run_checks: check replaces typecheck",
                "check" in result.executed and "typecheck" not in result.executed
            )
            test_result(
                "run_checks: lint/typecheck are skipped (visible)",
                any(s.command_type == "lint" and s.reason == "superseded_by_check" for s in result.skipped) and
                any(s.command_type == "typecheck" and s.reason == "superseded_by_check" for s in result.skipped)
            )

    print()
    print("[Section 6] run_checks: nothing ran is never a pass")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)

        with mock.patch("workflow.checks.execute_check") as mock_exec:
            # No commands at all
            result, err = run_checks({}, repo_root=repo_root)

            test_result(
                "run_checks: empty cache returns Unknown",
                isinstance(err, Unknown)
            )
            test_result(
                "run_checks: empty cache status is 'no_checks_ran'",
                result.status == "no_checks_ran"
            )
            test_result(
                "run_checks: empty cache all_passed is False",
                result.all_passed is False
            )
            test_result(
                "run_checks: empty cache never executed",
                not mock_exec.called
            )

    print()
    print("[Section 7] run_checks: all-null commands is never a pass")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)

        with mock.patch("workflow.checks.execute_check") as mock_exec:
            commands = {
                "format": None,
                "check": None,
                "lint": None,
                "test": None,
                "build": None
            }
            result, err = run_checks(commands, repo_root=repo_root)

            test_result(
                "run_checks: all-null returns Unknown",
                isinstance(err, Unknown)
            )
            test_result(
                "run_checks: all-null status is 'no_checks_ran'",
                result.status == "no_checks_ran"
            )
            test_result(
                "run_checks: all-null all_passed is False",
                result.all_passed is False
            )
            test_result(
                "run_checks: all-null never executed",
                not mock_exec.called
            )

    print()
    print("[Section 8] run_checks: single test command passes")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)

        with mock.patch("workflow.checks.execute_check") as mock_exec:
            mock_exec.return_value = CheckResult(
                success=True,
                returncode=0,
                stdout="",
                stderr="",
                error=None
            )

            commands = {
                "test": "pytest"
            }

            result, err = run_checks(commands, repo_root=repo_root)

            test_result(
                "run_checks: single command passes without error",
                err is None
            )
            test_result(
                "run_checks: single command all_passed is True",
                result.all_passed is True
            )
            test_result(
                "run_checks: single command status is 'passed'",
                result.status == "passed"
            )
            test_result(
                "run_checks: single command executed",
                result.executed == ["test"]
            )

    print()
    print("[Section 9] run_checks: repo-cache with only commands.test runs it")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        executed = []

        def mock_track(command, cwd=None, timeout=300, env=None):
            executed.append(command)
            return CheckResult(
                success=True,
                returncode=0,
                stdout="",
                stderr="",
                error=None
            )

        with mock.patch("workflow.checks.execute_check", side_effect=mock_track):
            commands = {
                "test": "pytest"
            }

            result, err = run_checks(commands, repo_root=repo_root)

            test_result(
                "run_checks: test command executed (live repro)",
                len(executed) == 1 and executed[0] == "pytest"
            )
            test_result(
                "run_checks: test result passes",
                result.all_passed is True
            )

    print()
    print("[Section 10] run_checks with timeout")

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)

        with mock.patch("workflow.checks.execute_check") as mock_exec:
            mock_exec.return_value = CheckResult(
                success=True,
                returncode=0,
                stdout="",
                stderr="",
                error=None
            )

            commands = {
                "test": "slow-test"
            }

            result, err = run_checks(commands, repo_root=repo_root, timeout=60)

            test_result(
                "run_checks: passes timeout to execute_check",
                mock_exec.called and mock_exec.call_args[1]["timeout"] == 60
            )

    print()
    print("[Section 11] execute_check runs without a timeout binary on PATH (#338)")

    result = execute_check("exit 0", cwd=None, env={"PATH": "/nonexistent"})
    test_result(
        "execute_check: a shell command succeeds with PATH=/nonexistent (no timeout binary needed)",
        result.success and result.returncode == 0,
        f"success={result.success} rc={result.returncode} error={result.error!r} stderr={result.stderr!r}",
    )

    result = execute_check("/bin/sleep 5", cwd=None, timeout=1, env={"PATH": "/nonexistent"})
    test_result(
        "execute_check: a slow command times out (returns, does not hang) with PATH=/nonexistent",
        not result.success and result.error is not None and "timed out" in str(result.error).lower(),
        f"success={result.success} error={result.error!r}",
    )

    print("[Section 12] validate: cache miss/hit/--force, failing runs, malformed records (#462 Part A)")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        count = root / "count.txt"
        work = _fixture_clone(root, {"check": _counter_cmd(count)})
        first = validate(work, mode="full")
        second = validate(work, mode="full")
        test_result(
            "validate: first run is a miss, executes once, and passes",
            first.get("cache") == "miss" and first.get("status") == "passed" and _runs(count) == 1,
            f"first={first!r} runs={_runs(count)}",
        )
        test_result(
            "validate: second run on the same tree is a hit with no re-execution",
            second.get("cache") == "hit" and second.get("status") == "passed"
            and _runs(count) == 1 and second.get("satisfied_by") == first.get("satisfied_by"),
            f"second={second!r} runs={_runs(count)}",
        )
        forced = validate(work, mode="full", force=True)
        test_result(
            "validate: --force bypasses a cache hit and re-executes",
            forced.get("cache") == "forced" and forced.get("status") == "passed" and _runs(count) == 2,
            f"forced={forced!r} runs={_runs(count)}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        count = root / "count.txt"
        main_dir = root / "main-wt"
        (main_dir / ".claude").mkdir(parents=True)
        yaml_path = main_dir / ".claude" / "project.yaml"
        work = _fixture_clone(root, {"lint": "true"})
        yaml_path.write_text(f"commands:\n  check: \"echo A >> '{count}'\"\n")
        y1 = validate(work, mode="full", main_worktree=main_dir)
        yaml_path.write_text(f"commands:\n  check: \"echo B >> '{count}'\"\n")
        y2 = validate(work, mode="full", main_worktree=main_dir)
        y3 = validate(work, mode="full", main_worktree=main_dir)
        test_result(
            "validate: a check command that changes in project.yaml (tree and repo-cache unchanged) is a miss, then the new command hits",
            y1.get("status") == "passed" and y2.get("cache") == "miss" and y3.get("cache") == "hit"
            and count.read_text().count("A") == 1 and count.read_text().count("B") == 1,
            f"y1={y1!r} y2={y2!r} y3={y3!r} count={count.read_text() if count.exists() else ''!r}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        count = root / "count.txt"
        work = _fixture_clone(root, {"check": _counter_cmd(count, ok=False)})
        r1 = validate(work, mode="full")
        r2 = validate(work, mode="full")
        records_dir = work / ".claude" / "check-records"
        pass_records = list(records_dir.glob("*.json")) if records_dir.exists() else []
        test_result(
            "validate: a failing run writes no pass record, so the next run is a miss that re-executes",
            r1.get("status") != "passed" and r2.get("cache") == "miss" and _runs(count) == 2 and not pass_records,
            f"r1={r1!r} r2={r2!r} runs={_runs(count)} records={pass_records}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        count = root / "count.txt"
        work = _fixture_clone(root, {"check": _counter_cmd(count)})
        validate(work, mode="full")
        record = next((work / ".claude" / "check-records").glob("*.json"))
        record.write_text("{not json")
        after = validate(work, mode="full")
        test_result(
            "validate: a malformed record is a miss and the check re-runs",
            after.get("cache") == "miss" and after.get("status") == "passed" and _runs(count) == 2,
            f"after={after!r} runs={_runs(count)}",
        )

    print("[Section 12b] validate: minimum keying, full-satisfies-minimum, fallback (#462 Part A)")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        count = root / "count.txt"
        work = _fixture_clone(root, {"check": _counter_cmd(count), "check_minimum": _counter_cmd(count)})
        m1 = validate(work, mode="minimum")
        m2 = validate(work, mode="minimum")
        test_result(
            "validate minimum: a repeat minimum run on the same key is a hit",
            m1.get("cache") == "miss" and m2.get("cache") == "hit" and _runs(count) == 1,
            f"m1={m1!r} m2={m2!r} runs={_runs(count)}",
        )
        cache_file = work / ".claude" / "repo-cache.json"
        cache_file.write_text(json.dumps({"version": 1, "commands": {
            "check": _counter_cmd(count),
            "check_minimum": _counter_cmd(count) + " # changed",
        }}) + "\n")
        m3 = validate(work, mode="minimum")
        test_result(
            "validate minimum: changing check_minimum changes the key (miss, re-executes)",
            m3.get("cache") == "miss" and _runs(count) == 2,
            f"m3={m3!r} runs={_runs(count)}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        count = root / "count.txt"
        work = _fixture_clone(root, {"check": _counter_cmd(count), "check_minimum": _counter_cmd(count)})
        validate(work, mode="full")
        minimum = validate(work, mode="minimum")
        test_result(
            "validate: a passing full run satisfies a minimum request (hit, no re-execution)",
            minimum.get("cache") == "hit" and minimum.get("status") == "passed" and _runs(count) == 1,
            f"minimum={minimum!r} runs={_runs(count)}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        count = root / "count.txt"
        work = _fixture_clone(root, {"check": _counter_cmd(count), "check_minimum": _counter_cmd(count)})
        validate(work, mode="minimum")
        full = validate(work, mode="full")
        test_result(
            "validate: a passing minimum run does NOT satisfy a full request (miss, re-executes)",
            full.get("cache") == "miss" and _runs(count) == 2,
            f"full={full!r} runs={_runs(count)}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        count = root / "count.txt"
        work = _fixture_clone(root, {"check": _counter_cmd(count)})
        out = validate(work, mode="minimum")
        test_result(
            "validate minimum with no check_minimum declared falls back to full",
            out.get("status") == "passed" and out.get("fell_back_to_full") is True
            and out.get("mode_executed") == "full" and _runs(count) == 1,
            f"out={out!r} runs={_runs(count)}",
        )

    test_result(
        "NON_CHECK_COMMANDS excludes install and check_minimum from executed extras",
        "install" in NON_CHECK_COMMANDS and "check_minimum" in NON_CHECK_COMMANDS,
        f"NON_CHECK_COMMANDS={sorted(NON_CHECK_COMMANDS)}",
    )
    planned, _skipped = build_check_order({"install": "npm ci", "check": "make", "check_minimum": "make min"})
    test_result(
        "build_check_order: check_minimum and install are never planned as extra checks",
        "check_minimum" not in planned and "install" not in planned and planned == ["check"],
        f"planned={planned}",
    )

    print("[Section 12c] validate: environment export and key-set pin (#462 Part A)")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        seen = root / "seen.txt"
        work = _fixture_clone(root, {"check": f"printf '%s' \"$CHECK_BASE\" > '{seen}'"})
        out = validate(work, mode="full")
        base_sha = _git(work, "rev-parse", "origin/main")
        test_result(
            "validate: the check command receives CHECK_BASE (the merge-base) in its environment",
            out.get("status") == "passed" and seen.exists() and seen.read_text() == base_sha,
            f"out={out!r} seen={seen.read_text() if seen.exists() else None!r} base={base_sha}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        count = root / "count.txt"
        work = _fixture_clone(root, {"check": _counter_cmd(count)})
        miss_out = validate(work, mode="full")
        hit_out = validate(work, mode="full")
        test_result(
            "validate: miss-path output keys equal CheckResults.to_dict keys plus the validate extras",
            set(miss_out.keys()) == TO_DICT_KEYS | EXTRA_KEYS,
            f"extra={sorted(set(miss_out.keys()) ^ (TO_DICT_KEYS | EXTRA_KEYS))}",
        )
        test_result(
            "validate: hit-path output has the same key set as the miss path",
            hit_out.get("cache") == "hit" and set(hit_out.keys()) == TO_DICT_KEYS | EXTRA_KEYS,
            f"extra={sorted(set(hit_out.keys()) ^ (TO_DICT_KEYS | EXTRA_KEYS))}",
        )

    print("[Section 12d] validate: base movement, untracked files, eviction, tree mutation, fallback caching (#462 Part A)")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        count = root / "count.txt"
        work = _fixture_clone(root, {"check": _counter_cmd(count)})
        first = validate(work, mode="full")
        other = root / "other"
        _git(root, "clone", "-q", str(root / "origin.git"), str(other))
        (other / "later.txt").write_text("later\n")
        _git(other, "add", "-A")
        _git(other, "commit", "-q", "-m", "later")
        _git(other, "push", "-q", "origin", "main")
        _git(work, "fetch", "-q", "origin")
        second = validate(work, mode="full")
        test_result(
            "validate: an advanced origin/main with the same merge-base is still a hit (no re-execution)",
            second.get("cache") == "hit" and first.get("satisfied_by") == second.get("satisfied_by")
            and _runs(count) == 1,
            f"second={second!r} runs={_runs(count)}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        count = root / "count.txt"
        work = _fixture_clone(root, {"check": _counter_cmd(count), "check_minimum": _counter_cmd(count)})
        m1 = validate(work, mode="minimum")
        # M1 adds the same feature.txt content on main, so HEAD's tree is unchanged but its merge-base moves.
        other = root / "other"
        _git(root, "clone", "-q", str(root / "origin.git"), str(other))
        (other / "feature.txt").write_text("one\n")
        _git(other, "add", "-A")
        _git(other, "commit", "-q", "-m", "same content on main")
        _git(other, "push", "-q", "origin", "main")
        m1_sha = _git(other, "rev-parse", "HEAD")
        _git(work, "fetch", "-q", "origin")
        tree = _git(work, "rev-parse", "HEAD^{tree}")
        new_head = _git(work, "commit-tree", tree, "-p", m1_sha, "-m", "rebased onto same content")
        _git(work, "update-ref", "refs/heads/feature", new_head)
        m2 = validate(work, mode="minimum")
        test_result(
            "validate minimum: same tree with a different merge-base is a miss (changed-files hash differs)",
            m1.get("cache") == "miss" and m2.get("cache") == "miss" and _runs(count) == 2,
            f"m1={m1!r} m2={m2!r} runs={_runs(count)}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        count = root / "count.txt"
        work = _fixture_clone(root, {"check": _counter_cmd(count)})
        validate(work, mode="full")
        (work / "untracked-notes.txt").write_text("not committed\n")
        after = validate(work, mode="full")
        test_result(
            "validate: an untracked (non-ignored) file changes the tree and is a miss",
            after.get("cache") == "miss" and _runs(count) == 2,
            f"after={after!r} runs={_runs(count)}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        count = root / "count.txt"
        flag = root / "fail-flag.txt"
        work = _fixture_clone(root, {"check": f"echo run >> '{count}' && test ! -f '{flag}'"})
        passed = validate(work, mode="full")
        records_dir = work / ".claude" / "check-records"
        recorded = list(records_dir.glob("*.json")) if records_dir.exists() else []
        flag.write_text("fail now\n")
        forced_fail = validate(work, mode="full", force=True)
        remaining = list(records_dir.glob("*.json")) if records_dir.exists() else []
        test_result(
            "validate: a forced failing run evicts the older pass record for the same key",
            passed.get("status") == "passed" and len(recorded) == 1
            and forced_fail.get("status") != "passed" and remaining == [],
            f"passed={passed!r} recorded={recorded} forced_fail={forced_fail!r} remaining={remaining}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        count = root / "count.txt"
        mutated = root / "work" / "mutated.txt"  # _fixture_clone places the work tree at root/work
        work = _fixture_clone(root, {"check": f"echo run >> '{count}' && echo x > '{mutated}'"})
        out = validate(work, mode="full")
        records_dir = work / ".claude" / "check-records"
        recorded = list(records_dir.glob("*.json")) if records_dir.exists() else []
        test_result(
            "validate: a tree that changes during the run writes no record (next run is a miss)",
            out.get("status") == "passed" and recorded == [],
            f"out={out!r} records={recorded}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        count = root / "count.txt"
        work = _fixture_clone(root, {"check": _counter_cmd(count)})
        first = validate(work, mode="minimum")
        second = validate(work, mode="minimum")
        test_result(
            "validate minimum fallback is keyed as full and caches: second run is a hit with no re-execution",
            first.get("fell_back_to_full") is True and first.get("cache") == "miss"
            and second.get("cache") == "hit" and _runs(count) == 1,
            f"first={first!r} second={second!r} runs={_runs(count)}",
        )

    print("[Section 12e] validate: scratch changed-file list, unresolvable base, unknown-schema records (#462 Part A)")
    from workflow.checks import resolve_check_base

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        count = root / "count.txt"
        copied = root / "copied-list.txt"
        listing = root / "listing.txt"
        cmd = f"cp \"$CHECK_CHANGED_FILES\" '{copied}' && printf '%s' \"$CHECK_CHANGED_FILES\" > '{listing}' && echo run >> '{count}'"
        work = _fixture_clone(root, {"check": "true", "check_minimum": cmd})
        (work / "untracked.txt").write_text("u\n")
        out = validate(work, mode="minimum")
        listed = copied.read_text() if copied.exists() else ""
        scratch = Path(listing.read_text()) if listing.exists() else None
        scratch_outside = scratch is not None and not str(scratch).startswith(str(work) + os.sep)
        scratch_removed = scratch is not None and not scratch.exists()
        test_result(
            "validate minimum: CHECK_CHANGED_FILES lists committed and untracked files, lives outside the worktree, and is removed after the run",
            out.get("status") == "passed" and "feature.txt" in listed and "untracked.txt" in listed
            and scratch_outside and scratch_removed,
            f"out={out!r} listed={listed!r} scratch={scratch!r}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        count = root / "count.txt"
        work = _fixture_clone(root, {"check": "true", "check_minimum": _counter_cmd(count)})
        _git(work, "checkout", "-q", "--detach")
        base = resolve_check_base(work)
        m1 = validate(work, mode="minimum")
        m2 = validate(work, mode="minimum")
        records_dir = work / ".claude" / "check-records"
        records = list(records_dir.glob("*.json")) if records_dir.exists() else []
        test_result(
            "resolve_check_base is None on a detached HEAD",
            base is None,
            f"base={base!r}",
        )
        test_result(
            "validate minimum with an unresolvable base runs uncached: no hit, re-executes, writes no record",
            m1.get("cache") != "hit" and m2.get("cache") != "hit" and _runs(count) == 2 and not records,
            f"m1={m1!r} m2={m2!r} runs={_runs(count)} records={records}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        count = root / "count.txt"
        work = _fixture_clone(root, {"check": _counter_cmd(count)})
        validate(work, mode="full")
        for rec in (work / ".claude" / "check-records").glob("*.json"):
            data = json.loads(rec.read_text())
            data["schema_version"] = 2
            rec.write_text(json.dumps(data) + "\n")
        after = validate(work, mode="full")
        test_result(
            "validate: a record with an unknown schema_version fails closed (a miss that re-executes)",
            after.get("cache") == "miss" and after.get("status") == "passed" and _runs(count) == 2,
            f"after={after!r} runs={_runs(count)}",
        )

    print()
    h.summarize_and_exit()
