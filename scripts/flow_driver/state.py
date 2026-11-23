"""
Durable v2 flow state for the /expert-flow driver (Layer 1 of #485, ADR-0026 §5-§10).

state.json is the single commit point of every v2 transition. It is written only by LockedFlow.commit,
which is reachable only through mutating(). mutating() takes the flow lock once per invocation and
re-reads state under it. Side files (archives, driver-log.jsonl lines) are written before that replace
and named by their transition key, so a crash between writes replays as a no-op.

Schema (driver_format 2). Strict on the enumerated fields. Tolerant of unknown top-level keys, which are
kept verbatim in `extra` (Q5 = A):
  driver_format      int, always 2
  run_id             str, 16 hex characters minted by init
  issue              int
  repo               str
  created, updated   YYYY-MM-DDTHH:MM:SSZ; `updated` is set on every write
  step               worktree, plan, track, implement, review, fix, verify, assess, ship or merge
  status             running, awaiting, paused, failed, done or abandoned
  live               null, or {step, attempt, nonce}: exactly the projection of pending_spawn (D1)
  pending_spawn      null, or {step, attempt, nonce, state, receipt_seen}; state is spawned,
                     parked-awaiting-answer, resumed or receipt-landed; receipt_seen is null or
                     {sha256, size}
  attempts           {step: int >= 0}: the fleet's counter. Advanced only by record_spawn and by the
                     reconcile repair (Layer 1 Step 4), so attempts[step] == pending_spawn.attempt after a spawn
  failures           {step: int >= 0}
  stop_reason        {step: str}
  stop_source        {step: receipt, validation, audit or tree}
  pr                 null or str
  worktree           null or an absolute path
  healed             bool
  attempt_log        {step: {"<attempt>": receipt-landed, cleared, receipt-malformed or abandoned}}.
                     Kept separate from attempts so the fleet's attempts.plan keeps its meaning
  applied            the canonical transition keys already applied (see apply_transition)
  recovery           {rebases, heals, retros}: flow-level one-shot counters (Layer 2, Q3 = B). Absent in a
                     Layer 1 file, which loads as zeros. Written in the same replace as the transition it gates
  heads              {verify, ship}: the driver-owned HEAD sources for the ship guard (Layer 2). Each is a
                     40 or 64 lowercase hex SHA or null. Absent in a Layer 1 file, which loads as nulls
  bases              {implement, fix}: the worktree HEAD at the first SPAWN of each step (Layer 2 parity, Step 6),
                     a 40 or 64 lowercase hex value or null. Recorded once, never overwritten on Retry or replay; a
                     failed read leaves it null. Absent loads as nulls. `unavailable` is render-time only, never stored

  pending_spawn also carries (Layer 2, all optional on load): purpose (step, rebase, heal or retro), fleet
  (null or {run_id}; recorded only, session D7), receipt_first_seen (null or a UTC timestamp, the D4 anchor)
  and ledger_seq (null or the ledger high-water seq at spawn).

Skeleton findings this schema answers (docs/v2-inventory/skeleton-findings.md):
  AC2-K1-kill, AC2-K2-kill, PHANTOM-LIVE: `live` is a projection of pending_spawn (D1), so a cleared
    pending spawn cannot leave a live record behind.
  ORPHAN-OVERLAP: the flock fd is non-inheritable (fsutil.nonblocking_flock), so no child keeps the lock.
  LATE-RECEIPT, RESUME-b: a receipt only satisfies pending_spawn.attempt. A stale receipt is archived.
  PERMISSION-VOID: an attempt that ends with no receipt is recorded in attempt_log, not in attempts.
  S-D1: coverage disclaimer. The classification below is exhaustive over the five Owned cases. It is
    not a proof that the schema covers every state the skeleton produced.

Lock order (ADR-0026 §10): the fleet's per-ticket lock, then the flow lock (.driver.lock). main.lock is
never taken while the flow lock is held.

Python 3.8 floor: typing.Literal and typing.get_args only, no match statements, no X | Y at runtime.
"""

import dataclasses
import json
import os
import re
import secrets
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Callable,
    Dict,
    FrozenSet,
    Iterator,
    List,
    Literal,
    Mapping,
    NamedTuple,
    NewType,
    Optional,
    Set,
    Tuple,
    TypeVar,
    Union,
    get_args,
)

from scripts.workflow import fsutil
from scripts.workflow.fsutil import LockHeld, nonblocking_flock

from .model import assert_never

DRIVER_FORMAT_V2 = 2
SKELETON_FORMAT = "skeleton"
STATE_FILE = "state.json"
LOCK_FILE = ".driver.lock"
DRIVER_LOG_FILE = "driver-log.jsonl"
V1_FLOWS_DIR = ("flows",)  # under ~/.claude/

Step = Literal["worktree", "plan", "track", "implement", "review", "fix", "verify", "assess", "ship", "merge"]
Status = Literal["running", "awaiting", "paused", "failed", "done", "abandoned"]
StopSource = Literal["receipt", "validation", "audit", "tree"]
PendingState = Literal["spawned", "parked-awaiting-answer", "resumed", "receipt-landed"]
Purpose = Literal["step", "rebase", "heal", "retro", "audit"]
AttemptOutcome = Literal[
    "receipt-landed",
    "cleared",
    "receipt-malformed",
    "abandoned",
    "retried",
    "redone",
    "unknown-skill-retried",
]
# A git object name in heads.*: 40 hex (SHA-1) or 64 hex (SHA-256 repos).
Sha = NewType("Sha", str)
TransitionKind = Literal["once", "repeatable"]
RefusalClass = Literal[
    "locked",
    "v1-owned",
    "skeleton-owned",
    "schema-invalid",
    "no-state",
    "exists",
    "verifier-timeout",
    "v2-owned",
    "not-skeleton",
    "cascade-exhausted",
]

