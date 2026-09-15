#!/usr/bin/env python3
"""
Fast, standalone check: has this commit already been reviewed?

Queries .claude/github-cache.json for a prior review cached against the current branch
and commit, returning a machine- or human-readable status. Useful in hooks, in other
commands, or for one-off shell invocations — no LLM, no subagents, immediate result.

Resolves git state from the current working directory (PROJECT_ROOT, branch, commit hash,
dirty status), compares against cached review metadata, and exits with 0 only when
already reviewed at the current commit in a clean tree (safe to fast-exit).

Exception handling policy: Read and parse failures (I/O, JSON) warn to stderr and continue
with safe defaults (reviewed=false, findings=null), never raising. This preserves idempotency
on read-after-read failures while allowing the caller to distinguish exit codes (success vs.
skip review). Missing files are not treated as errors — an empty cache is a valid, common state.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional, Dict, Any, Tuple


def get_git_info() -> Tuple[str, str, str, bool]:
    """
    Resolve current git state: PROJECT_ROOT, BRANCH (with slashes → dashes),
    short HASH, and DIRTY (tracked-file changes only, untracked noise ignored).

    Returns (project_root, branch, hash, dirty).
    Raises subprocess.CalledProcessError if git commands fail (expected only if cwd is not in a repo).
    """
    project_root = subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"], text=True
    ).strip()

    branch = (
        subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True)
        .strip()
        .replace("/", "-")
    )

    hash_short = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"], text=True
    ).strip()

    # Tracked-file changes only; untracked clutter isn't relevant to "does the committed
    # state match what was reviewed" and would cause alarm fatigue.
    dirty_output = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=no"], text=True
    ).strip()
    dirty = len(dirty_output) > 0

    return project_root, branch, hash_short, dirty


def read_review_cache(cache_path: Path) -> Optional[Dict[str, Any]]:
    """
    Read .claude/github-cache.json and extract the 'review' field.

    Returns the review object (dict) if present and valid, or None if missing/malformed.
    Warns to stderr on read/parse errors, never raises (idempotent safe-default policy).
    """
    if not cache_path.exists():
        return None

    try:
        with open(cache_path, "r") as f:
            data = json.load(f)
        return data.get("review")
    except (OSError, json.JSONDecodeError) as e:
        print(f"Warning: failed to read cache {cache_path}: {e}", file=sys.stderr)
        return None


def compute_status(
    branch: str,
    hash_short: str,
    dirty: bool,
    review: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Compute status from git state and cached review.

    Returns a dict with keys:
      - reviewed: bool — review.branch == branch and review.lastRun is present
      - current: bool — review.commit == hash_short (only meaningful if reviewed=true)
      - dirty: bool — has uncommitted tracked changes
      - lastRun, commit, branch, reviewDir, reviewers, findings — from cache (or null)
    """
    if review is None:
        return {
            "reviewed": False,
            "current": False,
            "dirty": dirty,
            "lastRun": None,
            "commit": None,
            "branch": None,
            "reviewDir": None,
            "reviewers": None,
            "findings": None,
        }

    reviewed = review.get("branch") == branch and "lastRun" in review
    current = reviewed and review.get("commit") == hash_short

    return {
        "reviewed": reviewed,
        "current": current,
        "dirty": dirty,
        "lastRun": review.get("lastRun"),
        "commit": review.get("commit"),
        "branch": review.get("branch"),
        "reviewDir": review.get("reviewDir"),
        "reviewers": review.get("reviewers"),
        "findings": review.get("findings"),
    }


def format_human_readable(status: Dict[str, Any]) -> str:
    """Format status as a single human-readable line."""
    if not status["reviewed"]:
        return "Not reviewed"

    commit_line = (
        f"Already reviewed at commit {status['commit']}"
        if status["current"]
        else f"Already reviewed at commit {status['commit']} — HEAD is now {status.get('hash_current', '?')}"
    )
    return f"{commit_line}  ·  Last run: {status['lastRun']}"


def main():
    parser = argparse.ArgumentParser(
        description="Check if the current commit has already been reviewed."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output machine-readable JSON instead of human-readable text.",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress stdout; only set exit code (0 if reviewed and current and clean, 1 otherwise).",
    )

    args = parser.parse_args()

    try:
        project_root, branch, hash_short, dirty = get_git_info()
    except subprocess.CalledProcessError as e:
        print(f"Error: failed to resolve git state: {e}", file=sys.stderr)
        sys.exit(1)

    cache_path = Path(project_root) / ".claude" / "github-cache.json"
    review = read_review_cache(cache_path)
    status = compute_status(branch, hash_short, dirty, review)

    # Add current hash to status
    status["hash_current"] = hash_short

    # Determine exit code: 0 only if reviewed, current, and not dirty
    exit_code = (
        0
        if status["reviewed"] and status["current"] and not status["dirty"]
        else 1
    )

    if not args.quiet:
        if args.json:
            print(json.dumps(status))
        else:
            print(format_human_readable(status))

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
