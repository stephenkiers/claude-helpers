#!/usr/bin/env python3
"""
Spec-driven tests for the #338 check-gate hardening.

Covers behaviour not already pinned by tests/test_workflow_checks.py,
tests/test_workflow_checks_cli.py and tests/test_flow_verify_receipt_check_gate.py:

- checks.py: invalid project.yaml checkTimeoutSecs values (negative, string) are ignored and
  named in timeout_source; invalid checkOptOut values (empty string, number) are ignored and the
  failure reason says checkOptOut must be a non-empty string.
- checks.py: OPT_OUT_SOURCE is exported and is the source an opt-out resolution reports.
- checks.py / CLI: a timed-out check's failure output names checkTimeoutSecs.
- checks.py: the no-check-configured message puts "sources tried" on its own line.
- flow-verify-receipt.py: an OK receipt whose CHECK lines contradict the resolver (opt-out source
  together with a command) is rejected; when re-resolution fails, the underlying resolution
  reason is reported.

Run with: python3 tests/test_check_gate_hardening_spec.py
"""

import contextlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from workflow.checks import (
    NO_CHECK_CONFIGURED_MESSAGE, OPT_OUT_SOURCE, resolve_check_command, resolve_check_timeout,
)
from _test_harness import Harness, REPO_ROOT
from test_flow_verify_receipt_check_gate import (
    make_repo, passing_lines, receipt_text, run_verify, write_opt_out, write_receipt,
)


@contextlib.contextmanager
def _env_unset(name):
    """Remove an environment variable for the duration of the block, then restore it."""
    saved = os.environ.pop(name, None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ[name] = saved


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _run_checks_cli(cache, env_extra=None):
    """Run `checks run -` with the given repo-cache dict on stdin; return the CompletedProcess."""
    env = {k: v for k, v in os.environ.items() if k != "SHIPIT_CHECK_TIMEOUT_SECS"}
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-m", "scripts.workflow.cli", "checks", "run", "-"],
        input=json.dumps(cache), capture_output=True, text=True,
        cwd=str(REPO_ROOT), env=env,
    )


if __name__ == "__main__":
    h = Harness("CHECK GATE HARDENING SPEC TEST SUITE")
    test_result = h.test_result

    print("[Section 1] OPT_OUT_SOURCE is exported and reported by an opt-out resolution")

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
    print("[Section 2] Invalid checkOptOut edges are ignored with the non-empty-string note")

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
    print("[Section 3] Invalid project.yaml checkTimeoutSecs edges are named in timeout_source")

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
    print("[Section 4] A timed-out check's failure output names checkTimeoutSecs")

    proc = _run_checks_cli({"commands": {"test": "sleep 3"}, "checkTimeoutSecs": 1})
    combined = proc.stdout + proc.stderr
    test_result(
        "timed-out check (checkTimeoutSecs=1, sleep 3): exit 1 and the timeout is reported",
        proc.returncode == 1 and "timed out after 1s" in combined,
        f"rc={proc.returncode} out={combined[:300]!r}",
    )
    test_result(
        "timed-out check failure output names checkTimeoutSecs",
        "(checkTimeoutSecs or SHIPIT_CHECK_TIMEOUT_SECS sets the limit)" in combined,
        f"out={combined[:300]!r}",
    )

    print()
    print("[Section 5] No-check-configured message: sources tried on its own line")

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
    print("[Section 6] flow-verify-receipt: contradicting CHECK lines and underlying resolution reason")

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        write_opt_out(repo, "docs-only repo")
        contradict = (
            f"CHECK: just check\nCHECK_EXIT: 0\nCHECK_SOURCE: opt-out\nCHECK_SHA: {head_sha}\n"
        )
        receipt = write_receipt(home, receipt_text(contradict))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "contradiction: opt-out source together with a command is rejected (exit 1, not ARTIFACTS: ok)",
            exit_code == 1 and "ARTIFACTS: ok" not in stdout
            and "check substituted: receipt ran 'just check', resolved 'opt-out'" in stdout,
            f"rc={exit_code} stdout={stdout[:300]!r} stderr={stderr[:200]!r}",
        )

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        # Invalid checkOptOut and no command: re-resolution fails with the resolver's own reason.
        _write(Path(repo) / ".claude" / "project.yaml", "checkOptOut: true\n")
        receipt = write_receipt(home, receipt_text(passing_lines(head_sha)))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "re-resolution failure: the underlying 'checkOptOut must be a non-empty string' reason is reported",
            exit_code == 1 and "checkOptOut must be a non-empty string" in stdout,
            f"rc={exit_code} stdout={stdout[:300]!r} stderr={stderr[:200]!r}",
        )

    print()
    print("[Section 7] checkTimeoutSecs boundary: 0 is ignored, 1 is accepted (valid_timeout_secs pin)")

    with tempfile.TemporaryDirectory() as tmp, _env_unset("SHIPIT_CHECK_TIMEOUT_SECS"):
        root = Path(tmp)
        wt, main = root / "wt", root / "main"
        wt.mkdir()
        main.mkdir()
        project_yaml = wt / ".claude" / "project.yaml"
        _write(project_yaml, "commands:\n  check: just check\ncheckTimeoutSecs: 0\n")
        seconds, timeout_source = resolve_check_timeout(wt, main, project_yaml, "project.yaml", "worktree")
        test_result(
            "checkTimeoutSecs 0 in project.yaml is ignored: falls back to the 300 default",
            seconds == 300,
            f"seconds={seconds!r} timeout_source={timeout_source!r}",
        )
        test_result(
            "checkTimeoutSecs 0 is named in timeout_source as ignored",
            timeout_source == "default (ignored invalid checkTimeoutSecs in worktree project.yaml)",
            f"timeout_source={timeout_source!r}",
        )
        _write(project_yaml, "commands:\n  check: just check\ncheckTimeoutSecs: 1\n")
        seconds, timeout_source = resolve_check_timeout(wt, main, project_yaml, "project.yaml", "worktree")
        test_result(
            "checkTimeoutSecs 1 in project.yaml is accepted: seconds 1, sourced from worktree project.yaml",
            seconds == 1 and timeout_source == "worktree project.yaml",
            f"seconds={seconds!r} timeout_source={timeout_source!r}",
        )

    print()
    h.summarize_and_exit()
