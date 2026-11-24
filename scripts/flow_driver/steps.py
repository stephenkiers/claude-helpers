"""
Typed stop codes, the closed Event and Directive unions, and the pure transition for the v2 driver
(Layer 2 Step 2 and Layer 3; ADR-0026; sessions D8 and W1).

transition() performs no I/O and takes no lock. loop.py gathers the facts at the edge, under the flow lock, calls
transition, commits the returned state, then writes the directive file and prints one NEXT line. Every dispatch over a
closed union ends in assert_never. The directive set is SPAWN, RESUME, WAIT, ASK, PARK, STOP, DONE and MERGE (Layer 3
adds MERGE).

Python 3.8 floor: typing.Literal, get_args, Optional, Union and Tuple only.
"""

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Dict, Literal, Optional, Tuple, Union, get_args

from . import driver, receipts, relay
from .model import AFTER_EVENTS, GENERAL_PURPOSE_AGENT, AfterEvent, assert_never, parse_after
from .state import ALLOWED_TRANSITIONS, LiveRecord, ParkedQuestion, PendingSpawn, Step, V2State

# The happy path the driver spawns step by step. Ship is the last SPAWN; what follows it (the merge gate and MERGE) is
# decided by gates.py in the loop and reaches transition() as a GateFact.
HAPPY_PATH: Tuple[Step, ...] = ("worktree", "plan", "track", "implement", "review", "fix", "verify", "ship")

StopCode = Literal[
    "merge-not-built",
    "auto-merge-refused",
    "ship-head-differs",
    "ship-pr-closed",
    "expected-head-mismatch",
    "gh-error",
    "resume-failed",
    "unknown-event",
    "retry-cap",
    "redo-cap",
    "malformed-cap",
    "rebase-cap",
    "heal-cap",
    "uncarried-direction",
    "north-star-preflight",
    "hook-preflight",
    "step-failed",
    "take-over",
    "ledger-error",
    "wait-timeout",
    "merge-not-merged",
    "merge-head-mismatch",
    "merge-held",
    "gate-paused",
    "gate-aborted",
    "audit-rejected",
    "audit-error",
    "assess-error",
    "plan-test-paths",
    "test-path-in-units",
    "test-only-plan",  # legacy: never emitted since #526; kept so archived receipts classify
    "existing-test-edit",
    "planned-change-breaks-test",
    "zero-unit-plan-incomplete",
]
STOP_CODES: Tuple[StopCode, ...] = get_args(StopCode)
_STOP_BY_NAME: Dict[str, StopCode] = {code: code for code in STOP_CODES}


def stop_reason(code: StopCode, detail: str) -> str:
    """The one builder of a stop reason: `<code>: <detail>` (session W1). state.json keeps it as a plain string."""
    return f"{code}: {detail}"


def parse_stop_reason(text: str) -> Tuple[Optional[StopCode], str]:
    """The one parser of a stop reason. An unknown or unprefixed text is (None, text), never an error."""
    code, sep, detail = text.partition(": ")
    if sep and code in _STOP_BY_NAME:
        return _STOP_BY_NAME[code], detail
    return None, text


# Events. One frozen type per stub label, plus the no-label re-entry and UnknownEvent (which maps to STOP).
@dataclass(frozen=True)
class Start:
    """The first `next` of a flow, sent as the `start` label."""


@dataclass(frozen=True)
class Reentry:
    """A `next` with no label: the stub re-entering the loop."""


@dataclass(frozen=True)
class SpawnLaunched:
    """The stub launched the spawned step agent."""


@dataclass(frozen=True)
class ResumeSent:
    """The stub sent the RESUME to the parked step agent."""


@dataclass(frozen=True)
class ResumeFailed:
    """The RESUME did not reach the step agent. Every failed RESUME is a STOP (session D3)."""


@dataclass(frozen=True)
class WaitSettled:
    """The stub's wait ended normally."""


@dataclass(frozen=True)
class WaitTimeout:
    """The stub's wait ran out. With a live record and no landed receipt this is a STOP (session Q4)."""


@dataclass(frozen=True)
class Notification:
    """The step agent's subagent is gone (session D4: this satisfies the rerun gate at once)."""


@dataclass(frozen=True)
class HumanContinue:
    """The human told the stub to continue."""


@dataclass(frozen=True)
class UnknownEvent:
    """A label that is not one of the stub's labels. Maps to STOP unknown-event."""

    label: str


