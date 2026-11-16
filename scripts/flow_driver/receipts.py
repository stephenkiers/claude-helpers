"""
Receipt handling for the v2 driver (Layer 1 Step 5, ADR-0026 §6-§9).

Receipts are verified step outputs. This module handles their path, parsing (via flow-verify-receipt.py),
classification (into Absent, InProgress, Landed, Stale, Malformed), storage in archives, and the
verifier subprocess wrapper. The verifier is called only from mutating commands, requiring a LockedFlow.

Well-formedness (Step 5, refined Q2 = C):
  - exactly one line starts with ATTEMPT:, with value matching ^[1-9][0-9]*$
  - exactly one line matches ^Decision: (OK|FAILED)\\s*$
  - the text contains <!-- step-end -->
  - the verifier's _receipt_attempt returns the same integer and _receipt_attempt_error is False

Classification rules (two-read stability):
  - Absent: no file
  - Landed: well-formed, attempt == pending.attempt
  - Stale: well-formed, attempt != pending.attempt
  - InProgress: incomplete, fingerprint different from pending.receipt_seen (or none stored)
  - Malformed: incomplete, fingerprint stable across two invocations (== pending.receipt_seen)

Transitions via apply_transition:
  - land: once-per-attempt, moves receipt to archive/<step>/a<k>.landed.md
  - archive_stale: repeatable, moves to archive/<step>/a<k>.stale.<sha256[:12]>.md
  - rerun_malformed: once-per-attempt, moves to archive/<step>/a<k>.malformed.md, returns rerun decision
  - store_fingerprint: repeatable, stores receipt_seen in pending_spawn (run before archive_stale/land to detect InProgress vs Malformed)

Python 3.8 floor: no match statements, no f-strings with = debug syntax.
"""

import hashlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Literal, NamedTuple, Optional, Sequence

from .state import LockedFlow, PendingSpawn, ReceiptFingerprint, Step, TransitionKey, V2State, apply_transition


# Map step names to their zero-padded indices in the Step order
_STEP_INDICES = {
    "worktree": "00",
    "plan": "01",
    "track": "02",
    "implement": "03",
    "review": "04",
    "fix": "05",
    "verify": "06",
    "assess": "07",
    "ship": "08",
    "merge": "09",
}


def receipt_path(flow_dir: Path, step: Step) -> Path:
    """Path to the receipt for step: steps/<NN>-<step>.md."""
    idx = _STEP_INDICES.get(step, "XX")  # type: ignore[arg-type]
    return flow_dir / "steps" / f"{idx}-{step}.md"


def _get_receipt_attempt(text: str) -> Optional[int]:
    """Extract ATTEMPT from receipt (via shared parser). Returns None when absent or invalid."""
    # This wraps the verifier's _receipt_attempt function
    for line in text.split("\n"):
        if line.startswith("ATTEMPT:"):
            attempt_str = line.split(":", 1)[1].strip()
            try:
                parsed = int(attempt_str)
                return parsed if parsed >= 1 else None
            except ValueError:
                return None
    return None


def _receipt_attempt_error(text: str) -> bool:
    """True when receipt has ATTEMPT: line but it's not a valid integer >= 1."""
    has_line = any(ln.startswith("ATTEMPT:") for ln in text.split("\n"))
    return has_line and _get_receipt_attempt(text) is None


def _is_well_formed(text: str) -> bool:
    """Check if receipt meets all well-formedness criteria."""
    # Count ATTEMPT: lines
    attempt_lines = [ln for ln in text.split("\n") if ln.startswith("ATTEMPT:")]
    if len(attempt_lines) != 1:
        return False
    # Check the ATTEMPT value
    if _receipt_attempt_error(text):
        return False
    if _get_receipt_attempt(text) is None:
        return False
    # Check for exactly one Decision: line
    decision_lines = [ln for ln in text.split("\n") if re.match(r"^Decision: (OK|FAILED)\s*$", ln)]
    if len(decision_lines) != 1:
        return False
    # Check for step-end marker
    if "<!-- step-end -->" not in text:
        return False
    return True


class Classification(NamedTuple):
    """Result of classifying a receipt."""
    state: Literal["absent", "in-progress", "landed", "stale", "malformed"]
    attempt: Optional[int] = None
    fingerprint: Optional[ReceiptFingerprint] = None


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


def _compute_fingerprint(text: str) -> ReceiptFingerprint:
    """Compute SHA256 and size of receipt text."""
    return ReceiptFingerprint(
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        size=len(text),
    )


def classify(locked: LockedFlow, pending: PendingSpawn) -> Classification:
    """
    Classify a receipt based on well-formedness and attempt match.

    For InProgress vs Malformed, requires two-read stability: an incomplete receipt is InProgress
    if its fingerprint differs from pending.receipt_seen, or Malformed if it's the same.
    """
    path = receipt_path(locked.path, pending.step)

    # Absent
    if not path.exists():
        return Classification(state="absent")

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return Classification(state="absent")

    fingerprint = _compute_fingerprint(text)

    # Well-formed: Landed or Stale
    if _is_well_formed(text):
        attempt = _get_receipt_attempt(text)
        assert attempt is not None  # _is_well_formed guarantees this
        if attempt == pending.attempt:
            return Classification(state="landed", attempt=attempt, fingerprint=fingerprint)
        else:
            return Classification(state="stale", attempt=attempt, fingerprint=fingerprint)

    # Incomplete: InProgress or Malformed
    if pending.receipt_seen is None or fingerprint != pending.receipt_seen:
        # Fingerprint changed or not yet stored: InProgress
        return Classification(state="in-progress", fingerprint=fingerprint)
    else:
        # Fingerprint stable: Malformed
        return Classification(state="malformed", fingerprint=fingerprint)


