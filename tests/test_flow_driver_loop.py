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
import os
import re
import subprocess
import sys
import tempfile
import typing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

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
        code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "start")
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
    code, out, _ = run_cli(home, "next", "--flow", str(flow), "--after", "start")
    verb, directive = next_line(out)
    directive.unlink()
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "start")
    expect(code == 0, f"next exited {code}: {err}")
    _, again = next_line(out)
    expect(again.exists(), "the deleted directive file was not recreated")
    expect(load_json(again).get("nonce") == nonce, "the recreated directive has a different nonce")
    expect(pending_of(flow)["nonce"] == nonce, "the nonce changed on re-render")


def test_04_dead_agent_waits_then_clear_live_respawns_with_new_attempt(base: Path) -> None:
    home = make_home(base)
    flow, _ = start_flow(home)
    first = pending_of(flow)
    code, out, _ = run_cli(home, "next", "--flow", str(flow), "--after", "start")
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


# A malformed --nonce is rejected before any state write
def test_61_nonce_format_is_validated(base: Path) -> None:
    """Invalid nonce format is rejected with exit code 2 before any state write."""
    home = make_home(base)
    flow, _ = start_flow(home)
    state_path = flow / "state.json"
    before = state_path.read_bytes()
    # Test: non-hex character
    code, _, _ = run_cli(home, "next", "--flow", str(flow), "--after", "notification", "--nonce", "zzzzzzzz")
    expect(code == 2, f"non-hex nonce was accepted with exit code {code}, expected 2")
    expect(state_path.read_bytes() == before, "invalid nonce changed state.json")
    # Test: wrong length
    code, _, _ = run_cli(home, "next", "--flow", str(flow), "--after", "notification", "--nonce", "ab12cd")
    expect(code == 2, f"short nonce was accepted with exit code {code}, expected 2")
    expect(state_path.read_bytes() == before, "invalid nonce changed state.json")
    # Test: uppercase (should be rejected, only lowercase allowed)
    code, _, _ = run_cli(home, "next", "--flow", str(flow), "--after", "notification", "--nonce", "AB12CD34")
    expect(code == 2, f"uppercase nonce was accepted with exit code {code}, expected 2")
    expect(state_path.read_bytes() == before, "invalid nonce changed state.json")


# count_driver_events returns 0 when the log cannot be read or decoded
def test_62_count_driver_events_handles_read_errors(base: Path) -> None:
    """count_driver_events returns 0 for a log that raises OSError or UnicodeDecodeError on read."""
    from scripts.flow_driver.state import DRIVER_LOG_FILE, count_driver_events

    # A directory where the log file belongs: read_text raises IsADirectoryError (an OSError), with no chmod needed.
    as_dir = Path(tempfile.mkdtemp(dir=str(base)))
    (as_dir / DRIVER_LOG_FILE).mkdir()
    count = count_driver_events(as_dir, "test")
    expect(count == 0, f"a directory in place of the log returned {count}, expected 0")

    # Invalid UTF-8 bytes: read_text raises UnicodeDecodeError (a ValueError).
    bad_bytes = Path(tempfile.mkdtemp(dir=str(base)))
    (bad_bytes / DRIVER_LOG_FILE).write_bytes(b'{"event": "test"}\n\xff\xfe\n')
    count = count_driver_events(bad_bytes, "test")
    expect(count == 0, f"an undecodable log returned {count}, expected 0")


# A worktree path that is a subdirectory of another repo is residue unknown
def test_63_residue_probe_rejects_worktree_inside_another_repo(base: Path) -> None:
    """Residue probe verifies that git show-toplevel matches the recorded worktree path."""
    home = make_home(base)
    git_commit_file(home)
    flow, _ = start_flow(home)
    pending = advance_to(home, flow, "implement")
    # Create a worktree within another repo (e.g., home/.../worktree_subdir inside the git repo)
    subdir = home / "wt" / "subdir"
    subdir.mkdir(parents=True, exist_ok=True)
    state = state_of(flow)
    state["worktree"] = str(subdir)  # This directory is inside the repo but not the root
    write_state(flow, state)
    gone_stop(home, flow, pending["nonce"])
    reason = state_of(flow).get("stop_reason", {}).get("implement", "")
    # The residue should be unknown because git show-toplevel in subdir returns parent, not subdir
    expect(
        "residue unknown (git root does not match worktree path)" in reason,
        f"mismatched repo path was not reported by the toplevel check: {reason!r}",
    )
    recs = records(flow, "subagent-gone")
    expect(len(recs) == 1 and recs[0].get("residue_paths") is None, f"residue_paths should be None for unknown residue: {recs!r}")


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


# ---------------------------------------------------------------------------
# #533 slice 1: subagent gone with no receipt (tests 42-60, written spec-blind from the plan)
# ---------------------------------------------------------------------------

GONE_PREFIX = "subagent-gone-no-receipt: step implement attempt 1 handed back with no landed receipt; worktree: "
RAW_INCOMPLETE = "ATTEMPT: 1\nDecision: OK\nno step-end marker here\n"
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


