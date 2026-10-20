#!/usr/bin/env python3
"""
Append-only human-input ledger for /expert-flow.

Manages a per-flow ledger file (human.jsonl) that records all human inputs
(answers, directions, corrections) alongside metadata (step, attempt, timestamp).
Uses file-level locking (sidecar lock file) for concurrent access safety.

Record keys (in order): seq, ts, step, attempt, kind, text, transcript, status,
addressed_by, supersedes.

Subcommands:
  append: Append a new record to the ledger. Prints the new record's seq (integer).
  mark: Update a record's status. No output.
  list: List records (optionally filtered). Outputs JSON (default) or Markdown.

Stdout:
  append: prints the new seq number (integer)
  list --json (or no format option): prints a JSON array of records
  list --md: prints Markdown (one list item per record)
  mark: no output

Precondition:
  The flow directory must contain a state.json file (checked on all commands).

Sidecar lock file:
  human.jsonl.lock is created in the flow directory and used to serialize access.

Exit codes:
  0: success
  1: data/contract violation or write failure (message on stderr; file rolled back on write failure)
  2: argparse usage error
"""

import argparse
import contextlib
import fcntl
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypedDict, Optional, List, Dict, Any, NoReturn, Tuple, FrozenSet, get_args

# Type definitions
Kind = Literal["transcript", "answer", "direction", "question", "parked", "gate_note", "correction"]
Status = Literal["pending", "addressed", "parked", "superseded"]
MarkStatus = Literal["addressed", "parked"]

# Derived from the Literal types so the argparse choices cannot drift from the model.
KINDS: Tuple[Kind, ...] = get_args(Kind)
STATUSES: Tuple[Status, ...] = get_args(Status)
MARK_STATUSES: Tuple[MarkStatus, ...] = get_args(MarkStatus)


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


def validate_ts(ts: str) -> bool:
    r"""Check if ts is ISO-8601 UTC with Z suffix: ^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$"""
    if not isinstance(ts, str):
        return False
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z", ts))


