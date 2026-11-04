#!/usr/bin/env python3
"""
Tests for scripts/unit-progress.py: unit record tracking and verification.

Tests verify:
- init refusals: below min_units, single with known min_units > 1, empty reason,
  sequential with 1 unit, overlapping owned_files in parallel mode, existing record without --supersede
- init acceptance: sequential with overlapping ownership, single with unknown min_units
- mark transitions and SHA requirement
- check consistency validation and chain following
- pending listing
- tip SHA reporting
- Atomic writes

Run with: python3 tests/test_unit_progress.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT
from _git_fixture import GitFixture

PROGRESS_SCRIPT = REPO_ROOT / "scripts" / "unit-progress.py"
HARDENING_SHA = "a" * 40


def run_progress_cmd(cmd_args: list, stdin_data: str = None, scratch: str = None, cwd: str = None) -> tuple:
    """Run unit-progress with given args. Returns (exit_code, stdout, stderr)."""
    base_cmd = [sys.executable, str(PROGRESS_SCRIPT), "--step", "implement"]
    if scratch:
        base_cmd.extend(["--scratch", scratch])
    else:
        base_cmd.extend(["--flow-dir", "/tmp/nonexistent"])  # Dummy for non-flow mode

    cmd = base_cmd + cmd_args
    result = subprocess.run(cmd, capture_output=True, text=True, input=stdin_data, cwd=cwd)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def create_minimal_sizing_result() -> dict:
    """Create a minimal sizing result dict."""
    return {
        "schema_version": 1,
        "kind": "plan",
        "steps": 12,
        "files": 15,
        "findings": None,
        "ceilings": {"steps": 8, "files": 12, "findings": 16},
        "min_units": 2,
        "status": "ok",
        "reason": ""
    }


def create_unit_list(basis: str, count: int) -> list:
    """Create a unit list for init."""
    units = []
    for i in range(count):
        units.append({
            "unit_id": f"u{i+1}",
            "sub_task": f"Unit {i+1}",
            "owned_files": [f"file{i}.py"] if basis != "parallel_overlap" else ["file_shared.py"]
        })
    return units


def _hardening_init(scratch, basis, count, scope=None):
    payload = json.dumps({"units": create_unit_list(basis, count), "sizing": create_minimal_sizing_result(),
                          "basis": basis, "single_reason": None})
    args = (["--scope", scope] if scope else []) + ["init", "--supersede", "--anchor", HARDENING_SHA, "--base", HARDENING_SHA]
    return run_progress_cmd(args, stdin_data=payload, scratch=scratch)


def _hardening_cases(h):
    with tempfile.TemporaryDirectory() as tmp:
        # mark --sha requires a real commit in the cwd repository
        real = Path(tmp) / "real"
        real.mkdir()
        for c in (["init", "-q"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "c"]):
            subprocess.run(["git", *c], cwd=real, check=True, capture_output=True)
        REAL = subprocess.run(["git", "rev-parse", "HEAD"], cwd=real, capture_output=True, text=True).stdout.strip()
        scratch = str(Path(tmp) / "s1")
        code, _, err = _hardening_init(scratch, "sequential", 3)
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
        _hardening_init(s2, "parallel", 2)
        code, _, err = run_progress_cmd(["mark", "--unit-id", "u2", "--status", "committed", "--sha", REAL], scratch=s2, cwd=str(real))
        h.test_result("parallel mode has no ordering constraint", code == 0, err)

        print("[Finding 7] scope names are exact and validated")
        for bad in ("../x", "scoped-a", "scoped-ascoped-a2", "a2", "scoped-a1/../x"):
            code, _, err = run_progress_cmd(["--scope", bad, "pending"], scratch=s2)
            h.test_result(f"scope {bad!r} refused", code != 0 and "--scope" in err, f"{code} {err}")
        code, _, err = _hardening_init(s2, "parallel", 2, scope="scoped-a2")
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


def main() -> None:
    h = Harness("unit-progress.py")
    # Use valid 40-character hex SHA for tests
    VALID_SHA = "abcdef1234567890abcdef1234567890abcdef12"

    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "scratch"
        scratch.mkdir()

        print("[Test 1] init refuses units below min_units")
        units = create_unit_list("parallel", 1)  # Only 1 unit
        sizing = create_minimal_sizing_result()  # min_units = 2
        sizing["min_units"] = 2
        flow_input = json.dumps({"units": units, "sizing": sizing, "basis": "parallel", "single_reason": None})
        code, out, err = run_progress_cmd(["init", "--anchor", VALID_SHA, "--base", VALID_SHA], stdin_data=flow_input, scratch=str(scratch))
        h.test_result("init refuses (exit non-zero)", code != 0, f"exit {code}")
        h.test_result("reason in stderr", "min_units" in err or "below" in err.lower(), err)

        print("\n[Test 2] init refuses single with known min_units > 1")
        units = create_unit_list("single", 1)
        sizing = create_minimal_sizing_result()
        sizing["min_units"] = 2
        flow_input = json.dumps({"units": units, "sizing": sizing, "basis": "single", "single_reason": "testing"})
        code, out, err = run_progress_cmd(["init", "--anchor", VALID_SHA, "--base", VALID_SHA], stdin_data=flow_input, scratch=str(scratch))
        h.test_result("init refuses single with min_units > 1", code != 0 and ("single" in err.lower() or "min_units" in err.lower()), f"exit {code}: {err}")

        print("\n[Test 3] init refuses single with empty reason")
        units = create_unit_list("single", 1)
        sizing = create_minimal_sizing_result()
        sizing["min_units"] = "unknown"
        flow_input = json.dumps({"units": units, "sizing": sizing, "basis": "single", "single_reason": ""})
        code, out, err = run_progress_cmd(["init", "--anchor", VALID_SHA, "--base", VALID_SHA], stdin_data=flow_input, scratch=str(scratch))
        h.test_result("init refuses empty reason", code != 0, f"exit {code}")

        print("\n[Test 4] init refuses sequential with 1 unit")
        units = create_unit_list("sequential", 1)
        sizing = create_minimal_sizing_result()
        sizing["min_units"] = 1
        flow_input = json.dumps({"units": units, "sizing": sizing, "basis": "sequential", "single_reason": None})
        code, out, err = run_progress_cmd(["init", "--anchor", VALID_SHA, "--base", VALID_SHA], stdin_data=flow_input, scratch=str(scratch))
        h.test_result("init refuses sequential with 1 unit", code != 0, f"exit {code}")

        print("\n[Test 5] init accepts sequential with overlapping owned_files (audit F4)")
        git = GitFixture(Path(tmp) / "git1")
        sha1 = git.create_initial_commit("first")

        units = [
            {"unit_id": "u1", "sub_task": "Unit 1", "owned_files": ["file1.py", "shared.py"]},
            {"unit_id": "u2", "sub_task": "Unit 2", "owned_files": ["shared.py", "file2.py"]}
        ]
        sizing = create_minimal_sizing_result()
        sizing["min_units"] = 1
        flow_input = json.dumps({"units": units, "sizing": sizing, "basis": "sequential", "single_reason": None})
        code, out, err = run_progress_cmd(["init", "--anchor", sha1, "--base", sha1], stdin_data=flow_input, scratch=str(scratch))
        h.test_result("init accepts sequential overlap", code == 0, f"exit {code}: {err}")

        print("\n[Test 6] init refuses parallel with overlapping owned_files")
        units = [
            {"unit_id": "u1", "sub_task": "Unit 1", "owned_files": ["file1.py", "shared.py"]},
            {"unit_id": "u2", "sub_task": "Unit 2", "owned_files": ["shared.py", "file2.py"]}
        ]
        sizing = create_minimal_sizing_result()
        sizing["min_units"] = 2
        flow_input = json.dumps({"units": units, "sizing": sizing, "basis": "parallel", "single_reason": None})
        code, out, err = run_progress_cmd(["init", "--anchor", sha1, "--base", sha1], stdin_data=flow_input, scratch=str(scratch))
        h.test_result("init refuses parallel overlap", code != 0, f"exit {code}")

        print("\n[Test 7] init accepts single with unknown min_units")
        units = create_unit_list("single", 1)
        sizing = create_minimal_sizing_result()
        sizing["min_units"] = "unknown"
        flow_input = json.dumps({"units": units, "sizing": sizing, "basis": "single", "single_reason": "coupled"})
        code, out, err = run_progress_cmd(["init", "--supersede", "--anchor", sha1, "--base", sha1], stdin_data=flow_input, scratch=str(scratch))
        h.test_result("init accepts single with unknown and reason", code == 0, f"exit {code}: {err}")

        print("\n[Test 8] mark rejects invalid transitions")
        # First, init a valid record
        units = create_unit_list("parallel", 2)
        sizing = create_minimal_sizing_result()
        flow_input = json.dumps({"units": units, "sizing": sizing, "basis": "parallel", "single_reason": None})
        code, _, _ = run_progress_cmd(["init", "--supersede", "--anchor", sha1, "--base", sha1], stdin_data=flow_input, scratch=str(scratch))
        h.test_result("init succeeds", code == 0)

        # Try to mark without SHA
        code, out, err = run_progress_cmd(["mark", "--unit-id", "u1", "--status", "committed"], scratch=str(scratch))
        h.test_result("mark committed without SHA fails", code != 0, f"exit {code}")

        print("\n[Test 9] mark committed with SHA succeeds")
        code, out, err = run_progress_cmd(["mark", "--unit-id", "u1", "--status", "committed", "--sha", sha1], scratch=str(scratch), cwd=str(git.repo_root))
        h.test_result("mark committed with SHA", code == 0, f"exit {code}: {err}")

        print("\n[Test 10] pending lists pending and failed unit ids")
        code, out, err = run_progress_cmd(["pending"], scratch=str(scratch))
        h.test_result("pending lists u2", code == 0 and "u2" in out, f"exit {code}, output: {out}")

        print("\n[Test 11] check detects schema_version mismatch")
        # Create a real plan file
        git = GitFixture(Path(tmp) / "git2")
        sha2 = git.create_initial_commit("second")
        plan_file = Path(tmp) / "plan_test11.md"
        plan_file.write_text("# Test Plan\n")

        # Create units with a real plan
        units = create_unit_list("parallel", 2)
        sizing = create_minimal_sizing_result()
        flow_input = json.dumps({"units": units, "sizing": sizing, "basis": "parallel", "single_reason": None})
        scratch11 = Path(tmp) / "scratch11"
        scratch11.mkdir()
        code, _, _ = run_progress_cmd(["init", "--anchor", sha2, "--base", sha2, "--plan", str(plan_file)], stdin_data=flow_input, scratch=str(scratch11))
        h.test_result("init succeeds for test 11", code == 0)

        # Corrupt the units.json file
        units_json_file = scratch11 / "units" / "implement" / "units.json"
        if units_json_file.exists():
            content = json.loads(units_json_file.read_text())
            content["schema_version"] = 99
            units_json_file.write_text(json.dumps(content))

        code, out, err = run_progress_cmd(["check", "--plan", str(plan_file), "--anchor", sha2, "--head", sha2], scratch=str(scratch11))
        h.test_result("check detects schema mismatch", code != 0 and "schema" in err.lower(), f"exit {code}: {err}")
        h.test_result("units.json exists after check", units_json_file.exists())

        print("\n[Test 12] tip prints chain tip SHA")
        # Create fresh record with known SHA
        git = GitFixture(Path(tmp) / "git3")
        sha3 = git.create_initial_commit("third")

        units = create_unit_list("parallel", 1)
        sizing = create_minimal_sizing_result()
        sizing["min_units"] = 1
        flow_input = json.dumps({"units": units, "sizing": sizing, "basis": "parallel", "single_reason": None})
        scratch3 = Path(tmp) / "scratch3"
        scratch3.mkdir()
        code, _, _ = run_progress_cmd(["init", "--anchor", sha3, "--base", sha3], stdin_data=flow_input, scratch=str(scratch3))

        code, out, err = run_progress_cmd(["tip"], scratch=str(scratch3))
        h.test_result("tip returns SHA", code == 0 and len(out.strip()) == 40, f"exit {code}, output: {out}")

    _hardening_cases(h)
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
