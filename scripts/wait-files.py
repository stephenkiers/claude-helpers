#!/usr/bin/env python3
"""Bounded, idempotent foreground wait on N children's output files (#532).

A step agent or skill never ends its turn to wait for children
(prompts/join-barrier-pattern.md § Waiting for the barrier). Where children are launched in the
background, it blocks on their output files with this script instead.

Usage:
  wait-files.py mark --phase P --state-dir D [--force]
  wait-files.py wait --phase P --state-dir D --deadline SECS [--timeout SECS] [--since EPOCH]
                     (--sentinel TEXT | --exists) ID=PATH [ID=PATH ...]

mark   records the current epoch for phase P in D/child-wait-since.json (atomic write) and prints
       it. Call it immediately before the launch, so the launch time survives context compaction.
       The epoch is math.ceil(time.time()), a whole second, so it never lands before the launch.
       mark refuses to overwrite an open phase: one that has a recorded mark with no closed row in
       D/child-wait.jsonl for that same mark. It exits 2 and names --force. Pass --force to re-mark
       a phase deliberately. Use one phase per STATE_DIR. After context compaction, pass an
       explicit --since to wait instead of re-marking.

wait   polls the files every 5 s (no model tokens) for one slice of --timeout seconds (default
       540, allowed 300..570: the floor stops the wait being tuned down into polling). --since
       defaults to the phase's recorded mark; with neither, it is a usage error. Each id is:
         ok       file exists, mtime >= since, and (--sentinel) its last non-empty line, stripped,
                  equals TEXT; (--exists) existence plus freshness is enough
         stale    file exists but mtime < since
         writing  fresh file without the sentinel yet (never treated as bad)
         missing  no file
       Exit codes:
         0  every id ok: prints "closed: <n> ids"
         3  slice over before the deadline: prints "pending: <ids>"; re-issue the call at once
         4  now >= since + deadline: prints "deadline: outstanding <ids> (phase P, attempt n,
            deadline SECSs)", plus "loop: repeated child-wait deadline for phase P" when n >= 2
         2  usage error
       On closed and deadline (never pending) one JSON line is appended to D/child-wait.jsonl.
       D/child-wait-attempts.json counts deadlines per phase. The count is consecutive per phase:
       each deadline adds one, and a closed outcome resets it to 0. mark does NOT reset the count;
       it only records the epoch.

Time anchor: --since and --deadline share one epoch anchor. The deadline is measured from since,
so a long gap between the mark and the wait eats into the deadline. After a long gap, re-run mark
with --force to re-anchor the phase, or pass an explicit --since for the original launch time.
Bookkeeping (attempt counter, telemetry) is best-effort: an OSError there prints a one-line stderr
warning and never changes the outcome or exit code. An unreadable attempts file counts as empty.

The script never relaunches a child, retries, writes a stand-in, or touches a watched file.
"""
import argparse
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, Literal, NamedTuple, NoReturn, Optional, Sequence, Tuple

SINCE_FILE = "child-wait-since.json"
ATTEMPTS_FILE = "child-wait-attempts.json"
TELEMETRY_FILE = "child-wait.jsonl"

DEFAULT_TIMEOUT = 540
MIN_TIMEOUT = 300
MAX_TIMEOUT = 570
POLL_SECS = 5.0

EXIT_CLOSED = 0
EXIT_USAGE = 2
EXIT_PENDING = 3
EXIT_DEADLINE = 4

# Closed-set values, as Literal aliases (Python 3.8 compatible).
State = Literal["ok", "stale", "writing", "missing"]
Outcome = Literal["closed", "pending", "deadline"]


class UsageError(Exception):
    pass


class WaitResult(NamedTuple):
    status: Outcome
    outstanding: List[str]
    states: Dict[str, State]
    attempt: int  # attempt number on deadline; 0 otherwise
    elapsed_secs: int


def _warn(message: str) -> None:
    print("warning: %s" % message, file=sys.stderr)