def parse_record(obj: Any, label: str) -> Record:
    """Parse and validate a record object from the ledger or new record.

    Args:
        obj: The object to parse (should be dict).
        label: A description for error messages (e.g. "human.jsonl line 5" or "append: new record").

    Returns:
        A validated Record.

    Raises:
        SystemExit(1) on any validation error.
    """
    if not isinstance(obj, dict):
        error(f"flow-ledger: {label}: not a dict")

    # Check exact keys
    expected_keys = {"seq", "ts", "step", "attempt", "kind", "text", "transcript", "status", "addressed_by", "supersedes"}
    actual_keys = set(obj.keys())
    if actual_keys != expected_keys:
        missing = expected_keys - actual_keys
        extra = actual_keys - expected_keys
        msg = f"flow-ledger: {label}: key mismatch"
        if missing:
            msg += f" (missing: {', '.join(sorted(missing))})"
        if extra:
            msg += f" (extra: {', '.join(sorted(extra))})"
        error(msg)

    # Validate seq: int >= 1 (reject bool)
    if type(obj["seq"]) is bool or not isinstance(obj["seq"], int):
        error(f"flow-ledger: {label}: seq must be int, not {type(obj['seq']).__name__}")
    if obj["seq"] < 1:
        error(f"flow-ledger: {label}: seq must be >= 1")

    # Validate ts: ISO-8601 UTC format with Z suffix
    ts_val = obj.get("ts")
    if not isinstance(ts_val, str) or not validate_ts(ts_val):
        error(f"flow-ledger: {label}: ts must be ISO-8601 UTC with Z suffix")

    # Validate step: matches ^[a-z0-9-]+$
    if not isinstance(obj["step"], str) or not re.fullmatch(r"[a-z0-9-]+", obj["step"]):
        error(f"flow-ledger: {label}: step must match ^[a-z0-9-]+$")

    # Validate attempt: int >= 1 (reject bool)
    if type(obj["attempt"]) is bool or not isinstance(obj["attempt"], int):
        error(f"flow-ledger: {label}: attempt must be int, not {type(obj['attempt']).__name__}")
    if obj["attempt"] < 1:
        error(f"flow-ledger: {label}: attempt must be >= 1")

    # Validate kind
    if obj["kind"] not in KINDS:
        error(f"flow-ledger: {label}: kind must be one of {KINDS}")

    # Validate text: non-empty string
    if not isinstance(obj["text"], str) or not obj["text"]:
        error(f"flow-ledger: {label}: text must be non-empty string")

    # Validate transcript: null or valid relative path
    if not validate_relative_path(obj.get("transcript")):
        error(f"flow-ledger: {label}: transcript must be null or relative path with no ..")

    # Validate addressed_by: null or valid relative path
    if not validate_relative_path(obj.get("addressed_by")):
        error(f"flow-ledger: {label}: addressed_by must be null or relative path with no ..")

    # Validate status
    if obj["status"] not in STATUSES:
        error(f"flow-ledger: {label}: status must be one of {STATUSES}")

    # Validate addressed_by invariant
    if obj["status"] == "addressed" and obj["addressed_by"] is None:
        error(f"flow-ledger: {label}: status addressed requires addressed_by to be set")
    if obj["addressed_by"] is not None and obj["status"] not in ("addressed", "superseded"):
        error(f"flow-ledger: {label}: non-null addressed_by requires status addressed or superseded")

    # Validate supersedes: only for kind=correction
    supersedes = obj.get("supersedes")
    if obj["kind"] == "correction":
        if supersedes is None:
            error(f"flow-ledger: {label}: kind correction requires supersedes")
        if type(supersedes) is bool or not isinstance(supersedes, int):
            error(f"flow-ledger: {label}: supersedes must be int")
        if supersedes < 1:
            error(f"flow-ledger: {label}: supersedes must be >= 1")
        if supersedes >= obj["seq"]:
            error(f"flow-ledger: {label}: supersedes must be < seq")
    else:
        if supersedes is not None:
            error(f"flow-ledger: {label}: supersedes only allowed for kind correction")

    return obj  # type: ignore


def load_records(ledger_file: Path) -> List[Record]:
    """Load and validate all records from ledger file. Return empty list if file missing.

    On any error reading or validating the ledger, calls error() which exits with code 1.
    """
    if not ledger_file.exists():
        return []

    records: List[Record] = []
    try:
        with open(ledger_file, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                if not line.endswith("\n"):
                    error(f"flow-ledger: human.jsonl line {line_no}: missing trailing newline (torn write; delete the partial last line to repair)")
                if not line.rstrip("\n"):
                    error(f"flow-ledger: human.jsonl line {line_no}: blank line")
                try:
                    obj = json.loads(line.rstrip("\n"))
                except json.JSONDecodeError as e:
                    error(f"flow-ledger: human.jsonl line {line_no}: JSON parse error: {e}")
                rec = parse_record(obj, f"human.jsonl line {line_no}")
                records.append(rec)
    except (OSError, UnicodeDecodeError) as e:
        error(f"flow-ledger: human.jsonl: read error: {e}")

    # Check seq continuity
    if records:
        for i, rec in enumerate(records, 1):
            if rec["seq"] != i:
                error(f"flow-ledger: human.jsonl line {i}: seq must be exactly 1..n in order, got {rec['seq']}")

    return records


def transition_allowed(current_status: Status, new_status: Status) -> bool:
    """Check if transition from current_status to new_status is allowed.

    Only pending -> addressed or pending -> parked are allowed.
    """
    allowed_transitions: Dict[Status, FrozenSet[Status]] = {
        "pending": frozenset(["addressed", "parked"]),
        "addressed": frozenset(),
        "parked": frozenset(),
        "superseded": frozenset(),
    }
    return new_status in allowed_transitions.get(current_status, frozenset())


def write_records(ledger_file: Path, records: List[Record]) -> None:
    """Write records to ledger file via temp file + os.replace.

    Creates the temp file with mode 0o600 and syncs before replace.
    On any error, removes the temp file and calls error() to exit with code 1.
    """
    temp_path = None
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
                # Flush and fsync before close
                f.flush()
                os.fsync(f.fileno())
            # mkstemp creates 0600; preserve that for the ledger
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, str(ledger_file))
        except Exception:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            raise
    except (OSError, IOError) as e:
        error(f"flow-ledger: append: write failed ({e}); ledger rolled back")


