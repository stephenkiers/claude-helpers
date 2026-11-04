#!/usr/bin/env python3
"""Hardening tests for scripts/unit-progress.py: sequential ordering, scope naming, scoped chain check.

Run with: python3 tests/test_unit_progress_hardening.py
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT
from test_unit_progress import create_minimal_sizing_result, create_unit_list, run_progress_cmd

SHA = "a" * 40


def init(scratch, basis, count, scope=None):
    payload = json.dumps({"units": create_unit_list(basis, count), "sizing": create_minimal_sizing_result(),
                          "basis": basis, "single_reason": None})
    args = (["--scope", scope] if scope else []) + ["init", "--supersede", "--anchor", SHA, "--base", SHA]
    return run_progress_cmd(args, stdin_data=payload, scratch=scratch)


def main():
    h = Harness("UNIT PROGRESS HARDENING")
    with tempfile.TemporaryDirectory() as tmp:
        # mark --sha requires a real commit in the cwd repository
        real = Path(tmp) / "real"
        real.mkdir()
        for c in (["init", "-q"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "c"]):
            subprocess.run(["git", *c], cwd=real, check=True, capture_output=True)
        REAL = subprocess.run(["git", "rev-parse", "HEAD"], cwd=real, capture_output=True, text=True).stdout.strip()
        scratch = str(Path(tmp) / "s1")
        code, _, err = init(scratch, "sequential", 3)
        h.test_result("sequential init ok", code == 0, err)

        print("[Finding 5] sequential order is enforced by mark")
        code, _, err = run_progress_cmd(["mark", "--unit-id", "u2", "--status", "committed", "--sha", REAL], scratch=scratch, cwd=str(real))
        h.test_result("u2 cannot commit before u1", code != 0 and "sequential order" in err, f"{code} {err}")
        code, _, err = run_progress_cmd(["mark", "--unit-id", "u1", "--status", "committed", "--sha", REAL], scratch=scratch, cwd=str(real))
        h.test_result("u1 commits first", code == 0, err)
        code, _, err = run_progress_cmd(["mark", "--unit-id", "u2", "--status", "committed", "--sha", REAL], scratch=scratch, cwd=str(real))
        h.test_result("u2 commits after u1", code == 0, err)
        code, _, err = run_progress_cmd(["mark", "--unit-id", "u3", "--status", "failed"], scratch=scratch)
        h.test_result("u3 can fail after prefix committed", code == 0, err)

        s2 = str(Path(tmp) / "s2")
        init(s2, "parallel", 2)
        code, _, err = run_progress_cmd(["mark", "--unit-id", "u2", "--status", "committed", "--sha", REAL], scratch=s2, cwd=str(real))
        h.test_result("parallel mode has no ordering constraint", code == 0, err)

        print("[Finding 7] scope names are exact and validated")
        for bad in ("../x", "scoped-a", "scoped-ascoped-a2", "a2", "scoped-a1/../x"):
            code, _, err = run_progress_cmd(["--scope", bad, "pending"], scratch=s2)
            h.test_result(f"scope {bad!r} refused", code != 0 and "--scope" in err, f"{code} {err}")
        code, _, err = init(s2, "parallel", 2, scope="scoped-a2")
        h.test_result("scoped-a2 init ok", code == 0, err)
        h.test_result("scoped dir is units/implement/scoped-a2 (no double prefix)",
                      (Path(s2) / "units" / "implement" / "scoped-a2" / "units.json").exists())

        print("[Finding 8] scoped check validates the chain")
        plan = Path(tmp) / "plan.md"
        plan.write_text("plan")
        repo = Path(tmp) / "repo"
        repo.mkdir()
        for c in (["init", "-q"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"]):
            subprocess.run(["git", *c], cwd=repo, check=True, capture_output=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
        s3 = str(Path(tmp) / "s3")
        def payload():
            return json.dumps({"units": create_unit_list("parallel", 2), "sizing": create_minimal_sizing_result(),
                               "basis": "parallel", "single_reason": None})
        for sc in (None, "scoped-a1"):
            args = (["--scope", sc] if sc else []) + ["init", "--supersede", "--plan", str(plan), "--anchor", head, "--base", head]
            run_progress_cmd(args, stdin_data=payload(), scratch=s3)

        def check(scope):
            cmd = [sys.executable, str(REPO_ROOT / "scripts" / "unit-progress.py"), "--step", "implement", "--scratch", s3,
                   "--scope", scope, "check", "--plan", str(plan), "--anchor", head, "--head", head]
            r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
            return r.returncode, r.stderr
        code, err = check("scoped-a1")
        h.test_result("valid scoped chain passes", code == 0, err)
        sc_units = Path(s3) / "units" / "implement" / "scoped-a1" / "units.json"
        d = json.loads(sc_units.read_text())
        d["anchor_sha"] = "b" * 40
        sc_units.write_text(json.dumps(d))
        code, err = check("scoped-a1")
        h.test_result("scoped anchor mismatch rejected", code != 0, err)
        d["anchor_sha"] = head
        d["base_sha"] = "c" * 40
        sc_units.write_text(json.dumps(d))
        code, err = check("scoped-a1")
        h.test_result("scoped base not on HEAD history rejected", code != 0 and "ancestor" in err, err)
        (Path(s3) / "units" / "implement" / "units.json").unlink()
        code, err = check("scoped-a1")
        h.test_result("scoped record without main record rejected", code != 0, err)
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
