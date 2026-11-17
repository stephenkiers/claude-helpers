"""
Ledger adapter for the v2 /expert-flow driver (Layer 2 Step 5, ADR-0026 Decision 9, the Ledger block).

This module calls scripts/flow-ledger.py as a subprocess with a list argv, and never writes human.jsonl itself.
The flow lock is held by the caller, and flow-ledger.py takes its own sidecar lock inside each call.

  snapshot_at_spawn / ensure_snapshot: a per-step, per-attempt JSON snapshot of the ledger records up to the
    high-water seq at spawn (pending_spawn.ledger_seq). The snapshot holds only fields that flow-ledger never
    rewrites (seq, ts, step, attempt, kind, text, transcript, supersedes), so a missing snapshot re-renders to
    identical bytes even after a later mark. Status is read live, not from the snapshot.
  human_ledger_line: the `HUMAN_LEDGER:` line a prompt carries. The driver never inlines `<<<HUMAN_LEDGER`
    fences; routing those to Pass 2, the Amalgamator and the Triage Chief is the step's own job.
  carry_forward: on a landed OK receipt for plan, implement, review or fix, every pending direction in the
    snapshot must appear in the receipt's `Addressed:` line, else UncarriedDirection (STOP uncarried-direction).
  close_out_directions: at ship time, marks every still-pending direction addressed. A second run finds none
    pending and changes nothing, so human.jsonl stays byte-identical.

Every failure of flow-ledger.py (non-zero exit, start failure, unparseable output) is LedgerError, which the
caller maps to STOP ledger-error. Nothing raises out of the adapter.

Python 3.8 floor: typing.Optional/Union/Tuple/FrozenSet only, no match statements, no X | Y at runtime.
"""

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple, Union

from scripts.workflow import fsutil

from .preflight import CANNOT_START, Runner, run_command
from .state import Step

SNAPSHOT_DIR = "ledger-snapshots"
_ADDRESSED_LINE = re.compile(r"^Addressed:\s*(.*?)\s*$", re.MULTILINE)
_SEQ_TEXT = re.compile(r"[1-9][0-9]*")


def ledger_script() -> Path:
    """scripts/flow-ledger.py, beside this package's parent directory."""
    return Path(__file__).resolve().parent.parent / "flow-ledger.py"


@dataclass(frozen=True)
class LedgerRecord:
    """One human.jsonl record, parsed at the edge. Every field is validated before it reaches the driver."""

    seq: int
    ts: str
    step: str
    attempt: int
    kind: str
    text: str
    transcript: Optional[str]
    status: str
    addressed_by: Optional[str]
    supersedes: Optional[int]


@dataclass(frozen=True)
class LedgerError:
    """flow-ledger.py failed or printed something unparseable. The caller maps this to STOP ledger-error."""

    detail: str


@dataclass(frozen=True)
class Snapshot:
    """The per-attempt snapshot file, and the high-water seq it was rendered up to."""

    path: Path
    ledger_seq: int


@dataclass(frozen=True)
class CarryOk:
    """Every pending direction at or below the snapshot seq is named in the receipt's Addressed line."""


@dataclass(frozen=True)
class UncarriedDirection:
    """Pending direction seqs the receipt's Addressed line does not name. The caller maps this to STOP uncarried-direction."""

    seqs: Tuple[int, ...]


@dataclass(frozen=True)
class CloseOut:
    """The seqs this close-out marked addressed. Empty on a repeat run."""

    marked: Tuple[int, ...]


LedgerResult = Union[LedgerError, Snapshot]


class _Malformed(Exception):
    """One record field has the wrong shape. Converted to LedgerError at the parse boundary."""


def _as_int(obj: Dict[str, object], key: str) -> int:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _Malformed(f"record field {key} is not an integer")
    return value