STEPS: Tuple[Step, ...] = get_args(Step)
STATUSES: Tuple[Status, ...] = get_args(Status)
STOP_SOURCES: Tuple[StopSource, ...] = get_args(StopSource)
PENDING_STATES: Tuple[PendingState, ...] = get_args(PendingState)
PURPOSES: Tuple[Purpose, ...] = get_args(Purpose)
ATTEMPT_OUTCOMES: Tuple[AttemptOutcome, ...] = get_args(AttemptOutcome)
ParkedKind = Literal["step", "gate", "merge-gate", "audit", "assess"]
AuditState = Literal["spawned", "passed"]
MergeAnswer = Literal["Merge", "Hold", "Take over"]
PARKED_KINDS: Tuple[ParkedKind, ...] = get_args(ParkedKind)
AUDIT_STATES: Tuple[AuditState, ...] = get_args(AuditState)
MERGE_ANSWERS: Tuple[MergeAnswer, ...] = get_args(MergeAnswer)
_STEP_BY_NAME: Dict[str, Step] = {step: step for step in STEPS}
_STATUS_BY_NAME: Dict[str, Status] = {status: status for status in STATUSES}
_PARKED_KIND_BY_NAME: Dict[str, ParkedKind] = {kind: kind for kind in PARKED_KINDS}
_AUDIT_STATE_BY_NAME: Dict[str, AuditState] = {state: state for state in AUDIT_STATES}
_MERGE_ANSWER_BY_NAME: Dict[str, MergeAnswer] = {answer: answer for answer in MERGE_ANSWERS}
_STOP_SOURCE_BY_NAME: Dict[str, StopSource] = {source: source for source in STOP_SOURCES}
_PENDING_BY_NAME: Dict[str, PendingState] = {state: state for state in PENDING_STATES}
_PURPOSE_BY_NAME: Dict[str, Purpose] = {purpose: purpose for purpose in PURPOSES}
_OUTCOME_BY_NAME: Dict[str, AttemptOutcome] = {outcome: outcome for outcome in ATTEMPT_OUTCOMES}

# Pending states whose live record exists (D1). parked-awaiting-answer and receipt-landed have none.
_LIVE_STATES: FrozenSet[PendingState] = frozenset({"spawned", "resumed"})

# The only legal (previous, new) pending states, checked on every transition. (None, None) means the
# transition leaves pending_spawn alone. Parked records are never cleared here, as in model.py.
ALLOWED_TRANSITIONS: FrozenSet[Tuple[Optional[PendingState], Optional[PendingState]]] = frozenset(
    {
        (None, None),
        (None, "spawned"),  # record_spawn
        ("spawned", "spawned"),  # store_fingerprint, archive_stale
        ("resumed", "resumed"),
        ("spawned", "parked-awaiting-answer"),
        ("parked-awaiting-answer", "parked-awaiting-answer"),
        ("parked-awaiting-answer", "resumed"),
        ("spawned", "receipt-landed"),  # land
        ("resumed", "receipt-landed"),
        ("spawned", None),  # rerun_malformed, record_cleared
        ("resumed", None),
        ("receipt-landed", None),  # record_cleared after a landed receipt
        ("receipt-landed", "spawned"),  # record_spawn replacing a landed record (Layer 2 rerun, recover)
    }
)

# Every transition name is declared exactly once here. "once" transitions run at most once per
# (run_id, step, attempt). "repeatable" transitions also carry a content hash in their key.
TransitionName = Literal[
    "record_spawn",
    "land",
    "rerun_malformed",
    "record_cleared",
    "archive_stale",
    "store_fingerprint",
    "park",
    "resume",
    "recover",
    "restore_previous",
    "abandon",
]

TRANSITION_KINDS: Dict[TransitionName, TransitionKind] = {
    "record_spawn": "once",
    "land": "once",
    "rerun_malformed": "once",
    "record_cleared": "once",
    "archive_stale": "repeatable",
    "store_fingerprint": "repeatable",
    "park": "once",
    "resume": "once",
    "recover": "once",
    "restore_previous": "once",
    "abandon": "once",
}

_KNOWN_KEYS: FrozenSet[str] = frozenset(
    {
        "driver_format",
        "run_id",
        "issue",
        "repo",
        "created",
        "updated",
        "step",
        "status",
        "live",
        "pending_spawn",
        "attempts",
        "failures",
        "stop_reason",
        "stop_source",
        "pr",
        "worktree",
        "healed",
        "attempt_log",
        "applied",
        "recovery",
        "heads",
        "bases",
        "step_gates",
        "audit",
        "audit_gate",
        "assess",
        "assess_gate",
        "parked",
        "merge_gate",
    }
)
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_ATTEMPT_KEY = re.compile(r"[1-9][0-9]*")
_MUTATING_TOKEN = object()
_T = TypeVar("_T")


class StateSchemaError(Exception):
    """A state.json field is missing, has the wrong type or value, or breaks the live projection rule."""

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


class Refusal(Exception):
    """The driver cannot resolve this situation itself. The CLI prints `refused: <class>: <detail>`."""

    def __init__(self, refusal_class: RefusalClass, detail: str) -> None:
        super().__init__(f"refused: {refusal_class}: {detail}")
        self.refusal_class = refusal_class
        self.detail = detail


class LiveRecord(NamedTuple):
    step: Step
    attempt: int
    nonce: str


@dataclass(frozen=True)
class ReceiptFingerprint:
    sha256: str
    size: int


@dataclass(frozen=True)
class FleetIdentity:
    """The fleet run that launched a spawn (session D7). Recorded only; the automatic clear-live is Layer 4."""

    run_id: str


@dataclass(frozen=True)
class PendingSpawn:
    step: Step
    attempt: int
    nonce: str
    state: PendingState
    receipt_seen: Optional[ReceiptFingerprint]
    purpose: Purpose = "step"
    fleet: Optional[FleetIdentity] = None
    receipt_first_seen: Optional[str] = None  # UTC YYYY-MM-DDTHH:MM:SSZ, the D4 quiet-interval anchor
    ledger_seq: Optional[int] = None  # the ledger high-water seq at spawn, used to re-render the snapshot


@dataclass(frozen=True)
class Recovery:
    """Flow-level one-shot counters (Q3 = B). Each is written in the same replace as the transition it gates."""

    rebases: int = 0
    heals: int = 0
    retros: int = 0


@dataclass(frozen=True)
class Heads:
    """
    Driver-owned HEAD sources for the ship guard (ADR-0026 re-read design). verify is the worktree HEAD re-read
    when the verify receipt lands OK; ship is the HEAD re-read when the ship receipt lands.
    """

    verify: Optional[Sha] = None
    ship: Optional[Sha] = None


@dataclass(frozen=True)
class Bases:
    """
    The worktree HEAD recorded at the first SPAWN of the implement and fix steps (Step 6). Each is a commit
    hex value or None. Written once and never overwritten on Retry or replay; `unavailable` is a render-time string,
    never stored here.
    """

    implement: Optional[Sha] = None
    fix: Optional[Sha] = None


@dataclass(frozen=True)
class ParkedQuestion:
    """
    A question parked mid-flow by the driver or a step agent (Layer 3, Session D1).

    Invariant: `item` is set only for an `assess` park, where it is the outstanding item id being asked. For every
    other kind it is None. The invariant is documented here and not enforced at construction; the assess park
    (relay.park) passes the item and every other park passes none.
    """

    step: Step
    attempt: int
    nn: int
    kind: ParkedKind
    item: Optional[str] = None


