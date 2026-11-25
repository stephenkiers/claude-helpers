#!/usr/bin/env python3
"""
Loop-level tests for the opt-in assess stage (`driver start --assess`): off by default, the assessor SPAWN and WAIT after a
landed verify, the batch ASK, per-item `answer --item` answers, and the stage advancing to ship only when every item has a
disposition. Drives the real driver CLI under a temp HOME with fake step agents (no model runs).

Run with: python3 tests/test_flow_driver_assess_loop.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Tuple

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from _flow_driver_loop_helpers import MAIN, FakeGh, base_env, load_json, make_home, next_line, run_cli, state_of, write_receipt  # noqa: E402
from _test_harness import Harness  # noqa: E402

# v1 flow-assessor reply grammar (#538); a records file ends with the assessment-end sentinel.
ASSESS_SENTINEL = "<!-- assessment-end -->"
RECORD = (
    "implement:Step 3 | status=unfinished | kind=decision | scope=in-scope | leave_as_is_safe=no | cause=c | evidence=e"
    " | finish_plan=f | verification_proof=v | options=Finish it => pause"
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
    args = ["start", "--flow", str(flow)] + ([] if assess else ["--no-assess"])  # assess is on by default (#538)
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


def test_nothing_outstanding_skips(base: Path) -> None:
    _, flow, _, verb, nxt = to_landed_verify(base, assess=True, outstanding=False)
    expect(verb == "SPAWN" and nxt["step"] == "ship", f"an empty outstanding set must not spawn an assessor: {nxt}")
    expect(state_of(flow)["assess_gate"]["state"] == "passed", "the empty pass was not recorded")


def test_spawn_wait_ask_answer_advance(base: Path) -> None:
    home, flow, env, verb, nxt = to_landed_verify(base, assess=True)
    expect(verb == "SPAWN" and nxt["step"] == "assess", f"expected the assessor SPAWN, got {nxt}")
    prompt = Path(str(nxt["prompt_file"])).read_text(encoding="utf-8")
    # flow-assessor has no Write tool: it replies, and assess-reply writes the records file (#538).
    expect("07-assess-records.txt" not in prompt, "the assessor prompt must not name a records file to write")
    expect(ASSESS_SENTINEL in prompt, "the assessor prompt does not name the assessment-end reply sentinel")
    code, out, _ = run_cli(home, "next", "--flow", str(flow), "--after", "spawn-launched", extra_env=env)
    verb, _ = next_line(out)
    expect(verb == "WAIT", f"an assessor with no records printed {verb}, expected WAIT")
    (flow / "steps" / "07-assess-records.txt").write_text(RECORD + "\n" + ASSESS_SENTINEL + "\n", encoding="utf-8")
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
    (flow / "steps" / "07-assess-records.txt").write_text(RECORD + "\n" + ASSESS_SENTINEL + "\n", encoding="utf-8")
    run_cli(home, "next", "--flow", str(flow), "--after", "notification", extra_env=env)
    # Try to answer the assess batch without --item, which should be refused
    code, _, err = run_cli(home, "answer", "--flow", str(flow), "--text", "Finish it", extra_env=env)
    expect(code != 0, f"plain answer on assess batch was accepted; exit code={code}, stderr={err}")
    expect("--item" in err, f"error message does not mention --item: {err}")


def test_item_id_with_a_space_is_shell_quoted_in_the_answer_command(base: Path) -> None:
    home, flow, env, _, _ = to_landed_verify(base, assess=True)
    run_cli(home, "next", "--flow", str(flow), "--after", "spawn-launched", extra_env=env)
    (flow / "steps" / "07-assess-records.txt").write_text(RECORD + "\n" + ASSESS_SENTINEL + "\n", encoding="utf-8")
    _, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "notification", extra_env=env)
    verb, path = next_line(out)
    expect(verb == "ASK", f"expected the batch ASK, got {verb} {err}")
    command = str(load_json(path)["answer_command"])
    expect(command.endswith("--item 'implement:Step 3'"), f"the item id is not shell-quoted: {command}")


def test_assess_spawn_directive_carries_no_reply_delimiter(base: Path) -> None:
    _, _, _, verb, nxt = to_landed_verify(base, assess=True)
    expect(verb == "SPAWN" and nxt["step"] == "assess", f"expected assess SPAWN, got {verb} {nxt}")
    reply = nxt.get("reply")
    expect(reply is not None and "command" in reply and "delimiter" not in reply,
           f"assess SPAWN directive reply should have command but not delimiter: {reply}")
    directive_text = json.dumps(nxt)
    expect("REPLY-DELIMITER" not in directive_text and "assessment-end" not in directive_text,
           f"SPAWN directive contains delimiter or sentinel: {directive_text}")


def test_spawn_failed_on_assess_spawn_is_idempotent(base: Path) -> None:
    home, flow, env, verb, nxt = to_landed_verify(base, assess=True)
    expect(verb == "SPAWN" and nxt["step"] == "assess", f"expected assess SPAWN, got {verb} {nxt}")
    code, _, err = run_cli(home, "next", "--flow", str(flow), "--after", "spawn-launched", extra_env=env)
    expect(code == 0, f"spawn-launched exited {code}: {err}")
    expect(state_of(flow)["pending_spawn"] is not None, "no pending spawn after spawn-launched")
    code1, _, err1 = run_cli(home, "next", "--flow", str(flow), "--after", "spawn-failed", extra_env=env)
    expect(code1 == 0, f"first spawn-failed exited {code1}: {err1}")
    first = state_of(flow)
    code2, _, err2 = run_cli(home, "next", "--flow", str(flow), "--after", "spawn-failed", extra_env=env)
    expect(code2 == 0, f"second spawn-failed exited {code2}: {err2}")
    second = state_of(flow)
    first_status, second_status = first.get("status"), second.get("status")
    first_step, second_step = first.get("step"), second.get("step")
    expect(first_status == second_status, f"duplicate spawn-failed changed status: {first_status} -> {second_status}")
    expect(first_step == second_step, f"duplicate spawn-failed changed step: {first_step} -> {second_step}")


def test_assess_off_advances_verify_to_ship(base: Path) -> None:
    """D26: with assess off, a landed verify with an outstanding item goes straight to ship."""
    _, flow, _, verb, nxt = to_landed_verify(base, assess=False)
    expect(verb == "SPAWN" and nxt["step"] == "ship", f"assess off must advance verify to ship: {verb} {nxt}")
    state = state_of(flow)
    expect(state["assess"] is False and state["assess_gate"] is None, f"assess off ran the stage: {state['assess_gate']}")


def assess_reply_cli(home: Path, flow: Path, nonce: str, data: bytes, env: Dict[str, str]) -> Tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(MAIN), "assess-reply", "--flow", str(flow), "--nonce", nonce],
        input=data, capture_output=True, env=base_env(home, env), cwd=str(home), timeout=120,
    )
    return proc.returncode, proc.stderr.decode("utf-8", "replace")


def test_assess_reply_cli(base: Path) -> None:
    home, flow, env, verb, nxt = to_landed_verify(base, assess=True)
    expect(verb == "SPAWN" and nxt["step"] == "assess", f"expected the assessor SPAWN, got {verb} {nxt}")
    run_cli(home, "next", "--flow", str(flow), "--after", "spawn-launched", extra_env=env)
    records = flow / "steps" / "07-assess-records.txt"
    data = (RECORD + "\n" + ASSESS_SENTINEL + "\n").encode("utf-8")
    code, err = assess_reply_cli(home, flow, "not-the-nonce", data, env)
    expect(code == 1 and "not the live assessor spawn" in err, f"a wrong nonce exited {code}: {err}")
    expect(not records.exists(), "a wrong nonce wrote the records file")
    code, err = assess_reply_cli(home, flow, str(nxt["nonce"]), data, env)
    expect(code == 0 and records.read_bytes() == data, f"the live nonce exited {code}: {err}")
    code, err = assess_reply_cli(home, flow, str(nxt["nonce"]), data.replace(b"cause=c", b"cause=again"), env)
    expect(code == 1 and "already written" in err, f"a duplicate reply exited {code}: {err}")
    expect(records.read_bytes() == data, "a duplicate reply overwrote the records")
    _, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "notification", extra_env=env)
    verb, _ = next_line(out)
    expect(verb == "ASK", f"the written records did not lead to the batch ASK: {verb} {err}")


def test_assessor_spawn_failed_then_resume(base: Path) -> None:
    home, flow, env, _, nxt = to_landed_verify(base, assess=True)
    run_cli(home, "next", "--flow", str(flow), "--after", "spawn-launched", extra_env=env)
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "spawn-failed", extra_env=env)
    verb, _ = next_line(out)
    expect(code == 0 and verb == "STOP", f"an assessor spawn-failed printed {verb}: {err}")
    code, _, err = run_cli(home, "clear-live", "--flow", str(flow), "--reason", "x", extra_env=env)
    expect(code != 0 and "no live record" in err, f"clear-live on a spawn-failed pause exited {code}: {err}")
    code, _, err = run_cli(home, "recover", "--flow", str(flow), "--choice", "resume", extra_env=env)
    expect(code == 0, f"recover resume exited {code}: {err}")
    expect(state_of(flow)["status"] == "running", f"resume did not reopen the flow: {state_of(flow)['status']}")
    code, out, err = run_cli(home, "next", "--flow", str(flow), extra_env=env)
    verb, path = next_line(out)
    spawn = load_json(path)
    expect(verb == "SPAWN" and spawn["step"] == "assess" and spawn["agent_type"] == "flow-assessor"
           and spawn["nonce"] != nxt["nonce"], f"resume did not re-spawn the assessor fresh: {verb} {spawn} {err}")


def test_duplicate_spawn_failed_keeps_the_paused_stop(base: Path) -> None:
    """D5: a second spawn-failed after an assessor spawn-failed leaves the flow paused on the same spawn-failed stop."""
    home, flow, env, _, _ = to_landed_verify(base, assess=True)
    run_cli(home, "next", "--flow", str(flow), "--after", "spawn-launched", extra_env=env)
    run_cli(home, "next", "--flow", str(flow), "--after", "spawn-failed", extra_env=env)
    first = state_of(flow)
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "spawn-failed", extra_env=env)
    verb, _ = next_line(out)
    second = state_of(flow)
    expect(code == 0 and verb == "STOP", f"a duplicate spawn-failed printed {verb}: {err}")
    expect(second["status"] == "paused", f"a duplicate spawn-failed left status {second['status']!r}, expected paused")
    expect(str(second["stop_reason"].get("verify", "")).startswith("spawn-failed:"), f"stop reason: {second['stop_reason']}")
    expect(second["stop_reason"] == first["stop_reason"], "a duplicate spawn-failed rewrote the stop reason")


CHECKS = [
    ("2: nothing outstanding skips the assessor", test_nothing_outstanding_skips),
    ("3: spawn, wait, ask, answer, advance", test_spawn_wait_ask_answer_advance),
    ("4: bad assessor records halt", test_bad_records_halt),
    ("5: --item without a parked batch is refused", test_item_answer_without_batch_refused),
    ("6: assessor wait-timeout stops; clear-live re-spawns", test_assessor_wait_timeout_then_clear_live),
    ("7: plain answer refuses assess batch", test_plain_answer_refuses_assess_batch),
    ("8: item id with a space is shell-quoted in the answer command", test_item_id_with_a_space_is_shell_quoted_in_the_answer_command),
    ("9: assess SPAWN directive carries no reply delimiter", test_assess_spawn_directive_carries_no_reply_delimiter),
    ("10: spawn-failed on the assess SPAWN is idempotent", test_spawn_failed_on_assess_spawn_is_idempotent),
    ("11: D26 assess off advances verify to ship", test_assess_off_advances_verify_to_ship),
    ("12: assess-reply CLI: live nonce, wrong nonce, duplicate", test_assess_reply_cli),
    ("13: assessor spawn-failed refuses clear-live and resumes", test_assessor_spawn_failed_then_resume),
    ("14: D5 a duplicate spawn-failed stays paused on spawn-failed", test_duplicate_spawn_failed_keeps_the_paused_stop),
]


def main() -> None:
    h = Harness("FLOW DRIVER ASSESS LOOP")
    for name, fn in CHECKS:
        check(h, name, fn)
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
