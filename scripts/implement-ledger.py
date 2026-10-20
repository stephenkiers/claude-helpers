#!/usr/bin/env python3
"""Fail-closed run ledger for /implement-with-haiku (the only writer of its pointer and tombstone files).

Usage:
    implement-ledger.py init --scratch S --start-sha X [--pause] [--resumed-from ID]
                             [--carried A..B] [--session-id SID] [--run-key K]
    implement-ledger.py locate [--repo R] [--run-key K | --since ISO | --flow-dir D --step {implement,fix}]
    implement-ledger.py check [--run-key K]
    implement-ledger.py render [--run-key K]
    implement-ledger.py finish [--run-key K] [--log P --state-dir D]

Each run has a ledger at <SCRATCH>/ledger.md with one line per mandatory step:
    <run-key> DONE <step-id> <evidence>     (evidence: absolute path to a non-empty file under SCRATCH,
                                             or carried:<A>..<B> for round1 only)
    <run-key> SKIPPED <step-id> <reason>    (reason must be permitted for the run's class)
    <run-key> FINISHED                      (written by a successful finish; exactly-once per run)
Lines starting with '#' and blank lines are ignored. Any other line that does not parse, names a
foreign run key, an unknown step or reason, or carries bad evidence is malformed: it is reported by
check and never satisfies a step.

The run's pointer is <STATE_ROOT>/<run-key>.pointer (JSON). STATE_ROOT is
$IMPLEMENT_LEDGER_STATE_DIR when set, else ~/.claude/state/implement-with-haiku. Finished runs are
tombstoned to <run-key>.finished; runs replaced by a newer init are renamed to <run-key>.superseded.

The class (mechanical | test-only | full) is read from the round-sizing evidence file
(class=<value>); a missing or unreadable round-sizing row is treated as full.

Skip reasons: mechanical, test-only, no-test-files-changed, dirty-file, no-comparison-operator,
no-new-tests, pause-not-requested. See permitted_skip_reasons() for the
authoritative skip matrix.

Exit codes:
    init    0 ok; 2 invalid arguments or refused overwrite or I/O failure.
    locate  0 found (also when already finished); 3 nothing found, no begin or retry line for --step, an
            invalid flow dir, or an unreadable flow log; 2 invalid arguments or unreadable state.
    check   0 complete; 1 incomplete or malformed, or no pointer found; 2 unreadable state.
    render  0 rendered; 3 nothing found; 2 unreadable state.
    finish  0 success, or already finished (no-op); 1 gate failed (command-end recorded as
            steps_skipped), or success event could not be recorded (FINISHED not written);
            3 no pointer or tombstone found (steps_skipped recorded); 2 bad arguments.

Importable API (tests load this file with importlib, since the name is hyphenated):
STEP_IDS, SKIP_REASONS, CLASSES, ClassLiteral, SkipReasonLiteral, LedgerError, state_root,
required_steps, permitted_skip_reasons, parse_ledger, evaluate, render_lines, check_lines, main.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Literal, Optional, Tuple

# Shared leaf module (log.md attempt-start parsing and ISO-8601); same fallback as flow-verify-receipt.py.
try:
    import flow_log
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    import flow_log

STEP_IDS: Tuple[str, ...] = (
    "round1",
    "integration-gate",
    "round-sizing",
    "pause",
    "usage-check-round1-join",
    "fanout",
    "round2-apply",
    "partc-dedup",
    "partc-reference-check",
    "partc-mutation-a",
    "partc-mutation-b",
    "round3-followup",
    "round4",
    "usage-check-final",
)
ClassLiteral = Literal["mechanical", "test-only", "full"]
SkipReasonLiteral = Literal[
    "mechanical",
    "test-only",
    "no-test-files-changed",
    "dirty-file",
    "no-comparison-operator",
    "no-new-tests",
    "pause-not-requested",
]
SKIP_REASONS: FrozenSet[str] = frozenset(
    {
        "mechanical",
        "test-only",
        "no-test-files-changed",
        "dirty-file",
        "no-comparison-operator",
        "no-new-tests",
        "pause-not-requested",
    }
)
CLASSES: Tuple[str, ...] = ("mechanical", "test-only", "full")

PAUSE_ROWS: FrozenSet[str] = frozenset({"pause", "usage-check-round1-join"})
MECHANICAL_ROWS: FrozenSet[str] = frozenset({"round1", "integration-gate", "round-sizing", "usage-check-final"})
ROUND2_APPLY = "round2-apply"
FURTHER_SKIPS: Dict[str, FrozenSet[str]] = {
    "round2-apply": frozenset({"no-new-tests"}),
    "round4": frozenset({"no-test-files-changed"}),
    "partc-dedup": frozenset({"no-new-tests"}),
    "partc-reference-check": frozenset({"no-new-tests"}),
    "partc-mutation-a": frozenset({"dirty-file", "no-new-tests"}),
    "partc-mutation-b": frozenset({"dirty-file", "no-comparison-operator", "no-new-tests"}),
}

COMMAND_NAME = "implement-with-haiku"
LEDGER_NAME = "ledger.md"
POINTER_SUFFIX = ".pointer"
FINISHED_SUFFIX = ".finished"
SUPERSEDED_SUFFIX = ".superseded"
TOMBSTONE_TTL_DAYS = 14
# Clock skew between log.md's `date -u` and created_epoch (int(time.time())) tolerated by the bounded scan.
SINCE_SKEW_SECS = 5
RUN_METRICS = Path(__file__).resolve().parent / "run-metrics.py"

SHA_RE = re.compile(r"[0-9a-fA-F]{7,40}")
CARRIED_RE = re.compile(r"^carried:([0-9a-fA-F]{7,40})\.\.([0-9a-fA-F]{7,40})$")
RUN_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
ROW_RE = re.compile(r"^(\S+) (?:(DONE) (\S+) (.+)|(SKIPPED) (\S+) (\S+)|(FINISHED))$")
CLASS_RE = re.compile(r"^class=(mechanical|test-only|full)(?![\w-])")


class LedgerError(Exception):
    """Raised for invalid arguments, unreadable state, or an unusable pointer."""


def state_root() -> Path:
    """Directory holding pointer and tombstone files (IMPLEMENT_LEDGER_STATE_DIR overrides, for tests)."""
    override = os.environ.get("IMPLEMENT_LEDGER_STATE_DIR")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~/.claude/state/implement-with-haiku"))


def env_session() -> Optional[str]:
    """CLAUDE_CODE_SESSION_ID, or None when unset or empty."""
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or None


def git_toplevel() -> str:
    """Repo toplevel of the current directory; falls back to the cwd outside a git repo."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=10
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return str(Path.cwd().resolve())


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def required_steps(cls: str, pause: bool) -> FrozenSet[str]:
    """Rows that must be DONE (or permitted-SKIPPED where the matrix allows) for a class."""
    if cls == "mechanical":
        base = set(MECHANICAL_ROWS)
    elif cls == "test-only":
        base = set(STEP_IDS) - {ROUND2_APPLY}
    else:
        base = set(STEP_IDS)
    if pause:
        base |= PAUSE_ROWS
    else:
        base -= PAUSE_ROWS
    return frozenset(base)


