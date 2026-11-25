#!/usr/bin/env python3
"""
Harness cases for the v2 driver Layer 2 step loop (#486, ADR-0026).

Covers the cases from the Layer 2 plan's Tests list that the CLI, the NEXT line, the directive files,
state.json and driver.log can express without importing implementation internals:

- Test 1: happy path worktree..verify, ship SPAWN, then the merge-gate question parks once gh reports an OPEN PR.
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

import dataclasses
import json
import re
import subprocess
import sys
import tempfile
import typing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Tuple

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
from scripts.flow_driver import receipts, recovery, relay  # noqa: E402
from scripts.flow_driver.state import ReceiptFingerprint, mutating  # noqa: E402
from test_flow_driver_assess_loop import to_landed_verify  # noqa: E402
from test_flow_driver_audit_loop import to_landed_plan  # noqa: E402

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


def worktree_head(home: Path) -> str:
    """The real HEAD of the fixture worktree: the SHA a ship guard cross-check compares against."""
    out = subprocess.run(["git", "-C", str(home / "wt"), "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
    return out.stdout.strip()


def gh_open_env(gh: FakeGh, head: str) -> Dict[str, str]:
    gh.json_file.write_text(
        json.dumps({"number": 7, "state": "OPEN", "headRefOid": head}), encoding="utf-8"
    )
    gh.mode = "open"
    return gh.env()


# ---------------------------------------------------------------------------
# Test 1: happy path to the merge STOP
# ---------------------------------------------------------------------------

def test_01_landed_ship_parks_the_merge_gate_question(base: Path) -> None:
    home = make_home(base)
    gh = FakeGh(base, mode="nopr")
    env = gh.env()
    flow, first = start_flow(home, extra_env=env)

    def open_pr_before_final_next() -> None:
        env.update(gh_open_env(gh, worktree_head(home)))

    verb, directive, spawned = drive_happy_path(
        home, flow, first, env, before_next={"ship": open_pr_before_final_next}
    )
    expect(spawned == HAPPY_ORDER, f"steps spawned were {spawned}, expected {HAPPY_ORDER}")
    expect(verb == "ASK", f"after ship the next directive was {verb}, expected ASK")
    text = directive_text(directive)
    expect("merge-gate" in text, "landed ship did not park the merge-gate question")
    for option in ["Merge", "Hold", "Take over"]:
        expect(option in text, f"merge-gate question lacks option {option!r}")
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
    gh = FakeGh(base, mode=mode, json_text=json_text.replace(CLOSED_HEAD, worktree_head(home)))
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


def test_zero_unit_stop_codes_parse_and_are_listed(base: Path) -> None:
    """#526: zero-unit-plan-incomplete is a typed stop code; test-only-plan stays parseable (legacy)."""
    from scripts.flow_driver import receipts
    from scripts.flow_driver.steps import StopCode, parse_stop_reason

    expect(parse_stop_reason("test-only-plan: legacy detail") == ("test-only-plan", "legacy detail"),
           "legacy test-only-plan no longer parses")
    got = parse_stop_reason("zero-unit-plan-incomplete: no Round 2 tests")
    expect(got == ("zero-unit-plan-incomplete", "no Round 2 tests"), f"parse returned {got!r}")
    stop_codes = set(typing.get_args(StopCode))
    expect("zero-unit-plan-incomplete" in stop_codes, "zero-unit-plan-incomplete missing from StopCode")
    expect("zero-unit-plan-incomplete" in receipts.TEST_PATH_CODES, "zero-unit-plan-incomplete missing from TEST_PATH_CODES")
    missing = set(receipts.TEST_PATH_CODES) - stop_codes
    expect(not missing, f"TEST_PATH_CODES members not in StopCode: {sorted(missing)}")


def test_no_command_doc_emits_legacy_test_only_plan(base: Path) -> None:
    """#526: nothing under commands/ names the retired test-only-plan stop code."""
    offenders = [str(p.relative_to(REPO_ROOT)) for p in sorted((REPO_ROOT / "commands").rglob("*.md"))
                 if "test-only-plan" in p.read_text(encoding="utf-8")]
    expect(not offenders, f"commands/ files still contain test-only-plan: {offenders}")