class _Attr(dict):  # type: ignore[type-arg]
    """A dict whose keys are also readable as attributes, for a pending record of either shape."""

    def __getattr__(self, key: str) -> Any:
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc


def unattributed_reason(step: str) -> str:
    return (
        f"notification-unattributed: step {step} attempt 1 got a notification with no nonce and has no "
        "landed receipt; the driver cannot tell whose agent ended, so confirm the agent is not still running first; clear it with "
    )


def clear_command(flow: Path) -> str:
    from scripts.flow_driver.driver import clear_live_command

    return clear_live_command(flow)


def records(flow: Path, event: str) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    for line in log_lines(flow):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("event") == event:
            found.append(rec)
    return found


def attempt_outcome(state: Dict[str, Any], step: str, attempt: int) -> Any:
    return state.get("attempt_log", {}).get(step, {}).get(str(attempt))


def write_state(flow: Path, state: Dict[str, Any]) -> None:
    (flow / "state.json").write_text(json.dumps(state), encoding="utf-8")


def advance_to(home: Path, flow: Path, target: str) -> Dict[str, Any]:
    """Land each earlier step's receipt with an attributed notification until ``target`` is the live step."""
    env = FakeGh(home, mode="nopr").env()  # the worktree landing runs the north-star gh-auth preflight
    for _ in range(10):
        pending = pending_of(flow)
        expect(pending is not None, "no live record while advancing")
        if pending["step"] == target:
            return pending
        step = pending["step"]
        extra = f"WORKTREE: {home / 'wt'}\n" if step == "worktree" else ""
        write_receipt(flow, step, pending["attempt"], extra=extra)
        code, out, err = run_cli(
            home, "next", "--flow", str(flow), "--after", "notification", "--nonce", pending["nonce"],
            extra_env=env,
        )
        expect(code == 0, f"next exited {code} after {step}: {err}")
        verb, _ = next_line(out)
        expect(verb == "SPAWN", f"advancing past {step} printed {verb}, expected SPAWN")
    raise AssertionError(f"the loop did not reach step {target}")


def implement_flow(base: Path) -> Tuple[Path, Path, Dict[str, Any]]:
    home = make_home(base)
    flow, _ = start_flow(home)
    pending = advance_to(home, flow, "implement")
    expect(pending["attempt"] == 1, f"implement attempt was {pending['attempt']}, expected 1")
    return home, flow, pending


def gone_stop(home: Path, flow: Path, nonce: str) -> Path:
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "notification", "--nonce", nonce)
    expect(code == 0, f"next exited {code}: {err}")
    verb, directive = next_line(out)
    expect(verb == "STOP", f"attributed notification with no receipt printed {verb}, expected STOP")
    return directive


def write_raw_receipt(flow: Path) -> None:
    from scripts.flow_driver import receipts

    path = receipts.receipt_path(flow, "implement")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(RAW_INCOMPLETE, encoding="utf-8")


