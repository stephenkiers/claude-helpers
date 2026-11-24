"""
Receipt handling for the v2 driver (Layer 1 Step 5, ADR-0026 §6-§9).

Receipts are verified step outputs. This module handles their path, parsing (via flow-verify-receipt.py),
classification (into Absent, InProgress, Landed, Stale, Malformed), storage in archives, and the
verifier subprocess wrapper. The verifier is called only from mutating commands, requiring a LockedFlow.

Well-formedness (Step 5, refined Q2 = C):
  - exactly one line starts with ATTEMPT:, and its value after strip() matches ^[1-9][0-9]*$
  - exactly one line matches ^Decision: (OK|FAILED)\\s*$
  - the text contains <!-- step-end -->
  - the verifier's _receipt_attempt returns the same integer and _receipt_attempt_error is False

Classification rules (two-read stability):
  - Absent: no file
  - Landed: well-formed, attempt == pending.attempt
  - Stale: well-formed, attempt != pending.attempt
  - InProgress: incomplete receipt on a parked or resumed record (basis "awaiting-answer"), or incomplete with
    fingerprint different from pending.receipt_seen (basis "fingerprint-changed")
  - Malformed: incomplete receipt on a spawned record with fingerprint stable across two invocations (== pending.receipt_seen)

Transitions via apply_transition:
  - land: once-per-attempt, moves receipt to archive/<step>/a<k>.landed.md, pending -> receipt-landed
  - archive_stale: repeatable, moves to archive/<step>/a<k>.stale.<sha256[:12]>.md
  - rerun_malformed: once-per-attempt, moves to archive/<step>/a<k>.malformed.md, returns rerun decision
  - store_fingerprint: repeatable, stores receipt_seen in pending_spawn (run before archive_stale/land to detect InProgress vs Malformed)
  - settle_in_progress: routes InProgress receipts to store_fingerprint only when basis is "fingerprint-changed"

Python 3.8 floor: no match statements, no f-strings with = debug syntax.
"""

import dataclasses
import hashlib
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Callable, Dict, List, Literal, NamedTuple, Optional, Protocol, cast, Sequence, Tuple, Union

from .state import STEPS, LockedFlow, PendingSpawn, ReceiptFingerprint, Step, TransitionKey, V2State, apply_transition


# Receipt file prefixes derive from the one step order (state.STEPS), so they cannot drift from it.
_STEP_INDICES: Dict[Step, str] = {step: f"{i:02d}" for i, step in enumerate(STEPS)}

# The strict ATTEMPT value: a positive integer with no leading zero (Q2 = C).
_ATTEMPT_VALUE = re.compile(r"[1-9][0-9]*")
_DECISION_LINE = re.compile(r"^Decision: (OK|FAILED)\s*$")
_STEP_END = "<!-- step-end -->"
_VERIFIER_PATH = Path(__file__).parent.parent / "flow-verify-receipt.py"


class _VerifierModule(Protocol):
    """The two helpers of flow-verify-receipt.py this module calls (loaded by path, so typed here)."""

    def _receipt_attempt(self, text: str) -> Optional[int]: ...

    def _receipt_attempt_error(self, text: str) -> bool: ...


_verifier_module: Optional[ModuleType] = None


class Receipt(NamedTuple):
    """A parsed, well-formed step receipt."""
    step: Step
    attempt: int
    decision: Literal["OK", "FAILED"]
    rebuilt: bool


def receipt_path(flow_dir: Path, step: Step) -> Path:
    """Path to the receipt for step: steps/<NN>-<step>.md."""
    idx = _STEP_INDICES.get(step)
    if idx is None:
        raise ValueError(f"unknown step: {step!r}")
    return flow_dir / "steps" / f"{idx}-{step}.md"


def _load_verifier() -> _VerifierModule:
    """Load flow-verify-receipt.py once; it is the authoritative ATTEMPT parser (one parser, Step 5)."""
    global _verifier_module
    if _verifier_module is None:
        spec = importlib.util.spec_from_file_location("flow_verify_receipt", _VERIFIER_PATH)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load verifier from {_VERIFIER_PATH}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _verifier_module = module
    return cast(_VerifierModule, _verifier_module)


def _attempt_lines(text: str) -> List[str]:
    return [ln for ln in text.split("\n") if ln.startswith("ATTEMPT:")]


