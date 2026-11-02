#!/usr/bin/env python3
"""
Test suite for checks CLI subprocess invocation and exit codes.

Tests that the CLI properly exits with:
- 0: checks passed
- 1: a check failed
- 2: no checks ran / coverage violation

Run with: python3 tests/test_workflow_checks_cli.py
"""

import sys
import json
import subprocess
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("WORKFLOW CHECKS CLI TEST SUITE")
    test_result = h.test_result

    print("[Section 1] CLI exit code 2 when no checks ran (empty cache)")

    with tempfile.TemporaryDirectory() as tmpdir:
        cache_json = "{}"
        proc = subprocess.run(
            [sys.executable, "-m", "scripts.workflow.cli", "checks", "run", "-"],
            input=cache_json,
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent
        )

        test_result(
            "checks run with empty cache exits 2",
            proc.returncode == 2
        )
        try:
            output = json.loads(proc.stdout)
            test_result(
                "checks run output is valid JSON",
                isinstance(output, dict)
            )
            test_result(
                "checks run status is 'no_checks_ran'",
                output.get("status") == "no_checks_ran"
            )
            test_result(
                "checks run all_passed is False",
                output.get("all_passed") is False
            )
        except json.JSONDecodeError:
            test_result(
                "checks run output is valid JSON",
                False
            )

    print()
    print("[Section 2] CLI exit code 0 when checks pass")

    with tempfile.TemporaryDirectory() as tmpdir:
        cache_json = json.dumps({
            "commands": {
                "test": "true"
            }
        })
        proc = subprocess.run(
            [sys.executable, "-m", "scripts.workflow.cli", "checks", "run", "-"],
            input=cache_json,
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent
        )

        test_result(
            "checks run with passing test exits 0",
            proc.returncode == 0
        )
        try:
            output = json.loads(proc.stdout)
            test_result(
                "checks run status is 'passed'",
                output.get("status") == "passed"
            )
            test_result(
                "checks run all_passed is True",
                output.get("all_passed") is True
            )
        except json.JSONDecodeError:
            test_result(
                "checks run output is valid JSON",
                False
            )

    print()
    print("[Section 3] CLI exit code 1 when check fails")

    with tempfile.TemporaryDirectory() as tmpdir:
        cache_json = json.dumps({
            "commands": {
                "test": "false"
            }
        })
        proc = subprocess.run(
            [sys.executable, "-m", "scripts.workflow.cli", "checks", "run", "-"],
            input=cache_json,
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent
        )

        test_result(
            "checks run with failing test exits 1",
            proc.returncode == 1
        )
        try:
            output = json.loads(proc.stdout)
            test_result(
                "checks run status is 'failed'",
                output.get("status") == "failed"
            )
            test_result(
                "checks run all_passed is False",
                output.get("all_passed") is False
            )
        except json.JSONDecodeError:
            test_result(
                "checks run output is valid JSON",
                False
            )

    print()
    print("[Section 4] CLI --allow-no-checks flag converts exit 2 to 0")

    with tempfile.TemporaryDirectory() as tmpdir:
        cache_json = "{}"
        proc = subprocess.run(
            [sys.executable, "-m", "scripts.workflow.cli", "checks", "run", "--allow-no-checks", "-"],
            input=cache_json,
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent
        )

        test_result(
            "checks run --allow-no-checks with empty cache exits 0",
            proc.returncode == 0
        )
        try:
            output = json.loads(proc.stdout)
            test_result(
                "checks run --allow-no-checks status still 'no_checks_ran'",
                output.get("status") == "no_checks_ran"
            )
        except json.JSONDecodeError:
            test_result(
                "checks run --allow-no-checks output is valid JSON",
                False
            )

    print()
    print("[Section 5] CLI --timeout flag")

    with tempfile.TemporaryDirectory() as tmpdir:
        cache_json = json.dumps({
            "commands": {
                "test": "true"
            }
        })
        proc = subprocess.run(
            [sys.executable, "-m", "scripts.workflow.cli", "checks", "run", "--timeout", "10", "-"],
            input=cache_json,
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent
        )

        test_result(
            "checks run --timeout completes successfully",
            proc.returncode == 0
        )

    print()
    print("[Section 6] Timeout precedence: cache checkTimeoutSecs < env < --timeout")

    def run_slow(extra_args, env_extra, cache_timeout):
        import os
        cache = {"commands": {"test": "sleep 3"}}
        if cache_timeout is not None:
            cache["checkTimeoutSecs"] = cache_timeout
        env = {k: v for k, v in os.environ.items() if k != "SHIPIT_CHECK_TIMEOUT_SECS"}
        env.update(env_extra)
        return subprocess.run(
            [sys.executable, "-m", "scripts.workflow.cli", "checks", "run", *extra_args, "-"],
            input=json.dumps(cache), capture_output=True, text=True,
            cwd=Path(__file__).parent.parent, env=env
        )

    proc = run_slow([], {}, 1)
    test_result("cache checkTimeoutSecs=1 times out a 3s check", proc.returncode == 1 and "timed out after 1s" in proc.stdout)
    proc = run_slow([], {}, 10)
    test_result("cache checkTimeoutSecs=10 lets a 3s check pass", proc.returncode == 0)
    proc = run_slow([], {"SHIPIT_CHECK_TIMEOUT_SECS": "1"}, 10)
    test_result("env overrides cache", proc.returncode == 1 and "timed out after 1s" in proc.stdout)
    proc = run_slow(["--timeout", "10"], {"SHIPIT_CHECK_TIMEOUT_SECS": "1"}, 1)
    test_result("--timeout overrides env and cache", proc.returncode == 0)
    proc = run_slow([], {}, -5)
    test_result("invalid cache checkTimeoutSecs falls back to default (3s check passes)", proc.returncode == 0)

    print()
    print("[Section 7] checks resolve-check: JSON fields, opt-out vs not-configured exit 1 (#338)")

    def clean_env(**extra):
        import os
        env = {k: v for k, v in os.environ.items() if k != "SHIPIT_CHECK_TIMEOUT_SECS"}
        env.update(extra)
        return env

    def resolve_cli(wt, main=None, **env_extra):
        args = [sys.executable, "-m", "scripts.workflow.cli", "checks", "resolve-check", "--worktree", str(wt)]
        if main is not None:
            args += ["--main-worktree", str(main)]
        return subprocess.run(args, capture_output=True, text=True,
                              cwd=Path(__file__).parent.parent, env=clean_env(**env_extra))

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        wt, main = root / "wt", root / "main"
        (wt / ".claude").mkdir(parents=True)
        (main / ".claude").mkdir(parents=True)
        (main / ".claude" / "repo-cache.json").write_text(
            json.dumps({"commands": {"check": "exit 0"}, "checkTimeoutSecs": 900}))

        proc = resolve_cli(wt, main)
        out = json.loads(proc.stdout) if proc.stdout.strip() else {}
        test_result(
            "resolve-check with a configured command exits 0",
            proc.returncode == 0,
            f"rc={proc.returncode} stderr={proc.stderr[:200]!r}",
        )
        test_result(
            "resolve-check JSON carries command, opt_out_reason, timeout_secs, timeout_source",
            out.get("command") == "exit 0"
            and out.get("opt_out_reason") is None
            and out.get("timeout_secs") == 900
            and "repo-cache" in str(out.get("timeout_source")),
            f"out={out}",
        )

        proc = resolve_cli(wt, main, SHIPIT_CHECK_TIMEOUT_SECS="42")
        out = json.loads(proc.stdout) if proc.stdout.strip() else {}
        test_result(
            "resolve-check timeout_secs reflects SHIPIT_CHECK_TIMEOUT_SECS",
            out.get("timeout_secs") == 42,
            f"out={out}",
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        wt, main = root / "wt", root / "main"
        (wt / ".claude").mkdir(parents=True)
        (main / ".claude").mkdir(parents=True)
        (wt / ".claude" / "project.yaml").write_text('checkOptOut: "docs-only repo"\n')

        proc = resolve_cli(wt, main)
        out = json.loads(proc.stdout) if proc.stdout.strip() else {}
        test_result(
            "resolve-check with checkOptOut exits 1 (fail-closed for existing consumers)",
            proc.returncode == 1,
            f"rc={proc.returncode}",
        )
        test_result(
            "resolve-check opt-out: source 'opt-out' and opt_out_reason is the reason",
            out.get("source") == "opt-out" and out.get("opt_out_reason") == "docs-only repo" and out.get("command") is None,
            f"out={out}",
        )
        opt_out_source = out.get("source")

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        wt, main = root / "wt", root / "main"
        (wt / ".claude").mkdir(parents=True)
        (main / ".claude").mkdir(parents=True)

        proc = resolve_cli(wt, main)
        out = json.loads(proc.stdout) if proc.stdout.strip() else {}
        test_result(
            "resolve-check with nothing configured exits 1",
            proc.returncode == 1,
            f"rc={proc.returncode}",
        )
        test_result(
            "resolve-check not-configured: failure_reason names .claude/project.yaml and commands.check, opt_out_reason null",
            out.get("opt_out_reason") is None
            and ".claude/project.yaml" in str(out.get("failure_reason"))
            and "commands.check" in str(out.get("failure_reason")),
            f"out={out}",
        )
        test_result(
            "resolve-check opt-out and not-configured have distinct source values (read source, not exit code)",
            out.get("source") != opt_out_source and out.get("source") is None,
            f"not-configured source={out.get('source')!r} opt-out source={opt_out_source!r}",
        )

    print()
    print("[Section 8] checks run -: a resolved timeout, the timeout prefix, and no exit 2 on a timeout (#338)")

    from workflow.checks import TIMEOUT_ERROR_PREFIX

    def run_cli_cache(cache, cwd=None, env_extra=None):
        import os
        env = {k: v for k, v in os.environ.items() if k != "SHIPIT_CHECK_TIMEOUT_SECS"}
        env["PYTHONPATH"] = str(Path(__file__).parent.parent)
        env.update(env_extra or {})
        return subprocess.run(
            [sys.executable, "-m", "scripts.workflow.cli", "checks", "run", "-"],
            input=json.dumps(cache), capture_output=True, text=True,
            cwd=cwd or Path(__file__).parent.parent, env=env,
        )

    proc = run_cli_cache({"commands": {"check": "exit 0"}, "checkTimeoutSecs": 5})
    out = json.loads(proc.stdout) if proc.stdout.strip() else {}
    test_result(
        "checks run - with a passing single check exits 0",
        proc.returncode == 0 and out.get("status") == "passed",
        f"rc={proc.returncode} out={out}",
    )
    test_result(
        "checks run - runs only the check command (planned and executed are ['check'])",
        out.get("planned") == ["check"] and out.get("executed") == ["check"],
        f"planned={out.get('planned')} executed={out.get('executed')}",
    )

    proc = run_cli_cache({"commands": {"check": "exit 3"}, "checkTimeoutSecs": 5})
    test_result(
        "checks run - with a failing check (exit 3) exits 1",
        proc.returncode == 1,
        f"rc={proc.returncode}",
    )

    proc = run_cli_cache({"commands": {"check": "/bin/sleep 5"}, "checkTimeoutSecs": 1})
    out = json.loads(proc.stdout) if proc.stdout.strip() else {}
    errors = [r.get("error") or "" for r in out.get("results", [])]
    test_result(
        "checks run - timeout exits 1 (not 2) with the timeout prefix in the step error",
        proc.returncode == 1 and any(e.startswith(TIMEOUT_ERROR_PREFIX) for e in errors),
        f"rc={proc.returncode} errors={errors}",
    )

    print()
    print("[Section 9] checks run -: the check runs from the cwd it is started in (#338 cwd contract)")

    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir) / "worktree"
        elsewhere = Path(tmpdir) / "elsewhere"
        worktree.mkdir()
        elsewhere.mkdir()
        (worktree / "marker").write_text("here\n")

        proc_wt = run_cli_cache({"commands": {"check": "test -f marker"}}, cwd=worktree)
        test_result(
            "checks run - started in the worktree runs the command there (marker found, exit 0)",
            proc_wt.returncode == 0,
            f"rc={proc_wt.returncode} out={proc_wt.stdout[:200]!r}",
        )

        proc_else = run_cli_cache({"commands": {"check": "test -f marker"}}, cwd=elsewhere)
        test_result(
            "checks run - started outside the worktree checks the wrong tree (no marker, exit 1)",
            proc_else.returncode == 1,
            f"rc={proc_else.returncode} out={proc_else.stdout[:200]!r}",
        )

    print("[Section 10] checks ensure-check CLI: CHECK_SOURCE first line and CHECK_ACTION (#417 Step 2)")

    def run_checks_cli(args):
        return subprocess.run(
            [sys.executable, "-m", "scripts.workflow.cli", "checks"] + args,
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent,
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        main = base / "main"
        feat = base / "feat"
        feat.mkdir(parents=True)
        main.mkdir(parents=True)
        (feat / "package-lock.json").write_text("{}")
        (feat / "package.json").write_text(json.dumps({"scripts": {"check": "run-all"}}))
        proc = run_checks_cli(["ensure-check", "--worktree", str(feat), "--main-worktree", str(main)])
        lines = proc.stdout.splitlines()
        test_result(
            "ensure-check detected: exit 0, CHECK_SOURCE is the first line, then CHECK_ACTION and CHECK_COMMAND",
            proc.returncode == 0 and lines[:2] == ["CHECK_SOURCE: repo-cache (package-json)", "CHECK_ACTION: detected"]
            and "CHECK_COMMAND: npm run check" in lines,
            f"rc={proc.returncode} out={proc.stdout!r} err={proc.stderr[-200:]!r}",
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        main = base / "main"
        feat = base / "feat"
        feat.mkdir(parents=True)
        main.mkdir(parents=True)
        (feat / "CLAUDE.md").write_text("Run `just check` before pushing.\n")
        proc = run_checks_cli(["ensure-check", "--worktree", str(feat), "--main-worktree", str(main)])
        test_result(
            "ensure-check unresolved: exit 0, CHECK_SOURCE unresolved, SUGGESTION is labelled unverified",
            proc.returncode == 0 and proc.stdout.splitlines()[0] == "CHECK_SOURCE: unresolved"
            and "CHECK_ACTION: unresolved" in proc.stdout
            and "SUGGESTION: just check (from CLAUDE.md, unverified)" in proc.stdout,
            f"rc={proc.returncode} out={proc.stdout!r}",
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        main = base / "main"
        main.mkdir(parents=True)
        proc = run_checks_cli(["record-check-answer", "--main-worktree", str(main), "--command", "make verify"])
        resolved = run_checks_cli(["resolve-check", "--worktree", str(main)])
        test_result(
            "record-check-answer --command exits 0, prints CHECK_RECORDED, and the answer resolves",
            proc.returncode == 0 and proc.stdout.startswith("CHECK_RECORDED:")
            and json.loads(resolved.stdout)["command"] == "make verify",
            f"rc={proc.returncode} out={proc.stdout!r}",
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        main = Path(tmpdir) / "main"
        main.mkdir(parents=True)
        (main / ".claude").mkdir()
        (main / ".claude" / "repo-cache.json").write_text(json.dumps({"version": 1, "commands": {"check": "from-cache"}}))
        proc = run_checks_cli(["record-check-answer", "--main-worktree", str(main), "--command", "new"])
        test_result(
            "record-check-answer that does not verify exits 1 and names the YAML to add by hand",
            proc.returncode == 1 and "add this YAML by hand" in proc.stdout and 'check: "new"' in proc.stdout,
            f"rc={proc.returncode} out={proc.stdout!r}",
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        main = Path(tmpdir) / "main"
        main.mkdir(parents=True)
        proc = run_checks_cli(
            ["record-check-answer", "--main-worktree", str(main), "--command", "a", "--opt-out", "b"]
        )
        test_result(
            "record-check-answer with both --command and --opt-out is an argparse usage error (exit 2)",
            proc.returncode == 2,
            f"rc={proc.returncode} err={proc.stderr[-200:]!r}",
        )

    print()
    h.summarize_and_exit()