def git_commit_file(home: Path) -> None:
    wt = home / "wt"
    (wt / "tracked.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt), "add", "tracked.txt"], check=True, capture_output=True, env=GIT_ENV)
    subprocess.run(["git", "-C", str(wt), "commit", "-q", "-m", "add tracked"], check=True, capture_output=True, env=GIT_ENV)


def other_nonce(nonce: str) -> str:
    return "cd34ab12" if nonce == "ab12cd34" else "ab12cd34"


# Test 42: event construction carries the nonce (plan item 1)
def test_42_event_from_label_carries_nonce(base: Path) -> None:
    from scripts.flow_driver.steps import Notification, Reentry, UnknownEvent, event_from_label

    ev = event_from_label("notification", nonce="ab12cd34")
    expect(isinstance(ev, Notification) and ev.nonce == "ab12cd34", f"notification with nonce built {ev!r}")
    ev2 = event_from_label("notification")
    expect(isinstance(ev2, Notification) and ev2.nonce is None, f"nonce-less notification built {ev2!r}")
    expect(isinstance(event_from_label(None), Reentry), "event_from_label(None) is not Reentry")
    expect(isinstance(event_from_label("no-such-label"), UnknownEvent), "unknown label is not UnknownEvent")


# Test 43: the CLI accepts --nonce only with --after notification (plan item 2)
def test_43_nonce_is_refused_without_notification_label(base: Path) -> None:
    home = make_home(base)
    flow, _ = start_flow(home)
    nonce = pending_of(flow)["nonce"]
    state_path = flow / "state.json"
    before = state_path.read_bytes()
    code, _, _ = run_cli(home, "next", "--flow", str(flow), "--after", "spawn-launched", "--nonce", "ab12cd34")
    expect(code != 0, "--nonce with --after spawn-launched was accepted")
    expect(state_path.read_bytes() == before, "a refused --nonce changed state.json")
    code, out, err = run_cli(
        home, "next", "--flow", str(flow), "--after", "notification", "--nonce", other_nonce(nonce)
    )
    expect(code == 0, f"--nonce with --after notification exited {code}: {err}")
    verb, _ = next_line(out)
    expect(verb == "STOP", f"a never-issued nonce on a live record printed {verb}, expected STOP")


# Test 44: attributed gone with an absent receipt is a STOP (plan item 3)
def test_44_attributed_gone_with_absent_receipt_is_stop(base: Path) -> None:
    from scripts.flow_driver import receipts

    home, flow, pending = implement_flow(base)
    expect(not receipts.receipt_path(flow, "implement").exists(), "a receipt exists before the gone check")
    gone_stop(home, flow, pending["nonce"])
    state = state_of(flow)
    reason = state.get("stop_reason", {}).get("implement", "")
    expect(state.get("status") == "failed", f"status is {state.get('status')!r}, expected failed")
    expect(reason.startswith(GONE_PREFIX), f"stop reason was {reason!r}")
    expect(reason.endswith("clear it with " + clear_command(flow.resolve())), "stop reason does not end with the clear-live command")
    expect("1" not in state.get("attempt_log", {}).get("implement", {}), "attempt 1 gained an attempt_log entry")
    recs = records(flow, "subagent-gone")
    expect(len(recs) == 1, f"expected one subagent-gone record, got {len(recs)}")
    expect(recs[0].get("receipt") == "absent", f"receipt field was {recs[0].get('receipt')!r}")


# Test 45: first-seen incomplete receipt is gone; classify returns a typed Gone (plan item 4)
def test_45_first_seen_incomplete_receipt_is_gone_and_classify_is_typed(base: Path) -> None:
    from scripts.flow_driver import receipts
    from scripts.flow_driver.state import mutating

    home, flow, pending = implement_flow(base)
    write_raw_receipt(flow)
    expect(pending_of(flow).get("receipt_seen") is None, "receipt_seen was set before any read")
    with mutating(flow) as locked:
        attributed = receipts.classify(locked, _Attr(pending_of(flow)), attributed=True)
        plain = receipts.classify(locked, _Attr(pending_of(flow)))
    expect(isinstance(attributed, receipts.Gone), f"attributed classify returned {attributed!r}, expected Gone")
    expect(attributed.state == "gone", f"Gone.state was {attributed.state!r}")
    expect(isinstance(plain, receipts.InProgress), f"unattributed classify returned {plain!r}, expected InProgress")
    gone_stop(home, flow, pending["nonce"])
    reason = state_of(flow).get("stop_reason", {}).get("implement", "")
    expect(reason.startswith(GONE_PREFIX), f"stop reason was {reason!r}")
    recs = records(flow, "subagent-gone")
    expect(len(recs) == 1 and recs[0].get("receipt") == "incomplete", f"subagent-gone records were {recs!r}")


# Test 46: an awaiting question is a normal park, never gone (plan item 5)
def test_46_awaiting_question_is_a_park_not_gone(base: Path) -> None:
    from scripts.flow_driver import relay
    from scripts.flow_driver.state import ParkedQuestion

    home, flow, pending = implement_flow(base)
    path = relay.question_path(flow, ParkedQuestion(step="implement", attempt=1, nn=1, kind="step"))
    path.parent.mkdir(parents=True, exist_ok=True)
    question = {
        "summary": "Pick one",
        "questions": [{
            "id": "q1", "question": "Which?", "header": "Pick", "multiSelect": False,
            "options": [{"label": "Alpha", "description": "a"}, {"label": "Beta", "description": "b"}],
        }],
    }
    path.write_text(json.dumps(question), encoding="utf-8")
    code, out, err = run_cli(
        home, "next", "--flow", str(flow), "--after", "notification", "--nonce", pending["nonce"]
    )
    expect(code == 0, f"next exited {code}: {err}")
    verb, _ = next_line(out)
    expect(verb in ("ASK", "PARK"), f"an awaiting question printed {verb}, expected ASK or PARK")
    state = state_of(flow)
    expect(state.get("stop_reason", {}).get("implement") is None, "an awaiting question was stopped as gone")
    expect(records(flow, "subagent-gone") == [], "an awaiting question logged subagent-gone")


# Test 47: a landed receipt wins over an attributed notification (plan item 6)
def test_47_landed_receipt_wins_over_attributed_notification(base: Path) -> None:
    home_a = make_home(base)
    flow_a, _ = start_flow(home_a)
    nonce_a = pending_of(flow_a)["nonce"]
    write_receipt(flow_a, "worktree", 1, extra=f"WORKTREE: {home_a / 'wt'}\n")
    code, out, err = run_cli(
        home_a, "next", "--flow", str(flow_a), "--after", "notification", "--nonce", nonce_a,
        extra_env=FakeGh(home_a, mode="nopr").env(),
    )
    expect(code == 0, f"next exited {code}: {err}")
    verb_a, _ = next_line(out)

    home_b = make_home(base)
    flow_b, _ = start_flow(home_b)
    write_receipt(flow_b, "worktree", 1, extra=f"WORKTREE: {home_b / 'wt'}\n")
    code, out, err = run_cli(
        home_b, "next", "--flow", str(flow_b), "--after", "wait-settled",
        extra_env=FakeGh(home_b, mode="nopr").env(),
    )
    expect(code == 0, f"wait-settled exited {code}: {err}")
    verb_b, _ = next_line(out)

    expect(verb_a == "SPAWN" and verb_b == verb_a, f"attributed printed {verb_a}, wait-settled printed {verb_b}")
    expect(pending_of(flow_a)["step"] == pending_of(flow_b)["step"] == "plan", "the landed step did not advance to plan")
    expect(state_of(flow_a).get("status") == "running", "a landed receipt left the flow not running")
    expect(records(flow_a, "subagent-gone") == [], "a landed receipt logged subagent-gone")


# Test 48: a nonce issued earlier by this flow (stale duplicate) is logged and ignored; a never-issued one is not
def test_48_foreign_nonce_is_logged_and_ignored(base: Path) -> None:
    home = make_home(base)
    flow, _ = start_flow(home)
    old_nonce = pending_of(flow)["nonce"]
    code, _, err = run_cli(home, "clear-live", "--flow", str(flow), "--reason", "dead agent")
    expect(code == 0, f"clear-live exited {code}: {err}")
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "start")
    expect(next_line(out)[0] == "SPAWN", "the re-spawn after clear-live did not SPAWN")
    nonce = pending_of(flow)["nonce"]
    expect(nonce != old_nonce, "the re-spawn reused the nonce")
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "notification", "--nonce", old_nonce)
    expect(code == 0, f"next exited {code}: {err}")
    verb, _ = next_line(out)
    expect(verb == "WAIT", f"a stale duplicate nonce printed {verb}, expected WAIT")
    expect(state_of(flow).get("status") == "running", "a stale duplicate nonce changed the flow status")
    recs = records(flow, "notification-foreign")
    expect(len(recs) == 1, f"expected one notification-foreign record, got {len(recs)}")
    expect(recs[0].get("nonce") == nonce, "notification-foreign nonce is not the pending nonce")
    expect(recs[0].get("notification_nonce") == old_nonce, "notification-foreign notification_nonce is wrong")