Event = Union[
    Start,
    Reentry,
    SpawnLaunched,
    ResumeSent,
    ResumeFailed,
    WaitSettled,
    WaitTimeout,
    Notification,
    HumanContinue,
    UnknownEvent,
]

_EVENT_BY_LABEL: Dict[AfterEvent, Event] = {
    "start": Start(),
    "spawn-launched": SpawnLaunched(),
    "resume-sent": ResumeSent(),
    "resume-failed": ResumeFailed(),
    "wait-settled": WaitSettled(),
    "wait-timeout": WaitTimeout(),
    "notification": Notification(),
    "human-continue": HumanContinue(),
}
# The map is keyed by AfterEvent; a label added to or removed from the model without a matching entry fails at import.
if set(_EVENT_BY_LABEL) != set(AFTER_EVENTS):
    raise RuntimeError("_EVENT_BY_LABEL keys do not match model.AFTER_EVENTS")


def event_from_label(label: Optional[str]) -> Event:
    """Map the logged `--after` label to an Event. No label is Reentry; an unknown label is UnknownEvent."""
    parsed = parse_after(label)
    if parsed == "none":
        return Reentry()
    if parsed == "unrecognized":
        return UnknownEvent(label=label if label is not None else "")
    return _EVENT_BY_LABEL[parsed]


# Directives. The closed union; the verb is a class constant, never a field.
AgentType = Literal["general-purpose"]
_SPAWN_AGENT: AgentType = "general-purpose"
# The spawn agent type is a Literal here so the directive is typed; it must equal the model constant.
if _SPAWN_AGENT != GENERAL_PURPOSE_AGENT:
    raise RuntimeError("steps._SPAWN_AGENT does not match model.GENERAL_PURPOSE_AGENT")


@dataclass(frozen=True)
class SpawnDirective:
    verb: ClassVar[Literal["SPAWN"]] = "SPAWN"
    next_command: str
    agent_type: AgentType
    prompt_file: str
    step: Step
    attempt: int
    nonce: str


@dataclass(frozen=True)
class ResumeDirective:
    verb: ClassVar[Literal["RESUME"]] = "RESUME"
    next_command: str
    step: Step
    attempt: int
    answer_file: str


@dataclass(frozen=True)
class WaitDirective:
    verb: ClassVar[Literal["WAIT"]] = "WAIT"
    next_command: str
    receipt_file: str
    clear_live_command: str
    nonce: str  # the live record's nonce, so a re-rendered WAIT file names the same attempt (Q1 = A)
    require_change: bool = False


@dataclass(frozen=True)
class AskDirective:
    verb: ClassVar[Literal["ASK"]] = "ASK"
    next_command: str
    question_file: str
    answer_file: str
    answer_command: str
    options: Tuple[str, ...] = ()  # the closed option labels of a driver question (gate, merge-gate); () for a step question


@dataclass(frozen=True)
class ParkDirective:
    verb: ClassVar[Literal["PARK"]] = "PARK"
    next_command: str
    question_file: str
    answer_file: str
    answer_command: str
    options: Tuple[str, ...] = ()


@dataclass(frozen=True)
class StopDirective:
    verb: ClassVar[Literal["STOP"]] = "STOP"
    next_command: str
    reason: str


@dataclass(frozen=True)
class DoneDirective:
    verb: ClassVar[Literal["DONE"]] = "DONE"
    next_command: str


@dataclass(frozen=True)
class MergeDirective:
    verb: ClassVar[Literal["MERGE"]] = "MERGE"
    next_command: str
    agent_type: AgentType
    prompt_file: str
    step: Step
    attempt: int
    nonce: str


Directive = Union[SpawnDirective, ResumeDirective, WaitDirective, AskDirective, ParkDirective, StopDirective, DoneDirective, MergeDirective]


def directive_to_dict(directive: Directive) -> Dict[str, object]:
    """The JSON object written to the directive file. The verb is included; the class constant is not a field."""
    data: Dict[str, object] = dataclasses.asdict(directive)
    data["verb"] = directive.verb
    return data


# Path helpers for everything a directive names. Names carry the nonce where the plan says so.
def step_prompt_path(flow: Path, step: str, attempt: int, nonce: str) -> Path:
    return flow / "prompts" / f"{step}-a{attempt}-{nonce}.md"


def directive_file_path(flow: Path, step: str, attempt: int, nonce: str, verb: str) -> Path:
    """The directive file for one emitted verb. The verb is in the name, so a SPAWN and a WAIT of one attempt never share it."""
    return flow / "directives" / f"{verb.lower()}-{step}-a{attempt}-{nonce}.json"


