#!/usr/bin/env python3
"""
Session discovery for expert-review transcripts: resolves session_id from transcript-origin.json
or via path-scan fallback.

Core exports (used by reviewer-yield.py and write-transcript-origin.py):
- resolve_session: resolves session_id for a review directory
- SessionRef, Unresolved: TypedDicts for successful and failed resolutions

Resolution method: "origin" (from transcript-origin.json), "path-scan" (from sessionl jsonl
grep), or "unavailable" (not found, ambiguous, or explicitly unavailable).
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, TypedDict, Union


_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class SessionRef(TypedDict):
    """Resolved session reference."""
    session_id: str
    project_dir: str
    resolution: str  # "origin" or "path-scan"
    recorded_at: str
    resolved: bool  # discriminator: True


class Unresolved(TypedDict):
    """Unresolved session reference."""
    session_id: None
    resolution: str  # "origin-missing", "ambiguous-path-scan", "origin-unavailable", "session-dir-missing", "not-attributable-by-construction"
    reason: str
    resolved: bool  # discriminator: False


def resolve_session(review_dir: str) -> Union[SessionRef, Unresolved]:
    """
    Resolve the session_id for a review directory by trying two sources in order:
    1. transcript-origin.json (if valid)
    2. Path-scan fallback (grep ~/.claude/projects/*/*.jsonl for review-dir basename)

    Returns SessionRef on success, Unresolved on failure. Unresolved includes a reason
    suitable for the yield row's `reason` field.

    Path-scan:
    - Exactly one matching session: resolves as "path-scan", returns session_id
    - Zero matches: returns Unresolved("origin-missing")
    - Multiple matches: returns Unresolved("ambiguous-path-scan"), never picks one
    """
    review_dir_path = Path(review_dir).expanduser().resolve()
    origin_file = review_dir_path / "transcript-origin.json"

    # Try origin file first
    if origin_file.exists():
        try:
            origin_data = json.loads(origin_file.read_text())
        except (json.JSONDecodeError, OSError):
            return Unresolved(session_id=None, resolution="origin-unavailable", reason="transcript-origin.json unreadable", resolved=False)

        if not isinstance(origin_data, dict):
            return Unresolved(session_id=None, resolution="origin-unavailable", reason="transcript-origin.json not an object", resolved=False)

        resolution = origin_data.get("resolution")
        session_id = origin_data.get("session_id")
        project_dir = origin_data.get("project_dir")
        recorded_at = origin_data.get("recorded_at", datetime.now(timezone.utc).isoformat())

        # If origin says unavailable, return that
        if resolution == "unavailable":
            return Unresolved(session_id=None, resolution="origin-unavailable", reason="resolution is unavailable", resolved=False)

        if not session_id or not project_dir:
            return Unresolved(session_id=None, resolution="origin-unavailable", reason="session_id or project_dir missing", resolved=False)

        if not isinstance(session_id, str) or not isinstance(project_dir, str):
            return Unresolved(session_id=None, resolution="origin-unavailable", reason="session_id/project_dir invalid type", resolved=False)

        if not _SAFE_ID_RE.match(session_id) or not _SAFE_ID_RE.match(project_dir):
            return Unresolved(session_id=None, resolution="origin-unavailable", reason="session_id/project_dir failed validation", resolved=False)

        # Valid origin
        return SessionRef(session_id=session_id, project_dir=project_dir, resolution="origin", recorded_at=recorded_at, resolved=True)

    # Origin file doesn't exist; try path scan
    review_dir_name = review_dir_path.name
    matched_sessions = _find_matching_sessions(review_dir_name)

    if len(matched_sessions) == 0:
        return Unresolved(session_id=None, resolution="origin-missing", reason="origin-missing", resolved=False)
    elif len(matched_sessions) == 1:
        session_id, project_dir = matched_sessions[0]
        return SessionRef(session_id=session_id, project_dir=project_dir, resolution="path-scan", recorded_at=datetime.now(timezone.utc).isoformat(), resolved=True)
    else:
        return Unresolved(session_id=None, resolution="ambiguous-path-scan", reason="ambiguous-path-scan", resolved=False)


def _find_matching_sessions(review_dir_name: str) -> List[tuple]:
    """
    Search ~/.claude/projects/*/*.jsonl and subagent jsonl files for lines matching the
    review_dir basename. Return list of (session_id, project_dir) tuples.

    Basename matches are expected to be unique because reviews use a timestamp + random suffix
    in their directory name, but collisions are handled by returning all matches (ambiguous
    collisions are detected by the caller).
    """
    projects_root = Path.home() / ".claude" / "projects"
    if not projects_root.exists():
        return []

    matched = []

    # Search each project dir's sessionl files
    for project_dir in projects_root.iterdir():
        if not project_dir.is_dir():
            continue

        # Search {project_dir}/*.jsonl
        for jsonl_file in project_dir.glob("*.jsonl"):
            if _contains_review_dir(jsonl_file, review_dir_name):
                # Extract session_id from parent dir name
                session_id = jsonl_file.parent.name
                matched.append((session_id, project_dir.name))

        # Search {project_dir}/**/subagents/*.jsonl
        for subagent_file in project_dir.glob("*/subagents/*.jsonl"):
            if _contains_review_dir(subagent_file, review_dir_name):
                session_id = subagent_file.parent.parent.name
                matched.append((session_id, project_dir.name))

    return matched


def _contains_review_dir(jsonl_file: Path, review_dir_name: str) -> bool:
    """Check if a jsonl file contains any reference to the review_dir basename."""
    try:
        with open(jsonl_file, "r") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    if not isinstance(entry, dict):
                        continue
                    # Check any path-like fields
                    for value in entry.values():
                        if isinstance(value, str) and review_dir_name in value:
                            return True
                        elif isinstance(value, dict):
                            for v in value.values():
                                if isinstance(v, str) and review_dir_name in v:
                                    return True
                        elif isinstance(value, list):
                            for item in value:
                                if isinstance(item, str) and review_dir_name in item:
                                    return True
                                elif isinstance(item, dict):
                                    for v in item.values():
                                        if isinstance(v, str) and review_dir_name in v:
                                            return True
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        print(f"Warning: OSError reading {jsonl_file}: {e}", file=sys.stderr)

    return False
