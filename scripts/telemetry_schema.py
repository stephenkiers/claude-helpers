#!/usr/bin/env python3
"""
Single source of truth for telemetry event schema and serialization.

Provides event builders, validators, and persistence helpers — ensuring every tool
that writes telemetry (run-metrics, claude-transcript-metrics) uses the same schema
and encoding. Never duplicates logic across writers.
"""

import fcntl
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, NamedTuple, Optional, TypedDict, Union, get_args


class CommandStateEntry(TypedDict, total=False):
    """One entry in SessionState.commands: command/stage state for a single command lifecycle.

    All fields are optional. session_id is stored on every entry (not just top-level) so
    a mismatch guard can verify that the entry belongs to the current session before
    reading, clearing, or resolving it — mirrors AgentBeganAtEntry's session_id guard.

    command and command_began_at are set by command-begin. stage_id, stage, and
    stage_began_at are set by stage-begin and cleared by stage-end.

    _monotonic_ns is a strictly monotonic nanosecond timestamp used as a tiebreaker
    when command_began_at or stage_began_at timestamps are equal (ensuring stable
    LIFO ordering even under timestamp ties).
    """
    session_id: Optional[str]
    command: Optional[str]
    command_began_at: Optional[str]
    stage_id: Optional[str]
    stage: Optional[str]
    stage_began_at: Optional[str]
    _monotonic_ns: Optional[int]


class SessionState(TypedDict, total=False):
    """Typed representation of session-scoped state dict.

    Stores a dict of command lifecycle entries keyed by command_id. The reserved
    "unknown" entry key holds stage activity (an open stage) that has no owning command
    (a stage-begin with no prior command-begin — legal today and must stay legal).
    The "unknown" entry's command field is None; a name-matching command-end can never
    adopt it.

    Each entry is a CommandStateEntry, storing session_id on every entry (not just
    top-level) so mutation and resolution can verify session_id matches before
    reading/clearing.

    Note: session_began_at and the per-agent began_at map live in a SEPARATE file
    (see session_meta_path) rather than here, because command state entries have
    different lifetimes from session/agent timing — the session and agents must survive
    a command-end, but a command state entry is popped when its command ends.
    """
    commands: dict[str, CommandStateEntry]


class AgentBeganAtEntry(TypedDict):
    """One entry in SessionMeta.agents: an agent's begin timestamp plus the session_id
    it was recorded under, so resolve_and_clear_agent_began_at can require a genuine
    session_id match before returning/clearing it (mirrors the command/stage path's
    CAS guard)."""
    session_id: str
    began_at: str


class CommandEndResolution(NamedTuple):
    """Return type for resolve_and_clear_command_state."""
    command_id: str
    state_mismatch: Optional[bool]
    cleared: bool
    began_at: Optional[str]


class StageEndResolution(NamedTuple):
    """Return type for resolve_and_clear_stage_state."""
    command_id: str
    stage_id: str
    state_mismatch: Optional[bool]
    cleared: bool
    began_at: Optional[str]


class AgentUsageEntry(TypedDict, total=False):
    """One entry in UsageState.agents: usage tokens and metadata for a single agent."""
    session_id: str
    status: Literal["counted", "unparseable", "no_transcript_path", "path_not_a_file", "parse_raised", "parsed_empty", "session-mismatch"]
    counted_tokens: Optional[int]
    tokens: dict
    token_confidence: Optional[str]
    recorded_at: str


class ActiveRun(TypedDict, total=False):
    """The current /implement-with-haiku run's budget baseline.

    Minted fresh every time seam="round1-join" is checked, so a re-invocation
    in the same Claude Code session starts a fresh budget (decision 13) instead
    of inheriting the whole session's cumulative usage.
    """
    run_id: str
    started_at: str
    baseline_counted_tokens: int


class UsageState(TypedDict, total=False):
    """Usage tracking state, stored under SessionMeta.usage.

    Separate from the begin/end timing map (SessionMeta.agents) — tracks
    token accounting for each agent in the session.
    """
    session_id: Optional[str]
    counted_tokens: int
    unaccounted_no_agent_id_tokens: int
    last_reported_crossing_at_tokens: Optional[int]
    last_reported_floor_count: int
    seams_checked: list
    agents: dict[str, AgentUsageEntry]
    active_run: ActiveRun


class SessionMeta(TypedDict, total=False):
    """Typed representation of the session-scoped meta state dict (session_meta_path).

    Kept separate from SessionState (state_path) because that file is deleted
    wholesale by command-end; session/agent timing must survive command lifecycles.
    """
    session_id: Optional[str]
    session_began_at: Optional[str]
    agents: dict[str, AgentBeganAtEntry]
    usage: UsageState


class FindingsCounts(TypedDict, total=False):
    """Typed representation of the findings event field.

    All fields optional; a stage may report only the subset that applies.
    """
    produced: int
    accepted: int
    unique: int
    rejected: int
    acted_upon: int


class ChecksCounts(TypedDict, total=False):
    """Typed representation of the checks event field.

    All fields optional; a stage may report only the subset that applies.
    """
    executed: int
    passed: int


SCHEMA_VERSION = 1
UNKNOWN = "unknown"

EVENT_TYPES = frozenset({
    "session.begin",
    "session.end",
    "command.begin",
    "command.end",
    "stage.begin",
    "stage.end",
    "agent.begin",
    "agent.end",
})

OUTCOME_STATUSES = frozenset({"success", "failure", "interrupted"})
FAILURE_CLASSES = frozenset({"timeout", "api_error", "test_failure", "guard_block", "other"})
EFFORT_LEVELS = frozenset({"1", "2", "3", "4", "5"})
RUN_MODES = frozenset({"local", "pr", "coworker"})
FINDINGS_KEYS = frozenset(FindingsCounts.__annotations__.keys())
CHECKS_KEYS = frozenset(ChecksCounts.__annotations__.keys())

COMMAND_STATE_MAX_AGE_SECONDS = 12 * 60 * 60  # 12h — matches diagnose's stale/recent split
COMMAND_STATE_MAX_ENTRIES = 16

COUNTED_TOKEN_KEYS = ("input", "output", "cache_creation")
THRESHOLD_STATES = frozenset({"under-threshold", "over-threshold-unreported", "over-threshold-reported", "unavailable", "session-mismatch"})
ThresholdState = Literal["under-threshold", "over-threshold-unreported", "over-threshold-reported", "unavailable", "session-mismatch"]
USAGE_GATE_DEFAULT_THRESHOLD = 130_000
UNPARSEABLE_STATUSES = frozenset(
    s for s in get_args(AgentUsageEntry.__annotations__["status"])
    if s != "counted" and s != "session-mismatch"
)