# Test 49: a nonce-less notification with no landed receipt fails closed (plan item 8)
def test_49_missing_nonce_fails_closed_to_unattributed_stop(base: Path) -> None:
    home = make_home(base)
    flow, _ = start_flow(home)
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "notification")
    expect(code == 0, f"next exited {code}: {err}")
    verb, _ = next_line(out)
    expect(verb == "STOP", f"a nonce-less notification printed {verb}, expected STOP")
    reason = state_of(flow).get("stop_reason", {}).get("worktree", "")
    expect(reason.startswith(unattributed_reason("worktree")), f"stop reason was {reason!r}")
    expect(reason.endswith("clear it with " + clear_command(flow.resolve())), "unattributed stop does not end with the clear-live command")


# Test 50: nonce-less versus attributed notification on a stable malformed receipt (plan item 9)
def malformed_at_implement(base: Path) -> Tuple[Path, Path, Dict[str, Any]]:
    home, flow, pending = implement_flow(base)
    write_raw_receipt(flow)
    code, _, err = run_cli(home, "next", "--flow", str(flow), "--after", "wait-settled")
    expect(code == 0, f"wait-settled exited {code}: {err}")
    expect(pending_of(flow).get("receipt_seen") is not None, "precondition: wait-settled did not record the receipt fingerprint")
    return home, flow, pending


def test_50a_missing_nonce_on_malformed_receipt_stops_without_rerun(base: Path) -> None:
    home, flow, _ = malformed_at_implement(base)
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "notification")
    expect(code == 0, f"next exited {code}: {err}")
    verb, _ = next_line(out)
    expect(verb == "STOP", f"a nonce-less notification on a malformed receipt printed {verb}, expected STOP")
    reason = state_of(flow).get("stop_reason", {}).get("implement", "")
    expect(reason.startswith(unattributed_reason("implement")), f"stop reason was {reason!r}")
    expect(attempt_outcome(state_of(flow), "implement", 1) != "receipt-malformed", "a receipt-malformed outcome was written")


def test_50b_attributed_notification_on_malformed_receipt_reruns(base: Path) -> None:
    home, flow, pending = malformed_at_implement(base)
    code, out, err = run_cli(
        home, "next", "--flow", str(flow), "--after", "notification", "--nonce", pending["nonce"]
    )
    expect(code == 0, f"next exited {code}: {err}")
    verb, _ = next_line(out)
    expect(verb == "SPAWN", f"an attributed notification on a malformed receipt printed {verb}, expected SPAWN")
    state = state_of(flow)
    expect(attempt_outcome(state, "implement", 1) == "receipt-malformed", "attempt 1 was not recorded receipt-malformed")
    expect(pending_of(flow)["attempt"] == 2, "the malformed rerun did not open attempt 2")


