#!/usr/bin/env python3
"""
Loop-level test for `recover --choice reverify`: a PR whose head differs from the shipped head stops at the merge gate, reverify
re-reads the PR, and the merge gate is asked once the head matches.

Drives the real driver CLI under a temp HOME with a fake gh and fake step agents (no model runs).

Run with: python3 tests/test_flow_driver_recover_loop.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from _flow_driver_loop_helpers import FakeGh, directive_text, drive_happy_path, make_home, next_line, run_cli, start_flow, state_of  # noqa: E402
from _test_harness import Harness  # noqa: E402

OTHER_HEAD = "b" * 40


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check(h: Harness, name: str, fn: Callable[[Path], None]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            fn(Path(tmp))
        except AssertionError as exc:
            h.test_result(name, False, str(exc) or "assertion failed")
        except Exception as exc:
            h.test_result(name, False, f"{type(exc).__name__}: {exc}")
        else:
            h.test_result(name, True)


def test_reverify_reasks_the_merge_gate(base: Path) -> None:
    home = make_home(base)
    gh = FakeGh(base, mode="nopr")
    env = gh.env()
    flow, first = start_flow(home, extra_env=env)
    head = subprocess.run(["git", "-C", str(home / "wt"), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()

    def open_pr(sha: str) -> None:
        gh.json_file.write_text(json.dumps({"number": 7, "state": "OPEN", "headRefOid": sha}), encoding="utf-8")
        gh.mode = "open"
        env.update(gh.env())

    verb, _, _ = drive_happy_path(home, flow, first, env, before_next={"ship": lambda: open_pr(OTHER_HEAD)})
    state = state_of(flow)
    expect(verb == "STOP" and state["stop_reason"]["ship"].startswith("merge-head-mismatch"), f"expected a head-mismatch STOP: {verb} {state['stop_reason']}")
    code, _, err = run_cli(home, "recover", "--flow", str(flow), "--choice", "reverify", extra_env=env)
    expect(code == 0, f"recover reverify exited {code}: {err}")
    expect(state_of(flow)["status"] == "running", "reverify did not reopen the flow")
    code, out, _ = run_cli(home, "next", "--flow", str(flow), "--after", "notification", extra_env=env)
    verb, _ = next_line(out)
    expect(verb == "STOP", f"the PR is still on the other head, so the re-read must stop again, got {verb}")
    open_pr(head)  # the PR now matches the shipped head
    code, _, err = run_cli(home, "recover", "--flow", str(flow), "--choice", "reverify", extra_env=env)
    expect(code == 0, f"second reverify exited {code}: {err}")
    code, out, _ = run_cli(home, "next", "--flow", str(flow), "--after", "notification", extra_env=env)
    verb, path = next_line(out)
    expect(verb == "ASK" and "merge-gate" in directive_text(path), f"the merge gate was not asked after the re-read: {verb}")
    expect(state_of(flow)["merge_gate"]["approved_head"] == head, "the gate was recorded for a head other than the matching one")


CHECKS = [("1: reverify re-reads the PR and asks the merge gate once the head matches", test_reverify_reasks_the_merge_gate)]


def main() -> None:
    h = Harness("FLOW DRIVER RECOVER LOOP")
    for name, fn in CHECKS:
        check(h, name, fn)
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