def permitted_skip_reasons(cls: str, step: str, pause: bool) -> FrozenSet[str]:
    """Reasons under which a SKIPPED row for step is acceptable for a class. Single source of the skip matrix."""
    reasons = set()
    if step not in required_steps(cls, pause):
        if cls == "mechanical":
            reasons.add("mechanical")
        if cls == "test-only" and step == ROUND2_APPLY:
            reasons.add("test-only")
        if step in PAUSE_ROWS and not pause:
            reasons.add("pause-not-requested")
    if cls in ("test-only", "full"):
        reasons |= FURTHER_SKIPS.get(step, frozenset())
    return frozenset(reasons)


def _check_evidence_file(evidence: str, scratch: Path) -> Optional[str]:
    """Return an error string unless evidence is an absolute, non-empty file under scratch."""
    if not Path(evidence).is_absolute():
        return "evidence path is not absolute"
    real = os.path.realpath(evidence)
    try:
        Path(real).relative_to(os.path.realpath(str(scratch)))
    except ValueError:
        return "evidence path is outside the scratch dir"
    try:
        text = Path(real).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "evidence file is missing or unreadable"
    if not text.strip():
        return "evidence file is empty"
    return None


def _parse_row(line: str, lineno: int, run_key: str, scratch: Path) -> Dict[str, Any]:
    entry: Dict[str, Any] = {"lineno": lineno, "text": line, "step": None, "kind": None, "value": None, "error": None}
    match = ROW_RE.match(line)
    if not match:
        entry["error"] = "bad syntax"
        return entry
    key = match.group(1)
    if match.group(8):  # FINISHED
        if key == run_key:
            entry["kind"] = "finished"
        else:
            entry["error"] = "foreign run key"
        return entry
    if key != run_key:
        entry["error"] = "foreign run key"
        return entry
    if match.group(2):  # DONE
        step, evidence = match.group(3), match.group(4)
        entry.update(step=step, kind="done", value=evidence)
        if step not in STEP_IDS:
            entry["error"] = f"unknown step {step!r}"
        elif evidence.startswith("carried:"):
            if step != "round1":
                entry["error"] = "carried evidence is only valid for round1"
            elif not CARRIED_RE.match(evidence):
                entry["error"] = "carried evidence must be carried:<A>..<B> with 7-40 hex SHAs"
        else:
            entry["error"] = _check_evidence_file(evidence, scratch)
    else:  # SKIPPED
        step, reason = match.group(6), match.group(7)
        entry.update(step=step, kind="skip", value=reason)
        if step not in STEP_IDS:
            entry["error"] = f"unknown step {step!r}"
        elif reason not in SKIP_REASONS:
            entry["error"] = f"unknown skip reason {reason!r}"
    return entry