def _parse_iso_or_none(timestamp_str: Optional[str]) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp string to a timezone-aware datetime, or None on parse error.

    Treats naive (timezone-unaware) datetimes as errors and returns None, since all
    telemetry timestamps must be timezone-aware for safe comparison.

    Args:
        timestamp_str: ISO 8601 timestamp string, or None

    Returns:
        A timezone-aware datetime object, or None if the string is falsy, unparseable, or naive.
    """
    if not timestamp_str:
        return None
    try:
        normalized = timestamp_str.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return None
        return parsed
    except (ValueError, TypeError, AttributeError):
        return None


def _load_command_entries(state: dict) -> dict[str, CommandStateEntry]:
    """Load command entries from state, migrating legacy flat format to new keyed dict.

    If state already has a "commands" key with a dict value, return it as-is.

    If "commands" key is present but not a dict (malformed), log a warning and treat
    as corrupted (return empty dict).

    Otherwise, convert a legacy flat state (top-level command_id/command/etc.) into
    a single entry keyed by command_id, or return an empty dict if neither format is found.

    This is called on every state read to ensure smooth migration from old code to new.
    """
    if "commands" in state:
        commands = state.get("commands")
        if isinstance(commands, dict):
            return commands
        else:
            # Malformed: "commands" key present but not a dict
            print(f"telemetry: state has malformed 'commands' field (not a dict): {type(commands).__name__}", file=sys.stderr)
            return {}

    # Legacy flat format: migrate to new dict format
    if state.get("command_id"):
        command_id = state["command_id"]
        entry: CommandStateEntry = {
            "session_id": state.get("session_id"),
            "command": state.get("command"),
            "command_began_at": state.get("command_began_at"),
            "stage_id": state.get("stage_id"),
            "stage": state.get("stage"),
            "stage_began_at": state.get("stage_began_at"),
        }
        return {command_id: entry}

    return {}


def _sort_by_began_at_desc(entries: list, ts_key: str) -> list:
    """Sort entries by timestamp field in descending order, with monotonic tiebreaker.

    Args:
        entries: list of (cid, entry) tuples to sort
        ts_key: timestamp field name (e.g., "command_began_at" or "stage_began_at")

    Returns:
        A new sorted list, ordered descending by timestamp, with _monotonic_ns as tiebreaker.
    """
    return sorted(entries, key=lambda x: (x[1].get(ts_key, ""), x[1].get("_monotonic_ns", 0)), reverse=True)


def _evict_expired(entries: dict[str, CommandStateEntry], keep_id: Optional[str], now: datetime) -> None:
    """Mutate entries in place, dropping expired/excess entries — never keep_id.

    Called after a mutate_fn has already resolved its own target entry, passing that
    entry's id as keep_id, so a call can never evict the entry it is about to read,
    set, or clear.

    Drops any entry (other than keep_id) whose command_began_at is older than
    COMMAND_STATE_MAX_AGE_SECONDS (12h, matching diagnose's stale/recent split), then
    enforces a hard cap of COMMAND_STATE_MAX_ENTRIES by evicting the oldest-begun
    entries first. An entry with a missing command_began_at (including the
    reserved "unknown" entry, whose command field is None) is treated as never-expiring
    by age but still counts toward the hard cap, sorting as oldest.
    """
    def began_at_epoch(entry: CommandStateEntry) -> float:
        parsed = _parse_iso_or_none(entry.get("command_began_at"))
        if parsed is None:
            return float("-inf")
        return parsed.timestamp()

    for cid in list(entries.keys()):
        if cid == keep_id:
            continue
        raw = entries[cid].get("command_began_at")
        if not raw:
            continue
        age_seconds = now.timestamp() - began_at_epoch(entries[cid])
        if age_seconds > COMMAND_STATE_MAX_AGE_SECONDS:
            del entries[cid]
            print(f"telemetry: evicted expired state entry for command_id={cid}", file=sys.stderr)

    if len(entries) > COMMAND_STATE_MAX_ENTRIES:
        evictable = sorted(
            (cid for cid in entries if cid != keep_id),
            key=lambda cid: began_at_epoch(entries[cid]),
        )
        overflow = len(entries) - COMMAND_STATE_MAX_ENTRIES
        for cid in evictable[:overflow]:
            del entries[cid]
            print(f"telemetry: evicted excess state entry for command_id={cid}", file=sys.stderr)


def outcome_success() -> dict:
    """Return a success outcome dict."""
    return {"status": "success"}


def outcome_failure(failure_class: str) -> dict:
    """Return a failure outcome dict.

    Raises ValueError if failure_class is not in FAILURE_CLASSES.
    """
    if failure_class not in FAILURE_CLASSES:
        raise ValueError(f"Invalid failure_class: {failure_class}")
    return {"status": "failure", "class": failure_class}


def outcome_interrupted() -> dict:
    """Return an interrupted outcome dict."""
    return {"status": "interrupted"}


def build_event(
    event_type,
    *,
    session_id,
    timestamp,
    command_id=None,
    resumed_from=None,
    stage_id=None,
    agent_id=None,
    repo=None,
    cwd=None,
    command=None,
    stage=None,
    agent_type=None,
    parent=None,
    model=None,
    tokens=None,
    turns=UNKNOWN,
    elapsed_seconds=UNKNOWN,
    retries=UNKNOWN,
    peak_concurrency=UNKNOWN,
    transcript_size=UNKNOWN,
    outcome=None,
    output_artifact_size=UNKNOWN,
    findings=None,
    checks=None,
    token_confidence=None,
    state_mismatch=None,
    effort=None,
    mode=None,
    reviewer_count=None,
) -> dict:
    """Build a well-formed telemetry event.

    Raises ValueError if event_type not in EVENT_TYPES, or if session_id/timestamp
    are falsy (both always required).

    tokens, if provided, must be a dict with keys a subset of
    {"input","output","cache_read","cache_creation"}. Any missing keys are filled
    with UNKNOWN in the output.

    Metric fields (turns, elapsed_seconds, retries, peak_concurrency, transcript_size,
    output_artifact_size) default to and keep the literal string "unknown" rather than
    being omitted, since we want missing metrics visible, not silently absent.

    state_mismatch is Optional[Literal[True]] — either True or None, never False.
    It is included in the output only if not None.

    effort, if provided, must be a string in EFFORT_LEVELS ({"1", "2", "3", "4", "5"}).

    mode, if provided, must be a string in RUN_MODES ({"local", "pr", "coworker"}).

    reviewer_count, if provided, must be a non-negative integer.

    resumed_from, if provided, must be a non-empty string (a command_id from a prior
    /implement-with-haiku run that was interrupted and is being resumed after /clear).
    It is included in the output only if not None.

    model, if provided, is an open-ended string tier (matches the project's
    --model haiku|sonnet|opus|fable convention elsewhere; other values may be added
    without schema changes).

    All other parameters are included in the output only if not None.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Invalid event_type: {event_type}")
    if not session_id:
        raise ValueError("session_id is required and must be non-empty")
    if not timestamp:
        raise ValueError("timestamp is required and must be non-empty")

    # Normalize tokens dict: fill missing keys with UNKNOWN
    normalized_tokens = None
    if tokens is not None:
        normalized_tokens = {}
        for key in ("input", "output", "cache_read", "cache_creation"):
            normalized_tokens[key] = tokens.get(key, UNKNOWN)

    # Build the event dict, always including required fields + metric fields
    event = {
        "schema_version": SCHEMA_VERSION,
        "event_type": event_type,
        "timestamp": timestamp,
        "session_id": session_id,
        "turns": turns,
        "elapsed_seconds": elapsed_seconds,
        "retries": retries,
        "peak_concurrency": peak_concurrency,
        "transcript_size": transcript_size,
        "output_artifact_size": output_artifact_size,
    }

    # Add optional correlation IDs and descriptive fields
    if command_id is not None:
        event["command_id"] = command_id
    if resumed_from is not None:
        event["resumed_from"] = resumed_from
    if stage_id is not None:
        event["stage_id"] = stage_id
    if agent_id is not None:
        event["agent_id"] = agent_id
    if repo is not None:
        event["repo"] = repo
    if cwd is not None:
        event["cwd"] = cwd
    if command is not None:
        event["command"] = command
    if stage is not None:
        event["stage"] = stage
    if agent_type is not None:
        event["agent_type"] = agent_type
    if parent is not None:
        event["parent"] = parent
    if model is not None:
        event["model"] = model
    if normalized_tokens is not None:
        event["tokens"] = normalized_tokens
    if outcome is not None:
        event["outcome"] = outcome
    if findings is not None:
        event["findings"] = findings
    if checks is not None:
        event["checks"] = checks
    if token_confidence is not None:
        event["token_confidence"] = token_confidence
    if state_mismatch is not None:
        event["state_mismatch"] = state_mismatch
    if effort is not None:
        event["effort"] = effort
    if mode is not None:
        event["mode"] = mode
    if reviewer_count is not None:
        event["reviewer_count"] = reviewer_count

    return event


def validate_event(event: dict) -> list:
    """Validate an event dict. Returns a list of error strings (empty = valid)."""
    errors = []

    # Check schema_version
    if "schema_version" not in event:
        errors.append("Missing schema_version")
    elif not isinstance(event["schema_version"], int) or event["schema_version"] != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}, got {event.get('schema_version')}")

    # Check event_type
    if "event_type" not in event:
        errors.append("Missing event_type")
    elif event.get("event_type") not in EVENT_TYPES:
        errors.append(f"event_type not in {EVENT_TYPES}, got {event.get('event_type')}")

    # Check timestamp (must be parseable via fromisoformat, allowing trailing Z)
    if "timestamp" not in event:
        errors.append("Missing timestamp")
    else:
        ts_str = event.get("timestamp", "")
        try:
            ts_normalized = ts_str.replace("Z", "+00:00") if isinstance(ts_str, str) else ts_str
            parsed_ts = datetime.fromisoformat(ts_normalized)
            # Timestamp must be timezone-aware
            if parsed_ts.tzinfo is None:
                errors.append(f"timestamp must be timezone-aware (include 'Z' or a UTC offset), got a naive timestamp: {ts_str}")
        except (ValueError, TypeError):
            errors.append(f"timestamp not parseable as ISO format: {ts_str}")

    # Check session_id
    if "session_id" not in event:
        errors.append("Missing session_id")
    elif not isinstance(event.get("session_id"), str) or not event.get("session_id"):
        errors.append("session_id must be a non-empty string")

    # Check command_id if present (must be non-empty string, not empty string)
    if "command_id" in event:
        command_id = event.get("command_id")
        if command_id == "":
            errors.append("command_id must not be an empty string; use 'unknown' instead")
        elif not isinstance(command_id, str):
            errors.append(f"command_id must be a string, got {type(command_id).__name__}")

    # Check resumed_from if present (must be non-empty string, not empty string)
    if "resumed_from" in event:
        resumed_from = event.get("resumed_from")
        if resumed_from == "":
            errors.append("resumed_from must not be an empty string")
        elif not isinstance(resumed_from, str):
            errors.append(f"resumed_from must be a string, got {type(resumed_from).__name__}")

    # Check stage_id if present (must be non-empty string, not empty string)
    if "stage_id" in event:
        stage_id = event.get("stage_id")
        if stage_id == "":
            errors.append("stage_id must not be an empty string; use 'unknown' instead")
        elif not isinstance(stage_id, str):
            errors.append(f"stage_id must be a string, got {type(stage_id).__name__}")

    # Check outcome if present
    if "outcome" in event:
        outcome = event.get("outcome")
        if not isinstance(outcome, dict):
            errors.append("outcome must be a dict")
        else:
            status = outcome.get("status")
            if status not in OUTCOME_STATUSES:
                errors.append(f"outcome.status not in {OUTCOME_STATUSES}, got {status}")
            elif status == "failure":
                if "class" not in outcome:
                    errors.append("outcome with status='failure' must have a 'class' field")
                elif outcome.get("class") not in FAILURE_CLASSES:
                    errors.append(f"outcome.class not in {FAILURE_CLASSES}, got {outcome.get('class')}")

    # Check findings if present: dict of non-negative ints, keys a subset of the allowed set
    if "findings" in event:
        findings = event.get("findings")
        if not isinstance(findings, dict):
            errors.append("findings must be a dict")
        else:
            for key, value in findings.items():
                if key not in FINDINGS_KEYS:
                    errors.append(f"findings key not in {FINDINGS_KEYS}, got {key}")
                elif not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    errors.append(f"findings.{key} must be a non-negative int, got {value!r}")

    # Check checks if present: dict of non-negative ints, keys a subset of the allowed set
    if "checks" in event:
        checks = event.get("checks")
        if not isinstance(checks, dict):
            errors.append("checks must be a dict")
        else:
            for key, value in checks.items():
                if key not in CHECKS_KEYS:
                    errors.append(f"checks key not in {CHECKS_KEYS}, got {key}")
                elif not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    errors.append(f"checks.{key} must be a non-negative int, got {value!r}")

    # Check effort if present
    if "effort" in event:
        effort = event.get("effort")
        if effort not in EFFORT_LEVELS:
            errors.append(f"effort not in {EFFORT_LEVELS}, got {effort}")

    # Check mode if present
    if "mode" in event:
        mode = event.get("mode")
        if mode not in RUN_MODES:
            errors.append(f"mode not in {RUN_MODES}, got {mode}")

    # Check reviewer_count if present: must be non-negative int
    if "reviewer_count" in event:
        reviewer_count = event.get("reviewer_count")
        if not isinstance(reviewer_count, int) or isinstance(reviewer_count, bool) or reviewer_count < 0:
            errors.append(f"reviewer_count must be a non-negative int, got {reviewer_count!r}")

    return errors


