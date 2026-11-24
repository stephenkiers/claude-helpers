#!/usr/bin/env python3
"""Bounded, idempotent foreground wait on N children's output files (#532).

A step agent or skill never ends its turn to wait for children
(prompts/join-barrier-pattern.md § Waiting for the barrier). Where children are launched in the
background, it blocks on their output files with this script instead.

Usage:
  wait-files.py mark --phase P --state-dir D
  wait-files.py wait --phase P --state-dir D --deadline SECS [--timeout SECS] [--since EPOCH]
                     (--sentinel TEXT | --exists) ID=PATH [ID=PATH ...]

mark   records the current epoch for phase P in D/child-wait-since.json (atomic write) and prints
       it. Call it immediately before the launch, so the launch time survives context compaction.

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
       D/child-wait-attempts.json counts deadlines per phase and resets to 0 on closed.

The script never relaunches a child, retries, writes a stand-in, or touches a watched file.
"""
import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, NoReturn, Optional, Sequence, Tuple

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


class UsageError(Exception):
    pass


class WaitResult(NamedTuple):
    status: str  # "closed" | "pending" | "deadline"
    outstanding: List[str]
    states: Dict[str, str]
    attempt: int  # attempt number on deadline; 0 otherwise
    elapsed_secs: int


def _read_json_object(path: Path) -> Dict[str, int]:
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


def mark(phase: str, state_dir: Path, now: Optional[int] = None) -> int:
    """Record the launch epoch for `phase` and return it."""
    epoch = int(time.time()) if now is None else now
    path = state_dir / SINCE_FILE
    data = _read_json_object(path)
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


def file_state(path: Path, since: int, sentinel: Optional[str]) -> str:
    """Return ok | stale | writing | missing for one watched file (read-only)."""
    try:
        mtime = os.stat(str(path)).st_mtime
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "missing"
    if mtime < since:
        return "stale"
    if sentinel is None:
        return "ok"
    return "ok" if _last_nonempty_line(path) == sentinel.strip() else "writing"


def _states(ids: Sequence[Tuple[str, Path]], since: int, sentinel: Optional[str]) -> Dict[str, str]:
    return {i: file_state(p, since, sentinel) for i, p in ids}


def _append_telemetry(state_dir: Path, row: Dict[str, object]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(str(state_dir / TELEMETRY_FILE), "a") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


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

    `sentinel` None means --exists mode. Records telemetry and the attempt counter on closed and
    deadline outcomes only.
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
            attempts = _read_json_object(state_dir / ATTEMPTS_FILE)
            attempts[phase] = 0
            _write_json_atomic(state_dir / ATTEMPTS_FILE, attempts)
            _append_telemetry(state_dir, {
                "ts": int(now), "phase": phase, "outcome": "closed", "since": since,
                "deadline_secs": deadline, "elapsed_secs": elapsed, "ids": states,
                "outstanding": [], "attempt": 0,
            })
            return WaitResult("closed", [], states, 0, elapsed)
        if now >= since + deadline:
            attempts = _read_json_object(state_dir / ATTEMPTS_FILE)
            attempt = attempts.get(phase, 0) + 1
            attempts[phase] = attempt
            _write_json_atomic(state_dir / ATTEMPTS_FILE, attempts)
            _append_telemetry(state_dir, {
                "ts": int(now), "phase": phase, "outcome": "deadline", "since": since,
                "deadline_secs": deadline, "elapsed_secs": elapsed, "ids": states,
                "outstanding": outstanding, "attempt": attempt,
            })
            return WaitResult("deadline", outstanding, states, attempt, elapsed)
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
    w = sub.add_parser("wait")
    w.add_argument("--phase", required=True)
    w.add_argument("--state-dir", required=True)
    w.add_argument("--deadline", required=True, type=int)
    w.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    w.add_argument("--since", type=int)
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
            print(mark(args.phase, Path(args.state_dir)))
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
