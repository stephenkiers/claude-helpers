#!/usr/bin/env python3
"""Pinning tests for /expert-fleet invariants (Q2).

These tests assert three hard constraints:
1. The /expert-flow parser rejects `--fleet` combined with `--auto-merge`, `--gates`, or `--redo`,
   with the exact fleet rejection message (not just any output that mentions `--fleet`).
2. The headless argv `fleet.py` builds never carries `--auto-merge`.
3. The default `permission_mode` written by `fleet.py init` is `auto` (never `bypassPermissions`),
   and an unsupported mode is rejected by argparse.
4. The Accept and continue label is relayed unchanged through the resume argv, and fleet.py
   hard-codes no gate option labels (#365).
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

# The exact text the parser sets in BAD for `--fleet` with a flag it does not combine with.
FLEET_FLAG_BAD = "--fleet combines only with an issue, --resume, --plan-effort, and --review-effort"


def main():
    h = Harness("fleet invariants")

    # Case 1: Parser rejects --fleet with --auto-merge, --gates, and --redo
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
        # The bash block is parameterized: it reads arguments from $@ and sets $BAD on rejection.
        cmd = f"{bash_block}\nprintf %s \"$BAD\""

        def parse_bad(*argv):
            result = subprocess.run(
                ["bash", "-c", cmd, "_", *argv],
                capture_output=True,
                text=True,
            )
            return result.stdout

        reject_cases = {
            "--fleet --auto-merge": ["12", "--fleet", "--auto-merge"],
            "--fleet --gates": ["12", "--fleet", "--gates"],
            # --redo needs --resume to parse; with --resume present the fleet rule is what fires.
            "--fleet --redo": ["--resume", "12", "--fleet", "--redo", "plan"],
        }
        for label, argv in reject_cases.items():
            bad = parse_bad(*argv)
            h.test_result(
                f"Parser rejects {label} with the fleet message",
                bad == FLEET_FLAG_BAD,
                f"BAD={bad!r}"
            )

        accept_cases = {
            "12 --fleet": ["12", "--fleet"],
            "--resume 12 --fleet": ["--resume", "12", "--fleet"],
        }
        for label, argv in accept_cases.items():
            bad = parse_bad(*argv)
            h.test_result(
                f"Parser: {label} accepts (empty BAD)",
                bad == "",
                f"BAD={bad!r}"
            )

    # Case 2: The headless argv never carries --auto-merge (fresh and resume)
    fresh_argv = fl.claude_cmd("/expert-flow 1 --fleet", "auto")
    h.test_result(
        "Fresh fleet argv has no --auto-merge",
        "--auto-merge" not in fresh_argv,
        f"argv={fresh_argv}"
    )
    resume_argv = fl.claude_cmd("Fleet answer for Flow stop:\nRetry", "auto", "sess-1")
    h.test_result(
        "Resume fleet argv has no --auto-merge",
        "--auto-merge" not in resume_argv,
        f"argv={resume_argv}"
    )

    # Case 3: Default permission_mode is 'auto'; an unsupported mode is rejected.
    # FLEET_ROOT is computed from Path.home() at import, so HOME is pointed at a temp dir and the
    # init run writes only there.
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        tickets_file = Path(tmp) / "tickets.json"
        tickets_file.write_text(json.dumps([{"issue": 1, "title": "Test ticket"}]))

        main_wt = Path(tmp) / "main"
        main_wt.mkdir()

        env = {**os.environ, "HOME": str(home)}
        init_base = [
            sys.executable, str(SCRIPT), "init",
            "--repo", "test/repo",
            "--main-worktree", str(main_wt),
            "--epic", "https://github.com/test/repo/issues/100",
            "--tickets", str(tickets_file),
        ]

        # Call fleet.py init with no --permission-mode
        result = subprocess.run(init_base, capture_output=True, text=True, env=env)

        if result.returncode == 0:
            fleet_dir = Path(result.stdout.strip())
            h.test_result(
                "init writes only under the temp HOME",
                str(fleet_dir).startswith(str(home / ".claude" / "fleets") + os.sep),
                f"fleet_dir={fleet_dir}"
            )
            fleet_json = json.loads((fleet_dir / "fleet.json").read_text())
            permission_mode = fleet_json["permission_mode"]

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

        bad_mode = subprocess.run(
            init_base + ["--permission-mode", "yolo"],
            capture_output=True,
            text=True,
            env=env,
        )
        h.test_result(
            "init rejects an unsupported --permission-mode",
            bad_mode.returncode != 0 and "invalid choice" in bad_mode.stderr,
            f"rc={bad_mode.returncode} stderr={bad_mode.stderr!r}"
        )

    # Case 4: the Accept and continue label reaches the flow unchanged through the fleet's resume argv,
    # and fleet.py hard-codes no gate option labels (the labels come from the flow's own gate ask).
    accept_answer = "Fleet answer for Flow stop:\nAccept and continue"
    accept_argv = fl.claude_cmd(accept_answer, "auto", "sess-1")
    h.test_result(
        "Resume argv relays the Accept and continue answer unchanged",
        accept_answer in accept_argv,
        f"argv={accept_argv}"
    )
    fleet_text = SCRIPT.read_text(encoding="utf-8")
    for label in ("Accept and continue", "Retry", "Take over"):
        for quote in ('"', "'"):
            hard_coded = f"{quote}{label}"
            h.test_result(
                f"fleet.py has no hard-coded option string {hard_coded!r}",
                hard_coded not in fleet_text,
                f"found {hard_coded!r} in scripts/fleet.py"
            )

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
