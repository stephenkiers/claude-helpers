"""
Pure transition function for the v2 skeleton driver (Step 3, S-Q1 A, v2 plan Q5 B, W1 A).

`decide` performs no I/O. The CLI gathers an Observation at the edge (validated state and pending record,
the parsed receipt, the question and answer presence, and a fresh nonce), calls decide, and then writes the
result. The rules are a closed list; each one has a test case.
"""

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple, Union

from .model import (
    AskDirective,
    ClearPending,
    Directive,
    DoneDirective,
    GENERAL_PURPOSE_AGENT,
    KeepPending,
    LogEntry,
    ParkDirective,
    PendingSpawn,
    PendingState,
    RUNNER_PREFIX,
    ResumeDirective,
    SKELETON_STEP,
    SetPending,
    SpawnDirective,
    StateRecord,
    StopDirective,
    WaitDirective,
    answer_path,
    prompt_path,
    question_path,
    receipt_path,
    transition_allowed,
)


@dataclass(frozen=True)
class ReceiptView:
    attempt: Optional[int]
    has_decision: bool
    awaiting: bool


_ATTEMPT_RE = re.compile(r"^ATTEMPT:\s*(\d+)\s*$", re.MULTILINE)
_DECISION_RE = re.compile(r"^Decision:", re.MULTILINE)
AWAITING_MARKER = "<!-- awaiting-answers:"


def parse_receipt(text: str) -> ReceiptView:
    """The ATTEMPT line, a Decision line and the awaiting-answers marker of a per-step receipt."""
    match = _ATTEMPT_RE.search(text)
    return ReceiptView(
        attempt=int(match.group(1)) if match else None,
        has_decision=_DECISION_RE.search(text) is not None,
        awaiting=AWAITING_MARKER in text,
    )


@dataclass(frozen=True)
class Observation:
    flow_dir: Path
    state: Optional[StateRecord]
    pending: Optional[PendingSpawn]
    receipt: Optional[ReceiptView]
    question_present: bool
    answer_present: bool
    fresh_nonce: str
    fault: Optional[str]


PendingChange = Union[KeepPending, SetPending, ClearPending]


@dataclass(frozen=True)
class Decision:
    directive: Directive
    pending: PendingChange
    attempt: int
    last_attempt: int
    events: Tuple[LogEntry, ...]


def _quoted(flow: Path) -> str:
    return shlex.quote(str(flow))


def next_command(flow: Path) -> str:
    return f"{RUNNER_PREFIX} next --flow {_quoted(flow)}"


def answer_command(flow: Path) -> str:
    return f"{RUNNER_PREFIX} answer --flow {_quoted(flow)} --text '<your answer>'"


def clear_live_command(flow: Path) -> str:
    return f"{RUNNER_PREFIX} clear-live --flow {_quoted(flow)} --reason 'human recovery'"


def _attempt_text(attempt: Optional[int]) -> str:
    return "none" if attempt is None else str(attempt)


def _stop(obs: Observation, reason: str) -> Decision:
    last = obs.state.last_attempt if obs.state is not None else 0
    return Decision(
        directive=StopDirective(next_command=next_command(obs.flow_dir), reason=reason),
        pending=KeepPending(),
        attempt=last,
        last_attempt=last,
        events=(LogEntry("stop", reason),),
    )


def _resulting_state(change: PendingChange, prev: Optional[PendingState]) -> Optional[PendingState]:
    if isinstance(change, KeepPending):
        return prev
    if isinstance(change, SetPending):
        return change.record.state
    if isinstance(change, ClearPending):
        return None
    raise AssertionError(f"unhandled pending change: {change!r}")


def _checked(obs: Observation, decision: Decision, prev: Optional[PendingState]) -> Decision:
    """Refuse (STOP) any decision whose pending transition is not in the allowed table."""
    new = _resulting_state(decision.pending, prev)
    if not transition_allowed(prev, new):
        return _stop(obs, f"transition {prev!r} -> {new!r} is not in the allowed table")
    return decision


def _ask_or_park(obs: Observation, state: StateRecord, attempt: int) -> Union[AskDirective, ParkDirective]:
    question = str(question_path(obs.flow_dir, attempt))
    answer = str(answer_path(obs.flow_dir, attempt))
    command = answer_command(obs.flow_dir)
    if state.mode == "interactive":
        return AskDirective(
            next_command=next_command(obs.flow_dir),
            question_file=question,
            answer_file=answer,
            answer_command=command,
        )
    return ParkDirective(
        next_command=next_command(obs.flow_dir),
        question_file=question,
        answer_file=answer,
        answer_command=command,
    )


