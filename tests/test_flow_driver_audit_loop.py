#!/usr/bin/env python3
"""
Loop-level tests for the opt-in fidelity audit (`driver start --audit`): off by default, an audit SPAWN and WAIT after a
landed plan, a pass advancing to the next step, and a rejection stopping with a question that recover retries or (allowlisted, human-only) accepts.

Drives the real driver CLI under a temp HOME with fake step agents (no model runs).

Run with: python3 tests/test_flow_driver_audit_loop.py
"""

import sys
import tempfile
from pathlib import Path
from typing import Callable, Dict, Tuple

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from _flow_driver_loop_helpers import FakeGh, load_json, make_home, next_line, run_cli, state_of, write_receipt  # noqa: E402
from _test_harness import Harness  # noqa: E402


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


ENVS: Dict[str, Dict[str, str]] = {}


def to_landed_plan(base: Path, audit: bool) -> Tuple[Path, Path, Dict[str, object]]:
    """Start a flow, land the worktree step, land the plan receipt; return (home, flow, the directive after plan landed)."""
    home = make_home(base)
    env = FakeGh(base, mode="nopr").env()
    flow = home / ".claude" / "flows-v2" / "owner_name" / "486-probe-abc123"
    code, _, err = run_cli(home, "init", "--flow", str(flow), "--worktree", str(home / "wt"), "--repo", "owner/name")
    expect(code == 0, f"init exited {code}: {err}")
    args = ["start", "--flow", str(flow)] + (["--audit"] if audit else [])
    code, out, err = run_cli(home, *args, extra_env=env)
    expect(code == 0, f"start exited {code}: {err}")
    verb, path = next_line(out)
    current = load_json(path)
    write_receipt(flow, "worktree", current["attempt"], extra=f"WORKTREE: {home / 'wt'}\n")
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "notification", extra_env=env)
    expect(code == 0, f"next exited {code}: {err}")
    verb, path = next_line(out)
    plan = load_json(path)
    expect(plan.get("step") == "plan", f"expected the plan SPAWN, got {plan}")
    write_receipt(flow, "plan", plan["attempt"])
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "notification", extra_env=env)
    expect(code == 0, f"next exited {code}: {err}")
    verb, path = next_line(out)
    ENVS[str(flow)] = env
    return home, flow, {"verb": verb, **load_json(path)}


def test_off_by_default(base: Path) -> None:
    _, _, nxt = to_landed_plan(base, audit=False)
    expect(nxt["verb"] == "SPAWN" and nxt["step"] == "track", f"without --audit the flow did not advance: {nxt}")


def test_audit_spawn_wait_pass(base: Path) -> None:
    home, flow, nxt = to_landed_plan(base, audit=True)
    expect(nxt["verb"] == "SPAWN" and nxt["step"] == "plan", f"expected the audit SPAWN, got {nxt}")
    prompt = Path(str(nxt["prompt_file"])).read_text(encoding="utf-8")
    expect("Fidelity Audit" in prompt and "audit-plan-a1.md" in prompt, "the audit prompt lacks its heading or verdict file")
    code, out, _ = run_cli(home, "next", "--flow", str(flow), "--after", "spawn-launched", extra_env=ENVS[str(flow)])
    verb, _ = next_line(out)
    expect(verb == "WAIT", f"a spawned audit with no verdict printed {verb}, expected WAIT")
    (flow / "steps" / "audit-plan-a1.md").write_text("pass\n", encoding="utf-8")
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "notification", extra_env=ENVS[str(flow)])
    verb, path = next_line(out)
    expect(verb == "SPAWN" and load_json(path)["step"] == "track", f"a passed audit did not advance: {verb} {load_json(path)}")
    code, out, _ = run_cli(home, "next", "--flow", str(flow), "--after", "notification", extra_env=ENVS[str(flow)])
    expect(state_of(flow)["audit_gate"]["state"] == "passed", "the pass was not recorded")


