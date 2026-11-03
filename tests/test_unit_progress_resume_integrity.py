#!/usr/bin/env python3
"""Resume-integrity contract for scripts/unit-progress.py, exercised through its CLI only.

Covers: the ancestor rule for `check` (later commits on top of the chain tip are accepted, a
committed SHA that is not reachable from HEAD is rejected), out-of-declared-order completion in
parallel mode, the chain tip for sequential runs, a distinct exit code for "no record" versus an
integrity failure, and the `mark` guards (no units.json, SHA only for committed, SHA must be an
existing commit).

Every scenario runs against a real throwaway git repository created in a temp directory.

Run with: python3 tests/test_unit_progress_resume_integrity.py
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

PROGRESS = REPO_ROOT / "scripts" / "unit-progress.py"
GIT_ID = ["-c", "user.email=test@example.com", "-c", "user.name=Test", "-c", "commit.gpgsign=false"]
SIZING = {
    "schema_version": 1,
    "kind": "plan",
    "steps": 2,
    "files": 2,
    "findings": None,
    "ceilings": {"steps": 8, "files": 12, "findings": 16},
    "min_units": 1,
    "status": "ok",
    "reason": "",
}
MISSING_SHA = "f" * 40


def git(repo, *args):
    result = subprocess.run(["git", *GIT_ID, *args], cwd=str(repo), capture_output=True, text=True, check=True)
    return result.stdout.strip()


def make_history(root):
    """Linear history of three commits on main. Returns (repo, [c0, c1, c2])."""
    repo = Path(root) / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    shas = []
    for i in range(3):
        (repo / f"f{i}.txt").write_text(f"{i}\n")
        git(repo, "add", f"f{i}.txt")
        git(repo, "commit", "-q", "-m", f"c{i}")
        shas.append(git(repo, "rev-parse", "HEAD"))
    return repo, shas


def make_side_commit(repo, base):
    """Create a commit on a side branch off `base` and return to main. Not an ancestor of main's HEAD."""
    git(repo, "checkout", "-q", "-b", "side", base)
    (repo / "side.txt").write_text("side\n")
    git(repo, "add", "side.txt")
    git(repo, "commit", "-q", "-m", "side")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    return sha


def unit(uid):
    return {"unit_id": uid, "sub_task": f"task {uid}", "owned_files": [f"{uid}.py"]}


def payload(basis, units):
    return json.dumps({"units": units, "sizing": SIZING, "basis": basis, "single_reason": None})


def run(args, scratch, cwd, stdin=None):
    cmd = [sys.executable, str(PROGRESS), "--step", "implement", "--scratch", str(scratch), *args]
    result = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, input=stdin)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def init(scratch, cwd, plan, anchor, basis, uids):
    units = [unit(u) for u in uids]
    return run(["init", "--anchor", anchor, "--base", anchor, "--plan", str(plan)], scratch, cwd,
               stdin=payload(basis, units))


def check(scratch, cwd, plan, anchor, head):
    return run(["check", "--plan", str(plan), "--anchor", anchor, "--head", head], scratch, cwd)


def units_path(scratch):
    return Path(scratch) / "units" / "implement" / "units.json"


