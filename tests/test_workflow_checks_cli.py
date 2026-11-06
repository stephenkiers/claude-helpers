#!/usr/bin/env python3
"""
Test suite for the checks CLI subprocess invocation and exit codes.

Covers resolve-check, ensure-check, record-check-answer and validate. The `checks run`
subcommand and the check receipt were removed (ADR-0013 Amendment 12), so they are not tested here.

Exit codes exercised:
- resolve-check: 0 when a command resolves, 1 when it does not (opt-out or not configured)
- validate: 0 passed, 1 failed, 2 for a usage error (argparse)

Run with: python3 tests/test_workflow_checks_cli.py
"""

import sys
import json
import os
import subprocess
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _test_harness import Harness, REPO_ROOT


_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), env={**os.environ, **_GIT_ENV},
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _fixture_work_tree(root, commands):
    """Bare origin with one commit on main, cloned as a work tree one commit ahead, with repo-cache commands."""
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
    (work / ".claude" / "repo-cache.json").write_text(json.dumps({"version": 1, "commands": commands}) + "\n")
    return work


def _checks_cli(*args):
    """Run `python3 -m scripts.workflow.cli checks <args>` from the repo root."""
    env = {k: v for k, v in os.environ.items() if k != "SHIPIT_CHECK_TIMEOUT_SECS"}
    return subprocess.run(
        [sys.executable, "-m", "scripts.workflow.cli", "checks", *args],
        capture_output=True, text=True, cwd=str(REPO_ROOT), env=env,
    )


if __name__ == "__main__":
    h = Harness("WORKFLOW CHECKS CLI TEST SUITE")
    test_result = h.test_result

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

    def _parse_json(text):
        try:
            return json.loads(text)
        except ValueError:
            return {}

    print("[Section 16] checks resolve-check --mode: minimum resolves check_minimum, full resolves check (#462 Part A)")
    with tempfile.TemporaryDirectory() as tmp:
        work = _fixture_work_tree(Path(tmp), {"check": "true", "check_minimum": "echo minimum-cmd && true"})
        r_min = _checks_cli("resolve-check", "--worktree", str(work), "--mode", "minimum")
        out_min = _parse_json(r_min.stdout)
        test_result(
            "resolve-check --mode minimum resolves the check_minimum command",
            r_min.returncode == 0 and out_min.get("command") == "echo minimum-cmd && true",
            f"rc={r_min.returncode} out={out_min!r} err={r_min.stderr[-300:]!r}",
        )
        r_full = _checks_cli("resolve-check", "--worktree", str(work), "--mode", "full")
        out_full = _parse_json(r_full.stdout)
        test_result(
            "resolve-check --mode full resolves the check command",
            r_full.returncode == 0 and out_full.get("command") == "true",
            f"rc={r_full.returncode} out={out_full!r}",
        )
        r_bad = _checks_cli("resolve-check", "--worktree", str(work), "--mode", "bogus")
        test_result(
            "resolve-check with an invalid --mode exits 2 (argparse rejects it)",
            r_bad.returncode == 2,
            f"rc={r_bad.returncode} err={r_bad.stderr[-300:]!r}",
        )
        r_vbad = _checks_cli("validate", "--worktree", str(work), "--mode", "bogus")
        test_result(
            "validate with an invalid --mode exits 2 (argparse rejects it)",
            r_vbad.returncode == 2,
            f"rc={r_vbad.returncode} err={r_vbad.stderr[-300:]!r}",
        )

    print("[Section 16b] checks validate: exit codes and cache state through the CLI (#462 Part A)")
    with tempfile.TemporaryDirectory() as tmp:
        work = _fixture_work_tree(Path(tmp), {"check": "true", "check_minimum": "true"})
        v1 = _checks_cli("validate", "--worktree", str(work), "--mode", "full")
        j1 = _parse_json(v1.stdout)
        v2 = _checks_cli("validate", "--worktree", str(work), "--mode", "full")
        j2 = _parse_json(v2.stdout)
        test_result(
            "checks validate: a first passing run exits 0 and reports cache=miss",
            v1.returncode == 0 and j1.get("status") == "passed" and j1.get("cache") == "miss",
            f"rc={v1.returncode} out={j1!r} err={v1.stderr[-300:]!r}",
        )
        test_result(
            "checks validate: a repeat run on the same tree exits 0 and reports cache=hit",
            v2.returncode == 0 and j2.get("cache") == "hit" and j2.get("status") == "passed",
            f"rc={v2.returncode} out={j2!r}",
        )
        v3 = _checks_cli("validate", "--worktree", str(work), "--mode", "full", "--force")
        j3 = _parse_json(v3.stdout)
        test_result(
            "checks validate --force exits 0 and reports cache=forced",
            v3.returncode == 0 and j3.get("cache") == "forced",
            f"rc={v3.returncode} out={j3!r}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        work = _fixture_work_tree(Path(tmp), {"check": "true", "check_minimum": "false"})
        vf = _checks_cli("validate", "--worktree", str(work), "--mode", "minimum")
        jf = _parse_json(vf.stdout)
        test_result(
            "checks validate: a failing minimum check exits 1 with a non-passed status",
            vf.returncode == 1 and jf.get("status") not in (None, "passed"),
            f"rc={vf.returncode} out={jf!r}",
        )

    print("[Section 16c] resolve-check falls back to the full command when check_minimum is undeclared (#462 Part A)")
    with tempfile.TemporaryDirectory() as tmp:
        work = _fixture_work_tree(Path(tmp), {"check": "true"})
        r_fb = _checks_cli("resolve-check", "--worktree", str(work), "--mode", "minimum")
        out_fb = _parse_json(r_fb.stdout)
        test_result(
            "resolve-check --mode minimum with no check_minimum resolves the full command and reports fell_back_to_full",
            r_fb.returncode == 0 and out_fb.get("command") == "true" and out_fb.get("fell_back_to_full") is True,
            f"rc={r_fb.returncode} out={out_fb!r} err={r_fb.stderr[-300:]!r}",
        )

    print()
    h.summarize_and_exit()
