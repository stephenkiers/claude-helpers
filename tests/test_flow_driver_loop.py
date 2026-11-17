#!/usr/bin/env python3
"""
Harness cases for the v2 driver Layer 2 step loop (#486, ADR-0026).

Covers the cases from the Layer 2 plan's Tests list that the CLI, the NEXT line, the directive files,
state.json and driver.log can express without importing implementation internals:

- Test 1: happy path worktree..verify, ship SPAWN, then merge STOP once gh reports an OPEN PR.
- Test 2: double completion (a repeated next with no disk change) returns the same directive and nonce.
- Test 3: a deleted directive file is recreated with the same nonce on the next call.
- Test 4: dead agent: WAIT, then clear-live, then a SPAWN with a new attempt and nonce.
- Test 7: an unrecognised --after label is STOP unknown-event.
- Test 10: start --auto-merge is refused before any flow state exists.
- Test 11: the prompt file name carries the current nonce.
- Test 12: a receipt for a cleared attempt is never landed.
- Test 24: gh error and CLOSED PR are STOPs at the ship guard.
- Test 35: abandon refuses a live record; an abandoned flow is re-abandoned with a message.
- Test 38: the stub doc is under 150 lines, is experimental and carries the one-at-a-time next rule.
- Test 40: stop reason round trip for every StopCode.
- Test 41: wait-timeout is a STOP naming clear-live, repeated with no new attempt.

Not covered here (see the report): Tests 5, 6, 8, 9, 13-23, 25-34, 36, 37, 39 need fake-clock hooks,
recovery commands, a v1 fixture or a real worktree and are listed as gaps in the author's report.

Run with: python3 tests/test_flow_driver_loop.py
"""

import json
import re
import sys
import tempfile
import typing
from pathlib import Path
from typing import Any, Callable, Dict

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from _flow_driver_loop_helpers import (  # noqa: E402
    FakeGh,
    directive_text,
    drive_happy_path,
    load_json,
    log_lines,
    make_home,
    next_line,
    pending_of,
    run_cli,
    start_flow,
    state_of,
    write_receipt,
)
from _test_harness import REPO_ROOT, Harness  # noqa: E402

DOC = REPO_ROOT / "commands" / "expert-flow-v2.md"
HAPPY_ORDER = ["worktree", "plan", "track", "implement", "review", "fix", "verify", "ship"]
CLOSED_HEAD = "a" * 40


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check(h: Harness, name: str, fn: Callable[[Path], None], base: Path) -> None:
    try:
        fn(base)
    except AssertionError as exc:
        h.test_result(name, False, str(exc) or "assertion failed")
    except Exception as exc:
        h.test_result(name, False, f"{type(exc).__name__}: {exc}")
    else:
        h.test_result(name, True)


def fresh(base: Path) -> Path:
    return Path(tempfile.mkdtemp(dir=str(base)))


def gh_open_env(gh: FakeGh, head: str) -> Dict[str, str]:
    gh.json_file.write_text(
        json.dumps({"number": 7, "state": "OPEN", "headRefOid": head}), encoding="utf-8"
    )
    gh.mode = "open"
    return gh.env()


# ---------------------------------------------------------------------------
# Test 1: happy path to the merge STOP
# ---------------------------------------------------------------------------

def test_01_happy_path_reaches_ship_then_merge_stop(base: Path) -> None:
    home = make_home(base)
    gh = FakeGh(base, mode="nopr")
    env = gh.env()
    flow, first = start_flow(home, extra_env=env)

    def open_pr_before_final_next() -> None:
        env.update(gh_open_env(gh, CLOSED_HEAD))

    verb, directive, spawned = drive_happy_path(
        home, flow, first, env, before_next={"ship": open_pr_before_final_next}
    )
    expect(spawned == HAPPY_ORDER, f"steps spawned were {spawned}, expected {HAPPY_ORDER}")
    expect(verb == "STOP", f"after ship the next directive was {verb}, expected STOP")
    text = directive_text(directive)
    expect("merge-not-built: " in text, "merge STOP reason does not start merge-not-built: ")
    expect("merge gate not built (Layer 3)" in text, "merge STOP lacks the Layer 3 detail")
    expect(not any("merge" in step for step in spawned), "a merge step was spawned")


# ---------------------------------------------------------------------------
# Tests 2, 3, 4: double completion, crash after NEXT, dead agent
# ---------------------------------------------------------------------------

