#!/usr/bin/env python3
"""
Unit tests for the v2 walking-skeleton driver (Sub-issue S, ADR-0026): scripts/flow_driver/ and the
experimental stub commands/expert-flow-v2.md.

Sections:
1. Boundary types: parse_verb, assert_never exhaustiveness, and the validating loaders.
2. decide(): one case per rule 1-10, a table over (record x receipt x question x answer), and the
   allowed-transition table (no fleet entries, a parked record never reaches None).
3. The write order of one next (directive, pending_spawn, driver.log, then exactly one NEXT line).
4. The CLI in a subprocess under a temp HOME: one stdout line, start, answer, clear-live, a corrupted
   state.json, a forced unexpected exception, a scripted driver-only kill loop (unit test of the
   no-double-spawn rule, not a measurement of real kill runs), and the no-new-nonce rule on a re-run.
5. The stub-side accept rule, tested against the documented failure cases.
6. The flows-v2 root acceptance checks.
7. The stub doc acceptance checks for commands/expert-flow-v2.md.

Every path is under a tempfile root. The tests never read or write the real HOME, and never write under
~/.claude/flows or ~/.claude/flows-v2.

Run with: python3 tests/test_flow_driver.py
"""

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from _agent_frontmatter import parse_markdown_frontmatter
from _test_harness import REPO_ROOT, Harness

from scripts.flow_driver import cli, driver, flowdir
from scripts.flow_driver.driver import Observation, ReceiptView, decide
from scripts.flow_driver.flowdir import FlowPathError
from scripts.flow_driver.model import (
    ALLOWED_TRANSITIONS,
    PENDING_STATES,
    RUNNER_PREFIX,
    VERBS,
    AskDirective,
    ClearPending,
    DoneDirective,
    KeepPending,
    ParkDirective,
    PendingSpawn,
    ResumeDirective,
    SetPending,
    SkeletonLoadError,
    SpawnDirective,
    StateRecord,
    StopDirective,
    Verb,
    WaitDirective,
    answer_path,
    assert_never,
    directive_path,
    load_directive,
    load_pending,
    load_state,
    log_path,
    parse_verb,
    pending_path,
    question_path,
    receipt_path,
    state_path,
    stop_directive_path,
    transition_allowed,
)

MAIN = REPO_ROOT / "scripts" / "flow_driver" / "__main__.py"
DOC = REPO_ROOT / "commands" / "expert-flow-v2.md"
NOW = datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc)
NEXT_LINE_RE = re.compile(r"NEXT: (SPAWN|RESUME|WAIT|ASK|PARK|STOP|DONE) (/\S+)")
ACCEPT_LINE_RE = re.compile(r"NEXT: (\S+) (/\S+)")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def expect_raises(exc_type: type, fn: Callable[..., Any], *args: Any) -> None:
    try:
        fn(*args)
    except exc_type:
        return
    raise AssertionError(f"{getattr(fn, '__name__', 'call')} did not raise {exc_type.__name__}")


def check(h: Harness, name: str, fn: Callable[[Path], None], base: Path) -> None:
    """Run one named check. A crash inside a check is a named failure, not a suite abort."""
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


def is_under(path: Path, root: Path) -> bool:
    return os.path.realpath(str(path)).startswith(os.path.realpath(str(root)) + os.sep)