def default_log_path() -> Path:
    """Return the default telemetry log path."""
    return Path.home() / ".claude" / "telemetry" / "events.jsonl"


def default_state_dir() -> Path:
    """Return the default telemetry state directory for session-scoped IDs."""
    return Path.home() / ".claude" / "telemetry" / "state"


def state_path(session_id: str, state_dir: Path = None) -> Path:
    """Return the state file path for a given session_id.

    Args:
        session_id: the session ID (used as filename; should not be "unknown")
        state_dir: directory to store state files (default: default_state_dir())

    Returns:
        Path to the session's state file
    """
    if state_dir is None:
        state_dir = default_state_dir()
    safe_id = session_id if re.match(r"^[A-Za-z0-9_-]+$", session_id or "") else "unknown"
    return state_dir / f"{safe_id}.json"


def session_meta_path(session_id: str, state_dir: Path = None) -> Path:
    """Return the session-meta state file path for a given session_id.

    Deliberately a different file from state_path(): session_began_at and per-agent
    began_at timestamps (which must outlive individual command lifecycles) are tracked
    here, while command state entries (popped when their command ends) live in state_path.
    Different lifetimes justify the two-file split.
    """
    if state_dir is None:
        state_dir = default_state_dir()
    safe_id = session_id if re.match(r"^[A-Za-z0-9_-]+$", session_id or "") else "unknown"
    return state_dir / f"{safe_id}.session.json"


def load_and_update_state(path: Path, mutate_fn) -> dict:
    """Atomically read-modify-write a JSON state file.

    Uses fcntl.flock to ensure atomic read-modify-write across concurrent callers.
    If the file doesn't exist or contains corrupt/empty JSON, treats it as an empty dict {}.
    If the state_dir doesn't exist, creates it with mode 0700.

    IMPORTANT: This function must NOT be converted to write-temp-then-os.replace.
    os.replace swaps the inode; every other process holds flock on the old inode,
    so mutual exclusion evaporates silently. Mutual exclusion depends on one
    flock-protected whole-file read-modify-write per operation with the existing
    in-place ftruncate + write.

    Args:
        path: path to the state file
        mutate_fn: callable(state_dict) -> new_state_dict; receives the read dict,
                   returns the mutated dict to write back

    Returns:
        The final state dict that was written (the return value of mutate_fn)
    """
    path = Path(path)

    # Ensure parent directory exists with secure permissions
    old_umask = os.umask(0o077)
    try:
        parent_existed = path.parent.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
    finally:
        os.umask(old_umask)
    if not parent_existed:
        os.chmod(path.parent, 0o700)

    # Open with O_RDWR | O_CREAT to support read-modify-write
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            # Read existing content
            file_size = os.fstat(fd).st_size
            if file_size > 0:
                try:
                    content = os.read(fd, file_size).decode("utf-8")
                    state = json.loads(content)
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                    # Corrupt file (bad JSON or bad encoding): treat as empty
                    state = {}
            else:
                # Empty or new file
                state = {}

            # Apply mutation
            new_state = mutate_fn(state)

            # Truncate and rewrite
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            new_content = json.dumps(new_state)
            os.write(fd, new_content.encode("utf-8"))

            return new_state
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def init_command_state(path: Path, command_id: str, command: str, began_at: Optional[str] = None, session_id: Optional[str] = None) -> None:
    """Atomically initialize session state for a command lifecycle.

    A new command-begin adds an entry under its own command_id; last-write-wins applies
    only within the same command_id, and no other entry is replaced or dropped.

    began_at (an ISO timestamp) is stashed so a later command-end resolving via this
    command_id can compute elapsed_seconds.

    session_id is stored on the entry (not just top-level) so mutation and resolution
    can verify session_id matches before reading/clearing — mirrors the agent path.
    """
    def mutate(state: dict) -> dict:
        entries = _load_command_entries(state)
        entries[command_id] = {
            "session_id": session_id,
            "command": command,
            "command_began_at": began_at,
            "stage_id": None,
            "stage": None,
            "stage_began_at": None,
            "_monotonic_ns": time.monotonic_ns(),
        }
        _evict_expired(entries, keep_id=command_id, now=datetime.now(timezone.utc))
        return {"commands": entries}
    load_and_update_state(path, mutate)