def test_02_repeated_next_returns_same_directive_and_nonce(base: Path) -> None:
    home = make_home(base)
    flow, _ = start_flow(home)
    before = pending_of(flow)
    expect(before is not None, "no live record after start")
    outs = []
    for _ in range(2):
        code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "notification")
        expect(code == 0, f"next exited {code}: {err}")
        outs.append(out)
    expect(outs[0] == outs[1], f"repeated next printed different lines: {outs!r}")
    after = pending_of(flow)
    expect(after["nonce"] == before["nonce"], "a repeated next changed the nonce")
    expect(after["attempt"] == before["attempt"], "a repeated next opened an attempt")
    verb, directive = next_line(outs[0])
    expect(verb == "WAIT", f"a live record with no receipt printed {verb}, expected WAIT")
    expect(directive.name in outs[1], "the two calls named different directive files")


def test_03_deleted_directive_is_recreated_with_same_nonce(base: Path) -> None:
    home = make_home(base)
    flow, _ = start_flow(home)
    nonce = pending_of(flow)["nonce"]
    code, out, _ = run_cli(home, "next", "--flow", str(flow), "--after", "notification")
    verb, directive = next_line(out)
    directive.unlink()
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "notification")
    expect(code == 0, f"next exited {code}: {err}")
    _, again = next_line(out)
    expect(again.exists(), "the deleted directive file was not recreated")
    expect(load_json(again).get("nonce") == nonce, "the recreated directive has a different nonce")
    expect(pending_of(flow)["nonce"] == nonce, "the nonce changed on re-render")


def test_04_dead_agent_waits_then_clear_live_respawns_with_new_attempt(base: Path) -> None:
    home = make_home(base)
    flow, _ = start_flow(home)
    first = pending_of(flow)
    code, out, _ = run_cli(home, "next", "--flow", str(flow), "--after", "notification")
    verb, _ = next_line(out)
    expect(verb == "WAIT", f"dead agent printed {verb}, expected WAIT")

    code, _, err = run_cli(home, "clear-live", "--flow", str(flow), "--reason", "dead agent")
    expect(code == 0, f"clear-live exited {code}: {err}")
    joined = "\n".join(log_lines(flow))
    expect(first["nonce"] in joined, "the log does not record the cleared nonce")

    code, out, _ = run_cli(home, "next", "--flow", str(flow), "--after", "start")
    verb, _ = next_line(out)
    expect(verb == "SPAWN", f"after clear-live printed {verb}, expected SPAWN")
    second = pending_of(flow)
    expect(second["attempt"] == first["attempt"] + 1, "the re-spawn did not open attempt N+1")
    expect(second["nonce"] != first["nonce"], "the re-spawn reused the nonce")


# ---------------------------------------------------------------------------
# Test 7: unknown event label
# ---------------------------------------------------------------------------

def test_07_unknown_event_label_is_stop(base: Path) -> None:
    home = make_home(base)
    flow, _ = start_flow(home)
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "no-such-event")
    expect(code == 0, f"next exited {code}: {err}")
    verb, directive = next_line(out)
    expect(verb == "STOP", f"unknown label printed {verb}, expected STOP")
    expect("unknown-event: " in directive_text(directive), "STOP reason does not start unknown-event: ")


# ---------------------------------------------------------------------------
# Test 10: --auto-merge refused
# ---------------------------------------------------------------------------

def test_10_auto_merge_is_refused_before_any_state(base: Path) -> None:
    home = make_home(base)
    flow = home / ".claude" / "flows-v2" / "owner_name" / "486-refused-abc123"
    code, out, err = run_cli(
        home, "start", "--flow", str(flow), "--auto-merge", "--repo", "owner/name", "--mode", "interactive",
        "--wait-mode", "end-turn", "--step-minutes", "1",
    )
    expect("STOP auto-merge-refused: " in out + err, f"--auto-merge did not print STOP auto-merge-refused: {out!r} {err!r}")
    root = home / ".claude" / "flows-v2"
    created = list(root.rglob("state.json")) if root.exists() else []
    expect(not created, f"--auto-merge created flow state: {created}")


# ---------------------------------------------------------------------------
# Test 11 and 12: nonce naming and stale ATTEMPT after clear-live
# ---------------------------------------------------------------------------

def test_11_prompt_file_is_named_with_attempt_and_current_nonce(base: Path) -> None:
    home = make_home(base)
    flow, first = start_flow(home)
    nonce = pending_of(flow)["nonce"]
    prompts = sorted((flow / "prompts").glob("*.md")) if (flow / "prompts").exists() else []
    pattern = re.compile(r"^[a-z]+-a1-" + re.escape(nonce) + r"\.md$")
    expect(any(pattern.match(p.name) for p in prompts),
           f"no prompt named <step>-a1-{nonce}.md; found {[p.name for p in prompts]}")