# ---------------------------------------------------------------------------
# #549: a parked or resumed step's incomplete placeholder receipt is held, never re-run.
# Numbering: 549-05 and 549-06 live in tests/test_flow_driver_receipts.py (classify and settle_in_progress), so the
# numbers below skip 05 and 06. 549-10a and 549-10b are two cases of one scenario, not a gap.
# The driver reads wall-clock time, so "the clock advanced" is simulated by back-dating receipt_first_seen
# in state.json to 120 seconds before the call (past RERUN_QUIET_SECONDS, which is 60).
# ---------------------------------------------------------------------------

FIX_QUESTION = "questions/fix-01.json"
PLACEHOLDER = f"ATTEMPT: 1\n<!-- awaiting-answers: {FIX_QUESTION} -->\n"


def _backdate(flow: Path, seconds: int = 120) -> None:
    state = state_of(flow)
    pending = state.get("pending_spawn")
    if not pending:
        return
    old = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    pending["receipt_first_seen"] = old.strftime("%Y-%m-%dT%H:%M:%SZ")
    (flow / "state.json").write_text(json.dumps(state), encoding="utf-8")


def _set_pending(flow: Path, **fields: Any) -> None:
    with mutating(flow) as locked:
        current = locked.require_state()
        pending = dataclasses.replace(current.pending_spawn, **fields)  # type: ignore[type-var]
        locked.commit(dataclasses.replace(current, pending_spawn=pending))


def _drive_to_fix(base: Path) -> Tuple[Path, Path, Dict[str, str], Dict[str, Any]]:
    """Start a flow and answer worktree..review with OK receipts. Returns (home, flow, env, the fix SPAWN)."""
    home = make_home(base)
    env = FakeGh(base, mode="nopr").env()
    flow, cur = start_flow(home, extra_env=env)
    for step in HAPPY_ORDER[:5]:
        extra = f"WORKTREE: {home / 'wt'}\n" if step == "worktree" else ""
        write_receipt(flow, step, cur["attempt"], extra=extra)
        code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "notification", extra_env=env)
        expect(code == 0, f"next exited {code} after {step}: {err}")
        verb, directive = next_line(out)
        expect(verb == "SPAWN", f"after {step} the verb was {verb}, expected SPAWN")
        cur = load_json(directive)
    expect(cur["step"] == "fix", f"the loop stopped at {cur['step']}, expected fix")
    return home, flow, env, cur


def _next(home: Path, flow: Path, env: Dict[str, str], after: Any = None) -> Tuple[str, Path]:
    args = ["next", "--flow", str(flow)] + (["--after", after] if after else [])
    code, out, err = run_cli(home, *args, extra_env=env)
    expect(code == 0, f"next --after {after} exited {code}: {err}")
    return next_line(out)


def _write_question(flow: Path) -> None:
    (flow / "questions").mkdir(exist_ok=True)
    (flow / "questions" / "fix-01.json").write_text(json.dumps({"summary": "Pick one", "options": ["A", "B"]}), encoding="utf-8")


def _write_placeholder(flow: Path, text: str = PLACEHOLDER, question: bool = True) -> Path:
    """The placeholder receipt; with a question file present the driver parks on the next look (ASK)."""
    if question:
        _write_question(flow)
    path = receipts.receipt_path(flow, "fix")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _park_fix(flow: Path) -> None:
    _write_question(flow)
    with mutating(flow) as locked:
        relay.park(locked, flow, "fix", 1, "step", nn=1)


def _answer(home: Path, flow: Path, env: Dict[str, str]) -> None:
    code, _, err = run_cli(home, "answer", "--flow", str(flow), "--text", "A", extra_env=env)
    expect(code == 0, f"answer exited {code}: {err}")


def _resumed_fix(home: Path, flow: Path, env: Dict[str, str]) -> None:
    """Park the fix question (unless the driver already did), answer it and take the RESUME. The record is now resumed."""
    if pending_of(flow)["state"] != "parked-awaiting-answer":
        _park_fix(flow)
    verb, _ = _next(home, flow, env)
    expect(verb == "ASK", f"the parked fix question printed {verb}, expected ASK")
    _answer(home, flow, env)
    verb, _ = _next(home, flow, env, "human-continue")
    expect(verb == "RESUME", f"after the answer the verb was {verb}, expected RESUME")


