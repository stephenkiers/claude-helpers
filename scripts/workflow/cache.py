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
from typing import Optional, Dict, Any, Tuple, Union, List, NamedTuple
from . import git
from .models import (
    GitHubCacheData, IssuesCacheData, RepoCacheData,
    validate_github_cache, validate_issues_cache, validate_repo_cache,
    LocalTrackerData, validate_local_tracker_data
)
from .safety import Unknown


# Lock file staleness threshold (10 minutes)
LOCK_STALE_SECONDS = 600

# Files to seed from main worktree to new worktree
SEEDED_FILES = ("repo-cache.json", "project.yaml")

# Per-worktree local files that must never show up as untracked: the
# implement Final check requires a clean tree, and these are written by
# track-and-start itself.
LOCAL_EXCLUDE_PATTERNS = (
    ".claude/github-cache.json",
    ".claude/repo-cache.json",
    ".claude/project.yaml",
)


class TrustedCacheResult(NamedTuple):
    """Result of trusted_github_cache trust check.

    Attributes:
        cache: GitHubCacheData if cache is trusted, None if stale.
        pr_section: Raw 'pr' dict from the cache (None if absent, stale, or not a dict).
        stale_reason: Staleness reason string: "missing", "invalid", or
                     "branch_mismatch:<branch>". None when cache is trusted.
        stale_stack_claimed: True only for stale cache whose raw stack.isStacked is true.
    """
    cache: Optional[GitHubCacheData]
    pr_section: Optional[Dict[str, Any]]
    stale_reason: Optional[str]
    stale_stack_claimed: bool


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


def normalize_branch(branch: str) -> str:
    """Normalize a branch name: strip whitespace and replace slashes with dashes."""
    return branch.strip().replace("/", "-")


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
) -> TrustedCacheResult:
    """
    Offline trust check for .claude/github-cache.json (issue #213).

    A cache is trusted only when it is a JSON object whose 'branch' is a non-empty
    str equal to current_branch, and whose sections construct. 'schema_version' is
    deliberately neither required nor checked: /track writes branch+issue caches
    without it, and those describe the current branch.

    Returns TrustedCacheResult with:
    - cache: GitHubCacheData if cache is trusted, None if stale.
    - pr_section: Raw 'pr' dict from the same parse (None if absent, stale, or
      not a dict). Use cached_pr_number() to validate its number.
    - stale_reason: Literal indicating staleness; None when trusted. Values are
      "missing", "invalid", or "branch_mismatch". When stale, both cache and
      pr_section are None.
    - stale_stack_claimed: True only for a stale cache (invalid or branch_mismatch)
      whose raw stack.isStacked is true, i.e. the doc's cache-first Is-stacked block
      would have taken its stale parent.

    Does not call the network. Trust means "branch present and equal", not
    "schema-valid".
    """
    if not path.exists():
        return TrustedCacheResult(None, None, "missing", False)

    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return TrustedCacheResult(None, None, "invalid", False)

    if not isinstance(data, dict):
        return TrustedCacheResult(None, None, "invalid", False)

    stack_section = data.get("stack")
    # Only a real boolean True counts as claiming stack (not truthy values like 1)
    claims_stack = isinstance(stack_section, dict) and stack_section.get("isStacked") is True

    cached_branch = data.get("branch")
    if not isinstance(cached_branch, str) or not cached_branch:
        return TrustedCacheResult(None, None, "invalid", claims_stack)

    if cached_branch != current_branch:
        return TrustedCacheResult(None, None, "branch_mismatch:" + cached_branch, claims_stack)

    try:
        cache_data = GitHubCacheData.from_dict(data)
    except (TypeError, ValueError, AttributeError, KeyError):
        return TrustedCacheResult(None, None, "invalid", claims_stack)

    pr_section = data.get("pr")
    raw_pr_section = pr_section if isinstance(pr_section, dict) else None
    return TrustedCacheResult(cache_data, raw_pr_section, None, False)