def main():
    h = Harness("UNIT PROGRESS RESUME INTEGRITY")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo, (c0, c1, c2) = make_history(root / "a")
        plan = root / "plan.md"
        plan.write_text("# plan\n")

        print("[Ancestor rule] later commits on top of the chain tip are accepted")
        s_seq = root / "seq"
        code, _, err = init(s_seq, repo, plan, c0, "sequential", ["u1", "u2"])
        h.test_result("sequential init succeeds", code == 0, err)
        code, _, err = run(["mark", "--unit-id", "u1", "--status", "committed", "--sha", c1], s_seq, repo)
        h.test_result("mark u1 committed at c1", code == 0, err)
        code, _, err = check(s_seq, repo, plan, c0, c1)
        h.test_result("check passes with HEAD == chain tip", code == 0, err)
        code, _, err = check(s_seq, repo, plan, c0, c2)
        h.test_result("check passes when HEAD has advanced past the tip (later commit on top)", code == 0, err)

        print("[Ancestor rule] a committed SHA not reachable from HEAD is rejected")
        side = make_side_commit(repo, c0)
        s_bad = root / "bad"
        init(s_bad, repo, plan, c0, "sequential", ["u1", "u2"])
        mark_code, _, mark_err = run(["mark", "--unit-id", "u1", "--status", "committed", "--sha", side], s_bad, repo)
        if mark_code != 0:
            print(f"  (setup note: mark refused the side commit: {mark_err!r}; check is still exercised below)")
        code, _, err = check(s_bad, repo, plan, c0, c2)
        h.test_result("check rejects a committed SHA not reachable from HEAD",
                      code != 0 and "not reachable" in err, f"rc={code} err={err!r}")
        integrity_rc = code

        print("[Ancestor rule] parallel completion in non-declared order still passes")
        s_par = root / "par"
        init(s_par, repo, plan, c0, "parallel", ["u1", "u2"])
        code, _, err = run(["mark", "--unit-id", "u2", "--status", "committed", "--sha", c2], s_par, repo)
        h.test_result("parallel: u2 completes first at c2", code == 0, err)
        code, _, err = run(["mark", "--unit-id", "u1", "--status", "committed", "--sha", c1], s_par, repo)
        h.test_result("parallel: u1 completes second at c1", code == 0, err)
        code, _, err = check(s_par, repo, plan, c0, c2)
        h.test_result("parallel: check passes with HEAD at the latest completion", code == 0, err)

        print("[Chain tip] sequential tip is the last committed unit's SHA")
        code, out, err = run(["tip"], s_seq, repo)
        h.test_result("sequential tip prints c1 after u1 commits", code == 0 and out == c1, f"rc={code} out={out!r} err={err!r}")

        print("[No record] distinct exit code, separate from integrity failure")
        s_none = root / "none"
        s_none.mkdir()
        code, _, err = check(s_none, repo, plan, c0, c0)
        h.test_result("check with no record exits non-zero", code != 0, f"rc={code}")
        h.test_result("check with no record reports 'no record'", "no record" in err.lower(), err)
        h.test_result("no-record exit code differs from integrity-failure exit code",
                      code != integrity_rc, f"no-record rc={code} integrity rc={integrity_rc}")
        code, _, err = run(["pending"], s_none, repo)
        h.test_result("pending with no record exits non-zero", code != 0, f"rc={code} err={err!r}")

        print("[Mark guards] mark requires an existing units.json")
        s_gone = root / "gone"
        init(s_gone, repo, plan, c0, "sequential", ["u1", "u2"])
        units_path(s_gone).unlink()
        code, _, err = run(["mark", "--unit-id", "u1", "--status", "committed", "--sha", c1], s_gone, repo)
        h.test_result("mark committed without units.json fails", code != 0, f"rc={code} err={err!r}")
        code, _, err = run(["mark", "--unit-id", "u1", "--status", "failed"], s_gone, repo)
        h.test_result("mark failed without units.json fails", code != 0, f"rc={code} err={err!r}")

        print("[Mark guards] SHA validation")
        s_sha = root / "sha"
        init(s_sha, repo, plan, c0, "sequential", ["u1", "u2"])
        code, _, err = run(["mark", "--unit-id", "u1", "--status", "committed", "--sha", "not-a-sha"], s_sha, repo)
        h.test_result("mark committed with a non-hex SHA is rejected", code != 0, f"rc={code} err={err!r}")
        code, _, err = run(["mark", "--unit-id", "u1", "--status", "committed", "--sha", MISSING_SHA], s_sha, repo)
        h.test_result("mark committed with a well-formed SHA that is not a commit is rejected",
                      code != 0, f"rc={code} err={err!r}")
        s_failsha = root / "failsha"
        init(s_failsha, repo, plan, c0, "sequential", ["u1", "u2"])
        code, _, err = run(["mark", "--unit-id", "u1", "--status", "failed", "--sha", c1], s_failsha, repo)
        h.test_result("mark failed with --sha is rejected (SHA only for committed)", code != 0, f"rc={code} err={err!r}")
        code, _, err = run(["mark", "--unit-id", "u1", "--status", "committed", "--sha", c1], s_sha, repo)
        h.test_result("control: mark committed with a real SHA succeeds", code == 0, err)

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
