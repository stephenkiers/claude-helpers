#!/usr/bin/env python3
"""Check receipt: /merge-and-cleanup reuses /shipit's passed gate only while it still applies.

Run with: python3 tests/test_check_receipt.py
"""
import json
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

    # Test attestation and receipt flow
    result = checks.CheckResults(all_passed=True, status="passed", executed=["check"])
    ok, err = checks.record_check_attestation(wt, CMDS, "origin/main", result)
    h.test_result("record attestation succeeds", ok and err is None)

    ok, err = checks.write_check_receipt(wt, CMDS)
    h.test_result("write receipt from attestation succeeds", ok and err is None)

    fresh, why = checks.check_receipt_fresh(wt, CMDS)
    h.test_result("fresh right after write", fresh, why)

    # Test: write refused with no attestation
    _, wt_no_att = make_repo()
    ok, err = checks.write_check_receipt(wt_no_att, CMDS)
    h.test_result("write refused with no attestation", not ok and err is not None)

    # Test: write refused when attested run failed
    _, wt_failed = make_repo()
    failed_result = checks.CheckResults(all_passed=False, status="failed", failed_at="check")
    checks.record_check_attestation(wt_failed, CMDS, "origin/main", failed_result)
    ok, err = checks.write_check_receipt(wt_failed, CMDS)
    h.test_result("write refused when attested run failed", not ok and err is not None)

    # Test: write refused when HEAD tree differs
    _, wt_tree_diff = make_repo()
    checks.record_check_attestation(wt_tree_diff, CMDS, "origin/main", result)
    (wt_tree_diff / "c.txt").write_text("c")
    git(wt_tree_diff, "add", ".")
    git(wt_tree_diff, "commit", "-qm", "more")
    ok, err = checks.write_check_receipt(wt_tree_diff, CMDS)
    h.test_result("write refused when HEAD tree differs", not ok and "HEAD tree" in str(err))

    # Test: write refused when commands digest differs
    _, wt_cmd_diff = make_repo()
    checks.record_check_attestation(wt_cmd_diff, CMDS, "origin/main", result)
    ok, err = checks.write_check_receipt(wt_cmd_diff, {"check": "false"})
    h.test_result("write refused when commands digest differs", not ok and "commands" in str(err))

    # Test: base_sha in receipt is from attestation (move origin/main after attestation)
    _, wt2 = make_repo()
    checks.record_check_attestation(wt2, CMDS, "origin/main", result)
    ok, err = checks.write_check_receipt(wt2, CMDS)
    h.test_result("write receipt with attested base_sha", ok and err is None)

    # Move origin/main after the attestation was recorded
    other = Path(tempfile.mkdtemp()) / "other"
    git(other.parent, "clone", "-q", subprocess.check_output(["git", "remote", "get-url", "origin"], cwd=wt2, text=True).strip(), str(other))
    (other / "n.txt").write_text("n")
    git(other, "add", ".")
    git(other, "commit", "-qm", "moved")
    git(other, "push", "-q", "origin", "HEAD:main")
    fresh, why = checks.check_receipt_fresh(wt2, CMDS)
    h.test_result("base moved after attestation -> not fresh", not fresh and "moved" in why, why)

    # Test: receipt not fresh for corrupt JSON
    _, wt_corrupt = make_repo()
    receipt_path = wt_corrupt / checks.RECEIPT_RELPATH
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text("invalid json")
    fresh, why = checks.check_receipt_fresh(wt_corrupt, CMDS, fetch=False)
    h.test_result("corrupt receipt -> not fresh", not fresh)

    # Test: receipt not fresh for missing tree
    _, wt_no_tree = make_repo()
    receipt_path = wt_no_tree / checks.RECEIPT_RELPATH
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt = {"schema_version": 1, "base_ref": "origin/main", "base_sha": "abc123"}
    receipt_path.write_text(json.dumps(receipt))
    fresh, why = checks.check_receipt_fresh(wt_no_tree, CMDS, fetch=False)
    h.test_result("missing tree -> not fresh", not fresh and "empty" in why)

    # Test: receipt not fresh for missing base_sha
    _, wt_no_sha = make_repo()
    receipt_path = wt_no_sha / checks.RECEIPT_RELPATH
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt = {"schema_version": 1, "tree": "abc123", "base_ref": "origin/main"}
    receipt_path.write_text(json.dumps(receipt))
    fresh, why = checks.check_receipt_fresh(wt_no_sha, CMDS, fetch=False)
    h.test_result("missing base_sha -> not fresh", not fresh and "empty" in why)

    # Test: receipt not fresh for fetch failure
    _, wt_fetch_fail = make_repo()
    receipt_path = wt_fetch_fail / checks.RECEIPT_RELPATH
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt = {"schema_version": 1, "tree": "abc123", "base_ref": "origin/nonexistent", "base_sha": "abc123"}
    receipt_path.write_text(json.dumps(receipt))
    fresh, why = checks.check_receipt_fresh(wt_fetch_fail, CMDS, fetch=True)
    h.test_result("fetch failure -> not fresh", not fresh and "could not fetch" in why)

    # Test: happy path with changed commands -> not fresh
    h.test_result("changed commands -> not fresh", not checks.check_receipt_fresh(wt, {"check": "false"})[0])

    # Test: merge gate - verify the receipt freshness works correctly
    # Create a new repo to avoid stale base issues from previous tests
    _, wt_merge = make_repo()
    (wt_merge / ".claude").mkdir()
    (wt_merge / ".claude" / "repo-cache.json").write_text('{"schema_version": "1", "commands": {"check": "true"}}')
    # Commit the repo-cache.json so there are no uncommitted files
    git(wt_merge, "add", ".claude/repo-cache.json")
    git(wt_merge, "commit", "-qm", "add cache")

    # Record attestation and write receipt
    result_merge = checks.CheckResults(all_passed=True, status="passed", executed=["check"])
    att_ok, att_err = checks.record_check_attestation(wt_merge, {"check": "true"}, "origin/main", result_merge)
    h.test_result("record attestation for merge gate test", att_ok and att_err is None, att_err)

    rec_ok, rec_err = checks.write_check_receipt(wt_merge, {"check": "true"})
    h.test_result("write receipt for merge gate test", rec_ok and rec_err is None, rec_err)

    fresh, why = checks.check_receipt_fresh(wt_merge, {"check": "true"}, fetch=False)
    h.test_result("fresh receipt after writing", fresh, why)

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