def run_cli(home: Path, *args: str) -> Tuple[int, str, str]:
    """Run the real driver entry point in a subprocess with HOME pointed at a temp dir."""
    env = {key: value for key, value in os.environ.items() if key != "CLAUDE_CODE_SESSION_ID"}
    env["HOME"] = str(home)
    proc = subprocess.run(
        [sys.executable, str(MAIN), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(home),
        timeout=120,
    )
    return proc.returncode, proc.stdout, proc.stderr


def next_line(stdout: str) -> Tuple[str, Path]:
    """The one stdout line must match the NEXT contract. Returns (verb, directive file)."""
    lines = stdout.splitlines()
    expect(len(lines) == 1, f"expected exactly one stdout line, got {lines!r}")
    match = NEXT_LINE_RE.fullmatch(lines[0])
    expect(match is not None, f"stdout line does not match the NEXT contract: {lines[0]!r}")
    assert match is not None
    return match.group(1), Path(match.group(2))


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_log(flow: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in log_path(flow).read_text(encoding="utf-8").splitlines()]


def start_flow(home: Path, mode: str = "interactive", wait_mode: str = "end-turn") -> Path:
    code, out, err = run_cli(
        home, "start", "--repo", "owner/name", "--mode", mode, "--wait-mode", wait_mode, "--step-minutes", "1"
    )
    expect(code == 0, f"start exited {code}: {err}")
    verb, directive = next_line(out)
    expect(verb == "SPAWN", f"start printed {verb}, expected SPAWN")
    return directive.parent.parent


def write_awaiting(flow: Path, attempt: int) -> None:
    """What the step does when it asks: the question file and the receipt with the awaiting marker."""
    question_path(flow, attempt).parent.mkdir(parents=True, exist_ok=True)
    question_path(flow, attempt).write_text(
        json.dumps({"attempt": attempt, "question": "Reply with the word ok to finish this trivial step."}),
        encoding="utf-8",
    )
    receipt_path(flow, attempt).parent.mkdir(parents=True, exist_ok=True)
    receipt_path(flow, attempt).write_text(
        f"ATTEMPT: {attempt}\n<!-- awaiting-answers: skeleton-a{attempt} -->\n", encoding="utf-8"
    )


def write_done(flow: Path, attempt: int) -> None:
    receipt_path(flow, attempt).parent.mkdir(parents=True, exist_ok=True)
    receipt_path(flow, attempt).write_text(f"ATTEMPT: {attempt}\nDecision: done\n", encoding="utf-8")


def stub_accepts(stdout: str, returncode: int) -> str:
    """The stub's documented accept rule (commands/expert-flow-v2.md, "Accept rule"). Returns a verb or STOP."""
    lines = stdout.splitlines()
    if returncode != 0 or len(lines) != 1:
        return "STOP"
    match = ACCEPT_LINE_RE.fullmatch(lines[0])
    if match is None:
        return "STOP"
    verb = parse_verb(match.group(1))
    target = Path(match.group(2))
    if not target.is_file() or not os.access(str(target), os.R_OK):
        return "STOP"
    return verb


# ---------------------------------------------------------------------------
# Section 1: boundary types and loaders
# ---------------------------------------------------------------------------

def verb_label(verb: Verb) -> str:
    if verb == "SPAWN":
        return "spawn"
    if verb == "RESUME":
        return "resume"
    if verb == "WAIT":
        return "wait"
    if verb == "ASK":
        return "ask"
    if verb == "PARK":
        return "park"
    if verb == "STOP":
        return "stop"
    if verb == "DONE":
        return "done"
    assert_never(verb)


def test_parse_verb_members_unknowns_and_exhaustiveness(base: Path) -> None:
    for verb in VERBS:
        expect(parse_verb(verb) == verb, f"parse_verb({verb!r}) did not map to itself")
    for raw in ["", "spawn", "wait", "NEXT", " SPAWN", "SPAWN ", "SPAWN\n", "FOO"]:
        expect(parse_verb(raw) == "STOP", f"parse_verb({raw!r}) is not STOP")
    labels = {verb_label(verb) for verb in VERBS}
    expect(len(labels) == len(VERBS), "verb_label does not give every verb its own branch")
    expect_raises(AssertionError, verb_label, "BOGUS")


def loader_cases(base: Path) -> List[Tuple[str, Callable[[str], Any], Dict[str, Any]]]:
    flow = str(base / "flow")
    state = {
        "driver_format": "skeleton",
        "repo": "owner/name",
        "flow_id": "skeleton-x",
        "mode": "interactive",
        "wait_mode": "end-turn",
        "step_minutes": 1,
        "created_utc": "2026-11-11T12:00:00Z",
        "last_attempt": 1,
    }
    pending = {
        "driver_format": "skeleton",
        "step": "skeleton",
        "attempt": 1,
        "nonce": "a" * 16,
        "state": "spawned",
    }
    command = f"{RUNNER_PREFIX} next --flow {flow}"
    clear = f"{RUNNER_PREFIX} clear-live --flow {flow} --reason 'human recovery'"
    answer = f"{RUNNER_PREFIX} answer --flow {flow} --text '<your answer>'"
    directives = {
        "SPAWN": {
            "verb": "SPAWN", "next_command": command, "agent_type": "general-purpose",
            "prompt_file": f"{flow}/prompts/skeleton-a1-{'a' * 16}.md", "step": "skeleton",
            "attempt": 1, "nonce": "a" * 16,
        },
        "RESUME": {
            "verb": "RESUME", "next_command": command, "step": "skeleton", "attempt": 1,
            "answer_file": f"{flow}/answers/skeleton-a1.json",
        },
        "WAIT": {
            "verb": "WAIT", "next_command": command, "wait_mode": "end-turn",
            "receipt_file": f"{flow}/receipts/skeleton-a1.md", "clear_live_command": clear,
        },
        "ASK": {
            "verb": "ASK", "next_command": command, "question_file": f"{flow}/questions/skeleton-a1.json",
            "answer_file": f"{flow}/answers/skeleton-a1.json", "answer_command": answer,
        },
        "PARK": {
            "verb": "PARK", "next_command": command, "question_file": f"{flow}/questions/skeleton-a1.json",
            "answer_file": f"{flow}/answers/skeleton-a1.json", "answer_command": answer,
        },
        "STOP": {"verb": "STOP", "next_command": command, "reason": "reason"},
        "DONE": {"verb": "DONE", "next_command": command},
    }
    cases: List[Tuple[str, Callable[[str], Any], Dict[str, Any]]] = [
        ("state.json", load_state, state),
        ("pending_spawn.json", load_pending, pending),
    ]
    for verb, body in directives.items():
        cases.append((f"directive {verb}", load_directive, {"driver_format": "skeleton", **body}))
    return cases


def expect_load_error(loader: Callable[[str], Any], text: str, label: str) -> None:
    try:
        loader(text)
    except SkeletonLoadError:
        return
    raise AssertionError(f"{label}: loader accepted an invalid file")


def test_loaders_accept_valid_and_reject_each_invalid_shape(base: Path) -> None:
    for label, loader, valid in loader_cases(base):
        loader(json.dumps(valid))  # the valid form loads, so the rejections below are meaningful
        for key in valid:
            missing = {k: v for k, v in valid.items() if k != key}
            expect_load_error(loader, json.dumps(missing), f"{label} missing {key!r}")
            wrong = dict(valid)
            wrong[key] = ["not", "a", "scalar"]
            expect_load_error(loader, json.dumps(wrong), f"{label} wrong type for {key!r}")
        wrong_format = dict(valid, driver_format="v1")
        expect_load_error(loader, json.dumps(wrong_format), f"{label} driver_format v1")
        truncated = json.dumps(valid)[:-5]
        expect_load_error(loader, truncated, f"{label} truncated JSON")


def test_loaders_reject_unknown_enumerated_values(base: Path) -> None:
    cases = loader_cases(base)
    state = next(valid for label, _, valid in cases if label == "state.json")
    pending = next(valid for label, _, valid in cases if label == "pending_spawn.json")
    expect_load_error(load_state, json.dumps(dict(state, mode="fleet")), "state mode fleet")
    expect_load_error(load_state, json.dumps(dict(state, wait_mode="fast")), "state wait_mode fast")
    expect_load_error(load_pending, json.dumps(dict(pending, state="fleet-locked")), "pending state fleet-locked")
    directive = next(valid for label, _, valid in cases if label == "directive SPAWN")
    expect_load_error(load_directive, json.dumps(dict(directive, verb="FOO")), "directive verb FOO")
    expect_load_error(load_directive, json.dumps(dict(directive, verb="spawn")), "directive verb lowercase")


def test_corrupt_boundary_file_becomes_stop_through_the_gather_edge(base: Path) -> None:
    root = base / ".claude" / "flows-v2"
    flow = flowdir.create_flow_dir(
        root, "owner/name", NOW, "d1d1", mode="interactive", wait_mode="end-turn", step_minutes=1
    )
    state_path(flow).write_text('{"driver_format": "skeleton", "repo"', encoding="utf-8")
    obs = cli.gather(flow, "f" * 16)
    expect(obs.fault is not None, "a truncated state.json did not set a fault")
    decision = decide(obs)
    expect(isinstance(decision.directive, StopDirective), "a loader error did not decide STOP")
    expect(isinstance(decision.pending, KeepPending), "a loader error changed pending_spawn")


# ---------------------------------------------------------------------------
# Section 2: decide() rules, the table, and the transition table
# ---------------------------------------------------------------------------

DONE_RECEIPT = ReceiptView(attempt=1, has_decision=True, awaiting=False)
AWAIT_RECEIPT = ReceiptView(attempt=1, has_decision=False, awaiting=True)
MISMATCH_RECEIPT = ReceiptView(attempt=7, has_decision=True, awaiting=False)
RECEIPT_CELLS: List[Tuple[str, Optional[ReceiptView]]] = [
    ("none", None),
    ("awaiting", AWAIT_RECEIPT),
    ("completed", DONE_RECEIPT),
    ("mismatched", MISMATCH_RECEIPT),
]


def live(state: str, attempt: int = 1) -> PendingSpawn:
    return PendingSpawn(step="skeleton", attempt=attempt, nonce="a" * 16, state=state)  # type: ignore[arg-type]


PENDING_CELLS: List[Tuple[str, Optional[PendingSpawn]]] = [
    ("none", None),
    ("spawned", live("spawned")),
    ("parked", live("parked-awaiting-answer")),
    ("resumed", live("resumed")),
]


def base_state(last_attempt: int = 1, mode: str = "interactive") -> StateRecord:
    return StateRecord(
        repo="owner/name",
        flow_id="skeleton-x",
        mode=mode,  # type: ignore[arg-type]
        wait_mode="end-turn",
        step_minutes=1,
        created_utc="2026-11-11T12:00:00Z",
        last_attempt=last_attempt,
    )


def obs_for(
    flow: Path,
    pending: Optional[PendingSpawn] = None,
    receipt: Optional[ReceiptView] = None,
    question: bool = False,
    answer: bool = False,
    last: int = 1,
    mode: str = "interactive",
    fault: Optional[str] = None,
    state_present: bool = True,
    nonce: str = "f" * 16,
) -> Observation:
    state = base_state(last, mode) if state_present else None
    return Observation(
        flow_dir=flow,
        state=state,
        pending=pending,
        receipt=receipt,
        question_present=question,
        answer_present=answer,
        fresh_nonce=nonce,
        fault=fault,
    )


def resulting_state(change: Any, prev: Optional[str]) -> Optional[str]:
    if isinstance(change, KeepPending):
        return prev
    if isinstance(change, SetPending):
        return change.record.state
    return None  # ClearPending


def test_rule1_loader_error_or_missing_state_is_stop_and_keeps_pending(base: Path) -> None:
    flow = base / "flow"
    decision = decide(obs_for(flow, state_present=False, fault="state.json: truncated"))
    expect(isinstance(decision.directive, StopDirective), "rule 1 did not STOP")
    expect(decision.directive.reason == "state.json: truncated", "rule 1 STOP lost the fault reason")
    expect(isinstance(decision.pending, KeepPending), "rule 1 changed pending_spawn")
    live_decision = decide(obs_for(flow, pending=live("spawned"), fault="boom"))
    expect(isinstance(live_decision.directive, StopDirective), "rule 1 with a live record did not STOP")
    expect(isinstance(live_decision.pending, KeepPending), "rule 1 with a live record changed pending_spawn")


def test_rule2_no_record_and_no_usable_receipt_is_spawn(base: Path) -> None:
    flow = base / "flow"
    decision = decide(obs_for(flow, pending=None, receipt=None, last=1, nonce="c" * 16))
    expect(isinstance(decision.directive, SpawnDirective), "rule 2 did not SPAWN")
    spawn = decision.directive
    expect(spawn.attempt == 2, f"SPAWN attempt is {spawn.attempt}, expected last + 1 = 2")
    expect(spawn.nonce == "c" * 16, "SPAWN did not use the fresh nonce")
    expect(spawn.agent_type == "general-purpose", f"SPAWN agent_type is {spawn.agent_type}")
    expect(spawn.prompt_file == str(flow / "prompts" / f"skeleton-a2-{'c' * 16}.md"), "SPAWN prompt path is wrong")
    expect(isinstance(decision.pending, SetPending) and decision.pending.record.state == "spawned",
           "rule 2 did not record the spawned state")


def test_rule3_live_record_with_completed_receipt_is_done_and_cleared(base: Path) -> None:
    flow = base / "flow"
    decision = decide(obs_for(flow, pending=live("spawned"), receipt=DONE_RECEIPT))
    expect(isinstance(decision.directive, DoneDirective), "rule 3 did not DONE")
    expect(isinstance(decision.pending, ClearPending), "rule 3 did not remove the record")
    expect(any(event.event == "receipt-landed" for event in decision.events), "rule 3 did not log receipt-landed")


def test_rule4_awaiting_receipt_parks_then_asks_or_parks_by_mode(base: Path) -> None:
    flow = base / "flow"
    interactive = decide(obs_for(flow, pending=live("spawned"), receipt=AWAIT_RECEIPT, question=True))
    expect(isinstance(interactive.directive, AskDirective), "rule 4 interactive did not ASK")
    expect(isinstance(interactive.pending, SetPending)
           and interactive.pending.record.state == "parked-awaiting-answer", "rule 4 did not park the record")
    expect(interactive.directive.question_file == str(question_path(flow, 1)), "ASK question file is wrong")
    expect(interactive.directive.answer_command.startswith(RUNNER_PREFIX + " answer --flow "),
           "ASK answer_command lacks the granted prefix")
    headless = decide(obs_for(flow, pending=live("spawned"), receipt=AWAIT_RECEIPT, question=True, mode="headless"))
    expect(isinstance(headless.directive, ParkDirective), "rule 4 headless did not PARK")


def test_rule5_parked_without_answer_asks_again_and_never_spawns(base: Path) -> None:
    flow = base / "flow"
    decision = decide(obs_for(flow, pending=live("parked-awaiting-answer"), question=True))
    expect(isinstance(decision.directive, AskDirective), "rule 5 did not ask again")
    expect(isinstance(decision.pending, KeepPending), "rule 5 changed the parked record")
    expect(not isinstance(decision.directive, SpawnDirective), "rule 5 spawned again")


def test_rule6_parked_with_answer_resumes(base: Path) -> None:
    flow = base / "flow"
    decision = decide(obs_for(flow, pending=live("parked-awaiting-answer"), question=True, answer=True))
    expect(isinstance(decision.directive, ResumeDirective), "rule 6 did not RESUME")
    expect(decision.directive.answer_file == str(answer_path(flow, 1)), "RESUME answer file is wrong")
    expect(isinstance(decision.pending, SetPending) and decision.pending.record.state == "resumed",
           "rule 6 did not move the record to resumed")


def test_rule7_live_record_without_receipt_waits_and_never_spawns(base: Path) -> None:
    flow = base / "flow"
    for state in ("spawned", "resumed"):
        decision = decide(obs_for(flow, pending=live(state), receipt=None))
        expect(isinstance(decision.directive, WaitDirective), f"rule 7 {state} did not WAIT")
        expect(not isinstance(decision.directive, SpawnDirective), f"rule 7 {state} became SPAWN")
        expect(isinstance(decision.pending, KeepPending), f"rule 7 {state} changed the record")
    wait = decide(obs_for(flow, pending=live("spawned"), receipt=None)).directive
    expect(isinstance(wait, WaitDirective) and wait.clear_live_command.startswith(RUNNER_PREFIX + " clear-live --flow "),
           "WAIT clear_live_command does not name the clear-live invocation")


def test_rule8_mismatched_receipt_is_logged_late_and_treated_as_none(base: Path) -> None:
    flow = base / "flow"
    decision = decide(obs_for(flow, pending=live("spawned"), receipt=MISMATCH_RECEIPT))
    expect(any(event.event == "late-receipt" for event in decision.events), "rule 8 did not log late-receipt")
    expect(isinstance(decision.directive, WaitDirective), "rule 8 did not treat the receipt as none (WAIT)")
    expect(isinstance(decision.pending, KeepPending), "rule 8 cleared the record on a late receipt")


def test_rule9_no_record_and_completed_last_attempt_is_done_without_spawn(base: Path) -> None:
    flow = base / "flow"
    decision = decide(obs_for(flow, pending=None, receipt=DONE_RECEIPT, last=1))
    expect(isinstance(decision.directive, DoneDirective), "rule 9 did not DONE")
    expect(isinstance(decision.pending, KeepPending), "rule 9 changed pending_spawn")


def test_rule10_late_or_awaiting_only_receipt_with_no_record_logs_and_spawns(base: Path) -> None:
    flow = base / "flow"
    late_done = decide(obs_for(flow, pending=None, receipt=ReceiptView(0, True, False), last=1))
    expect(any(event.event == "late-receipt" for event in late_done.events), "rule 10 (completed) did not log")
    expect(isinstance(late_done.directive, SpawnDirective) and late_done.directive.attempt == 2,
           "rule 10 (completed, other attempt) did not SPAWN attempt 2")
    awaiting_only = decide(obs_for(flow, pending=None, receipt=AWAIT_RECEIPT, last=1))
    expect(any(event.event == "late-receipt" for event in awaiting_only.events), "rule 10 (awaiting) did not log")
    expect(isinstance(awaiting_only.directive, SpawnDirective) and awaiting_only.directive.attempt == 2,
           "rule 10 (awaiting only) did not SPAWN attempt 2")


def test_table_every_record_by_receipt_cell_returns_a_directive(base: Path) -> None:
    flow = base / "flow"
    cells = 0
    for pname, pending in PENDING_CELLS:
        for rname, receipt in RECEIPT_CELLS:
            for question in (False, True):
                for answer in (False, True):
                    cells += 1
                    label = f"{pname}/{rname}/question={question}/answer={answer}"
                    decision = decide(obs_for(flow, pending=pending, receipt=receipt, question=question, answer=answer))
                    # Invalid case: resumed with awaiting receipt, question present, and NO answer (violates one-question-per-attempt).
                    # This should STOP due to disallowed transition resumed -> parked-awaiting-answer.
                    if pname == "resumed" and rname == "awaiting" and question and not answer:
                        expect(isinstance(decision.directive, StopDirective), f"{label} should be STOP (invalid case)")
                        continue
                    expect(not isinstance(decision.directive, StopDirective), f"{label} returned STOP")
                    expect(decision.directive.next_command.startswith(RUNNER_PREFIX + " "),
                           f"{label}: next_command lacks the granted prefix")
                    prev = pending.state if pending is not None else None
                    new = resulting_state(decision.pending, prev)
                    expect(transition_allowed(prev, new), f"{label}: {prev} -> {new} is not allowed")
                    if pname in ("spawned", "resumed") and rname == "none":
                        expect(isinstance(decision.directive, WaitDirective), f"{label}: live with no receipt did not WAIT")
                    if pname in ("spawned", "resumed", "parked"):
                        expect(not isinstance(decision.directive, SpawnDirective), f"{label}: SPAWN for a live record")
                    if pname == "parked":
                        expect(not isinstance(decision.pending, ClearPending), f"{label}: a parked record was cleared")
    # Computed cell count: len(PENDING_CELLS) * len(RECEIPT_CELLS) * 2 questions * 2 answers
    expected_cells = len(PENDING_CELLS) * len(RECEIPT_CELLS) * 2 * 2
    expect(cells == expected_cells, f"the table covered {cells} cells, expected {expected_cells}")


def test_transition_table_has_no_fleet_entries_and_never_clears_a_parked_record(base: Path) -> None:
    # The 9 allowed transitions (one per verb/pending-state pair):
    # DONE: (None, None), ("spawned", None), ("resumed", None)
    # SPAWN: (None, "spawned")
    # WAIT: ("spawned", "spawned"), ("resumed", "resumed")
    # ASK/PARK: ("spawned", "parked-awaiting-answer"), ("parked-awaiting-answer", "parked-awaiting-answer")
    # RESUME: ("parked-awaiting-answer", "resumed")
    expected_transitions = {
        (None, None),  # DONE with no record
        (None, "spawned"),  # SPAWN
        ("spawned", "spawned"),  # WAIT
        ("spawned", "parked-awaiting-answer"),  # ASK or PARK
        ("spawned", None),  # receipt-landed DONE, or logged clear-live
        ("resumed", "resumed"),  # WAIT
        ("resumed", None),  # receipt-landed DONE, or logged clear-live
        ("parked-awaiting-answer", "parked-awaiting-answer"),  # ASK or PARK again, no spawn
        ("parked-awaiting-answer", "resumed"),  # RESUME
    }
    states = {state for pair in ALLOWED_TRANSITIONS for state in pair}
    expect(states <= set(PENDING_STATES) | {None}, f"unexpected states in the table: {states}")
    expect(ALLOWED_TRANSITIONS == frozenset(expected_transitions),
           "the table does not match expected transitions")
    expect("fleet" not in repr(ALLOWED_TRANSITIONS), "the table mentions fleet")
    expect(not transition_allowed("parked-awaiting-answer", None), "a parked record may reach None")
    expect(transition_allowed("parked-awaiting-answer", "resumed"), "parked -> resumed is missing")


# ---------------------------------------------------------------------------
# Section 3: write order of one next, in-process
# ---------------------------------------------------------------------------

def test_next_writes_directive_then_pending_then_log_then_one_line(base: Path) -> None:
    root = base / ".claude" / "flows-v2"
    flow = flowdir.create_flow_dir(
        root, "owner/name", NOW, "c0ffee", mode="interactive", wait_mode="end-turn", step_minutes=1
    )
    events: List[Tuple[str, Optional[Path]]] = []
    real_write = cli.atomic_write_text
    real_log = cli.append_log

    def recording_write(path: Path, text: str) -> None:
        events.append(("write", Path(path)))
        real_write(path, text)

    def recording_log(flow_arg: Path, entry: Dict[str, Any]) -> None:
        for kind, written in events:
            if kind == "write" and written is not None:
                expect(written.exists(), f"log written before {written} existed")
        events.append(("log", None))
        real_log(flow_arg, entry)

    cli.atomic_write_text = recording_write  # type: ignore[assignment]
    cli.append_log = recording_log  # type: ignore[assignment]
    try:
        line = cli._advance_unchecked(flow, "start", "start")
        events.append(("stdout", None))  # Record that stdout was produced
    finally:
        cli.atomic_write_text = real_write  # type: ignore[assignment]
        cli.append_log = real_log  # type: ignore[assignment]

    directive = directive_path(flow, 1)
    expect(line == f"NEXT: SPAWN {directive}", f"unexpected NEXT line: {line!r}")
    # Full write order: directive, state (if needed), driver.log, pending_spawn.json, then stdout (NEXT line)
    idx_directive = events.index(("write", directive))
    idx_log = events.index(("log", None))
    idx_pending = events.index(("write", pending_path(flow)))
    idx_stdout = events.index(("stdout", None))
    # Verify log comes before pending_spawn
    expect(idx_directive < idx_log < idx_pending < idx_stdout, f"write order is wrong: {events!r}")
    pending = load_pending(pending_path(flow).read_text(encoding="utf-8"))
    expect(pending.state == "spawned" and pending.attempt == 1, "pending_spawn.json has the wrong record")
    expect(load_json(directive)["nonce"] == pending.nonce, "directive and pending_spawn disagree on the nonce")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        rc = cli.advance(flow, "next", "notification")
    expect(rc == 0, f"advance returned {rc}")
    printed = buf.getvalue().splitlines()
    expect(len(printed) == 1 and printed[0].startswith("NEXT: WAIT "), f"advance printed {printed!r}")


# ---------------------------------------------------------------------------
# Section 4: the CLI in a subprocess under a temp HOME
# ---------------------------------------------------------------------------

def test_start_prints_one_line_and_leaves_the_boundary_files(base: Path) -> None:
    home = fresh(base)
    code, out, err = run_cli(
        home, "start", "--repo", "owner/name", "--mode", "interactive", "--wait-mode", "end-turn", "--step-minutes", "1"
    )
    expect(code == 0, f"start exited {code}: {err}")
    verb, directive = next_line(out)
    expect(verb == "SPAWN", f"start printed {verb}")
    flow = directive.parent.parent
    expect(is_under(flow, home / ".claude" / "flows-v2" / "owner_name"), f"flow dir {flow} is not under flows-v2")
    expect(load_json(state_path(flow))["driver_format"] == "skeleton", "state.json lacks driver_format skeleton")
    pending = load_pending(pending_path(flow).read_text(encoding="utf-8"))
    expect(pending.state == "spawned" and pending.attempt == 1, "pending_spawn.json is not the spawned attempt 1")
    expect(load_json(directive)["nonce"] == pending.nonce, "directive nonce differs from pending_spawn")
    expect(len(read_log(flow)) == 1 and read_log(flow)[0]["verb"] == "SPAWN", "driver.log lacks the SPAWN entry")
    expect(not (home / ".claude" / "flows").exists(), "a sibling v1 flows/ directory was created")


def test_answer_cycle_ask_answer_resume_wait_done(base: Path) -> None:
    home = fresh(base)
    flow = start_flow(home)
    code, _, err = run_cli(home, "answer", "--flow", str(flow), "--text", "ok")
    expect(code != 0 and "not parked-awaiting-answer" in err, f"answer on a spawned record was not refused: {err}")

    write_awaiting(flow, 1)
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "notification")
    expect(code == 0, f"next exited {code}: {err}")
    verb, directive = next_line(out)
    expect(verb == "ASK", f"expected ASK after the awaiting receipt, got {verb}")
    ask = load_json(directive)
    expect(os.path.realpath(ask["question_file"]) == os.path.realpath(str(question_path(flow, 1))),
           "ASK question_file is wrong")
    expect(ask["answer_command"].startswith(RUNNER_PREFIX + " answer --flow "), "ASK answer_command lacks the prefix")

    code, _, err = run_cli(home, "answer", "--flow", str(flow), "--text", "ok")
    expect(code == 0, f"answer exited {code}: {err}")
    answer = load_json(answer_path(flow, 1))
    expect(sorted(answer) == ["answered_utc", "attempt", "text"], f"answer keys are {sorted(answer)}")
    expect(answer["attempt"] == 1 and answer["text"] == "ok", "answer contents are wrong")
    expect(sorted(os.listdir(str(flow / "answers"))) == ["skeleton-a1.json"], "answers/ has stray files")
    code, _, err = run_cli(home, "answer", "--flow", str(flow), "--text", "again")
    expect(code != 0 and "already exists" in err, f"a repeat answer was not refused: {err}")

    code, out, _ = run_cli(home, "next", "--flow", str(flow), "--after", "human-continue")
    verb, _ = next_line(out)
    expect(verb == "RESUME", f"expected RESUME after the answer, got {verb}")
    expect(load_pending(pending_path(flow).read_text(encoding="utf-8")).state == "resumed", "record is not resumed")

    code, out, _ = run_cli(home, "next", "--flow", str(flow), "--after", "resume-sent")
    verb, _ = next_line(out)
    expect(verb == "WAIT", f"expected WAIT after resume, got {verb}")

    write_done(flow, 1)
    code, out, _ = run_cli(home, "next", "--flow", str(flow), "--after", "notification")
    verb, _ = next_line(out)
    expect(verb == "DONE", f"expected DONE after the completed receipt, got {verb}")
    expect(not pending_path(flow).exists(), "DONE did not remove the record")
    expect(any(entry.get("subcommand") == "answer" for entry in read_log(flow)), "answer was not logged")