def resolve_and_clear_command_state(path: Path, session_id: Optional[str], command_id_arg: Optional[str], command_name: str) -> CommandEndResolution:
    """Atomically resolve command_id/state_mismatch and clear the command entry.

    Single critical section implementing the resolution truth table:
    - If --command-id is given: use that entry if session_id matches; state_mismatch = None.
      If the entry exists but belongs to a different session, state_mismatch = True and
      the entry is left untouched (a cross-session collision, not a data corruption).
    - If no --command-id: prefer entries whose command name matches, so sibling
      commands with distinct names never collide:
      - Exactly 1 name match: use it, state_mismatch = None.
      - 2+ name matches: use LIFO (innermost open, latest begun), state_mismatch = None.
      - 0 name matches: resolve to UNKNOWN, state_mismatch = None, entries left
        untouched — preserves today's documented behavior even if entries exist for
        this session_id (per the issue's Step 3 truth table; no ambient fallback).

    CAS-clears: removes the entry under the resolved command_id ONLY if its recorded
    command_id matches. If the entry doesn't exist or command_id differs, the state
    is left UNTOUCHED.

    The entry's session_id must match the given session_id (or both be None) — a
    mismatch guard against session ID collisions.

    began_at is only surfaced when the entry actually cleared (i.e. this call is
    genuinely closing the entry's lifecycle) — otherwise there's no trustworthy
    correlation between the resolved command_id and any stored timestamp.

    Returns a CommandEndResolution with (command_id, state_mismatch, cleared, began_at).
    """
    result = {}

    def mutate(state: dict) -> dict:
        entries = _load_command_entries(state)

        command_id = UNKNOWN
        state_mismatch = None
        resolved_entry = None
        resolved_cid = None

        if command_id_arg:
            # Explicit --command-id: look it up
            if command_id_arg in entries:
                entry = entries[command_id_arg]
                if entry.get("session_id") == session_id:
                    command_id = command_id_arg
                    resolved_entry = entry
                    resolved_cid = command_id_arg
                    state_mismatch = None
                else:
                    # Entry exists but session_id doesn't match — collision
                    command_id = command_id_arg
                    state_mismatch = True
                    resolved_entry = None
                    resolved_cid = None
                    print(
                        f"telemetry: skipped resolving command_id={command_id_arg} "
                        "(state belongs to a different, concurrently in-flight session)",
                        file=sys.stderr,
                    )
            else:
                command_id = command_id_arg
                state_mismatch = None
                resolved_entry = None
                resolved_cid = None
        else:
            # No --command-id: prefer an exact name match first (so sibling commands
            # with distinct names never collide); preserve today's documented behavior
            # by resolving to UNKNOWN when no name matches are found, even if entries
            # exist for this session (per the issue's Step 3 truth table).
            name_matches = [
                (cid, entry) for cid, entry in entries.items()
                if entry.get("command") == command_name and entry.get("session_id") == session_id
            ]

            if not name_matches:
                # No name match: resolve to UNKNOWN, leave entries untouched
                command_id = UNKNOWN
                state_mismatch = None
                resolved_entry = None
                resolved_cid = None
            elif len(name_matches) == 1:
                command_id, resolved_entry = name_matches[0]
                resolved_cid = command_id
                state_mismatch = None
            else:
                # 2+ matches: LIFO by command_began_at (innermost open = latest begun), with monotonic tiebreaker
                name_matches = _sort_by_began_at_desc(name_matches, "command_began_at")
                command_id, resolved_entry = name_matches[0]
                resolved_cid = command_id
                state_mismatch = None

        result["command_id"] = command_id
        result["state_mismatch"] = state_mismatch

        # CAS-clear: only pop the entry if it exists and matches
        if resolved_entry and resolved_cid and resolved_cid in entries and entries[resolved_cid] is resolved_entry:
            result["cleared"] = True
            result["began_at"] = resolved_entry.get("command_began_at")
            del entries[resolved_cid]
        else:
            result["cleared"] = False
            result["began_at"] = None

        _evict_expired(entries, keep_id=resolved_cid, now=datetime.now(timezone.utc))
        return {"commands": entries}

    load_and_update_state(path, mutate)
    if not result.get("cleared") and result.get("command_id") not in (None, UNKNOWN) and not result.get("state_mismatch"):
        print(
            f"telemetry: skipped clearing state for command_id={result['command_id']} "
            "(state belongs to a different, concurrently in-flight command)",
            file=sys.stderr,
        )
    return CommandEndResolution(result["command_id"], result["state_mismatch"], result.get("cleared", False), result.get("began_at"))


def resolve_and_set_stage_state(
    path: Path, session_id: Optional[str], command_id_arg: Optional[str], stage_id: str, stage_name: str, began_at: Optional[str] = None
) -> str:
    """Atomically resolve command_id and write stage fields into the state entry.

    Single critical section implementing the stage-begin resolution:
    - If --command-id is given and entry exists with matching session_id: use it.
    - If no --command-id:
      - If exactly 1 open entry with matching session_id: use it (innermost/LIFO).
      - If 0 entries or all have wrong session_id: use "unknown" entry (or create it).

    Sets stage_id/stage/stage_began_at on the resolved entry, preserving its
    command/command_began_at unchanged.

    began_at (an ISO timestamp) is stashed so a later stage-end can compute elapsed_seconds.

    Returns the resolved command_id.
    """
    result = {}

    def mutate(state: dict) -> dict:
        entries = _load_command_entries(state)

        if command_id_arg:
            # Explicit command_id: use it if present and session_id matches
            if command_id_arg in entries:
                entry = entries[command_id_arg]
                if entry.get("session_id") == session_id:
                    command_id = command_id_arg
                else:
                    # Session mismatch: refuse to adopt/mutate; fall back to unknown
                    command_id = UNKNOWN
            else:
                # Explicit ID not found: create it with the provided session_id
                command_id = command_id_arg
                entries[command_id] = {"session_id": session_id, "_monotonic_ns": time.monotonic_ns()}
        else:
            # No explicit command_id: find LIFO innermost-open entry with matching session_id
            matching_entries = [
                (cid, entry) for cid, entry in entries.items()
                if cid != UNKNOWN and entry.get("session_id") == session_id
            ]
            if matching_entries:
                # LIFO by command_began_at (innermost = latest begun), with monotonic tiebreaker
                matching_entries = _sort_by_began_at_desc(matching_entries, "command_began_at")
                command_id, _ = matching_entries[0]
            else:
                # No matching entries: use reserved "unknown" entry
                command_id = UNKNOWN
                if command_id not in entries:
                    entries[command_id] = {"session_id": session_id, "command": None, "_monotonic_ns": time.monotonic_ns()}

        result["command_id"] = command_id

        # Set stage fields on the resolved entry
        if command_id not in entries:
            entries[command_id] = {"session_id": session_id, "_monotonic_ns": time.monotonic_ns()}
        entries[command_id]["stage_id"] = stage_id
        entries[command_id]["stage"] = stage_name
        entries[command_id]["stage_began_at"] = began_at

        _evict_expired(entries, keep_id=command_id, now=datetime.now(timezone.utc))
        return {"commands": entries}

    load_and_update_state(path, mutate)
    return result["command_id"]


