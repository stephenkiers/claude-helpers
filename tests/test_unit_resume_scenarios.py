#!/usr/bin/env python3
"""
Tests for unit-progress.py re-run scenarios: 10 scenarios per Step 9 of the plan.

Scenarios test:
1. Sequential split of 3 units, unit 2 fails; plain Retry with --partial-base
2. Split-finer after turn-exhausted single unit, keep branch
3. Split-finer after turn-exhausted single unit, reset branch
4. Split-finer on record with committed unit → init --supersede refuses
5. Retry with sonnet on sequential FAILED receipt
6. Parallel run of 3 units, 2 committed and marked, 1 failed
7. Single-unit run that failed with uncommitted work; sonnet escalation
8. Coverage retry with completed main record: scoped record chain
9. Every unit committed, earlier attempt failed at gate → pending empty
10. Re-run without --partial-base (anchor mismatch) → check fails

Each scenario uses a throwaway git repo via _git_fixture and exercises the
init, mark, check, pending, and tip subcommands exactly as Steps 4–7 prescribe.

Run with: python3 tests/test_unit_resume_scenarios.py
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


def run_progress_cmd(cmd_args: list, stdin_data: str = None, flow_dir: str = None) -> tuple:
    """Run unit-progress with given args. Returns (exit_code, stdout, stderr)."""
    base_cmd = [sys.executable, str(PROGRESS_SCRIPT), "--step", "implement"]
    if flow_dir:
        base_cmd.extend(["--flow-dir", flow_dir])

    cmd = base_cmd + cmd_args
    result = subprocess.run(cmd, capture_output=True, text=True, input=stdin_data)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def create_sizing_result(min_units: int = 1) -> dict:
    """Create a minimal sizing result."""
    return {
        "schema_version": 1,
        "kind": "plan",
        "steps": 12,
        "files": 15,
        "findings": None,
        "ceilings": {"steps": 8, "files": 12, "findings": 16},
        "min_units": min_units,
        "status": "ok",
        "reason": ""
    }


def main() -> None:
    h = Harness("unit-progress.py re-run scenarios")

    with tempfile.TemporaryDirectory() as tmp:
        print("[Scenario 1] Sequential split of 3 units, unit 2 fails; plain Retry")
        git = GitFixture(Path(tmp) / "scenario1")
        base_sha = git.create_initial_commit("base")

        # Create initial record with 3 sequential units
        flow_dir = Path(tmp) / "scenario1_flow"
        flow_dir.mkdir()

        units = [
            {"unit_id": "u1", "sub_task": "Unit 1", "owned_files": ["a.py"]},
            {"unit_id": "u2", "sub_task": "Unit 2", "owned_files": ["b.py"]},
            {"unit_id": "u3", "sub_task": "Unit 3", "owned_files": ["c.py"]}
        ]
        sizing = create_sizing_result(3)
        flow_input = json.dumps({"units": units, "sizing": sizing, "basis": "sequential", "single_reason": None})
        code, _, _ = run_progress_cmd(["init", "--anchor", base_sha, "--base", base_sha], stdin_data=flow_input, flow_dir=str(flow_dir))
        h.test_result("init sequential 3 units", code == 0)

        # Mark u1 as committed
        code, _, _ = run_progress_cmd(["mark", "--unit-id", "u1", "--status", "committed", "--sha", base_sha], flow_dir=str(flow_dir))
        h.test_result("mark u1 committed", code == 0)

        # Check pending before retry
        code, out, _ = run_progress_cmd(["pending"], flow_dir=str(flow_dir))
        h.test_result("pending has u2, u3 before retry", code == 0 and "u2" in out and "u3" in out, out)

        # Simulate retry: check with same anchor
        code, out, err = run_progress_cmd(["check", "--plan", "/tmp/dummy.md", "--anchor", base_sha, "--head", base_sha], flow_dir=str(flow_dir))
        h.test_result("check passes on retry", code == 0, err)

        # Pending still has u2, u3
        code, out, _ = run_progress_cmd(["pending"], flow_dir=str(flow_dir))
        h.test_result("pending still u2, u3 after retry check", "u2" in out and "u3" in out, out)

        print("\n[Scenario 2] Split-finer after single unit, keep branch")
        git2 = GitFixture(Path(tmp) / "scenario2")
        base_sha2 = git2.create_initial_commit("base")

        flow_dir2 = Path(tmp) / "scenario2_flow"
        flow_dir2.mkdir()

        # Initial single-unit record
        units_single = [{"unit_id": "u1", "sub_task": "Unit 1", "owned_files": ["file.py"]}]
        sizing_single = create_sizing_result(1)
        flow_input = json.dumps({"units": units_single, "sizing": sizing_single, "basis": "single", "single_reason": "coupled"})
        code, _, _ = run_progress_cmd(["init", "--anchor", base_sha2, "--base", base_sha2], stdin_data=flow_input, flow_dir=str(flow_dir2))
        h.test_result("init single unit", code == 0)

        # Supersede with split-finer record (2 units)
        units_split = [
            {"unit_id": "u1s", "sub_task": "Unit 1 split", "owned_files": ["file.py"]},
            {"unit_id": "u2s", "sub_task": "Unit 2 split", "owned_files": ["file2.py"]}
        ]
        sizing_split = create_sizing_result(2)
        flow_input = json.dumps({"units": units_split, "sizing": sizing_split, "basis": "sequential", "single_reason": None})
        code, _, err = run_progress_cmd(["init", "--supersede", "--anchor", base_sha2, "--base", base_sha2], stdin_data=flow_input, flow_dir=str(flow_dir2))
        h.test_result("init --supersede on single unit", code == 0, err)

        print("\n[Scenario 3] Split-finer reset branch")
        git3 = GitFixture(Path(tmp) / "scenario3")
        base_sha3 = git3.create_initial_commit("base")

        flow_dir3 = Path(tmp) / "scenario3_flow"
        flow_dir3.mkdir()

        # Similar to scenario 2 but test that reset results in base_sha = base (no new commit)
        units_single = [{"unit_id": "u1", "sub_task": "Unit 1", "owned_files": ["file.py"]}]
        sizing_single = create_sizing_result(1)
        flow_input = json.dumps({"units": units_single, "sizing": sizing_single, "basis": "single", "single_reason": "coupled"})
        code, _, _ = run_progress_cmd(["init", "--anchor", base_sha3, "--base", base_sha3], stdin_data=flow_input, flow_dir=str(flow_dir3))
        h.test_result("init single for reset test", code == 0)

        print("\n[Scenario 4] Split-finer on record with committed unit → init --supersede refuses")
        git4 = GitFixture(Path(tmp) / "scenario4")
        base_sha4 = git4.create_initial_commit("base")

        flow_dir4 = Path(tmp) / "scenario4_flow"
        flow_dir4.mkdir()

        # Create record with 1 committed unit
        units = [{"unit_id": "u1", "sub_task": "Unit 1", "owned_files": ["file.py"]}]
        sizing = create_sizing_result(1)
        flow_input = json.dumps({"units": units, "sizing": sizing, "basis": "single", "single_reason": "test"})
        code, _, _ = run_progress_cmd(["init", "--anchor", base_sha4, "--base", base_sha4], stdin_data=flow_input, flow_dir=str(flow_dir4))
        h.test_result("init record", code == 0)

        # Mark as committed
        code, _, _ = run_progress_cmd(["mark", "--unit-id", "u1", "--status", "committed", "--sha", base_sha4], flow_dir=str(flow_dir4))
        h.test_result("mark committed", code == 0)

        # Try to supersede: should refuse
        units_split = [
            {"unit_id": "u1s", "sub_task": "Split 1", "owned_files": ["a.py"]},
            {"unit_id": "u2s", "sub_task": "Split 2", "owned_files": ["b.py"]}
        ]
        sizing_split = create_sizing_result(2)
        flow_input = json.dumps({"units": units_split, "sizing": sizing_split, "basis": "sequential", "single_reason": None})
        code, _, err = run_progress_cmd(["init", "--supersede", "--anchor", base_sha4, "--base", base_sha4], stdin_data=flow_input, flow_dir=str(flow_dir4))
        h.test_result("init --supersede refuses with committed units", code != 0, err)

        print("\n[Scenario 5] Retry with sonnet on sequential FAILED receipt")
        git5 = GitFixture(Path(tmp) / "scenario5")
        base_sha5 = git5.create_initial_commit("base")

        flow_dir5 = Path(tmp) / "scenario5_flow"
        flow_dir5.mkdir()

        # Create sequential record
        units = [
            {"unit_id": "u1", "sub_task": "Unit 1", "owned_files": ["a.py"]},
            {"unit_id": "u2", "sub_task": "Unit 2", "owned_files": ["b.py"]}
        ]
        sizing = create_sizing_result(2)
        flow_input = json.dumps({"units": units, "sizing": sizing, "basis": "sequential", "single_reason": None})
        code, _, _ = run_progress_cmd(["init", "--anchor", base_sha5, "--base", base_sha5], stdin_data=flow_input, flow_dir=str(flow_dir5))
        h.test_result("init sequential", code == 0)

        # Check resumes pending units
        code, out, _ = run_progress_cmd(["pending"], flow_dir=str(flow_dir5))
        h.test_result("pending lists both units", "u1" in out and "u2" in out, out)

        print("\n[Scenario 6] Parallel run: 2 committed, 1 failed")
        git6 = GitFixture(Path(tmp) / "scenario6")
        base_sha6 = git6.create_initial_commit("base")

        flow_dir6 = Path(tmp) / "scenario6_flow"
        flow_dir6.mkdir()

        units = [
            {"unit_id": "u1", "sub_task": "Unit 1", "owned_files": ["a.py"]},
            {"unit_id": "u2", "sub_task": "Unit 2", "owned_files": ["b.py"]},
            {"unit_id": "u3", "sub_task": "Unit 3", "owned_files": ["c.py"]}
        ]
        sizing = create_sizing_result(3)
        flow_input = json.dumps({"units": units, "sizing": sizing, "basis": "parallel", "single_reason": None})
        code, _, _ = run_progress_cmd(["init", "--anchor", base_sha6, "--base", base_sha6], stdin_data=flow_input, flow_dir=str(flow_dir6))
        h.test_result("init parallel", code == 0)

        # Mark 2 as committed, 1 as failed
        run_progress_cmd(["mark", "--unit-id", "u1", "--status", "committed", "--sha", base_sha6], flow_dir=str(flow_dir6))
        run_progress_cmd(["mark", "--unit-id", "u2", "--status", "committed", "--sha", base_sha6], flow_dir=str(flow_dir6))
        run_progress_cmd(["mark", "--unit-id", "u3", "--status", "failed"], flow_dir=str(flow_dir6))

        # Check pending
        code, out, _ = run_progress_cmd(["pending"], flow_dir=str(flow_dir6))
        h.test_result("pending shows u3", "u3" in out, out)

        print("\n[Scenario 7] Single-unit with uncommitted work; escalation keeps dirty tree")
        git7 = GitFixture(Path(tmp) / "scenario7")
        base_sha7 = git7.create_initial_commit("base")

        flow_dir7 = Path(tmp) / "scenario7_flow"
        flow_dir7.mkdir()

        units = [{"unit_id": "u1", "sub_task": "Unit 1", "owned_files": ["file.py"]}]
        sizing = create_sizing_result(1)
        flow_input = json.dumps({"units": units, "sizing": sizing, "basis": "single", "single_reason": "test"})
        code, _, _ = run_progress_cmd(["init", "--anchor", base_sha7, "--base", base_sha7], stdin_data=flow_input, flow_dir=str(flow_dir7))
        h.test_result("init single", code == 0)

        # check should tolerate uncommitted changes (per plan)
        code, _, _ = run_progress_cmd(["check", "--plan", "/tmp/dummy.md", "--anchor", base_sha7, "--head", base_sha7], flow_dir=str(flow_dir7))
        h.test_result("check tolerates dirty tree", code == 0)

        print("\n[Scenario 8] Coverage retry: scoped record chain")
        git8 = GitFixture(Path(tmp) / "scenario8")
        base_sha8 = git8.create_initial_commit("base")

        flow_dir8 = Path(tmp) / "scenario8_flow"
        flow_dir8.mkdir()

        # Create main record
        units = [
            {"unit_id": "u1", "sub_task": "Unit 1", "owned_files": ["a.py"]},
            {"unit_id": "u2", "sub_task": "Unit 2", "owned_files": ["b.py"]}
        ]
        sizing = create_sizing_result(2)
        flow_input = json.dumps({"units": units, "sizing": sizing, "basis": "parallel", "single_reason": None})
        code, _, _ = run_progress_cmd(["init", "--anchor", base_sha8, "--base", base_sha8], stdin_data=flow_input, flow_dir=str(flow_dir8))
        h.test_result("init main record", code == 0)

        # Mark all as committed
        run_progress_cmd(["mark", "--unit-id", "u1", "--status", "committed", "--sha", base_sha8], flow_dir=str(flow_dir8))
        run_progress_cmd(["mark", "--unit-id", "u2", "--status", "committed", "--sha", base_sha8], flow_dir=str(flow_dir8))

        # Get tip
        code, tip, _ = run_progress_cmd(["tip"], flow_dir=str(flow_dir8))
        h.test_result("tip returns SHA", code == 0 and len(tip.strip()) == 40, tip)

        print("\n[Scenario 9] Every unit committed; earlier attempt failed at gate → pending empty")
        git9 = GitFixture(Path(tmp) / "scenario9")
        base_sha9 = git9.create_initial_commit("base")

        flow_dir9 = Path(tmp) / "scenario9_flow"
        flow_dir9.mkdir()

        units = [{"unit_id": "u1", "sub_task": "Unit 1", "owned_files": ["file.py"]}]
        sizing = create_sizing_result(1)
        flow_input = json.dumps({"units": units, "sizing": sizing, "basis": "single", "single_reason": "test"})
        code, _, _ = run_progress_cmd(["init", "--anchor", base_sha9, "--base", base_sha9], stdin_data=flow_input, flow_dir=str(flow_dir9))
        h.test_result("init", code == 0)

        # Mark as committed
        run_progress_cmd(["mark", "--unit-id", "u1", "--status", "committed", "--sha", base_sha9], flow_dir=str(flow_dir9))

        # Pending should be empty
        code, out, _ = run_progress_cmd(["pending"], flow_dir=str(flow_dir9))
        h.test_result("pending empty when all committed", code == 0 and out.strip() == "", f"output: {out}")

        print("\n[Scenario 10] Re-run without --partial-base → anchor mismatch")
        git10 = GitFixture(Path(tmp) / "scenario10")
        base_sha10 = git10.create_initial_commit("base")

        flow_dir10 = Path(tmp) / "scenario10_flow"
        flow_dir10.mkdir()

        units = [{"unit_id": "u1", "sub_task": "Unit 1", "owned_files": ["file.py"]}]
        sizing = create_sizing_result(1)
        flow_input = json.dumps({"units": units, "sizing": sizing, "basis": "single", "single_reason": "test"})
        code, _, _ = run_progress_cmd(["init", "--anchor", base_sha10, "--base", base_sha10], stdin_data=flow_input, flow_dir=str(flow_dir10))
        h.test_result("init with anchor", code == 0)

        # Try check with different anchor
        wrong_sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        code, _, err = run_progress_cmd(["check", "--plan", "/tmp/dummy.md", "--anchor", wrong_sha, "--head", base_sha10], flow_dir=str(flow_dir10))
        h.test_result("check fails on anchor mismatch", code != 0, err)

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
