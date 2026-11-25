#!/usr/bin/env python3
"""Tests for scripts/flow3/driver.py: every row of the transition table, caps, errors, purity."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_TMP = tempfile.mkdtemp(prefix="flow3-test-")
os.environ["FLOW3_HOME"] = _TMP

from _test_harness import Harness  # noqa: E402

import copy  # noqa: E402
from dataclasses import replace  # noqa: E402

from scripts.flow3 import driver  # noqa: E402
from scripts.flow3.config import MAX_REVIEW_ROUNDS  # noqa: E402
from scripts.flow3.types import Event, FlowState, make_stop  # noqa: E402

h = Harness("flow3 driver")
RC = "python3 -m scripts.flow3 resume r 1"


def S(stage: str = "worktree", status: str = "running", **kw: object) -> FlowState:
    return replace(FlowState(), stage=stage, status=status, **kw)  # type: ignore[arg-type]


def check(name: str, state: FlowState, event: Event, stage: str, status: str, action: str, **extra: object) -> None:
    before = copy.deepcopy(state)
    try:
        new, act = driver.step(state, event, RC)
    except Exception as exc:  # noqa: BLE001
        h.test_result(name, False, repr(exc))
        return
    ok = new.stage == stage and new.status == status and act.kind == action
    if action == "run_stage":
        ok = ok and act.stage == stage
    for k, v in extra.items():
        ok = ok and getattr(new, k) == v
    ok = ok and state == before
    h.test_result(name, ok, f"got {new} {act}")


def raises(name: str, state: FlowState, event: Event) -> None:
    try:
        driver.step(state, event, RC)
        h.test_result(name, False, "no error")
    except ValueError:
        h.test_result(name, True)


done = lambda s, **info: Event("stage_done", stage=s, info=dict(info))  # noqa: E731

check("start", S(), Event("start"), "worktree", "running", "run_stage")
for cur, nxt in (("worktree", "plan"), ("plan", "spec_tests"), ("spec_tests", "implement"), ("implement", "review"), ("fix", "review")):
    check(f"{cur} done -> {nxt}", S(cur), done(cur), nxt, "running", "run_stage")
check("review done, fix needed -> fix", S("review"), done("review", fix_needed=True), "fix", "running", "run_stage", review_round=1)
check("review done, clean -> ship", S("review"), done("review", fix_needed=False), "ship", "running", "run_stage", review_round=1)
check("review done, no info -> ship", S("review"), done("review"), "ship", "running", "run_stage")
check("review 2 -> fix", S("review", review_round=1), done("review", fix_needed=True), "fix", "running", "run_stage", review_round=2)
check("review cap: third review ships", S("review", review_round=MAX_REVIEW_ROUNDS - 1), done("review", fix_needed=True),
      "ship", "running", "run_stage", review_round=MAX_REVIEW_ROUNDS)
check("sendbacks>0 review ships", S("review", sendbacks=1), done("review", fix_needed=True), "ship", "running", "run_stage")
check("ship done -> merge_gate", S("ship"), done("ship"), "merge_gate", "merge_gate", "merge_gate")
gate = S("merge_gate", "merge_gate", review_round=2)
check("gate merge", gate, Event("merge_decision", decision="merge"), "merge", "running", "run_stage")
check("gate send_back", gate, Event("merge_decision", decision="send_back"), "fix", "running", "run_stage", sendbacks=1, review_round=2)
check("gate send_back again", replace(gate, sendbacks=1), Event("merge_decision", decision="send_back"), "fix", "running", "run_stage", sendbacks=2)
new, act = driver.step(gate, Event("merge_decision", decision="stop", notes="not now"), RC)
h.test_result("gate stop with notes", new.status == "stopped" and act.kind == "stop" and act.stop is not None
              and act.stop.cls == "human" and act.stop.reason == "not now" and act.stop.resume_command == RC and new.stop == act.stop)
new, act = driver.step(gate, Event("merge_decision", decision="stop"), RC)
h.test_result("gate stop default reason", act.stop is not None and act.stop.reason.strip() != "")
check("merge done -> done", S("merge"), done("merge"), "done", "done", "finish")
check("needs_human parks", S("plan"), Event("stage_needs_human", stage="plan"), "plan", "waiting_human", "park")
check("answered reruns", S("plan", "waiting_human"), Event("answered"), "plan", "running", "run_stage")
new, act = driver.step(S("implement"), Event("stage_failed", stage="implement", stop_cls="check", reason="boom",
                                              evidence="e.txt", tried=("x", "y")), RC)
h.test_result("failed stops with details", new.status == "stopped" and act.kind == "stop" and act.stop is not None
              and act.stop.cls == "check" and act.stop.reason == "boom" and act.stop.stage == "implement"
              and act.stop.evidence == "e.txt" and act.stop.tried == ("x", "y") and act.stop.resume_command == RC
              and new.stop == act.stop)
stopped = replace(S("implement", "stopped"), stop=make_stop("check", "boom", "implement", RC))
check("resume stopped reruns, clears stop", stopped, Event("resume"), "implement", "running", "run_stage", stop=None)
gstop = replace(S("merge_gate", "stopped"), stop=make_stop("human", "no", "merge_gate", RC))
check("resume stopped at gate returns to gate", gstop, Event("resume"), "merge_gate", "merge_gate", "merge_gate", stop=None)
check("resume waiting_human parks", S("plan", "waiting_human"), Event("resume"), "plan", "waiting_human", "park")
check("resume at gate", gate, Event("resume"), "merge_gate", "merge_gate", "merge_gate")
for ev in (Event("resume"), done("done"), Event("start"), Event("answered")):
    check(f"done absorbs {ev.kind}", S("done", "done"), ev, "done", "done", "finish")

raises("stage_done for wrong stage", S("plan"), done("review"))
raises("needs_human for wrong stage", S("plan"), Event("stage_needs_human", stage="fix"))
raises("failed for wrong stage", S("plan"), Event("stage_failed", stage="fix", stop_cls="check", reason="x"))
raises("answered while running", S("plan"), Event("answered"))
raises("merge_decision while running", S("plan"), Event("merge_decision", decision="merge"))
raises("stage_done while waiting", S("plan", "waiting_human"), done("plan"))
raises("stage_done while stopped", S("plan", "stopped"), done("plan"))
check("resume while running reruns the stage", S("plan"), Event("resume"), "plan", "running", "run_stage")
raises("start mid-flow", S("plan"), Event("start"))
raises("unknown event", S("plan"), Event("bogus"))
raises("unknown merge decision", gate, Event("merge_decision", decision="maybe"))
raises("failed with unknown class", S("plan"), Event("stage_failed", stage="plan", stop_cls="nope", reason="x"))
raises("failed with empty reason", S("plan"), Event("stage_failed", stage="plan", stop_cls="check", reason="  "))

# Walk the loop: three reviews with fixes needed still ends at ship.
st = S("review")
seq = []
for _ in range(6):
    st, act = driver.step(st, done("review", fix_needed=True), RC)
    seq.append(act.stage)
    if act.stage == "ship":
        break
    st, act = driver.step(st, done("fix"), RC)
    seq.append(act.stage)
h.test_result("review/fix order with cap", seq == ["fix", "review", "fix", "review", "ship"], str(seq))

h.summarize_and_exit()
