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
from datetime import datetime
from pathlib import Path

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
            datetime.fromisoformat(ts_normalized)
        except (ValueError, TypeError):
            errors.append(f"timestamp not parseable as ISO format: {ts_str}")

    # Check session_id
    if "session_id" not in event:
        errors.append("Missing session_id")
    elif not isinstance(event.get("session_id"), str) or not event.get("session_id"):
        errors.append("session_id must be a non-empty string")

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

    return errors


def default_log_path() -> Path:
    """Return the default telemetry log path."""
    return Path.home() / ".claude" / "telemetry" / "events.jsonl"


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
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)

    # Open with os.open to use O_APPEND | O_CREAT atomically
    fd = os.open(str(path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.chmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            line = json.dumps(event, sort_keys=True) + "\n"
            os.write(fd, line.encode("utf-8"))
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