# Test 51: gone and unattributed STOPs are clearable and do not open Retry (plan item 10)
def test_51a_gone_stop_is_clearable_and_refuses_retry(base: Path) -> None:
    home, flow, pending = implement_flow(base)
    nonce = pending["nonce"]
    gone_stop(home, flow, nonce)
    code, _, _ = run_cli(home, "recover", "--flow", str(flow), "--choice", "retry")
    expect(code != 0, "recover --choice retry was accepted after a gone STOP")
    code, _, err = run_cli(home, "clear-live", "--flow", str(flow), "--reason", "x")
    expect(code == 0, f"clear-live exited {code}: {err}")
    state = state_of(flow)
    expect(attempt_outcome(state, "implement", 1) == "cleared", "attempt 1 was not recorded cleared")
    expect(state.get("status") == "running", f"status after clear-live is {state.get('status')!r}")
    code, out, _ = run_cli(home, "next", "--flow", str(flow), "--after", "start")
    verb, _ = next_line(out)
    expect(verb == "SPAWN", f"after clear-live printed {verb}, expected SPAWN")
    second = pending_of(flow)
    expect(second["attempt"] == 2, "the re-spawn did not open attempt 2")
    expect(second["nonce"] != nonce, "the re-spawn reused the nonce")


def test_51b_unattributed_stop_is_clearable(base: Path) -> None:
    home, flow, pending = implement_flow(base)
    code, out, _ = run_cli(home, "next", "--flow", str(flow), "--after", "notification")
    verb, _ = next_line(out)
    expect(verb == "STOP", f"nonce-less notification printed {verb}, expected STOP")
    code, _, err = run_cli(home, "clear-live", "--flow", str(flow), "--reason", "x")
    expect(code == 0, f"clear-live exited {code}: {err}")
    state = state_of(flow)
    expect(attempt_outcome(state, "implement", 1) == "cleared", "attempt 1 was not recorded cleared")
    code, out, _ = run_cli(home, "next", "--flow", str(flow), "--after", "start")
    verb, _ = next_line(out)
    expect(verb == "SPAWN", f"after clear-live printed {verb}, expected SPAWN")
    expect(pending_of(flow)["attempt"] == 2, "the re-spawn did not open attempt 2")


