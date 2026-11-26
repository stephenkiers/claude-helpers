"""
The flow state machine: one pure function (state, event) -> (state, action).

No I/O, no clock, no mutation of its inputs. `resume_command` is only copied into a StopInfo.
"""

import dataclasses
from typing import Optional, Tuple

from scripts.flow3.config import MAX_REVIEW_ROUNDS
from scripts.flow3.types import Action, Event, FlowState, make_stop

# Stages whose completion simply moves to the next stage.
_NEXT = {
    "worktree": "plan",
    "plan": "spec_tests",
    "spec_tests": "implement",
    "implement": "review",
    "fix": "review",
}


def _bad(state: FlowState, event: Event) -> ValueError:
    return ValueError(
        f"invalid event {event.kind!r} in stage {state.stage!r} with status {state.status!r}")


def _run(state: FlowState, stage: str, review_round: Optional[int] = None,
         sendbacks: Optional[int] = None) -> Tuple[FlowState, Action]:
    """Move to `stage` as running, with any stop cleared; round and send-back counts only when given."""
    new = FlowState(
        stage=stage, status="running",
        review_round=state.review_round if review_round is None else review_round,
        sendbacks=state.sendbacks if sendbacks is None else sendbacks,
        stop=None,
    )
    return new, Action("run_stage", stage=stage)


def _check_stage(state: FlowState, event: Event) -> None:
    if event.stage != state.stage:
        raise ValueError(
            f"event {event.kind!r} is for stage {event.stage!r} but the flow is at {state.stage!r}")


def step(state: FlowState, event: Event, resume_command: str) -> Tuple[FlowState, Action]:
    """Apply one event. Raises ValueError for a mismatched stage or any pair not in the table."""
    kind = event.kind

    if state.status == "done":
        return state, Action("finish")

    if kind == "start":
        if (state.stage, state.status, state.review_round, state.sendbacks) != ("worktree", "running", 0, 0):
            raise _bad(state, event)
        return _run(state, "worktree")

    if kind == "stage_done":
        _check_stage(state, event)
        if state.status != "running":
            raise _bad(state, event)
        stage = state.stage
        if stage == "fix" and state.sendbacks == 0 and state.review_round >= MAX_REVIEW_ROUNDS:
            # The one fix run after the last review round (fixes the human asked for) is not reviewed again.
            return _run(state, "ship")
        if stage in _NEXT:
            return _run(state, _NEXT[stage])
        if stage == "review":
            rounds = state.review_round + 1
            fix_needed = bool(event.info.get("fix_needed"))
            if fix_needed and state.sendbacks == 0 and rounds < MAX_REVIEW_ROUNDS:
                return _run(state, "fix", review_round=rounds)
            if state.sendbacks == 0 and bool(event.info.get("human_fix")):
                # Past the cap, only the fixes the human asked for are run, once.
                return _run(state, "fix", review_round=rounds)
            return _run(state, "ship", review_round=rounds)
        if stage == "ship":
            new = dataclasses.replace(state, stage="merge_gate", status="merge_gate")
            return new, Action("merge_gate")
        if stage == "merge":
            new = dataclasses.replace(state, stage="done", status="done")
            return new, Action("finish")
        raise _bad(state, event)

    if kind == "stage_needs_human":
        _check_stage(state, event)
        if state.status != "running":
            raise _bad(state, event)
        return dataclasses.replace(state, status="waiting_human"), Action("park")

    if kind == "stage_failed":
        _check_stage(state, event)
        if state.status != "running":
            raise _bad(state, event)
        stop = make_stop(event.stop_cls, event.reason, state.stage, resume_command,
                         evidence=event.evidence, tried=list(event.tried))
        return dataclasses.replace(state, status="stopped", stop=stop), Action("stop", stop=stop)

    if kind == "answered":
        if state.status != "waiting_human":
            raise _bad(state, event)
        return _run(state, state.stage)

    if kind == "merge_decision":
        if state.status != "merge_gate":
            raise _bad(state, event)
        if event.decision == "merge":
            return _run(state, "merge")
        if event.decision == "send_back":
            return _run(state, "fix", sendbacks=state.sendbacks + 1)
        if event.decision == "stop":
            reason = event.notes.strip() or "stopped by the human at the merge gate"
            stop = make_stop("human", reason, state.stage, resume_command)
            return dataclasses.replace(state, status="stopped", stop=stop), Action("stop", stop=stop)
        raise ValueError(f"unknown merge decision: {event.decision!r}")

    if kind == "resume":
        if state.status == "stopped":
            if state.stage == "merge_gate":
                new = dataclasses.replace(state, status="merge_gate", stop=None)
                return new, Action("merge_gate")
            return _run(state, state.stage)
        if state.status == "waiting_human":
            return state, Action("park")
        if state.status == "merge_gate":
            return state, Action("merge_gate")
        if state.status == "running":
            # The process that was running this stage died. Stages are re-enterable: run it again.
            # (engine.run_flow holds a per-flow lock, so this never doubles a live run.)
            return _run(state, state.stage)
        raise _bad(state, event)

    raise _bad(state, event)