def _get_receipt_attempt(text: str) -> Optional[int]:
    """
    The strict ATTEMPT value: exactly one ATTEMPT: line whose value after strip() matches ^[1-9][0-9]*$.

    Returns None when absent, duplicated, non-numeric, zero or leading-zero. Leading zeros are rejected here
    even though the verifier accepts them, so "ATTEMPT: 02" is incomplete and re-runs (Q2 = C).
    """
    lines = _attempt_lines(text)
    if len(lines) != 1:
        return None
    value = lines[0].split(":", 1)[1].strip()
    if not _ATTEMPT_VALUE.fullmatch(value):
        return None
    return int(value)


def receipt_attempt(text: str) -> Optional[int]:
    """Public form of the strict ATTEMPT parse (None when absent, duplicated or not [1-9][0-9]*)."""
    return _get_receipt_attempt(text)


def _receipt_attempt_error(text: str) -> bool:
    """True when receipt has ATTEMPT: line but it's not a valid integer >= 1 (verifier-compatible)."""
    verifier = _load_verifier()
    return verifier._receipt_attempt_error(text)


def parse_receipt(step: Step, text: str) -> Optional[Receipt]:
    """
    Parse a receipt into a Receipt when it is well-formed; None when it is incomplete (Q2 = C).

    The verifier is the authoritative parser: its _receipt_attempt must return the same integer as the strict
    driver parse, and its _receipt_attempt_error must be False.
    """
    attempt = _get_receipt_attempt(text)
    if attempt is None:
        return None
    if _receipt_attempt_error(text):
        return None
    if _load_verifier()._receipt_attempt(text) != attempt:
        return None
    decisions = [m.group(1) for m in (_DECISION_LINE.match(ln) for ln in text.split("\n")) if m is not None]
    if len(decisions) != 1:
        return None
    if _STEP_END not in text:
        return None
    decision: Literal["OK", "FAILED"] = "OK" if decisions[0] == "OK" else "FAILED"
    rebuilt = any(ln.strip() == "REBUILT: yes" for ln in text.split("\n"))
    return Receipt(step=step, attempt=attempt, decision=decision, rebuilt=rebuilt)


TEST_PATH_CODES: Tuple[str, ...] = (
    "plan-test-paths",
    "test-path-in-units",
    "test-only-plan",  # legacy: never emitted since #526; kept so archived receipts classify
    "existing-test-edit",
    "planned-change-breaks-test",
    "zero-unit-plan-incomplete",
)


def failed_test_path_stop(text: str) -> Optional[Tuple[str, str]]:
    """
    The (code, detail) of a FAILED receipt whose first `Reason:` line starts with one of the six test-path codes
    (`<code>:` or `<code> `), or None. The detail is the rest of the reason line. A step receipt is data: only the
    closed code set is ever returned, never receipt text as a code.
    """
    for line in text.split("\n"):
        if not line.startswith("Reason:"):
            continue
        reason = line[len("Reason:"):].strip()
        for code in TEST_PATH_CODES:
            if reason == code or reason.startswith(code + ":") or reason.startswith(code + " "):
                return code, reason[len(code):].lstrip(": ").strip() or "(no detail)"
        return None
    return None


class Absent(NamedTuple):
    """No receipt file."""
    state: Literal["absent"] = "absent"


class InProgress(NamedTuple):
    """Incomplete receipt: either awaiting-answer on a parked or resumed record, or fingerprint changed on a spawned record."""
    fingerprint: ReceiptFingerprint
    basis: Literal["fingerprint-changed", "awaiting-answer"]
    state: Literal["in-progress"] = "in-progress"


class Landed(NamedTuple):
    """Well-formed receipt for the pending attempt."""
    receipt: Receipt
    fingerprint: ReceiptFingerprint
    state: Literal["landed"] = "landed"

    @property
    def attempt(self) -> int:
        return self.receipt.attempt


class Stale(NamedTuple):
    """Well-formed receipt for a different attempt."""
    receipt: Receipt
    fingerprint: ReceiptFingerprint
    state: Literal["stale"] = "stale"

    @property
    def attempt(self) -> int:
        return self.receipt.attempt


class Malformed(NamedTuple):
    """Incomplete receipt whose fingerprint was stable across two reads."""
    fingerprint: ReceiptFingerprint
    state: Literal["malformed"] = "malformed"


Classification = Union[Absent, InProgress, Landed, Stale, Malformed]


class RebuildRefused(Exception):
    """Cannot rebuild receipt: source is FAILED and new decision is OK."""
    pass


class VerifierTimeout(Exception):
    """Verifier subprocess exceeded its timeout."""
    pass


