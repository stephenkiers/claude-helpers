#!/usr/bin/env python3
"""
Fix-step test-edit checks for scripts/flow-verify-receipt.py (#497).

An OK fix receipt may name an existing-test edit that a human approved in the fix step's
answers file (TEST_EDIT: <finding id> <path> <answers file>), together with the approval commit
(TEST_EDIT_COMMIT). The verifier accepts such an edit only when the approval is recorded in the
flow's human ledger, the answers file bytes still match the recorded sha256, the finding id and
path match the approved question, and the approval commit touches only the approved test file.
Edits that were declined, failed to apply, or do not match the approval are reported as
OUTSTANDING lines.

Fixtures use real temporary git repositories and real FLOW_DIR trees. Ledger records are appended
through scripts/flow-ledger.py, never hand-built. Answers files use the relay shape written by the
fix step.

Run with: python3 tests/test_flow_verify_receipt_test_edit.py
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _test_harness import Harness, REPO_ROOT  # noqa: E402
from _assess_fixtures import (  # noqa: E402
    BASE_IMPL, DECISION_OPTIONS, ISSUE_URL, S3, S4, TS, assess_line, disp, gate, make_flow, make_home,
    outstanding_set, parse_outstanding, write_answers, write_assess, write_marker,
)

VERIFIER = REPO_ROOT / "scripts" / "flow-verify-receipt.py"
LEDGER = REPO_ROOT / "scripts" / "flow-ledger.py"
SINCE = "2026-10-13T00:00:00Z"
GIT_ID = ["-c", "user.email=test@example.com", "-c", "user.name=Test", "-c", "commit.gpgsign=false"]
DECLINE = "Decline — leave OUTSTANDING"
TEST_EDIT_D3 = "TEST_EDIT: D3 tests/test_x.py answers/fix-02.json"
DECLINED_D5 = "D5 (test-edit-declined: tests/test_z.py) [self-report]"

BASE_FILES = {
    "README.md": "# repo\n",
    "src/app.py": 'VALUE = "old"\n',
    "tests/test_x.py": "def test_x():\n    assert True\n",
    "tests/test_z.py": "def test_z():\n    assert True\n",
    "tests/test_w.py": "def test_w():\n    assert True\n",
}
UNITS = ({"src/app.py": 'VALUE = "new"\n'}, (), "ordinary units")
OTHER_UNIT = ({"src/other.py": "def g():\n    return 2\n"}, (), "another ordinary unit")
APPROVED_X = ({"tests/test_x.py": "def test_x():\n    assert 1 == 1\n"}, (), "approved test edit")


def git(cwd, *args):
    result = subprocess.run(["git", *GIT_ID, *args], cwd=str(cwd), capture_output=True, text=True, check=True)
    return result.stdout.strip()


def build_repo(root, steps):
    """Linear repo: BASE_FILES as the base commit, then one commit per (files, removed, message) in
    `steps`. Returns (repo path, [sha of each commit in order, base first])."""
    repo = Path(root) / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    shas = []
    for files, removed, message in [(BASE_FILES, (), "base"), *steps]:
        for rel, text in files.items():
            path = repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        for rel in removed:
            (repo / rel).unlink()
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", message)
        shas.append(git(repo, "rev-parse", "HEAD"))
    return str(repo), shas


def build_scenario(root, approval=APPROVED_X, later=()):
    """base, ordinary units, the approval commit, then any `later` steps.
    Returns (repo, base sha, approval sha, HEAD sha)."""
    repo, shas = build_repo(root, [UNITS, approval, *later])
    return repo, shas[0], shas[2], shas[-1]


def write_check_cache(repo):
    cache = Path(repo) / ".claude"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "repo-cache.json").write_text(json.dumps({"commands": {"check": "just check"}}))


def fix_receipt(head, *, test_edit="TEST_EDIT: none", test_edit_commit=None, outstanding=()):
    lines = ["ROUND 1", "CHECK: just check", "CHECK_EXIT: 0", "CHECK_SOURCE: repo-cache", f"CHECK_SHA: {head}",
             "SPLIT_BASIS: parallel", "MIN_UNITS: 1", "PRIOR_SPLIT_BASIS: none", "UNITS: 3", test_edit]
    if test_edit_commit is not None:
        lines.append(f"TEST_EDIT_COMMIT: {test_edit_commit}")
    lines += [f"OUTSTANDING: {item}" for item in outstanding] or ["OUTSTANDING: none"]
    lines += ["Decision: OK", "<!-- step-end -->"]
    return "\n".join(lines) + "\n"


def fix_flow(td):
    """FLOW_DIR (with a log of an OK implement and fix) plus HOME and a review dir for the fix step."""
    home = make_home(td)
    flow = make_flow(td, log=[f"{TS} fix ok"])
    review_dir = Path(home) / ".claude" / "reviews" / "rv"
    review_dir.mkdir(parents=True)
    (review_dir / "claude-action-plan.md").write_text("# Action plan\n\nNo items to do.\n")
    return home, flow, review_dir


def env_for(home):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    env["HOME"] = str(home)
    env["FLOW_VERIFY_GIT_TIMEOUT_SECS"] = "60"
    return env


def run_fix(flow, home, repo, base, review_dir, receipt):
    """Write steps/05-fix.md and run the verifier's fix step on it. Returns (rc, stdout, stderr)."""
    path = flow / "steps" / "05-fix.md"
    path.write_text(receipt)
    cmd = [sys.executable, str(VERIFIER), "--step", "fix", "--receipt", str(path), "--since", SINCE,
           "--repo", repo, "--branch", "HEAD", "--base", base, "--review-dir", str(review_dir),
           "--flow-dir", str(flow)]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env_for(home))
    return result.returncode, result.stdout, result.stderr


