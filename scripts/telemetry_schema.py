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
from typing import Literal, NamedTuple, Optional, TypedDict, Union


class SessionState(TypedDict, total=False):
    """Typed representation of session-scoped state dict.

    Fields command_id, command, stage_id, stage are optional; either present or absent.
    `command_began_at`/`stage_began_at` hold ISO timestamps used to compute
    elapsed_seconds when the matching end event resolves via this state file.

    Note: session_began_at and the per-agent began_at map live in a SEPARATE file
    (see session_meta_path) rather than here, because command-end unconditionally
    deletes this file on completion (test_command_end_deletes_state_file) — sharing
    a file would wipe session/agent timing whenever a command finished.
    """
    command_id: Optional[str]
    command: Optional[str]
    command_began_at: Optional[str]
    stage_id: Optional[str]
    stage: Optional[str]
    stage_began_at: Optional[str]


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
    began_at: Optional[str]


class AgentUsageEntry(TypedDict, total=False):
    """One entry in UsageState.agents: usage tokens and metadata for a single agent."""
    session_id: str
    status: Literal["counted", "unparseable", "session-mismatch"]
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

COUNTED_TOKEN_KEYS = ("input", "output", "cache_creation")
THRESHOLD_STATES = frozenset({"under-threshold", "over-threshold-unreported", "over-threshold-reported", "unavailable", "session-mismatch"})
ThresholdState = Literal["under-threshold", "over-threshold-unreported", "over-threshold-reported", "unavailable", "session-mismatch"]
USAGE_GATE_DEFAULT_THRESHOLD = 130_000


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

    Deliberately a different file from state_path(): that file is deleted wholesale
    by resolve_and_clear_command_state on every command-end, so session_began_at and
    per-agent began_at timestamps (which must outlive individual command lifecycles)
    are tracked here instead.
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


def init_command_state(path: Path, command_id: str, command: str, began_at: Optional[str] = None) -> None:
    """Atomically initialize session state for a new command lifecycle.

    Replaces any prior state unconditionally (a new command-begin always wins —
    this matches command-begin's existing behavior of starting a fresh lifecycle).

    began_at (an ISO timestamp) is stashed so a later command-end resolving via this
    same state file can compute elapsed_seconds.
    """
    def mutate(state: dict) -> dict:
        return {
            "command_id": command_id,
            "command": command,
            "command_began_at": began_at,
            "stage_id": None,
            "stage": None,
            "stage_began_at": None,
        }
    load_and_update_state(path, mutate)


def resolve_and_clear_command_state(path: Path, command_id_arg: Optional[str], command_name: str) -> CommandEndResolution:
    """Atomically resolve command_id/state_mismatch and clear state for command-end.

    Single critical section combining what were previously 2-3 separate locked calls
    plus an unguarded unlink:
    - Resolves command_id: explicit command_id_arg wins; else the state's command_id;
      else telemetry_schema.UNKNOWN.
    - Computes state_mismatch: only when command_id_arg was NOT given (i.e. explicit ID
      always trusted, no mismatch check needed) — True if state has a recorded 'command'
      that differs from command_name, else None.
    - CAS-clears state: state is reset to {} ONLY if the state's own command_id equals the
      resolved command_id (i.e. this call is finishing the lifecycle that state currently
      describes). If the state's command_id differs (a concurrent command-begin already
      replaced it, or --command-id was passed for a different lifecycle), the state is left
      UNTOUCHED (do not clear it) and a warning is printed to stderr.
    - began_at is only surfaced when the state actually cleared (i.e. this call is
      genuinely closing the lifecycle the state describes) — otherwise there's no
      trustworthy correlation between the resolved command_id and any stored timestamp.

    Returns a CommandEndResolution with (command_id, state_mismatch, cleared, began_at).
    """
    result = {}

    def mutate(state: dict) -> dict:
        recorded_command_id = state.get("command_id")
        recorded_began_at = state.get("command_began_at")
        if command_id_arg:
            command_id = command_id_arg
            state_mismatch = None
        else:
            command_id = recorded_command_id or UNKNOWN
            recorded_command = state.get("command")
            state_mismatch = True if (recorded_command and recorded_command != command_name) else None
        result["command_id"] = command_id
        result["state_mismatch"] = state_mismatch
        if recorded_command_id and recorded_command_id == command_id:
            result["cleared"] = True
            result["began_at"] = recorded_began_at
            try:
                os.unlink(str(path))
            except (OSError, FileNotFoundError):
                pass
            return {}
        result["cleared"] = False
        result["began_at"] = None
        return state

    load_and_update_state(path, mutate)
    if not result.get("cleared") and result.get("command_id") not in (None, UNKNOWN):
        print(
            f"telemetry: skipped clearing state for command_id={result['command_id']} "
            "(state belongs to a different, concurrently in-flight command)",
            file=sys.stderr,
        )
    return CommandEndResolution(result["command_id"], result["state_mismatch"], result.get("cleared", False), result.get("began_at"))


