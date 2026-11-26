"""
The engine: feeds events to the pure driver, saves state, and performs the actions it returns.

It loops while the action is `run_stage`, turning each stage's outcome into the next event. Any
other action ends the loop and is returned to the caller.
"""

import traceback
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from scripts.flow3 import driver, record
from scripts.flow3.stages import StageFn
from scripts.flow3.state import FlowDir
from scripts.flow3.types import Action, Event, FlowStop, StageResult
from scripts.workflow.fsutil import LockHeld, nonblocking_flock

__all__ = ["StageFn", "run_flow", "resume_command"]


def resume_command(flow: FlowDir) -> str:
    """The exact command that resumes this flow."""
    return f"python3 -m scripts.flow3 resume {flow.get('repo')} {flow.get('issue')}"


def _run_one(flow: FlowDir, stage: str, stages: Mapping[str, StageFn],
             build_ctx: Callable[[FlowDir], Any]) -> Event:
    """Run one stage function and convert its outcome to the next event."""
    fn = stages.get(stage)
    if fn is None:
        return Event("stage_failed", stage=stage, stop_cls="runner",
                     reason=f"no function is registered for stage {stage!r}")
    try:
        result = fn(build_ctx(flow))
        if not isinstance(result, StageResult):
            raise TypeError(f"stage {stage!r} returned {type(result).__name__}, not StageResult")
    except FlowStop as stop:
        return Event("stage_failed", stage=stage, stop_cls=stop.cls, reason=stop.reason,
                     evidence=stop.evidence, tried=tuple(stop.tried))
    except Exception as exc:  # a stage bug must become a recorded stop, never a lost traceback
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        errors = flow.path / "errors"
        errors.mkdir(exist_ok=True)
        path = errors / f"{stage}-{stamp}.txt"
        path.write_text(traceback.format_exc(), encoding="utf-8")
        return Event("stage_failed", stage=stage, stop_cls="runner",
                     reason=f"{type(exc).__name__}: {exc}", evidence=str(path))
    if result.kind == "needs_human":
        return Event("stage_needs_human", stage=stage)
    return Event("stage_done", stage=stage, info=dict(result.info))


def run_flow(flow: FlowDir, event: Event, stages: Mapping[str, StageFn],
             build_ctx: Callable[[FlowDir], Any]) -> Action:
    """Apply `event`, then run stages until an action that needs the process to exit. Returns it.

    One process drives a flow at a time: a second caller gets `FlowBusy`.
    """
    try:
        with nonblocking_flock(flow.path / ".run.lock"):
            return _run_flow(flow, event, stages, build_ctx)
    except LockHeld:
        raise FlowBusy("another process is already running %s" % flow.name)


class FlowBusy(Exception):
    """The flow is being driven by another live process."""


def _run_flow(flow: FlowDir, event: Event, stages: Mapping[str, StageFn],
              build_ctx: Callable[[FlowDir], Any]) -> Action:
    while True:
        before = flow.flow_state()
        state, action = driver.step(before, event, resume_command(flow))
        if event.kind == "resume" and before.status == "stopped":
            record.clear_stop(flow)
        flow.set_flow_state(state)
        flow.event("transition", event=event.kind, stage=event.stage, to_stage=state.stage,
                   status=state.status)

        if action.kind == "run_stage":
            flow.event("stage_start", stage=action.stage)
            event = _run_one(flow, action.stage, stages, build_ctx)
            outcome = {"stage_done": "done", "stage_needs_human": "needs_human",
                       "stage_failed": "failed"}[event.kind]
            flow.event("stage_end", stage=action.stage, outcome=outcome)
            continue

        if action.kind == "stop" and action.stop is not None:
            record.write_stop(flow, action.stop)
            record.journal(flow, f"Stopped ({action.stop.cls}) at {action.stop.stage}: {action.stop.reason}")
            flow.event("stop", cls=action.stop.cls, reason=action.stop.reason, stage=action.stop.stage,
                       evidence=action.stop.evidence)
        else:
            record.journal(flow, {
                "park": f"Parked at {state.stage}: waiting for the human's answers.",
                "merge_gate": "At the merge gate: waiting for the human's decision.",
                "finish": "Flow finished.",
            }.get(action.kind, action.kind))
            flow.event(action.kind, stage=state.stage)
        return action