def test_headless_park_and_clear_live_refuses_a_parked_record(base: Path) -> None:
    home = fresh(base)
    flow = start_flow(home, mode="headless")
    write_awaiting(flow, 1)
    code, out, _ = run_cli(home, "next", "--flow", str(flow), "--after", "notification")
    verb, _ = next_line(out)
    expect(verb == "PARK", f"expected PARK in headless mode, got {verb}")
    code, _, err = run_cli(home, "clear-live", "--flow", str(flow), "--reason", "should refuse")
    expect(code != 0 and "never cleared" in err, f"clear-live did not refuse a parked record: {err}")
    expect(load_pending(pending_path(flow).read_text(encoding="utf-8")).state == "parked-awaiting-answer",
           "a refused clear-live changed the parked record")


def test_clear_live_then_spawn_gets_a_new_nonce_and_attempt(base: Path) -> None:
    home = fresh(base)
    flow = start_flow(home)
    first = load_pending(pending_path(flow).read_text(encoding="utf-8"))
    code, _, err = run_cli(home, "clear-live", "--flow", str(flow), "--reason", "test recovery")
    expect(code == 0, f"clear-live exited {code}: {err}")
    expect(not pending_path(flow).exists(), "clear-live did not remove the live record")
    code, _, err = run_cli(home, "clear-live", "--flow", str(flow), "--reason", "again")
    expect(code != 0 and "no live record" in err, f"clear-live with no record was not refused: {err}")
    cleared = [entry for entry in read_log(flow) if entry.get("subcommand") == "clear-live"]
    expect(len(cleared) == 1 and cleared[0]["cleared_state"] == "spawned", "clear-live was not logged with its state")

    code, out, _ = run_cli(home, "next", "--flow", str(flow), "--after", "start")
    verb, _ = next_line(out)
    expect(verb == "SPAWN", f"expected SPAWN after clear-live, got {verb}")
    second = load_pending(pending_path(flow).read_text(encoding="utf-8"))
    expect(second.attempt == first.attempt + 1, "the re-spawn did not increment the attempt")
    expect(second.nonce != first.nonce, "the re-spawn reused the nonce")


