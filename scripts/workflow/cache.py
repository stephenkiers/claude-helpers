"""
Cache reading, validation, hashing, and freshness checks.

Reads: read_github_cache, read_issues_cache, read_repo_cache, read_local_tracker.
Writes: write_cache (atomic rename via a temp file) and write_local_tracker,
plus lock-file helpers for coordinating concurrent writers.
"""

import json
import hashlib
import os
import tempfile
import time
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, Union, List
from . import git
from .models import (
    GitHubCacheData, IssuesCacheData, RepoCacheData,
    validate_github_cache, validate_issues_cache, validate_repo_cache,
    LocalTrackerData, validate_local_tracker_data
)
from .safety import Unknown


# Lock file staleness threshold (10 minutes)
LOCK_STALE_SECONDS = 600


def read_github_cache(path: Path) -> Tuple[Optional[GitHubCacheData], Optional[Unknown]]:
    """
    Read and validate .claude/github-cache.json.

    Returns (GitHubCacheData or None, Unknown error or None).
    """
    if not path.exists():
        return None, Unknown("github-cache.json not found")

    try:
        data = json.loads(path.read_text())
        if not validate_github_cache(data):
            return None, Unknown("github-cache.json schema validation failed")
        return GitHubCacheData.from_dict(data), None
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
        return None, Unknown(f"Failed to read github-cache.json: {e}")