def combined(out, err):
    return (out or "") + (err or "")


def passed_fix(rc, out):
    return rc == 0 and "ARTIFACTS: ok" in (out or "")


def write_fix_answers(flow, seq, entries):
    """entries: (question id, finding id, path, approve). Writes questions/fix-<seq>.json and
    answers/fix-<seq>.json in the relay shape (each question offers the approval label and the
    decline label). Returns the sha256 of the answers file bytes."""
    questions = []
    answers = {}
    for qid, item, path, approve in entries:
        approve_label = f"Approve edit: {item} {path}"
        questions.append({
            "id": qid, "item": item, "question": f"Approve the existing-test edit for {item}?",
            "header": "Test edit",
            "options": [{"label": approve_label, "description": ""}, {"label": DECLINE, "description": ""}],
        })
        answers[qid] = approve_label if approve else DECLINE
    (flow / "questions" / f"fix-{seq:02d}.json").write_text(json.dumps({"step": "fix", "seq": seq, "questions": questions}))
    answer_file = flow / "answers" / f"fix-{seq:02d}.json"
    answer_file.write_text(json.dumps({"step": "fix", "seq": seq, "answers": answers, "annotations": {}}))
    return hashlib.sha256(answer_file.read_bytes()).hexdigest()


def ledger_append(flow, home, text, step="fix"):
    """Record an answer through the real flow-ledger.py append."""
    result = subprocess.run(
        [sys.executable, str(LEDGER), "append", "--flow-dir", str(flow), "--kind", "answer",
         "--step", step, "--attempt", "1", "--text", text],
        capture_output=True, text=True, env=env_for(home))
    if result.returncode != 0:
        raise AssertionError(f"flow-ledger append failed: {result.stderr}")


def approved_flow(td, entries, *, ledger=True, ledger_step="fix"):
    """A fix FLOW_DIR whose answers/fix-02.json holds `entries`, recorded in the ledger unless ledger=False."""
    home, flow, review_dir = fix_flow(td)
    sha = write_fix_answers(flow, 2, entries)
    if ledger:
        ledger_append(flow, home, f"answer_ref: answers/fix-02.json\nsha256: {sha}", step=ledger_step)
    return home, flow, review_dir