def test_reject_stops_and_retry_recovers(base: Path) -> None:
    home, flow, nxt = to_landed_plan(base, audit=True)
    (flow / "steps" / "audit-plan-a1.md").write_text("reject: reduced run (audit): two experts only\n", encoding="utf-8")
    code, out, _ = run_cli(home, "next", "--flow", str(flow), "--after", "notification", extra_env=ENVS[str(flow)])
    verb, _ = next_line(out)
    state = state_of(flow)
    expect(verb == "STOP" and state["status"] == "failed", f"a rejection printed {verb} / {state['status']}")
    expect(state["stop_reason"]["plan"].startswith("audit-rejected:"), f"stop reason {state['stop_reason']}")
    question = load_json(flow / "questions" / "plan-a1-audit.json")
    expect(question["options"] == ["Retry", "Accept and continue"], f"the rejection offers {question['options']}")
    code, _, err = run_cli(home, "recover", "--flow", str(flow), "--choice", "retry", extra_env=ENVS[str(flow)])
    expect(code == 0, f"recover retry exited {code}: {err}")
    code, out, _ = run_cli(home, "next", "--flow", str(flow), extra_env=ENVS[str(flow)])
    verb, path = next_line(out)
    spawn = load_json(path)
    expect(verb == "SPAWN" and spawn["step"] == "plan" and spawn["attempt"] == 2, f"retry did not respawn plan a2: {verb} {spawn}")


def test_malformed_verdict_halts(base: Path) -> None:
    home, flow, _ = to_landed_plan(base, audit=True)
    (flow / "steps" / "audit-plan-a1.md").write_text("looks fine to me\n", encoding="utf-8")
    code, out, _ = run_cli(home, "next", "--flow", str(flow), "--after", "notification", extra_env=ENVS[str(flow)])
    verb, _ = next_line(out)
    expect(verb == "STOP" and state_of(flow)["stop_reason"]["plan"].startswith("audit-error:"), "a malformed verdict did not halt")


def test_accept_allowlisted_rejection(base: Path) -> None:
    home, flow, _ = to_landed_plan(base, audit=True)
    (flow / "steps" / "audit-plan-a1.md").write_text("reject: reduced run (audit): two experts only\n", encoding="utf-8")
    run_cli(home, "next", "--flow", str(flow), "--after", "notification", extra_env=ENVS[str(flow)])
    question = load_json(flow / "questions" / "plan-a1-audit.json")
    expect("Accept and continue" in question["options"], f"an allowlisted rejection offers {question['options']}")
    code, _, err = run_cli(home, "recover", "--flow", str(flow), "--choice", "accept", "--note", "two experts is fine here", extra_env=ENVS[str(flow)])
    expect(code == 0, f"recover accept exited {code}: {err}")
    state = state_of(flow)
    expect(state["status"] == "running" and state["audit_gate"]["state"] == "passed", f"accept left {state['status']}")
    expect((flow / "steps" / "audit-plan-a1.accepted.md").exists(), "the accept trail was not written")
    code, out, _ = run_cli(home, "next", "--flow", str(flow), extra_env=ENVS[str(flow)])
    verb, path = next_line(out)
    expect(verb == "SPAWN" and load_json(path)["step"] == "track", f"accept did not advance to track: {verb}")


def test_accept_refused_off_allowlist(base: Path) -> None:
    home, flow, _ = to_landed_plan(base, audit=True)
    (flow / "steps" / "audit-plan-a1.md").write_text("reject: the plan ignores the ticket\n", encoding="utf-8")
    run_cli(home, "next", "--flow", str(flow), "--after", "notification", extra_env=ENVS[str(flow)])
    question = load_json(flow / "questions" / "plan-a1-audit.json")
    expect(question["options"] == ["Retry"], f"off-allowlist offers {question['options']}")
    code, _, _ = run_cli(home, "recover", "--flow", str(flow), "--choice", "accept", extra_env=ENVS[str(flow)])
    expect(code != 0 and state_of(flow)["status"] == "failed", "accept off the allowlist was not refused")


def test_reverify_refused_without_mismatch(base: Path) -> None:
    home, flow, _ = to_landed_plan(base, audit=True)
    code, _, _ = run_cli(home, "recover", "--flow", str(flow), "--choice", "reverify", extra_env=ENVS[str(flow)])
    expect(code != 0, "reverify with no merge-head-mismatch stop was not refused")


CHECKS = [
    ("1: audit is off by default", test_off_by_default),
    ("2: spawn, wait, pass advances", test_audit_spawn_wait_pass),
    ("3: rejection stops; retry respawns", test_reject_stops_and_retry_recovers),
    ("4: a malformed verdict halts", test_malformed_verdict_halts),
    ("5: accept passes an allowlisted rejection", test_accept_allowlisted_rejection),
    ("6: accept refused off the allowlist", test_accept_refused_off_allowlist),
    ("7: reverify refused without a mismatch", test_reverify_refused_without_mismatch),
]


def main() -> None:
    h = Harness("FLOW DRIVER AUDIT LOOP")
    for name, fn in CHECKS:
        check(h, name, fn)
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