# Test 52: residue summary counts changed paths in a real repo (plan item 11, first case)
def test_52_residue_summary_counts_changed_paths(base: Path) -> None:
    home = make_home(base)
    git_commit_file(home)
    flow, _ = start_flow(home)
    pending = advance_to(home, flow, "implement")
    (home / "wt" / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    (home / "wt" / "untracked.txt").write_text("x\n", encoding="utf-8")
    gone_stop(home, flow, pending["nonce"])
    reason = state_of(flow).get("stop_reason", {}).get("implement", "")
    expect(
        re.search(r"worktree: HEAD [0-9a-f]{12}, 2 changed path\(s\)", reason) is not None,
        f"residue text missing from stop reason: {reason!r}",
    )
    recs = records(flow, "subagent-gone")
    expect(len(recs) == 1 and recs[0].get("residue_paths") == 2, f"subagent-gone records were {recs!r}")


# Test 53: no worktree recorded (plan item 11, second case)
def test_53_residue_reports_no_worktree_recorded(base: Path) -> None:
    home, flow, pending = implement_flow(base)
    state = state_of(flow)
    state["worktree"] = None
    write_state(flow, state)
    gone_stop(home, flow, pending["nonce"])
    reason = state_of(flow).get("stop_reason", {}).get("implement", "")
    expect("worktree: no worktree recorded" in reason, f"stop reason was {reason!r}")
    recs = records(flow, "subagent-gone")
    expect(len(recs) == 1 and recs[0].get("residue_paths") is None, f"subagent-gone records were {recs!r}")


# Test 54: a non-repo worktree is residue unknown and still STOPs (plan item 11, third case)
def test_54_non_repo_worktree_is_residue_unknown_and_still_stops(base: Path) -> None:
    home, flow, pending = implement_flow(base)
    plain = Path(tempfile.mkdtemp(dir=str(base)))
    state = state_of(flow)
    state["worktree"] = str(plain)
    write_state(flow, state)
    gone_stop(home, flow, pending["nonce"])
    reason = state_of(flow).get("stop_reason", {}).get("implement", "")
    expect("worktree: residue unknown (" in reason, f"stop reason was {reason!r}")
    recs = records(flow, "subagent-gone")
    expect(len(recs) == 1 and recs[0].get("residue_paths") is None, f"subagent-gone records were {recs!r}")


# Test 55: the probe is a pure read (plan item 11, fourth case)
def test_55_residue_probe_leaves_index_untouched(base: Path) -> None:
    home = make_home(base)
    git_commit_file(home)
    flow, _ = start_flow(home)
    pending = advance_to(home, flow, "implement")
    wt = home / "wt"
    (wt / "tracked.txt").write_text("one\nstaged\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt), "add", "tracked.txt"], check=True, capture_output=True, env=GIT_ENV)
    index = wt / ".git" / "index"
    before = index.read_bytes()
    gone_stop(home, flow, pending["nonce"])
    expect(not (wt / ".git" / "index.lock").exists(), "the probe left .git/index.lock behind")
    expect(index.read_bytes() == before, "the residue probe changed the index contents")


# Test 56: Facts carries gone_residue exactly when receipt is gone (plan item 12)
def test_56_facts_gone_residue_iff_gone(base: Path) -> None:
    import inspect

    from scripts.flow_driver.steps import Facts

    def build(**given: Any) -> Any:
        kwargs: Dict[str, Any] = {}
        for name, param in inspect.signature(Facts).parameters.items():
            if name in given:
                kwargs[name] = given[name]
            elif param.default is inspect.Parameter.empty:
                kwargs[name] = None
        return Facts(**kwargs)

    try:
        build(receipt="none", gone_residue=None)
    except Exception as exc:
        raise AssertionError(f"control Facts(receipt='none') failed to construct: {exc}")
    for label, given in (
        ("receipt gone without residue", {"receipt": "gone", "gone_residue": None}),
        ("residue without gone", {"receipt": "none", "gone_residue": "x"}),
    ):
        try:
            build(**given)
        except Exception:
            continue
        raise AssertionError(f"Facts accepted {label}")
    try:
        build(receipt="gone", gone_residue="x")
    except Exception as exc:
        raise AssertionError(f"Facts(receipt='gone', gone_residue='x') did not construct: {exc}")


# Test 57: stop codes are listed and parse (plan item 13)
def test_57_new_stop_codes_are_listed_and_parse(base: Path) -> None:
    from scripts.flow_driver.steps import STOP_CODES, parse_stop_reason

    for code in ("subagent-gone-no-receipt", "notification-unattributed"):
        expect(code in STOP_CODES, f"{code} missing from STOP_CODES")
    got = parse_stop_reason("subagent-gone-no-receipt: d")
    expect(got == ("subagent-gone-no-receipt", "d"), f"parse returned {got!r}")


# Test 58: derived count from the driver log (plan item 14)
def test_58_derived_count_reads_the_driver_log(base: Path) -> None:
    from scripts.flow_driver.state import count_driver_events

    flow = Path(tempfile.mkdtemp(dir=str(base)))
    rows = [
        {"event": "subagent-gone", "step": "implement", "attempt": 1},
        {"event": "subagent-gone", "step": "plan", "attempt": 1},
        {"event": "notification-foreign", "step": "implement", "attempt": 1},
    ]
    (flow / "driver-log.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    expect(count_driver_events(flow, "subagent-gone") == 2, "total subagent-gone count was not 2")
    expect(count_driver_events(flow, "subagent-gone", step="plan") == 1, "plan-filtered count was not 1")
    expect(count_driver_events(Path(tempfile.mkdtemp(dir=str(base))), "subagent-gone") == 0, "missing log did not count 0")

    torn = Path(tempfile.mkdtemp(dir=str(base)))
    lines = [json.dumps(rows[0]), "{not json", json.dumps(rows[1])]
    (torn / "driver-log.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    expect(count_driver_events(torn, "subagent-gone") == 2, "an unparseable line was not skipped")


# Test 59: replay is idempotent after a gone STOP (plan item 15)
def test_59_replay_after_gone_stop_is_idempotent(base: Path) -> None:
    home, flow, pending = implement_flow(base)
    nonce = pending["nonce"]
    gone_stop(home, flow, nonce)
    first_reason = state_of(flow).get("stop_reason", {}).get("implement", "")

    code, out, err = run_cli(home, "next", "--flow", str(flow))
    expect(code == 0, f"next with no --after exited {code}: {err}")
    verb, _ = next_line(out)
    expect(verb == "STOP", f"replayed next printed {verb}, expected STOP")
    expect(state_of(flow).get("stop_reason", {}).get("implement", "") == first_reason, "replayed next changed the stop reason")

    gone_stop(home, flow, nonce)
    expect(state_of(flow).get("stop_reason", {}).get("implement", "") == first_reason, "a duplicate notification changed the stop reason")
    expect(len(records(flow, "subagent-gone")) == 1, "a replayed or duplicate notification logged a second subagent-gone")
    expect(pending_of(flow)["attempt"] == 1 and pending_of(flow)["nonce"] == nonce, "replay opened an attempt or changed the nonce")


# Test 60: orchestrator prose passes the nonce and names the new stops (plan item 16)
def test_60_command_doc_passes_nonce_and_names_new_stops(base: Path) -> None:
    text = DOC.read_text(encoding="utf-8")
    expect("next --after notification --nonce" in text, "doc lacks next --after notification --nonce")
    recovery = text.find("Recovery")
    expect(recovery != -1, "doc has no Recovery section")
    tail = text[recovery:]
    for code in ("subagent-gone-no-receipt", "notification-unattributed"):
        expect(code in tail, f"Recovery STOP list does not name {code}")
    rows = [line for line in text.splitlines() if line.lstrip().startswith("|") and "clear-live" in line]
    expect(
        any("subagent-gone-no-receipt" in row and "notification-unattributed" in row for row in rows),
        "no clear-live table row names both new codes",
    )
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tests" / "test_command_doc_shell_conventions.py")],
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=300,
    )
    expect(result.returncode == 0, f"shell conventions test exited {result.returncode}: {result.stdout[-400:]}")


# ---------------------------------------------------------------------------
# #533 round 2 behaviours (F2 reason, F3, F4, F12, F15), written spec-blind from the action plan
# ---------------------------------------------------------------------------

# F2: a nonce this flow never issued fails closed to a notification-unattributed STOP (not a silent WAIT)
def test_64_never_issued_nonce_stops_as_unattributed(base: Path) -> None:
    home = make_home(base)
    flow, _ = start_flow(home)
    nonce = pending_of(flow)["nonce"]
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "notification", "--nonce", other_nonce(nonce))
    expect(code == 0, f"next exited {code}: {err}")
    verb, _ = next_line(out)
    expect(verb == "STOP", f"a never-issued nonce printed {verb}, expected STOP")
    state = state_of(flow)
    reason = state.get("stop_reason", {}).get("worktree", "")
    expect(reason.startswith("notification-unattributed:"), f"stop reason was {reason!r}")
    expect(state.get("status") == "failed", f"status is {state.get('status')!r}, expected failed")


# F3: an attributed notification that meets a Stale receipt (an earlier attempt's) is gone: archive, then STOP
def test_65_attributed_notification_on_stale_receipt_is_gone(base: Path) -> None:
    from scripts.flow_driver import receipts

    home = make_home(base)
    flow, _ = start_flow(home)
    code, _, err = run_cli(home, "clear-live", "--flow", str(flow), "--reason", "stale receipt")
    expect(code == 0, f"clear-live exited {code}: {err}")
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "start")
    expect(code == 0, f"next exited {code}: {err}")
    verb, _ = next_line(out)
    expect(verb == "SPAWN", f"re-spawn printed {verb}")
    second = pending_of(flow)
    expect(second["attempt"] == 2 and second["step"] == "worktree", f"re-spawn was {second!r}")
    write_receipt(flow, "worktree", 1, extra=f"WORKTREE: {home / 'wt'}\n")  # the cleared attempt's receipt
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "notification", "--nonce", second["nonce"])
    expect(code == 0, f"next exited {code}: {err}")
    verb, _ = next_line(out)
    expect(verb == "STOP", f"an attributed notification on a stale receipt printed {verb}, expected STOP")
    state = state_of(flow)
    reason = state.get("stop_reason", {}).get("worktree", "")
    expect(reason.startswith("subagent-gone-no-receipt:"), f"stop reason was {reason!r}")
    expect(state.get("status") == "failed", f"status is {state.get('status')!r}, expected failed")
    expect(not receipts.receipt_path(flow, "worktree").exists(), "the stale receipt was not archived before the STOP")