def _no_malformed_archive(flow: Path) -> None:
    expect(not (flow / "archive" / "fix" / "a1.malformed.md").exists(), "a1.malformed.md was archived")
    expect(not any("receipt-malformed-rerun" in line for line in log_lines(flow)), "receipt-malformed-rerun was logged")


def test_549_01_ticket_regression_end_turn(base: Path) -> None:
    home, flow, env, _ = _drive_to_fix(base)
    _write_placeholder(flow)
    verb, _ = _next(home, flow, env, "spawn-launched")
    expect(verb == "ASK", f"the launched fix with a question printed {verb}, expected ASK (driver parks)")
    _resumed_fix(home, flow, env)
    verb, _ = _next(home, flow, env, "resume-sent")
    expect(verb == "WAIT", f"resume-sent printed {verb}, expected WAIT")
    for label in ["notification", "notification", "wait-settled"]:
        _backdate(flow)
        verb, _ = _next(home, flow, env, label)
        expect(verb == "WAIT", f"after {label} with 120s elapsed the verb was {verb}, expected WAIT (held, no rerun)")
    _no_malformed_archive(flow)
    pending = pending_of(flow)
    expect(pending["attempt"] == 1 and pending["state"] == "resumed", f"the record is {pending}, expected attempt 1 resumed")
    write_receipt(flow, "fix", 1)
    _backdate(flow)
    _next(home, flow, env, "notification")
    expect((flow / "archive" / "fix" / "a1.landed.md").exists(), "the final attempt-1 receipt was not archived as landed")
    pending = pending_of(flow)
    expect(pending is None or pending["step"] != "fix",
           f"after the final receipt the record is {pending}, expected a roll-forward past fix")


def test_549_02_resume_clears_fingerprints(base: Path) -> None:
    home, flow, env, _ = _drive_to_fix(base)
    _write_placeholder(flow, question=False)
    _next(home, flow, env, "spawn-launched")
    _next(home, flow, env)  # a bare look stamps receipt_first_seen
    before = pending_of(flow)
    expect(before["receipt_seen"] is not None and before["receipt_first_seen"] is not None,
           f"precondition failed: the record before resume is {before}")
    _resumed_fix(home, flow, env)
    after = pending_of(flow)
    expect(after["state"] == "resumed", f"the record is {after['state']}, expected resumed")
    expect(after["receipt_seen"] is None, f"receipt_seen survived the resume: {after['receipt_seen']}")
    expect(after["receipt_first_seen"] is None, f"receipt_first_seen survived the resume: {after['receipt_first_seen']}")


def test_549_03_parked_and_unanswered_is_held(base: Path) -> None:
    home, flow, env, _ = _drive_to_fix(base)
    placeholder = _write_placeholder(flow)
    _next(home, flow, env, "spawn-launched")
    _park_fix(flow)
    parked = pending_of(flow)
    expect(parked["state"] == "parked-awaiting-answer", f"the record is {parked['state']}, expected parked-awaiting-answer")
    fingerprint_at_park = parked["receipt_seen"]
    directive_names = set()
    for _ in range(3):
        _backdate(flow)
        code, out, err = run_cli(home, "next", "--flow", str(flow), extra_env=env)
        expect(code == 0, f"bare next on a parked record exited {code}: {err}")
        verb, directive = next_line(out)
        expect(verb == "ASK", f"a parked unanswered record printed {verb}, expected ASK")
        expect("fix-01" in directive_text(directive), "the ASK does not name the parked question file")
        directive_names.add(directive.name)
    expect(len(directive_names) == 1, f"the ASK directive changed between calls: {sorted(directive_names)}")
    _no_malformed_archive(flow)
    expect(placeholder.exists() and "awaiting-answers" in placeholder.read_text(encoding="utf-8"),
           "the placeholder is no longer at the step receipt path")
    expect(pending_of(flow)["receipt_seen"] == fingerprint_at_park, "a fingerprint was stored while parked")


