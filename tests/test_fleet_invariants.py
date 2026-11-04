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
import contextlib
import importlib.util
import io
import json
import os
import re
import stat
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

    # Case 5: heal_pass never raises (Step 8, D1). The fixture has a real worktree and a stubbed heal CLI,
    # so the probe, the tail reservation and the run all execute for real up to the CLI boundary.
    print()
    print("### heal_pass() never raises and tail file permissions")

    def heal_fixture(root: Path, raise_in_run: bool = False):
        fleet_dir = root / "fleet"
        fleet_dir.mkdir()
        wt = root / "wt"
        wt.mkdir()
        main_wt = root / "main"
        main_wt.mkdir()
        (fleet_dir / "fleet.json").write_text(json.dumps(
            {"repo": "test/repo", "main_worktree": str(main_wt), "tickets": [{"issue": 1, "heals": {}}]}))
        flow_dir = root / "flow"
        flow_dir.mkdir()
        (flow_dir / "state.json").write_text(json.dumps({
            "issue": 1, "step": "implement", "status": "failed", "failures": {"implement": 1},
            "worktree": str(wt),
        }))
        calls: list = []
        seen_child: list = []

        def fake_cli(argv, cwd, timeout, on_spawn=None):
            calls.append(argv)
            if "probe" in argv:
                return {"tool": "install", "timeout_secs": 30, "stop_key": fl.current_stop_key(flow_dir)}
            # Stand in for the installer child: record it the way the real spawn hook does, then let it exit.
            child = subprocess.Popen([sys.executable, "-c", "pass"])
            on_spawn(child.pid)
            child.wait()
            seen_child.append(json.loads((fleet_dir / "fleet.json").read_text())["tickets"][0].get("heal_child"))
            if raise_in_run:
                raise RuntimeError("injected failure")
            return {"tool": "install", "status": "healed", "recommendation": "retry-recommended"}

        return fleet_dir, flow_dir, main_wt, calls, seen_child, fake_cli

    real_cli, real_base = fl._run_cli, fl._default_base
    fl._default_base = lambda repo, main_worktree: "origin/main"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            fleet_dir, flow_dir, main_wt, calls, seen_child, fake_cli = heal_fixture(Path(tmp))
            fl._run_cli = fake_cli
            try:
                fl.heal_pass(fleet_dir, 1, "test/repo", flow_dir, main_wt)
                h.test_result("heal_pass does not raise with valid inputs", True)
            except Exception as e:
                h.test_result("heal_pass does not raise with valid inputs", False, f"raised {e}")

            h.test_result("heal_pass reached the run call", bool(calls) and "run" in calls[-1],
                          f"calls={calls}")
            heals_dir = fleet_dir / "heals"
            h.test_result("heals directory is 0700", stat.S_IMODE(heals_dir.stat().st_mode) == 0o700,
                          f"Got mode {oct(stat.S_IMODE(heals_dir.stat().st_mode))}")
            tail_arg = Path(calls[-1][calls[-1].index("--tail-file") + 1])
            seq = tail_arg.with_suffix(".seq")
            h.test_result("tail file lives in heals and ends .tail",
                          tail_arg.parent == heals_dir and tail_arg.suffix == ".tail", f"tail={tail_arg}")
            h.test_result("tail reservation has mode 0600",
                          seq.exists() and stat.S_IMODE(seq.stat().st_mode) == 0o600,
                          f"seq={seq}")
            h.test_result("heal_child recorded while the installer ran",
                          bool(seen_child) and isinstance(seen_child[0], dict) and isinstance(seen_child[0].get("pid"), int),
                          f"seen={seen_child}")
            fdata = json.loads((fleet_dir / "fleet.json").read_text())["tickets"][0]
            h.test_result("heal outcome recorded", fdata["heals"]["missing-deps"]["outcome"] == "healed")
            h.test_result("heal_child cleared once the child exited", "heal_child" not in fdata)

            # A second pass for the same capped cause runs nothing and leaves no new reservation behind.
            seq_count = len(list(heals_dir.glob("*.seq")))
            fl.heal_pass(fleet_dir, 1, "test/repo", flow_dir, main_wt)
            # With every cause spent, the pass skips the probe too (#425 D5), so no CLI call is made at all.
            h.test_result("capped cause: no probe, no second run and no leaked reservation",
                          len(calls) == 2 and len(list(heals_dir.glob("*.seq"))) == seq_count,
                          f"calls={calls}")

        # heal_pass should never raise on an injected failure; the stage and class go to stderr.
        with tempfile.TemporaryDirectory() as tmp:
            fleet_dir, flow_dir, main_wt, calls, seen_child, fake_cli = heal_fixture(Path(tmp), raise_in_run=True)
            fl._run_cli = fake_cli
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err):
                    fl.heal_pass(fleet_dir, 1, "test/repo", flow_dir, main_wt)
                h.test_result("heal_pass does not raise on an injected run failure", True)
            except Exception as e:
                h.test_result("heal_pass does not raise on an injected run failure", False, f"raised {e}")
            h.test_result("failure names the stage and class on stderr",
                          "heal failed at stage run (RuntimeError)" in err.getvalue(), f"stderr={err.getvalue()!r}")
            fdata = json.loads((fleet_dir / "fleet.json").read_text())["tickets"][0]
            h.test_result("failed run still finishes its record as take-over",
                          fdata["heals"]["missing-deps"]["outcome"] == "failed"
                          and fdata["heals"]["missing-deps"]["recommendation"] == "take-over")
    finally:
        fl._run_cli, fl._default_base = real_cli, real_base

    with tempfile.TemporaryDirectory() as tmp:
        # heal_pass with None flow_dir should return silently
        try:
            fl.heal_pass(Path(tmp), 1, "test/repo", None, Path(tmp))
            h.test_result("heal_pass returns silently when flow_dir is None", True)
        except Exception as e:
            h.test_result("heal_pass returns silently when flow_dir is None", False, f"raised {e}")

    # heal_pass with relative worktree path should refuse before probing
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fleet_dir = root / "fleet"
        fleet_dir.mkdir()
        main_wt = root / "main"
        main_wt.mkdir()
        (fleet_dir / "fleet.json").write_text(json.dumps(
            {"repo": "test/repo", "main_worktree": str(main_wt), "tickets": [{"issue": 1, "heals": {}}]}))
        flow_dir = root / "flow"
        flow_dir.mkdir()
        (flow_dir / "state.json").write_text(json.dumps({
            "issue": 1, "step": "implement", "status": "failed", "failures": {"implement": 1},
            "worktree": "relative/path",  # Relative path instead of absolute
        }))
        calls: list = []

        def fake_cli_never_called(argv, cwd, timeout, on_spawn=None):
            calls.append(argv)
            return None

        fl._run_cli = fake_cli_never_called
        try:
            fl.heal_pass(fleet_dir, 1, "test/repo", flow_dir, main_wt)
            h.test_result("heal_pass with relative worktree path returns silently", True)
        except Exception as e:
            h.test_result("heal_pass with relative worktree path returns silently", False, f"raised {e}")
        h.test_result("heal_pass with relative worktree path does not call the CLI",
                      not calls, f"calls={calls}")

    # Stop-key mirror: fleet.current_stop_key must equal heal.stop_key_for for every stop shape
    sys.path.insert(0, str(REPO_ROOT))
    from scripts.workflow import heal as heal_mod
    with tempfile.TemporaryDirectory() as tmp:
        fd = Path(tmp)
        (fd / "state.json").write_text(json.dumps(
            {"step": "verify", "status": "failed", "stop_reason": {"verify": "lint failed"}}))
        h.test_result(
            "stop key mirror: no open ask",
            fl.current_stop_key(fd) == heal_mod.stop_key_for(None, "verify", "failed", "lint failed"),
        )
        ask = json.dumps({"status": "open", "q": "x"}).encode()
        (fd / "fleet-ask.json").write_bytes(ask)
        h.test_result(
            "stop key mirror: open ask",
            fl.current_stop_key(fd) == heal_mod.stop_key_for(ask, "verify", "failed", "lint failed"),
        )
        (fd / "fleet-ask.json").unlink()
        (fd / "state.json").write_text(json.dumps({"step": "implement", "status": "paused"}))
        h.test_result(
            "stop key mirror: no stop_reason recorded",
            fl.current_stop_key(fd) == heal_mod.stop_key_for(None, "implement", "paused", ""),
        )
        (fd / "state.json").write_text(json.dumps({"step": ["implement"], "status": "failed", "stop_reason": {}}))
        h.test_result(
            "stop key: non-string step returns a key instead of raising",
            isinstance(fl.current_stop_key(fd), str),
        )

    # Mirrored constants: fleet.py carries copies of heal.py / deps.py values. These pins fail when either moves.
    h.test_result("HEAL_STEPS mirrors heal.KNOWN_STEPS", fl.HEAL_STEPS == heal_mod.KNOWN_STEPS)
    h.test_result("HEAL_STATUSES mirrors heal._STATUS_VALUES", fl.HEAL_STATUSES == heal_mod._STATUS_VALUES)
    h.test_result("HEAL_TOOL_CAUSES mirrors heal.TOOLS",
                  fl.HEAL_TOOL_CAUSES == {t.name: t.cause for t in heal_mod.TOOLS})
    h.test_result("HEAL_MAX_TIMEOUT_SECS mirrors deps.DEPS_TIMEOUT_SECS",
                  fl.HEAL_MAX_TIMEOUT_SECS == heal_mod.deps.DEPS_TIMEOUT_SECS)
    h.test_result("HEAL_MAX_TIMEOUT_SECS equals the heal timeout_secs upper bound",
                  fl.HEAL_MAX_TIMEOUT_SECS == heal_mod.PARAM_BOUNDS["timeout_secs"][1])

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
