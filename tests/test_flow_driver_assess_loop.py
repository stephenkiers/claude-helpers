#!/usr/bin/env python3
"""
Loop-level tests for the opt-in assess stage (`driver start --assess`): off by default, the assessor SPAWN and WAIT after a
landed verify, the batch ASK, per-item `answer --item` answers, and the stage advancing to ship only when every item has a
disposition. Drives the real driver CLI under a temp HOME with fake step agents (no model runs).

Run with: python3 tests/test_flow_driver_assess_loop.py
"""

import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Tuple

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from _flow_driver_loop_helpers import FakeGh, load_json, make_home, next_line, run_cli, state_of, write_receipt  # noqa: E402
from _test_harness import Harness  # noqa: E402

RECORD = (
    "implement:Step 3 | status=open | kind=code | scope=isolated | leave_as_is_safe=false | cause=c | evidence=e"
    " | finish_plan=f | verification_proof=v | options=Finish it"
)


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


def to_landed_verify(base: Path, assess: bool, outstanding: bool = True) -> Tuple[Path, Path, Dict[str, str], str, Dict[str, Any]]:
    """Drive worktree..verify with OK receipts (implement carries an OUTSTANDING line); return what `next` prints after verify."""
    home = make_home(base)
    env = FakeGh(base, mode="nopr").env()
    flow = home / ".claude" / "flows-v2" / "owner_name" / "486-probe-abc123"
    code, _, err = run_cli(home, "init", "--flow", str(flow), "--worktree", str(home / "wt"), "--repo", "owner/name")
    expect(code == 0, f"init exited {code}: {err}")
    args = ["start", "--flow", str(flow)] + (["--assess"] if assess else [])
    code, out, err = run_cli(home, *args, extra_env=env)
    expect(code == 0, f"start exited {code}: {err}")
    verb, path = next_line(out)
    current = load_json(path)
    for _ in range(12):
        step = current["step"]
        extra = f"WORKTREE: {home / 'wt'}\n" if step == "worktree" else ""
        if step == "implement" and outstanding:
            extra = "OUTSTANDING: Step 3 (tests missing) [self-report]\n"
        write_receipt(flow, step, current["attempt"], extra=extra)
        code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "notification", extra_env=env)
        expect(code == 0, f"next exited {code} after {step}: {err}")
        verb, path = next_line(out)
        current = {"verb": verb, **load_json(path)}
        if step == "verify":
            return home, flow, env, verb, current
    raise AssertionError("the loop never reached verify")


def test_off_by_default(base: Path) -> None:
    _, _, _, verb, nxt = to_landed_verify(base, assess=False)
    expect(verb == "SPAWN" and nxt["step"] == "ship", f"without --assess verify did not advance to ship: {nxt}")


def test_nothing_outstanding_skips(base: Path) -> None:
    _, flow, _, verb, nxt = to_landed_verify(base, assess=True, outstanding=False)
    expect(verb == "SPAWN" and nxt["step"] == "ship", f"an empty outstanding set must not spawn an assessor: {nxt}")
    expect(state_of(flow)["assess_gate"]["state"] == "passed", "the empty pass was not recorded")


def test_spawn_wait_ask_answer_advance(base: Path) -> None:
    home, flow, env, verb, nxt = to_landed_verify(base, assess=True)
    expect(verb == "SPAWN" and nxt["step"] == "assess", f"expected the assessor SPAWN, got {nxt}")
    prompt = Path(str(nxt["prompt_file"])).read_text(encoding="utf-8")
    expect("07-assess-records.txt" in prompt, "the assessor prompt does not name its records file")
    code, out, _ = run_cli(home, "next", "--flow", str(flow), "--after", "spawn-launched", extra_env=env)
    verb, _ = next_line(out)
    expect(verb == "WAIT", f"an assessor with no records printed {verb}, expected WAIT")
    (flow / "steps" / "07-assess-records.txt").write_text(RECORD + "\n", encoding="utf-8")
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "notification", extra_env=env)
    verb, path = next_line(out)
    ask = load_json(path)
    expect(verb == "ASK" and "--item" in str(ask["answer_command"]), f"expected the batch ASK, got {verb} {ask} {err}")
    expect(state_of(flow)["parked"]["kind"] == "assess", "the batch was not parked")
    code, _, err = run_cli(home, "answer", "--flow", str(flow), "--item", "implement:Step 3", "--text", "Bogus", extra_env=env)
    expect(code != 0, "an answer outside the item's options was accepted")
    code, _, err = run_cli(home, "answer", "--flow", str(flow), "--item", "implement:Step 3", "--text", "Drop with a reason", extra_env=env)
    expect(code != 0, "Drop without a note was accepted")
    code, _, err = run_cli(
        home, "answer", "--flow", str(flow), "--item", "implement:Step 3", "--text", "Drop with a reason", "--note", "out of scope", extra_env=env
    )
    expect(code == 0, f"a valid answer was refused: {err}")
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "notification", extra_env=env)
    verb, path = next_line(out)
    expect(verb == "SPAWN" and load_json(path)["step"] == "ship", f"a fully answered batch did not advance to ship: {verb} {load_json(path)} {err}")
    state = state_of(flow)
    expect(state["assess_gate"]["state"] == "passed" and state["parked"] is None, "the pass was not recorded or parked not cleared")
    expect((flow / "steps" / "08-ship.outstanding.md").exists(), "the disposition marker was not written")