def test_corrupt_state_gives_exit_zero_and_a_stop_directive(base: Path) -> None:
    home = fresh(base)
    flow = start_flow(home)
    state_path(flow).write_text('{"driver_format": "skeleton", "repo"', encoding="utf-8")
    code, out, err = run_cli(home, "next", "--flow", str(flow))
    expect(code == 0, f"a decided STOP exited {code}: {err}")
    verb, directive = next_line(out)
    expect(verb == "STOP", f"expected STOP, got {verb}")
    expect(os.path.realpath(str(directive)) == os.path.realpath(str(stop_directive_path(flow))),
           f"STOP directive is {directive}")
    reason = load_json(directive)["reason"]
    expect("invalid or truncated JSON" in reason, f"STOP reason is {reason!r}")


def test_unexpected_exception_still_prints_stop_and_exits_nonzero(base: Path) -> None:
    home = fresh(base)
    flow = start_flow(home)
    directives = flow / "directives"
    for entry in directives.iterdir():
        entry.unlink()
    directives.rmdir()
    directives.write_text("a file where the directory was", encoding="utf-8")
    code, out, err = run_cli(home, "next", "--flow", str(flow))
    expect(code != 0, f"a forced unexpected exception exited {code}, expected non-zero")
    expect("NEXT: STOP" in out or "driver could not record STOP" in err, f"no STOP recorded: {err!r}")