def resolve_and_set_stage_state(
    path: Path, command_id_arg: Optional[str], stage_id: str, stage_name: str, began_at: Optional[str] = None
) -> str:
    """Atomically resolve command_id and write stage fields into state for stage-begin.

    Single critical section: resolves command_id (explicit command_id_arg wins; else the
    state's existing command_id; else telemetry_schema.UNKNOWN), then sets stage_id/stage
    on the same state dict, preserving whatever command_id/command was already present.

    began_at (an ISO timestamp) is stashed so a later stage-end resolving via this same
    state file can compute elapsed_seconds.

    Returns the resolved command_id.
    """
    result = {}

    def mutate(state: dict) -> dict:
        recorded_command_id = state.get("command_id")
        command_id = command_id_arg or recorded_command_id or UNKNOWN
        result["command_id"] = command_id
        state["stage_id"] = stage_id
        state["stage"] = stage_name
        state["stage_began_at"] = began_at
        return state

    load_and_update_state(path, mutate)
    return result["command_id"]


def resolve_and_clear_stage_state(
    path: Path, command_id_arg: Optional[str], stage_id_arg: Optional[str], stage_name: str
) -> StageEndResolution:
    """Atomically resolve command_id/stage_id/state_mismatch and clear stage fields for stage-end.

    Single critical section combining what were previously 3-4 separate locked calls:
    - Resolves stage_id: explicit stage_id_arg wins; else the state's stage_id; else UNKNOWN.
    - Resolves command_id: explicit command_id_arg wins; else the state's command_id; else UNKNOWN.
    - Computes state_mismatch: only when stage_id_arg was NOT given — True if state has a
      recorded 'stage' that differs from stage_name, else None.
    - CAS-clears ONLY the stage_id/stage fields (never command_id/command — the command
      lifecycle is still in flight) ONLY if the state's own stage_id equals the resolved
      stage_id. If it differs (a concurrent stage-begin already replaced it), the stage
      fields are left UNTOUCHED and a warning is printed to stderr.
    - began_at is only surfaced when the stage fields actually cleared — otherwise there's
      no trustworthy correlation between the resolved stage_id and any stored timestamp.

    Returns a StageEndResolution with (command_id, stage_id, state_mismatch, began_at).
    """
    result = {}

    def mutate(state: dict) -> dict:
        recorded_stage_id = state.get("stage_id")
        recorded_command_id = state.get("command_id")
        recorded_began_at = state.get("stage_began_at")
        if stage_id_arg:
            stage_id = stage_id_arg
            state_mismatch = None
        else:
            stage_id = recorded_stage_id or UNKNOWN
            recorded_stage = state.get("stage")
            state_mismatch = True if (recorded_stage and recorded_stage != stage_name) else None
        command_id = command_id_arg or recorded_command_id or UNKNOWN
        result["command_id"] = command_id
        result["stage_id"] = stage_id
        result["state_mismatch"] = state_mismatch
        if recorded_stage_id and recorded_stage_id == stage_id:
            result["cleared"] = True
            result["began_at"] = recorded_began_at
            state["stage_id"] = None
            state["stage"] = None
            state["stage_began_at"] = None
            return state
        result["cleared"] = False
        result["began_at"] = None
        return state

    load_and_update_state(path, mutate)
    if not result.get("cleared") and result.get("stage_id") not in (None, UNKNOWN):
        print(
            f"telemetry: skipped clearing stage state for stage_id={result['stage_id']} "
            "(state belongs to a different, concurrently in-flight stage)",
            file=sys.stderr,
        )
    return StageEndResolution(result["command_id"], result["stage_id"], result["state_mismatch"], result.get("began_at"))


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


def prune_stale_state(state_dir: Path = None, max_age_seconds: int = 86400) -> None:
    """Best-effort prune of state files older than max_age_seconds.

    Never raises; failures are silently ignored. If state_dir doesn't exist, returns
    quietly without error.

    Args:
        state_dir: directory containing state files (default: default_state_dir())
        max_age_seconds: files older than this (in seconds) are deleted (default: 86400 = 24h)
    """
    if state_dir is None:
        state_dir = default_state_dir()

    try:
        if not state_dir.exists():
            return

        cutoff_time = time.time() - max_age_seconds
        for file_path in state_dir.iterdir():
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
                elif status in ("unparseable", "session-mismatch"):
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
                elif status in ("unparseable", "session-mismatch") or counted is None:
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