# Fixture 10: D3's edit is approved, D5's question is declined.
ENTRIES_D3_APPROVED_D5_DECLINED = [("q1", "D3", "tests/test_x.py", True), ("q2", "D5", "tests/test_z.py", False)]


def main():
    h = Harness("FLOW-VERIFY-RECEIPT TEST-EDIT APPROVAL TESTS (#497)")

    print("[Receipt and outstanding-set contract without any approval]")
    with tempfile.TemporaryDirectory() as td:
        repo, base, _approval, head = build_scenario(td, approval=OTHER_UNIT)
        write_check_cache(repo)
        home, flow, review_dir = fix_flow(td)
        outstanding = [
            "D4 (test-edit-declined: tests/test_x.py) [self-report]",
            "D7 (test-removal: tests/test_y.py) [self-report]",
            "D9 (test-edit-coupled: tests/test_w.py) [self-report]",
        ]
        rc, out, err = run_fix(flow, home, repo, base, review_dir,
                               fix_receipt(head, outstanding=outstanding))
        h.test_result("OK fix receipt with TEST_EDIT none and three self-report OUTSTANDING lines passes",
                      passed_fix(rc, out), combined(out, err))
        rc2, out2, err2 = outstanding_set(flow, home)
        _count, rows = parse_outstanding(out2)
        h.test_result("outstanding-set exits 0 for that flow", rc2 == 0, combined(out2, err2))
        h.test_result("outstanding-set lists fix:D4, fix:D7 and fix:D9",
                      all(key in rows for key in ("fix:D4", "fix:D7", "fix:D9")), out2)
        h.test_result("outstanding-set gives each of those ids provenance 'self-report'",
                      all(rows.get(key, ("",))[0] == "self-report" for key in ("fix:D4", "fix:D7", "fix:D9")), out2)

    print()
    print("[Approved-set subset check]")
    with tempfile.TemporaryDirectory() as td:
        repo, base, approval, head = build_scenario(td)
        write_check_cache(repo)
        home, flow, review_dir = approved_flow(td, ENTRIES_D3_APPROVED_D5_DECLINED)
        rc, out, err = run_fix(flow, home, repo, base, review_dir,
                               fix_receipt(head, test_edit=TEST_EDIT_D3, test_edit_commit=approval,
                                           outstanding=[DECLINED_D5]))
        h.test_result("approved subset: TEST_EDIT for the approved path passes the test-edit check",
                      passed_fix(rc, out), combined(out, err))

        rc, out, err = run_fix(flow, home, repo, base, review_dir,
                               fix_receipt(head, test_edit="TEST_EDIT: D5 tests/test_z.py answers/fix-02.json",
                                           test_edit_commit=approval, outstanding=[DECLINED_D5]))
        h.test_result("unapproved path: TEST_EDIT for a declined question is rejected",
                      rc != 0 and not passed_fix(rc, out) and "tests/test_z.py" in combined(out, err),
                      combined(out, err))

    with tempfile.TemporaryDirectory() as td:
        repo, base, approval, head = build_scenario(td)
        write_check_cache(repo)
        home, flow, review_dir = approved_flow(td, [("q1", "D3", "tests/test_x.py", True),
                                                    ("q2", "D5", "tests/test_x.py", False)])
        rc, out, err = run_fix(flow, home, repo, base, review_dir,
                               fix_receipt(head, test_edit="TEST_EDIT: D5 tests/test_x.py answers/fix-02.json",
                                           test_edit_commit=approval, outstanding=[DECLINED_D5]))
        h.test_result("approval bound to finding id: TEST_EDIT for D5 on the approved path is rejected naming D5",
                      rc != 0 and not passed_fix(rc, out) and "D5" in combined(out, err), combined(out, err))
        rc, out, err = run_fix(flow, home, repo, base, review_dir,
                               fix_receipt(head, test_edit="TEST_EDIT: D3 tests/test_x.py answers/fix-02.json",
                                           test_edit_commit=approval, outstanding=[DECLINED_D5]))
        h.test_result("approval bound to finding id: TEST_EDIT for D3 on the same path passes",
                      passed_fix(rc, out), combined(out, err))

    with tempfile.TemporaryDirectory() as td:
        repo, base, approval, head = build_scenario(td)
        write_check_cache(repo)
        home, flow, review_dir = approved_flow(td, ENTRIES_D3_APPROVED_D5_DECLINED)
        write_fix_answers(flow, 2, [("q1", "D3", "tests/test_x.py", True), ("q2", "D5", "tests/test_z.py", True)])
        rc, out, err = run_fix(flow, home, repo, base, review_dir,
                               fix_receipt(head, test_edit=TEST_EDIT_D3, test_edit_commit=approval,
                                           outstanding=[DECLINED_D5]))
        h.test_result("sha256 mismatch: answers file rewritten after the ledger record is rejected naming sha256",
                      rc != 0 and not passed_fix(rc, out) and "sha256" in combined(out, err), combined(out, err))

    with tempfile.TemporaryDirectory() as td:
        repo, base, approval, head = build_scenario(td)
        write_check_cache(repo)
        home, flow, review_dir = approved_flow(td, ENTRIES_D3_APPROVED_D5_DECLINED, ledger=False)
        rc, out, err = run_fix(flow, home, repo, base, review_dir,
                               fix_receipt(head, test_edit=TEST_EDIT_D3, test_edit_commit=approval,
                                           outstanding=[DECLINED_D5]))
        h.test_result("no ledger record: approved edit without the ledger record is rejected naming answers/fix-02.json",
                      rc != 0 and not passed_fix(rc, out) and "answers/fix-02.json" in combined(out, err),
                      combined(out, err))

    print()
    print("[Approval commit set]")
    with tempfile.TemporaryDirectory() as td:
        extra_source = ({"tests/test_x.py": "def test_x():\n    assert 1 == 1\n", "src/app.py": 'VALUE = "other"\n'},
                        (), "approval also edits source")
        repo, base, approval, head = build_scenario(td, approval=extra_source)
        write_check_cache(repo)
        home, flow, review_dir = approved_flow(td, ENTRIES_D3_APPROVED_D5_DECLINED)
        rc, out, err = run_fix(flow, home, repo, base, review_dir,
                               fix_receipt(head, test_edit=TEST_EDIT_D3, test_edit_commit=approval,
                                           outstanding=[DECLINED_D5]))
        h.test_result("commit set: approval commit that also modifies src/app.py is rejected",
                      rc != 0 and not passed_fix(rc, out), combined(out, err))

    with tempfile.TemporaryDirectory() as td:
        deletion = ({}, ("tests/test_x.py",), "approval deletes the test")
        repo, base, approval, head = build_scenario(td, approval=deletion)
        write_check_cache(repo)
        home, flow, review_dir = approved_flow(td, ENTRIES_D3_APPROVED_D5_DECLINED)
        rc, out, err = run_fix(flow, home, repo, base, review_dir,
                               fix_receipt(head, test_edit=TEST_EDIT_D3, test_edit_commit=approval,
                                           outstanding=[DECLINED_D5]))
        h.test_result("commit set: approval commit that deletes tests/test_x.py is rejected",
                      rc != 0 and not passed_fix(rc, out), combined(out, err))

    with tempfile.TemporaryDirectory() as td:
        later = ({"tests/test_x.py": "def test_x():\n    assert 2 == 2\n"}, (), "later edit to the same test")
        repo, base, approval, head = build_scenario(td, later=[later])
        write_check_cache(repo)
        home, flow, review_dir = approved_flow(td, ENTRIES_D3_APPROVED_D5_DECLINED)
        rc, out, err = run_fix(flow, home, repo, base, review_dir,
                               fix_receipt(head, test_edit=TEST_EDIT_D3, test_edit_commit=approval,
                                           outstanding=[DECLINED_D5]))
        h.test_result("commit set: a later commit touching the same test does not cause a rejection",
                      passed_fix(rc, out), combined(out, err))

    print()
    print("[Ledger kind and step binding]")
    for name in ("test_flow_verify_receipt_outstanding_gate.py", "test_flow_verify_receipt_assess.py"):
        result = subprocess.run([sys.executable, str(REPO_ROOT / "tests" / name)], capture_output=True, text=True)
        h.test_result(f"existing {name} fixtures pass unmodified", result.returncode == 0,
                      (result.stdout + result.stderr)[-400:])

    for ledger_step, expect_pass in (("fix", False), ("assess", True)):
        with tempfile.TemporaryDirectory() as td:
            home = make_home(td)
            flow = make_flow(td, implement=BASE_IMPL)
            write_assess(flow, [assess_line(S3, kind="decision", options=DECISION_OPTIONS),
                                assess_line(S4, kind="decision", options=DECISION_OPTIONS)])
            write_answers(flow, 1, [("q1", S3, "Open an issue", None), ("q2", S4, "Open an issue", None)], ledger=False)
            write_marker(flow, [disp(S3, "filed-as-issue", option="Open an issue", route="issue", url=ISSUE_URL),
                                disp(S4, "filed-as-issue", option="Open an issue", route="issue", url=ISSUE_URL)])
            sha = hashlib.sha256((flow / "answers" / "assess-01.json").read_bytes()).hexdigest()
            ledger_append(flow, home, f"answer_ref: answers/assess-01.json\nsha256: {sha}", step=ledger_step)
            rc, out, err = gate(flow, home)
            text = combined(out, err)
            if expect_pass:
                h.test_result("assess answer_ref: a step-assess ledger record satisfies it (control)", rc == 0, text)
            else:
                h.test_result("assess answer_ref: a step-fix ledger record never satisfies an assess answer_ref",
                              rc != 0, text)

    with tempfile.TemporaryDirectory() as td:
        home, flow, review_dir = approved_flow(td, ENTRIES_D3_APPROVED_D5_DECLINED, ledger_step="assess")
        repo, base, approval, head = build_scenario(td)
        write_check_cache(repo)
        rc, out, err = run_fix(flow, home, repo, base, review_dir,
                               fix_receipt(head, test_edit=TEST_EDIT_D3, test_edit_commit=approval,
                                           outstanding=[DECLINED_D5]))
        h.test_result("fix TEST_EDIT ref: a step-assess ledger record never satisfies a fix answers ref",
                      rc != 0 and not passed_fix(rc, out) and "answers/fix-02.json" in combined(out, err),
                      combined(out, err))

    print()
    print("[Apply conflict]")
    with tempfile.TemporaryDirectory() as td:
        repo, base, approval, head = build_scenario(td)
        write_check_cache(repo)
        home, flow, review_dir = approved_flow(td, [("q1", "D3", "tests/test_x.py", True),
                                                    ("q2", "D5", "tests/test_z.py", True)])
        rc, out, err = run_fix(flow, home, repo, base, review_dir,
                               fix_receipt(head, test_edit=TEST_EDIT_D3, test_edit_commit=approval,
                                           outstanding=["D5 (test-edit-apply-conflict: tests/test_z.py) [self-report]"]))
        h.test_result("apply conflict: D3 applied and D5 reported as apply-conflict passes",
                      passed_fix(rc, out), combined(out, err))
        rc2, out2, err2 = outstanding_set(flow, home)
        _count, rows = parse_outstanding(out2)
        h.test_result("apply conflict: outstanding-set lists fix:D5", rc2 == 0 and "fix:D5" in rows,
                      combined(out2, err2))

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