def test_bad_records_halt(base: Path) -> None:
    home, flow, env, _, _ = to_landed_verify(base, assess=True)
    (flow / "steps" / "07-assess-records.txt").write_text("nonsense\n", encoding="utf-8")
    code, out, _ = run_cli(home, "next", "--flow", str(flow), "--after", "notification", extra_env=env)
    verb, _ = next_line(out)
    expect(verb == "STOP" and state_of(flow)["status"] == "failed", f"bad assessor records printed {verb}")
    expect(state_of(flow)["stop_reason"]["verify"].startswith("assess-error:"), str(state_of(flow)["stop_reason"]))


def test_item_answer_without_batch_refused(base: Path) -> None:
    home, flow, env, _, _ = to_landed_verify(base, assess=False)
    code, _, err = run_cli(home, "answer", "--flow", str(flow), "--item", "x", "--text", "y", extra_env=env)
    expect(code != 0, "--item with no assess batch parked was accepted")


def test_assessor_wait_timeout_then_clear_live(base: Path) -> None:
    home, flow, env, _, nxt = to_landed_verify(base, assess=True)
    first_nonce = nxt["nonce"]
    run_cli(home, "next", "--flow", str(flow), "--after", "spawn-launched", extra_env=env)
    code, out, _ = run_cli(home, "next", "--flow", str(flow), "--after", "wait-timeout", extra_env=env)
    verb, _ = next_line(out)
    state = state_of(flow)
    expect(verb == "STOP" and state["stop_reason"]["verify"].startswith("wait-timeout:"), f"a timed-out assessor printed {verb} / {state['stop_reason']}")
    code, _, err = run_cli(home, "clear-live", "--flow", str(flow), "--reason", "assessor never answered", extra_env=env)
    expect(code == 0, f"clear-live exited {code}: {err}")
    expect(state_of(flow)["status"] == "running" and state_of(flow).get("assess_gate") is None, "clear-live did not reopen the assess stage")
    code, out, _ = run_cli(home, "next", "--flow", str(flow), extra_env=env)
    verb, path = next_line(out)
    spawn = load_json(path)
    expect(verb == "SPAWN" and spawn["step"] == "assess" and spawn["nonce"] != first_nonce, f"the assessor was not re-spawned fresh: {verb} {spawn}")


def test_plain_answer_refuses_assess_batch(base: Path) -> None:
    home, flow, env, _, _ = to_landed_verify(base, assess=True)
    (flow / "steps" / "07-assess-records.txt").write_text(RECORD + "\n", encoding="utf-8")
    run_cli(home, "next", "--flow", str(flow), "--after", "notification", extra_env=env)
    # Try to answer the assess batch without --item, which should be refused
    code, _, err = run_cli(home, "answer", "--flow", str(flow), "--text", "Finish it", extra_env=env)
    expect(code != 0, f"plain answer on assess batch was accepted; exit code={code}, stderr={err}")
    expect("--item" in err, f"error message does not mention --item: {err}")


def test_item_id_with_a_space_is_shell_quoted_in_the_answer_command(base: Path) -> None:
    home, flow, env, _, _ = to_landed_verify(base, assess=True)
    run_cli(home, "next", "--flow", str(flow), "--after", "spawn-launched", extra_env=env)
    (flow / "steps" / "07-assess-records.txt").write_text(RECORD + "\n", encoding="utf-8")
    _, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "notification", extra_env=env)
    verb, path = next_line(out)
    expect(verb == "ASK", f"expected the batch ASK, got {verb} {err}")
    command = str(load_json(path)["answer_command"])
    expect(command.endswith("--item 'implement:Step 3'"), f"the item id is not shell-quoted: {command}")


CHECKS = [
    ("1: assess is off by default", test_off_by_default),
    ("2: nothing outstanding skips the assessor", test_nothing_outstanding_skips),
    ("3: spawn, wait, ask, answer, advance", test_spawn_wait_ask_answer_advance),
    ("4: bad assessor records halt", test_bad_records_halt),
    ("5: --item without a parked batch is refused", test_item_answer_without_batch_refused),
    ("6: assessor wait-timeout stops; clear-live re-spawns", test_assessor_wait_timeout_then_clear_live),
    ("7: plain answer refuses assess batch", test_plain_answer_refuses_assess_batch),
    ("8: item id with a space is shell-quoted in the answer command", test_item_id_with_a_space_is_shell_quoted_in_the_answer_command),
]


def main() -> None:
    h = Harness("FLOW DRIVER ASSESS LOOP")
    for name, fn in CHECKS:
        check(h, name, fn)
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