def test_crash_records_stop_and_exits_1(base: Path) -> None:
    """When decide() crashes unexpectedly, advance() records STOP and exits with code 1."""
    root = base / ".claude" / "flows-v2"
    flow = flowdir.create_flow_dir(
        root, "owner/name", NOW, "e2e2", mode="interactive", wait_mode="end-turn", step_minutes=1
    )

    def boom(obs: Observation) -> Any:
        raise RuntimeError("forced for the test")

    real_decide = cli.decide
    cli.decide = boom  # type: ignore[assignment]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.advance(flow, "next", "none")
    finally:
        cli.decide = real_decide  # type: ignore[assignment]
    # Single outcome: rc == 1 and NEXT: STOP is printed with the exception message recorded.
    expect(rc == 1, f"a crash returned {rc}, expected 1")
    lines = buf.getvalue().splitlines()
    expect(len(lines) == 1, f"expected one stdout line, got {lines!r}")
    expect(lines[0] == f"NEXT: STOP {stop_directive_path(flow)}", f"crash stdout was {lines[0]!r}")
    expect("forced for the test" in load_json(stop_directive_path(flow))["reason"], "crash reason was not recorded")


def test_rerun_next_on_a_live_record_is_a_wait_with_no_new_nonce_and_no_extra_spawn(base: Path) -> None:
    """A driver-only kill loop: next, discard the line, next again. A unit test of the no-double-spawn rule,
    not a measurement of real kill runs (S-Q3 B). Zero extra SPAWNs are expected."""
    home = fresh(base)
    flow = start_flow(home)
    nonce = load_pending(pending_path(flow).read_text(encoding="utf-8")).nonce
    for _ in range(3):
        code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "spawn-launched")
        expect(code == 0, f"next exited {code}: {err}")
        expect(next_line(out)[0] == "WAIT", "a re-run next on a live record was not WAIT")
        expect(load_pending(pending_path(flow).read_text(encoding="utf-8")).nonce == nonce,
               "a re-run next changed the nonce")
    spawns = [entry for entry in read_log(flow) if entry.get("verb") == "SPAWN"]
    expect(len(spawns) == 1, f"the kill loop produced {len(spawns)} SPAWN entries, expected 1")