def test_549_04_old_state_file_with_stale_stamps_is_held(base: Path) -> None:
    home, flow, env, _ = _drive_to_fix(base)
    _write_placeholder(flow)
    _next(home, flow, env, "spawn-launched")
    fingerprint = pending_of(flow)["receipt_seen"]
    _resumed_fix(home, flow, env)
    # Simulate a state.json written before the fix: a resumed record that still carries the placeholder's stamps.
    old = (datetime.now(timezone.utc) - timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _set_pending(flow, receipt_seen=ReceiptFingerprint(**fingerprint), receipt_first_seen=old)
    verb, _ = _next(home, flow, env, "notification")
    expect(verb == "WAIT", f"a resumed record with stale stamps printed {verb}, expected WAIT")
    _no_malformed_archive(flow)


def test_549_07_resume_command_on_resumed_placeholder_waits(base: Path) -> None:
    home, flow, env, _ = _drive_to_fix(base)
    _write_placeholder(flow)
    _next(home, flow, env, "spawn-launched")
    _resumed_fix(home, flow, env)
    for _ in range(3):
        _backdate(flow)
        code, out, err = run_cli(home, "resume", "--flow", str(flow), extra_env=env)
        expect(code == 0, f"resume exited {code}: {err}")
        expect("outcome=wait" in out, f"resume printed {out.strip()!r}, expected outcome=wait")
        expect("outcome=rerun" not in out, "resume re-ran a resumed placeholder")
    _no_malformed_archive(flow)


def test_549_08_multi_marker_placeholder_is_held(base: Path) -> None:
    home, flow, env, _ = _drive_to_fix(base)
    _write_placeholder(flow)
    _next(home, flow, env, "spawn-launched")
    _resumed_fix(home, flow, env)
    _write_placeholder(
        flow,
        "ATTEMPT: 1\n<!-- awaiting-answers: questions/fix-01.json -->\n<!-- awaiting-answers: questions/fix-02.json -->\n",
    )
    for _ in range(2):
        _backdate(flow)
        verb, _ = _next(home, flow, env, "notification")
        expect(verb == "WAIT", f"a two-marker resumed receipt printed {verb}, expected WAIT")
    _no_malformed_archive(flow)
    expect(pending_of(flow)["state"] == "resumed", "the two-marker receipt moved the record off resumed")


def test_549_09_landed_and_stale_still_win_while_resumed(base: Path) -> None:
    home, flow, env, _ = _drive_to_fix(base)
    _write_placeholder(flow)
    _next(home, flow, env, "spawn-launched")
    _resumed_fix(home, flow, env)
    write_receipt(flow, "fix", 2)
    _next(home, flow, env, "notification")
    expect(any("stale-nonce" in line for line in log_lines(flow)), "the attempt-2 receipt was not logged as stale-nonce")
    expect(pending_of(flow)["state"] == "resumed", f"a stale receipt moved the record to {pending_of(flow)['state']}")
    write_receipt(flow, "fix", 1)
    _backdate(flow)
    _next(home, flow, env, "notification")
    expect((flow / "archive" / "fix" / "a1.landed.md").exists(), "the attempt-1 receipt did not land while resumed")


def test_549_10_spawned_non_placeholder_malformed_still_reruns(base: Path) -> None:
    home, flow, env, _ = _drive_to_fix(base)
    receipts.receipt_path(flow, "fix").write_text("ATTEMPT: 1\npartial work, no step-end marker\n", encoding="utf-8")
    _next(home, flow, env)  # a bare look first stores the fingerprint: the receipt must be stable before a notification re-runs it
    verb, directive = _next(home, flow, env, "notification")
    expect(verb == "SPAWN" and load_json(directive)["attempt"] == 2, f"a malformed spawned receipt printed {verb}, expected SPAWN attempt 2")
    expect((flow / "archive" / "fix" / "a1.malformed.md").exists(), "the malformed attempt-1 receipt was not archived")
    expect(any("receipt-malformed-rerun" in line for line in log_lines(flow)), "no receipt-malformed-rerun was logged")
    receipts.receipt_path(flow, "fix").write_text("ATTEMPT: 2\npartial again\n", encoding="utf-8")
    _next(home, flow, env)  # stable again after the re-run: a bare look stores the fingerprint first
    verb, _ = _next(home, flow, env, "notification")
    expect(verb == "STOP" and state_of(flow)["stop_reason"]["fix"].startswith("malformed-cap:"),
           f"a second malformed receipt in a row printed {verb}, expected STOP malformed-cap")


def test_549_10b_spawned_bare_look_waits_then_reruns_after_quiet_interval(base: Path) -> None:
    home, flow, env, _ = _drive_to_fix(base)
    receipts.receipt_path(flow, "fix").write_text("ATTEMPT: 1\npartial work, no step-end marker\n", encoding="utf-8")
    verb, _ = _next(home, flow, env)
    expect(verb == "WAIT", f"the first bare look printed {verb}, expected WAIT")
    verb, _ = _next(home, flow, env)
    expect(verb == "WAIT", f"a second bare look inside the quiet interval printed {verb}, expected WAIT")
    _backdate(flow)
    verb, directive = _next(home, flow, env)
    expect(verb == "SPAWN" and load_json(directive)["attempt"] == 2,
           f"a bare look past the quiet interval printed {verb}, expected SPAWN attempt 2")


def test_549_11_wait_directive_carries_require_change(base: Path) -> None:
    home, flow, env, _ = _drive_to_fix(base)
    _write_placeholder(flow, question=False)
    _, directive = _next(home, flow, env, "spawn-launched")
    expect(load_json(directive)["require_change"] is False, "the WAIT for a spawned record must have require_change false")
    _resumed_fix(home, flow, env)
    verb, directive = _next(home, flow, env, "resume-sent")
    expect(verb == "WAIT", f"resume-sent printed {verb}, expected WAIT")
    expect(load_json(directive).get("require_change") is True, "the WAIT for a resumed record must have require_change true")
    text = directive_text(directive)
    expect('"require_change": true' in text, "the resumed WAIT file does not serialise require_change true")


def test_549_11b_audit_and_assessor_waits_keep_require_change_false(base: Path) -> None:
    home, flow, env, _, _ = to_landed_verify(base, assess=True)
    verb, directive = _next(home, flow, env, "spawn-launched")
    expect(verb == "WAIT", f"the assessor spawn printed {verb}, expected WAIT")
    expect(load_json(directive).get("require_change") is False, "the assessor WAIT must keep require_change false")
    home, flow, _ = to_landed_plan(base, audit=True)
    verb, directive = _next(home, flow, {}, "spawn-launched")
    expect(verb == "WAIT", f"the audit spawn printed {verb}, expected WAIT")
    expect(load_json(directive).get("require_change") is False, "the audit WAIT must keep require_change false")


def test_549_12_foreground_bound_for_resumed_record(base: Path) -> None:
    home, flow, env, _ = _drive_to_fix(base)
    _write_placeholder(flow)
    _next(home, flow, env, "spawn-launched")
    _resumed_fix(home, flow, env)
    verb, directive = _next(home, flow, env, "wait-timeout")
    expect(verb == "STOP", f"a resumed record at wait-timeout printed {verb}, expected STOP")
    expect(state_of(flow)["stop_reason"]["fix"].startswith("wait-timeout:"), "the STOP is not wait-timeout")
    expect("clear-live" in directive_text(directive), "the wait-timeout STOP does not name clear-live")
    # The landed variant: a final receipt on disk when the timeout fires lands instead of stopping.
    home, flow, env, _ = _drive_to_fix(base)
    _write_placeholder(flow)
    _next(home, flow, env, "spawn-launched")
    _resumed_fix(home, flow, env)
    write_receipt(flow, "fix", 1)
    verb, _ = _next(home, flow, env, "wait-timeout")
    expect(verb != "STOP", "a landed receipt at wait-timeout still stopped")
    expect((flow / "archive" / "fix" / "a1.landed.md").exists(), "the landed receipt was not archived at wait-timeout")


def test_549_14_wait_timeout_on_parked_record_reasks(base: Path) -> None:
    home, flow, env, _ = _drive_to_fix(base)
    _write_placeholder(flow)
    verb, _ = _next(home, flow, env, "spawn-launched")
    expect(verb == "ASK", f"the launched fix with a question printed {verb}, expected ASK (driver parks)")
    expect(pending_of(flow)["state"] == "parked-awaiting-answer", "the record did not park")
    attempt_before = pending_of(flow)["attempt"]
    fingerprint_before = pending_of(flow)["receipt_seen"]
    verb, _ = _next(home, flow, env, "wait-timeout")
    expect(verb == "ASK", f"a parked record at wait-timeout printed {verb}, expected ASK (the question is still pending)")
    expect(pending_of(flow)["state"] == "parked-awaiting-answer", "wait-timeout un-parked the record")
    expect(pending_of(flow)["attempt"] == attempt_before, "wait-timeout on a parked record re-ran the step (new attempt)")
    expect(pending_of(flow)["receipt_seen"] == fingerprint_before, "wait-timeout stored a fingerprint for the held receipt")
    _no_malformed_archive(flow)


def _section(text: str, heading_pattern: str) -> str:
    match = re.search(heading_pattern, text, re.MULTILINE | re.IGNORECASE)
    expect(match is not None, f"no heading matches {heading_pattern!r}")
    rest = text[match.end():]
    following = re.search(r"^#{1,6} ", rest, re.MULTILINE)
    return rest[: following.start()] if following else rest


def test_549_13_docs_and_docstrings(base: Path) -> None:
    text = DOC.read_text(encoding="utf-8")
    gaps = _section(text, r"^#{1,6} .*known gaps.*$")
    expect("and the malformed rerun" not in gaps, "Known gaps still says 'and the malformed rerun'")
    expect("--require-change" in text, "the stub doc never passes fleet.py wait --require-change")
    expect(re.search(r"\$[0-9]", text) is None, "the stub doc contains a bare positional token")
    doc = recovery.__doc__ or ""
    expect("malformed_rerun" in doc, "the recovery docstring no longer mentions malformed_rerun at all")


SECTIONS = [
    ("[Happy path and merge pin]", [
        ("1: happy path worktree..ship, then a landed ship parks the merge-gate question", test_01_landed_ship_parks_the_merge_gate_question),
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
        ("zero-unit-plan-incomplete is a typed stop and test-only-plan still parses", test_zero_unit_stop_codes_parse_and_are_listed),
        ("no commands/ doc contains the retired test-only-plan code", test_no_command_doc_emits_legacy_test_only_plan),
    ]),
    ("[#549 held placeholder receipts on parked and resumed records]", [
        ("549-01: ticket regression, end-turn shape, resumed placeholder held then landed", test_549_01_ticket_regression_end_turn),
        ("549-02: resume clears receipt_seen and receipt_first_seen", test_549_02_resume_clears_fingerprints),
        ("549-03: parked and unanswered is held across bare next calls (D1)", test_549_03_parked_and_unanswered_is_held),
        ("549-04: old state file with stale stamps on a resumed record is held", test_549_04_old_state_file_with_stale_stamps_is_held),
        ("549-07: resume command on a resumed placeholder waits, never reruns", test_549_07_resume_command_on_resumed_placeholder_waits),
        ("549-08: multi-marker resumed placeholder is held", test_549_08_multi_marker_placeholder_is_held),
        ("549-09: landed and stale still win while resumed", test_549_09_landed_and_stale_still_win_while_resumed),
        ("549-10a: spawned non-placeholder malformed receipt still reruns, then malformed-cap", test_549_10_spawned_non_placeholder_malformed_still_reruns),
        ("549-10b: spawned bare look waits inside the quiet interval, reruns after it", test_549_10b_spawned_bare_look_waits_then_reruns_after_quiet_interval),
        ("549-11: WAIT directive require_change true for resumed, false otherwise", test_549_11_wait_directive_carries_require_change),
        ("549-11b: audit and assessor WAITs keep require_change false", test_549_11b_audit_and_assessor_waits_keep_require_change_false),
        ("549-12: foreground wait-timeout bounds a resumed record; a landed receipt wins", test_549_12_foreground_bound_for_resumed_record),
        ("549-14: wait-timeout on a parked record re-asks, no rerun, no fingerprint", test_549_14_wait_timeout_on_parked_record_reasks),
        ("549-13: stub doc Known gaps, --require-change, no bare positional token; recovery docstring", test_549_13_docs_and_docstrings),
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