@dataclass(frozen=True)
class AuditRecord:
    """The fidelity audit of one landed step attempt (Layer 3, opt-in): spawned, then passed. Separate from pending_spawn."""

    step: Step
    attempt: int
    nonce: str
    state: AuditState


@dataclass(frozen=True)
class MergeGateRecord:
    """The merge gate decision point: PR identity and approved head (Layer 3, Session W1)."""

    pr_number: int
    approved_head: Sha
    repo: str
    answer: Optional[MergeAnswer] = None


class TransitionKey(NamedTuple):
    run_id: str
    step: Step
    attempt: int


@dataclass(frozen=True)
class V2State:
    driver_format: int
    run_id: str
    issue: int
    repo: str
    created: str
    updated: str
    step: Step
    status: Status
    live: Optional[LiveRecord]
    pending_spawn: Optional[PendingSpawn]
    attempts: Mapping[Step, int]
    failures: Mapping[Step, int]
    stop_reason: Mapping[Step, str]
    stop_source: Mapping[Step, StopSource]
    pr: Optional[str]
    worktree: Optional[str]
    healed: bool
    attempt_log: Mapping[Step, Mapping[int, AttemptOutcome]]
    applied: Tuple[str, ...]
    extra: Mapping[str, object]
    recovery: Recovery = Recovery()
    heads: Heads = Heads()
    bases: Bases = Bases()
    step_gates: bool = False
    audit: bool = False
    audit_gate: Optional[AuditRecord] = None
    assess: bool = False
    assess_gate: Optional[AuditRecord] = None  # same shape as the audit record; step is always "assess" here
    parked: Optional[ParkedQuestion] = None
    merge_gate: Optional[MergeGateRecord] = None


@dataclass(frozen=True)
class InitSpec:
    repo: str
    issue: int
    worktree: str


# Ownership dispatch. classify_state returns exactly one of these, and every dispatch site ends in assert_never.
@dataclass(frozen=True)
class V2Owned:
    path: Path
    state: V2State


@dataclass(frozen=True)
class V1Foreign:
    path: Path


@dataclass(frozen=True)
class SkeletonForeign:
    path: Path


@dataclass(frozen=True)
class InvalidState:
    path: Path
    field: str
    reason: str


@dataclass(frozen=True)
class NoState:
    path: Path


Owned = Union[V2Owned, V1Foreign, SkeletonForeign, InvalidState, NoState]


