#!/usr/bin/env python3
"""
Append-only human-input ledger for /expert-flow.

Manages a per-flow ledger file (human.jsonl) that records all human inputs
(answers, directions, corrections) alongside metadata (step, attempt, timestamp).
Uses file-level locking (sidecar lock file) for concurrent access safety.

Record keys (in order): seq, ts, step, attempt, kind, text, transcript, status,
addressed_by, supersedes.

Exit codes:
  0: success
  1: data/contract violation (message on stderr, file unchanged)
  2: argparse usage error
"""

import argparse
import fcntl
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypedDict, Optional, List, Dict, Any, NoReturn, Tuple, get_args

# Type definitions
Kind = Literal["transcript", "answer", "direction", "question", "parked", "gate_note", "correction"]
Status = Literal["pending", "addressed", "parked", "superseded"]

# Derived from the Literal types so the argparse choices cannot drift from the model.
KINDS: Tuple[Kind, ...] = get_args(Kind)
STATUSES: Tuple[Status, ...] = get_args(Status)


class Record(TypedDict):
    """Human-input ledger record with all required keys."""
    seq: int
    ts: str
    step: str
    attempt: int
    kind: Kind
    text: str
    transcript: Optional[str]
    status: Status
    addressed_by: Optional[str]
    supersedes: Optional[int]


def error(msg: str) -> NoReturn:
    """Print error to stderr and exit with code 1."""
    print(msg, file=sys.stderr)
    sys.exit(1)


def usage_error(msg: str) -> NoReturn:
    """Print error to stderr and exit with code 2 (argparse usage error)."""
    print(msg, file=sys.stderr)
    sys.exit(2)


def validate_relative_path(path: Optional[str]) -> bool:
    """Check if path is non-empty relative path with no .. segments and not absolute."""
    if path is None:
        return True
    if not isinstance(path, str) or not path:
        return False
    if path.startswith("/"):
        return False
    if ".." in path.split("/"):
        return False
    return True


def validate_record(obj: Any, line_no: int) -> None:
    """Validate a record object. Raise on any violation."""
    if not isinstance(obj, dict):
        error(f"flow-ledger: human.jsonl line {line_no}: not a dict")

    # Check exact keys
    expected_keys = {"seq", "ts", "step", "attempt", "kind", "text", "transcript", "status", "addressed_by", "supersedes"}
    actual_keys = set(obj.keys())
    if actual_keys != expected_keys:
        missing = expected_keys - actual_keys
        extra = actual_keys - expected_keys
        msg = f"flow-ledger: human.jsonl line {line_no}: key mismatch"
        if missing:
            msg += f" (missing: {', '.join(sorted(missing))})"
        if extra:
            msg += f" (extra: {', '.join(sorted(extra))})"
        error(msg)

    # Validate seq: int >= 1 (reject bool)
    if type(obj["seq"]) is bool or not isinstance(obj["seq"], int):
        error(f"flow-ledger: human.jsonl line {line_no}: seq must be int, not {type(obj['seq']).__name__}")
    if obj["seq"] < 1:
        error(f"flow-ledger: human.jsonl line {line_no}: seq must be >= 1")

    # Validate ts: string
    if not isinstance(obj["ts"], str):
        error(f"flow-ledger: human.jsonl line {line_no}: ts must be string")

    # Validate step: matches ^[a-z0-9-]+$
    if not isinstance(obj["step"], str) or not re.fullmatch(r"[a-z0-9-]+", obj["step"]):
        error(f"flow-ledger: human.jsonl line {line_no}: step must match ^[a-z0-9-]+$")

    # Validate attempt: int >= 1 (reject bool)
    if type(obj["attempt"]) is bool or not isinstance(obj["attempt"], int):
        error(f"flow-ledger: human.jsonl line {line_no}: attempt must be int, not {type(obj['attempt']).__name__}")
    if obj["attempt"] < 1:
        error(f"flow-ledger: human.jsonl line {line_no}: attempt must be >= 1")

    # Validate kind
    if obj["kind"] not in KINDS:
        error(f"flow-ledger: human.jsonl line {line_no}: kind must be one of {KINDS}")

    # Validate text: non-empty string
    if not isinstance(obj["text"], str) or not obj["text"]:
        error(f"flow-ledger: human.jsonl line {line_no}: text must be non-empty string")

    # Validate transcript: null or valid relative path
    if not validate_relative_path(obj.get("transcript")):
        error(f"flow-ledger: human.jsonl line {line_no}: transcript must be null or relative path with no ..")

    # Validate addressed_by: null or valid relative path
    if not validate_relative_path(obj.get("addressed_by")):
        error(f"flow-ledger: human.jsonl line {line_no}: addressed_by must be null or relative path with no ..")

    # Validate status
    if obj["status"] not in STATUSES:
        error(f"flow-ledger: human.jsonl line {line_no}: status must be one of {STATUSES}")

    # Validate addressed_by invariant
    if obj["status"] == "addressed" and obj["addressed_by"] is None:
        error(f"flow-ledger: human.jsonl line {line_no}: status addressed requires addressed_by to be set")
    if obj["addressed_by"] is not None and obj["status"] not in ("addressed", "superseded"):
        error(f"flow-ledger: human.jsonl line {line_no}: non-null addressed_by requires status addressed or superseded")

    # Validate supersedes: only for kind=correction
    supersedes = obj.get("supersedes")
    if obj["kind"] == "correction":
        if supersedes is None:
            error(f"flow-ledger: human.jsonl line {line_no}: kind correction requires supersedes")
        if type(supersedes) is bool or not isinstance(supersedes, int):
            error(f"flow-ledger: human.jsonl line {line_no}: supersedes must be int")
        if supersedes < 1:
            error(f"flow-ledger: human.jsonl line {line_no}: supersedes must be >= 1")
        if supersedes >= obj["seq"]:
            error(f"flow-ledger: human.jsonl line {line_no}: supersedes must be < seq")
    else:
        if supersedes is not None:
            error(f"flow-ledger: human.jsonl line {line_no}: supersedes only allowed for kind correction")