def resolve_and_clear_stage_state(
    path: Path, session_id: Optional[str], command_id_arg: Optional[str], stage_id_arg: Optional[str], stage_name: str
) -> StageEndResolution:
    """Atomically resolve command_id/stage_id/state_mismatch and clear stage fields.

    Single critical section implementing the stage-end resolution:
    - If --stage-id is given: scan all entries for that stage_id (uuid4, globally unique).
    - If no --stage-id: prefer entries with an open stage whose name matches, so
      sibling open stages with distinct names never collide:
      - Exactly 1 open stage matching the name: use it, state_mismatch = None.
      - 2+ matching: use LIFO by stage_began_at, state_mismatch = None.
      - 0 name matches: resolve to UNKNOWN, state_mismatch = None, entries left
        untouched — preserves today's documented behavior even if entries have an
        open stage for this session_id (per the issue's Step 3 truth table; no
        ambient fallback).

    CAS-clears: removes stage_id/stage/stage_began_at ONLY if the entry's own
    stage_id matches the resolved stage_id. Command fields (command/command_began_at)
    are never touched — the command lifecycle survives a stage end.

    The entry's session_id must match the given session_id — a mismatch guard.

    began_at is only surfaced when the stage fields actually cleared.

    Returns a StageEndResolution with (command_id, stage_id, state_mismatch, began_at).
    """
    result = {}

    def mutate(state: dict) -> dict:
        entries = _load_command_entries(state)

        stage_id = UNKNOWN
        command_id = UNKNOWN
        state_mismatch = None
        resolved_entry = None
        resolved_cid = None

        if stage_id_arg:
            # Explicit --stage-id: scan all entries for matching stage_id (uuid4, globally unique)
            for cid, entry in entries.items():
                if entry.get("stage_id") == stage_id_arg and entry.get("session_id") == session_id:
                    stage_id = stage_id_arg
                    command_id = cid
                    resolved_entry = entry
                    resolved_cid = cid
                    break
            else:
                # Not found: use explicit stage_id_arg, and command_id_arg if given, else UNKNOWN
                # resolved_cid remains None here; safe because resolved_entry is also None,
                # so the CAS check below will refuse any eviction (no entry to protect anyway).
                stage_id = stage_id_arg
                command_id = command_id_arg or UNKNOWN
        else:
            # No --stage-id: prefer an exact stage-name match among entries with an
            # open stage first (so sibling open stages with distinct names never
            # collide); preserve today's documented behavior by resolving to UNKNOWN
            # when no name matches are found, even if entries with open stages exist
            # for this session (per the issue's Step 3 truth table).
            name_matches = [
                (cid, entry) for cid, entry in entries.items()
                if entry.get("stage") == stage_name and entry.get("stage_id") and entry.get("session_id") == session_id
            ]

            if not name_matches:
                # No name match: resolve to UNKNOWN, leave entries untouched
                stage_id = UNKNOWN
                command_id = UNKNOWN
                state_mismatch = None
            elif len(name_matches) == 1:
                command_id, resolved_entry = name_matches[0]
                resolved_cid = command_id
                stage_id = resolved_entry.get("stage_id", UNKNOWN)
                state_mismatch = None
            else:
                # 2+ matches: LIFO by stage_began_at, with monotonic tiebreaker
                name_matches = _sort_by_began_at_desc(name_matches, "stage_began_at")
                command_id, resolved_entry = name_matches[0]
                resolved_cid = command_id
                stage_id = resolved_entry.get("stage_id", UNKNOWN)
                state_mismatch = None

        result["command_id"] = command_id
        result["stage_id"] = stage_id
        result["state_mismatch"] = state_mismatch

        # CAS-clear: only remove stage fields if entry exists and stage_id matches
        if resolved_entry and resolved_cid and resolved_cid in entries and entries[resolved_cid].get("stage_id") == stage_id:
            result["cleared"] = True
            result["began_at"] = resolved_entry.get("stage_began_at")
            entries[resolved_cid]["stage_id"] = None
            entries[resolved_cid]["stage"] = None
            entries[resolved_cid]["stage_began_at"] = None
        else:
            result["cleared"] = False
            result["began_at"] = None

        _evict_expired(entries, keep_id=resolved_cid, now=datetime.now(timezone.utc))
        return {"commands": entries}

    load_and_update_state(path, mutate)
    if not result.get("cleared") and result.get("stage_id") not in (None, UNKNOWN):
        print(
            f"telemetry: skipped clearing stage state for stage_id={result['stage_id']} "
            "(state belongs to a different, concurrently in-flight stage)",
            file=sys.stderr,
        )
    return StageEndResolution(result["command_id"], result["stage_id"], result["state_mismatch"], result.get("cleared", False), result.get("began_at"))


def record_agent_began_at(path: Path, session_id: str, agent_id: str, began_at: str) -> None:
    """Atomically record an agent's begin timestamp, keyed by agent_id, in the session state.

    Stored alongside the session_id it was recorded under (not just the bare
    timestamp) so resolve_and_clear_agent_began_at can require a genuine session_id
    match before returning/clearing it — guards against session IDs that collide
    onto the same state file (see session_meta_path's filename-safe-character
    sanitization) from reading or clearing each other's agent timing.

    A duplicate agent_id within the same call sequence overwrites the prior entry
    (last-write-wins) — intentional, matching the rest of this state machinery's
    "last write always wins" semantics; there is no expectation of two concurrent
    agent-begins sharing one agent_id.
    """
    def mutate(state: dict) -> dict:
        agents = state.get("agents")
        if not isinstance(agents, dict):
            agents = {}
        agents[agent_id] = {"session_id": session_id, "began_at": began_at}
        state["agents"] = agents
        return state

    load_and_update_state(path, mutate)


def resolve_and_clear_agent_began_at(path: Path, session_id: str, agent_id: str) -> Optional[str]:
    """Atomically pop and return an agent's begin timestamp, or None if never recorded
    or the recorded entry belongs to a different session_id (a genuine-match guard,
    mirroring the command/stage path — see record_agent_began_at).

    Entries recorded by a pre-parity-audit version of this code (a bare string value
    instead of a {"session_id", "began_at"} dict) are treated as no match: popped
    without a warning to self-heal old-format state, but never returned as a
    began_at value, since there's nothing to genuinely match against.
    """
    result = {}

    def mutate(state: dict) -> dict:
        agents = state.get("agents")
        if not isinstance(agents, dict):
            agents = {}
        entry = agents.pop(agent_id, None)
        if isinstance(entry, dict) and entry.get("session_id") == session_id:
            result["began_at"] = entry.get("began_at")
        else:
            result["began_at"] = None
            result["mismatch"] = entry is not None and isinstance(entry, dict)
        state["agents"] = agents
        return state

    load_and_update_state(path, mutate)
    if result.get("mismatch"):
        print(
            f"telemetry: skipped returning agent state for agent_id={agent_id} "
            "(state belongs to a different, concurrently in-flight or collided session)",
            file=sys.stderr,
        )
    return result["began_at"]


def init_session_state(path: Path, session_id: str, began_at: str) -> None:
    """Atomically record a session's id and begin timestamp, preserving other fields
    (notably `agents`, which must survive a session-begin call).

    Repeat calls (e.g. a duplicate SessionStart hook firing) overwrite session_id/
    session_began_at unconditionally — a new session-begin always wins, mirroring
    init_command_state's documented "a new command-begin always wins" behavior.
    """
    def mutate(state: dict) -> dict:
        state["session_id"] = session_id
        state["session_began_at"] = began_at
        return state

    load_and_update_state(path, mutate)


def resolve_and_clear_session_began_at(path: Path, session_id: str) -> Optional[str]:
    """Atomically resolve a session's begin timestamp and delete the session-meta file,
    for session-end.

    Mirrors resolve_and_clear_command_state's CAS pattern: only returns/clears
    session_began_at, and only deletes the file, if the state's own recorded
    session_id equals the given session_id (i.e. this call is genuinely closing the
    session lifecycle the state describes, not a stale/collided file — session IDs
    that fail session_meta_path's filename-safe-character check all collide onto the
    same "unknown.session.json" file, so an ID check is required here, not just path
    scoping). If it differs, the file is left UNTOUCHED (do not clear or delete it)
    and a warning is printed to stderr; returns None in that case.

    Deleting the whole file (rather than only clearing session_began_at, as the old
    get_session_began_at left `agents` behind) also sweeps any orphaned `agents`
    entries — a session ending is that state file's natural end of life.

    Returns the session's begin timestamp (str), or None if no genuine match was found.
    """
    result = {}

    def mutate(state: dict) -> dict:
        recorded_session_id = state.get("session_id")
        recorded_began_at = state.get("session_began_at")
        if recorded_session_id and recorded_session_id == session_id:
            result["began_at"] = recorded_began_at
            result["cleared"] = True
            try:
                os.unlink(str(path))
            except (OSError, FileNotFoundError):
                pass
            return {}
        result["began_at"] = None
        result["cleared"] = False
        result["recorded_session_id"] = recorded_session_id
        return state

    load_and_update_state(path, mutate)
    if not result.get("cleared") and result.get("recorded_session_id"):
        print(
            f"telemetry: skipped clearing session state for session_id={session_id} "
            "(state belongs to a different, concurrently in-flight or collided session)",
            file=sys.stderr,
        )
    return result["began_at"]


