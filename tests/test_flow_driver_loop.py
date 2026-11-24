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

import json
import re
import subprocess
import sys
import tempfile
import typing
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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
from scripts.flow_driver import loop, model, receipts, state, steps  # noqa: E402

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
# Parity catch-up (#538): agent wiring, prompt shape, spawn-failed, assess default-on, doc contract,
# vocabulary. Written from the plan's Testing Strategy; expected values are literals here.
# ---------------------------------------------------------------------------

PARITY_AGENTS = {
    "worktree": "general-purpose",
    "plan": "flow-plan",
    "track": "general-purpose",
    "implement": "flow-implement",
    "review": "flow-review",
    "fix": "flow-implement",
    "verify": "general-purpose",
    "ship": "flow-ship",
}
RESTRICTED_PROMPT_STEPS = ["plan", "implement", "review", "fix", "ship"]
GENERAL_PROMPT_STEPS = ["worktree", "track", "verify"]
DEFENCE_BEGIN = "<!-- flow-step-defence:begin -->"
DEFENCE_END = "<!-- flow-step-defence:end -->"
DECISION_SENTENCE = 'Your receipt must contain a "Decision:" line whose value is exactly OK or FAILED'


def _walk(home: Path, flow: Path, first: Dict[str, Any], env: Dict[str, str],
          stop_at: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Answer each SPAWN with an OK receipt. Returns the first SPAWN directive seen per step."""
    seen: Dict[str, Dict[str, Any]] = {}
    current = first
    for _ in range(12):
        step = current["step"]
        seen.setdefault(step, current)
        if step == stop_at:
            return seen
        extra = f"WORKTREE: {home / 'wt'}\n" if step == "worktree" else ""
        write_receipt(flow, step, current["attempt"], extra=extra)
        code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "notification", extra_env=env)
        expect(code == 0, f"next exited {code} after {step}: {err}")
        verb, directive = next_line(out)
        if verb != "SPAWN":
            return seen
        current = load_json(directive)
    raise AssertionError(f"no stop within 12 spawns: {sorted(seen)}")


def _prompt_text(flow: Path, directive: Dict[str, Any]) -> str:
    matches = sorted((flow / "prompts").glob(f"{directive['step']}-a{directive['attempt']}-*.md"))
    expect(len(matches) == 1, f"expected one prompt for {directive['step']}, found {[m.name for m in matches]}")
    return matches[0].read_text(encoding="utf-8")


def _defence_block(text: str) -> str:
    expect(DEFENCE_BEGIN in text and DEFENCE_END in text, "defence markers missing from agents/flow-plan.md")
    return text.split(DEFENCE_BEGIN, 1)[1].split(DEFENCE_END, 1)[0]


def _parity_flow(base: Path) -> Tuple[Path, Path, Dict[str, str], Dict[str, Any]]:
    home = make_home(base)
    env = FakeGh(home, mode="nopr").env()
    flow, first = start_flow(home, extra_env=env)
    return home, flow, env, first


def test_p01_spawn_agent_types_per_step(base: Path) -> None:
    home, flow, env, first = _parity_flow(base)
    seen = _walk(home, flow, first, env)
    for step, agent in PARITY_AGENTS.items():
        expect(step in seen, f"no SPAWN seen for {step}")
        expect(seen[step].get("agent_type") == agent,
               f"{step} SPAWN agent_type {seen[step].get('agent_type')!r}, expected {agent!r}")


def test_p02_replayed_plan_spawn_keeps_flow_plan(base: Path) -> None:
    home, flow, env, first = _parity_flow(base)
    _walk(home, flow, first, env, stop_at="plan")
    code, _, err = run_cli(home, "clear-live", "--flow", str(flow), "--reason", "dead agent", extra_env=env)
    expect(code == 0, f"clear-live exited {code}: {err}")
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "start", extra_env=env)
    expect(code == 0, f"next exited {code}: {err}")
    verb, directive = next_line(out)
    expect(verb == "SPAWN", f"replay printed {verb}, expected SPAWN")
    replay = load_json(directive)
    expect(replay.get("step") == "plan" and replay.get("agent_type") == "flow-plan",
           f"replayed SPAWN is {replay.get('step')!r}/{replay.get('agent_type')!r}, expected plan/flow-plan")


def test_p03_every_step_prompt_has_defence_block_and_decision_line(base: Path) -> None:
    home, flow, env, first = _parity_flow(base)
    seen = _walk(home, flow, first, env)
    block = _defence_block((REPO_ROOT / "agents" / "flow-plan.md").read_text(encoding="utf-8"))
    expect(block.strip() != "", "the defence block in agents/flow-plan.md is empty")
    for step in PARITY_AGENTS:
        expect(step in seen, f"no SPAWN seen for {step}")
        text = _prompt_text(flow, seen[step])
        expect(block in text, f"{step} prompt lacks the defence block byte-for-byte")
        expect(DECISION_SENTENCE in text, f"{step} prompt lacks the Decision line")
        expect("never DONE, SUCCESS or anything else" in text, f"{step} prompt does not forbid DONE and SUCCESS")


def test_p04_restricted_step_prompts_invoke_the_skill_and_name_the_contract(base: Path) -> None:
    home, flow, env, first = _parity_flow(base)
    seen = _walk(home, flow, first, env)
    for step in RESTRICTED_PROMPT_STEPS:
        expect(step in seen, f"no SPAWN seen for {step}")
        text = _prompt_text(flow, seen[step])
        skill = loop.STEP_COMMANDS[step].split()[0].lstrip("/")
        expect(f"Skill(skill='{skill}', args='" in text, f"{step} prompt has no Skill(skill='{skill}', args=...) call")
        expect("skill invocation failed" in text, f"{step} prompt lacks the skill-failure wording")
        expect("~/.claude/prompts/flow-step-contract.md" in text, f"{step} prompt does not name the step contract")
        expect("Reason: step contract unreadable" in text, f"{step} prompt lacks the unreadable-contract reason")
        expect("Reply with the receipt's Decision: and Reason: lines." in text, f"{step} prompt lacks the reply line")
        expect("Run this step's work with:" not in text, f"{step} prompt still carries the general-purpose sentence")
        worktree = re.search(r"WORKTREE=(\S+)", text)
        expect(worktree is not None, f"{step} prompt has no WORKTREE= header")
        expect(f"in cwd {worktree.group(1)}" in text, f"{step} Skill sentence does not run in the WORKTREE")


def test_p05_general_purpose_prompts_keep_the_run_this_steps_work_line(base: Path) -> None:
    home, flow, env, first = _parity_flow(base)
    seen = _walk(home, flow, first, env)
    for step in GENERAL_PROMPT_STEPS:
        expect(step in seen, f"no SPAWN seen for {step}")
        text = _prompt_text(flow, seen[step])
        expect("Run this step's work with:" in text, f"{step} prompt lost the work-with sentence")
        expect("Skill(" not in text, f"{step} prompt gained a Skill call")


def test_p06_header_lines_stand_on_their_own(base: Path) -> None:
    home, flow, env, first = _parity_flow(base)
    seen = _walk(home, flow, first, env)
    for step in PARITY_AGENTS:
        expect(step in seen, f"no SPAWN seen for {step}")
        lines = _prompt_text(flow, seen[step]).splitlines()
        for prefix in ("FLOW_DIR=", "WORKTREE=", "RECEIPT=", "STEP=", "ATTEMPT: ", "NONCE="):
            expect(any(line.startswith(prefix) for line in lines), f"{step} prompt has no line starting {prefix!r}")


def test_p07_spawn_failed_on_implement_pauses_with_resume_guidance(base: Path) -> None:
    home, flow, env, first = _parity_flow(base)
    seen = _walk(home, flow, first, env, stop_at="implement")
    expect("implement" in seen, "no implement SPAWN reached")
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "spawn-failed", extra_env=env)
    verb, directive = next_line(out)
    expect(verb == "STOP", f"spawn-failed printed {verb}, expected STOP: {err}")
    reason = load_json(directive).get("reason", "")
    for needle in ("/setup-local", "restart the session", "recover --choice resume", "flow-implement"):
        expect(needle in reason, f"spawn-failed STOP reason lacks {needle!r}: {reason}")
    expect(pending_of(flow) is None, "pending spawn is not cleared after spawn-failed")
    expect(state_of(flow).get("status") == "paused",
           f"status after spawn-failed is {state_of(flow).get('status')!r}, expected paused")


def test_p08_recover_resume_after_spawn_failed_respawns_same_step_and_agent(base: Path) -> None:
    home, flow, env, first = _parity_flow(base)
    _walk(home, flow, first, env, stop_at="implement")
    run_cli(home, "next", "--flow", str(flow), "--after", "spawn-failed", extra_env=env)
    code, _, err = run_cli(home, "recover", "--flow", str(flow), "--choice", "resume", extra_env=env)
    expect(code == 0, f"recover resume exited {code}: {err}")
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "start", extra_env=env)
    verb, directive = next_line(out)
    expect(verb == "SPAWN", f"next after resume printed {verb}, expected SPAWN")
    respawn = load_json(directive)
    expect(respawn.get("step") == "implement" and respawn.get("agent_type") == "flow-implement",
           f"respawn is {respawn.get('step')!r}/{respawn.get('agent_type')!r}, expected implement/flow-implement")


def test_p09_spawn_failed_without_a_live_spawn_is_not_the_spawn_failed_stop(base: Path) -> None:
    home, flow, env, first = _parity_flow(base)
    code, _, err = run_cli(home, "clear-live", "--flow", str(flow), "--reason", "probe", extra_env=env)
    expect(code == 0, f"clear-live exited {code}: {err}")
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "spawn-failed", extra_env=env)
    verb, directive = next_line(out)
    expect(verb == "STOP", f"spawn-failed with no live spawn printed {verb}, expected STOP")
    reason = load_json(directive).get("reason", "")
    expect("restart the session" not in reason and "/setup-local" not in reason,
           f"no-live-spawn STOP used the spawn-failed message: {reason}")


def test_p10_assess_is_on_by_default_and_no_assess_turns_it_off(base: Path) -> None:
    home = make_home(base)
    flows = home / ".claude" / "flows-v2" / "owner_name"
    on, off = flows / "538-on-abc123", flows / "538-off-abc123"
    for flow in (on, off):
        code, _, err = run_cli(home, "init", "--flow", str(flow), "--worktree", str(home / "wt"),
                               "--repo", "owner/name")
        expect(code == 0, f"init exited {code}: {err}")
    code, _, err = run_cli(home, "start", "--flow", str(on))
    expect(code == 0, f"start exited {code}: {err}")
    code, _, err = run_cli(home, "start", "--flow", str(off), "--no-assess")
    expect(code == 0, f"start --no-assess exited {code}: {err}")
    expect(state_of(on).get("assess") is True, f"default start stored assess={state_of(on).get('assess')!r}")
    expect(state_of(off).get("assess") is False, f"--no-assess stored assess={state_of(off).get('assess')!r}")


def test_p11_stub_doc_names_spawn_failed_and_run_dir_unresolved(base: Path) -> None:
    text = DOC.read_text(encoding="utf-8")
    expect("spawn-failed" in text, "the stub doc never names spawn-failed")
    heading = re.search(r"^#+ .*Recovery", text, re.M)
    expect(heading is not None, "the stub doc has no Recovery heading")
    tail = text[heading.start():]
    expect("spawn-failed" in tail and "run-dir-unresolved" in tail,
           "the Recovery section does not name both spawn-failed and run-dir-unresolved")


def test_p12_stub_doc_bash_grant_is_exactly_the_two_entries(base: Path) -> None:
    frontmatter = DOC.read_text(encoding="utf-8").split("---", 2)[1]
    found = sorted(set(re.findall(r"Bash\([^)]*\)", frontmatter)))
    expected = sorted([
        'Bash(python3 "$HOME/.claude/scripts/flow_driver/__main__.py":*)',
        'Bash(python3 "$HOME/.claude/scripts/fleet.py" wait:*)',
    ])
    expect(found == expected, f"allowed-tools Bash grant is {found}, expected {expected}")
    allowed = [line for line in frontmatter.splitlines() if line.startswith("allowed-tools:")]
    expect(len(allowed) == 1, f"expected one allowed-tools line, found {len(allowed)}")
    expect(re.search(r"\bBash\b(?!\()", allowed[0]) is None,
           f"allowed-tools grants a bare Bash token: {allowed[0]!r}")


def test_p13_stub_doc_pipes_the_assess_reply_through_a_quoted_heredoc(base: Path) -> None:
    text = DOC.read_text(encoding="utf-8")
    expect("foreground" in text.lower(), "the stub doc does not say to run the assessor Task in the foreground")
    expect("<<'" in text, "the stub doc has no quoted heredoc for the assess reply")
    expect("reply.command" in text and "reply.delimiter" in text,
           "the stub doc does not name reply.command and reply.delimiter")


def test_p14_step_agents_cover_every_step_and_match_the_literal_map(base: Path) -> None:
    expect(set(steps.STEP_AGENTS) == set(state.STEPS),
           f"STEP_AGENTS keys {sorted(steps.STEP_AGENTS)} != STEPS {sorted(state.STEPS)}")
    expected = dict(PARITY_AGENTS, assess="flow-assessor", merge="flow-merge")
    expect(dict(steps.STEP_AGENTS) == expected, f"STEP_AGENTS is {dict(steps.STEP_AGENTS)}, expected {expected}")


def test_p15_restricted_steps_are_the_six_flow_command_steps(base: Path) -> None:
    expected = {"plan", "implement", "fix", "review", "ship", "merge"}
    expect(set(loop.RESTRICTED_STEPS) == expected, f"RESTRICTED_STEPS is {sorted(loop.RESTRICTED_STEPS)}")
    expect("assess" not in loop.RESTRICTED_STEPS, "assess must not be a restricted step")


def test_p16_every_flow_agent_in_the_map_has_an_agent_file(base: Path) -> None:
    for agent in set(steps.STEP_AGENTS.values()):
        if agent.startswith("flow-"):
            expect((REPO_ROOT / "agents" / f"{agent}.md").is_file(), f"agents/{agent}.md is missing")


def test_p17_merge_and_audit_agent_constants(base: Path) -> None:
    expect(steps.MERGE_AGENT == "flow-merge", f"MERGE_AGENT is {steps.MERGE_AGENT!r}")
    expect(steps.STEP_AGENTS["merge"] == "flow-merge", "STEP_AGENTS['merge'] is not flow-merge")
    expect(loop.AUDIT_AGENT == "general-purpose", f"AUDIT_AGENT is {loop.AUDIT_AGENT!r}")


def test_p18_decision_vocabulary_and_line_pattern(base: Path) -> None:
    expect(receipts.DECISION_VALUES == ("OK", "FAILED"), f"DECISION_VALUES is {receipts.DECISION_VALUES}")
    expect(receipts._DECISION_LINE.search("Decision: OK") is not None, "Decision: OK does not match")
    expect(receipts._DECISION_LINE.search("Decision: FAILED") is not None, "Decision: FAILED does not match")
    expect(receipts._DECISION_LINE.search("Decision: DONE") is None, "Decision: DONE matches the receipt line")


def test_p19_spawn_failed_is_an_after_event(base: Path) -> None:
    expect("spawn-failed" in model.AFTER_EVENTS, "spawn-failed is not in model.AFTER_EVENTS")


def _tools_value(frontmatter: str) -> str:
    """The value of the frontmatter ``tools:`` key: its own line plus any indented or list continuation lines."""
    out: List[str] = []
    capturing = False
    for line in frontmatter.splitlines():
        if line.startswith("tools:"):
            capturing = True
            out.append(line[len("tools:"):])
        elif capturing and line.startswith((" ", "\t", "-")):
            out.append(line)
        elif capturing:
            break
    return "\n".join(out)


def test_p20_flow_assessor_tools_are_read_only(base: Path) -> None:
    text = (REPO_ROOT / "agents" / "flow-assessor.md").read_text(encoding="utf-8")
    tools = _tools_value(text.split("---", 2)[1])
    expect(tools.strip() != "", "flow-assessor frontmatter has no tools: key")
    expect(not re.search(r"\bWrite\b", tools), "flow-assessor tools grant Write")
    expect(not re.search(r"\bEdit\b", tools), "flow-assessor tools grant Edit")
    expect(re.search(r"\bBash\b(?!\()", tools) is None, f"flow-assessor tools grant a bare Bash token: {tools.strip()!r}")
    for entry in re.findall(r"Bash\(([^)]*)\)", tools):
        expect(re.match(r"^(git (diff|log|show|rev-parse|status)|ls)\b", entry.strip()) is not None,
               f"flow-assessor Bash entry {entry!r} is outside the read-only set")


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
    ("[Parity catch-up #538: agent wiring and prompt shape]", [
        ("p01: SPAWN agent_type per step", test_p01_spawn_agent_types_per_step),
        ("p02: replayed plan SPAWN keeps flow-plan", test_p02_replayed_plan_spawn_keeps_flow_plan),
        ("p03: every prompt has the defence block and Decision line", test_p03_every_step_prompt_has_defence_block_and_decision_line),
        ("p04: restricted prompts invoke the skill and name the contract", test_p04_restricted_step_prompts_invoke_the_skill_and_name_the_contract),
        ("p05: general-purpose prompts keep the work-with sentence", test_p05_general_purpose_prompts_keep_the_run_this_steps_work_line),
        ("p06: header lines stand on their own", test_p06_header_lines_stand_on_their_own),
    ]),
    ("[Parity catch-up #538: spawn-failed, assess default, doc contract, vocabulary]", [
        ("p07: spawn-failed on implement pauses with resume guidance", test_p07_spawn_failed_on_implement_pauses_with_resume_guidance),
        ("p08: recover resume after spawn-failed respawns the same step and agent", test_p08_recover_resume_after_spawn_failed_respawns_same_step_and_agent),
        ("p09: spawn-failed without a live spawn is not the spawn-failed stop", test_p09_spawn_failed_without_a_live_spawn_is_not_the_spawn_failed_stop),
        ("p10: assess default-on, --no-assess turns it off", test_p10_assess_is_on_by_default_and_no_assess_turns_it_off),
        ("p11: stub doc names spawn-failed and run-dir-unresolved in Recovery", test_p11_stub_doc_names_spawn_failed_and_run_dir_unresolved),
        ("p12: stub doc Bash grant is exactly the two entries", test_p12_stub_doc_bash_grant_is_exactly_the_two_entries),
        ("p13: stub doc pipes the assess reply through a quoted heredoc", test_p13_stub_doc_pipes_the_assess_reply_through_a_quoted_heredoc),
        ("p14: STEP_AGENTS covers every step and matches the literal map", test_p14_step_agents_cover_every_step_and_match_the_literal_map),
        ("p15: RESTRICTED_STEPS is the six flow command steps", test_p15_restricted_steps_are_the_six_flow_command_steps),
        ("p16: every flow agent in the map has an agent file", test_p16_every_flow_agent_in_the_map_has_an_agent_file),
        ("p17: MERGE_AGENT and AUDIT_AGENT constants", test_p17_merge_and_audit_agent_constants),
        ("p18: DECISION_VALUES and the Decision line pattern", test_p18_decision_vocabulary_and_line_pattern),
        ("p19: spawn-failed is an after event", test_p19_spawn_failed_is_an_after_event),
        ("p20: flow-assessor tools are read-only", test_p20_flow_assessor_tools_are_read_only),
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
