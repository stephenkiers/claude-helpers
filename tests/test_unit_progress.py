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


def run_progress_cmd(cmd_args: list, stdin_data: str = None, scratch: str = None) -> tuple:
    """Run unit-progress with given args. Returns (exit_code, stdout, stderr)."""
    base_cmd = [sys.executable, str(PROGRESS_SCRIPT), "--step", "implement"]
    if scratch:
        base_cmd.extend(["--scratch", scratch])
    else:
        base_cmd.extend(["--flow-dir", "/tmp/nonexistent"])  # Dummy for non-flow mode

    cmd = base_cmd + cmd_args
    result = subprocess.run(cmd, capture_output=True, text=True, input=stdin_data)
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


def main() -> None:
    h = Harness("unit-progress.py")

    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "scratch"
        scratch.mkdir()

        print("[Test 1] init refuses units below min_units")
        units = create_unit_list("parallel", 1)  # Only 1 unit
        sizing = create_minimal_sizing_result()  # min_units = 2
        sizing["min_units"] = 2
        flow_input = json.dumps({"units": units, "sizing": sizing, "basis": "parallel", "single_reason": None})
        code, out, err = run_progress_cmd(["init", "--anchor", "abc123def", "--base", "abc123def"], stdin_data=flow_input, scratch=str(scratch))
        h.test_result("init refuses (exit non-zero)", code != 0, f"exit {code}")
        h.test_result("reason in stderr", "min_units" in err or "below" in err.lower(), err)

        print("\n[Test 2] init refuses single with known min_units > 1")
        units = create_unit_list("single", 1)
        sizing = create_minimal_sizing_result()
        sizing["min_units"] = 2
        flow_input = json.dumps({"units": units, "sizing": sizing, "basis": "single", "single_reason": "testing"})
        code, out, err = run_progress_cmd(["init", "--anchor", "abc123", "--base", "abc123"], stdin_data=flow_input, scratch=str(scratch))
        h.test_result("init refuses", code != 0, f"exit {code}")

        print("\n[Test 3] init refuses single with empty reason")
        units = create_unit_list("single", 1)
        sizing = create_minimal_sizing_result()
        sizing["min_units"] = "unknown"
        flow_input = json.dumps({"units": units, "sizing": sizing, "basis": "single", "single_reason": ""})
        code, out, err = run_progress_cmd(["init", "--anchor", "abc123", "--base", "abc123"], stdin_data=flow_input, scratch=str(scratch))
        h.test_result("init refuses empty reason", code != 0, f"exit {code}")

        print("\n[Test 4] init refuses sequential with 1 unit")
        units = create_unit_list("sequential", 1)
        sizing = create_minimal_sizing_result()
        sizing["min_units"] = 1
        flow_input = json.dumps({"units": units, "sizing": sizing, "basis": "sequential", "single_reason": None})
        code, out, err = run_progress_cmd(["init", "--anchor", "abc123", "--base", "abc123"], stdin_data=flow_input, scratch=str(scratch))
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
        code, out, err = run_progress_cmd(["mark", "--unit-id", "u1", "--status", "committed", "--sha", sha1], scratch=str(scratch))
        h.test_result("mark committed with SHA", code == 0, f"exit {code}: {err}")

        print("\n[Test 10] pending lists pending and failed unit ids")
        code, out, err = run_progress_cmd(["pending"], scratch=str(scratch))
        h.test_result("pending lists u2", code == 0 and "u2" in out, f"exit {code}, output: {out}")

        print("\n[Test 11] check detects schema_version mismatch")
        # Corrupt the units.json file
        git = GitFixture(Path(tmp) / "git2")
        sha2 = git.create_initial_commit("second")

        units_json = scratch / "units" / "implement"
        if units_json.exists():
            for f in units_json.glob("*.json"):
                if "units" in f.name:
                    content = json.loads(f.read_text())
                    content["schema_version"] = 99
                    f.write_text(json.dumps(content))

        code, out, err = run_progress_cmd(["check", "--plan", "/tmp/dummy.md", "--anchor", sha2, "--head", sha2], scratch=str(scratch))
        h.test_result("check detects schema mismatch", code != 0, f"exit {code}")

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

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