# Field-named access to one JSON object. Every failure names the offending field by its path.
class _Fields:
    def __init__(self, obj: Dict[str, object], prefix: str = "") -> None:
        self._obj = obj
        self._prefix = prefix

    def field(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def raw(self, key: str) -> object:
        if key not in self._obj:
            raise StateSchemaError(self.field(key), "missing")
        return self._obj[key]

    def present(self, key: str) -> bool:
        return key in self._obj

    def raw_or_none(self, key: str) -> object:
        """The raw value, or None when the key is absent. For Layer 2 keys that Layer 1 files lack."""
        return self._obj[key] if key in self._obj else None

    def text(self, key: str) -> str:
        return _as_text(self.field(key), self.raw(key))

    def integer(self, key: str, minimum: Optional[int] = None) -> int:
        return _as_int(self.field(key), self.raw(key), minimum)

    def boolean(self, key: str) -> bool:
        value = self.raw(key)
        if not isinstance(value, bool):
            raise StateSchemaError(self.field(key), "must be true or false")
        return value

    def timestamp(self, key: str) -> str:
        text = self.text(key)
        if _TIMESTAMP.fullmatch(text) is None:
            raise StateSchemaError(self.field(key), "must be YYYY-MM-DDTHH:MM:SSZ")
        return text

    def optional_text(self, key: str) -> Optional[str]:
        value = self.raw(key)
        if value is None:
            return None
        return _as_text(self.field(key), value)

    def pick(self, key: str, table: Dict[str, _T]) -> _T:
        return _as_enum(self.field(key), self.raw(key), table)

    def mapping(self, key: str) -> Dict[str, object]:
        return _as_object(self.field(key), self.raw(key))


def _as_text(field: str, value: object) -> str:
    if not isinstance(value, str):
        raise StateSchemaError(field, "must be a string")
    return value


def _as_int(field: str, value: object, minimum: Optional[int]) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StateSchemaError(field, "must be an integer")
    if minimum is not None and value < minimum:
        raise StateSchemaError(field, f"must be at least {minimum}")
    return value


def _as_enum(field: str, value: object, table: Dict[str, _T]) -> _T:
    text = _as_text(field, value)
    if text not in table:
        raise StateSchemaError(field, f"unknown value {text!r}")
    return table[text]


def _as_object(field: str, value: object) -> Dict[str, object]:
    if not isinstance(value, dict):
        raise StateSchemaError(field, "must be a JSON object")
    return value


def _non_empty(fields: _Fields, key: str) -> str:
    text = fields.text(key)
    if not text:
        raise StateSchemaError(fields.field(key), "must not be empty")
    return text


def _load_live(value: object) -> Optional[LiveRecord]:
    if value is None:
        return None
    fields = _Fields(_as_object("live", value), "live.")
    return LiveRecord(
        step=fields.pick("step", _STEP_BY_NAME),
        attempt=fields.integer("attempt", 1),
        nonce=_non_empty(fields, "nonce"),
    )


def _load_fingerprint(value: object, field: str) -> Optional[ReceiptFingerprint]:
    if value is None:
        return None
    fields = _Fields(_as_object(field, value), f"{field}.")
    sha256 = fields.text("sha256")
    if _SHA256.fullmatch(sha256) is None:
        raise StateSchemaError(fields.field("sha256"), "must be 64 lowercase hex characters")
    return ReceiptFingerprint(sha256=sha256, size=fields.integer("size", 0))


def _load_fleet(value: object, field: str) -> Optional[FleetIdentity]:
    if value is None:
        return None
    fields = _Fields(_as_object(field, value), f"{field}.")
    return FleetIdentity(run_id=_non_empty(fields, "run_id"))


def _load_optional_timestamp(value: object, field: str) -> Optional[str]:
    if value is None:
        return None
    text = _as_text(field, value)
    if _TIMESTAMP.fullmatch(text) is None:
        raise StateSchemaError(field, "must be YYYY-MM-DDTHH:MM:SSZ")
    return text


def _load_pending(value: object) -> Optional[PendingSpawn]:
    if value is None:
        return None
    fields = _Fields(_as_object("pending_spawn", value), "pending_spawn.")
    purpose_value = fields.raw_or_none("purpose")
    ledger_seq_value = fields.raw_or_none("ledger_seq")
    return PendingSpawn(
        step=fields.pick("step", _STEP_BY_NAME),
        attempt=fields.integer("attempt", 1),
        nonce=_non_empty(fields, "nonce"),
        state=fields.pick("state", _PENDING_BY_NAME),
        receipt_seen=_load_fingerprint(fields.raw("receipt_seen"), fields.field("receipt_seen")),
        purpose="step" if purpose_value is None else _as_enum(fields.field("purpose"), purpose_value, _PURPOSE_BY_NAME),
        fleet=_load_fleet(fields.raw_or_none("fleet"), fields.field("fleet")),
        receipt_first_seen=_load_optional_timestamp(
            fields.raw_or_none("receipt_first_seen"), fields.field("receipt_first_seen")
        ),
        ledger_seq=None if ledger_seq_value is None else _as_int(fields.field("ledger_seq"), ledger_seq_value, 0),
    )


def _optional_counter(block: _Fields, key: str) -> int:
    value = block.raw_or_none(key)
    return 0 if value is None else _as_int(block.field(key), value, 0)


def _load_recovery(fields: _Fields) -> Recovery:
    """An absent block loads as zeros, so a Layer 1 state.json needs no migration."""
    if not fields.present("recovery"):
        return Recovery()
    block = _Fields(_as_object("recovery", fields.raw("recovery")), "recovery.")
    return Recovery(
        rebases=_optional_counter(block, "rebases"),
        heals=_optional_counter(block, "heals"),
        retros=_optional_counter(block, "retros"),
    )


def _load_head(value: object, field: str) -> Optional[Sha]:
    if value is None:
        return None
    text = _as_text(field, value)
    if _GIT_SHA.fullmatch(text) is None:
        raise StateSchemaError(field, "must be 40 or 64 lowercase hex characters")
    return Sha(text)


def _load_bases(fields: _Fields) -> Bases:
    """An absent block loads as both None, so a file without it needs no migration."""
    if not fields.present("bases"):
        return Bases()
    block = _Fields(_as_object("bases", fields.raw("bases")), "bases.")
    return Bases(
        implement=_load_head(block.raw_or_none("implement"), block.field("implement")),
        fix=_load_head(block.raw_or_none("fix"), block.field("fix")),
    )


def _load_heads(fields: _Fields) -> Heads:
    """An absent block loads as nulls, so a Layer 1 state.json needs no migration."""
    if not fields.present("heads"):
        return Heads()
    block = _Fields(_as_object("heads", fields.raw("heads")), "heads.")
    return Heads(
        verify=_load_head(block.raw_or_none("verify"), block.field("verify")),
        ship=_load_head(block.raw_or_none("ship"), block.field("ship")),
    )


def _load_parked(value: object, field: str) -> Optional[ParkedQuestion]:
    """An absent field loads as None (Layer 3, absent in earlier files)."""
    if value is None:
        return None
    fields = _Fields(_as_object(field, value), f"{field}.")
    kind_value = fields.raw("kind")
    kind_text = _as_text(fields.field("kind"), kind_value)
    kind = _PARKED_KIND_BY_NAME.get(kind_text)
    if kind is None:
        raise StateSchemaError(fields.field("kind"), "must be one of step, gate, merge-gate, audit, assess")
    return ParkedQuestion(
        step=fields.pick("step", _STEP_BY_NAME),
        attempt=fields.integer("attempt", 1),
        nn=fields.integer("nn", 0),
        kind=kind,
        item=fields.optional_text("item"),
    )


def _load_audit_gate(value: object, field: str, expected_step: Optional[Step] = None) -> Optional[AuditRecord]:
    """
    An absent field loads as None (Layer 3, absent in earlier files). When expected_step is given, the record's step
    must be that step: assess_gate reuses AuditRecord and is always the assess step.
    """
    if value is None:
        return None
    fields = _Fields(_as_object(field, value), f"{field}.")
    state_text = _as_text(fields.field("state"), fields.raw("state"))
    state = _AUDIT_STATE_BY_NAME.get(state_text)
    if state is None:
        raise StateSchemaError(fields.field("state"), "must be spawned or passed")
    step = fields.pick("step", _STEP_BY_NAME)
    if expected_step is not None and step != expected_step:
        raise StateSchemaError(fields.field("step"), f"must be {expected_step}")
    return AuditRecord(
        step=step,
        attempt=fields.integer("attempt", 1),
        nonce=fields.text("nonce"),
        state=state,
    )


def _load_merge_gate(value: object, field: str) -> Optional[MergeGateRecord]:
    """An absent field loads as None (Layer 3, absent in earlier files)."""
    if value is None:
        return None
    fields = _Fields(_as_object(field, value), f"{field}.")
    answer_value = fields.raw_or_none("answer")
    answer: Optional[MergeAnswer] = None
    if answer_value is not None:
        answer_text = _as_text(fields.field("answer"), answer_value)
        answer = _MERGE_ANSWER_BY_NAME.get(answer_text)
        if answer is None:
            raise StateSchemaError(fields.field("answer"), "must be Merge, Hold or Take over")
    approved_head_text = fields.text("approved_head")
    if _GIT_SHA.fullmatch(approved_head_text) is None:
        raise StateSchemaError(fields.field("approved_head"), "must be 40 or 64 lowercase hex characters")
    return MergeGateRecord(
        pr_number=fields.integer("pr_number", 1),
        approved_head=Sha(approved_head_text),
        repo=fields.text("repo"),
        answer=answer,
    )


def _step_map(fields: _Fields, key: str, check: Callable[[str, object], _T]) -> Dict[Step, _T]:
    out: Dict[Step, _T] = {}
    for name, value in fields.mapping(key).items():
        field = f"{fields.field(key)}.{name}"
        out[_as_enum(field, name, _STEP_BY_NAME)] = check(field, value)
    return out


def _counter(field: str, value: object) -> int:
    return _as_int(field, value, 0)


def _stop_source(field: str, value: object) -> StopSource:
    return _as_enum(field, value, _STOP_SOURCE_BY_NAME)


def _load_attempt_log(fields: _Fields) -> Dict[Step, Dict[int, AttemptOutcome]]:
    out: Dict[Step, Dict[int, AttemptOutcome]] = {}
    for step_name, per_step in fields.mapping("attempt_log").items():
        step_field = f"{fields.field('attempt_log')}.{step_name}"
        step = _as_enum(step_field, step_name, _STEP_BY_NAME)
        for attempt_text, outcome in _as_object(step_field, per_step).items():
            attempt_field = f"{step_field}.{attempt_text}"
            if _ATTEMPT_KEY.fullmatch(attempt_text) is None:
                raise StateSchemaError(attempt_field, "attempt key must be a positive integer string")
            out.setdefault(step, {})[int(attempt_text)] = _as_enum(attempt_field, outcome, _OUTCOME_BY_NAME)
    return out


def _load_applied(fields: _Fields) -> Tuple[str, ...]:
    value = fields.raw("applied")
    if not isinstance(value, list):
        raise StateSchemaError(fields.field("applied"), "must be a JSON array")
    items: List[str] = [_as_text(fields.field("applied"), item) for item in value]
    return tuple(items)


def _load_worktree(fields: _Fields) -> Optional[str]:
    worktree = fields.optional_text("worktree")
    if worktree is not None and not os.path.isabs(worktree):
        raise StateSchemaError(fields.field("worktree"), "must be an absolute path")
    return worktree


def _project_live(pending: Optional[PendingSpawn]) -> Optional[LiveRecord]:
    """D1: live is exactly the projection of pending_spawn, and only spawned or resumed project."""
    if pending is None or pending.state not in _LIVE_STATES:
        return None
    return LiveRecord(step=pending.step, attempt=pending.attempt, nonce=pending.nonce)


_RUN_ID_RE = re.compile(r"[0-9a-f]{16}")
_REPO_SHAPE_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def load_v2_state(raw: object) -> V2State:
    """Validate a parsed state.json object into a V2State. Raises StateSchemaError on any violation."""
    if not isinstance(raw, dict):
        raise StateSchemaError("state.json", "top level is not a JSON object")
    obj: Dict[str, object] = raw
    fields = _Fields(obj)
    driver_format = fields.integer("driver_format")
    if driver_format != DRIVER_FORMAT_V2:
        raise StateSchemaError("driver_format", f"must be {DRIVER_FORMAT_V2}, got {driver_format}")
    state = V2State(
        driver_format=driver_format,
        run_id=fields.text("run_id"),
        issue=fields.integer("issue"),
        repo=fields.text("repo"),
        created=fields.timestamp("created"),
        updated=fields.timestamp("updated"),
        step=fields.pick("step", _STEP_BY_NAME),
        status=fields.pick("status", _STATUS_BY_NAME),
        live=_load_live(fields.raw("live")),
        pending_spawn=_load_pending(fields.raw("pending_spawn")),
        attempts=_step_map(fields, "attempts", _counter),
        failures=_step_map(fields, "failures", _counter),
        stop_reason=_step_map(fields, "stop_reason", _as_text),
        stop_source=_step_map(fields, "stop_source", _stop_source),
        pr=fields.optional_text("pr"),
        worktree=_load_worktree(fields),
        healed=fields.boolean("healed"),
        attempt_log=_load_attempt_log(fields),
        applied=_load_applied(fields),
        extra={key: value for key, value in obj.items() if key not in _KNOWN_KEYS},
        recovery=_load_recovery(fields),
        heads=_load_heads(fields),
        bases=_load_bases(fields),
        step_gates=fields.boolean("step_gates") if fields.present("step_gates") else False,
        audit=fields.boolean("audit") if fields.present("audit") else False,
        audit_gate=_load_audit_gate(fields.raw_or_none("audit_gate"), "audit_gate"),
        assess=fields.boolean("assess") if fields.present("assess") else False,
        assess_gate=_load_audit_gate(fields.raw_or_none("assess_gate"), "assess_gate", expected_step="assess"),
        parked=_load_parked(fields.raw_or_none("parked"), "parked"),
        merge_gate=_load_merge_gate(fields.raw_or_none("merge_gate"), "merge_gate"),
    )
    if state.live != _project_live(state.pending_spawn):
        raise StateSchemaError("live", "must be null unless pending_spawn is spawned or resumed, then a copy of it")
    if not _RUN_ID_RE.fullmatch(state.run_id):
        raise StateSchemaError("run_id", "must be 16 lowercase hex characters")
    if not _REPO_SHAPE_RE.fullmatch(state.repo):
        raise StateSchemaError("repo", "must be OWNER/NAME")
    pending = state.pending_spawn
    if pending is not None and state.attempts.get(pending.step, 0) < pending.attempt:
        raise StateSchemaError("attempts", "the counter for the pending step must be >= pending_spawn.attempt")
    if len(set(state.applied)) != len(state.applied):
        raise StateSchemaError("applied", "must not contain duplicate keys")
    return state


def _pending_to_dict(pending: Optional[PendingSpawn]) -> Optional[Dict[str, object]]:
    if pending is None:
        return None
    seen: Optional[Dict[str, object]] = None
    if pending.receipt_seen is not None:
        seen = {"sha256": pending.receipt_seen.sha256, "size": pending.receipt_seen.size}
    fleet: Optional[Dict[str, object]] = None
    if pending.fleet is not None:
        fleet = {"run_id": pending.fleet.run_id}
    return {
        "step": pending.step,
        "attempt": pending.attempt,
        "nonce": pending.nonce,
        "state": pending.state,
        "receipt_seen": seen,
        "purpose": pending.purpose,
        "fleet": fleet,
        "receipt_first_seen": pending.receipt_first_seen,
        "ledger_seq": pending.ledger_seq,
    }


def _live_to_dict(live: Optional[LiveRecord]) -> Optional[Dict[str, object]]:
    if live is None:
        return None
    return {"step": live.step, "attempt": live.attempt, "nonce": live.nonce}


def _parked_to_dict(parked: Optional[ParkedQuestion]) -> Optional[Dict[str, object]]:
    if parked is None:
        return None
    return {
        "step": parked.step,
        "attempt": parked.attempt,
        "nn": parked.nn,
        "kind": parked.kind,
        "item": parked.item,
    }


def _audit_gate_to_dict(record: Optional[AuditRecord]) -> Optional[Dict[str, object]]:
    if record is None:
        return None
    return {"step": record.step, "attempt": record.attempt, "nonce": record.nonce, "state": record.state}


def _merge_gate_to_dict(merge_gate: Optional[MergeGateRecord]) -> Optional[Dict[str, object]]:
    if merge_gate is None:
        return None
    return {
        "pr_number": merge_gate.pr_number,
        "approved_head": merge_gate.approved_head,
        "repo": merge_gate.repo,
        "answer": merge_gate.answer,
    }


def v2_state_to_dict(state: V2State) -> Dict[str, object]:
    """The JSON object for a V2State, with keys sorted and the preserved extra keys merged back in."""
    attempt_log: Dict[str, Dict[str, str]] = {
        step: {str(attempt): outcome for attempt, outcome in sorted(per_step.items())}
        for step, per_step in sorted(state.attempt_log.items())
    }
    data: Dict[str, object] = dict(state.extra)
    data.update(
        {
            "driver_format": state.driver_format,
            "run_id": state.run_id,
            "issue": state.issue,
            "repo": state.repo,
            "created": state.created,
            "updated": state.updated,
            "step": state.step,
            "status": state.status,
            "live": _live_to_dict(state.live),
            "pending_spawn": _pending_to_dict(state.pending_spawn),
            "attempts": dict(state.attempts),
            "failures": dict(state.failures),
            "stop_reason": dict(state.stop_reason),
            "stop_source": dict(state.stop_source),
            "pr": state.pr,
            "worktree": state.worktree,
            "healed": state.healed,
            "attempt_log": attempt_log,
            "applied": list(state.applied),
            "recovery": {
                "rebases": state.recovery.rebases,
                "heals": state.recovery.heals,
                "retros": state.recovery.retros,
            },
            "heads": {"verify": state.heads.verify, "ship": state.heads.ship},
            "bases": {"implement": state.bases.implement, "fix": state.bases.fix},
            "step_gates": state.step_gates,
            "audit": state.audit,
            "audit_gate": _audit_gate_to_dict(state.audit_gate),
            "assess": state.assess,
            "assess_gate": _audit_gate_to_dict(state.assess_gate),
            "parked": _parked_to_dict(state.parked),
            "merge_gate": _merge_gate_to_dict(state.merge_gate),
        }
    )
    return dict(sorted(data.items()))


def _v1_flows_root() -> Path:
    return Path(os.path.expanduser("~")).joinpath(".claude", *V1_FLOWS_DIR).resolve()


def _is_under_v1_root(path: Path) -> bool:
    resolved = path.resolve()
    root = _v1_flows_root()
    return resolved == root or root in resolved.parents


def classify_state(flow_dir: Path) -> Owned:
    """
    Decide who owns a flow dir from its state.json. Pure: it writes nothing and takes no lock.

    A dir under ~/.claude/flows/ is v1 whatever its contents. No state.json is NoState. A state.json
    without driver_format is v1. driver_format "skeleton" is skeleton-owned. Only an int 2 that validates
    is v2. Every other value, including True, "2" and 2.0, is invalid.
    """
    path = Path(flow_dir)
    if _is_under_v1_root(path):
        return V1Foreign(path)
    try:
        text = (path / STATE_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        return NoState(path)
    except (OSError, UnicodeDecodeError) as exc:
        return InvalidState(path, STATE_FILE, f"unreadable ({exc.__class__.__name__})")
    try:
        raw: object = json.loads(text)
    except (ValueError, RecursionError):
        return InvalidState(path, STATE_FILE, "not valid JSON")
    if not isinstance(raw, dict):
        return InvalidState(path, STATE_FILE, "top level is not a JSON object")
    if "driver_format" not in raw:
        return V1Foreign(path)
    driver_format = raw["driver_format"]
    if isinstance(driver_format, str) and driver_format == SKELETON_FORMAT:
        return SkeletonForeign(path)
    if type(driver_format) is int and driver_format == DRIVER_FORMAT_V2:
        try:
            return V2Owned(path, load_v2_state(raw))
        except StateSchemaError as exc:
            return InvalidState(path, exc.field, exc.reason)
    return InvalidState(path, "driver_format", f"unrecognised value {driver_format!r}")


@dataclass(frozen=True)
class ReadOnlyFlow:
    """A read-only view of one flow dir. Produced by read_flow; holds no lock and opens nothing for writing."""

    path: Path
    owned: Owned


def read_flow(flow_dir: Path) -> ReadOnlyFlow:
    path = Path(flow_dir)
    return ReadOnlyFlow(path=path, owned=classify_state(path))


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _append_driver_log(flow_dir: Path, record: Dict[str, object]) -> None:
    """Append one JSON line to driver-log.jsonl. Append-only; a crash replay may leave a duplicate line."""
    line = json.dumps(record, sort_keys=True)
    with open(flow_dir / DRIVER_LOG_FILE, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


class LockedFlow:
    """
    Capability for one mutating invocation. Only mutating() constructs it, while it holds the flow lock.

    commit() is the only writer of state.json. Once the mutating context exits, both commit and log raise
    RuntimeError, so the capability cannot be kept and used later.
    """

    def __init__(self, path: Path, state: Optional[V2State], *, token: object) -> None:
        if token is not _MUTATING_TOKEN:
            raise RuntimeError("LockedFlow is constructed only by mutating()")
        self.path = path
        self._state = state
        self._open = True

    @property
    def state(self) -> Optional[V2State]:
        return self._state

    def require_state(self) -> V2State:
        if self._state is None:
            raise RuntimeError("this flow has no state.json yet; run init_state first")
        return self._state

    def commit(self, new_state: V2State) -> None:
        """Stamp updated, re-apply the D1 projection, validate, then replace state.json atomically."""
        self._check_open("commit")
        stamped = dataclasses.replace(
            new_state,
            updated=_utc_now(),
            live=_project_live(new_state.pending_spawn),
        )
        text = json.dumps(v2_state_to_dict(stamped), indent=2, sort_keys=True) + "\n"
        load_v2_state(json.loads(text))  # the bytes must read back as a valid v2 state before they replace the old file
        fsutil.atomic_write_text(self.path / STATE_FILE, text, fsync=True)
        self._state = stamped

    def log(self, event: str, *, step: Step, attempt: int, **fields: object) -> None:
        self._check_open("log")
        record: Dict[str, object] = {
            "ts": _utc_now(),
            "event": event,
            "run_id": None if self._state is None else self._state.run_id,
            "step": step,
            "attempt": attempt,
        }
        record.update(fields)
        _append_driver_log(self.path, record)

    def _check_open(self, what: str) -> None:
        if not self._open:
            raise RuntimeError(f"{what}: the mutating context has exited; take a new one")

    def _close(self) -> None:
        self._open = False


def v1_owned_refusal(name: str) -> Refusal:
    return Refusal("v1-owned", f"flow {name} is a v1 flow and is read-only to the v2 driver")


def v2_owned_refusal(name: str) -> Refusal:
    return Refusal("v2-owned", f"flow {name} is a v2 flow; use init/resume")


def no_state_refusal(name: str) -> Refusal:
    return Refusal("no-state", f"flow {name} has no state.json; run init first")


def _refuse_for_write(owned: Owned, flow_dir: Path, create: Optional[InitSpec]) -> None:
    """Refuse every ownership the driver must not write to. Used before the lock and again under it."""
    name = flow_dir.name
    if isinstance(owned, V1Foreign):
        raise v1_owned_refusal(name)
    if isinstance(owned, SkeletonForeign):
        raise Refusal("skeleton-owned", f'flow {name} has driver_format "skeleton"')
    if isinstance(owned, InvalidState):
        raise Refusal("schema-invalid", f"{owned.field}: {owned.reason}")
    if isinstance(owned, NoState):
        if create is None:
            raise no_state_refusal(name)
        return
    if isinstance(owned, V2Owned):
        if create is not None:
            raise Refusal("exists", f"flow {name} already has state.json")
        return
    assert_never(owned)


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of the reconcile pass: per-step results and any repairs applied."""

    per_step: Mapping[Step, Union[Literal["Ok"], Mapping[str, object]]]
    repairs: Tuple[Tuple[Step, int, int], ...]  # (step, from_counter, to_counter)


_ARCHIVE_NAME = re.compile(r"^a([1-9][0-9]*)\.(landed|malformed|stale\.[0-9a-f]+|rebuilt)\.md$")


def _scan_step_archive(step_archive_dir: Path) -> Tuple[Set[int], int]:
    """Scan: (landed attempts, highest attempt) found in archive/<step>/. Reads only, trusted sources only."""
    landed: Set[int] = set()
    highest = 0
    if not step_archive_dir.is_dir():
        return landed, highest
    from . import receipts as receipts_module  # local import to avoid cycle
    for entry in step_archive_dir.iterdir():
        if entry.name.startswith(".") or entry.name.endswith(".tmp"):
            continue
        match = _ARCHIVE_NAME.match(entry.name)
        if not match:
            continue
        k = int(match.group(1))
        highest = max(highest, k)
        if match.group(2) == "landed":
            landed.add(k)
            try:
                from_receipt = receipts_module.receipt_attempt(entry.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
            if from_receipt is not None:
                highest = max(highest, from_receipt)
    return landed, highest


def reconcile(locked: LockedFlow) -> ReconcileResult:
    """
    Reconcile attempts with archived receipts (Layer 1 Step 4).

    Called under the lock, after validation, before the caller decides. For each step in attempts,
    attempt_log or archive/: compare recorded landed attempts with archived landed receipts,
    repair a lagging counter from the highest archived attempt (trusted sources only: filename
    attempts and parsed ATTEMPT from landed archives), and log mismatches. Never refuses.

    Returns a ReconcileResult with per-step reconcile outcomes and any counter repairs applied.
    """
    state = locked.require_state()
    archive_base = locked.path / "archive"
    repairs: List[Tuple[Step, int, int]] = []
    per_step: Dict[Step, Union[Literal["Ok"], Mapping[str, object]]] = {}
    new_attempts = dict(state.attempts)

    # Collect all steps that appear anywhere
    steps_to_check = set(state.attempts.keys()) | set(state.attempt_log.keys())
    if archive_base.is_dir():
        for step_dir in archive_base.iterdir():
            if step_dir.is_dir():
                try:
                    step = _as_enum("step", step_dir.name, _STEP_BY_NAME)
                    steps_to_check.add(step)
                except StateSchemaError:
                    pass

    for step in sorted(steps_to_check):
        # Find landed attempts recorded in attempt_log with outcome receipt-landed
        landed_recorded = set()
        if step in state.attempt_log:
            for attempt, outcome in state.attempt_log[step].items():
                if outcome == "receipt-landed":
                    landed_recorded.add(attempt)

        landed_archived, highest_archived = _scan_step_archive(archive_base / step)

        # Detect mismatch (kept in per_step; a later "Ok" never overwrites it)
        counter = state.attempts.get(step, 0)
        if landed_recorded != landed_archived:
            missing = landed_recorded - landed_archived
            unrecorded = landed_archived - landed_recorded
            mismatch: Dict[str, object] = {
                "event": "attempt-reconcile-mismatch",
                "step": step,
                "counter": counter,
                "landed_recorded": sorted(landed_recorded),
                "landed_archived": sorted(landed_archived),
            }
            if missing:
                mismatch["missing_archives"] = sorted(missing)
            if unrecorded:
                mismatch["unrecorded_archives"] = sorted(unrecorded)
            log_fields = {k: v for k, v in mismatch.items() if k not in ("event", "step")}
            locked.log("attempt-reconcile-mismatch", step=step, attempt=counter, **log_fields)
            per_step[step] = mismatch

        # Repair: raise the counter, unless this step's spawn is pending (record_spawn already owns it)
        if counter < highest_archived:
            pending = state.pending_spawn
            if pending is not None and pending.step == step:
                locked.log("attempt-counter-repair-deferred", step=step, attempt=counter, to=highest_archived)
            else:
                repairs.append((step, counter, highest_archived))
                new_attempts[step] = highest_archived
                locked.log("attempt-counter-repaired", step=step, attempt=highest_archived, **{"from": counter, "to": highest_archived})
        per_step.setdefault(step, "Ok")

    if repairs:
        locked.commit(dataclasses.replace(state, attempts=new_attempts))

    return ReconcileResult(per_step=per_step, repairs=tuple(repairs))


@contextmanager
def mutating(flow_dir: Path, *, create: Optional[InitSpec] = None) -> Iterator[LockedFlow]:
    """
    The only way to write a v2 flow. Order of operations:

      1. Pre-lock classification, used only to refuse. A v1 or skeleton dir is refused before .driver.lock
         exists, so it stays byte-identical.
      2. For init (create given): mkdir the flow dir.
      3. Take the non-blocking flock. LockHeld becomes Refusal("locked").
      4. Re-read and re-classify under the lock, and discard the pre-lock value.
      5. Run the reconcile hook, then yield the LockedFlow.
    """
    path = Path(flow_dir)
    _refuse_for_write(classify_state(path), path, create)
    if create is not None:
        path.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        try:
            stack.enter_context(nonblocking_flock(path / LOCK_FILE))
        except LockHeld as exc:
            raise Refusal("locked", f"flow {path.name} is held by another driver invocation") from exc
        owned = classify_state(path)
        _refuse_for_write(owned, path, create)
        state = owned.state if isinstance(owned, V2Owned) else None
        locked = LockedFlow(path, state, token=_MUTATING_TOKEN)
        if state is not None:
            reconcile(locked)
        try:
            yield locked
        finally:
            locked._close()


def canonical_key(key: TransitionKey, name: str, content: Optional[str]) -> str:
    base = f"{key.run_id}:{key.step}:{key.attempt}:{name}"
    return base if content is None else f"{base}:{content}"


def _require_pending_transition(prev: V2State, new: V2State) -> None:
    prev_state = None if prev.pending_spawn is None else prev.pending_spawn.state
    new_state = None if new.pending_spawn is None else new.pending_spawn.state
    if (prev_state, new_state) not in ALLOWED_TRANSITIONS:
        raise RuntimeError(f"illegal pending_spawn transition {prev_state!r} -> {new_state!r}")
    if prev.pending_spawn is not None and new.pending_spawn is not None:
        before, after = prev.pending_spawn, new.pending_spawn
        if (before.step, before.attempt, before.nonce) != (after.step, after.attempt, after.nonce):
            raise RuntimeError("illegal pending_spawn transition: the spawn identity changed")


TransitionFn = Callable[[LockedFlow, V2State], V2State]


def apply_transition(
    locked: LockedFlow,
    key: TransitionKey,
    name: TransitionName,
    fn: TransitionFn,
    *,
    content: Optional[str] = None,
    postcondition: Optional[Callable[[V2State], bool]] = None,
) -> None:
    """
    Run one named transition once, keyed so a replay is a no-op.

    A "once" transition has the key f"{run_id}:{step}:{attempt}:{name}". It is skipped when that key is
    already in state.applied. A "repeatable" transition also carries `content` (a sha256) in its key. It is
    skipped only when its key is applied AND `postcondition` holds, so a second, different receipt is still
    processed. fn(locked, state) writes its side files and returns the new state. The key is then appended
    to applied, the transition is logged, and the state is committed in one replace.
    """
    kind = TRANSITION_KINDS.get(name)
    if kind is None:
        raise ValueError(f"undeclared transition {name!r}")
    if kind == "once" and content is not None:
        raise ValueError(f"once-per-attempt transition {name!r} takes no content")
    if kind == "repeatable" and content is None:
        raise ValueError(f"repeatable transition {name!r} requires content")
    if kind == "repeatable" and postcondition is None:
        raise ValueError(f"repeatable transition {name!r} requires a postcondition")
    state = locked.require_state()
    if key.run_id != state.run_id:
        raise ValueError(f"transition key run_id {key.run_id!r} does not match the flow")
    canon = canonical_key(key, name, content)
    if canon in state.applied and (kind == "once" or (postcondition is not None and postcondition(state))):
        return
    new_state = fn(locked, state)
    _require_pending_transition(state, new_state)
    locked.log(name, step=key.step, attempt=key.attempt, key=canon)
    applied = new_state.applied if canon in new_state.applied else new_state.applied + (canon,)
    locked.commit(dataclasses.replace(new_state, applied=applied))


def init_state(locked: LockedFlow, spec: InitSpec) -> None:
    """Create state.json for a new flow: a fresh run_id, step worktree, status running, every map empty."""
    if locked.state is not None:
        raise RuntimeError("init_state: the flow already has state.json")
    run_id = secrets.token_hex(8)
    now = _utc_now()
    state = V2State(
        driver_format=DRIVER_FORMAT_V2,
        run_id=run_id,
        issue=spec.issue,
        repo=spec.repo,
        created=now,
        updated=now,
        step="worktree",
        status="running",
        live=None,
        pending_spawn=None,
        attempts={},
        failures={},
        stop_reason={},
        stop_source={},
        pr=None,
        worktree=spec.worktree,
        healed=False,
        assess=True,
        attempt_log={},
        applied=(),
        extra={},
    )
    _append_driver_log(
        locked.path,
        {"ts": now, "event": "init", "run_id": run_id, "step": None, "attempt": None, "issue": spec.issue, "repo": spec.repo},
    )
    locked.commit(state)


def _is_closed_repeatable(canon: str, run_id: str, step: Step, new_attempt: int) -> bool:
    """True for a repeatable key (run:step:attempt:name:content) of an earlier attempt of step."""
    parts = canon.split(":")
    if len(parts) < 5 or parts[0] != run_id or parts[1] != step:
        return False
    try:
        return int(parts[2]) < new_attempt
    except ValueError:
        return False


def record_spawn(locked: LockedFlow, step: Step, nonce: str) -> int:
    """
    Open a spawn for the next attempt of step. The counter and pending_spawn are written in the same replace,
    so after any spawn attempts[step] == pending_spawn.attempt. Returns the attempt number.
    """
    state = locked.require_state()
    attempt = state.attempts.get(step, 0) + 1
    key = TransitionKey(state.run_id, step, attempt)

    def _spawn(_: LockedFlow, current: V2State) -> V2State:
        if current.pending_spawn is not None:
            raise RuntimeError("record_spawn: a spawn is already pending for this flow")
        attempts = dict(current.attempts)
        attempts[step] = attempt
        pending = PendingSpawn(step=step, attempt=attempt, nonce=nonce, state="spawned", receipt_seen=None)
        # Repeatable keys (they carry a content sha) of attempts that closed are dead weight: drop them so
        # `applied` does not grow with every polled receipt (Layer 4 polling will add one per fingerprint).
        kept = tuple(k for k in current.applied if not _is_closed_repeatable(k, current.run_id, step, attempt))
        return dataclasses.replace(current, attempts=attempts, pending_spawn=pending, applied=kept)

    apply_transition(locked, key, "record_spawn", _spawn)
    return attempt


def record_cleared(locked: LockedFlow, key: TransitionKey) -> None:
    """
    Clear the pending spawn for key without a receipt, recording the attempt as cleared. A pending spawn
    that already landed keeps its receipt-landed outcome, which land recorded.
    """

    def _clear(_: LockedFlow, current: V2State) -> V2State:
        pending = current.pending_spawn
        if pending is None or (pending.step, pending.attempt) != (key.step, key.attempt):
            raise RuntimeError(f"record_cleared: no pending spawn for {key.step} attempt {key.attempt}")
        outcomes: Dict[Step, Dict[int, AttemptOutcome]] = {
            step: dict(per_step) for step, per_step in current.attempt_log.items()
        }
        if pending.state != "receipt-landed":
            outcomes.setdefault(key.step, {})[key.attempt] = "cleared"
        return dataclasses.replace(current, pending_spawn=None, attempt_log=outcomes)

    apply_transition(locked, key, "record_cleared", _clear)