def _read_json_object(path: Path) -> Dict[str, int]:
    """Read a JSON object of int values. Non-int entries are dropped with one warning each."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise UsageError("unreadable %s: %s" % (path, exc))
    if not isinstance(data, dict):
        raise UsageError("%s is not a JSON object" % path)
    out: Dict[str, int] = {}
    for key, value in data.items():
        if isinstance(value, int) and not isinstance(value, bool):
            out[str(key)] = value
        else:
            _warn("dropped non-int entry %r in %s" % (key, path))
    return out


def _write_json_atomic(path: Path, data: Dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _phase_closed_since(state_dir: Path, phase: str, since: int) -> bool:
    """True when a closed telemetry row exists for this phase and mark epoch."""
    try:
        lines = (state_dir / TELEMETRY_FILE).read_text().splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (isinstance(row, dict) and row.get("phase") == phase and row.get("outcome") == "closed"
                and row.get("since") == since):
            return True
    return False


def mark(phase: str, state_dir: Path, now: Optional[int] = None, force: bool = False) -> int:
    """Record the launch epoch for `phase` and return it.

    Refuses (UsageError naming --force) when the phase is open: it has a recorded mark that has
    not yet closed. `force` re-marks it deliberately.
    """
    path = state_dir / SINCE_FILE
    data = _read_json_object(path)
    prior = data.get(phase)
    if prior is not None and not force and not _phase_closed_since(state_dir, phase, prior):
        raise UsageError("phase %r is already open (marked at %d and not closed); pass --force to "
                         "re-mark it, or pass an explicit --since to wait" % (phase, prior))
    epoch = math.ceil(time.time()) if now is None else now
    data[phase] = epoch
    _write_json_atomic(path, data)
    return epoch


def recorded_since(phase: str, state_dir: Path) -> Optional[int]:
    return _read_json_object(state_dir / SINCE_FILE).get(phase)


def _last_nonempty_line(path: Path) -> Optional[str]:
    try:
        with open(str(path), "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 65536))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        if line.strip():
            return line.strip()
    return None


def file_state(path: Path, since: int, sentinel: Optional[str]) -> State:
    """Return ok | stale | writing | missing for one watched file (read-only).

    A missing file is "missing". Other OSErrors (permission, I/O) also yield "missing", but print
    a one-line stderr warning so the cause is visible.
    """
    try:
        mtime = os.stat(str(path)).st_mtime
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        _warn("cannot stat %s: %s" % (path, exc))
        return "missing"
    if mtime < since:
        return "stale"
    if sentinel is None:
        return "ok"
    return "ok" if _last_nonempty_line(path) == sentinel.strip() else "writing"


def _states(ids: Sequence[Tuple[str, Path]], since: int, sentinel: Optional[str]) -> Dict[str, State]:
    return {i: file_state(p, since, sentinel) for i, p in ids}


def _read_attempts(state_dir: Path) -> Dict[str, int]:
    """Best-effort read of the attempt counter: an unreadable file counts as empty."""
    try:
        return _read_json_object(state_dir / ATTEMPTS_FILE)
    except UsageError as exc:
        _warn("%s; treating attempt counter as empty" % exc)
        return {}


def _record_outcome(phase: str, state_dir: Path, result: WaitResult, since: int, deadline: int,
                    ts: int) -> None:
    """Best-effort bookkeeping for a closed or deadline outcome. Never changes the result."""
    if result.status == "pending":
        return
    try:
        attempts = _read_attempts(state_dir)
        attempts[phase] = result.attempt
        _write_json_atomic(state_dir / ATTEMPTS_FILE, attempts)
    except OSError as exc:
        _warn("could not update %s: %s" % (state_dir / ATTEMPTS_FILE, exc))
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": ts, "phase": phase, "outcome": result.status, "since": since,
            "deadline_secs": deadline, "elapsed_secs": result.elapsed_secs, "ids": result.states,
            "outstanding": result.outstanding if result.status == "deadline" else [],
            "attempt": result.attempt,
        }
        with open(str(state_dir / TELEMETRY_FILE), "a") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError as exc:
        _warn("could not append telemetry to %s: %s" % (state_dir / TELEMETRY_FILE, exc))


def wait_once(
    phase: str,
    state_dir: Path,
    ids: Sequence[Tuple[str, Path]],
    since: int,
    deadline: int,
    timeout: float,
    sentinel: Optional[str] = None,
    poll: float = POLL_SECS,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> WaitResult:
    """One wait slice. No CLI timeout floor here; the CLI enforces it.

    Polling is computed first; the attempt counter and telemetry are written afterwards by a
    best-effort helper, so a bookkeeping failure never changes the outcome.
    `sentinel` None means --exists mode. Bookkeeping runs on closed and deadline outcomes only.
    """
    if not ids:
        raise UsageError("no ids to wait on")
    start = clock()
    while True:
        now = clock()
        states = _states(ids, since, sentinel)
        outstanding = sorted(i for i, s in states.items() if s != "ok")
        elapsed = int(now - since)
        if not outstanding:
            result = WaitResult("closed", [], states, 0, elapsed)
            _record_outcome(phase, state_dir, result, since, deadline, int(now))
            return result
        if now >= since + deadline:
            attempt = _read_attempts(state_dir).get(phase, 0) + 1
            result = WaitResult("deadline", outstanding, states, attempt, elapsed)
            _record_outcome(phase, state_dir, result, since, deadline, int(now))
            return result
        if now - start >= timeout:
            return WaitResult("pending", outstanding, states, 0, elapsed)
        remaining = min(poll, timeout - (now - start), since + deadline - now)
        sleep(max(0.0, remaining))


def _parse_ids(pairs: Sequence[str]) -> List[Tuple[str, Path]]:
    out: List[Tuple[str, Path]] = []
    seen = set()
    for pair in pairs:
        if "=" not in pair:
            raise UsageError("expected ID=PATH, got %r" % pair)
        ident, path = pair.split("=", 1)
        if not ident or not path or "," in ident:
            raise UsageError("bad ID=PATH %r" % pair)
        if ident in seen:
            raise UsageError("duplicate id %r" % ident)
        seen.add(ident)
        out.append((ident, Path(path)))
    if not out:
        raise UsageError("at least one ID=PATH is required")
    return out


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UsageError(message)


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="wait-files.py", description="Bounded foreground wait on child files.")
    sub = parser.add_subparsers(dest="cmd")
    m = sub.add_parser("mark")
    m.add_argument("--phase", required=True)
    m.add_argument("--state-dir", required=True)
    m.add_argument("--force", action="store_true",
                   help="re-mark a phase that is already open (see the module docstring)")
    w = sub.add_parser("wait")
    w.add_argument("--phase", required=True)
    w.add_argument("--state-dir", required=True)
    w.add_argument("--deadline", required=True, type=int)
    w.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    w.add_argument("--since", type=int,
                   help="epoch anchor shared with --deadline; pass it explicitly after a long gap")
    mode = w.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sentinel")
    mode.add_argument("--exists", action="store_true")
    w.add_argument("pairs", nargs="+", metavar="ID=PATH")
    # Subparsers built by add_parser use the parent's class, so errors raise UsageError too.
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args = _build_parser().parse_args(argv)
        if args.cmd == "mark":
            print(mark(args.phase, Path(args.state_dir), force=args.force))
            return EXIT_CLOSED
        if args.cmd != "wait":
            raise UsageError("expected a subcommand: mark or wait")
        if not (MIN_TIMEOUT <= args.timeout <= MAX_TIMEOUT):
            raise UsageError("--timeout must be between %d and %d" % (MIN_TIMEOUT, MAX_TIMEOUT))
        if args.deadline <= 0:
            raise UsageError("--deadline must be positive")
        state_dir = Path(args.state_dir)
        since = args.since if args.since is not None else recorded_since(args.phase, state_dir)
        if since is None:
            raise UsageError("no --since and no recorded mark for phase %r" % args.phase)
        if args.sentinel is not None and not args.sentinel.strip():
            raise UsageError("--sentinel must not be blank")
        ids = _parse_ids(args.pairs)
        result = wait_once(args.phase, state_dir, ids, since, args.deadline, args.timeout,
                           sentinel=None if args.exists else args.sentinel)
    except UsageError as exc:
        print("usage error: %s" % exc, file=sys.stderr)
        return EXIT_USAGE
    if result.status == "closed":
        print("closed: %d ids" % len(ids))
        return EXIT_CLOSED
    if result.status == "pending":
        print("pending: %s" % ",".join(result.outstanding))
        return EXIT_PENDING
    print("deadline: outstanding %s (phase %s, attempt %d, deadline %ds)"
          % (",".join(result.outstanding), args.phase, result.attempt, args.deadline))
    if result.attempt >= 2:
        print("loop: repeated child-wait deadline for phase %s" % args.phase)
    return EXIT_DEADLINE


if __name__ == "__main__":
    sys.exit(main())