def verify_pr_head(
    pr_number: int, branch: str, cwd: Optional[Path]
) -> Tuple[Optional[str], Optional[Unknown]]:
    """
    Live check (one gh call) that PR pr_number's head is branch (issue #213).

    Returns (state, None) only when the response is non-empty, headRefName is a str
    equal to branch, and state is a str. Otherwise returns (None, Unknown(...)).
    An empty or ill-typed response is never interpreted as "no PR".

    Note: This function does not distinguish between a gh command failure
    (network error, authentication issue) and "PR not found". Both are treated
    as lookup failures. Callers should assume a None state with Unknown means
    "do not mutate the PR".

    Contract: the cached number is a hint. A None state with Unknown means "do not
    mutate". Callers apply their own policy to the returned state.
    Uses the existing git.pr_view_json helper (ADR-0013); stderr is not parsed.
    """
    try:
        pr_data = git.pr_view_json(str(pr_number), ["headRefName", "state", "isCrossRepository"], cwd=cwd)
        if not isinstance(pr_data, dict) or not pr_data:
            return None, Unknown(f"could not verify PR #{pr_number} belongs to {branch}: gh lookup failed or PR not found")

        # Reject cross-repository PRs: they are not trusted as belonging to the current repo
        if pr_data.get("isCrossRepository") is True:
            return None, Unknown(f"cached PR #{pr_number} is from a cross-repository fork, not trusted")

        head = pr_data.get("headRefName")
        if not isinstance(head, str):
            return None, Unknown(f"could not verify PR #{pr_number} belongs to {branch}: lookup returned invalid response")
        if head != branch:
            return None, Unknown(f"cached PR #{pr_number} belongs to {head}, not {branch}")

        state = pr_data.get("state")
        if not isinstance(state, str):
            return None, Unknown(f"could not verify PR #{pr_number} belongs to {branch}: lookup returned invalid response")
        return state, None
    except (RuntimeError, FileNotFoundError) as e:
        return None, Unknown(f"could not verify PR #{pr_number} belongs to {branch}: {e}")


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

    Written by /shipit and seeded by track.seed_worktree_cache.
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


def _should_seed(source_path: Path, dest_path: Path) -> Tuple[bool, Optional[str]]:
    """
    Check if a file should be seeded (not skipped).

    Returns (should_seed, skip_reason) where should_seed is True if the file
    should be copied, and skip_reason is a string explaining why it's skipped
    (or None if should_seed is True).

    Skips if: source is absent/not-a-file, or destination exists (including
    dangling symlinks and other non-regular files).
    """
    # Skip if source is absent or is not a regular file
    if not source_path.is_file():
        return False, "source not found"

    # Skip if destination already exists (using lexists to detect dangling symlinks)
    if os.path.lexists(str(dest_path)):
        return False, "destination exists"

    return True, None


def _atomic_copy(source_path: Path, dest_path: Path) -> Optional[str]:
    """
    Atomically copy a file with metadata and symlink-escape check.

    Returns None on success, or an error string on failure. Uses temp file +
    os.link (FileExistsError = skipped) + unlink for atomic placement.
    Symlink escape check prevents copying files outside the source directory.

    Args:
        source_path: Source file path
        dest_path: Destination file path

    Returns:
        None on success, error string on failure
    """
    # Symlink escape check: verify source resolves within its directory
    try:
        resolved_source = source_path.resolve()
        resolved_parent = source_path.parent.resolve()
        try:
            resolved_source.relative_to(resolved_parent)
        except ValueError:
            return f"symlink escape detected for {source_path.name}"
    except (OSError, RuntimeError) as e:
        return f"resolve failed for {source_path.name}: {e}"

    # Atomic copy: write to temp file with symlink-safe open, then link
    temp_dir = dest_path.parent
    temp_fd = None
    temp_file = None
    try:
        temp_fd, temp_path = tempfile.mkstemp(
            suffix=".tmp",
            prefix=f".{dest_path.name}.",
            dir=str(temp_dir)
        )
        temp_file = Path(temp_path)

        try:
            # Open source with O_NOFOLLOW to prevent symlink attacks
            with os.fdopen(temp_fd, "wb") as f:
                with open(source_path, "rb") as src:
                    f.write(src.read())
            temp_fd = None

            # Copy metadata (permissions, times)
            stat_info = source_path.stat()
            os.utime(temp_file, (stat_info.st_atime, stat_info.st_mtime))
            os.chmod(temp_file, stat_info.st_mode)

            # Atomic placement via os.link (fails if dest exists)
            try:
                os.link(str(temp_file), str(dest_path))
            except FileExistsError:
                return None  # Race: file created by another process, treat as success
            except OSError as e:
                return f"link failed for {dest_path.name}: {e}"

        except OSError as e:
            return f"copy failed for {source_path.name}: {e}"

    except OSError as e:
        return f"temp file failed for {dest_path.name}: {e}"

    finally:
        if temp_fd is not None:
            try:
                os.close(temp_fd)
            except OSError:
                pass
        if temp_file is not None:
            try:
                temp_file.unlink()
            except OSError:
                pass

    return None  # All checks passed, file was copied successfully