def _as_text(obj: Dict[str, object], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise _Malformed(f"record field {key} is not a string")
    return value


def _as_opt_text(obj: Dict[str, object], key: str) -> Optional[str]:
    value = obj.get(key)
    if value is None or isinstance(value, str):
        return value
    raise _Malformed(f"record field {key} is neither a string nor null")


def _as_opt_int(obj: Dict[str, object], key: str) -> Optional[int]:
    value = obj.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _Malformed(f"record field {key} is neither an integer nor null")
    return value


def _parse_record(item: object) -> LedgerRecord:
    if not isinstance(item, dict):
        raise _Malformed("a ledger record is not a JSON object")
    obj: Dict[str, object] = item
    return LedgerRecord(
        seq=_as_int(obj, "seq"),
        ts=_as_text(obj, "ts"),
        step=_as_text(obj, "step"),
        attempt=_as_int(obj, "attempt"),
        kind=_as_text(obj, "kind"),
        text=_as_text(obj, "text"),
        transcript=_as_opt_text(obj, "transcript"),
        status=_as_text(obj, "status"),
        addressed_by=_as_opt_text(obj, "addressed_by"),
        supersedes=_as_opt_int(obj, "supersedes"),
    )


def _parse_records(stdout: str) -> Union[List[LedgerRecord], LedgerError]:
    try:
        raw: object = json.loads(stdout)
    except ValueError:
        return LedgerError("flow-ledger.py list did not print JSON")
    if not isinstance(raw, list):
        return LedgerError("flow-ledger.py list did not print a JSON array")
    try:
        return [_parse_record(item) for item in raw]
    except _Malformed as exc:
        return LedgerError(f"flow-ledger.py list: {exc}")


def _first_line(text: str) -> str:
    stripped = text.strip()
    return stripped.splitlines()[0] if stripped else ""


def _run_ledger(run: Runner, verb: str, flow_dir: Path, extra: Sequence[str]) -> Union[str, LedgerError]:
    """Run one flow-ledger.py subcommand with a list argv and return its stdout, or a LedgerError."""
    argv = [sys.executable, str(ledger_script()), verb, "--flow-dir", str(flow_dir), *extra]
    result = run(argv)
    if result.returncode != 0:
        detail = _first_line(result.stderr) or f"exit {result.returncode}"
        if result.returncode == CANNOT_START:
            return LedgerError(f"flow-ledger.py {verb}: {detail}")
        return LedgerError(f"flow-ledger.py {verb} exited {result.returncode}: {detail}")
    return result.stdout


def list_records(
    flow_dir: Path,
    run: Runner = run_command,
    status: Optional[str] = None,
    kind: Optional[str] = None,
) -> Union[List[LedgerRecord], LedgerError]:
    """All records of the flow's ledger, optionally filtered by status and kind. Read-only."""
    extra: List[str] = ["--json"]
    if status is not None:
        extra.extend(["--status", status])
    if kind is not None:
        extra.extend(["--kind", kind])
    stdout = _run_ledger(run, "list", flow_dir, extra)
    if isinstance(stdout, LedgerError):
        return stdout
    return _parse_records(stdout)


def snapshot_path(flow_dir: Path, step: Step, attempt: int) -> Path:
    """The snapshot file for one attempt of one step: ledger-snapshots/<step>-a<attempt>.json in the flow dir."""
    return flow_dir / SNAPSHOT_DIR / f"{step}-a{attempt}.json"


def render_snapshot(records: Sequence[LedgerRecord], ledger_seq: int) -> str:
    """
    The snapshot bytes: the records with seq <= ledger_seq, as a sorted-key JSON array of immutable fields.

    Deterministic: the same records and seq always give the same bytes, so a re-render matches the original.
    """
    rows: List[Dict[str, object]] = []
    for record in records:
        if record.seq > ledger_seq:
            continue
        row: Dict[str, object] = {
            "seq": record.seq,
            "ts": record.ts,
            "step": record.step,
            "attempt": record.attempt,
            "kind": record.kind,
            "text": record.text,
            "transcript": record.transcript,
            "supersedes": record.supersedes,
        }
        rows.append(row)
    return json.dumps(rows, indent=2, sort_keys=True) + "\n"


def _write_snapshot(path: Path, text: str) -> Optional[LedgerError]:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fsutil.atomic_write_text(path, text, fsync=True)
    except OSError as exc:
        return LedgerError(f"could not write ledger snapshot {path.name} ({exc.__class__.__name__})")
    return None


def snapshot_at_spawn(flow_dir: Path, step: Step, attempt: int, run: Runner = run_command) -> LedgerResult:
    """
    Write the snapshot for a new spawn. The high-water seq is the largest seq the ledger holds now (0 when empty),
    and it is recorded as pending_spawn.ledger_seq by the caller in the same state replace.
    """
    records = list_records(flow_dir, run)
    if isinstance(records, LedgerError):
        return records
    ledger_seq = max((record.seq for record in records), default=0)
    path = snapshot_path(flow_dir, step, attempt)
    error = _write_snapshot(path, render_snapshot(records, ledger_seq))
    if error is not None:
        return error
    return Snapshot(path=path, ledger_seq=ledger_seq)


def ensure_snapshot(
    flow_dir: Path,
    step: Step,
    attempt: int,
    ledger_seq: int,
    run: Runner = run_command,
) -> LedgerResult:
    """
    Return the attempt's snapshot, re-rendering it from ledger_seq when the file is missing (never a stop).

    An existing file is returned as it is. A missing one is rendered from the same seq, which gives identical bytes.
    """
    path = snapshot_path(flow_dir, step, attempt)
    if path.is_file():
        return Snapshot(path=path, ledger_seq=ledger_seq)
    records = list_records(flow_dir, run)
    if isinstance(records, LedgerError):
        return records
    error = _write_snapshot(path, render_snapshot(records, ledger_seq))
    if error is not None:
        return error
    return Snapshot(path=path, ledger_seq=ledger_seq)


def human_ledger_line(snapshot: Snapshot) -> str:
    """The `HUMAN_LEDGER:` line a step prompt carries. It names the snapshot and never inlines the records."""
    return f"HUMAN_LEDGER: {snapshot.path}"


def addressed_seqs(receipt_text: str) -> FrozenSet[int]:
    """
    The seqs on a receipt's `Addressed:` line. `Addressed: none` and an absent or malformed line name nothing,
    so every pending direction is then uncarried (the verifier already rejects a malformed line).
    """
    match = _ADDRESSED_LINE.search(receipt_text)
    if match is None:
        return frozenset()
    body = match.group(1)
    if body == "none":
        return frozenset()
    parts = [part.strip() for part in body.split(",")]
    if any(_SEQ_TEXT.fullmatch(part) is None for part in parts):
        return frozenset()
    return frozenset(int(part) for part in parts)


def carry_forward(
    flow_dir: Path,
    ledger_seq: int,
    receipt_text: str,
    run: Runner = run_command,
) -> Union[CarryOk, UncarriedDirection, LedgerError]:
    """
    Check the Addressed carry-forward for one landed OK receipt (plan, implement, review or fix).

    The directions in scope are the pending direction records with seq <= ledger_seq (the snapshot's high-water).
    A direction addressed earlier is no longer pending, so it is not in scope.
    """
    pending = list_records(flow_dir, run, status="pending", kind="direction")
    if isinstance(pending, LedgerError):
        return pending
    in_scope = sorted(record.seq for record in pending if record.seq <= ledger_seq)
    carried = addressed_seqs(receipt_text)
    uncarried = tuple(seq for seq in in_scope if seq not in carried)
    if uncarried:
        return UncarriedDirection(seqs=uncarried)
    return CarryOk()


def _relative_artifact(by: str) -> bool:
    if not by or by.startswith("/"):
        return False
    return ".." not in by.split("/")


def close_out_directions(flow_dir: Path, by: str, run: Runner = run_command) -> Union[CloseOut, LedgerError]:
    """
    Ship-time close-out: mark every still-pending direction addressed, citing `by` (a relative artifact path).

    Idempotent. It reads the pending directions first and marks only those, so a second run marks nothing and
    writes nothing. A failed mark stops the close-out with a LedgerError.
    """
    if not _relative_artifact(by):
        return LedgerError("close-out artifact path must be relative with no ..")
    pending = list_records(flow_dir, run, status="pending", kind="direction")
    if isinstance(pending, LedgerError):
        return pending
    marked: List[int] = []
    for seq in sorted(record.seq for record in pending):
        result = _run_ledger(run, "mark", flow_dir, ["--seq", str(seq), "--status", "addressed", "--by", by])
        if isinstance(result, LedgerError):
            return result
        marked.append(seq)
    return CloseOut(marked=tuple(marked))