# Gate outcomes, as facts. gates.py decides (and is imported only by loop.py, so transition stays pure); the loop
# performs the effects the outcome needs (park the question, record the merge gate, redo), then hands the result here.
@dataclass(frozen=True)
class GateAdvance:
    """No gate stands in the way (or the human chose Continue): open the next step."""


@dataclass(frozen=True)
class GateAsk:
    """A gate question is parked (state.parked is set and committed): emit its ASK, or its PARK when headless."""

    options: Tuple[str, ...]


@dataclass(frozen=True)
class GateStop:
    """Stop with this code. Pause and Hold leave the flow `paused`; every other gate stop is `failed`."""

    code: StopCode
    detail: str
    status: Literal["failed", "paused"] = "failed"


@dataclass(frozen=True)
class GateMerge:
    """Emit this MERGE directive (built only by gates.py) and record its pending spawn."""

    directive: MergeDirective


@dataclass(frozen=True)
class GateDone:
    """The PR is merged: the flow is done."""


GateFact = Union[GateAdvance, GateAsk, GateStop, GateMerge, GateDone]


# Facts gathered at the edge. The receipt fact is the outcome of loop.py's receipt settling, under the lock.
ReceiptFact = Literal["none", "gate-hold", "failed", "rerun", "malformed-cap"]
QuestionFact = Literal["none", "awaiting", "answered"]


@dataclass(frozen=True)
class Facts:
    flow: Path
    nonce: str  # fresh for this call; used only if the call opens a new spawn
    # none: nothing to act on (absent, in progress, stale); gate-hold: a malformed receipt is held by the D4 gate;
    # failed: the pending attempt landed a FAILED receipt; rerun: malformed rerun already recorded, spawn next attempt;
    # malformed-cap: the D5 cap refuses another rerun
    receipt: ReceiptFact
    question: QuestionFact
    headless: bool  # PARK instead of ASK
    gate: Optional[GateFact] = None  # the gate outcome for this call, or None when no gate stage applies
    # a FAILED receipt whose Reason names one of the six test-path codes: (code, detail); None otherwise
    failed_stop: Optional[Tuple[StopCode, str]] = None

    def __post_init__(self) -> None:
        """A failed_stop exists only for a FAILED receipt. Any other pairing is a wiring defect in loop.py, never a STOP."""
        if self.failed_stop is not None and self.receipt != "failed":
            raise RuntimeError(f"Facts.failed_stop is set but receipt is {self.receipt!r}, not 'failed'")


def _live(pending: Optional[PendingSpawn]) -> Optional[LiveRecord]:
    """D1: live is exactly the projection of a spawned or resumed pending record."""
    if pending is None or pending.state not in ("spawned", "resumed"):
        return None
    return LiveRecord(step=pending.step, attempt=pending.attempt, nonce=pending.nonce)


def _with_pending(state: V2State, pending: Optional[PendingSpawn]) -> V2State:
    return dataclasses.replace(state, pending_spawn=pending, live=_live(pending))


def _allowed(prev: Optional[PendingSpawn], new: Optional[PendingSpawn]) -> None:
    """Refuse a pending change that is not in the allowed table. A defect here is a RuntimeError, never a STOP."""
    prev_state = None if prev is None else prev.state
    new_state = None if new is None else new.state
    if (prev_state, new_state) not in ALLOWED_TRANSITIONS:
        raise RuntimeError(f"illegal pending_spawn transition {prev_state!r} -> {new_state!r}")


def _spawn(state: V2State, facts: Facts, step: Step) -> Tuple[V2State, Directive]:
    attempt = state.attempts.get(step, 0) + 1
    pending = PendingSpawn(step=step, attempt=attempt, nonce=facts.nonce, state="spawned", receipt_seen=None)
    _allowed(state.pending_spawn, pending)
    attempts: Dict[Step, int] = dict(state.attempts)
    attempts[step] = attempt
    new_state = dataclasses.replace(_with_pending(state, pending), step=step, attempts=attempts)
    spawn = SpawnDirective(
        next_command=driver.next_command(facts.flow),
        agent_type=_SPAWN_AGENT,
        prompt_file=str(step_prompt_path(facts.flow, step, attempt, facts.nonce)),
        step=step,
        attempt=attempt,
        nonce=facts.nonce,
    )
    return new_state, spawn


