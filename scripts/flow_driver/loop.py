"""
The mutating v2 driver loop (Layer 2 Step 7; ADR-0026; sessions D3, D4, Q1 and Q4).

start, next, answer and clear-live each take the flow lock once, through state.mutating. Inside that hold one `next`:

  1. settles the pending receipt: lands a well-formed receipt, archives a stale one, stores a new fingerprint, or
     applies the D4 rerun gate to a malformed receipt (and the D5 cap);
  2. calls the pure steps.transition with the facts it gathered;
  3. commits the returned state (state replace first);
  4. writes the step prompt (SPAWN only) and the nonce-named directive file;
  5. returns the one `NEXT: <VERB> <file>` line that cli.py prints.

A crash after step 3 leaves state.json ahead of the files. The next call rewrites the same files from the committed
pending record, so the nonce, and therefore the file names, are the same (Q1 = A).

Python 3.8 floor: typing.Callable, Optional, Tuple only.
"""

import dataclasses
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from scripts.workflow.fsutil import atomic_write_text

from . import receipts
from .model import assert_never
from .recovery import Stop as RecoveryStop
from .recovery import malformed_rerun
from .state import DRIVER_LOG_FILE, LockedFlow, PendingSpawn, Refusal, TransitionKey, V2State, mutating, record_cleared
from .steps import (
    Facts,
    Notification,
    QuestionFact,
    ReceiptFact,
    SpawnDirective,
    answer_file_path,
    directive_file_path,
    directive_to_dict,
    event_from_label,
    question_file_path,
    step_prompt_path,
    transition,
)

# The D4 quiet interval: a malformed receipt is rerun only after this long since it was first seen, unless a
# notification says the step's subagent is gone. Measured across invocations from the injected clock.
RERUN_QUIET_SECONDS = 60
_TS = "%Y-%m-%dT%H:%M:%SZ"
# Clear-live is the human's way out of a wait-timeout or resume-failed STOP; these codes reopen the step.
_CLEARABLE_STOPS = ("wait-timeout", "resume-failed")

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(now: datetime) -> str:
    return now.strftime(_TS)


class LoopRefusal(Exception):
    """A refusal that is not a Refusal class: the command cannot act on the flow now. cli.py prints `refused: <msg>`."""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, text)