def decide(obs: Observation) -> Decision:
    """The closed transition rules. Rule numbers match the plan's Step 3 list."""
    # Rule 1: any loader error or unreadable boundary file is STOP, and pending_spawn is left unchanged.
    state = obs.state
    if obs.fault is not None or state is None:
        return _stop(obs, obs.fault if obs.fault is not None else "state.json is unavailable")

    last = state.last_attempt
    pending = obs.pending
    receipt = obs.receipt
    ref = pending.attempt if pending is not None else last
    next_cmd = next_command(obs.flow_dir)
    events: List[LogEntry] = []

    # Rules 8 and 10: a receipt for another attempt, or an awaiting-only receipt with no record, is logged as
    # late and treated as no receipt.
    if receipt is not None and (receipt.attempt != ref or (pending is None and receipt.awaiting)):
        events.append(
            LogEntry(
                "late-receipt",
                f"receipt ATTEMPT {_attempt_text(receipt.attempt)} against current attempt {ref}",
            )
        )
    receipt_done = (
        receipt is not None and receipt.attempt == ref and receipt.has_decision and not receipt.awaiting
    )
    receipt_awaiting = receipt is not None and receipt.attempt == ref and receipt.awaiting

    if pending is None:
        # Rule 9: a completed receipt for the last attempt, with no record, is DONE with no spawn.
        if receipt_done:
            return _checked(
                obs,
                Decision(
                    directive=DoneDirective(next_command=next_cmd),
                    pending=KeepPending(),
                    attempt=last,
                    last_attempt=last,
                    events=tuple(events),
                ),
                None,
            )
        # Rule 2: no record and no usable receipt is SPAWN, with attempt = last + 1 and a fresh nonce.
        new_attempt = last + 1
        nonce = obs.fresh_nonce
        record = PendingSpawn(step=SKELETON_STEP, attempt=new_attempt, nonce=nonce, state="spawned")
        spawn = SpawnDirective(
            next_command=next_cmd,
            agent_type=GENERAL_PURPOSE_AGENT,
            prompt_file=str(prompt_path(obs.flow_dir, new_attempt, nonce)),
            step=SKELETON_STEP,
            attempt=new_attempt,
            nonce=nonce,
        )
        return _checked(
            obs,
            Decision(
                directive=spawn,
                pending=SetPending(record),
                attempt=new_attempt,
                last_attempt=new_attempt,
                events=tuple(events),
            ),
            None,
        )

    prev = pending.state

    # Rules 5 and 6: a parked record is answered or waits. decide never clears it.
    if prev == "parked-awaiting-answer":
        if obs.answer_present:
            # Rule 6: the answer file exists, so RESUME the step (v2 plan W1 A).
            events.append(LogEntry("resumed", f"answer present for attempt {pending.attempt}"))
            resumed = PendingSpawn(
                step=pending.step, attempt=pending.attempt, nonce=pending.nonce, state="resumed"
            )
            resume = ResumeDirective(
                next_command=next_cmd,
                step=pending.step,
                attempt=pending.attempt,
                answer_file=str(answer_path(obs.flow_dir, pending.attempt)),
            )
            return _checked(
                obs,
                Decision(
                    directive=resume,
                    pending=SetPending(resumed),
                    attempt=pending.attempt,
                    last_attempt=last,
                    events=tuple(events),
                ),
                prev,
            )
        # Rule 5: no answer yet, so ASK or PARK again. No spawn, so a repeat is harmless.
        return _checked(
            obs,
            Decision(
                directive=_ask_or_park(obs, state, pending.attempt),
                pending=KeepPending(),
                attempt=pending.attempt,
                last_attempt=last,
                events=tuple(events),
            ),
            prev,
        )

    # The record is spawned or resumed.
    # Rule 3: a completed receipt for the live attempt removes the record (receipt-landed) and is DONE.
    if receipt_done:
        events.append(LogEntry("receipt-landed", f"attempt {pending.attempt} completed"))
        return _checked(
            obs,
            Decision(
                directive=DoneDirective(next_command=next_cmd),
                pending=ClearPending(),
                attempt=pending.attempt,
                last_attempt=last,
                events=tuple(events),
            ),
            prev,
        )
    # Rule 4: an awaiting receipt with its question and no answer parks the record, then ASK or PARK.
    if receipt_awaiting and obs.question_present and not obs.answer_present:
        parked = PendingSpawn(
            step=pending.step, attempt=pending.attempt, nonce=pending.nonce, state="parked-awaiting-answer"
        )
        events.append(LogEntry("parked", f"attempt {pending.attempt} awaiting an answer"))
        return _checked(
            obs,
            Decision(
                directive=_ask_or_park(obs, state, pending.attempt),
                pending=SetPending(parked),
                attempt=pending.attempt,
                last_attempt=last,
                events=tuple(events),
            ),
            prev,
        )
    # Rule 7: a live record with no matching receipt (or no question) is WAIT, never SPAWN (S-Q1 A).
    wait = WaitDirective(
        next_command=next_cmd,
        wait_mode=state.wait_mode,
        receipt_file=str(receipt_path(obs.flow_dir)),
        clear_live_command=clear_live_command(obs.flow_dir),
    )
    return _checked(
        obs,
        Decision(
            directive=wait,
            pending=KeepPending(),
            attempt=pending.attempt,
            last_attempt=last,
            events=tuple(events),
        ),
        prev,
    )