def _compute_elapsed_for_sweep(began_at: Optional[str], end_timestamp: str) -> Union[int, str]:
    """Compute whole-second elapsed duration between an ISO began_at and end timestamp.

    Used by sweep_open_command_state; run-metrics.py's _compute_elapsed is an alias
    for this function (that module imports this one, not the reverse). Returns UNKNOWN
    (never a fabricated number) if began_at is missing/unparseable, if end_timestamp is
    unparseable, or if the delta is negative.
    """
    begin_dt = _parse_iso_or_none(began_at)
    end_dt = _parse_iso_or_none(end_timestamp)
    if begin_dt is None or end_dt is None:
        return UNKNOWN
    delta = (end_dt - begin_dt).total_seconds()
    if delta < 0:
        return UNKNOWN
    return int(delta)


def sweep_open_command_state(path: Path, session_meta_path: Path, session_id: str) -> tuple[list, Optional[str]]:
    """Sweep open command/stage entries at session end, clearing session_began_at atomically.

    Loads state_path(session_id), selects entries with matching session_id, and processes
    them innermost-first (reverse command_began_at order). For each open entry:
    - If a stage is open, emit event info for stage.end with outcome=interrupted
    - Then emit event info for command.end with outcome=interrupted (unless it's "unknown")
    - The reserved "unknown" entry emits stage.end only, never command.end

    Pops all swept entries; returns remaining (foreign-session) entries back to state file.

    Also clears session_began_at from the session meta file within the same coordination boundary
    (two separate file locks, but in a coordinated sequence).

    Never raises; wraps all errors internally. Returns a tuple (events_list, session_began_at).
    On any write failure, returns ([], None) so no events are emitted if state wasn't actually updated.

    Returns a tuple:
    - events_to_emit: list of dicts, each with keys: event_type, command_id, stage_id, stage, command, elapsed_seconds
    - session_began_at: the session's begin timestamp (str) if session_began_at was successfully
      cleared, else None (mirrors resolve_and_clear_session_began_at's own return contract)
    """
    events_to_emit = []
    session_began_at = None

    try:
        def mutate(state: dict) -> dict:
            entries = _load_command_entries(state)

            # Filter to entries with matching session_id, sort innermost-first
            matching_cids = [
                cid for cid in entries.keys()
                if entries[cid].get("session_id") == session_id
            ]
            # Sort by command_began_at, reverse (innermost = latest = last in iteration)
            matching_cids.sort(key=lambda cid: entries[cid].get("command_began_at", ""), reverse=True)

            now = datetime.now(timezone.utc).isoformat()

            for cid in matching_cids:
                entry = entries[cid]

                # Emit stage.end if stage is open
                if entry.get("stage_id") and entry.get("stage"):
                    stage_elapsed = _compute_elapsed_for_sweep(entry.get("stage_began_at"), now)
                    events_to_emit.append({
                        "event_type": "stage.end",
                        "command_id": cid,
                        "stage_id": entry.get("stage_id"),
                        "stage": entry.get("stage"),
                        "elapsed_seconds": stage_elapsed,
                    })

                # Emit command.end only if not the reserved "unknown" entry
                if cid != UNKNOWN and entry.get("command"):
                    cmd_elapsed = _compute_elapsed_for_sweep(entry.get("command_began_at"), now)
                    events_to_emit.append({
                        "event_type": "command.end",
                        "command_id": cid,
                        "command": entry.get("command"),
                        "elapsed_seconds": cmd_elapsed,
                    })

                # Pop the swept entry
                del entries[cid]

            return {"commands": entries}

        load_and_update_state(path, mutate)
        # Sweep succeeded; now clear session_began_at
        session_began_at = resolve_and_clear_session_began_at(session_meta_path, session_id)
    except Exception as e:
        # Wrap all errors so this can never raise (runs in a hook)
        print(f"telemetry: sweep_open_command_state failed: {type(e).__name__}: {e}", file=sys.stderr)
        # On write failure, return empty events so nothing is emitted
        return ([], None)

    return (events_to_emit, session_began_at)


def prune_stale_state(state_dir: Path = None, current_session_id: Optional[str] = None, max_age_seconds: int = 86400) -> None:
    """Best-effort prune of state files older than max_age_seconds.

    Never raises; failures are silently ignored. If state_dir doesn't exist, returns
    quietly without error.

    Skips the current session's files (both {safe_id}.json and {safe_id}.session.json)
    to avoid reaping the live session's state while command-begin is running.

    Args:
        state_dir: directory containing state files (default: default_state_dir())
        current_session_id: session ID to skip during pruning (default: None, no skipping)
        max_age_seconds: files older than this (in seconds) are deleted (default: 86400 = 24h)
    """
    if state_dir is None:
        state_dir = default_state_dir()

    # Compute safe IDs to skip
    skip_files = set()
    if current_session_id and re.match(r"^[A-Za-z0-9_-]+$", current_session_id):
        skip_files.add(f"{current_session_id}.json")
        skip_files.add(f"{current_session_id}.session.json")
    elif current_session_id:
        # Collides onto "unknown"
        skip_files.add("unknown.json")
        skip_files.add("unknown.session.json")

    try:
        if not state_dir.exists():
            return

        cutoff_time = time.time() - max_age_seconds
        for file_path in state_dir.iterdir():
            if file_path.name in skip_files:
                # Skip current session files
                continue
            if file_path.is_file() and file_path.suffix == ".json":
                try:
                    mtime = file_path.stat().st_mtime
                    if mtime < cutoff_time:
                        file_path.unlink()
                        print(f"telemetry: pruned stale state file {file_path}", file=sys.stderr)
                except (OSError, FileNotFoundError):
                    # File already deleted or permission issue; ignore
                    pass
    except (OSError, PermissionError) as e:
        print(f"telemetry: state dir access failed: {e}", file=sys.stderr)


def parse_transcript_tokens(path: Path) -> dict:
    """Parse a Claude Code transcript JSONL and extract usage metrics.

    Returns a dict with keys: tokens (dict with input/output/cache_read/cache_creation),
    turns (int), lines_parsed (int), lines_skipped (int), cost_state (dict or None),
    first_assistant_event (dict or None).

    Raises OSError or FileNotFoundError if the file cannot be read.
    For parse errors, the exception is raised (caller decides how to handle).

    Distinguishes between *malformed* (raises an exception) and *well-formed but empty*
    (returns cleanly with zero countable tokens). The caller's cmd_agent_end uses these
    to set different status values: parse_raised vs parsed_empty.
    """
    path = Path(path)
    lines_parsed = 0
    lines_skipped = 0
    seen_message_ids = {}  # message_id -> event dict (keeps last)
    cost_state = None
    first_assistant_event = None

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            try:
                event = json.loads(line)
                lines_parsed += 1

                if event.get("type") == "cost-state":
                    cost_state = event

                if event.get("type") == "assistant":
                    if first_assistant_event is None:
                        first_assistant_event = event
                    msg = event.get("message")
                    if msg and isinstance(msg, dict):
                        msg_id = msg.get("id")
                        if msg_id:
                            seen_message_ids[msg_id] = event
            except (json.JSONDecodeError, ValueError):
                lines_skipped += 1

    # If file had content but all lines failed to parse, raise an exception
    if lines_skipped > 0 and lines_parsed == 0:
        raise ValueError(f"Transcript file {path} had {lines_skipped} lines, none of which parsed as valid JSON")

    total_input = UNKNOWN
    total_output = UNKNOWN
    total_cache_read = UNKNOWN
    total_cache_creation = UNKNOWN

    input_sum = 0
    output_sum = 0
    cache_read_sum = 0
    cache_creation_sum = 0
    has_input = False
    has_output = False
    has_cache_read = False
    has_cache_creation = False

    for msg_id, event in seen_message_ids.items():
        msg = event.get("message", {})
        usage = msg.get("usage", {})

        if "input_tokens" in usage:
            input_sum += usage["input_tokens"]
            has_input = True
        if "output_tokens" in usage:
            output_sum += usage["output_tokens"]
            has_output = True
        if "cache_read_input_tokens" in usage:
            cache_read_sum += usage["cache_read_input_tokens"]
            has_cache_read = True
        if "cache_creation_input_tokens" in usage:
            cache_creation_sum += usage["cache_creation_input_tokens"]
            has_cache_creation = True

    if has_input:
        total_input = input_sum
    if has_output:
        total_output = output_sum
    if has_cache_read:
        total_cache_read = cache_read_sum
    if has_cache_creation:
        total_cache_creation = cache_creation_sum

    turns = len(seen_message_ids)

    return {
        "tokens": {
            "input": total_input,
            "output": total_output,
            "cache_read": total_cache_read,
            "cache_creation": total_cache_creation,
        },
        "turns": turns,
        "lines_parsed": lines_parsed,
        "lines_skipped": lines_skipped,
        "cost_state": cost_state,
        "first_assistant_event": first_assistant_event,
    }