def _wait(state: V2State, facts: Facts, pending: PendingSpawn) -> Tuple[V2State, Directive]:
    wait = WaitDirective(
        next_command=driver.next_command(facts.flow),
        receipt_file=str(receipts.receipt_path(facts.flow, pending.step)),
        clear_live_command=driver.clear_live_command(facts.flow),
        nonce=pending.nonce,
        require_change=(pending.state == "resumed"),
    )
    return state, wait


def _ask_or_park(
    facts: Facts, state: V2State, options: Tuple[str, ...] = ()
) -> Union[AskDirective, ParkDirective]:
    """Create an ASK or PARK directive using relay to map the parked question to its paths."""
    # For Layer 2 step questions, if state.parked is not set, construct a temporary one
    pending = state.pending_spawn
    if state.parked is None:
        if pending is None or pending.state != "parked-awaiting-answer":
            raise RuntimeError("_ask_or_park: no pending or state.parked")
        # Layer 2: one question per attempt, so nn == attempt
        parked = ParkedQuestion(step=pending.step, attempt=pending.attempt, nn=pending.attempt, kind="step")
    else:
        parked = state.parked

    question = str(relay.question_path(facts.flow, parked))
    answer = str(relay.answer_path(facts.flow, parked))
    command = driver.answer_command(facts.flow)
    if facts.headless:
        return ParkDirective(
            next_command=driver.next_command(facts.flow),
            question_file=question,
            answer_file=answer,
            answer_command=command,
            options=options,
        )
    return AskDirective(
        next_command=driver.next_command(facts.flow),
        question_file=question,
        answer_file=answer,
        answer_command=command,
        options=options,
    )


def _stop(state: V2State, facts: Facts, code: StopCode, detail: str) -> Tuple[V2State, Directive]:
    reason = stop_reason(code, detail)
    reasons: Dict[Step, str] = dict(state.stop_reason)
    reasons[state.step] = reason
    failed = dataclasses.replace(state, status="failed", stop_reason=reasons)
    return failed, StopDirective(next_command=driver.next_command(facts.flow), reason=reason)


def _advance(state: V2State, facts: Facts, landed: Step) -> Tuple[V2State, Directive]:
    """The landed step's receipt is recorded; open the next step. Ship is never advanced past here: the merge gate decides."""
    if landed not in HAPPY_PATH:
        return _stop(state, facts, "step-failed", f"step {landed} landed outside the happy path")
    if landed == "ship":
        # The loop always supplies a GateFact for a landed ship (the merge gate); reaching here is a wiring defect.
        raise RuntimeError("a landed ship reached the transition without a merge-gate fact")
    return _spawn(state, facts, HAPPY_PATH[HAPPY_PATH.index(landed) + 1])


def _apply_gate(state: V2State, facts: Facts, gate: GateFact) -> Tuple[V2State, Directive]:
    """Apply a gate outcome the loop decided. Parked state was committed by the loop before this call."""
    pending = state.pending_spawn
    if isinstance(gate, GateAdvance):
        cleared = dataclasses.replace(state, parked=None)
        if pending is not None and pending.state == "receipt-landed":
            return _advance(cleared, facts, pending.step)
        raise RuntimeError("a gate advance needs a landed step to advance from")
    if isinstance(gate, GateAsk):
        return state, _ask_or_park(facts, state, gate.options)
    if isinstance(gate, GateStop):
        reason = stop_reason(gate.code, gate.detail)
        reasons: Dict[Step, str] = dict(state.stop_reason)
        reasons[state.step] = reason
        stopped = dataclasses.replace(state, status=gate.status, stop_reason=reasons, parked=None)
        return stopped, StopDirective(next_command=driver.next_command(facts.flow), reason=reason)
    if isinstance(gate, GateMerge):
        merge = gate.directive
        opened = PendingSpawn(step=merge.step, attempt=merge.attempt, nonce=merge.nonce, state="spawned", receipt_seen=None)
        _allowed(pending, opened)
        attempts: Dict[Step, int] = dict(state.attempts)
        attempts[merge.step] = merge.attempt
        merged = dataclasses.replace(_with_pending(state, opened), step=merge.step, attempts=attempts, parked=None)
        return merged, merge
    if isinstance(gate, GateDone):
        done = dataclasses.replace(state, status="done", pending_spawn=None, live=None, parked=None)
        return done, DoneDirective(next_command=driver.next_command(facts.flow))
    assert_never(gate)


