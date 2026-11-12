"""
Boundary types for the v2 skeleton driver (Sub-issue S, ADR-0026).

Everything the stub reads or the driver writes at the boundary is typed here: the closed verb set, the
directive union, the pending_spawn record, the state.json record, the allowed-transition table, the flow-dir
layout, and the validating loaders. Only the measured boundary is strictly typed (S-D4); the state dict
stays minimal. Python 3.8 floor: typing.Literal only, no match statements, no X | Y at runtime.
"""

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Dict, FrozenSet, Literal, NoReturn, Optional, Tuple, TypeVar, Union, get_args

DRIVER_FORMAT = "skeleton"
RUNNER_PREFIX = 'python3 "$HOME/.claude/scripts/flow_driver/__main__.py"'
SKELETON_STEP = "skeleton"
GENERAL_PURPOSE_AGENT = "general-purpose"
STOP_DIRECTIVE_NAME = "skeleton-stop.json"

Verb = Literal["SPAWN", "RESUME", "WAIT", "ASK", "PARK", "STOP", "DONE"]
VERBS: Tuple[Verb, ...] = get_args(Verb)  # type: ignore[assignment]
_VERB_BY_NAME: Dict[str, Verb] = {verb: verb for verb in VERBS}


def parse_verb(raw: str) -> Verb:
    """Map a stdout token to a verb. Unknown, lowercase and empty tokens are STOP; this never raises."""
    return _VERB_BY_NAME.get(raw, "STOP")


AfterEvent = Literal[
    "start",
    "spawn-launched",
    "resume-sent",
    "resume-failed",
    "wait-settled",
    "wait-timeout",
    "notification",
    "human-continue",
]
AFTER_EVENTS: Tuple[AfterEvent, ...] = get_args(AfterEvent)  # type: ignore[assignment]
_AFTER_BY_NAME: Dict[str, AfterEvent] = {event: event for event in AFTER_EVENTS}


def parse_after(raw: Optional[str]) -> Union[AfterEvent, Literal["none", "unrecognized"]]:
    """The logged `--after` label. Absent is "none"; an unknown label is "unrecognized". Never read by decide."""
    if raw is None:
        return "none"
    return _AFTER_BY_NAME.get(raw, "unrecognized")


Mode = Literal["interactive", "headless"]
WaitMode = Literal["end-turn", "foreground"]
PendingState = Literal["spawned", "parked-awaiting-answer", "resumed"]
MODES: Tuple[Mode, ...] = get_args(Mode)  # type: ignore[assignment]
WAIT_MODES: Tuple[WaitMode, ...] = get_args(WaitMode)  # type: ignore[assignment]
PENDING_STATES: Tuple[PendingState, ...] = get_args(PendingState)  # type: ignore[assignment]
_MODE_BY_NAME: Dict[str, Mode] = {mode: mode for mode in MODES}
_WAIT_MODE_BY_NAME: Dict[str, WaitMode] = {mode: mode for mode in WAIT_MODES}
_PENDING_BY_NAME: Dict[str, PendingState] = {state: state for state in PENDING_STATES}