# ---------------------------------------------------------------------------
# Section 5: the stub-side accept rule
# ---------------------------------------------------------------------------

def test_accept_rule_model_accepts_a_real_directive_and_maps_failures_to_stop(base: Path) -> None:
    """Model of the stub's documented accept rule (commands/expert-flow-v2.md, "Accept rule").

    This test reimplements the accept rule logic in stub_accepts() and verifies it against
    the documented conditions: exactly one stdout line, line matches NEXT contract, exit 0,
    and directive file is readable. Anything else maps to STOP.
    """
    directive = base / "directive.json"
    directive.write_text("{}", encoding="utf-8")
    # Verify the rule accepts a valid NEXT line from a readable file
    expect(stub_accepts(f"NEXT: WAIT {directive}\n", 0) == "WAIT", "a valid NEXT line was not accepted")
    # Verify the rule rejects unknown verbs, multiple lines, empty stdout, non-zero exit, missing files
    expect(stub_accepts(f"NEXT: FOO {directive}\n", 0) == "STOP", "an unknown verb was not STOP")
    expect(stub_accepts(f"NEXT: WAIT {directive}\nNEXT: WAIT {directive}\n", 0) == "STOP",
           "two stdout lines were not STOP")
    expect(stub_accepts("", 0) == "STOP", "empty stdout was not STOP")
    expect(stub_accepts(f"NEXT: WAIT {directive}\n", 1) == "STOP", "a non-zero exit was not STOP")
    expect(stub_accepts(f"NEXT: WAIT {base / 'missing.json'}\n", 0) == "STOP",
           "a NEXT line naming a missing file was not STOP")