def load_records(ledger_file: Path) -> List[Record]:
    """Load and validate all records from ledger file. Return empty list if file missing."""
    if not ledger_file.exists():
        return []

    records: List[Record] = []
    try:
        with open(ledger_file, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                if not line.endswith("\n"):
                    error(f"flow-ledger: human.jsonl line {line_no}: missing trailing newline")
                if not line.rstrip("\n"):
                    error(f"flow-ledger: human.jsonl line {line_no}: blank line")
                try:
                    obj = json.loads(line.rstrip("\n"))
                except json.JSONDecodeError as e:
                    error(f"flow-ledger: human.jsonl line {line_no}: JSON parse error: {e}")
                validate_record(obj, line_no)
                records.append(obj)  # type: ignore
    except (OSError, UnicodeDecodeError) as e:
        error(f"flow-ledger: human.jsonl: read error: {e}")

    # Check seq continuity
    if records:
        for i, rec in enumerate(records, 1):
            if rec["seq"] != i:
                error(f"flow-ledger: human.jsonl line {i}: seq must be exactly 1..n in order, got {rec['seq']}")

    return records


def transition_allowed(current_status: Status, new_status: Status) -> bool:
    """Check if transition from current_status to new_status is allowed."""
    allowed_transitions: Dict[Status, set] = {
        "pending": {"addressed", "parked", "superseded"},
        "addressed": set(),
        "parked": set(),
        "superseded": set(),
    }
    return new_status in allowed_transitions.get(current_status, set())


def write_records(ledger_file: Path, records: List[Record]) -> None:
    """Write records to ledger file via temp file + os.replace."""
    try:
        # Use same directory for temp file for atomic replacement
        temp_fd, temp_path = tempfile.mkstemp(
            dir=ledger_file.parent,
            prefix=".human-",
            suffix=".jsonl",
        )
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                for rec in records:
                    f.write(json.dumps(rec, separators=(",", ":")) + "\n")
            # mkstemp creates 0600; keep the mode of the existing ledger (append-created, 0644 by default)
            if ledger_file.exists():
                os.chmod(temp_path, stat.S_IMODE(os.stat(str(ledger_file)).st_mode))
            else:
                os.chmod(temp_path, 0o644)
            os.replace(temp_path, str(ledger_file))
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise
    except (OSError, IOError) as e:
        error(f"flow-ledger: human.jsonl: write error: {e}")


def cmd_append(
    flow_dir: Path,
    kind: Kind,
    step: str,
    attempt: int,
    text: str,
    transcript: Optional[str],
    supersedes: Optional[int],
) -> None:
    """Append a record to the ledger under lock."""
    ledger_file = flow_dir / "human.jsonl"
    lock_file = flow_dir / "human.jsonl.lock"

    # Acquire lock
    lock_f = None
    try:
        lock_f = open(lock_file, "a")
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)

        # Load and validate existing records
        records = load_records(ledger_file)

        # Compute next seq
        seq = len(records) + 1

        # Get current timestamp in ISO-8601 UTC
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Create new record
        new_record: Record = {
            "seq": seq,
            "ts": ts,
            "step": step,
            "attempt": attempt,
            "kind": kind,
            "text": text,
            "transcript": transcript,
            "status": "pending",
            "addressed_by": None,
            "supersedes": supersedes,
        }

        # Validate the new record
        validate_record(new_record, seq)

        # Check supersedes constraints
        if supersedes is not None:
            if kind != "correction":
                error("flow-ledger: human.jsonl: --supersedes requires --kind correction")
            if supersedes < 1 or supersedes >= seq:
                error("flow-ledger: human.jsonl: supersedes must exist and be < current seq")
            # Check that superseded record exists and is not already superseded
            if supersedes > len(records):
                error(f"flow-ledger: human.jsonl: no record with seq {supersedes}")
            if records[supersedes - 1]["status"] == "superseded":
                error(f"flow-ledger: human.jsonl: seq {supersedes} is already superseded")

        # Append or rewrite depending on supersedes
        if supersedes is None:
            # Simple append: one os.write of the full line to an O_APPEND descriptor
            data = (json.dumps(new_record, separators=(",", ":")) + "\n").encode("utf-8")
            fd = os.open(str(ledger_file), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o666)
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
            finally:
                os.close(fd)
        else:
            # Rewrite: mark superseded record, add new, rewrite entire file
            records[supersedes - 1]["status"] = "superseded"
            records.append(new_record)
            write_records(ledger_file, records)

        # Print new seq
        print(seq)

    finally:
        if lock_f:
            try:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
                lock_f.close()
            except OSError:
                pass