def counted_tokens(tokens: dict) -> Optional[int]:
    """Sum tokens from COUNTED_TOKEN_KEYS, returning None if any key is unknown or missing.

    Returns None if any of input/output/cache_creation is the string "unknown" or
    otherwise not an int. Otherwise returns the integer sum of those three keys.
    """
    total = 0
    for key in COUNTED_TOKEN_KEYS:
        value = tokens.get(key, UNKNOWN)
        if value == UNKNOWN or not isinstance(value, int) or isinstance(value, bool):
            return None
        total += value
    return total


def _read_only_state(path: Path) -> Optional[dict]:
    """Read a state file without creating, truncating, or locking for writing.

    Returns the parsed dict, or None if the file does not exist or contains
    corrupt/undecodable JSON (a distinct "absent/corrupt" signal — never {}
    silently, since callers must be able to tell "nothing here" from "here but empty").
    """
    path = Path(path)
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except FileNotFoundError:
        return None
    except OSError:
        return None

    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        try:
            file_size = os.fstat(fd).st_size
            if file_size == 0:
                return None
            try:
                content = os.read(fd, file_size).decode("utf-8")
                state = json.loads(content)
                return state
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                return None
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def peek_command_id(path: Path, command_name: str) -> Optional[str]:
    """Read-only lookup of the most recently begun command_id for a given command name.

    Returns the command_id of the entry whose command field equals command_name and
    whose command_began_at is the most recent (latest begun wins in a tie; uses
    _monotonic_ns as a tiebreaker for identical timestamps). Returns None if the file
    doesn't exist, is empty/unparseable, or no entry matches the command name.

    Unlike resolve_and_clear_command_state, this function never mutates the state file.
    """
    state = _read_only_state(path)
    if state is None:
        return None

    entries = _load_command_entries(state)
    if not entries:
        return None

    # Filter to entries whose command field matches command_name, skipping non-dict entries
    matching_entries = [
        (cid, entry) for cid, entry in entries.items()
        if isinstance(entry, dict) and entry.get("command") == command_name
    ]

    if not matching_entries:
        return None

    # Sort by command_began_at (most recent first), with monotonic tiebreaker
    matching_entries = _sort_by_began_at_desc(matching_entries, "command_began_at")

    # Return the most recently begun command_id
    return matching_entries[0][0]


def read_usage_state(path: Path, session_id: str) -> dict:
    """Read usage state from session-meta file, computing derived fields.

    Returns a dict with keys:
    - available: bool (False if no usage state found)
    - session_mismatch: bool (True if usage.session_id differs from given session_id)
    - counted_tokens: int (sum of counted tokens, 0 if unavailable)
    - is_floor: bool (True if any agent is unparseable/mismatched, has leftovers, or unaccounted_no_agent_id > 0)
    - accounted: int (count of agents with status=="counted")
    - total_known: int (accounted + unparseable/mismatched agents + leftovers)
    - last_reported_crossing_at_tokens: Optional[int]
    - seams_checked: list
    - has_unaccounted_no_agent_id: bool
    - leftover_launched_not_accounted: int (agents in SessionMeta.agents with no usage entry)
    """
    path = Path(path)
    state = _read_only_state(path)

    if state is None:
        return {
            "available": False,
            "session_mismatch": False,
            "counted_tokens": 0,
            "is_floor": False,
            "accounted": 0,
            "total_known": 0,
            "last_reported_crossing_at_tokens": None,
            "seams_checked": [],
            "has_unaccounted_no_agent_id": False,
            "leftover_launched_not_accounted": 0,
            "floor_count": 0,
            "last_reported_floor_count": 0,
        }

    usage = state.get("usage")
    # "available" means at least one agent has actually been accounted (usage.session_id is
    # only ever set by record_agent_usage) — not merely that a seam has been checked. Fix 6's
    # record_seam_checked can create a non-empty usage dict (active_run, seams_checked) before
    # any agent ever finishes; that alone must not flip state from unavailable to under-threshold.
    if not usage or not isinstance(usage, dict) or not usage.get("session_id"):
        return {
            "available": False,
            "session_mismatch": False,
            "counted_tokens": 0,
            "is_floor": False,
            "accounted": 0,
            "total_known": 0,
            "last_reported_crossing_at_tokens": None,
            "seams_checked": [],
            "has_unaccounted_no_agent_id": False,
            "leftover_launched_not_accounted": 0,
            "floor_count": 0,
            "last_reported_floor_count": 0,
        }

    usage_session_id = usage.get("session_id")
    if usage_session_id and usage_session_id != session_id:
        return {
            "available": True,
            "session_mismatch": True,
            "counted_tokens": 0,
            "is_floor": False,
            "accounted": 0,
            "total_known": 0,
            "last_reported_crossing_at_tokens": usage.get("last_reported_crossing_at_tokens"),
            "seams_checked": usage.get("seams_checked", []),
            "has_unaccounted_no_agent_id": False,
            "leftover_launched_not_accounted": 0,
            "floor_count": 0,
            "last_reported_floor_count": usage.get("last_reported_floor_count", 0),
        }

    counted_tokens_total = usage.get("counted_tokens", 0)
    accounted_count = 0
    unparseable_mismatched_count = 0

    agents = usage.get("agents", {})
    if isinstance(agents, dict):
        for agent_entry in agents.values():
            if isinstance(agent_entry, dict):
                status = agent_entry.get("status")
                if status == "counted":
                    accounted_count += 1
                elif status in UNPARSEABLE_STATUSES or status == "session-mismatch":
                    unparseable_mismatched_count += 1

    unaccounted_no_agent_id = usage.get("unaccounted_no_agent_id_tokens", 0)
    has_unaccounted = unaccounted_no_agent_id > 0

    begin_agents = state.get("agents", {})
    leftover_count = 0
    if isinstance(begin_agents, dict):
        for agent_id, agent_entry in begin_agents.items():
            if isinstance(agent_entry, dict):
                if agent_entry.get("session_id") == session_id:
                    if agent_id not in agents or agent_id == UNKNOWN:
                        leftover_count += 1

    total_known = accounted_count + unparseable_mismatched_count + leftover_count
    is_floor = has_unaccounted or unparseable_mismatched_count > 0 or leftover_count > 0
    floor_count = unparseable_mismatched_count + leftover_count

    # Scope the reader to the active run's budget (decision 13): usage.counted_tokens and
    # unaccounted_no_agent_id_tokens are session-cumulative (the write path never resets them),
    # so subtract the baseline minted at this run's round1-join to make a re-invocation start
    # a fresh budget instead of inheriting the whole session's total. No active_run yet (a
    # session that hasn't hit round1-join this run) means baseline 0 — unchanged behavior.
    active_run = usage.get("active_run")
    baseline = 0
    if isinstance(active_run, dict):
        baseline = active_run.get("baseline_counted_tokens", 0) or 0

    raw_total = counted_tokens_total + unaccounted_no_agent_id
    relative_total = max(0, raw_total - baseline)

    raw_last_reported = usage.get("last_reported_crossing_at_tokens")
    relative_last_reported = None
    if raw_last_reported is not None:
        relative_last_reported = max(0, raw_last_reported - baseline)

    return {
        "available": True,
        "session_mismatch": False,
        "counted_tokens": relative_total,
        "unaccounted_no_agent_id_tokens": unaccounted_no_agent_id,
        "is_floor": is_floor,
        "accounted": accounted_count,
        "total_known": total_known,
        "last_reported_crossing_at_tokens": relative_last_reported,
        "seams_checked": usage.get("seams_checked", []),
        "has_unaccounted_no_agent_id": has_unaccounted,
        "leftover_launched_not_accounted": leftover_count,
        "floor_count": floor_count,
        "last_reported_floor_count": usage.get("last_reported_floor_count", 0),
        "active_run_id": active_run.get("run_id") if isinstance(active_run, dict) else None,
    }