# F4: clear-live after a subagent-gone STOP archives the incomplete receipt, so attempt 2 never sees it
def test_66_clear_live_after_gone_archives_incomplete_receipt(base: Path) -> None:
    from scripts.flow_driver import receipts

    home, flow, pending = implement_flow(base)
    write_raw_receipt(flow)
    gone_stop(home, flow, pending["nonce"])
    code, _, err = run_cli(home, "clear-live", "--flow", str(flow), "--reason", "gone agent")
    expect(code == 0, f"clear-live exited {code}: {err}")
    expect(not receipts.receipt_path(flow, "implement").exists(), "the incomplete receipt is still at the landing path after clear-live")
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "start")
    expect(code == 0, f"next exited {code}: {err}")
    verb, _ = next_line(out)
    expect(verb == "SPAWN", f"re-spawn printed {verb}")
    expect(pending_of(flow)["attempt"] == 2, "the re-spawn did not open attempt 2")
    expect(not receipts.receipt_path(flow, "implement").exists(), "attempt 2 finds the incomplete receipt at the landing path")


# F12: git stderr in the residue probe is capped and control-character-free in the STOP detail
def test_67_residue_probe_output_is_capped_and_control_free(base: Path) -> None:
    home, flow, pending = implement_flow(base)
    long_dir = "/".join(["e\x1b[2J" + "x" * 190] * 6)  # about 1.2 KB of path with an ESC sequence; never created
    state = state_of(flow)
    state["worktree"] = str(home / long_dir)
    write_state(flow, state)
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "notification", "--nonce", pending["nonce"])
    expect(code == 0, f"next exited {code}: {err}")
    verb, _ = next_line(out)
    expect(verb == "STOP", f"a nonexistent worktree printed {verb}, expected STOP")
    reason = state_of(flow).get("stop_reason", {}).get("implement", "")
    expect(reason.startswith(GONE_PREFIX), f"stop reason was {reason!r}")
    expect(not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in reason), f"control characters in STOP detail: {reason!r}")
    expect(len(reason) < 2000, f"STOP detail is {len(reason)} characters, not capped")