def parse_ledger(ledger: Path, run_key: str, scratch: Path) -> Tuple[List[Dict[str, Any]], bool]:
    """Parse every row of a ledger. Returns (entries, finished). A missing ledger yields no entries."""
    try:
        text = ledger.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], False
    except (OSError, UnicodeDecodeError) as e:
        raise LedgerError(f"cannot read ledger {ledger}: {e}")
    entries: List[Dict[str, Any]] = []
    finished = False
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        entry = _parse_row(line, lineno, run_key, scratch)
        if entry["kind"] == "finished":
            finished = True
            continue
        entries.append(entry)
    return entries, finished


def _read_class(evidence: Path) -> str:
    """Class from a round-sizing evidence file; anything unreadable or unrecognised is full (fail closed)."""
    try:
        text = evidence.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "full"
    match = CLASS_RE.search(text)
    return match.group(1) if match else "full"


def _first_line(evidence: str) -> str:
    """Display text for a DONE row: the carried token, or the first non-blank line of the evidence file."""
    if evidence.startswith("carried:"):
        return evidence
    try:
        text = Path(evidence).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "(evidence unreadable)"
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return "(evidence empty)"


def evaluate(data: Dict[str, Any], ledger: Path, run_key: str) -> Dict[str, Any]:
    """Judge a ledger against the skip matrix. Pure read; never writes."""
    scratch = Path(data["scratch"])
    pause = bool(data.get("pause"))
    entries, finished = parse_ledger(ledger, run_key, scratch)

    round_sizing = [
        e for e in entries if e["step"] == "round-sizing" and e["kind"] == "done" and e["error"] is None
    ]
    cls = _read_class(Path(round_sizing[-1]["value"])) if round_sizing else "full"
    required = required_steps(cls, pause)

    problems = [f"line {e['lineno']}: {e['error']}" for e in entries if e["error"]]
    rows: Dict[str, Tuple[str, Optional[str]]] = {}
    missing: List[str] = []
    for step in STEP_IDS:
        mine = [e for e in entries if e["step"] == step]
        ok = [e for e in mine if e["error"] is None]
        dones = [e for e in ok if e["kind"] == "done"]
        skips = [e for e in ok if e["kind"] == "skip"]
        permitted = permitted_skip_reasons(cls, step, pause)
        for e in skips:
            if e["value"] not in permitted:
                problems.append(f"line {e['lineno']}: SKIPPED {step} {e['value']} is not permitted for class {cls}")
        allowed = [e for e in skips if e["value"] in permitted]
        if dones:
            rows[step] = ("done", _first_line(dones[-1]["value"]))
        elif allowed:
            rows[step] = ("skipped", allowed[-1]["value"])
        elif skips:
            rows[step] = ("forbidden", skips[-1]["value"])
        elif mine:
            rows[step] = ("malformed", None)
        else:
            rows[step] = ("absent", None)
        if step in required and rows[step][0] not in ("done", "skipped"):
            missing.append(step)

    return {
        "run_key": run_key,
        "class": cls,
        "pause": pause,
        "rows": rows,
        "missing": missing,
        "problems": problems,
        "complete": not missing and not problems,
        "finished": finished,
    }


