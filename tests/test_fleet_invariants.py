#!/usr/bin/env python3
"""Pinning tests for /expert-fleet invariants (Q2).

These tests assert two hard constraints:
1. The parser rejects `--fleet` with `--auto-merge`.
2. The default `permission_mode` is not `bypassPermissions`.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import REPO_ROOT, Harness

SCRIPT = REPO_ROOT / "scripts" / "fleet.py"
_spec = importlib.util.spec_from_file_location("fleet", SCRIPT)
fl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fl)

EXPERT_FLOW_CMD = REPO_ROOT / "commands" / "expert-flow.md"


def main():
    h = Harness("fleet invariants")

    # Case 1: Parser rejects --fleet with --auto-merge
    # Extract the bash block under "### Argument parsing" and run it.
    expert_flow_text = EXPERT_FLOW_CMD.read_text()

    # Find the argument parsing block
    match = re.search(
        r"### Argument parsing\n\n```bash\n(.*?)\n```",
        expert_flow_text,
        re.DOTALL
    )

    if not match:
        h.test_result(
            "Argument parsing bash block found",
            False,
            "### Argument parsing bash block not found in expert-flow.md"
        )
    else:
        bash_block = match.group(1)
        # Run the extracted block with test arguments
        # The bash block is parameterized: it reads arguments from $@ and populates variables

        # Test with 12 --fleet --auto-merge (should fail)
        cmd = f"{bash_block}\nprintf %s \"$BAD\""
        result = subprocess.run(
            ["bash", "-c", cmd, "_", "12", "--fleet", "--auto-merge"],
            capture_output=True,
            text=True,
        )
        bad_output = result.stdout
        h.test_result(
            "Parser rejects --fleet --auto-merge",
            "--fleet" in bad_output and len(bad_output) > 0,
            f"BAD={bad_output}"
        )

        # Test with 12 --fleet (BAD must be exactly empty)
        result = subprocess.run(
            ["bash", "-c", cmd, "_", "12", "--fleet"],
            capture_output=True,
            text=True,
        )
        bad_output_pass = result.stdout
        h.test_result(
            "Parser: 12 --fleet accepts (empty BAD)",
            bad_output_pass == "",
            f"BAD={bad_output_pass}"
        )

        # Test with --resume 12 --fleet (BAD must be exactly empty)
        result = subprocess.run(
            ["bash", "-c", cmd, "_", "--resume", "12", "--fleet"],
            capture_output=True,
            text=True,
        )
        bad_output_resume = result.stdout
        h.test_result(
            "Parser: --resume 12 --fleet accepts (empty BAD)",
            bad_output_resume == "",
            f"BAD={bad_output_resume}"
        )

    # Case 2: Default permission_mode is not bypassPermissions
    with tempfile.TemporaryDirectory() as tmp:
        fleet_root = Path(tmp) / "fleets"
        fleet_root.mkdir()
        tickets_file = Path(tmp) / "tickets.json"
        tickets_file.write_text(json.dumps([{"issue": 1, "title": "Test ticket"}]))

        main_wt = Path(tmp) / "main"
        main_wt.mkdir()

        env = {**os.environ, "FLEET_ROOT": str(fleet_root)}

        # Call fleet.py init with no --permission-mode
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "init",
             "--repo", "test/repo",
             "--main-worktree", str(main_wt),
             "--epic", "https://github.com/test/repo/issues/100",
             "--tickets", str(tickets_file)],
            capture_output=True,
            text=True,
            env=env
        )

        if result.returncode == 0:
            fleet_dir = Path(result.stdout.strip())
            fleet_json = json.loads((fleet_dir / "fleet.json").read_text())
            permission_mode = fleet_json.get("permission_mode", "auto")

            h.test_result(
                "Default permission_mode is 'auto'",
                permission_mode == "auto",
                f"Got {permission_mode}"
            )
            h.test_result(
                "permission_mode is not 'bypassPermissions'",
                permission_mode != "bypassPermissions",
                f"Got {permission_mode}"
            )
        else:
            h.test_result(
                "fleet.py init succeeds",
                False,
                f"stderr: {result.stderr}"
            )

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