def test_12_receipt_for_cleared_attempt_is_never_landed(base: Path) -> None:
    home = make_home(base)
    flow, _ = start_flow(home)
    old_nonce = pending_of(flow)["nonce"]
    run_cli(home, "clear-live", "--flow", str(flow), "--reason", "stale test")
    code, out, _ = run_cli(home, "next", "--flow", str(flow), "--after", "start")
    verb, _ = next_line(out)
    expect(verb == "SPAWN", f"re-spawn printed {verb}")
    write_receipt(flow, "worktree", 1)  # the attempt that was cleared
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "notification")
    expect(code == 0, f"next exited {code}: {err}")
    verb, directive = next_line(out)
    expect(verb in ("WAIT", "SPAWN"), f"a stale receipt printed {verb}")
    expect(not (verb == "SPAWN" and load_json(directive)["step"] != "worktree"),
           "a stale receipt advanced the flow")
    expect(any(old_nonce in line for line in log_lines(flow)), "the stale event was not logged with its nonce")


# ---------------------------------------------------------------------------
# Test 24: ship guard gh error and CLOSED PR
# ---------------------------------------------------------------------------

def _ship_guard_case(base: Path, mode: str, json_text: str) -> Any:
    home = make_home(base)
    gh = FakeGh(base, mode=mode, json_text=json_text)
    env = gh.env()
    flow, first = start_flow(home, extra_env=env)

    def set_gh() -> None:
        gh.mode = mode
        env.update(gh.env())

    verb, directive, spawned = drive_happy_path(
        home, flow, first, env, before_next={"verify": set_gh}
    )
    return verb, directive, spawned


def test_24a_gh_error_at_ship_guard_is_stop(base: Path) -> None:
    verb, directive, spawned = _ship_guard_case(base, "error", "{}")
    expect(verb == "STOP", f"gh error printed {verb}, expected STOP")
    expect("gh-error: " in directive_text(directive), "STOP reason does not start gh-error: ")
    expect("ship" not in spawned, "ship was spawned after a gh error")


def test_24b_closed_pr_is_stop_naming_the_pr(base: Path) -> None:
    body = json.dumps({"number": 7, "state": "CLOSED", "headRefOid": CLOSED_HEAD})
    verb, directive, spawned = _ship_guard_case(base, "closed", body)
    expect(verb == "STOP", f"CLOSED PR printed {verb}, expected STOP")
    expect("PR #7 closed unmerged" in directive_text(directive), "STOP does not name PR #7 closed unmerged")
    expect("ship" not in spawned, "ship was re-spawned for a CLOSED PR")


# ---------------------------------------------------------------------------
# Test 35: abandon
# ---------------------------------------------------------------------------

def test_35_abandon_refuses_live_then_abandons_once(base: Path) -> None:
    home = make_home(base)
    flow, _ = start_flow(home)
    code, out, err = run_cli(home, "abandon", "--flow", str(flow))
    text = out + err
    if out.startswith("NEXT: "):
        _, directive = next_line(out)
        text += directive_text(directive)
    expect("clear-live" in text, "abandon on a live record does not name clear-live")
    expect(state_of(flow).get("status") != "abandoned", "abandon changed status on a live record")

    run_cli(home, "clear-live", "--flow", str(flow), "--reason", "abandon test")
    code, out, err = run_cli(home, "abandon", "--flow", str(flow))
    expect(code == 0, f"abandon exited {code}: {err}")
    expect(state_of(flow).get("status") == "abandoned", "status did not become abandoned")
    code, out, err = run_cli(home, "abandon", "--flow", str(flow))
    text = out + err
    if out.startswith("NEXT: "):
        _, directive = next_line(out)
        text += directive_text(directive)
    expect("flow already abandoned" in text, "a second abandon did not say flow already abandoned")


# ---------------------------------------------------------------------------
# Test 38: stub doc
# ---------------------------------------------------------------------------

def test_38_stub_doc_limits_and_rules(base: Path) -> None:
    text = DOC.read_text(encoding="utf-8")
    lines = text.splitlines()
    expect(len(lines) < 150, f"stub doc is {len(lines)} lines, must be under 150")
    expect("experimental" in text.lower(), "stub doc lost the experimental label")
    expect(re.search(r"one `driver next` at a time", text) is not None
           or ("driver next" in text and "at a time" in text),
           "stub doc lacks the one-at-a-time driver next rule")
    for line in lines:
        lowered = line.lower()
        if "state.json" in lowered or "answers/" in lowered or "fleet-ask.json" in lowered:
            if "write" in lowered and not re.search(r"\b(never|not|do not|don't)\b", lowered):
                raise AssertionError(f"stub doc tells the model to write a boundary file: {line!r}")


