#!/usr/bin/env python3
"""
check_cleanup_hook: the pre-merge preflight for the teardown hook.

commands.cleanup_check is read from the MAIN worktree's project.yaml and runs in the target
worktree, so a teardown that would refuse after the merge is caught while the PR is still open.

Run with: python3 tests/test_workflow_cleanup_check_hook.py
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.cleanup import check_cleanup_hook
from _test_harness import Harness
from _git_fixture import GitFixture

SCRIPTS = Path(__file__).parent.parent / "scripts"


def _fixture(project_yaml):
    fixture = GitFixture()
    fixture.create_initial_commit("initial")
    fixture.create_branch("feature/test")
    wt = fixture.create_worktree("feature/test")
    if project_yaml is not None:
        claude_dir = fixture.repo_root / ".claude"
        claude_dir.mkdir(exist_ok=True)
        (claude_dir / "project.yaml").write_text(project_yaml)
    return fixture, wt


if __name__ == "__main__":
    h = Harness("WORKFLOW CLEANUP CHECK-HOOK TEST SUITE")
    t = h.test_result

    print("[Section 1] a passing check runs in the target worktree")
    fx, wt = _fixture('commands:\n  cleanup_check: "pwd > ../check-cwd.txt"\n')
    try:
        status, message = check_cleanup_hook(wt)
        cwd_file = wt.parent / "check-cwd.txt"
        t("status is pass", status == "pass", f"{status}: {message}")
        t("check ran with the target worktree as cwd",
          cwd_file.exists() and Path(cwd_file.read_text().strip()).resolve() == wt.resolve())
        t("the worktree still exists", wt.exists())
    finally:
        fx.cleanup()

    print("[Section 2] a failing check is fail with the output tail")
    fx, wt = _fixture('commands:\n  cleanup_check: "echo would refuse: plain env >&2; exit 3"\n')
    try:
        status, message = check_cleanup_hook(wt)
        t("status is fail", status == "fail", f"{status}: {message}")
        t("message names the exit code", "exit 3" in message, message)
        t("message carries the output tail", "would refuse: plain env" in message, message)
    finally:
        fx.cleanup()

    print("[Section 3] no project.yaml is none")
    fx, wt = _fixture(None)
    try:
        status, message = check_cleanup_hook(wt)
        t("status is none", status == "none", f"{status}: {message}")
    finally:
        fx.cleanup()

    print("[Section 4] a teardown with no preflight is none, and says so")
    fx, wt = _fixture('commands:\n  cleanup: "exit 1"\n')
    try:
        status, message = check_cleanup_hook(wt)
        t("status is none", status == "none", f"{status}: {message}")
        t("message says the teardown is not preflighted", "not preflighted" in message, message)
        t("the teardown itself did not run (worktree still exists)", wt.exists())
    finally:
        fx.cleanup()

    print("[Section 5] a blank cleanup_check value is fail")
    fx, wt = _fixture('commands:\n  cleanup_check: ""\n')
    try:
        status, message = check_cleanup_hook(wt)
        t("status is fail", status == "fail", f"{status}: {message}")
        t("message names the key", "commands.cleanup_check" in message, message)
    finally:
        fx.cleanup()

    print("[Section 6] the main worktree itself is none")
    fx, _ = _fixture('commands:\n  cleanup_check: "exit 1"\n')
    try:
        status, message = check_cleanup_hook(fx.repo_root)
        t("status is none for main", status == "none", f"{status}: {message}")
    finally:
        fx.cleanup()

    print("[Section 7] CLI prints CLEANUP_CHECK and exits 1 only on fail")
    for yaml_text, want_status, want_rc in (
        ('commands:\n  cleanup_check: "true"\n', "pass", 0),
        ('commands:\n  cleanup_check: "false"\n', "fail", 1),
        (None, "none", 0),
    ):
        fx, wt = _fixture(yaml_text)
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "workflow.cli", "cleanup", "check-hook", str(wt)],
                cwd=str(SCRIPTS), capture_output=True, text=True,
            )
            t(f"CLI {want_status}: prints CLEANUP_CHECK: {want_status}",
              proc.stdout.startswith(f"CLEANUP_CHECK: {want_status}:"), f"out={proc.stdout!r} err={proc.stderr[-300:]!r}")
            t(f"CLI {want_status}: exit {want_rc}", proc.returncode == want_rc, f"rc={proc.returncode}")
        finally:
            fx.cleanup()

    h.summarize_and_exit()