# F15: an audit completion that arrives after the audit has passed is not tagged notification-foreign
def test_68_audit_completion_is_tagged_with_its_source(base: Path) -> None:
    from test_flow_driver_audit_loop import ENVS, to_landed_plan

    home, flow, nxt = to_landed_plan(base, audit=True)
    audit_nonce = nxt["nonce"]
    (flow / "steps" / "audit-plan-a1.md").write_text("pass\n", encoding="utf-8")
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "notification", extra_env=ENVS[str(flow)])
    expect(code == 0, f"next exited {code}: {err}")
    verb, _ = next_line(out)
    expect(verb == "SPAWN", f"a passed audit printed {verb}, expected SPAWN of track")
    track_nonce = pending_of(flow)["nonce"]
    code, out, err = run_cli(
        home, "next", "--flow", str(flow), "--after", "notification", "--nonce", audit_nonce, extra_env=ENVS[str(flow)],
    )
    expect(code == 0, f"late audit notification exited {code}: {err}")
    expect(pending_of(flow)["nonce"] == track_nonce, "the late audit notification changed the live nonce")
    tagged = [
        rec for rec in records(flow, "notification-foreign")
        if rec.get("notification_nonce") == audit_nonce
    ]
    expect(len(tagged) == 1 and tagged[0].get("source") == "audit",
           f"a late audit completion is not tagged with source=audit: {tagged!r}")


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
    ("[#533 slice 1: subagent gone with no receipt]", [
        ("42: event_from_label carries the notification nonce", test_42_event_from_label_carries_nonce),
        ("43: --nonce is refused without --after notification", test_43_nonce_is_refused_without_notification_label),
        ("44: attributed gone with absent receipt is STOP, no attempt logged", test_44_attributed_gone_with_absent_receipt_is_stop),
        ("45: first-seen incomplete receipt is gone; classify returns typed Gone", test_45_first_seen_incomplete_receipt_is_gone_and_classify_is_typed),
        ("46: awaiting question is a park, never gone", test_46_awaiting_question_is_a_park_not_gone),
        ("47: landed receipt wins over an attributed notification", test_47_landed_receipt_wins_over_attributed_notification),
        ("48: foreign nonce is logged and ignored", test_48_foreign_nonce_is_logged_and_ignored),
        ("49: nonce-less notification fails closed to unattributed STOP", test_49_missing_nonce_fails_closed_to_unattributed_stop),
        ("50a: nonce-less notification on malformed receipt stops without rerun", test_50a_missing_nonce_on_malformed_receipt_stops_without_rerun),
        ("50b: attributed notification on malformed receipt reruns", test_50b_attributed_notification_on_malformed_receipt_reruns),
        ("51a: gone STOP is clearable and refuses retry", test_51a_gone_stop_is_clearable_and_refuses_retry),
        ("51b: unattributed STOP is clearable", test_51b_unattributed_stop_is_clearable),
        ("52: residue summary counts changed paths", test_52_residue_summary_counts_changed_paths),
        ("53: residue reports no worktree recorded", test_53_residue_reports_no_worktree_recorded),
        ("54: non-repo worktree is residue unknown and still STOPs", test_54_non_repo_worktree_is_residue_unknown_and_still_stops),
        ("55: residue probe leaves the index untouched", test_55_residue_probe_leaves_index_untouched),
        ("56: Facts gone_residue is set iff receipt is gone", test_56_facts_gone_residue_iff_gone),
        ("57: new stop codes are listed and parse", test_57_new_stop_codes_are_listed_and_parse),
        ("58: derived count reads the driver log", test_58_derived_count_reads_the_driver_log),
        ("59: replay after gone STOP is idempotent", test_59_replay_after_gone_stop_is_idempotent),
        ("60: command doc passes the nonce and names the new stops", test_60_command_doc_passes_nonce_and_names_new_stops),
        ("61: nonce format validation rejects invalid hex/length", test_61_nonce_format_is_validated),
        ("62: count_driver_events handles read errors robustly", test_62_count_driver_events_handles_read_errors),
        ("63: residue probe rejects a worktree inside another repo", test_63_residue_probe_rejects_worktree_inside_another_repo),
        ("64: never-issued nonce is a notification-unattributed STOP", test_64_never_issued_nonce_stops_as_unattributed),
        ("65: attributed notification on a stale receipt is gone and archives it", test_65_attributed_notification_on_stale_receipt_is_gone),
        ("66: clear-live after gone archives the incomplete receipt", test_66_clear_live_after_gone_archives_incomplete_receipt),
        ("67: residue probe detail is capped and control-free", test_67_residue_probe_output_is_capped_and_control_free),
        ("68: late audit completion is tagged with source audit", test_68_audit_completion_is_tagged_with_its_source),
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