def cmd_mark(flow_dir: Path, seq: int, status: Status, by: Optional[str]) -> None:
    """Mark a record with a new status under lock."""
    ledger_file = flow_dir / "human.jsonl"
    lock_file = flow_dir / "human.jsonl.lock"

    # Validate status
    if status == "pending":
        error("flow-ledger: human.jsonl: status cannot be marked as pending")

    # Validate --by requirement
    if status == "addressed" and by is None:
        error("flow-ledger: human.jsonl: status addressed requires --by")
    if status != "addressed" and by is not None:
        error("flow-ledger: human.jsonl: --by only allowed with status addressed")

    # Acquire lock
    lock_f = None
    try:
        lock_f = open(lock_file, "a")
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)

        # Load and validate records
        records = load_records(ledger_file)

        # Find and update record
        if seq < 1 or seq > len(records):
            error(f"flow-ledger: human.jsonl: no record with seq {seq}")

        record = records[seq - 1]
        if record["status"] != "pending":
            error(f"flow-ledger: human.jsonl: seq {seq} is {record['status']}, not pending")

        # Check transition
        if not transition_allowed(record["status"], status):
            error(f"flow-ledger: human.jsonl: cannot transition from {record['status']} to {status}")

        # Update record
        record["status"] = status
        if by is not None:
            record["addressed_by"] = by

        # Re-validate the modified record
        validate_record(record, seq)

        # Rewrite file
        write_records(ledger_file, records)

    finally:
        if lock_f:
            try:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
                lock_f.close()
            except OSError:
                pass