# ---------------------------------------------------------------------------
# Test 40: stop reason round trip
# ---------------------------------------------------------------------------

def test_40_stop_reason_round_trip_for_every_code(base: Path) -> None:
    sys.path.insert(0, str(REPO_ROOT))
    from scripts.flow_driver.steps import StopCode, parse_stop_reason, stop_reason

    codes = typing.get_args(StopCode)
    expect(len(codes) > 0, "StopCode has no members")
    for code in codes:
        detail = "detail with: colons and spaces"
        parsed = parse_stop_reason(stop_reason(code, detail))
        expect(parsed == (code, detail), f"round trip for {code} returned {parsed!r}")


# ---------------------------------------------------------------------------
# Test 41: wait-timeout
# ---------------------------------------------------------------------------

def test_41_wait_timeout_is_stop_naming_clear_live_and_repeats(base: Path) -> None:
    home = make_home(base)
    flow, _ = start_flow(home)
    first = pending_of(flow)
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "wait-timeout")
    expect(code == 0, f"next exited {code}: {err}")
    verb, directive = next_line(out)
    expect(verb == "STOP", f"wait-timeout printed {verb}, expected STOP")
    text = directive_text(directive)
    expect("wait-timeout: " in text, "STOP reason does not start wait-timeout: ")
    expect("clear-live" in text, "STOP does not name clear-live")

    code, out, _ = run_cli(home, "next", "--flow", str(flow), "--after", "wait-timeout")
    verb2, directive2 = next_line(out)
    expect(verb2 == "STOP" and "wait-timeout: " in directive_text(directive2), "the repeated call was not the same STOP")
    expect(pending_of(flow)["attempt"] == first["attempt"], "the repeated wait-timeout opened an attempt")

    run_cli(home, "clear-live", "--flow", str(flow), "--reason", "wait-timeout recovery")
    code, out, _ = run_cli(home, "next", "--flow", str(flow), "--after", "start")
    verb3, _ = next_line(out)
    expect(verb3 == "SPAWN", f"after clear-live printed {verb3}, expected SPAWN")
    expect(pending_of(flow)["attempt"] == first["attempt"] + 1, "the next SPAWN did not use attempt N+1")


SECTIONS = [
    ("[Happy path and merge pin]", [
        ("1: happy path worktree..ship, then merge-not-built STOP on OPEN PR", test_01_happy_path_reaches_ship_then_merge_stop),
    ]),
    ("[Double completion, crash re-render, dead agent]", [
        ("2: repeated next returns the same directive and nonce", test_02_repeated_next_returns_same_directive_and_nonce),
        ("3: deleted directive is recreated with the same nonce", test_03_deleted_directive_is_recreated_with_same_nonce),
        ("4: dead agent WAITs, clear-live logs the nonce, next SPAWN is attempt N+1", test_04_dead_agent_waits_then_clear_live_respawns_with_new_attempt),
    ]),
    ("[Events and start refusal]", [
        ("7: unknown --after label is STOP unknown-event", test_07_unknown_event_label_is_stop),
        ("10: --auto-merge is refused before any flow state", test_10_auto_merge_is_refused_before_any_state),
    ]),
    ("[Nonce and stale receipts]", [
        ("11: prompt file carries attempt and current nonce", test_11_prompt_file_is_named_with_attempt_and_current_nonce),
        ("12: receipt for a cleared attempt is never landed", test_12_receipt_for_cleared_attempt_is_never_landed),
    ]),
    ("[Ship guard]", [
        ("24a: gh error at ship guard is STOP gh-error", test_24a_gh_error_at_ship_guard_is_stop),
        ("24b: CLOSED PR is STOP naming PR #7 closed unmerged, no re-ship", test_24b_closed_pr_is_stop_naming_the_pr),
    ]),
    ("[Abandon, stub doc, stop codes, wait-timeout]", [
        ("35: abandon refuses a live record, then abandons once", test_35_abandon_refuses_live_then_abandons_once),
        ("38: stub doc is under 150 lines, experimental, one-at-a-time rule", test_38_stub_doc_limits_and_rules),
        ("40: stop reason round trip for every StopCode", test_40_stop_reason_round_trip_for_every_code),
        ("41: wait-timeout STOP names clear-live and repeats without an attempt", test_41_wait_timeout_is_stop_naming_clear_live_and_repeats),
    ]),
]


def main() -> None:
    h = Harness("FLOW DRIVER v2 LAYER 2 STEP LOOP")
    with tempfile.TemporaryDirectory(prefix="flow-driver-loop-") as tmp:
        base = Path(tmp)
        for title, checks in SECTIONS:
            print(title)
            for name, fn in checks:
                check(h, name, fn, base)
            print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