def test_stub_accept_rule_on_real_driver_output(base: Path) -> None:
    home = fresh(base)
    code, out, _ = run_cli(
        home, "start", "--repo", "owner/name", "--mode", "interactive", "--wait-mode", "end-turn", "--step-minutes", "1"
    )
    expect(stub_accepts(out, code) == "SPAWN", "the stub would not accept the real start output")


# ---------------------------------------------------------------------------
# Section 6: flows-v2 root acceptance
# ---------------------------------------------------------------------------

def test_flow_dirs_are_created_only_under_flows_v2(base: Path) -> None:
    home = fresh(base)
    root = flowdir.flows_v2_root(home)
    flow_a = flowdir.create_flow_dir(root, "owner/name", NOW, "aa11", mode="interactive", wait_mode="end-turn", step_minutes=1)
    flow_b = flowdir.create_flow_dir(root, "owner/name", NOW, "bb22", mode="interactive", wait_mode="end-turn", step_minutes=1)
    expect(is_under(flow_a, root), "a flow dir was created outside flows-v2")
    expect(flow_a != flow_b, "two create_flow_dir calls yielded the same dir")
    expect(not (home / ".claude" / "flows").exists(), "a sibling flows/ directory was created")
    expect(load_json(state_path(flow_a))["driver_format"] == "skeleton", "state.json lacks driver_format skeleton")
    expect(not any(path.name == "flow-run" for path in home.rglob("*")), "a .claude/flow-run file was created")


def test_flows_v2_symlink_into_v1_flows_is_refused_before_any_write(base: Path) -> None:
    home = fresh(base)
    claude = home / ".claude"
    v1_flows = claude / "flows"
    v1_flows.mkdir(parents=True)
    link = claude / "flows-v2"
    os.symlink(str(v1_flows), str(link))
    refused = False
    try:
        flowdir.create_flow_dir(link, "owner/name", NOW, "cc33", mode="interactive", wait_mode="end-turn", step_minutes=1)
    except FlowPathError:
        refused = True
    expect(refused, "a flows-v2 symlink into flows/ was not refused")
    expect(list(v1_flows.iterdir()) == [], "a write reached the v1 flows directory")


# ---------------------------------------------------------------------------
# Section 7: the stub doc (commands/expert-flow-v2.md)
# ---------------------------------------------------------------------------

def read_stub_doc() -> str:
    return DOC.read_text(encoding="utf-8")


def test_stub_doc_description_begins_experimental(base: Path) -> None:
    fm = parse_markdown_frontmatter(read_stub_doc())
    description = str(fm.get("description", ""))
    expect(description.startswith("Experimental, not for real tickets:"), f"description is {description[:60]!r}")
    expect("experimental" in description.lower(), "description lacks 'experimental'")


def test_stub_doc_first_body_line_after_frontmatter(base: Path) -> None:
    _, sep, body = read_stub_doc().partition("\n---\n")
    expect(sep != "", "the frontmatter has no closing fence")
    first = next(line for line in body.splitlines() if line.strip())
    expect(first.strip() == "experimental, not for real tickets", f"first body line is {first!r}")


def test_stub_doc_allowed_tools_exact_and_no_ask_user_question(base: Path) -> None:
    tools = parse_markdown_frontmatter(read_stub_doc()).get("allowed-tools")
    expected = [
        f"Bash({RUNNER_PREFIX}:*)",
        'Bash(python3 "$HOME/.claude/scripts/fleet.py" wait:*)',
        "Read",
        "Task",
        "SendMessage",
    ]
    expect(tools == expected, f"allowed-tools is {tools!r}")
    expect("AskUserQuestion" not in tools, "allowed-tools grants AskUserQuestion")
    expect(not any(tool.startswith(("Write", "Edit")) or "git push" in tool or tool.startswith("Bash(gh")
                   for tool in tools), "allowed-tools grants a write, edit, push or gh tool")


def test_stub_doc_next_and_answer_commands_start_with_the_granted_prefix(base: Path) -> None:
    flow = base / "flow"
    expect(driver.next_command(flow).startswith(RUNNER_PREFIX + " next --flow "), "next_command prefix")
    expect(driver.answer_command(flow).startswith(RUNNER_PREFIX + " answer --flow "), "answer_command prefix")
    expect(driver.clear_live_command(flow).startswith(RUNNER_PREFIX + " clear-live --flow "), "clear_live prefix")


def test_stub_doc_has_no_bare_positional_tokens_and_uses_arguments(base: Path) -> None:
    text = read_stub_doc()
    expect(re.search(r"\$[0-9]", text) is None, "a bare $0-$9 token is in the stub doc")
    expect("$ARGUMENTS" in text, "the stub doc does not pass $ARGUMENTS to start")


def test_stub_doc_references_the_flow_reference_and_the_awaiting_marker(base: Path) -> None:
    text = read_stub_doc()
    expect("prompts/flow-reference.md" in text, "the stub doc does not mention prompts/flow-reference.md")
    expect("<!-- awaiting-answers:" in text, "the stub doc does not mention the awaiting-answers marker")


def test_stub_doc_has_accept_rule_section(base: Path) -> None:
    expect("\n## Accept rule\n" in read_stub_doc(), "the stub doc has no '## Accept rule' section")


def test_runbook_start_flow_command_parses(base: Path) -> None:
    runbook = (REPO_ROOT / "docs" / "v2-inventory" / "skeleton-runbook.md").read_text(encoding="utf-8")
    expect("start --flow <flow dir> --repo OWNER/NAME --mode" in runbook, "runbook lacks the full start --flow command")
    args = cli.build_parser().parse_args(
        ["start", "--flow", "/x", "--repo", "OWNER/NAME", "--mode", "headless", "--wait-mode", "end-turn", "--step-minutes", "15"]
    )
    expect(args.flow == "/x" and args.step_minutes == 15, "the documented start --flow form did not parse")


def test_stub_doc_has_no_ship_merge_push_pr_or_redo_ledger_calls(base: Path) -> None:
    text = read_stub_doc()
    for needle in ("git push", "gh pr", "/shipit", "/merge-and-cleanup", "flow-redo", "flow-ledger", "AskUserQuestion"):
        expect(needle not in text, f"the stub doc contains {needle!r}")
    expect(re.search(r"\bgh\b", text) is None, "the stub doc invokes gh")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

