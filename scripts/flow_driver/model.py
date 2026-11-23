"""
Boundary vocabulary shared by the v2 driver (ADR-0026).

Layer 2 (Step 2, session D8) removes the skeleton directive union, the skeleton pending_spawn loaders and the skeleton
transition table. The closed Directive union and the pure transition live in steps.py; the recovery decisions live in
recovery.py. What remains here: the stdout verb set, the logged `--after` event labels, the mode types, assert_never,
and the skeleton state record that flowdir.py still names (flowdir.py is not edited in Layer 2). Python 3.8 floor:
typing.Literal only, no match statements, no X | Y at runtime.
"""

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Literal, NoReturn, Optional, Tuple, Union, get_args

DRIVER_FORMAT = "skeleton"
RUNNER_PREFIX = 'python3 "$HOME/.claude/scripts/flow_driver/__main__.py"'
GENERAL_PURPOSE_AGENT = "general-purpose"

Verb = Literal["SPAWN", "RESUME", "WAIT", "ASK", "PARK", "STOP", "DONE", "MERGE"]
VERBS: Tuple[Verb, ...] = get_args(Verb)
_VERB_BY_NAME: Dict[str, Verb] = {verb: verb for verb in VERBS}


def parse_verb(raw: str) -> Verb:
    """Map a stdout token to a verb. Unknown, lowercase and empty tokens are STOP; this never raises."""
    return _VERB_BY_NAME.get(raw, "STOP")


AfterEvent = Literal[
    "start",
    "spawn-launched",
    "resume-sent",
    "resume-failed",
    "spawn-failed",
    "wait-settled",
    "wait-timeout",
    "notification",
    "human-continue",
]
AFTER_EVENTS: Tuple[AfterEvent, ...] = get_args(AfterEvent)
_AFTER_BY_NAME: Dict[str, AfterEvent] = {event: event for event in AFTER_EVENTS}


def parse_after(raw: Optional[str]) -> Union[AfterEvent, Literal["none", "unrecognized"]]:
    """The logged `--after` label. Absent is "none"; an unknown label is "unrecognized"."""
    if raw is None:
        return "none"
    return _AFTER_BY_NAME.get(raw, "unrecognized")


Mode = Literal["interactive", "headless"]
WaitMode = Literal["end-turn", "foreground"]
MODES: Tuple[Mode, ...] = get_args(Mode)
WAIT_MODES: Tuple[WaitMode, ...] = get_args(WaitMode)
_MODE_BY_NAME: Dict[str, Mode] = {mode: mode for mode in MODES}
_WAIT_MODE_BY_NAME: Dict[str, WaitMode] = {mode: mode for mode in WAIT_MODES}


def assert_never(value: NoReturn) -> NoReturn:
    """Exhaustiveness check for the closed unions (Python 3.8, no typing_extensions)."""
    raise AssertionError(f"unhandled case: {value!r}")


@dataclass(frozen=True)
class StateRecord:
    """The skeleton state record. flowdir.py writes it for skeleton flows; the v2 driver never reads it."""

    repo: str
    flow_id: str
    mode: Mode
    wait_mode: WaitMode
    step_minutes: int
    created_utc: str
    last_attempt: int


# Flow-dir layout. The state path is the only one the v2 driver still needs from this module.
def state_path(flow: Path) -> Path:
    return flow / "state.json"


def state_to_dict(state: StateRecord) -> Dict[str, Any]:
    data: Dict[str, Any] = dataclasses.asdict(state)
    data["driver_format"] = DRIVER_FORMAT
    return data
