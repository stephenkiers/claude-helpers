#!/usr/bin/env python3
"""
Write transcript-origin.json for bounded transcript discovery.

Schema: transcript-origin.json records where and when a review was initiated,
enabling downstream tools to anchor transcript discovery to the correct project
session. Only session_id resolution values: "env" (valid CLAUDE_CODE_SESSION_ID)
or "unavailable". Legacy readers still accept "most-recent-dir" from older files.

project_dir fallback chain:
  1. If session_id resolved from env: glob ~/.claude/projects/*/[session_id].jsonl
  2. Fallback (session_id unavailable or glob failed): sanitize and record cwd
  3. Record both cwd and project_dir; downstream readers will apply same fallback chain

Usage:
    write-transcript-origin.py <REVIEW_DIR>

Exit codes:
    0 — file written successfully (including resolution unavailable)
    1 — write failure (I/O error, permission denied, etc.)
    2 — bad arguments (REVIEW_DIR missing or not under ~/.claude/reviews/)
"""

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _sanitize_project_dir(path: str) -> str:
    """
    Sanitize a path to a project dir id by replacing every char outside [A-Za-z0-9-] with -.
    Used as fallback when session_id is unavailable; mirrors reviewer-yield.py logic.
    Returns the sanitized string (may fail _SAFE_ID_RE if result is all dashes).
    """
    return re.sub(r"[^A-Za-z0-9-]", "-", path)


def main():
    # Validate arguments
    if len(sys.argv) != 2:
        print("Usage: write-transcript-origin.py <REVIEW_DIR>", file=sys.stderr)
        sys.exit(2)

    review_dir_str = sys.argv[1]
    review_dir = Path(review_dir_str).resolve()

    # Validate REVIEW_DIR exists and is under ~/.claude/reviews/
    reviews_base = (Path.home() / ".claude" / "reviews").resolve()
    try:
        review_dir.relative_to(reviews_base)
    except ValueError:
        print(
            f"Error: {review_dir} is not under {reviews_base}",
            file=sys.stderr,
        )
        sys.exit(2)

    if not review_dir.exists():
        print(
            f"Error: {review_dir} does not exist",
            file=sys.stderr,
        )
        sys.exit(2)

    # Get session_id from environment
    session_id = None
    resolution = "unavailable"

    env_session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if env_session_id and _SAFE_ID_RE.match(env_session_id):
        session_id = env_session_id
        resolution = "env"

    # Resolve project_dir: try session_id glob first, fall back to sanitized cwd
    project_dir = ""
    current_cwd = os.getcwd()

    if session_id:
        # If session_id is set, glob ~/.claude/projects/*/[session_id].jsonl
        projects_base = Path.home() / ".claude" / "projects"
        try:
            matches = list(projects_base.glob(f"*/{session_id}.jsonl"))
            if len(matches) == 1:
                # Exactly one match: use the project dir name
                project_dir = matches[0].parent.name
        except (OSError, ValueError):
            # Silently ignore glob errors; project_dir stays ""
            pass

    # If project_dir still empty, sanitize cwd as fallback
    if not project_dir:
        sanitized_cwd = _sanitize_project_dir(current_cwd)
        if _SAFE_ID_RE.match(sanitized_cwd):
            project_dir = sanitized_cwd
            if not session_id:
                # Warn when falling back to cwd with no session_id available
                print("Warning: session_id unavailable; using sanitized cwd as project_dir", file=sys.stderr)
        # else project_dir stays ""

    # Build the output object
    recorded_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    output = {
        "schema_version": 1,
        "cwd": current_cwd,
        "project_dir": project_dir,
        "session_id": session_id,
        "resolution": resolution,
        "recorded_at": recorded_at,
    }

    # Write atomically: temp file then os.replace
    try:
        # Create a temporary file in the target directory
        fd, temp_path = tempfile.mkstemp(
            dir=str(review_dir),
            prefix=".transcript-origin-",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(output, f)
            os.replace(temp_path, str(review_dir / "transcript-origin.json"))
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise
    except Exception as e:
        print(f"Error writing transcript-origin.json: {e}", file=sys.stderr)
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
