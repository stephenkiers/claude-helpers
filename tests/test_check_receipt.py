#!/usr/bin/env python3
"""Check receipt: /merge-and-cleanup reuses /shipit's passed gate only while it still applies.

Run with: python3 tests/test_check_receipt.py
"""
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow import checks
from workflow.merge import _run_merge_gate_checks
from _test_harness import Harness

CMDS = {"check": "true", "test": None}


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})


def make_repo():
    root = Path(tempfile.mkdtemp())
    origin, wt = root / "origin.git", root / "wt"
    git(root, "init", "-q", "--bare", "-b", "main", str(origin))
    git(root, "clone", "-q", str(origin), str(wt))
    (wt / "a.txt").write_text("a")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "base")
    git(wt, "push", "-q", "origin", "HEAD:main")
    git(wt, "checkout", "-qb", "feat")
    (wt / "b.txt").write_text("b")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "work")
    return root, wt


def main():
    h = Harness("check receipt")
    _, wt = make_repo()

    ok, err = checks.write_check_receipt(wt, CMDS, "origin/main")
    h.test_result("write succeeds", ok and err is None)
    fresh, why = checks.check_receipt_fresh(wt, CMDS)
    h.test_result("fresh right after write", fresh, why)

    h.test_result("changed commands -> not fresh", not checks.check_receipt_fresh(wt, {"check": "false"})[0])

    (wt / "c.txt").write_text("c")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "more")
    fresh, why = checks.check_receipt_fresh(wt, CMDS)
    h.test_result("new commit -> not fresh", not fresh and "tree" in why, why)

    _, wt2 = make_repo()
    checks.write_check_receipt(wt2, CMDS, "origin/main")
    other = Path(tempfile.mkdtemp()) / "other"
    git(other.parent, "clone", "-q", subprocess.check_output(["git", "remote", "get-url", "origin"], cwd=wt2, text=True).strip(), str(other))
    (other / "n.txt").write_text("n")
    git(other, "add", ".")
    git(other, "commit", "-qm", "moved")
    git(other, "push", "-q", "origin", "HEAD:main")
    fresh, why = checks.check_receipt_fresh(wt2, CMDS)
    h.test_result("base moved -> not fresh", not fresh and "moved" in why, why)

    h.test_result("no receipt -> not fresh", not checks.check_receipt_fresh(Path(tempfile.mkdtemp()), CMDS, fetch=False)[0])

    # merge gate: fresh receipt skips run_checks; stale receipt runs them
    _, wt3 = make_repo()
    (wt3 / ".claude").mkdir()
    (wt3 / ".claude" / "repo-cache.json").write_text('{"schema_version": "1", "commands": {"check": "true"}}')
    with mock.patch("workflow.checks.run_checks") as rc:
        checks.write_check_receipt(wt3, {"check": "true"}, "origin/main")
        res = _run_merge_gate_checks(wt3)
        h.test_result("fresh receipt: gate passes without running checks", res == (True, True, None) and not rc.called, str(res))
    with mock.patch("workflow.checks.run_checks") as rc:
        (wt3 / "x.txt").write_text("x")
        git(wt3, "add", ".")
        git(wt3, "commit", "-qm", "x")
        rc.return_value = (mock.Mock(all_passed=True, status="passed"), None)
        _run_merge_gate_checks(wt3)
        h.test_result("stale receipt: checks run", rc.called)
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