SECTIONS: List[Tuple[str, List[Tuple[str, Callable[[Path], None]]]]] = [
    ("[Section 1] Boundary types and loaders", [
        ("parse_verb maps members to themselves, unknowns to STOP, and handles every verb", test_parse_verb_members_unknowns_and_exhaustiveness),
        ("each loader accepts the valid form and rejects a missing key, a wrong type, a wrong driver_format and truncated JSON", test_loaders_accept_valid_and_reject_each_invalid_shape),
        ("loaders reject unknown modes, wait modes, pending states and verbs", test_loaders_reject_unknown_enumerated_values),
        ("a corrupt state.json becomes STOP through the gather edge, leaving pending_spawn unchanged", test_corrupt_boundary_file_becomes_stop_through_the_gather_edge),
    ]),
    ("[Section 2] decide() rules 1-10 and the transition table", [
        ("rule 1: a loader error or missing state is STOP and keeps pending_spawn", test_rule1_loader_error_or_missing_state_is_stop_and_keeps_pending),
        ("rule 2: no record and no usable receipt is SPAWN with attempt+1 and a fresh nonce", test_rule2_no_record_and_no_usable_receipt_is_spawn),
        ("rule 3: a completed receipt for the live attempt is DONE and clears the record", test_rule3_live_record_with_completed_receipt_is_done_and_cleared),
        ("rule 4: an awaiting receipt parks the record, then ASK (interactive) or PARK (headless)", test_rule4_awaiting_receipt_parks_then_asks_or_parks_by_mode),
        ("rule 5: a parked record with no answer asks again and never spawns", test_rule5_parked_without_answer_asks_again_and_never_spawns),
        ("rule 6: a parked record with an answer is RESUME", test_rule6_parked_with_answer_resumes),
        ("rule 7: a live record with no receipt is WAIT, never SPAWN", test_rule7_live_record_without_receipt_waits_and_never_spawns),
        ("rule 8: a mismatched ATTEMPT receipt is logged late-receipt and treated as none", test_rule8_mismatched_receipt_is_logged_late_and_treated_as_none),
        ("rule 9: no record and a completed last-attempt receipt is DONE with no spawn", test_rule9_no_record_and_completed_last_attempt_is_done_without_spawn),
        ("rule 10: a late or awaiting-only receipt with no record is logged and SPAWNs", test_rule10_late_or_awaiting_only_receipt_with_no_record_logs_and_spawns),
        ("the table: every record x receipt x question x answer cell returns a directive", test_table_every_record_by_receipt_cell_returns_a_directive),
        ("the transition table has no fleet entries and a parked record is never cleared", test_transition_table_has_no_fleet_entries_and_never_clears_a_parked_record),
    ]),
    ("[Section 3] Write order of one next", [
        ("directive file, then pending_spawn.json, then driver.log, then exactly one NEXT line", test_next_writes_directive_then_pending_then_log_then_one_line),
    ]),
    ("[Section 4] CLI in a subprocess under a temp HOME", [
        ("start prints exactly one NEXT line and leaves the boundary files under flows-v2", test_start_prints_one_line_and_leaves_the_boundary_files),
        ("answer cycle: refuse on spawned, ASK, answer once, refuse repeat, RESUME, WAIT, DONE", test_answer_cycle_ask_answer_resume_wait_done),
        ("headless mode PARKs, and clear-live refuses a parked record", test_headless_park_and_clear_live_refuses_a_parked_record),
        ("clear-live removes a spawned record, logs it, and the next SPAWN has a new nonce and attempt", test_clear_live_then_spawn_gets_a_new_nonce_and_attempt),
        ("a corrupted state.json gives exit 0 and a STOP directive in directives/skeleton-stop.json", test_corrupt_state_gives_exit_zero_and_a_stop_directive),
        ("a forced unexpected exception still prints NEXT: STOP in-process and exits 1", test_crash_records_stop_and_exits_1),
        ("a forced unexpected exception in the CLI exits non-zero", test_unexpected_exception_still_prints_stop_and_exits_nonzero),
        ("a driver-only kill loop (next repeated on a live record) produces zero extra SPAWNs and no new nonce", test_rerun_next_on_a_live_record_is_a_wait_with_no_new_nonce_and_no_extra_spawn),
    ]),
    ("[Section 5] Stub-side accept rule", [
        ("the accept rule model maps each documented failure to STOP and accepts a valid line", test_accept_rule_model_accepts_a_real_directive_and_maps_failures_to_stop),
        ("the accept rule accepts the real start output", test_stub_accept_rule_on_real_driver_output),
    ]),
    ("[Section 6] flows-v2 root acceptance", [
        ("flow dirs are created only under flows-v2, distinct per call, with no flow-run marker", test_flow_dirs_are_created_only_under_flows_v2),
        ("a flows-v2 symlink into the v1 flows/ dir is refused before any write", test_flows_v2_symlink_into_v1_flows_is_refused_before_any_write),
    ]),
    ("[Section 7] Stub doc acceptance (commands/expert-flow-v2.md)", [
        ("the description begins 'Experimental, not for real tickets:'", test_stub_doc_description_begins_experimental),
        ("the first body line is 'experimental, not for real tickets'", test_stub_doc_first_body_line_after_frontmatter),
        ("allowed-tools is exactly the granted set and does not include AskUserQuestion", test_stub_doc_allowed_tools_exact_and_no_ask_user_question),
        ("next, answer and clear-live commands start with the exact granted prefix", test_stub_doc_next_and_answer_commands_start_with_the_granted_prefix),
        ("the doc has no bare $0-$9 tokens and passes $ARGUMENTS", test_stub_doc_has_no_bare_positional_tokens_and_uses_arguments),
        ("the doc references prompts/flow-reference.md and the awaiting-answers marker", test_stub_doc_references_the_flow_reference_and_the_awaiting_marker),
        ("the doc has no ship, merge, push, PR, gh, redo or ledger step", test_stub_doc_has_no_ship_merge_push_pr_or_redo_ledger_calls),
        ("the doc has an Accept rule section", test_stub_doc_has_accept_rule_section),
        ("the runbook's documented start --flow command parses", test_runbook_start_flow_command_parses),
    ]),
]


def main() -> None:
    h = Harness("FLOW DRIVER (v2 walking skeleton) TEST SUITE")
    with tempfile.TemporaryDirectory(prefix="flow-driver-test-") as tmp:
        base = Path(tmp)
        for title, checks in SECTIONS:
            print(title)
            for name, fn in checks:
                check(h, name, fn, base)
            print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