def record_agent_usage(
    path: Path, session_id: str, agent_id: str, *, tokens: dict, token_confidence: Optional[str], status: str, recorded_at: str
) -> Optional[str]:
    """Atomically record agent usage and pop begin-timestamp in one transaction.

    Records usage tokens for an agent, folding them into usage.counted_tokens if applicable.
    Simultaneously pops the agent's begin-timestamp from the begin/end timing map.

    Guards against session_id mismatch: if usage.session_id is set and differs from
    session_id, records the agent with status="session-mismatch" and does NOT fold
    tokens into usage.counted_tokens.

    For agent_id == UNKNOWN, never folds into the keyed agents map; instead adds
    to unaccounted_no_agent_id_tokens when status=="counted" and counted_tokens() returns an int.

    Returns the popped began_at timestamp (str), or None if no match found or already popped.
    """
    path = Path(path)
    result = {}

    def mutate(state: dict) -> dict:
        agents_begin_map = state.get("agents", {})
        if not isinstance(agents_begin_map, dict):
            agents_begin_map = {}

        usage = state.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}

        usage_session_id = usage.get("session_id")
        if usage_session_id is None:
            usage["session_id"] = session_id
        session_mismatch = bool(usage_session_id and usage_session_id != session_id)

        begin_entry = agents_begin_map.pop(agent_id, None)
        began_at = None
        if isinstance(begin_entry, dict) and begin_entry.get("session_id") == session_id:
            began_at = begin_entry.get("began_at")
        result["began_at"] = began_at

        if session_mismatch:
            agents_dict = usage.get("agents", {})
            if not isinstance(agents_dict, dict):
                agents_dict = {}
            agents_dict[agent_id] = {
                "session_id": session_id,
                "status": "session-mismatch",
                "counted_tokens": None,
                "tokens": tokens,
                "token_confidence": token_confidence,
                "recorded_at": recorded_at,
            }
            usage["agents"] = agents_dict
        elif agent_id == UNKNOWN:
            agents_dict = usage.get("agents", {})
            if not isinstance(agents_dict, dict):
                agents_dict = {}
            agents_dict[agent_id] = {
                "session_id": session_id,
                "status": status,
                "counted_tokens": counted_tokens(tokens) if status == "counted" else None,
                "tokens": tokens,
                "token_confidence": token_confidence,
                "recorded_at": recorded_at,
            }
            usage["agents"] = agents_dict
            if status == "counted":
                counted = counted_tokens(tokens)
                if counted is not None:
                    current_unaccounted = usage.get("unaccounted_no_agent_id_tokens", 0)
                    usage["unaccounted_no_agent_id_tokens"] = current_unaccounted + counted
        else:
            agents_dict = usage.get("agents", {})
            if not isinstance(agents_dict, dict):
                agents_dict = {}

            if agent_id in agents_dict:
                usage["agents"] = agents_dict
            else:
                counted = counted_tokens(tokens) if status == "counted" else None
                agents_dict[agent_id] = {
                    "session_id": session_id,
                    "status": status,
                    "counted_tokens": counted,
                    "tokens": tokens,
                    "token_confidence": token_confidence,
                    "recorded_at": recorded_at,
                }
                usage["agents"] = agents_dict
                if status == "counted" and counted is not None:
                    current_counted = usage.get("counted_tokens", 0)
                    usage["counted_tokens"] = current_counted + counted
                elif status in UNPARSEABLE_STATUSES or status == "session-mismatch" or counted is None:
                    pass

        state["agents"] = agents_begin_map
        state["usage"] = usage
        return state

    load_and_update_state(path, mutate)
    return result.get("began_at")


def mark_usage_crossing_reported(path: Path, session_id: str) -> None:
    """Atomically mark a usage threshold crossing as reported.

    Sets last_reported_crossing_at_tokens to the max of the current counted_tokens
    and the existing last_reported_crossing_at_tokens (monotonic, never regresses).

    Reads the CURRENT counted_tokens inside the critical section to ensure consistency.
    Refuses (no-op) if usage.session_id is set and differs from session_id.
    """
    path = Path(path)

    def mutate(state: dict) -> dict:
        usage = state.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}

        usage_session_id = usage.get("session_id")
        if usage_session_id and usage_session_id != session_id:
            return state

        current_counted = usage.get("counted_tokens", 0) + (usage.get("unaccounted_no_agent_id_tokens", 0) or 0)
        existing_reported = usage.get("last_reported_crossing_at_tokens")
        new_reported = max(existing_reported or 0, current_counted)
        usage["last_reported_crossing_at_tokens"] = new_reported

        state["usage"] = usage
        return state

    load_and_update_state(path, mutate)


def mark_floor_reported(path: Path, session_id: str, floor_count: int) -> None:
    """Atomically latch the floor-agent count as reported (fix 12: non-threshold ask latching).

    Sets last_reported_floor_count to the max of floor_count and the existing value
    (monotonic, never regresses). A caller re-asks only once floor_count grows beyond
    this latched value (a *new* unparseable/session-mismatch/leftover agent appeared).
    Refuses (no-op) if usage.session_id is set and differs from session_id.
    """
    path = Path(path)

    def mutate(state: dict) -> dict:
        usage = state.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}

        usage_session_id = usage.get("session_id")
        if usage_session_id and usage_session_id != session_id:
            return state

        existing = usage.get("last_reported_floor_count", 0)
        usage["last_reported_floor_count"] = max(existing or 0, floor_count)

        state["usage"] = usage
        return state

    load_and_update_state(path, mutate)


def record_seam_checked(path: Path, session_id: str, seam: str) -> None:
    """Atomically record that a seam has been checked.

    Appends seam to usage.seams_checked if not already present (deduplicates).
    Refuses (no-op) if usage.session_id is set and differs from session_id.

    Fix 6 (decision 13): when seam=="round1-join", mints a fresh usage.active_run
    {run_id, started_at, baseline_counted_tokens} and resets seams_checked,
    last_reported_crossing_at_tokens, and last_reported_floor_count — this is
    /implement-with-haiku's own first seam, so every re-invocation in the same
    Claude Code session starts a fresh budget instead of inheriting the whole
    session's cumulative usage (which also accrues from other commands sharing
    the same SubagentStop write path, e.g. /expert-review, /expert-plan-v2).
    """
    path = Path(path)

    def mutate(state: dict) -> dict:
        usage = state.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}

        usage_session_id = usage.get("session_id")
        if usage_session_id and usage_session_id != session_id:
            return state

        if seam == "round1-join":
            baseline = usage.get("counted_tokens", 0) + (usage.get("unaccounted_no_agent_id_tokens", 0) or 0)
            usage["active_run"] = {
                "run_id": uuid.uuid4().hex,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "baseline_counted_tokens": baseline,
            }
            usage["seams_checked"] = []
            usage["last_reported_crossing_at_tokens"] = None
            usage["last_reported_floor_count"] = 0

        seams = usage.get("seams_checked", [])
        if not isinstance(seams, list):
            seams = []
        if seam not in seams:
            seams.append(seam)
        usage["seams_checked"] = seams

        state["usage"] = usage
        return state

    load_and_update_state(path, mutate)


def threshold_state(counted: int, threshold: int, last_reported: Optional[int]) -> ThresholdState:
    """Determine threshold state from counted tokens, threshold, and last reported crossing.

    Returns one of: under-threshold, over-threshold-unreported, over-threshold-reported.

    Escalation condition: over-threshold-unreported when
    counted > threshold and (last_reported is None or counted > last_reported + threshold).
    """
    if counted <= threshold:
        return "under-threshold"

    if last_reported is None or counted > last_reported + threshold:
        return "over-threshold-unreported"

    return "over-threshold-reported"


def append_event(path: Path, event: dict) -> None:
    """Atomically append an event (as JSON on one line) to the log file.

    Creates parent directories if missing. Uses fcntl.flock for atomic writes.
    Raises ValueError if the event fails validate_event().
    """
    # Validate event before any I/O
    errors = validate_event(event)
    if errors:
        raise ValueError(f"Invalid telemetry event: {'; '.join(errors)}")

    path = Path(path)
    old_umask = os.umask(0o077)
    try:
        parent_existed = path.parent.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
    finally:
        os.umask(old_umask)
    if not parent_existed:
        os.chmod(path.parent, 0o700)

    # Open with os.open to use O_APPEND | O_CREAT atomically
    fd = os.open(str(path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            line = json.dumps(event, sort_keys=True) + "\n"
            os.write(fd, line.encode("utf-8"))
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
