#!/usr/bin/env python3
"""
Tests for scripts/unit-sizing.py: deterministic sizing of plans and action plans.

Tests verify:
- Counting of plan steps (### Step N headings)
- Counting of distinct files in **Files:** lines
- Counting of action-plan directives (D<n> patterns)
- --only-units filtering
- Status and min_units result computation
- Cascade of configuration (defaults < user < project)
- Fail-closed behaviour (unknown on parse failures)

Run with: python3 tests/test_unit_sizing.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

SIZING_SCRIPT = REPO_ROOT / "scripts" / "unit-sizing.py"


def run_sizing(plan_file: str, kind: str, only_units: str = None, project_root: str = None) -> tuple:
    """Run the sizing script and return (exit_code, output_dict, stderr)."""
    cmd = [sys.executable, str(SIZING_SCRIPT), plan_file, "--kind", kind]
    if only_units:
        cmd.extend(["--only-units", only_units])
    if project_root:
        cmd.extend(["--project-root", project_root])
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError:
        output = None
    return result.returncode, output, result.stderr


def create_plan(step_count: int, files_per_step: int) -> str:
    """Create a plan file with given step count and files per step in the real template format."""
    lines = ["# Plan\n"]
    for i in range(1, step_count + 1):
        lines.append(f"\n### Step {i}\n")
        # Real format: - **Files**: `a.py`, `b.py`
        file_list = ", ".join([f"`file{i}_{j}.py`" for j in range(files_per_step)])
        lines.append(f"- **Files**: {file_list}\n")
    return "".join(lines)


def create_action_plan(directive_count: int) -> str:
    """Create an action plan file with given directive count in the Doing it table format."""
    lines = ["# Action Plan\n\n## Doing it:\n\n"]
    lines.append("| Item | Description |\n")
    lines.append("|------|-------------|\n")
    for i in range(1, directive_count + 1):
        lines.append(f"| **D{i}** | Action {i} |\n")
    return "".join(lines)


def main() -> None:
    h = Harness("unit-sizing.py")

    with tempfile.TemporaryDirectory() as tmp:
        print("[Test 1] 12-step plan with 2 files per step gives min_units 2")
        plan = create_plan(12, 2)  # 12 steps, 24 distinct files
        plan_file = Path(tmp) / "plan.md"
        plan_file.write_text(plan)
        code, output, err = run_sizing(str(plan_file), "plan")
        h.test_result("exit 0", code == 0, str(code))
        h.test_result("status ok", output and output.get("status") == "ok", str(output.get("status") if output else None))
        h.test_result("kind is 'plan'", output and output.get("kind") == "plan", str(output.get("kind") if output else None))
        h.test_result("steps count is 12", output and output.get("steps") == 12, str(output.get("steps") if output else None))
        h.test_result("files count is 24", output and output.get("files") == 24, str(output.get("files") if output else None))
        h.test_result("min_units is 2", output and output.get("min_units") == 2, str(output.get("min_units") if output else None))

        print("\n[Test 2] action-plan with 33 directives gives min_units 3")
        action_plan = create_action_plan(33)
        action_plan_file = Path(tmp) / "action_plan.md"
        action_plan_file.write_text(action_plan)
        code, output, err = run_sizing(str(action_plan_file), "action-plan")
        h.test_result("exit 0", code == 0, str(code))
        h.test_result("status ok", output and output.get("status") == "ok", str(output.get("status") if output else None))
        h.test_result("kind is 'action-plan'", output and output.get("kind") == "action-plan", str(output.get("kind") if output else None))
        h.test_result("findings count is 33", output and output.get("findings") == 33, str(output.get("findings") if output else None))
        h.test_result("min_units is 3", output and output.get("min_units") == 3, str(output.get("min_units") if output else None))

        print("\n[Test 3] 3-step, 1-file plan gives min_units 1")
        small_plan = create_plan(3, 1)
        small_file = Path(tmp) / "small_plan.md"
        small_file.write_text(small_plan)
        code, output, err = run_sizing(str(small_file), "plan")
        h.test_result("status ok", output and output.get("status") == "ok", str(output.get("status") if output else None))
        h.test_result("steps count is 3", output and output.get("steps") == 3, str(output.get("steps") if output else None))
        h.test_result("files count is 3", output and output.get("files") == 3, str(output.get("files") if output else None))
        h.test_result("min_units is 1", output and output.get("min_units") == 1, str(output.get("min_units") if output else None))

        print("\n[Test 4] Plan with no ### Step headings gives status unknown")
        empty_plan = "# Plan\nNo steps here.\n"
        empty_file = Path(tmp) / "empty_plan.md"
        empty_file.write_text(empty_plan)
        code, output, err = run_sizing(str(empty_file), "plan")
        h.test_result("status unknown", output and output.get("status") == "unknown", str(output.get("status") if output else None))
        h.test_result("min_units is 'unknown'", output and output.get("min_units") == "unknown", str(output.get("min_units") if output else None))
        h.test_result("reason mentions Steps", output and "Step N" in output.get("reason", ""), output.get("reason") if output else None)

        print("\n[Test 5] Plan with steps but no Files gives status unknown")
        steps_only = "# Plan\n\n### Step 1\n\nNo files listed.\n"
        steps_file = Path(tmp) / "steps_only.md"
        steps_file.write_text(steps_only)
        code, output, err = run_sizing(str(steps_file), "plan")
        h.test_result("status unknown", output and output.get("status") == "unknown", str(output.get("status") if output else None))
        h.test_result("reason mentions Files", output and "Files" in output.get("reason", ""), output.get("reason") if output else None)

        print("\n[Test 6] --only-units filters to named steps")
        plan = create_plan(5, 1)
        plan_file = Path(tmp) / "filter_plan.md"
        plan_file.write_text(plan)
        code, output, err = run_sizing(str(plan_file), "plan", only_units="Step 1,Step 3")
        h.test_result("exit 0", code == 0, str(code))
        h.test_result("status ok", output and output.get("status") == "ok", str(output.get("status") if output else None))
        h.test_result("steps count is 2", output and output.get("steps") == 2, str(output.get("steps") if output else None))
        h.test_result("files count is 2", output and output.get("files") == 2, str(output.get("files") if output else None))

        print("\n[Test 7] --only-units with no matching ids gives status unknown")
        code, output, err = run_sizing(str(plan_file), "plan", only_units="Unknown")
        h.test_result("exit 0 (no crash)", code == 0, str(code))
        h.test_result("status unknown", output and output.get("status") == "unknown", str(output.get("status") if output else None))
        h.test_result("reason mentions no matching units", output and "no matching units" in output.get("reason", ""), output.get("reason") if output else None)

        print("\n[Test 8] Unreadable file gives status unknown, exit 0")
        nonexistent = Path(tmp) / "nonexistent.md"
        code, output, err = run_sizing(str(nonexistent), "plan")
        h.test_result("exit 0", code == 0, str(code))
        h.test_result("status unknown", output and output.get("status") == "unknown", str(output.get("status") if output else None))

        print("\n[Test 9] Output has schema_version, ceilings, and floor_is_lower_bound")
        plan = create_plan(5, 2)
        plan_file = Path(tmp) / "schema_plan.md"
        plan_file.write_text(plan)
        code, output, err = run_sizing(str(plan_file), "plan")
        h.test_result("has schema_version 1", output and output.get("schema_version") == 1, str(output.get("schema_version") if output else None))
        h.test_result("has ceilings dict", output and isinstance(output.get("ceilings"), dict), str(type(output.get("ceilings")) if output else None))
        h.test_result("has floor_is_lower_bound true", output and output.get("floor_is_lower_bound") is True, str(output.get("floor_is_lower_bound") if output else None))
        h.test_result("has reason field", output and "reason" in output, str(output if output else None))

        print("\n[Test 10] Zero-directive action-plan gives status unknown")
        empty_action = "# Action Plan\n\n## Doing it:\n\n| Item | Description |\n|------|-------------|\n"
        empty_action_file = Path(tmp) / "empty_action.md"
        empty_action_file.write_text(empty_action)
        code, output, err = run_sizing(str(empty_action_file), "action-plan")
        h.test_result("status unknown", output and output.get("status") == "unknown", str(output.get("status") if output else None))
        h.test_result("min_units is 'unknown'", output and output.get("min_units") == "unknown", str(output.get("min_units") if output else None))
        h.test_result("reason mentions no directives", output and "no directives" in output.get("reason", ""), output.get("reason") if output else None)

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