def land(locked: LockedFlow, key: TransitionKey, step: Step, attempt: int) -> None:
    """
    Archive a well-formed landed receipt and record the attempt as receipt-landed.

    Transitions: pending_spawn.state -> receipt-landed, live -> None, attempt_log[attempt] = receipt-landed.
    """
    path = receipt_path(locked.path, step)
    archive_dir = locked.path / "archive" / step

    def _do_land(_: LockedFlow, state: V2State) -> V2State:
        # Move receipt to archive
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / f"a{attempt}.landed.md"
        if path.exists():
            path.rename(archive_path)

        # Update state: land the attempt, null pending_spawn and live
        import dataclasses
        outcomes = {s: dict(per_step) for s, per_step in state.attempt_log.items()}
        outcomes.setdefault(step, {})[attempt] = "receipt-landed"  # type: ignore[arg-type]
        new_state = dataclasses.replace(
            state,
            pending_spawn=None,
            attempt_log=outcomes,
        )
        return new_state

    apply_transition(locked, key, "land", _do_land)


def archive_stale(locked: LockedFlow, key: TransitionKey, step: Step, attempt: int, receipt_sha256: str) -> None:
    """
    Archive a well-formed stale receipt. Repeatable by sha256; pending_spawn stays pending.

    The receipt is moved to archive/<step>/a<attempt>.stale.<sha256[:12]>.md.
    pending_spawn and live are unchanged, so the current attempt remains awaited.
    """
    path = receipt_path(locked.path, step)
    archive_dir = locked.path / "archive" / step
    sha_short = receipt_sha256[:12]

    def _do_archive_stale(_: LockedFlow, state: V2State) -> V2State:
        # Move receipt to stale archive
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / f"a{attempt}.stale.{sha_short}.md"
        if path.exists():
            path.rename(archive_path)
        # State unchanged (pending_spawn and live stay as is)
        return state

    apply_transition(
        locked,
        key,
        "archive_stale",
        _do_archive_stale,
        content=receipt_sha256,
        postcondition=lambda s: not path.exists() or _compute_fingerprint(path.read_text(encoding="utf-8")).sha256 != receipt_sha256,
    )


def rerun_malformed(locked: LockedFlow, key: TransitionKey, step: Step, attempt: int) -> int:
    """
    Archive a malformed receipt and prepare for re-run.

    Transitions: pending_spawn -> None, live -> None, attempt_log[attempt] = receipt-malformed.
    Does not set status=failed or stop_reason/stop_source. Never refuses.
    Returns the next attempt number.
    """
    path = receipt_path(locked.path, step)
    archive_dir = locked.path / "archive" / step

    def _do_rerun_malformed(_: LockedFlow, state: V2State) -> V2State:
        # Move receipt to malformed archive
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / f"a{attempt}.malformed.md"
        if path.exists():
            path.rename(archive_path)

        # Update state: clear pending, record malformed outcome
        import dataclasses
        outcomes = {s: dict(per_step) for s, per_step in state.attempt_log.items()}
        outcomes.setdefault(step, {})[attempt] = "receipt-malformed"  # type: ignore[arg-type]
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


def store_fingerprint(locked: LockedFlow, key: TransitionKey, step: Step, attempt: int, fingerprint: ReceiptFingerprint) -> None:
    """
    Store the receipt fingerprint in pending_spawn.receipt_seen (repeatable transition).

    Used to detect two-read stability for InProgress/Malformed classification.
    """
    def _do_store_fingerprint(_: LockedFlow, state: V2State) -> V2State:
        import dataclasses
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
        postcondition=lambda s: s.pending_spawn is None or s.pending_spawn.receipt_seen != fingerprint,
    )


def rebuild_receipt(locked: LockedFlow, key: TransitionKey, step: Step, attempt: int, source: Optional[object], decision: str, body: str) -> Path:
    """
    Write a rebuilt receipt to archive (ADR-0026 §9 self-heal rule).

    Raises RebuildRefused if source is not None, source.decision == "FAILED" and decision == "OK".
    Writes archive/<step>/a<k>.rebuilt.md. Never overwrites the step receipt or archive. Logs a separate
    receipt-rebuilt event.
    """
    # Check the invariant: cannot turn FAILED into OK
    if source is not None:
        if hasattr(source, "decision") and source.decision == "FAILED" and decision == "OK":
            raise RebuildRefused("cannot rebuild FAILED receipt with OK decision")

    archive_dir = locked.path / "archive" / step
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"a{attempt}.rebuilt.md"

    # Build the rebuilt receipt text
    rebuilt_text = f"REBUILT: yes\nDecision: {decision}\n\n{body}\n"
    archive_path.write_text(rebuilt_text, encoding="utf-8")

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
        verifier = Path(__file__).parent.parent / "flow-verify-receipt.py"

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