def positive_int(value: Any) -> Optional[int]:
    """Return value if it is a positive int (bool excluded), else None."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def cached_pr_number(pr_section: Optional[Dict[str, Any]]) -> Optional[int]:
    """
    Extract the cached PR number from a raw github-cache 'pr' section.

    The number is a hint, not a fact: callers must verify it with verify_pr_head
    before mutating or trusting it. Anything other than a positive int is absent.
    """
    if not pr_section:
        return None
    return positive_int(pr_section.get("number"))


def trusted_github_cache(
    path: Path, current_branch: str
) -> Tuple[Optional[GitHubCacheData], Optional[Dict[str, Any]], Optional[str], bool]:
    """
    Offline trust check for .claude/github-cache.json (issue #213).

    A cache is trusted only when it is a JSON object whose 'branch' is a non-empty
    str equal to current_branch, and whose sections construct. 'schema_version' is
    deliberately neither required nor checked: /track writes branch+issue caches
    without it, and those describe the current branch.

    Returns (trusted_cache, raw_pr_section, stale_reason, stale_stack_claimed):
    - stale_reason is None when trusted; otherwise "missing", "invalid", or
      "branch_mismatch:<cached-branch>". When stale, both data values are None.
    - stale_stack_claimed is True only for a stale cache (invalid or branch_mismatch)
      whose raw stack.isStacked is true, i.e. the doc's cache-first Is-stacked block
      would have taken its stale parent.
    - raw_pr_section is the raw 'pr' dict from the same parse (None if absent or
      not a dict). Use cached_pr_number() to validate its number.

    Does not call the network. Trust means "branch present and equal", not
    "schema-valid".
    """
    if not path.exists():
        return None, None, "missing", False

    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None, None, "invalid", False

    if not isinstance(data, dict):
        return None, None, "invalid", False

    stack_section = data.get("stack")
    claims_stack = isinstance(stack_section, dict) and stack_section.get("isStacked") is True

    cached_branch = data.get("branch")
    if not isinstance(cached_branch, str) or not cached_branch:
        return None, None, "invalid", claims_stack

    if cached_branch != current_branch:
        return None, None, f"branch_mismatch:{cached_branch}", claims_stack

    try:
        cache_data = GitHubCacheData.from_dict(data)
    except (TypeError, ValueError, AttributeError, KeyError):
        return None, None, "invalid", claims_stack

    pr_section = data.get("pr")
    raw_pr_section = pr_section if isinstance(pr_section, dict) else None
    return cache_data, raw_pr_section, None, False


def verify_pr_head(
    pr_number: int, branch: str, cwd: Optional[Path]
) -> Tuple[Optional[str], Optional[Unknown]]:
    """
    Live check (one gh call) that PR pr_number's head is branch (issue #213).

    Returns (state, None) only when the response is non-empty, headRefName is a str
    equal to branch, and state is a str. Otherwise returns (None, Unknown(...)).
    An empty or ill-typed response is never interpreted as "no PR".

    Contract: the cached number is a hint. A None state with Unknown means "do not
    mutate". Callers apply their own policy to the returned state.
    Uses the existing git.pr_view_json helper (ADR-0013); stderr is not parsed.
    """
    pr_data = git.pr_view_json(str(pr_number), ["headRefName", "state"], cwd=cwd)
    if not isinstance(pr_data, dict) or not pr_data:
        return None, Unknown(f"could not verify PR #{pr_number} belongs to {branch}: lookup failed")

    head = pr_data.get("headRefName")
    if not isinstance(head, str):
        return None, Unknown(f"could not verify PR #{pr_number} belongs to {branch}: lookup failed")
    if head != branch:
        return None, Unknown(f"cached PR #{pr_number} belongs to {head}, not {branch}")

    state = pr_data.get("state")
    if not isinstance(state, str):
        return None, Unknown(f"could not verify PR #{pr_number} belongs to {branch}: lookup failed")
    return state, None


def read_issues_cache(path: Path) -> Tuple[Optional[IssuesCacheData], Optional[Unknown]]:
    """
    Read and validate local issues.json.

    Returns (IssuesCacheData or None, Unknown error or None).
    """
    if not path.exists():
        return None, Unknown("issues.json not found")

    try:
        data = json.loads(path.read_text())
        if not validate_issues_cache(data):
            return None, Unknown("issues.json schema validation failed")
        parsed = IssuesCacheData.from_dict(data)
        if parsed.dropped_keys:
            return parsed, Unknown(f"issues.json dropped malformed entries: {', '.join(parsed.dropped_keys)}")
        return parsed, None
    except (json.JSONDecodeError, OSError) as e:
        return None, Unknown(f"Failed to read issues.json: {e}")


def read_repo_cache(path: Path) -> Tuple[Optional[RepoCacheData], Optional[Unknown]]:
    """
    Read and validate .claude/repo-cache.json.

    Returns (RepoCacheData or None, Unknown error or None).

    Implemented ahead of schedule relative to RepoCacheData's "future use;
    stubbed for Phase 1" marker: no caller writes repo-cache.json yet, so
    this has no test coverage against a real file today. Kept (not
    removed) because the read path is simple, side-effect-free, and
    follows the same contract as its two siblings above — Phase 2's
    writer can add coverage when it lands.
    """
    if not path.exists():
        return None, Unknown("repo-cache.json not found")

    try:
        data = json.loads(path.read_text())
        if not validate_repo_cache(data):
            return None, Unknown("repo-cache.json schema validation failed")
        return RepoCacheData.from_dict(data), None
    except (json.JSONDecodeError, OSError) as e:
        return None, Unknown(f"Failed to read repo-cache.json: {e}")


def hash_file_content(content: str) -> str:
    """Compute SHA256 hash of content."""
    return hashlib.sha256(content.encode()).hexdigest()


def hash_cache_file(path: Path) -> Optional[str]:
    """Compute content hash of a cache file if it exists."""
    if not path.exists():
        return None
    try:
        content = path.read_text()
        return hash_file_content(content)
    except OSError:
        return None


def hash_github_cache_file(path: Path) -> Optional[str]:
    """Compute content hash of github-cache.json if it exists."""
    return hash_cache_file(path)


def hash_issues_cache_file(path: Path) -> Optional[str]:
    """Compute content hash of issues.json if it exists."""
    return hash_cache_file(path)


def read_local_tracker(path: Path) -> Tuple[Optional[LocalTrackerData], Optional[Unknown]]:
    """
    Read and validate project-root array-format issues.json (Local Plan Mode).

    Returns (LocalTrackerData or None, Unknown error or None).
    Missing file is distinct from malformed JSON or validation failure — include the path
    in the reason so "issues.json not found" is distinguishable from other files named
    issues.json elsewhere (e.g., worktree-parent issues.json read by read_issues_cache).
    """
    if not path.exists():
        return None, Unknown(f"local tracker issues.json not found at {path}")

    try:
        data = json.loads(path.read_text())
        if not validate_local_tracker_data(data):
            return None, Unknown("local tracker issues.json schema validation failed")
        parsed = LocalTrackerData.from_dict(data)
        if parsed.dropped_entries:
            return parsed, Unknown(f"local tracker issues.json dropped malformed entries: {', '.join(parsed.dropped_entries)}")
        return parsed, None
    except (json.JSONDecodeError, OSError) as e:
        return None, Unknown(f"Failed to read local tracker issues.json: {e}")


def write_cache(path: Path, data: Union[Dict[str, Any], List[Dict[str, Any]]]) -> Tuple[bool, Optional[Unknown]]:
    """
    Write cache file atomically with lock file serialization.

    Decision 1: temp-file + os.replace() atomic rename, plus sidecar lock file
    to serialize concurrent writers. Lock is created with os.open(..., os.O_CREAT | os.O_EXCL)
    for atomic create-if-absent. Returns (False, Unknown(...)) if lock already exists
    rather than blocking.

    On success or failure, cleans up temp file and lock file properly via try/finally.

    Lock file contains PID and timestamp for staleness detection. If an existing lock
    is older than LOCK_STALE_SECONDS, it's treated as stale and removed (retry once).
    """
    lock_path = path.with_suffix(path.suffix + ".lock")

    def acquire_lock() -> Tuple[bool, Optional[Unknown]]:
        """Try to acquire the lock, with staleness check."""
        try:
            lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(lock_fd, f"{os.getpid()} {time.time()}".encode())
            finally:
                os.close(lock_fd)
            return True, None
        except FileExistsError:
            # Check for stale lock
            try:
                lock_content = lock_path.read_text().strip()
                parts = lock_content.split()
                if len(parts) >= 2:
                    timestamp = float(parts[1])
                    age = time.time() - timestamp
                    if age > LOCK_STALE_SECONDS:
                        # Lock is stale, remove it and retry once
                        try:
                            lock_path.unlink()
                        except OSError:
                            pass
                        # Try to acquire again
                        try:
                            lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                            try:
                                os.write(lock_fd, f"{os.getpid()} {time.time()}".encode())
                            finally:
                                os.close(lock_fd)
                            return True, None
                        except FileExistsError:
                            return False, Unknown("cache locked by a concurrent writer")
                        except OSError as e:
                            # Unlink lock file on write error
                            try:
                                lock_path.unlink()
                            except OSError:
                                pass
                            return False, Unknown(f"Failed to write lock file: {e}")
            except (ValueError, OSError):
                # Can't parse lock file or read it, assume it's locked
                pass
            return False, Unknown("cache locked by a concurrent writer")
        except OSError as e:
            # Unlink lock file on creation/write error
            try:
                lock_path.unlink()
            except OSError:
                pass
            return False, Unknown(f"Failed to write lock file: {e}")

    try:
        lock_acquired, lock_error = acquire_lock()
        if not lock_acquired:
            return False, lock_error

        try:
            path.parent.mkdir(parents=True, exist_ok=True)

            fd, temp_path = tempfile.mkstemp(
                suffix=".tmp",
                prefix=f".{path.name}.",
                dir=str(path.parent)
            )
            temp_file = Path(temp_path)

            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f)

                os.replace(str(temp_file), str(path))
                return True, None

            except Exception as e:
                try:
                    temp_file.unlink()
                except OSError:
                    pass
                return False, Unknown(f"Failed to write cache: {e}")

        finally:
            try:
                lock_path.unlink()
            except OSError:
                pass

    except Exception as e:
        return False, Unknown(f"Unexpected error in write_cache: {e}")


def write_local_tracker(path: Path, data: LocalTrackerData) -> Tuple[bool, Optional[Unknown]]:
    """
    Write local tracker (project-root array-format issues.json) atomically.

    Delegates to write_cache, passing data.to_dict() which returns a top-level list.
    """
    return write_cache(path, data.to_dict())