class VerifierResult(NamedTuple):
    """Result of running the verifier."""
    returncode: int
    stdout: str
    stderr: str


def _compute_fingerprint(data: Union[bytes, str]) -> ReceiptFingerprint:  # noqa: UP037
    """Compute SHA256 and byte size of the raw receipt content (str is encoded as UTF-8)."""
    raw = data if isinstance(data, bytes) else data.encode("utf-8")
    return ReceiptFingerprint(sha256=hashlib.sha256(raw).hexdigest(), size=len(raw))


class ReceiptChanged(Exception):
    """The receipt changed between classification and archiving; the move was undone."""


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _archive_move(path: Path, dest: Path, accept: Callable[[bytes], bool]) -> None:
    """
    Move the step receipt to dest, then re-read the captured file and check accept(bytes).

    A receipt rewritten between classify and the move is not archived under the wrong name: on mismatch the
    file is put back (or, when the step path was re-created meanwhile, parked as a<k>.changed.<sha12>.md)
    and ReceiptChanged is raised. A missing receipt is a no-op (the move already happened on a crashed run).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return
    path.rename(dest)
    data = dest.read_bytes()
    if not accept(data):
        sha12 = hashlib.sha256(data).hexdigest()[:12]
        if path.exists():
            parked = dest.with_name(dest.name.rsplit(".", 2)[0] + f".changed.{sha12}.md")
            dest.rename(parked)
        else:
            dest.rename(path)
        raise ReceiptChanged(f"receipt changed while archiving to {dest.name}")
    _fsync_dir(dest.parent)
    _fsync_dir(path.parent)


def classify(locked: LockedFlow, pending: PendingSpawn) -> Classification:
    """
    Classify a receipt based on well-formedness and attempt match.

    For InProgress vs Malformed on spawned records, requires two-read stability: an incomplete receipt is
    InProgress if its fingerprint differs from pending.receipt_seen, or Malformed if it's the same.

    For parked or resumed records, an incomplete receipt is always InProgress (basis "awaiting-answer"),
    never Malformed: it is held and never re-run, even if its fingerprint appears stable. The fingerprint
    is not stored for such receipts.

    Cadence assumption (Layer 2 must honour it): Layer 2's quiet interval and the D4 subagent_gone gate
    apply to spawned records with Malformed receipts. The D4 gate is wired for `next` in loop._malformed_gate
    but does not apply to parked or resumed records. The `resume` command still reruns a spawned record's
    stable incomplete receipt (rule-1-live-step, no D4 gate and no D5 cap; ADR-0026 2026-11-16).
    """
    path = receipt_path(locked.path, pending.step)

    # Absent only when the file is genuinely missing; any other I/O error propagates (it is not "no receipt")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return Absent()

    # Undecodable bytes are replaced, so a corrupt receipt classifies (malformed) instead of vanishing
    text = raw.decode("utf-8", errors="replace")
    fingerprint = _compute_fingerprint(raw)

    # Well-formed: Landed or Stale
    receipt = parse_receipt(pending.step, text)
    if receipt is not None:
        if receipt.attempt == pending.attempt:
            return Landed(receipt=receipt, fingerprint=fingerprint)
        else:
            return Stale(receipt=receipt, fingerprint=fingerprint)

    # Incomplete: check for awaiting-answer state first
    if pending.state in ("parked-awaiting-answer", "resumed"):
        # Incomplete receipt on a parked or resumed record: always InProgress, never Malformed
        return InProgress(fingerprint=fingerprint, basis="awaiting-answer")

    # Incomplete on spawned record: InProgress or Malformed via two-read stability
    if pending.receipt_seen is None or fingerprint != pending.receipt_seen:
        # Fingerprint changed or not yet stored: InProgress
        return InProgress(fingerprint=fingerprint, basis="fingerprint-changed")
    else:
        # Fingerprint stable: Malformed
        return Malformed(fingerprint=fingerprint)


def _is_landed(step: Step, attempt: int, data: bytes) -> bool:
    receipt = parse_receipt(step, data.decode("utf-8", errors="replace"))
    return receipt is not None and receipt.attempt == attempt


def _receipt_sha_is_not(path: Path, sha256: str) -> bool:
    try:
        return _compute_fingerprint(path.read_bytes()).sha256 != sha256
    except FileNotFoundError:
        return True


def roll_forward(locked: LockedFlow, key: TransitionKey, pending: PendingSpawn) -> Optional[Literal["receipt-landed", "rerun"]]:
    """
    Finish an archive move whose state commit never happened (a crash between rename and commit).

    Only acts when the step receipt is absent and an archive for this attempt exists: a landed archive that
    parses as landed re-runs land; a malformed archive re-runs rerun_malformed. Returns None when nothing to do.
    """
    if receipt_path(locked.path, pending.step).exists():
        return None
    archive_dir = locked.path / "archive" / pending.step
    landed = archive_dir / f"a{pending.attempt}.landed.md"
    malformed = archive_dir / f"a{pending.attempt}.malformed.md"
    if landed.exists() and _is_landed(pending.step, pending.attempt, landed.read_bytes()):
        land(locked, key)
        return "receipt-landed"
    if malformed.exists():
        rerun_malformed(locked, key)
        return "rerun"
    return None


def land(locked: LockedFlow, key: TransitionKey) -> None:
    """
    Archive a well-formed landed receipt and record the attempt as receipt-landed.

    Transitions: pending_spawn.state -> receipt-landed (live is projected to null by commit),
    attempt_log[attempt] = receipt-landed. pending_spawn itself is kept, so the landing is visible.
    """
    step, attempt = key.step, key.attempt
    path = receipt_path(locked.path, step)
    archive_dir = locked.path / "archive" / step

    def _do_land(_: LockedFlow, state: V2State) -> V2State:
        pending = state.pending_spawn
        if pending is None or (pending.step, pending.attempt) != (step, attempt):
            raise RuntimeError(f"land: no pending spawn for {step} attempt {attempt}")

        # Move receipt to archive
        archive_path = archive_dir / f"a{attempt}.landed.md"
        _archive_move(path, archive_path, lambda b: _is_landed(step, attempt, b))

        # Update state: the pending spawn is receipt-landed (live null via the D1 projection)
        outcomes = {s: dict(per_step) for s, per_step in state.attempt_log.items()}
        outcomes.setdefault(step, {})[attempt] = "receipt-landed"
        new_state = dataclasses.replace(
            state,
            pending_spawn=dataclasses.replace(pending, state="receipt-landed"),
            attempt_log=outcomes,
        )
        return new_state

    apply_transition(locked, key, "land", _do_land)


def archive_stale(locked: LockedFlow, key: TransitionKey, receipt_sha256: str) -> None:
    """
    Archive a well-formed stale receipt. Repeatable by sha256; pending_spawn stays pending.

    The receipt is moved to archive/<step>/a<attempt>.stale.<sha256[:12]>.md.
    pending_spawn and live are unchanged, so the current attempt remains awaited.
    """
    step, attempt = key.step, key.attempt
    path = receipt_path(locked.path, step)
    archive_dir = locked.path / "archive" / step
    sha_short = receipt_sha256[:12]

    def _do_archive_stale(_: LockedFlow, state: V2State) -> V2State:
        # Move receipt to stale archive
        archive_path = archive_dir / f"a{attempt}.stale.{sha_short}.md"
        _archive_move(path, archive_path, lambda b: hashlib.sha256(b).hexdigest() == receipt_sha256)
        # State unchanged (pending_spawn and live stay as is)
        return state

    apply_transition(
        locked,
        key,
        "archive_stale",
        _do_archive_stale,
        content=receipt_sha256,
        postcondition=lambda s: _receipt_sha_is_not(path, receipt_sha256),
    )


def rerun_malformed(locked: LockedFlow, key: TransitionKey) -> int:
    """
    Archive a malformed receipt and prepare for re-run.

    Transitions: pending_spawn -> None, live -> None, attempt_log[attempt] = receipt-malformed.
    Does not set status=failed or stop_reason/stop_source. Never refuses.
    Returns the next attempt number.
    """
    step, attempt = key.step, key.attempt
    path = receipt_path(locked.path, step)
    archive_dir = locked.path / "archive" / step

    def _do_rerun_malformed(_: LockedFlow, state: V2State) -> V2State:
        # Move receipt to malformed archive
        archive_path = archive_dir / f"a{attempt}.malformed.md"
        _archive_move(path, archive_path, lambda b: parse_receipt(step, b.decode("utf-8", errors="replace")) is None)

        # Update state: clear pending, record malformed outcome
        outcomes = {s: dict(per_step) for s, per_step in state.attempt_log.items()}
        outcomes.setdefault(step, {})[attempt] = "receipt-malformed"
        new_state = dataclasses.replace(
            state,
            pending_spawn=None,
            attempt_log=outcomes,
        )
        return new_state

    apply_transition(locked, key, "rerun_malformed", _do_rerun_malformed)
    next_attempt = attempt + 1
    locked.log("receipt-malformed-rerun", step=key.step, attempt=key.attempt, next_attempt=next_attempt)
    return next_attempt


def store_fingerprint(locked: LockedFlow, key: TransitionKey, fingerprint: ReceiptFingerprint) -> None:
    """
    Store the receipt fingerprint in pending_spawn.receipt_seen (repeatable transition).

    Used to detect two-read stability for InProgress/Malformed classification.
    """
    step, attempt = key.step, key.attempt

    def _do_store_fingerprint(_: LockedFlow, state: V2State) -> V2State:
        if state.pending_spawn is None or state.pending_spawn.step != step or state.pending_spawn.attempt != attempt:
            raise RuntimeError(f"store_fingerprint: no matching pending spawn for {key.step} attempt {key.attempt}")

        new_pending = dataclasses.replace(
            state.pending_spawn,
            receipt_seen=fingerprint,
        )
        return dataclasses.replace(state, pending_spawn=new_pending)

    apply_transition(
        locked,
        key,
        "store_fingerprint",
        _do_store_fingerprint,
        content=fingerprint.sha256,
        postcondition=lambda s: s.pending_spawn is not None and s.pending_spawn.receipt_seen == fingerprint,
    )


def settle_in_progress(locked: LockedFlow, key: TransitionKey, pending: PendingSpawn, classified: InProgress) -> None:
    """
    Route an InProgress receipt: store the fingerprint only when basis is "fingerprint-changed" and the value differs.

    For "awaiting-answer", do nothing: the receipt is held and no fingerprint is stored. This helper enforces that
    the two consumers (loop._settle_receipt and resume._rule_1_action) cannot diverge on fingerprint storage.
    """
    if classified.basis == "fingerprint-changed":
        if pending.receipt_seen != classified.fingerprint:
            store_fingerprint(locked, key, classified.fingerprint)


def rebuild_receipt(
    locked: LockedFlow,
    key: TransitionKey,
    source: Optional[Receipt],
    decision: Literal["OK", "FAILED"],
    body: str,
) -> Path:
    """
    Write a rebuilt receipt to archive (ADR-0026 §9 self-heal rule).

    step and attempt come from key (TransitionKey(run_id, step, attempt)). Raises RebuildRefused if source is
    not None, source.decision == "FAILED" and decision == "OK". Writes archive/<step>/a<k>.rebuilt.md, never
    the step receipt and never an existing archive name. Logs a separate receipt-rebuilt event.
    """
    # Check the invariant: cannot turn FAILED into OK
    if source is not None and source.decision == "FAILED" and decision == "OK":
        raise RebuildRefused("cannot rebuild FAILED receipt with OK decision")

    archive_dir = locked.path / "archive" / key.step
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"a{key.attempt}.rebuilt.md"
    if source is not None and (source.step, source.attempt) != (key.step, key.attempt):
        raise RebuildRefused("source receipt does not belong to this transition key")

    # Build the rebuilt receipt text; exclusive create so an existing archive is never overwritten
    rebuilt_text = f"REBUILT: yes\nDecision: {decision}\n\n{body}\n"
    with open(archive_path, "x", encoding="utf-8") as fh:
        fh.write(rebuilt_text)

    locked.log("receipt-rebuilt", step=key.step, attempt=key.attempt)
    return archive_path


def run_verifier(locked: LockedFlow, args: Sequence[str], *, timeout_s: float = 120.0, verifier: Optional[Path] = None) -> VerifierResult:
    """
    Run flow-verify-receipt.py as a subprocess (the only path to the verifier).

    Requires a LockedFlow to enforce that only mutating commands can invoke the verifier.
    Runs with close_fds=True, never pass_fds. TimeoutExpired raises VerifierTimeout (recoverable).

    The timeout policy: a single timeout is recoverable (mutating re-enters and retries);
    a second consecutive timeout in the same invocation is refused.
    """
    if verifier is None:
        verifier = _VERIFIER_PATH

    try:
        result = subprocess.run(
            [sys.executable, str(verifier), *args],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            close_fds=True,
        )
        return VerifierResult(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)
    except subprocess.TimeoutExpired as exc:
        raise VerifierTimeout(f"verifier exceeded {timeout_s}s") from exc