def _log_locked_refusal(flow: Path, label: str) -> None:
    """A locked refusal appends one line in a single write and never takes the lock (plan Step 7)."""
    if not flow.is_dir():
        return
    record = {
        "ts": _stamp(_utc_now()),
        "event": "locked-refusal",
        "run_id": None,
        "step": None,
        "attempt": None,
        "label": label,
    }
    with open(flow / DRIVER_LOG_FILE, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _archived_landed_failed(locked: LockedFlow, pending: PendingSpawn) -> bool:
    """A receipt-landed pending record: the archived landed receipt says whether the attempt FAILED."""
    archived = locked.path / "archive" / pending.step / f"a{pending.attempt}.landed.md"
    try:
        text = archived.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    receipt = receipts.parse_receipt(pending.step, text)
    return receipt is not None and receipt.decision == "FAILED"


def _quiet_elapsed(first_seen: str, now: datetime) -> bool:
    first = datetime.strptime(first_seen, _TS).replace(tzinfo=timezone.utc)
    return now - first >= timedelta(seconds=RERUN_QUIET_SECONDS)


def _malformed_gate(locked: LockedFlow, pending: PendingSpawn, notified: bool, now: datetime) -> ReceiptFact:
    """The D4 rerun gate and the D5 cap for a malformed receipt."""
    state = locked.require_state()
    if pending.receipt_first_seen is None:
        stamped = dataclasses.replace(pending, receipt_first_seen=_stamp(now))
        locked.commit(dataclasses.replace(state, pending_spawn=stamped))
        return "gate-hold"
    if not (notified or _quiet_elapsed(pending.receipt_first_seen, now)):
        return "gate-hold"
    previous = state.attempt_log.get(pending.step, {}).get(pending.attempt - 1)
    decision = malformed_rerun(pending.step, pending.attempt, previous == "receipt-malformed")
    if isinstance(decision, RecoveryStop):
        return "malformed-cap"
    receipts.rerun_malformed(locked, TransitionKey(state.run_id, pending.step, pending.attempt))
    return "rerun"


def _settle_receipt(locked: LockedFlow, notified: bool, now: datetime) -> ReceiptFact:
    """Act on the pending attempt's receipt under the lock. Returns the fact transition() reads."""
    state = locked.require_state()
    pending = state.pending_spawn
    if pending is None:
        return "none"
    if pending.state == "receipt-landed":
        return "failed" if _archived_landed_failed(locked, pending) else "none"
    key = TransitionKey(state.run_id, pending.step, pending.attempt)
    classified = receipts.classify(locked, pending)
    if isinstance(classified, receipts.Absent):
        return "none"
    if isinstance(classified, receipts.InProgress):
        if pending.receipt_seen != classified.fingerprint:
            receipts.store_fingerprint(locked, key, classified.fingerprint)
        return "none"
    if isinstance(classified, receipts.Landed):
        receipts.land(locked, key)
        return "failed" if classified.receipt.decision == "FAILED" else "none"
    if isinstance(classified, receipts.Stale):
        receipts.archive_stale(locked, key, classified.fingerprint.sha256)
        locked.log(
            "stale-nonce",
            step=pending.step,
            attempt=pending.attempt,
            nonce=pending.nonce,
            receipt_attempt=classified.receipt.attempt,
        )
        return "none"
    if isinstance(classified, receipts.Malformed):
        return _malformed_gate(locked, pending, notified, now)
    assert_never(classified)


def _question_fact(flow: Path, pending: Optional[PendingSpawn]) -> QuestionFact:
    if pending is None:
        return "none"
    if answer_file_path(flow, pending.step, pending.attempt).exists():
        return "answered"
    if question_file_path(flow, pending.step, pending.attempt).exists():
        return "awaiting"
    return "none"


def _changed(old: V2State, new: V2State) -> bool:
    """True when the transition changed anything other than the stamped and derived fields."""
    return dataclasses.replace(old, updated="", live=None) != dataclasses.replace(new, updated="", live=None)


def render_step_prompt(flow: Path, step: str, attempt: int, nonce: str) -> str:
    """
    The prompt for one SPAWN. It carries the FLOW_DIR, HUMAN_LEDGER and ATTEMPT lines the step needs for its receipt.
    Full step bodies are not wired in Layer 2 Step 7; this is the attempt header only.
    """
    return (
        f"FLOW_DIR={flow}\n"
        f"HUMAN_LEDGER={flow / 'human.jsonl'}\n"
        f"STEP={step}\n"
        f"ATTEMPT: {attempt}\n"
        f"NONCE={nonce}\n"
    )


def _step_once(locked: LockedFlow, event_label: Optional[str], flow: Path, now: datetime, headless: bool) -> str:
    event = event_from_label(event_label)
    notified = isinstance(event, Notification)
    nonce = secrets.token_hex(4)
    receipt_fact = _settle_receipt(locked, notified, now)
    state = locked.require_state()
    facts = Facts(
        flow=flow,
        nonce=nonce,
        receipt=receipt_fact,
        question=_question_fact(flow, state.pending_spawn),
        headless=headless,
    )
    new_state, directive = transition(state, event, facts)
    if _changed(state, new_state):
        locked.commit(new_state)  # the state replace is the first write of the commit order
    committed = locked.require_state()
    pending = committed.pending_spawn
    file_nonce = "none" if pending is None else pending.nonce
    attempt = committed.attempts.get(committed.step, 0) if pending is None else pending.attempt
    step = committed.step if pending is None else pending.step
    if isinstance(directive, SpawnDirective):
        _write(step_prompt_path(flow, step, attempt, file_nonce), render_step_prompt(flow, step, attempt, file_nonce))
    dfile = directive_file_path(flow, step, attempt, file_nonce)
    _write(dfile, json.dumps(directive_to_dict(directive), indent=2, sort_keys=True) + "\n")
    locked.log(directive.verb.lower(), step=step, attempt=attempt, nonce=file_nonce)
    return f"NEXT: {directive.verb} {dfile}"


def run_next(flow: Path, after: Optional[str], *, now: Clock = _utc_now, headless: bool = False) -> str:
    """
    One mutating `next`. after is the logged event label (None for a bare re-entry, `start` for the first call).
    Returns the NEXT line. Raises Refusal (state.Refusal, including `locked`) when the flow cannot be driven.
    """
    try:
        with mutating(flow) as locked:
            return _step_once(locked, after, flow, now(), headless)
    except Refusal as exc:
        if exc.refusal_class == "locked":
            _log_locked_refusal(flow, after or "")
        raise


def run_start(flow: Path, *, now: Clock = _utc_now) -> str:
    """The first `next` of a v2 flow, sent as the `start` label."""
    return run_next(flow, "start", now=now)


def run_answer(flow: Path, text: str) -> str:
    """Write the human's answer for the parked attempt. The next `next` resumes it. One answer per attempt."""
    with mutating(flow) as locked:
        state = locked.require_state()
        pending = state.pending_spawn
        if pending is None or pending.state != "parked-awaiting-answer":
            raise LoopRefusal("answer refused: the record is not parked-awaiting-answer")
        target = answer_file_path(flow, pending.step, pending.attempt)
        if target.exists():
            raise LoopRefusal(f"answer refused: {target} already exists")
        payload = {"attempt": pending.attempt, "step": pending.step, "text": text, "answered_utc": _stamp(_utc_now())}
        _write(target, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        locked.log("answer", step=pending.step, attempt=pending.attempt, nonce=pending.nonce)
        return f"answer recorded: {target}"


def run_clear_live(flow: Path, reason: str) -> str:
    """
    Clear a spawned or resumed record without a receipt (logged with its nonce). A wait-timeout or resume-failed STOP
    is reopened, so the next `next` re-spawns the step with a fresh nonce. A parked record is answered, never cleared.
    """
    with mutating(flow) as locked:
        state = locked.require_state()
        pending = state.pending_spawn
        if pending is None or pending.state not in ("spawned", "resumed"):
            raise LoopRefusal("clear-live refused: there is no live record to clear")
        record_cleared(locked, TransitionKey(state.run_id, pending.step, pending.attempt))
        state = locked.require_state()
        reasons = dict(state.stop_reason)
        code = reasons.get(state.step, "").partition(": ")[0]
        if state.status == "failed" and code in _CLEARABLE_STOPS:
            reasons.pop(state.step, None)
            locked.commit(dataclasses.replace(state, status="running", stop_reason=reasons))
        locked.log(
            "clear-live",
            step=pending.step,
            attempt=pending.attempt,
            nonce=pending.nonce,
            reason=reason,
            cleared_state=pending.state,
        )
        return f"cleared live record: attempt {pending.attempt}, nonce {pending.nonce}"