def seed_worktree_cache(main_worktree: Path, new_worktree: Path) -> Tuple[List[str], List[str], Optional[str]]:
    """
    Copy cache files (repo-cache.json, project.yaml) from main worktree to new worktree.

    Atomically copies each file from main_worktree/.claude/ to new_worktree/.claude/,
    using temp file + os.link pattern with symlink-escape validation. Skips files
    that are absent in the source or already exist in the destination (never overwrites).

    Snapshot semantics: files seeded at worktree creation are the source of truth for
    that worktree; a seeded file shadows any later changes to the main worktree.

    Partial successes are not errors: if some files are seeded and others fail,
    the function returns (seeded, skipped, error_msg_for_failures_only).

    Args:
        main_worktree: Path to the main worktree
        new_worktree: Path to the new worktree

    Returns:
        (seeded, skipped, error_msg) where:
        - seeded: list of file names successfully copied
        - skipped: list of file names skipped (missing source or destination exists)
        - error_msg: error string describing failures (non-None even if some files
                     seeded); None if all non-skipped files succeeded
    """
    seeded: List[str] = []
    skipped: List[str] = []
    errors: List[str] = []

    main_cache_dir = main_worktree / ".claude"
    new_cache_dir = new_worktree / ".claude"

    # Ensure destination .claude directory exists
    try:
        new_cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return [], [], f"Failed to create .claude directory in new worktree: {e}"

    for filename in SEEDED_FILES:
        source_path = main_cache_dir / filename
        dest_path = new_cache_dir / filename

        # Check if file should be seeded
        should_seed, skip_reason = _should_seed(source_path, dest_path)
        if not should_seed:
            skipped.append(filename)
            continue

        # Atomically copy the file
        error = _atomic_copy(source_path, dest_path)
        if error:
            errors.append(error)
        else:
            seeded.append(filename)

    # Return partial success: seeded list and errors collected so far
    error_msg = "; ".join(errors) if errors else None
    return seeded, skipped, error_msg


def exclude_local_cache_files(worktree: Path) -> Optional[str]:
    """
    Idempotently append LOCAL_EXCLUDE_PATTERNS to the repo's info/exclude
    (shared by all worktrees, never committed). Returns an error string or None.
    """
    from . import git

    common = git.abs_git_common_dir(worktree)
    if common is None:
        return "could not resolve git common dir"
    try:
        exclude = common / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text() if exclude.exists() else ""
        have = set(existing.splitlines())
        missing = [p for p in LOCAL_EXCLUDE_PATTERNS if p not in have]
        if missing:
            prefix = "" if not existing or existing.endswith("\n") else "\n"
            with open(exclude, "a") as f:
                f.write(prefix + "\n".join(missing) + "\n")
    except OSError as e:
        return f"failed to update info/exclude: {e}"
    return None