def render_lines(ev: Dict[str, Any]) -> List[str]:
    """Summary block: one line per row, in STEP_IDS order."""
    required = required_steps(ev["class"], ev["pause"])
    out = [f"RUN_KEY: {ev['run_key']}", f"CLASS: {ev['class']} PAUSE: {'yes' if ev['pause'] else 'no'}"]
    for step in STEP_IDS:
        status, detail = ev["rows"][step]
        req = step in required
        if status == "done":
            text = detail or ""
        elif status == "skipped":
            text = f"skipped ({detail})"
        elif status == "forbidden":
            text = f"NOT RUN (SKIPPED {detail} not permitted)" if req else f"NOT PERMITTED (SKIPPED {detail})"
        elif status == "malformed":
            text = "NOT RUN (malformed row)" if req else "MALFORMED row"
        else:
            text = "NOT RUN" if req else f"skipped ({ev['class']})"
        out.append(f"{step}: {text}")
    return out


def check_lines(ev: Dict[str, Any]) -> List[str]:
    """Verdict plus every missing row and problem."""
    out = [f"CHECK: {'COMPLETE' if ev['complete'] else 'INCOMPLETE'}"]
    out += [f"MISSING: {step}" for step in ev["missing"]]
    out += [f"PROBLEM: {problem}" for problem in ev["problems"]]
    return out


