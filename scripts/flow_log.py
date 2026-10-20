#!/usr/bin/env python3
"""Shared leaf helpers for expert-flow's log.md and ISO-8601 UTC timestamps (stdlib only).

Owns the log.md attempt-start line format (the "log.md" block in commands/expert-flow.md,
lines 744-751): `<ISO-8601 UTC Z> <step> begin|retry`. This is the one place in code where that
format lives. Imported in-process by scripts/implement-ledger.py (`locate --flow-dir --step`) and
scripts/flow-verify-receipt.py (`--since-from-log`, and its `_parse_iso8601` alias). This module
imports no other repo script, so neither importer depends on the other.

Why it matters: a bound that is too early or wrong lets a stale run pass the expert-flow Fidelity
audit (a false pass that masks a skipped step), and a bound that is too late or missing gives a false
`ledger unavailable`. The value is produced by code and is never copied by the model.
"""

import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ISO_UTC_Z_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
STEP_RE = re.compile(r"[a-z][a-z-]*")
# Only the last 1 MiB of log.md is read (about 25,000 log lines of ~40 bytes each).
LOG_READ_CAP_BYTES = 1_048_576
# A begin/retry timestamp later than now plus this much skew is rejected (fail closed).
FUTURE_SKEW_SECS = 5


class FlowLogError(ValueError):
    """Raised when a flow directory or its log.md cannot yield a trustworthy attempt start."""


def parse_iso8601(iso_str: str) -> float:
    """Parse ISO-8601 UTC timestamp to float (epoch seconds).

    Handles formats like "2026-10-14T22:20:13Z" and "2026-10-14T22:20:13+00:00"
    with or without timezone offsets.
    """
    try:
        # Normalize trailing Z to +00:00 for fromisoformat
        normalized = iso_str.rstrip("Z")
        if normalized == iso_str:
            # Z was not present, so no normalization needed
            dt = datetime.fromisoformat(iso_str)
        else:
            # Z was present, add +00:00
            normalized = normalized + "+00:00"
            dt = datetime.fromisoformat(normalized)

        # Ensure timezone-aware, defaulting to UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.timestamp()
    except ValueError:
        raise ValueError(f"Could not parse ISO timestamp: {iso_str}")


def newest_attempt_start(log_path: Path, step: str) -> Optional[str]:
    """Return the timestamp of the newest `<step> begin|retry` line in log.md, or None if there is none.

    "Newest" is the last matching line in file order (log.md is append-only). Lines of other events
    are ignored. If the newest candidate is malformed, or its timestamp is not a valid UTC Z value,
    or it lies in the future, FlowLogError is raised. The function never falls back to an older line,
    because that would widen the bound. A missing, unreadable, or non-regular log.md raises FlowLogError.
    """
    if not STEP_RE.fullmatch(step):
        raise FlowLogError(f"invalid step name {step!r}")
    loose_re = re.compile(r"(\S+) " + re.escape(step) + r" (?:begin|retry)\s*")
    strict_re = re.compile(r"(\S+) " + re.escape(step) + r" (?:begin|retry)")

    if not log_path.is_file():
        raise FlowLogError("log.md is missing or not a regular file")
    try:
        size = log_path.stat().st_size
        truncated = size > LOG_READ_CAP_BYTES
        with open(str(log_path), "rb") as fh:
            if truncated:
                fh.seek(size - LOG_READ_CAP_BYTES)
            raw = fh.read()
    except OSError as e:
        raise FlowLogError(f"cannot read log.md: {e}")
    if truncated:
        # Drop the partial first line left by the seek.
        newline = raw.find(b"\n")
        raw = raw[newline + 1:] if newline != -1 else b""

    text = raw.decode("utf-8", errors="replace")
    newest: Optional[str] = None
    for line in text.split("\n"):
        if loose_re.fullmatch(line):
            newest = line
    if newest is None:
        return None

    match = strict_re.fullmatch(newest)
    if match is None:
        raise FlowLogError(f"malformed {step} begin or retry line in log.md")
    stamp = match.group(1)
    if not ISO_UTC_Z_RE.fullmatch(stamp):
        raise FlowLogError(f"malformed {step} begin or retry line in log.md")
    try:
        epoch = parse_iso8601(stamp)
    except ValueError:
        raise FlowLogError(f"malformed {step} begin or retry line in log.md")
    if epoch > time.time() + FUTURE_SKEW_SECS:
        raise FlowLogError(f"{step} begin or retry timestamp is in the future")
    return stamp


def validate_flow_dir(path: str) -> Path:
    """Return the realpath of a flow directory, or raise FlowLogError.

    The value must be absolute, its realpath must lie strictly under realpath(~/.claude/flows), and it
    must contain a regular-file log.md that is not a symlink. This follows the validity rule in
    prompts/flow-reference.md, without the state.json status check (the audit does not need it).
    """
    if not os.path.isabs(path):
        raise FlowLogError("flow dir must be an absolute path")
    real = os.path.realpath(path)
    flows_root = os.path.realpath(os.path.expanduser("~/.claude/flows"))
    try:
        rel = Path(real).relative_to(flows_root)
    except ValueError:
        raise FlowLogError("flow dir is outside ~/.claude/flows")
    if not rel.parts:
        raise FlowLogError("flow dir must be a flow directory under ~/.claude/flows")
    log = Path(real) / "log.md"
    if log.is_symlink() or not log.is_file():
        raise FlowLogError("log.md is missing, a symlink, or not a regular file")
    return Path(real)