def cmd_list(
    flow_dir: Path,
    status_filter: Optional[Status],
    kind_filter: Optional[Kind],
    output_format: str,
) -> None:
    """List records, optionally filtered, in JSON or Markdown format."""
    ledger_file = flow_dir / "human.jsonl"

    # No lock needed; read-only
    if not ledger_file.exists():
        # Missing file: no output, exit 0
        sys.exit(0)

    records = load_records(ledger_file)

    # Filter records
    filtered = records
    if status_filter:
        filtered = [r for r in filtered if r["status"] == status_filter]
    if kind_filter:
        filtered = [r for r in filtered if r["kind"] == kind_filter]

    if output_format == "json":
        print(json.dumps(filtered, separators=(",", ":")))
    else:  # markdown
        for rec in filtered:
            kind_status = f"[{rec['kind']}, {rec['status']}]"
            line = f"- #{rec['seq']} {kind_status} {rec['step']} attempt {rec['attempt']} ({rec['ts']})"

            # Append supersedes if set
            if rec["supersedes"] is not None:
                line += f" supersedes #{rec['supersedes']}"

            # Append addressed_by if set
            if rec["addressed_by"] is not None:
                line += f" by {rec['addressed_by']}"

            print(line)

            # Print text indented on following lines
            for text_line in rec["text"].split("\n"):
                print(f"  {text_line}")


def main() -> None:
    """Parse arguments and dispatch to subcommand."""
    # Parent parser with common arguments
    parent_parser = argparse.ArgumentParser(add_help=False)
    parent_parser.add_argument(
        "--flow-dir",
        required=True,
        help="Flow directory (must contain state.json)",
    )

    parser = argparse.ArgumentParser(
        description="Append-only human-input ledger for /expert-flow",
    )

    # Subcommands
    subparsers = parser.add_subparsers(dest="command", required=True, help="Subcommand")

    # append subcommand
    append_parser = subparsers.add_parser("append", help="Append a record", parents=[parent_parser])
    append_parser.add_argument("--kind", required=True, choices=KINDS, help="Record kind")
    append_parser.add_argument("--step", required=True, help="Step name (must match ^[a-z0-9-]+$)")
    append_parser.add_argument("--attempt", required=True, type=int, help="Attempt number (>= 1)")
    append_text_group = append_parser.add_mutually_exclusive_group(required=True)
    append_text_group.add_argument("--text", help="Record text")
    append_text_group.add_argument("--text-stdin", action="store_true", help="Read text from stdin")
    append_parser.add_argument("--transcript", help="Path to transcript file (relative)")
    append_parser.add_argument("--supersedes", type=int, help="Record seq to supersede (kind=correction only)")

    # mark subcommand
    mark_parser = subparsers.add_parser("mark", help="Mark a record", parents=[parent_parser])
    mark_parser.add_argument("--seq", required=True, type=int, help="Record seq")
    mark_parser.add_argument("--status", required=True, choices=["addressed", "parked", "superseded"], help="New status")
    mark_parser.add_argument("--by", help="Path to artifact that addressed the record (required for addressed)")

    # list subcommand
    list_parser = subparsers.add_parser("list", help="List records", parents=[parent_parser])
    list_parser.add_argument("--status", choices=STATUSES, help="Filter by status")
    list_parser.add_argument("--kind", choices=KINDS, help="Filter by kind")
    format_group = list_parser.add_mutually_exclusive_group()
    format_group.add_argument("--json", action="store_true", default=True, help="Output JSON (default)")
    format_group.add_argument("--md", action="store_true", help="Output Markdown")

    args = parser.parse_args()

    # Validate flow-dir
    flow_dir = Path(args.flow_dir).expanduser().resolve()
    if not flow_dir.is_dir():
        error(f"flow-ledger: {args.flow_dir}: not a directory")
    if not (flow_dir / "state.json").exists():
        error(f"flow-ledger: {args.flow_dir}: no state.json")

    try:
        if args.command == "append":
            # Get text from argument or stdin
            if args.text_stdin:
                text = sys.stdin.read()
                # Strip exactly one trailing newline
                if text.endswith("\n"):
                    text = text[:-1]
            else:
                text = args.text

            cmd_append(
                flow_dir=flow_dir,
                kind=args.kind,  # type: ignore
                step=args.step,
                attempt=args.attempt,
                text=text,
                transcript=args.transcript,
                supersedes=args.supersedes,
            )

        elif args.command == "mark":
            cmd_mark(
                flow_dir=flow_dir,
                seq=args.seq,
                status=args.status,  # type: ignore
                by=args.by,
            )

        elif args.command == "list":
            output_format = "md" if args.md else "json"
            cmd_list(
                flow_dir=flow_dir,
                status_filter=args.status,  # type: ignore
                kind_filter=args.kind,  # type: ignore
                output_format=output_format,
            )

    except SystemExit:
        # Let error() and usage_error() exits through
        raise


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        error(f"flow-ledger: unexpected error: {e}")