# The only legal (previous, new) pending states. decide() checks every decision against this table, so
# a parked record can never reach None (no clear-live of a parked record, and decide never clears one).
# One question per attempt: spawned -> parked on the first question, parked -> resumed on the answer.
ALLOWED_TRANSITIONS: FrozenSet[Tuple[Optional[PendingState], Optional[PendingState]]] = frozenset(
    {
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
)


def transition_allowed(prev: Optional[PendingState], new: Optional[PendingState]) -> bool:
    return (prev, new) in ALLOWED_TRANSITIONS


def assert_never(value: NoReturn) -> NoReturn:
    """Exhaustiveness check for the closed unions (Python 3.8, no typing_extensions)."""
    raise AssertionError(f"unhandled case: {value!r}")


class SkeletonLoadError(Exception):
    """A boundary file is missing a key, has a wrong type or format, or is truncated or not JSON."""


@dataclass(frozen=True)
class StateRecord:
    repo: str
    flow_id: str
    mode: Mode
    wait_mode: WaitMode
    step_minutes: int
    created_utc: str
    last_attempt: int


@dataclass(frozen=True)
class PendingSpawn:
    step: str
    attempt: int
    nonce: str
    state: PendingState


@dataclass(frozen=True)
class LogEntry:
    """An event decide() wants in driver.log. Logged only; decide never reads its own log back."""

    event: str
    detail: str


@dataclass(frozen=True)
class KeepPending:
    """decide leaves pending_spawn.json as it is."""


@dataclass(frozen=True)
class SetPending:
    """decide writes this record to pending_spawn.json."""

    record: PendingSpawn


@dataclass(frozen=True)
class ClearPending:
    """decide removes pending_spawn.json (receipt-landed)."""


@dataclass(frozen=True)
class SpawnDirective:
    verb: ClassVar[Literal["SPAWN"]] = "SPAWN"
    next_command: str
    agent_type: str
    prompt_file: str
    step: str
    attempt: int
    nonce: str


@dataclass(frozen=True)
class ResumeDirective:
    verb: ClassVar[Literal["RESUME"]] = "RESUME"
    next_command: str
    step: str
    attempt: int
    answer_file: str


@dataclass(frozen=True)
class WaitDirective:
    verb: ClassVar[Literal["WAIT"]] = "WAIT"
    next_command: str
    wait_mode: WaitMode
    receipt_file: str
    clear_live_command: str


@dataclass(frozen=True)
class AskDirective:
    verb: ClassVar[Literal["ASK"]] = "ASK"
    next_command: str
    question_file: str
    answer_file: str
    answer_command: str


@dataclass(frozen=True)
class ParkDirective:
    verb: ClassVar[Literal["PARK"]] = "PARK"
    next_command: str
    question_file: str
    answer_file: str
    answer_command: str


@dataclass(frozen=True)
class StopDirective:
    verb: ClassVar[Literal["STOP"]] = "STOP"
    next_command: str
    reason: str


@dataclass(frozen=True)
class DoneDirective:
    verb: ClassVar[Literal["DONE"]] = "DONE"
    next_command: str


Directive = Union[
    SpawnDirective,
    ResumeDirective,
    WaitDirective,
    AskDirective,
    ParkDirective,
    StopDirective,
    DoneDirective,
]


# Flow-dir layout. Every path the driver or the step prompt uses is built here.
def state_path(flow: Path) -> Path:
    return flow / "state.json"


def pending_path(flow: Path) -> Path:
    return flow / "pending_spawn.json"


def log_path(flow: Path) -> Path:
    return flow / "driver.log"


def directive_path(flow: Path, attempt: int) -> Path:
    return flow / "directives" / f"skeleton-a{attempt}.json"


def stop_directive_path(flow: Path) -> Path:
    return flow / "directives" / STOP_DIRECTIVE_NAME


def prompt_path(flow: Path, attempt: int, nonce: str) -> Path:
    return flow / "prompts" / f"skeleton-a{attempt}-{nonce}.md"


def question_path(flow: Path, attempt: int) -> Path:
    return flow / "questions" / f"skeleton-a{attempt}.json"


def answer_path(flow: Path, attempt: int) -> Path:
    return flow / "answers" / f"skeleton-a{attempt}.json"


def receipt_path(flow: Path, attempt: int) -> Path:
    return flow / "receipts" / f"skeleton-a{attempt}.md"


def _check_loaded_object(data: object, what: str) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise SkeletonLoadError(f"{what}: top level is not a JSON object")
    if "driver_format" not in data:
        raise SkeletonLoadError(f"{what}: missing key 'driver_format'")
    if data["driver_format"] != DRIVER_FORMAT:
        raise SkeletonLoadError(f"{what}: driver_format is {data['driver_format']!r}, expected {DRIVER_FORMAT!r}")
    return data


def _load_object(text: str, what: str) -> Dict[str, Any]:
    try:
        data: object = json.loads(text)
    except ValueError as exc:
        raise SkeletonLoadError(f"{what}: invalid or truncated JSON ({exc})") from exc
    return _check_loaded_object(data, what)


def _require(obj: Dict[str, Any], key: str, what: str) -> Any:
    if key not in obj:
        raise SkeletonLoadError(f"{what}: missing key {key!r}")
    return obj[key]


def _str(obj: Dict[str, Any], key: str, what: str) -> str:
    value = _require(obj, key, what)
    if not isinstance(value, str):
        raise SkeletonLoadError(f"{what}: {key!r} must be a string")
    return value


def _int(obj: Dict[str, Any], key: str, what: str) -> int:
    value = _require(obj, key, what)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SkeletonLoadError(f"{what}: {key!r} must be an integer")
    return value


T = TypeVar("T")


def _pick(obj: Dict[str, Any], key: str, what: str, table: Dict[str, T]) -> T:
    value = _str(obj, key, what)
    if value not in table:
        raise SkeletonLoadError(f"{what}: {key!r} has unknown value {value!r}")
    return table[value]


def _command(obj: Dict[str, Any], key: str, what: str) -> str:
    value = _str(obj, key, what)
    if not value.startswith(RUNNER_PREFIX + " "):
        raise SkeletonLoadError(f"{what}: {key!r} does not begin with the runner prefix")
    return value


def load_state(text: str) -> StateRecord:
    what = "state.json"
    obj = _load_object(text, what)
    step_minutes = _int(obj, "step_minutes", what)
    last_attempt = _int(obj, "last_attempt", what)
    if step_minutes < 1:
        raise SkeletonLoadError(f"{what}: 'step_minutes' must be at least 1")
    if last_attempt < 0:
        raise SkeletonLoadError(f"{what}: 'last_attempt' must not be negative")
    return StateRecord(
        repo=_str(obj, "repo", what),
        flow_id=_str(obj, "flow_id", what),
        mode=_pick(obj, "mode", what, _MODE_BY_NAME),
        wait_mode=_pick(obj, "wait_mode", what, _WAIT_MODE_BY_NAME),
        step_minutes=step_minutes,
        created_utc=_str(obj, "created_utc", what),
        last_attempt=last_attempt,
    )


def load_pending(text: str) -> PendingSpawn:
    """Load and validate the pending_spawn.json record.

    Raises:
        SkeletonLoadError: if the JSON is malformed, missing a required field, has a wrong type,
            attempt < 1, nonce is empty, step != "skeleton", or state is not a valid PendingState.
        RecursionError: if JSON decoding exceeds the recursion limit.
    """
    what = "pending_spawn.json"
    try:
        obj = _load_object(text, what)
    except RecursionError as exc:
        raise SkeletonLoadError(f"{what}: JSON recursion limit exceeded") from exc
    step = _str(obj, "step", what)
    attempt = _int(obj, "attempt", what)
    nonce = _str(obj, "nonce", what)
    if attempt < 1:
        raise SkeletonLoadError(f"{what}: 'attempt' must be at least 1")
    if not nonce:
        raise SkeletonLoadError(f"{what}: 'nonce' must not be empty")
    if step != SKELETON_STEP:
        raise SkeletonLoadError(f"{what}: 'step' must be {SKELETON_STEP!r}, got {step!r}")
    return PendingSpawn(
        step=step,
        attempt=attempt,
        nonce=nonce,
        state=_pick(obj, "state", what, _PENDING_BY_NAME),
    )


def load_directive(text: str) -> Directive:
    what = "directive"
    obj = _load_object(text, what)
    verb = _pick(obj, "verb", what, _VERB_BY_NAME)
    if verb == "SPAWN":
        return SpawnDirective(
            next_command=_command(obj, "next_command", what),
            agent_type=_str(obj, "agent_type", what),
            prompt_file=_str(obj, "prompt_file", what),
            step=_str(obj, "step", what),
            attempt=_int(obj, "attempt", what),
            nonce=_str(obj, "nonce", what),
        )
    if verb == "RESUME":
        return ResumeDirective(
            next_command=_command(obj, "next_command", what),
            step=_str(obj, "step", what),
            attempt=_int(obj, "attempt", what),
            answer_file=_str(obj, "answer_file", what),
        )
    if verb == "WAIT":
        return WaitDirective(
            next_command=_command(obj, "next_command", what),
            wait_mode=_pick(obj, "wait_mode", what, _WAIT_MODE_BY_NAME),
            receipt_file=_str(obj, "receipt_file", what),
            clear_live_command=_command(obj, "clear_live_command", what),
        )
    if verb == "ASK":
        return AskDirective(
            next_command=_command(obj, "next_command", what),
            question_file=_str(obj, "question_file", what),
            answer_file=_str(obj, "answer_file", what),
            answer_command=_command(obj, "answer_command", what),
        )
    if verb == "PARK":
        return ParkDirective(
            next_command=_command(obj, "next_command", what),
            question_file=_str(obj, "question_file", what),
            answer_file=_str(obj, "answer_file", what),
            answer_command=_command(obj, "answer_command", what),
        )
    if verb == "STOP":
        return StopDirective(
            next_command=_command(obj, "next_command", what),
            reason=_str(obj, "reason", what),
        )
    if verb == "DONE":
        return DoneDirective(next_command=_command(obj, "next_command", what))
    assert_never(verb)


def state_to_dict(state: StateRecord) -> Dict[str, Any]:
    data: Dict[str, Any] = dataclasses.asdict(state)
    data["driver_format"] = DRIVER_FORMAT
    return data


def pending_to_dict(pending: PendingSpawn) -> Dict[str, Any]:
    data: Dict[str, Any] = dataclasses.asdict(pending)
    data["driver_format"] = DRIVER_FORMAT
    return data


def directive_to_dict(directive: Directive) -> Dict[str, Any]:
    data: Dict[str, Any] = dataclasses.asdict(directive)
    data["driver_format"] = DRIVER_FORMAT
    data["verb"] = directive.verb
    return data