@contextlib.contextmanager
def ledger_lock(flow_dir: Path, shared: bool = False):  # type: ignore
    """Context manager to acquire and release a lock on the ledger.

    Args:
        flow_dir: The flow directory.
        shared: If True, acquire a shared lock (LOCK_SH); otherwise exclusive (LOCK_EX).

    Yields:
        None. The lock is held for the duration of the context.
    """
    lock_file = flow_dir / "human.jsonl.lock"
    lock_f = None
    try:
        # Create lock file with mode 0o600 if not present
        if not lock_file.exists():
            fd = os.open(str(lock_file), os.O_WRONLY | os.O_CREAT, 0o600)
            os.close(fd)

        lock_f = open(lock_file, "a")
        lock_type = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(lock_f.fileno(), lock_type)
        yield
    except OSError as e:
        error(f"flow-ledger: lock error: {e}")
    finally:
        if lock_f:
            try:
                lock_f.close()
            except OSError:
                pass


def cmd_append(
    flow_dir: Path,
    kind: Kind,
    step: str,
    attempt: int,
    text: str,
    transcript: Optional[str],
    supersedes: Optional[int],
) -> None:
    """Append a record to the ledger under lock.

    Validates CLI arguments first with non-"line N" error messages.
    On success, prints the new record's seq number.
    """
    # CLI-level validation first (not "line N" messages)
    if not isinstance(step, str) or not step or not re.fullmatch(r"[a-z0-9-]+", step):
        error("flow-ledger: append: --step must match ^[a-z0-9-]+$")
    if not isinstance(attempt, int) or attempt < 1:
        error("flow-ledger: append: --attempt must be integer >= 1")
    if not isinstance(text, str) or not text:
        error("flow-ledger: append: --text must be non-empty string")
    if not validate_relative_path(transcript):
        error("flow-ledger: append: --transcript must be null or relative path with no ..")
    if supersedes is not None:
        if kind != "correction":
            error("flow-ledger: append: --supersedes requires --kind correction")
        if not isinstance(supersedes, int) or supersedes < 1:
            error("flow-ledger: append: --supersedes must be integer >= 1")

    ledger_file = flow_dir / "human.jsonl"

    with ledger_lock(flow_dir, shared=False):
        # Load and validate existing records
        records = load_records(ledger_file)

        # Compute next seq
        seq = len(records) + 1

        # Additional supersedes checks that require knowing the record count
        if supersedes is not None:
            if supersedes >= seq:
                error(f"flow-ledger: append: --supersedes {supersedes} must be < next seq {seq}")
            if supersedes > len(records):
                error(f"flow-ledger: append: no record with seq {supersedes}")
            if records[supersedes - 1]["status"] == "superseded":
                error(f"flow-ledger: append: seq {supersedes} is already superseded")

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
        parse_record(new_record, "append: new record")

        # Append or rewrite depending on supersedes
        if supersedes is None:
            # Simple append: record the size before writing and rollback on error
            size_before = 0
            if ledger_file.exists():
                size_before = ledger_file.stat().st_size

            data = (json.dumps(new_record, separators=(",", ":")) + "\n").encode("utf-8")
            try:
                fd = os.open(str(ledger_file), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    view = memoryview(data)
                    while view:
                        view = view[os.write(fd, view):]
                    # Sync to disk
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError as e:
                # Rollback: truncate back to size before write
                try:
                    with open(str(ledger_file), "r+b") as f:
                        f.truncate(size_before)
                        os.fsync(f.fileno())
                except OSError:
                    pass
                error(f"flow-ledger: append: write failed ({e}); ledger rolled back")
        else:
            # Rewrite: mark superseded record, add new, rewrite entire file
            records[supersedes - 1]["status"] = "superseded"
            records.append(new_record)
            write_records(ledger_file, records)

        # Print new seq
        print(seq)


def cmd_mark(flow_dir: Path, seq: int, status: MarkStatus, by: Optional[str]) -> None:
    """Mark a record with a new status under lock.

    Allowed statuses: addressed, parked.
    For status=addressed, --by is required and is a relative artifact path (e.g., steps/05-fix.md).
    """
    # Validate --by requirement
    if status == "addressed" and by is None:
        error("flow-ledger: mark: --status addressed requires --by")
    if status != "addressed" and by is not None:
        error("flow-ledger: mark: --by only allowed with --status addressed")
    if by is not None and not validate_relative_path(by):
        error("flow-ledger: mark: --by must be null or relative path with no ..")

    ledger_file = flow_dir / "human.jsonl"

    with ledger_lock(flow_dir, shared=False):
        # Load and validate records
        records = load_records(ledger_file)

        # Find and update record
        if seq < 1 or seq > len(records):
            error(f"flow-ledger: mark: no record with seq {seq}")

        record = records[seq - 1]
        if record["status"] != "pending":
            error(f"flow-ledger: mark: seq {seq} is {record['status']}, not pending")

        # Check transition (transition_allowed is the single source of truth)
        if not transition_allowed(record["status"], status):
            error(f"flow-ledger: mark: cannot transition from {record['status']} to {status}")

        # Update record
        record["status"] = status
        if by is not None:
            record["addressed_by"] = by

        # Re-validate the modified record
        parse_record(record, f"mark: record {seq}")

        # Rewrite file
        write_records(ledger_file, records)


def cmd_list(
    flow_dir: Path,
    status_filter: Optional[Status],
    kind_filter: Optional[Kind],
    output_format: str,
) -> None:
    """List records, optionally filtered, in JSON or Markdown format.

    Output:
      JSON (default): prints a JSON array of record objects (prints [] if file missing)
      Markdown: prints one list item per record (prints nothing if file missing)
    """
    ledger_file = flow_dir / "human.jsonl"

    # If file doesn't exist, handle based on output format
    if not ledger_file.exists():
        if output_format == "json":
            print("[]")
        return

    # Take shared lock when file exists
    with ledger_lock(flow_dir, shared=True):
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
    mark_parser.add_argument("--status", required=True, choices=MARK_STATUSES, help="New status (addressed or parked)")
    mark_parser.add_argument("--by", help="Path to artifact that addressed the record (relative path; required for addressed)")

    # list subcommand
    list_parser = subparsers.add_parser("list", help="List records", parents=[parent_parser])
    list_parser.add_argument("--status", choices=STATUSES, help="Filter by status")
    list_parser.add_argument("--kind", choices=KINDS, help="Filter by kind")
    format_group = list_parser.add_mutually_exclusive_group()
    format_group.add_argument("--json", action="store_true", dest="json_format", help="Output JSON (default)")
    format_group.add_argument("--md", action="store_true", dest="md_format", help="Output Markdown")

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
                # Read stdin as UTF-8 regardless of locale
                text = sys.stdin.buffer.read().decode("utf-8")
                # Strip exactly one trailing newline
                if text.endswith("\n"):
                    text = text[:-1]
            else:
                text = args.text

            cmd_append(
                flow_dir=flow_dir,
                kind=args.kind,  # type: ignore[arg-type]
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
                status=args.status,  # type: ignore[arg-type]
                by=args.by,
            )

        elif args.command == "list":
            output_format = "md" if args.md_format else "json"
            cmd_list(
                flow_dir=flow_dir,
                status_filter=args.status,  # type: ignore[arg-type]
                kind_filter=args.kind,  # type: ignore[arg-type]
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
