#!/usr/bin/env python3
"""
Black-box tests for the heal rules of scripts/flow-verify-receipt.py (#380):
`--step heal`, the heal-aware `--step verify`, and the heal-aware `--step merge`.

Driven only through the CLI (subprocess). Every case builds a throwaway HOME holding a flow dir
under ~/.claude/flows/ (the verifier only accepts flow dirs there) plus a throwaway repo whose
history is: C0 (bases[implement]) -> C1 (implement work = sha_before) -> heal/fix commits.

Record shapes follow the shared contract: one `HEAL:` line of key=value tokens, `SCAN:` and
`COMPLEXITY:` lines, a healer receipt (check heals), a verify receipt with `CHECK_SHA:`/`HEALED:`,
a coverage table (`<id> | <class> | ...`) and an outstanding file for Fix now.

Each failing case is a one-field mutation of a passing case, and asserts the rejection twice: the
exit code, and (separately) that the Reason names the offending thing.

Run with: python3 tests/test_flow_verify_receipt_heal.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT
from _inproc import run_python, RepoTemplate

VERIFIER = REPO_ROOT / "scripts" / "flow-verify-receipt.py"
SINCE = "2026-10-13T00:00:00Z"
GIT_ID = ["-c", "user.email=test@example.com", "-c", "user.name=Test", "-c", "commit.gpgsign=false",
          "-c", "core.hooksPath=/dev/null"]
SIG = "a" * 64
TOOLS = "b" * 64


def run_git(repo, *args):
    result = subprocess.run(["git", *GIT_ID, *args], cwd=str(repo), capture_output=True, text=True)
    return result.stdout.strip()


def commit(repo, name, text, message):
    path = Path(repo) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", message)
    return run_git(repo, "rev-parse", "HEAD")


def _build_heal_base(root):
    """Build the base repo with C0 -> C1 for template."""
    run_git(root, "init", "-q")
    c0 = commit(root, "a.py", "x = 1\n", "base")
    c1 = commit(root, "a.py", "import os\nx = 1\n", "implement work")
    return c0, c1


_HEAL_BASE = RepoTemplate(_build_heal_base)


class World:
    """A HOME, a flow dir, and a repo with C0 -> C1 already committed."""

    def __init__(self, tmp):
        self.home = Path(tmp) / "home"
        self.flow = self.home / ".claude" / "flows" / "repo" / "12-slug-abc123"
        (self.flow / "steps").mkdir(parents=True)
        self.repo = Path(tmp) / "repo"
        _HEAL_BASE.copy(self.repo)
        self.c0, self.c1 = _HEAL_BASE.meta
        self.write_state()

    def write_state(self, gate_passed=False, healed=None):
        state = {"issue": 12, "slug": "slug", "step": "merge", "status": "running", "attempts": {}, "failures": {},
                 "gate_passed": gate_passed, "bases": {"implement": self.c0, "fix": self.c0}}
        if healed is not None:
            state["healed"] = healed
        (self.flow / "state.json").write_text(json.dumps(state))

    def step_file(self, name, text):
        path = self.flow / "steps" / name
        path.write_text(text)
        return path

    def heal_commit(self, parent=None, trailer="Flow-Heal: verify-1", filename="a.py", text="x = 1\n"):
        """Commit a heal on top of HEAD (or on a fresh branch off `parent`). Returns its sha."""
        if parent is not None:
            run_git(self.repo, "checkout", "-q", "-b", f"alt-{parent[:7]}", parent)
        message = "heal" + (f"\n\n{trailer}" if trailer else "")
        return commit(self.repo, filename, text, message)

    def run(self, step, receipt, extra=()):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        env["HOME"] = str(self.home)
        env["FLOW_VERIFY_GIT_TIMEOUT_SECS"] = "60"
        result = run_python([str(VERIFIER), "--step", step, "--receipt", str(receipt), "--since", SINCE,
                             "--flow-dir", str(self.flow), "--repo", str(self.repo), "--branch", "HEAD", *extra], env=env)
        return result.returncode, (result.stdout + result.stderr).strip()

    def run_e2e(self, step, receipt, extra=()):
        """End-to-end subprocess call for CLI coverage."""
        cmd = [sys.executable, str(VERIFIER), "--step", step, "--receipt", str(receipt), "--since", SINCE,
               "--flow-dir", str(self.flow), "--repo", str(self.repo), "--branch", "HEAD", *extra]
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        env["HOME"] = str(self.home)
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        return result.returncode, (result.stdout + result.stderr).strip()


def check_heal_line(w, sha_after_value, **overrides):
    fields = {"id": "verify-1", "source": "check", "state": "converged", "sha_before": w.c1, "sha_after": sha_after_value,
              "signature": SIG, "class": "lint", "tools_sha": TOOLS, "review": "required", "reasons": "counts-missing"}
    fields.update(overrides)
    tokens = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    return f"HEAL: {tokens}\nSCAN: heal pass -\nCOMPLEXITY: required counts-missing\n"


def healer_receipt(sha_after, heal_id="verify-1"):
    return (f"HEAL_ID: {heal_id}\nHEAL_SHA_AFTER: {sha_after}\nHEALED\nRemoved the unused import.\n"
            "<!-- step-end -->\n")


def verify_receipt(check_sha, healed=None):
    lines = ["RESULT: OK", f"CHECK_SHA: {check_sha}"]
    if healed:
        lines.append(f"HEALED: {healed}")
    return "\n".join(lines) + "\n<!-- step-end -->\n"


def build_check_world(tmp, *, record_overrides=None, heal_commits=1, amend=False, trailer="Flow-Heal: verify-1",
                      receipt_check_sha="AFTER", healed_marker="verify-1", with_healer_receipt=True,
                      write_record=True):
    """A converged check-heal scenario. Returns (world, heal_record_path, verify_receipt_path, sha_after)."""
    w = World(tmp)
    sha_after = None
    if amend:
        # one commit in the range, but its parent is C0 rather than sha_before
        sha_after = w.heal_commit(parent=w.c0, trailer=trailer, filename="amended.py", text="y = 1\n")
    else:
        for i in range(heal_commits):
            sha_after = w.heal_commit(trailer=trailer, filename="a.py", text=f"x = 1\n# {i}\n" if i else "x = 1\n")
    overrides = dict(record_overrides or {})
    record = w.step_file("04-verify.heal.md", check_heal_line(w, sha_after, **overrides)) if write_record else None
    if with_healer_receipt:
        w.step_file("04-verify.heal-1.md", healer_receipt(sha_after))
    check_sha = sha_after if receipt_check_sha == "AFTER" else receipt_check_sha
    receipt = w.step_file("04-verify.md", verify_receipt(check_sha, healed=healed_marker))
    return w, record, receipt, sha_after


def build_fixnow_world(tmp, *, record_overrides=None, fixed_unit_still_uncovered=False, verify_sha="AFTER",
                       ancestor_ok=True):
    """A converged Fix now scenario: three ordinary commits, no Flow-Heal trailer. Returns (world, record, receipt)."""
    w = World(tmp)
    commit(w.repo, "b.py", "b = 1\n", "unit one")
    commit(w.repo, "c.py", "c = 1\n", "unit two")
    sha_after = commit(w.repo, "d.py", "d = 1\n", "tests")
    sha_before = w.c1 if ancestor_ok else sha_after
    sha_to_record = sha_after if ancestor_ok else w.c1  # not-ancestor: sha_before is AFTER sha_after
    fields = {"id": "fix-now-1", "source": "fix-now", "state": "converged", "sha_before": sha_before,
              "sha_after": sha_to_record, "units": "u1,u2", "review": "required", "reasons": "counts-missing",
              "fixed": "u1", "outstanding": "u2"}
    fields.update(record_overrides or {})
    tokens = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    record = w.step_file("07-ship.fixnow.md", f"HEAL: {tokens}\nSCAN: fix-now pass -\nCOMPLEXITY: required counts-missing\n")
    w.step_file("06-fix.md", "ROUND 1\nOUTSTANDING: u2 (needs-human: needs a product call) [self-report]\n"
                             "Decision: OK\n<!-- step-end -->\n")
    rows = ["u1 | covered | b.py", "u2 | declared | needs a product call"]
    if fixed_unit_still_uncovered:
        rows[0] = "u1 | uncovered | not in diff"
    w.step_file("06-fix.coverage.txt", "\n".join(rows) + "\n")
    w.step_file("07-ship.outstanding.md", "u1: fix-now: fixed\nu2: fix-now: needs-human -> dropped\n"
                                          "<!-- outstanding-end -->\n")
    check_sha = sha_after if verify_sha == "AFTER" else verify_sha
    w.step_file("06-verify.md", verify_receipt(check_sha))
    return w, record, w.flow / "steps" / "06-verify.md"


def results(h, label, rc, out, expect_pass, keyword=None):
    if expect_pass:
        h.test_result(f"{label}: accepted (exit 0, ARTIFACTS: ok)", rc == 0 and "ARTIFACTS: ok" in out, f"rc={rc} out={out[:400]!r}")
        return
    h.test_result(f"{label}: rejected (exit 1)", rc == 1, f"rc={rc} out={out[:400]!r}")
    if keyword is not None:
        h.test_result(f"{label}: Reason mentions '{keyword}'", keyword.lower() in out.lower(), f"out={out[:400]!r}")


def test_check_heals(h):
    print("[Section 1] --step heal: source=check")
    with tempfile.TemporaryDirectory() as tmp:
        w, record, receipt, _ = build_check_world(tmp)
        rc, out = w.run_e2e("heal", record)
        results(h, "converged check heal, one Flow-Heal commit, healer receipt present", rc, out, True)
        rc, out = w.run("verify", receipt)
        results(h, "verify after the heal: HEALED + CHECK_SHA == sha_after", rc, out, True)

    with tempfile.TemporaryDirectory() as tmp:
        w, record, receipt, _ = build_check_world(tmp, heal_commits=2)
        rc, out = w.run("heal", record)
        results(h, "two commits in the range", rc, out, False, "commit")

    with tempfile.TemporaryDirectory() as tmp:
        w, record, receipt, _ = build_check_world(tmp, amend=True)
        rc, out = w.run("heal", record)
        results(h, "amended commit (one commit, parent is not sha_before)", rc, out, False, "parent")

    with tempfile.TemporaryDirectory() as tmp:
        w, record, receipt, _ = build_check_world(tmp, trailer=None)
        rc, out = w.run("heal", record)
        results(h, "heal commit without the Flow-Heal trailer", rc, out, False, "Flow-Heal")

    with tempfile.TemporaryDirectory() as tmp:
        w, record, receipt, _ = build_check_world(tmp, trailer="Flow-Heal: verify-9")
        rc, out = w.run("heal", record)
        results(h, "heal commit with a trailer naming a different heal id", rc, out, False, "Flow-Heal")

    for missing in ("signature", "tools_sha", "class", "sha_before"):
        with tempfile.TemporaryDirectory() as tmp:
            w, record, receipt, _ = build_check_world(tmp, record_overrides={missing: None})
            rc, out = w.run("heal", record)
            results(h, f"record missing '{missing}'", rc, out, False, missing)

    with tempfile.TemporaryDirectory() as tmp:
        w, record, receipt, _ = build_check_world(tmp, with_healer_receipt=False)
        rc, out = w.run("heal", record)
        results(h, "healer receipt absent", rc, out, False, "receipt")

    with tempfile.TemporaryDirectory() as tmp:
        w, record, receipt, sha_after = build_check_world(tmp, record_overrides={"state": "rejected:test-edit", "sha_after": None,
                                                                                 "review": None, "reasons": None})
        # For a rejected state, the verify receipt's CHECK_SHA should match sha_before (no healing occurred)
        w.step_file("04-verify.md", verify_receipt(w.c1))
        rc, out = w.run("heal", record)
        # A rejected record needs no converged-only fields; the contract only requires the common + check fields.
        h.test_result("rejected:* check record with its common fields is not rejected for converged-only fields",
                      rc == 0 and "ARTIFACTS: ok" in out, f"rc={rc} out={out[:300]!r}")

    print()
    print("[Section 2] --step verify: heal-aware checks")
    with tempfile.TemporaryDirectory() as tmp:
        w, record, receipt, _ = build_check_world(tmp, receipt_check_sha="STALE")
        w.step_file("04-verify.md", verify_receipt(w.c1, healed="verify-1"))
        rc, out = w.run("verify", w.flow / "steps" / "04-verify.md")
        results(h, "post-heal verify with CHECK_SHA == sha_before (stale)", rc, out, False, "CHECK_SHA")

    with tempfile.TemporaryDirectory() as tmp:
        w, record, receipt, _ = build_check_world(tmp, healed_marker=None)
        rc, out = w.run("verify", receipt)
        results(h, "converged check record but the verify receipt has no HEALED:", rc, out, False, "HEALED")

    with tempfile.TemporaryDirectory() as tmp:
        w, record, receipt, _ = build_check_world(tmp, healed_marker="verify-7")
        rc, out = w.run("verify", receipt)
        results(h, "verify receipt HEALED: names a different heal id", rc, out, False, "HEALED")

    with tempfile.TemporaryDirectory() as tmp:
        w, record, receipt, _ = build_check_world(tmp, write_record=False, healed_marker=None)
        rc, out = w.run("verify", receipt)
        results(h, "a Flow-Heal: commit in range with no HEAL: record", rc, out, False, "Flow-Heal")

    with tempfile.TemporaryDirectory() as tmp:
        w = World(tmp)
        receipt = w.step_file("04-verify.md", verify_receipt(w.c1))
        rc, out = w.run("verify", receipt)
        results(h, "control: a verify with no heal anywhere", rc, out, True)


def test_fix_now(h):
    print()
    print("[Section 3] --step heal: source=fix-now")
    with tempfile.TemporaryDirectory() as tmp:
        w, record, receipt = build_fixnow_world(tmp)
        rc, out = w.run("heal", record)
        results(h, "converged Fix now (units, no signature, no trailer, multi-commit range)", rc, out, True)

    with tempfile.TemporaryDirectory() as tmp:
        w, record, receipt = build_fixnow_world(tmp, fixed_unit_still_uncovered=True)
        rc, out = w.run("heal", record)
        results(h, "a unit marked 'fix-now: fixed' still in the re-run's uncovered rows", rc, out, False, "u1")

    with tempfile.TemporaryDirectory() as tmp:
        w, record, receipt = build_fixnow_world(tmp, verify_sha="0" * 40)
        rc, out = w.run("heal", record)
        results(h, "sha_after != the following verify's CHECK_SHA", rc, out, False, "CHECK_SHA")

    with tempfile.TemporaryDirectory() as tmp:
        w, record, receipt = build_fixnow_world(tmp, record_overrides={"units": None})
        rc, out = w.run("heal", record)
        results(h, "record missing 'units'", rc, out, False, "units")

    with tempfile.TemporaryDirectory() as tmp:
        w, record, receipt = build_fixnow_world(tmp, ancestor_ok=False)
        rc, out = w.run("heal", record)
        results(h, "sha_before is not an ancestor of sha_after", rc, out, False, "ancestor")

    print()
    print("[Section 4] --step heal: malformed records fail closed")
    with tempfile.TemporaryDirectory() as tmp:
        w = World(tmp)
        for label, text, kw in (
            ("a malformed HEAL: line", "HEAL: this is not key value tokens\n", None),
            ("an empty HEAL: line", "HEAL:\n", None),
            ("an unknown source", f"HEAL: id=x-1 source=bogus state=converged sha_before={w.c1}\n", "source"),
            ("no HEAL: line at all", "SCAN: heal pass -\n", None),
        ):
            record = w.step_file("04-verify.heal.md", text)
            rc, out = w.run("heal", record)
            results(h, label, rc, out, False, kw)


def test_merge(h):
    print()
    print("[Section 5] --step merge: a healed run needs a human gate_passed")
    with tempfile.TemporaryDirectory() as tmp:
        w = World(tmp)
        receipt = w.step_file("09-merge.md", "MERGED\n<!-- step-end -->\n")
        rc, out = w.run("merge", receipt)
        results(h, "control: merge with no heal and no gate", rc, out, True)

    with tempfile.TemporaryDirectory() as tmp:
        w, record, _, _ = build_check_world(tmp)
        receipt = w.step_file("09-merge.md", "MERGED\n<!-- step-end -->\n")
        rc, out = w.run("merge", receipt)
        results(h, "(a) converged heal record, gate_passed false", rc, out, False, "gate")
        w.write_state(gate_passed=True, healed=True)
        rc, out = w.run("merge", receipt)
        results(h, "(a) converged heal record, human gate_passed true", rc, out, True)

    with tempfile.TemporaryDirectory() as tmp:
        w, record, _, sha_after = build_check_world(
            tmp, record_overrides={"state": "rejected:test-edit", "reasons": None, "review": None})
        receipt = w.step_file("09-merge.md", "MERGED\n<!-- step-end -->\n")
        rc, out = w.run("merge", receipt)
        results(h, "(b) only a rejected:* check record, its Flow-Heal commit still in range", rc, out, False, "gate")

    with tempfile.TemporaryDirectory() as tmp:
        w, record, receipt = build_fixnow_world(tmp)
        receipt = w.step_file("09-merge.md", "MERGED\n<!-- step-end -->\n")
        rc, out = w.run("merge", receipt)
        results(h, "(c) only a source=fix-now record", rc, out, False, "gate")

    with tempfile.TemporaryDirectory() as tmp:
        w = World(tmp)
        w.write_state(healed=True)
        receipt = w.step_file("09-merge.md", "MERGED\n<!-- step-end -->\n")
        rc, out = w.run("merge", receipt)
        results(h, "state.json healed: true alone (no record, no commit)", rc, out, False, "gate")

    with tempfile.TemporaryDirectory() as tmp:
        w = World(tmp)
        w.heal_commit()
        receipt = w.step_file("09-merge.md", "MERGED\n<!-- step-end -->\n")
        rc, out = w.run("merge", receipt)
        results(h, "a Flow-Heal: commit alone (no record, healed unset)", rc, out, False, "gate")

    with tempfile.TemporaryDirectory() as tmp:
        w = World(tmp)
        w.step_file("04-verify.heal.md", "HEAL: id=verify-1 source=check state=interrupted sha_before=" + w.c1
                    + f" signature={SIG} class=lint\n")
        receipt = w.step_file("09-merge.md", "MERGED\n<!-- step-end -->\n")
        rc, out = w.run("merge", receipt)
        results(h, "an interrupted HEAL: record (any state counts)", rc, out, False, "gate")


def test_verify_merge_with_repo_branch(h):
    print()
    print("[Section 6] --step verify/merge with --repo/--branch supplied (directive #1)")
    with tempfile.TemporaryDirectory() as tmp:
        w, record, receipt, _ = build_check_world(tmp)
        # Test verify with --repo and --branch explicitly provided
        cmd = [sys.executable, str(VERIFIER), "--step", "verify", "--receipt", str(receipt),
               "--since", SINCE, "--flow-dir", str(w.flow), "--repo", str(w.repo), "--branch", "HEAD"]
        env = os.environ.copy()
        env["HOME"] = str(w.home)
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        rc, out = result.returncode, (result.stdout + result.stderr).strip()
        results(h, "verify with --repo and --branch supplied", rc, out, True)

    with tempfile.TemporaryDirectory() as tmp:
        w, record, receipt, _ = build_check_world(tmp)
        w.write_state(gate_passed=True, healed=True)
        receipt = w.step_file("09-merge.md", "MERGED\n<!-- step-end -->\n")
        # Test merge with --repo and --branch explicitly provided
        cmd = [sys.executable, str(VERIFIER), "--step", "merge", "--receipt", str(receipt),
               "--since", SINCE, "--flow-dir", str(w.flow), "--repo", str(w.repo), "--branch", "HEAD"]
        env = os.environ.copy()
        env["HOME"] = str(w.home)
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        rc, out = result.returncode, (result.stdout + result.stderr).strip()
        results(h, "merge with --repo and --branch supplied (gate_passed=true)", rc, out, True)


def main():
    h = Harness("FLOW VERIFY RECEIPT HEAL TEST SUITE")
    test_check_heals(h)
    test_fix_now(h)
    test_merge(h)
    test_verify_merge_with_repo_branch(h)
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