def _read_record(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise LedgerError(f"cannot read pointer {path}: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("scratch"), str) or not isinstance(data.get("run_key"), str):
        raise LedgerError(f"malformed pointer {path}")
    stem = path.name.rsplit(".", 1)[0]
    if data["run_key"] != stem:
        raise LedgerError(f"pointer {path} names run key {data['run_key']!r}")
    return data


def _scan_records(root: Path, warn_unreadable: bool = False) -> Tuple[List[Tuple[Path, str, Dict[str, Any]]], List[Path]]:
    """(path, kind, data) for each readable .pointer (kind 'pointer') or .finished (kind 'finished').

    Returns (records, unreadable_paths). If warn_unreadable is True and current session's pointer
    is among unreadable records, a warning is collected (handled by caller).
    """
    out: List[Tuple[Path, str, Dict[str, Any]]] = []
    unreadable: List[Path] = []
    if not root.is_dir():
        return out, unreadable
    for path in root.iterdir():
        if path.name.endswith(POINTER_SUFFIX):
            kind = "pointer"
        elif path.name.endswith(FINISHED_SUFFIX):
            kind = "finished"
        else:
            continue
        try:
            out.append((path, kind, _read_record(path)))
        except LedgerError:
            unreadable.append(path)
    return out, unreadable


def _created_key(path: Path, data: Dict[str, Any]) -> float:
    created = data.get("created_epoch")
    if isinstance(created, (int, float)) and not isinstance(created, bool):
        return float(created)
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _scan_since(
    root: Path, repo: Optional[str], since: float
) -> Tuple[Optional[Tuple[Path, bool, Dict[str, Any]]], int, int]:
    """One bounded, repo-gated scan over pointers and tombstones. Returns (found, unreadable, no_epoch).

    A record is eligible only when its repo equals repo, its created_epoch is an int or float (not a
    bool), and created_epoch >= since - SINCE_SKEW_SECS. The newest eligible record wins. Session is
    ignored. unreadable counts every unreadable record under root. no_epoch counts records of this repo
    that lack a numeric created_epoch (the mtime fallback is never used here). repo None yields no match.
    """
    if repo is None:
        return None, 0, 0
    records, unreadable = _scan_records(root)
    floor = since - SINCE_SKEW_SECS
    no_epoch = 0
    best: Optional[Tuple[Path, str, Dict[str, Any]]] = None
    best_epoch = 0.0
    for path, kind, data in records:
        if data.get("repo") != repo:
            continue
        created = data.get("created_epoch")
        if not isinstance(created, (int, float)) or isinstance(created, bool):
            no_epoch += 1
            continue
        if created < floor:
            continue
        if best is None or created > best_epoch:
            best, best_epoch = (path, kind, data), float(created)
    found = None if best is None else (best[0], best[1] == "finished", best[2])
    return found, len(unreadable), no_epoch


def locate_record(
    root: Path,
    session: Optional[str],
    repo: Optional[str],
    run_key: Optional[str],
    since: Optional[float] = None,
) -> Optional[Tuple[Path, bool, Dict[str, Any]]]:
    """Find a run's pointer. Returns (path, is_tombstone, data) or None.

    Resolution contract:
    - run_key given: the exact pointer, or else its tombstone, by name only. The repo filter is NOT
      applied here; the caller (_resolve, cmd_locate) applies it to prevent cross-repo resolution.
    - since None, no run_key: (1) newest non-superseded record for the session, then (2) newest
      unfinished pointer whose repo matches. A record from another repo is never resolved (pointers and
      tombstones alike) when repo is given. _resolve (check, render, finish) always takes this path.
    - since given (epoch seconds UTC), no run_key: one bounded scan that replaces passes 1-2. Pointers
      and tombstones are both eligible. The comparison is created_epoch >= since - 5 (SINCE_SKEW_SECS).
      Records without a numeric created_epoch are ineligible. repo is required and session is ignored,
      so a finished run from another session is found. Without a bound, a tombstone from another
      session is not found (the bound is mandatory for that fallback; see W1).
    - The bound comes from the newest `<STEP> begin|retry` line in the flow's log.md (see
      scripts/flow_log.py), so it is always produced by code and never copied by the model.
    """
    if run_key is None and since is not None:
        found, _unreadable, _no_epoch = _scan_since(root, repo, since)
        return found
    if run_key is not None:
        if not RUN_KEY_RE.match(run_key):
            raise LedgerError(f"invalid run key {run_key!r}")
        for suffix in (POINTER_SUFFIX, FINISHED_SUFFIX):
            path = root / f"{run_key}{suffix}"
            if path.is_file():
                data = _read_record(path)
                # When --run-key is supplied, ignore repo filter (F12)
                return path, suffix == FINISHED_SUFFIX, data
        return None
    records, _unreadable = _scan_records(root)
    records.sort(key=lambda r: _created_key(r[0], r[2]), reverse=True)
    for path, kind, data in records:
        if session is not None and data.get("session_id") == session and (repo is None or data.get("repo") == repo):
            return path, kind == "finished", data
    for path, kind, data in records:
        if kind == "pointer" and repo is not None and data.get("repo") == repo:
            return path, False, data
    return None


def _resolve(run_key: Optional[str], repo: Optional[str] = None) -> Optional[Tuple[Path, bool, Dict[str, Any]]]:
    """Exact pointer for run_key, else the same session-then-repo fallback that locate uses.

    F9: Never match "unknown" session without --run-key; require --run-key for finish/check/render
    when session is unknown.
    F12: Repo filter is applied: a pointer/tombstone from another repo is not resolved.
    """
    repo = repo or git_toplevel()
    if run_key is not None:
        result = locate_record(state_root(), None, repo, run_key)
        # Verify result is from the current repo (F12: don't cross repos)
        if result is not None:
            path, is_tombstone, data = result
            if data.get("repo") != repo:
                return None
        return result
    session = env_session()
    if session is None:
        return None  # F9: require --run-key when session is unknown
    return locate_record(state_root(), session, repo, None)


def _sha_or_error(value: str, what: str) -> str:
    if not SHA_RE.fullmatch(value):
        raise LedgerError(f"{what} must be 7-40 hex characters, got {value!r}")
    return value


def cmd_init(args: argparse.Namespace) -> int:
    root = state_root()
    scratch_arg = os.path.expanduser(args.scratch)
    if not os.path.isabs(scratch_arg):
        raise LedgerError("--scratch must be an absolute path")
    scratch = os.path.abspath(scratch_arg)
    start_sha = _sha_or_error(args.start_sha, "--start-sha")
    sha12 = start_sha[:12]
    explicit_session = args.session_id or env_session()
    epoch = int(time.time())
    if args.run_key:
        run_key = args.run_key
    elif explicit_session:
        run_key = f"{explicit_session}-{sha12}"
    else:
        run_key = f"unknown-{sha12}-{epoch}"
    if not RUN_KEY_RE.match(run_key):
        raise LedgerError(f"invalid run key {run_key!r}")
    if args.carried is not None and not CARRIED_RE.match(f"carried:{args.carried}"):
        raise LedgerError("--carried must be A..B with two 7-40 hex SHAs")

    toplevel = git_toplevel()
    session_label = explicit_session or "unknown"
    root.mkdir(parents=True, exist_ok=True)
    os.makedirs(scratch, exist_ok=True)
    ledger = Path(scratch) / LEDGER_NAME
    pointer = root / f"{run_key}{POINTER_SUFFIX}"
    if ledger.exists() or pointer.exists():
        raise LedgerError(f"refusing to overwrite an existing ledger or pointer for run key {run_key}")

    # F8: Create ledger and pointer first, then supersede old pointers on success.
    # F18: Clean up expired tombstones and stale pointers (they're not part of the new run).
    cutoff = time.time() - TOMBSTONE_TTL_DAYS * 86400
    for path in root.iterdir():
        if path.name.endswith((FINISHED_SUFFIX, SUPERSEDED_SUFFIX, POINTER_SUFFIX)):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass

    # Create the new ledger and pointer before superseding old pointers.
    lines = [f"# implement-with-haiku ledger run-key={run_key}"]
    if args.carried is not None:
        lines.append(f"{run_key} DONE round1 carried:{args.carried}")
    with open(ledger, "x", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    record = {
        "run_key": run_key,
        "session_id": session_label,
        "scratch": scratch,
        "repo": toplevel,
        "start_sha": start_sha,
        "pause": bool(args.pause),
        "resumed_from": args.resumed_from or None,
        "created": _now_iso(),
        "created_epoch": epoch,
    }
    tmp = root / f".{run_key}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(pointer))

    # Now that the new run is durable, supersede old pointers for this session/repo.
    records, _unreadable = _scan_records(root)
    for path, kind, data in records:
        if kind == "pointer" and path != pointer and data.get("session_id") == session_label and data.get("repo") == toplevel:
            stem = path.name[: -len(POINTER_SUFFIX)]
            os.replace(str(path), str(root / f"{stem}{SUPERSEDED_SUFFIX}"))

    print(f"RUN_KEY: {run_key}")
    print(f"LEDGER: {ledger}")
    print(f"POINTER: {pointer}")
    return 0


def cmd_locate(args: argparse.Namespace) -> int:
    """Locate the run. Unbounded (no --since/--flow-dir) keeps today's passes; a bound uses one scan.

    `already finished` means the record found is a tombstone. A bound derived from --flow-dir/--step
    is printed as `SINCE:`. Skip counts go to stderr only when non-zero. Any failure to derive or match
    the bound exits 3 (reported as "ledger unavailable" by the Fidelity audit).
    """
    root = state_root()
    repo = args.repo or git_toplevel()
    since_iso: Optional[str] = None
    if args.flow_dir is not None:
        try:
            flow_dir = flow_log.validate_flow_dir(args.flow_dir)
            since_iso = flow_log.newest_attempt_start(flow_dir / "log.md", args.step)
        except flow_log.FlowLogError as e:
            print(f"NOT FOUND: {e}", file=sys.stderr)
            return 3
        if since_iso is None:
            print(f"NOT FOUND: no begin or retry line for {args.step}", file=sys.stderr)
            return 3
    elif args.since is not None:
        since_iso = args.since

    if since_iso is not None:
        try:
            since_epoch = flow_log.parse_iso8601(since_iso)
        except ValueError as e:
            print(f"NOT FOUND: {e}", file=sys.stderr)
            return 3
        found, unreadable, no_epoch = _scan_since(root, repo, since_epoch)
        if unreadable or no_epoch:
            print(
                f"locate: skipped {unreadable} unreadable, {no_epoch} without created_epoch",
                file=sys.stderr,
            )
    else:
        found = locate_record(root, env_session() or "unknown", repo, args.run_key)
    if found is None:
        print("NOT FOUND: no implement-with-haiku run pointer", file=sys.stderr)
        return 3
    _path, finished, data = found
    ledger = Path(data["scratch"]) / LEDGER_NAME
    ev = evaluate(data, ledger, data["run_key"])
    if finished:
        print("already finished")
    if since_iso is not None:
        print(f"SINCE: {since_iso}")
    print(f"SCRATCH: {data['scratch']}")
    print(f"LEDGER: {ledger}")
    print(f"RUN_KEY: {data['run_key']}")
    first = ev["missing"][0] if ev["missing"] else "none"
    print(f"FIRST_MISSING: {first}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    found = _resolve(args.run_key)
    if found is None:
        print("CHECK: INCOMPLETE (no pointer found for this run)")
        return 1
    _path, _finished, data = found
    ev = evaluate(data, Path(data["scratch"]) / LEDGER_NAME, data["run_key"])
    for line in check_lines(ev):
        print(line)
    return 0 if ev["complete"] else 1


def cmd_render(args: argparse.Namespace) -> int:
    found = _resolve(args.run_key)
    if found is None:
        print("NOT FOUND: no implement-with-haiku run pointer", file=sys.stderr)
        return 3
    _path, _finished, data = found
    ev = evaluate(data, Path(data["scratch"]) / LEDGER_NAME, data["run_key"])
    for line in render_lines(ev):
        print(line)
    return 0


def _emit_command_end(log: Optional[str], state_dir: Optional[str], success: bool) -> bool:
    """Best-effort command-end emission through run-metrics.py. Errors go to stderr; returns True on success."""
    cmd = [sys.executable, str(RUN_METRICS)]
    if log:
        cmd += ["--log", log]
    if state_dir:
        cmd += ["--state-dir", state_dir]
    cmd += ["command-end", "--command", COMMAND_NAME]
    if success:
        cmd += ["--outcome", "success"]
    else:
        cmd += ["--outcome", "failure", "--failure-class", "steps_skipped"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"finish: command-end emission failed: {e}", file=sys.stderr)
        return False
    if proc.returncode != 0:
        print(f"finish: command-end emission exited {proc.returncode}: {proc.stderr.strip()}", file=sys.stderr)
        return False
    return True


def _replace_suffix(path: Path, old: str, new: str) -> Path:
    return path.with_name(path.name[: -len(old)] + new)


def _mark_finished(pointer: Path, ledger: Path, run_key: str) -> None:
    """F23: Extract helper — write FINISHED row to ledger and tombstone pointer. Atomic per run.

    Raises OSError if either write fails. Must only be called after command.end is emitted.
    """
    with open(ledger, "a", encoding="utf-8") as f:
        f.write(f"{run_key} FINISHED\n")
    if pointer.name.endswith(POINTER_SUFFIX):
        os.replace(str(pointer), str(_replace_suffix(pointer, POINTER_SUFFIX, FINISHED_SUFFIX)))


def cmd_finish(args: argparse.Namespace) -> int:
    # CLUSTER A (F1, F2, F7, F17): exactly-once per run semantics
    # 1. Check if already tombstoned (finished) before any emit
    # 2. Incomplete first `finish` does not emit (doc calls `check` first)
    # 3. Guard post-emit I/O with try/except
    try:
        found = _resolve(args.run_key)
    except LedgerError as e:
        print(f"finish: {e}", file=sys.stderr)
        try:
            _emit_command_end(args.log, args.state_dir, False)
        except (LedgerError, OSError):
            pass  # F19: catch both in finish OSError paths
        return 2
    if found is None:
        print("finish: no pointer or tombstone found for this run; recording steps_skipped", file=sys.stderr)
        try:
            _emit_command_end(args.log, args.state_dir, False)
        except (LedgerError, OSError):
            pass  # F19
        return 3
    pointer, finished, data = found

    # F2: tombstone-first no-op before any emit. If pointer is already finished, return immediately.
    if finished:
        print("already finished")
        return 0

    ledger = Path(data["scratch"]) / LEDGER_NAME
    run_key = data["run_key"]
    try:
        if not ledger.is_file():
            raise LedgerError(f"ledger missing: {ledger}")
        ev = evaluate(data, ledger, run_key)
    except Exception as e:  # fail closed: any check failure is steps_skipped
        print(f"finish: check failed: {e}", file=sys.stderr)
        try:
            _emit_command_end(args.log, args.state_dir, False)
        except (LedgerError, OSError):
            pass  # F19
        return 1

    # The completeness gate runs before any FINISHED marker is written.
    for line in render_lines(ev):
        print(line)
    for line in check_lines(ev):
        print(line)

    # Fail closed: an incomplete run records steps_skipped (ticket #259) and keeps its pointer so the
    # run can be completed and finished again. This must precede the `finished` branch below, so a
    # hand-appended FINISHED row cannot bypass the completeness gate.
    if not ev["complete"]:
        _emit_command_end(args.log, args.state_dir, False)
        return 1

    if ev["finished"]:
        # An earlier finish wrote FINISHED (after its success event) but did not tombstone the pointer.
        # Only the tombstone move remains; no second command.end is emitted.
        try:
            _mark_finished(pointer, ledger, run_key)
        except OSError as e:
            print(f"finish: failed to tombstone pointer after FINISHED row: {e}", file=sys.stderr)
            return 1
        print("already finished")
        return 0

    # F1, F2, F7: Only emit once per run. Guard post-emit I/O with try/except (CLUSTER A).
    if not _emit_command_end(args.log, args.state_dir, True):
        print("finish: success event not recorded; FINISHED not written and the run stays open", file=sys.stderr)
        return 1

    try:
        _mark_finished(pointer, ledger, run_key)
    except OSError as e:
        print(f"finish: failed to write FINISHED marker: {e}", file=sys.stderr)
        return 1

    print("FINISHED")
    return 0


def _iso_utc_arg(value: str) -> str:
    """argparse type for --since: an ISO-8601 UTC Z timestamp. Naive or malformed values are rejected."""
    if not flow_log.ISO_UTC_Z_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(f"--since must be ISO-8601 UTC like 2026-10-19T12:00:00Z, got {value!r}")
    try:
        flow_log.parse_iso8601(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--since is not a valid timestamp: {value!r}")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="implement-ledger.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command")
    sub.required = True

    p_init = sub.add_parser("init", help="create a run ledger and pointer")
    p_init.add_argument("--scratch", required=True)
    p_init.add_argument("--start-sha", required=True, dest="start_sha")
    p_init.add_argument("--pause", action="store_true")
    p_init.add_argument("--resumed-from", default=None, dest="resumed_from")
    p_init.add_argument("--carried", default=None, help="A..B; seeds round1 as carried")
    p_init.add_argument("--session-id", default=None, dest="session_id")
    p_init.add_argument("--run-key", default=None, dest="run_key")

    p_locate = sub.add_parser("locate", help="find the current run's scratch and ledger")
    p_locate.add_argument("--repo", default=None)
    bound = p_locate.add_mutually_exclusive_group()
    bound.add_argument("--run-key", default=None, dest="run_key")
    bound.add_argument(
        "--since",
        default=None,
        type=_iso_utc_arg,
        help="ISO-8601 UTC (Z) bound in epoch seconds; one bounded scan that also finds tombstones",
    )
    bound.add_argument(
        "--flow-dir",
        default=None,
        dest="flow_dir",
        help="expert-flow FLOW_DIR; the bound is the newest --step begin|retry line in its log.md",
    )
    p_locate.add_argument(
        "--step",
        default=None,
        choices=["implement", "fix"],
        help="step whose attempt start bounds the scan (required with --flow-dir, rejected otherwise)",
    )

    p_check = sub.add_parser("check", help="verify the ledger is complete")
    p_check.add_argument("--run-key", default=None, dest="run_key")

    p_render = sub.add_parser("render", help="print the summary block")
    p_render.add_argument("--run-key", default=None, dest="run_key")

    p_finish = sub.add_parser("finish", help="check, record command-end, and tombstone the run")
    p_finish.add_argument("--run-key", default=None, dest="run_key")
    p_finish.add_argument("--log", default=None)
    p_finish.add_argument("--state-dir", default=None, dest="state_dir")
    return parser


def main(argv: List[str]) -> int:
    """Entry point. argv is sys.argv (argv[0] is the program name)."""
    parser = _build_parser()
    args = parser.parse_args(argv[1:])
    if args.command == "locate":
        if args.flow_dir is not None and args.step is None:
            parser.error("locate: --step is required with --flow-dir")
        if args.flow_dir is None and args.step is not None:
            parser.error("locate: --step is only valid with --flow-dir")
    handlers = {
        "init": cmd_init,
        "locate": cmd_locate,
        "check": cmd_check,
        "render": cmd_render,
        "finish": cmd_finish,
    }
    try:
        return handlers[args.command](args)
    except LedgerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