def _wait_timed_out(state: V2State, facts: Facts) -> Optional[Tuple[V2State, Directive]]:
    """A wait-timeout with a live record and no landed receipt is a STOP naming clear-live (session Q4)."""
    pending = state.pending_spawn
    if pending is None or pending.state not in ("spawned", "resumed"):
        return None
    if facts.receipt != "none":
        return None
    detail = (
        f"step {pending.step} attempt {pending.attempt} is live with no landed receipt; "
        f"clear it with {driver.clear_live_command(facts.flow)}"
    )
    return _stop(state, facts, "wait-timeout", detail)


def transition(state: V2State, event: Event, facts: Facts) -> Tuple[V2State, Directive]:
    """
    The one pure transition. Returns the state to commit and the directive to emit. Replaying an event against the
    state it produced returns the same directive and allocates no new nonce or attempt (a WAIT keeps the live nonce).
    """
    if state.status in ("done", "abandoned"):
        # A finished flow never revives: no event, however odd, flips it to failed.
        return state, DoneDirective(next_command=driver.next_command(facts.flow))
    if isinstance(event, UnknownEvent):
        return _stop(state, facts, "unknown-event", f"label {event.label!r} is not a stub event")
    if isinstance(event, ResumeFailed):
        return _stop(state, facts, "resume-failed", f"the resume of step {state.step} failed")
    if isinstance(event, WaitTimeout):
        timed = _wait_timed_out(state, facts)
        if timed is not None:
            return timed
    elif isinstance(event, (Start, Reentry, SpawnLaunched, ResumeSent, WaitSettled, Notification, HumanContinue)):
        pass
    else:
        assert_never(event)

    if state.status in ("failed", "paused"):
        reason = state.stop_reason.get(state.step, stop_reason("step-failed", "the flow is failed"))
        return state, StopDirective(next_command=driver.next_command(facts.flow), reason=reason)
    if facts.receipt == "malformed-cap":
        return _stop(
            state,
            facts,
            "malformed-cap",
            f"step {state.step} receipt is malformed again; the automatic rerun is capped at one",
        )
    if facts.receipt == "failed":
        # A test-path code is a named stop with no Retry offered (the retry guard needs step-failed).
        if facts.failed_stop is not None:
            return _stop(state, facts, facts.failed_stop[0], facts.failed_stop[1])
        return _stop(state, facts, "step-failed", f"step {state.step} landed a FAILED receipt")
    if facts.receipt == "rerun":
        # D5: the malformed rerun is already recorded; the dispatch below opens the next attempt once no record is live.
        pass
    elif facts.receipt == "gate-hold":
        # D4: the malformed receipt is held by the gate; the dispatch below keeps the live record waiting.
        pass
    elif facts.receipt == "none":
        pass
    else:
        assert_never(facts.receipt)

    if facts.gate is not None:
        return _apply_gate(state, facts, facts.gate)

    pending = state.pending_spawn
    if pending is None:
        # No live record: the first spawn of a flow, a malformed rerun, or a spawn after clear-live.
        return _spawn(state, facts, state.step)
    if pending.state == "receipt-landed":
        return _advance(state, facts, pending.step)
    if pending.state == "parked-awaiting-answer":
        if facts.question == "answered":
            resumed = dataclasses.replace(pending, state="resumed", receipt_seen=None, receipt_first_seen=None)
            _allowed(pending, resumed)
            # For Layer 2 step questions, construct a temporary ParkedQuestion for path resolution
            parked_q = ParkedQuestion(step=pending.step, attempt=pending.attempt, nn=pending.attempt, kind="step")
            resume = ResumeDirective(
                next_command=driver.next_command(facts.flow),
                step=pending.step,
                attempt=pending.attempt,
                answer_file=str(relay.answer_path(facts.flow, parked_q)),
            )
            return _with_pending(state, resumed), resume
        return state, _ask_or_park(facts, state)
    if pending.state == "spawned":
        # A live spawned record: an awaiting question parks it; everything else waits.
        if facts.question == "awaiting":
            parked = dataclasses.replace(pending, state="parked-awaiting-answer")
            _allowed(pending, parked)
            updated_state = _with_pending(state, parked)
            return updated_state, _ask_or_park(facts, updated_state)
        return _wait(state, facts, pending)
    if pending.state == "resumed":
        # A live resumed record: the step agent is running, so wait for its receipt.
        return _wait(state, facts, pending)
    assert_never(pending.state)
