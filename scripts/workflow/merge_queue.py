"""
Local merge queue: serialized rebase, full gate, pinned force-push, squash-merge.

Coordinates multiple PRs in a repository with a kernel flock on merge.lock,
ensuring each PR is tested in full against the exact origin/<base> it lands on,
strictly one at a time, in arrival order.

Subcommands:
  enqueue (default) -- queue a PR for merge
  status -- show queue state
  reverify [SHA] -- clear a base-failed record
  bootstrap [--yes] -- anchor the base history
  resume [--pr N] -- resume a failed Claude hand-off
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field, asdict, fields
from enum import Enum
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple, Sequence

from . import git


# ============================================================================
# Exceptions
# ============================================================================

class LayoutMismatchError(RuntimeError):
    """Raised when the git layout does not match the worktrees/ structure."""
    pass


# ============================================================================
# Module-level utilities
# ============================================================================

# Compiled regex for validating 40-character hexadecimal SHA
SHA40_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def _is_valid_sha40(sha: str) -> bool:
    """Check if sha is a valid 40-hex string."""
    return SHA40_PATTERN.fullmatch(sha) is not None


# Module-level constants (with rationale comments)
POLL_SLEEP_INTERVAL = 1.0  # Poll for lower tickets and PR convergence once per second
GRACE_PERIOD_BEFORE_KILL = 5.0  # Grace period (seconds) before SIGKILL after SIGTERM
SCAN_BASE_MAX_DEPTH = 100  # Maximum depth to scan in base commit history for poison checks
PRUNE_OLD_RESULTS_LIMIT = 100  # Keep the most recent N result files to prevent unbounded growth

# Per-worktree direnv variables removed — not replaced — from the env of steps run in the
# scratch checkout. The default for merge-queue.json's `scratch_env_strip`. A step that needs
# a Compose project or database must set its own (`-p`, top-level `name:`, or inline assignment).
SCRATCH_ENV_STRIPPED_VARS = ("COMPOSE_PROJECT_NAME", "DATABASE_URL")


# ============================================================================
# Step Definition (normalized from Union[str, Dict])
# ============================================================================

@dataclass
class Step:
    """Normalized step definition."""
    cmd: str
    timeout_secs: Optional[int] = None


# ============================================================================
# Config (Step 1)
# ============================================================================

@dataclass
class MergeQueueConfig:
    """Configuration for the merge queue."""
    base: str
    steps: List[Step]  # Normalized step definitions
    cleanup: bool = False
    scratch_setup: List[str] = field(default_factory=list)
    inherit_lock_fd: bool = False
    allow_unverified: List[str] = field(default_factory=list)
    mutation_timeout_secs: int = 300  # Timeout for long-running git mutations (default 5 min)
    pr_merge_poll_secs: int = 60  # Timeout for polling PR metadata before merge (default 60s at ~1s intervals)
    scratch_env_strip: List[str] = field(default_factory=lambda: list(SCRATCH_ENV_STRIPPED_VARS))
    scratch_env: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization, converting Step objects to dicts."""
        result = asdict(self)
        # Ensure steps are represented as dicts for JSON
        if "steps" in result:
            steps_list = []
            for step in result["steps"]:
                if isinstance(step, dict):
                    steps_list.append(step)
                elif isinstance(step, Step):
                    steps_list.append(asdict(step))
                else:
                    steps_list.append(step)
            result["steps"] = steps_list
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MergeQueueConfig":
        """Construct from parsed JSON dict, normalizing steps to Step objects."""
        field_names = {f.name for f in fields(cls)}
        filtered_data = {k: v for k, v in data.items() if k in field_names}

        # Normalize steps from Union[str, Dict] to List[Step]
        if "steps" in filtered_data and filtered_data["steps"]:
            normalized_steps = []
            for step_data in filtered_data["steps"]:
                if isinstance(step_data, Step):
                    # Already a Step object (from to_dict -> asdict -> Step)
                    normalized_steps.append(step_data)
                elif isinstance(step_data, str):
                    normalized_steps.append(Step(cmd=step_data))
                elif isinstance(step_data, dict):
                    normalized_steps.append(Step(
                        cmd=step_data["cmd"],
                        timeout_secs=step_data.get("timeout_secs")
                    ))
            filtered_data["steps"] = normalized_steps

        return cls(**filtered_data)


def _check_outside_worktree(resolved: Path, source_label: str, cwd: Optional[Path] = None) -> Path:
    """
    Verify `resolved` is outside the PR worktree.

    Trust boundary: config must not be under git-common-dir (the shared git dir),
    under the repository root (--show-toplevel), or under the worktrees/ container.
    Fails closed: if the boundary can't be verified, refuse rather than silently
    trusting a path that might be inside the PR worktree.
    """
    try:
        git_common_dir_str = git.get_git_common_dir(cwd=cwd)
        # If git-common-dir returns a relative path, resolve it against cwd
        if not Path(git_common_dir_str).is_absolute():
            git_common_dir = ((cwd or Path.cwd()).resolve() / git_common_dir_str).resolve()
        else:
            git_common_dir = Path(git_common_dir_str).resolve()
        show_toplevel = Path(git.get_repository_root(cwd=cwd)).resolve()
    except Exception as e:
        raise RuntimeError(
            f"{source_label} trust-boundary check failed: could not resolve git directories: {e}"
        ) from e

    # Reject if under git-common-dir (the shared git directory)
    if resolved == git_common_dir or git_common_dir in resolved.parents:
        raise RuntimeError(
            f"{source_label} path is inside the git directory; must be outside: {resolved}\n"
            f"Place config at <repo>/merge-queue.json or override with an absolute path outside the repository"
        )

    # Reject if under the repository root (--show-toplevel)
    if resolved == show_toplevel or show_toplevel in resolved.parents:
        raise RuntimeError(
            f"{source_label} path is inside the repository; must be outside: {resolved}\n"
            f"Place config outside the repository or override with an absolute path"
        )

    # Reject if under a worktrees/ container directory
    if resolved.parent.name == "worktrees" or any(p.name == "worktrees" for p in resolved.parents):
        raise RuntimeError(
            f"{source_label} path is under a worktrees/ directory; must be outside: {resolved}\n"
            f"Place config at <repo>/merge-queue.json or override with an absolute path outside the repository"
        )

    return resolved


def resolve_config_path(config_flag: Optional[str] = None, cwd: Optional[Path] = None) -> Path:
    """
    Resolve the merge-queue config location.

    Precedence:
    1. --config <path> flag
    2. MERGE_QUEUE_CONFIG env var
    3. Default: <container>/merge-queue.json where <container> is parent of worktrees/
       (also the parent of .bare/ for a bare-repo + worktrees/ layout)

    For flags and env vars: error if they resolve inside a PR worktree (trust boundary).
    Raises if the layout doesn't match and no override is provided.
    """
    if config_flag:
        return _check_outside_worktree(Path(config_flag).resolve(), "--config", cwd=cwd)

    if env_path := os.environ.get("MERGE_QUEUE_CONFIG"):
        return _check_outside_worktree(Path(env_path).resolve(), "MERGE_QUEUE_CONFIG", cwd=cwd)

    try:
        git_common_dir_str = git.get_git_common_dir(cwd=cwd)
        # If git-common-dir returns a relative path, resolve it against cwd
        if not Path(git_common_dir_str).is_absolute():
            git_common_dir = ((cwd or Path.cwd()).resolve() / git_common_dir_str).resolve()
        else:
            git_common_dir = Path(git_common_dir_str).resolve()
    except Exception as e:
        raise RuntimeError(
            f"Could not determine default config location: {e}\n"
            f"Use --config <path> or set MERGE_QUEUE_CONFIG"
        )

    try:
        # /setup-repo layout: <repo>/worktrees/main/.git (main checkout) and
        # <repo>/worktrees/pr-1/ (linked worktrees). For a linked worktree,
        # git-common-dir points to the main worktree's .git, so:
        #   git_common_dir               = <repo>/worktrees/main/.git
        #   git_common_dir.parent        = <repo>/worktrees/main
        #   git_common_dir.parent.parent = <repo>/worktrees
        #   git_common_dir.parent.parent.parent = <repo>
        # Bare-repo layout: <repo>/.bare (data) + <repo>/.git gitfile + <repo>/worktrees/*.
        # Every worktree's git-common-dir is <repo>/.bare, so the container is its parent.
        if git_common_dir.name == ".bare" and (git_common_dir.parent / "worktrees").is_dir():
            return git_common_dir.parent / "merge-queue.json"
        if git_common_dir.parent.parent.name != "worktrees":
            raise LayoutMismatchError(
                "Layout does not match: git path does not show 'worktrees' at expected location\n"
                "Use --config <path> or set MERGE_QUEUE_CONFIG"
            )
        container = git_common_dir.parent.parent.parent
        return container / "merge-queue.json"
    except LayoutMismatchError:
        raise
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(
            f"Could not derive default config location: {e}\n"
            f"Use --config <path> or set MERGE_QUEUE_CONFIG"
        )


def _validate_positive_int(value: Any, field_label: str) -> None:
    """Raise ValueError unless value is a positive int (bool excluded, since bool is a subclass of int)."""
    if not (isinstance(value, int) and not isinstance(value, bool)):
        raise ValueError(f"{field_label} must be an integer")
    if value <= 0:
        raise ValueError(f"{field_label} must be a positive integer")


def load_and_validate_config(config_path: Path) -> MergeQueueConfig:
    """
    Load and validate the merge-queue config.

    Enforces strict schema:
    - base: non-empty string, must equal repo's default branch
    - steps: non-empty list of non-empty strings or {cmd: str, timeout_secs?: int}
    - cleanup: bool
    - scratch_setup: list of strings (optional)
    - scratch_env_strip: list of non-empty strings (optional)
    - scratch_env: object mapping strings to strings (optional)
    - inherit_lock_fd: bool (optional, default false)
    - allow_unverified: list of 40-hex shas (optional)

    Rejects unknown keys.
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    try:
        with open(config_path) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in config: {e}")

    return validate_config_data(data)


def validate_config_data(data: Any, cwd: Optional[Path] = None) -> MergeQueueConfig:
    """
    Validate an already-parsed config object against the strict schema
    (see load_and_validate_config). `cwd` selects the repository whose default
    branch `base` must equal; None means the process cwd.
    """
    if not isinstance(data, dict):
        raise ValueError("Config must be a JSON object (dict), not a list or primitive value")
    data = dict(data)

    known_keys = {"base", "steps", "cleanup", "scratch_setup", "scratch_env_strip", "scratch_env", "inherit_lock_fd", "allow_unverified", "mutation_timeout_secs", "pr_merge_poll_secs"}
    unknown = set(data.keys()) - known_keys
    if unknown:
        raise ValueError(f"Unknown config keys: {unknown}")

    if not isinstance(data.get("base"), str) or not data["base"]:
        raise ValueError("base must be a non-empty string")

    if not isinstance(data.get("steps"), list) or not data["steps"]:
        raise ValueError("steps must be a non-empty list")

    for step in data["steps"]:
        if isinstance(step, str):
            if not step:
                raise ValueError("steps: each step string must be non-empty")
        elif isinstance(step, dict):
            # Reject unknown keys in dict steps
            allowed_keys = {"cmd", "timeout_secs"}
            unknown_keys = set(step.keys()) - allowed_keys
            if unknown_keys:
                raise ValueError(f"steps: unknown keys in dict step: {unknown_keys}")
            if not isinstance(step.get("cmd"), str) or not step["cmd"]:
                raise ValueError("steps: each dict step must have a non-empty 'cmd' string")
            if "timeout_secs" in step:
                _validate_positive_int(step["timeout_secs"], "steps: timeout_secs")
        else:
            raise ValueError("steps: each step must be a string or dict")

    if "cleanup" in data and not isinstance(data["cleanup"], bool):
        raise ValueError("cleanup must be a boolean")

    if "scratch_setup" in data:
        if not isinstance(data["scratch_setup"], list):
            raise ValueError("scratch_setup must be a list of strings")
        for cmd in data["scratch_setup"]:
            if not isinstance(cmd, str) or not cmd:
                raise ValueError("scratch_setup: each command must be a non-empty string")

    if "scratch_env_strip" in data:
        if not isinstance(data["scratch_env_strip"], list):
            raise ValueError("scratch_env_strip must be a list of non-empty strings")
        for var in data["scratch_env_strip"]:
            if not isinstance(var, str) or not var:
                raise ValueError("scratch_env_strip must be a list of non-empty strings")

    if "scratch_env" in data:
        if not isinstance(data["scratch_env"], dict):
            raise ValueError("scratch_env must be an object mapping variable names to string values")
        for key, value in data["scratch_env"].items():
            if not isinstance(key, str) or not key:
                raise ValueError("scratch_env must be an object mapping variable names to string values")
            if not isinstance(value, str):
                raise ValueError("scratch_env must be an object mapping variable names to string values")

    if "inherit_lock_fd" in data and not isinstance(data["inherit_lock_fd"], bool):
        raise ValueError("inherit_lock_fd must be a boolean")

    if "allow_unverified" in data:
        if not isinstance(data["allow_unverified"], list):
            raise ValueError("allow_unverified must be a list of 40-hex shas")
        for sha in data["allow_unverified"]:
            if not isinstance(sha, str) or not _is_valid_sha40(sha):
                raise ValueError(f"allow_unverified: invalid sha {sha} (must be 40-hex)")

    if "mutation_timeout_secs" in data:
        _validate_positive_int(data["mutation_timeout_secs"], "mutation_timeout_secs")

    if "pr_merge_poll_secs" in data:
        _validate_positive_int(data["pr_merge_poll_secs"], "pr_merge_poll_secs")

    default_branch, err = git.get_default_branch(cwd=cwd) if cwd is not None else git.get_default_branch()
    if err or not default_branch:
        raise RuntimeError(f"Could not determine default branch: {err}")
    if data["base"] != default_branch:
        raise ValueError(
            f"base '{data['base']}' does not equal default branch '{default_branch}'"
        )

    # Normalize steps from Union[str, Dict] to List[Step]
    normalized_steps = []
    for step_data in data["steps"]:
        if isinstance(step_data, str):
            normalized_steps.append(Step(cmd=step_data))
        else:
            normalized_steps.append(Step(
                cmd=step_data["cmd"],
                timeout_secs=step_data.get("timeout_secs")
            ))
    data["steps"] = normalized_steps

    return MergeQueueConfig.from_dict(data)


def _touch_record(path: Path) -> None:
    """
    Create an empty marker file at path (mode 0o600).
    Safely opens and closes the fd to avoid leaks.
    """
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_CLOEXEC, 0o600)
    try:
        pass
    finally:
        os.close(fd)


# ============================================================================
# State Dir and Ticket Ordering (Step 2)
# ============================================================================

def get_state_dir() -> Path:
    """Get the merge-queue state directory."""
    git_common_dir = Path(git.get_git_common_dir()).resolve()
    state_dir = git_common_dir / "merge-queue"
    return state_dir


def ensure_state_dir() -> Path:
    """Create state dir with mode 0700 if it doesn't exist."""
    state_dir = get_state_dir()
    state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)

    # Ensure subdirs
    (state_dir / "tickets").mkdir(mode=0o700, exist_ok=True)
    (state_dir / "logs").mkdir(mode=0o700, exist_ok=True)
    (state_dir / "base-verified").mkdir(mode=0o700, exist_ok=True)
    (state_dir / "base-failed").mkdir(mode=0o700, exist_ok=True)
    (state_dir / "results").mkdir(mode=0o700, exist_ok=True)

    return state_dir


def get_scratch_dir() -> Path:
    """
    Get the scratch worktree directory.

    Deliberately placed outside the repo's own directory tree, unlike the rest of
    state_dir (which lives under git-common-dir). The scratch dir hosts a real
    checked-out git worktree, and ancestor-directory-walking tools run inside it
    (Cargo workspace discovery, relative sibling-repo path dependencies) walk
    upward from the scratch checkout looking for enclosing manifests/dirs. If
    scratch is nested anywhere under the repo's own checkout tree (e.g. under
    git-common-dir, which sits inside the main worktree), that walk escapes the
    scratch checkout and re-enters the enclosing repo, misresolving against the
    wrong copy. Keying by a hash of git-common-dir keeps one stable, reusable
    scratch location per repo across invocations.
    """
    git_common_dir = Path(git.get_git_common_dir()).resolve()
    digest = hashlib.sha256(str(git_common_dir).encode()).hexdigest()[:16]
    scratch_root = Path.home() / ".claude" / "merge-queue-scratch" / digest
    scratch_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    return scratch_root / "scratch"


def get_merge_lock_path() -> Path:
    """Get the merge.lock path."""
    return get_state_dir() / "merge.lock"


def get_alloc_lock_path() -> Path:
    """Get the alloc.lock path."""
    return get_state_dir() / "alloc.lock"


@dataclass
class Ticket:
    """Ticket metadata."""
    pr: int
    branch: str
    worktree: str
    enqueued_at: float


def acquire_ticket(pr: int, branch: str, worktree: str) -> Tuple[int, int]:
    """
    Acquire a ticket under alloc.lock.

    Returns (ticket_number, ticket_fd).

    Under alloc.lock:
    - List tickets/ and probe every lower ticket with LOCK_NB
    - Refuse if a live ticket already names the same PR (duplicate enqueue)
    - Compute n = max(live)+1 (or 1 if none)
    - Create tickets/<n:012d> with O_EXCL|O_CLOEXEC
    - Write ticket metadata
    - flock(LOCK_EX)
    - Release alloc.lock and return
    """
    state_dir = ensure_state_dir()
    tickets_dir = state_dir / "tickets"
    alloc_lock_path = get_alloc_lock_path()

    alloc_lock_fd = os.open(str(alloc_lock_path), os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        # Acquire alloc.lock
        fcntl.flock(alloc_lock_fd, fcntl.LOCK_EX)

        # List existing tickets and probe
        live_tickets = []
        try:
            ticket_files = sorted([f for f in tickets_dir.iterdir() if f.is_file()])
            for ticket_file in ticket_files:
                # Validate ticket filename before int()-parsing
                if not ticket_file.name.isdigit():
                    continue
                try:
                    ticket_fd = os.open(str(ticket_file), os.O_RDONLY | os.O_CLOEXEC)
                    try:
                        # Try to acquire non-blocking lock; if it fails, ticket is live
                        fcntl.flock(ticket_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        # Ticket is dead, unlock and close
                        fcntl.flock(ticket_fd, fcntl.LOCK_UN)
                        ticket_file.unlink(missing_ok=True)
                    except BlockingIOError:
                        # Ticket is live
                        try:
                            live_tickets.append(int(ticket_file.name))
                        except ValueError:
                            # Shouldn't happen given the filename check above, but be safe
                            pass
                    finally:
                        os.close(ticket_fd)
                except OSError:
                    pass
        except OSError:
            pass

        # Check for duplicate PR
        for ticket_num in live_tickets:
            ticket_path = tickets_dir / f"{ticket_num:012d}"
            try:
                with open(ticket_path) as f:
                    data = json.load(f)
                    if data.get("pr") == pr:
                        raise RuntimeError(f"PR {pr} already enqueued (ticket {ticket_num})")
            except (json.JSONDecodeError, OSError):
                pass

        # Compute next ticket number
        next_num = (max(live_tickets) + 1) if live_tickets else 1

        # Create ticket file with O_EXCL
        ticket_path = tickets_dir / f"{next_num:012d}"
        ticket_fd = os.open(
            str(ticket_path),
            os.O_CREAT | os.O_WRONLY | os.O_EXCL | os.O_CLOEXEC,
            0o600
        )

        # Write ticket metadata
        ticket_data = Ticket(pr=pr, branch=branch, worktree=worktree, enqueued_at=time.time())
        metadata = json.dumps(asdict(ticket_data))
        os.write(ticket_fd, metadata.encode())

        # flock(LOCK_EX) the ticket
        fcntl.flock(ticket_fd, fcntl.LOCK_EX)

        return next_num, ticket_fd

    finally:
        # Release alloc.lock
        try:
            fcntl.flock(alloc_lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(alloc_lock_fd)


def wait_turn(ticket_num: int) -> int:
    """
    Wait for our ticket's turn.

    Poll about once a second. Under alloc.lock, probe and unlink dead lower tickets.
    Once no live lower ticket remains, block on flock(merge.lock, LOCK_EX).

    Returns the merge_lock_fd.
    """
    state_dir = ensure_state_dir()
    tickets_dir = state_dir / "tickets"
    alloc_lock_path = get_alloc_lock_path()
    merge_lock_path = get_merge_lock_path()

    # Open merge.lock (never unlink, O_CLOEXEC)
    merge_lock_fd = os.open(
        str(merge_lock_path),
        os.O_CREAT | os.O_RDWR | os.O_CLOEXEC,
        0o600
    )

    while True:
        # Check for lower live tickets
        alloc_lock_fd = os.open(str(alloc_lock_path), os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(alloc_lock_fd, fcntl.LOCK_EX)

            has_lower_live = False
            try:
                for ticket_file in sorted(tickets_dir.iterdir()):
                    if not ticket_file.is_file():
                        continue
                    # Validate ticket filename before int()-parsing
                    if not ticket_file.name.isdigit():
                        continue
                    try:
                        n = int(ticket_file.name)
                    except ValueError:
                        # Non-numeric filename; skip
                        continue
                    if n >= ticket_num:
                        continue

                    try:
                        fd = os.open(str(ticket_file), os.O_RDONLY | os.O_CLOEXEC)
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            fcntl.flock(fd, fcntl.LOCK_UN)
                            ticket_file.unlink(missing_ok=True)
                        except BlockingIOError:
                            has_lower_live = True
                        finally:
                            os.close(fd)
                    except OSError:
                        pass
            except OSError:
                pass

            if not has_lower_live:
                # No lower live tickets; try to acquire merge.lock
                try:
                    fcntl.flock(merge_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    # Got it!
                    fcntl.flock(merge_lock_fd, fcntl.LOCK_UN)
                    # Release alloc.lock and go to blocking acquire
                    fcntl.flock(alloc_lock_fd, fcntl.LOCK_UN)
                    break
                except BlockingIOError:
                    pass

            fcntl.flock(alloc_lock_fd, fcntl.LOCK_UN)
        except (OSError, BlockingIOError):
            pass
        finally:
            os.close(alloc_lock_fd)

        time.sleep(POLL_SLEEP_INTERVAL)

    # Now block on merge.lock
    try:
        fcntl.flock(merge_lock_fd, fcntl.LOCK_EX)
    except OSError:
        os.close(merge_lock_fd)
        raise
    return merge_lock_fd


def release_ticket(ticket_num: int, merge_lock_fd: int, ticket_fd: int = -1) -> None:
    """
    Release the ticket and merge.lock under alloc.lock.

    Unlink ticket, unlock merge.lock, close all held fds.

    M17: ticket_fd parameter ensures ticket fd is always closed in finally block.
    """
    state_dir = get_state_dir()
    tickets_dir = state_dir / "tickets"
    alloc_lock_path = get_alloc_lock_path()

    alloc_lock_fd = os.open(str(alloc_lock_path), os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(alloc_lock_fd, fcntl.LOCK_EX)

        ticket_path = tickets_dir / f"{ticket_num:012d}"
        ticket_path.unlink(missing_ok=True)

        fcntl.flock(alloc_lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(alloc_lock_fd)

    # M17: Close ticket_fd in finally to avoid leaking it
    try:
        # Guard against invalid fd (e.g., -1 from error path)
        if ticket_fd >= 0:
            try:
                fcntl.flock(ticket_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(ticket_fd)
            except OSError:
                pass
    finally:
        # Close merge_lock_fd
        if merge_lock_fd >= 0:
            try:
                fcntl.flock(merge_lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(merge_lock_fd)
            except OSError:
                pass


# ============================================================================
# Step Runner (Step 3)
# ============================================================================

@dataclass
class StepOutcome:
    """Outcome of running a step."""
    success: bool
    step_name: str = ""
    log_path: Optional[str] = None
    error: Optional[str] = None
    timed_out: bool = False


def run_step(
    cmd: str,
    cwd: Path,
    log_path: Path,
    timeout_secs: Optional[int] = None,
    lock_fd_to_inherit: Optional[int] = None,
    env: Optional[Dict[str, str]] = None,
) -> StepOutcome:
    """
    Run a step via subprocess.Popen with a new session.

    Uses shell=True (steps are maintainer-authored shell strings).
    Captures stdout/stderr to log_path.
    In finally: killpg(SIGTERM), wait 5s grace, then killpg(SIGKILL).

    If timeout_secs is set, kill the group on expiry and return timed_out=True.
    `env`, when given, is used verbatim as the child's environment; when None the child inherits
    this process's environment. Scratch-checkout callers pass `_scratch_step_env(config)`, which
    removes the `scratch_env_strip` variables (default `COMPOSE_PROJECT_NAME`, `DATABASE_URL`)
    without replacement.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)

    log_fd = os.open(str(log_path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    pgid: Optional[int] = None
    proc: Optional[subprocess.Popen] = None
    timed_out = False
    error: Optional[str] = None

    def term_handler(signum: int, frame: Any) -> None:
        nonlocal timed_out
        if signum in (signal.SIGTERM, signal.SIGINT):
            timed_out = True
            # Restore default handlers before raising to avoid re-entrancy
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            raise SystemExit(1)

    old_sigterm = signal.signal(signal.SIGTERM, term_handler)
    old_sigint = signal.signal(signal.SIGINT, term_handler)

    try:
        popen_kwargs: Dict[str, Any] = {
            "shell": True,
            "start_new_session": True,
            "stdout": log_fd,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
            "close_fds": True,
        }
        if env is not None:
            popen_kwargs["env"] = env
        if lock_fd_to_inherit is not None:
            popen_kwargs["pass_fds"] = (lock_fd_to_inherit,)

        proc = subprocess.Popen(cmd, cwd=str(cwd), **popen_kwargs)
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            # Process exited before we could get pgid
            pgid = None

        # Wait with timeout
        start = time.monotonic()
        while True:
            try:
                proc.wait(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                if timeout_secs is not None and (time.monotonic() - start) >= timeout_secs:
                    timed_out = True
                    raise TimeoutError(f"Step timed out after {timeout_secs}s")

    except TimeoutError as e:
        error = str(e)
    except SystemExit:
        # Re-raised from signal handler
        if proc is not None and error is None:
            error = "Interrupted by signal"
    except Exception as e:
        error = f"Step failed: {e}"
    finally:
        # Kill the process group if we have one
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGTERM)
                # Make grace sleep non-interruptible w.r.t. escalation
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                time.sleep(5.0)
            except ProcessLookupError:
                pass
            finally:
                # Restore original handlers
                signal.signal(signal.SIGTERM, old_sigterm)
                signal.signal(signal.SIGINT, old_sigint)

            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            # Restore handlers if we never got pgid
            signal.signal(signal.SIGTERM, old_sigterm)
            signal.signal(signal.SIGINT, old_sigint)

        os.close(log_fd)

    # Check exit code if process completed normally
    success = False
    if proc is not None and not timed_out and error is None:
        success = (proc.returncode == 0)
    elif error is None and not timed_out:
        success = True

    return StepOutcome(
        success=success,
        log_path=str(log_path),
        error=error,
        timed_out=timed_out,
    )


# ============================================================================
# Git/gh Runner Seam and Force-Push (Step 4)
# ============================================================================

class Runner:
    """
    Seam for all git/gh calls so tests can inject fakes.

    Validates branch and ref names, rejecting leading `-`, whitespace,
    and control characters. Passes them after `--` where git allows.
    """

    @staticmethod
    def validate_branch(branch: str) -> str:
        """Validate a branch name."""
        if not branch or branch.startswith("-"):
            raise ValueError(f"Invalid branch name: {branch}")
        # Reject whitespace and control characters
        if any(c.isspace() or ord(c) < 0x20 for c in branch):
            raise ValueError(f"Invalid branch name: {branch}")
        return branch

    @staticmethod
    def validate_ref(ref: str) -> str:
        """Validate a ref (40-hex sha or branch)."""
        if not ref or ref.startswith("-"):
            raise ValueError(f"Invalid ref: {ref}")
        # Reject whitespace and control characters
        if any(c.isspace() or ord(c) < 0x20 for c in ref):
            raise ValueError(f"Invalid ref: {ref}")
        return ref

    @staticmethod
    def run_git(args: List[str], cwd: Optional[Path] = None, check: bool = True, timeout: Optional[int] = None) -> str:
        """Run git with argv list."""
        if timeout is None:
            timeout = git.DEFAULT_TIMEOUT
        return git.run_git_command(args, cwd=cwd, check=check, timeout=timeout)

    @staticmethod
    def run_gh(args: List[str], cwd: Optional[Path] = None, check: bool = True, timeout: Optional[int] = None) -> str:
        """Run gh with argv list."""
        if timeout is None:
            timeout = git.DEFAULT_TIMEOUT
        return git.run_gh_command(args, cwd=cwd, check=check, timeout=timeout)

    @staticmethod
    def force_push_tested(
        branch: str,
        tested_sha: str,
        lease_sha: str,
        cwd: Optional[Path] = None,
        timeout: Optional[int] = None,
    ) -> bool:
        """
        Force-push with pinned lease.

        The ONLY force-push call site in merge_queue.py.
        Asserts both shas match ^[0-9a-f]{40}$ (anchored, no trailing newline).
        Runs: git push --force-with-lease=<branch>:<lease_sha> origin <tested_sha>:refs/heads/<branch>

        Returns True if push succeeded, False if lease rejected (no-op).
        Raises RuntimeError on timeout; other errors are raised as-is.
        """
        # Validate shas are exactly 40 hex digits (anchored regex)
        if not _is_valid_sha40(tested_sha.strip()):
            raise ValueError(f"tested_sha is not 40-hex: {tested_sha}")
        if not _is_valid_sha40(lease_sha.strip()):
            raise ValueError(f"lease_sha is not 40-hex: {lease_sha}")

        branch = Runner.validate_branch(branch)

        try:
            args = [
                "push",
                f"--force-with-lease={branch}:{lease_sha}",
                "origin",
                f"{tested_sha}:refs/heads/{branch}",
            ]
            Runner.run_git(args, cwd=cwd, timeout=timeout)
            return True
        except RuntimeError as e:
            # M2: Timeout; will be caught and handled by caller with re-querying
            if "timed out" in str(e):
                raise
            # Unexpected RuntimeError; re-raise
            raise
        except git.GitCommandError as e:
            # Lease rejection: "stale info" or "fast-forward check failed"
            # Other rejections (branch protection, hooks) should be raised
            error_msg = str(e).lower()
            if "stale" in error_msg or "fast-forward" in error_msg:
                # Lease was rejected (branch moved)
                return False
            # Branch protection, hooks, or other server-side rejections: raise
            raise


# ============================================================================
# Merge-Gate Trailer and Poison Scan (Step 5)
# ============================================================================

def build_trailer(tested_base: str, tested_head: str) -> str:
    """Build a Merge-Gate trailer."""
    return f"Merge-Gate: merge-queue tested-base={tested_base} tested-head={tested_head}"


def parse_trailer(commit: str, cwd: Optional[Path] = None) -> Tuple[Optional[str], Optional[str]]:
    """
    Parse Merge-Gate trailer from a commit.

    Runs git interpret-trailers --parse on the commit message via stdin.
    Matches only the final trailer block.
    Text in the subject or earlier in the body does not count.
    The trailer is valid only if tested-base is an ancestor of the commit.

    Returns (tested_base, tested_head) if valid, (None, None) otherwise.
    """
    try:
        # Get commit message via git show
        commit_msg = Runner.run_git(["show", "-s", "--format=%B", commit], cwd=cwd, check=False)
        # Pipe it to git interpret-trailers --parse
        output = git.run_git_command_input(
            ["interpret-trailers", "--parse"],
            input_data=commit_msg,
            cwd=cwd,
            check=False
        )
    except Exception:
        return None, None

    lines = output.strip().split("\n")
    for line in reversed(lines):
        if line.startswith("Merge-Gate:"):
            match = re.search(
                r"Merge-Gate: merge-queue tested-base=([0-9a-f]{40}) tested-head=([0-9a-f]{40})",
                line
            )
            if match:
                base, head = match.groups()
                # Decided #5: Verify tested-base is the immediate first parent, not just any ancestor
                try:
                    parent = Runner.run_git(["rev-parse", f"{commit}^1"], cwd=cwd).strip()
                    if parent == base:
                        return base, head
                except Exception:
                    # Could not get parent; fail closed
                    pass
            return None, None

    return None, None


def scan_base(
    base_sha: str,
    allow_unverified: List[str],
    cwd: Optional[Path] = None,
    max_depth: int = 100,
) -> Tuple[bool, List[str]]:
    """
    Scan origin/<base> first-parent history for unverified commits.

    An anchor is a commit with a valid trailer or a sha with a base-verified record.
    Shas in allow_unverified are skipped (excused individually) but the scan continues,
    so an allowlisted commit can't hide an untrailered commit below it.

    Caps the walk depth (default 100 commits) to avoid unbounded history traversal under lock.
    Fails closed: if max_depth is exceeded, returns (False, unverified) to trigger verification.

    Returns (has_anchor, unverified_commits).
    """
    state_dir = get_state_dir()
    verified_dir = state_dir / "base-verified"

    try:
        log_output = Runner.run_git(
            ["log", "--format=%H", f"--max-count={max_depth + 1}", "--first-parent", base_sha],
            cwd=cwd,
        )
    except Exception:
        return False, []

    commits = log_output.strip().split("\n")
    unverified: List[str] = []
    depth = 0

    for commit in commits:
        commit = commit.strip()
        if not commit:
            continue

        depth += 1
        if depth > max_depth:
            # Exceeded depth limit; fail closed
            return False, unverified

        # Check base-verified record
        if (verified_dir / commit).exists():
            return True, unverified

        # Check allow_unverified allowlist
        if commit in allow_unverified:
            continue

        # Check trailer
        tested_base, tested_head = parse_trailer(commit, cwd=cwd)
        if tested_base and tested_head:
            return True, unverified

        # This commit is unanchored
        unverified.append(commit)

    return False, unverified


# ============================================================================
# Outcomes (Step 6)
# ============================================================================

class MergeOutcome(Enum):
    """Outcome of a run_one() attempt."""
    MERGED = "merged"
    KICKBACK = "kickback"
    PUSHED_NOT_MERGED = "pushed_not_merged"
    REFUSED = "refused"
    INTERNAL_ERROR = "internal_error"


class MergePROutcome(Enum):
    """Outcome of a _merge_pr() attempt."""
    SUCCESS = "success"
    POLL_TIMEOUT = "poll_timeout"
    GH_MERGE_REJECTED = "gh_merge_rejected"
    GH_MERGE_ERROR = "gh_merge_error"
    ALREADY_MERGED_OTHER_HEAD = "already_merged_other_head"
    GH_MERGE_UNVERIFIED = "gh_merge_unverified"


class VerifyBaseOutcome(Enum):
    """Outcome of verifying base in scratch worktree."""
    VERIFIED = "verified"
    GATE_FAILED = "gate_failed"
    INFRA_ERROR = "infra_error"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class MergeResult:
    """Result of run_one()."""
    outcome: MergeOutcome
    pr: int
    branch: str
    worktree: str
    orig_head: Optional[str] = None
    tested_sha: Optional[str] = None
    reason: str = ""
    details: Optional[str] = None
    failing_step: Optional[str] = None
    log_path: Optional[str] = None
    restore_failed: Optional[str] = None
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # M6: Unique identifier for this run

    def __post_init__(self) -> None:
        """Enforce outcome-specific invariants on construction."""
        self.validate_outcome_invariants()

    def validate_outcome_invariants(self) -> None:
        """Validate outcome-specific invariants; raises ValueError if violated."""
        if self.outcome == MergeOutcome.MERGED:
            # MERGED: must have tested_sha
            if not self.tested_sha:
                raise ValueError("MERGED outcome requires tested_sha")
        elif self.outcome == MergeOutcome.KICKBACK:
            # KICKBACK: must have orig_head; may have tested_sha, failing_step, log_path
            if not self.orig_head:
                raise ValueError("KICKBACK outcome requires orig_head")
        elif self.outcome == MergeOutcome.PUSHED_NOT_MERGED:
            # PUSHED_NOT_MERGED: must have tested_sha and orig_head
            if not self.tested_sha:
                raise ValueError("PUSHED_NOT_MERGED outcome requires tested_sha")
            if not self.orig_head:
                raise ValueError("PUSHED_NOT_MERGED outcome requires orig_head")
        elif self.outcome == MergeOutcome.REFUSED:
            # REFUSED: minimal outcome, only requires basic fields (all required by signature)
            pass
        elif self.outcome == MergeOutcome.INTERNAL_ERROR:
            # INTERNAL_ERROR: may have any combination of fields
            pass


# ============================================================================
# Queue Log (Step 8)
# ============================================================================

def append_queue_log(event: Dict[str, Any]) -> None:
    """Append an event to queue.jsonl with atomic write."""
    state_dir = ensure_state_dir()
    log_path = state_dir / "queue.jsonl"

    fd = os.open(str(log_path), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        line = json.dumps(event)
        os.write(fd, (line + "\n").encode())
    finally:
        os.close(fd)


def prune_old_results(max_results: int = 100) -> None:
    """
    Prune old result files to prevent unbounded growth.

    Keeps the most recent max_results files in results/ directory.
    """
    state_dir = get_state_dir()
    results_dir = state_dir / "results"
    if not results_dir.exists():
        return

    # List all result files, sorted by modification time (newest first)
    try:
        result_files = sorted(
            results_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
    except Exception:
        return

    # Delete files beyond the max_results threshold
    for old_file in result_files[max_results:]:
        try:
            old_file.unlink()
        except Exception:
            pass


# ============================================================================
# Result JSON (Step 7)
# ============================================================================

def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    """Write JSON to path atomically via mkstemp + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Use mkstemp for a unique temp filename in the same directory as the destination
    fd, temp_path_str = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    temp_path = Path(temp_path_str)
    try:
        os.write(fd, json.dumps(data, indent=2).encode())
    except BaseException:
        # Clean up temp file on any exception (including interrupt)
        try:
            os.close(fd)
        except Exception:
            pass
        try:
            temp_path.unlink()
        except Exception:
            pass
        raise
    finally:
        try:
            os.close(fd)
        except Exception:
            pass
    os.replace(str(temp_path), str(path))


def write_result_json(result: MergeResult, cwd: Optional[Path] = None) -> None:
    """
    Write result.json to state dir atomically.

    M6: Writes to both:
    - result.json (shared, backward-compatible)
    - result-<pr>.json (per-PR, for Phase 4 reading)
    - results/<pr>-<ts>.json (timestamped archive)
    """
    state_dir = ensure_state_dir()
    result_path = state_dir / "result.json"

    # Use asdict to serialize the result
    result_dict = asdict(result)
    # Normalize outcome to lowercase enum value
    result_dict["outcome"] = result.outcome.value
    result_dict["timestamp"] = time.time()

    # Write to shared result.json (backward-compatible)
    _atomic_write_json(result_path, result_dict)

    # M6: Write to per-PR result file for Phase 4 reading
    per_pr_result_path = state_dir / f"result-{result.pr}.json"
    _atomic_write_json(per_pr_result_path, result_dict)

    # Also keep a timestamped copy in results/<pr>-<ts>.json
    ts = int(time.time() * 1000)
    copy_path = state_dir / "results" / f"{result.pr}-{ts}.json"
    _atomic_write_json(copy_path, result_dict)

    # Prune old result files to prevent unbounded growth
    prune_old_results()


def _check_result_json_kickback_state(local_head: str, cwd: Optional[Path] = None) -> bool:
    """
    Check if current local HEAD matches the tested_sha of the last KICKBACK in result.json.

    This is the documented fallback state for post-kickback worktrees (Decision 5):
    the worktree may be left at the last tested (kicked-back) SHA, and preflight
    knows to expect and accept that specific state via result.json.

    Returns True if result.json exists with outcome KICKBACK and tested_sha == local_head.
    """
    try:
        state_dir = get_state_dir()
        result_path = state_dir / "result.json"
        if not result_path.exists():
            return False

        with open(result_path) as f:
            result_data = json.load(f)

        # Check if last result was a KICKBACK
        if result_data.get("outcome") != MergeOutcome.KICKBACK.value:
            return False

        # Check if local HEAD matches the tested_sha from that KICKBACK
        tested_sha = result_data.get("tested_sha")
        if tested_sha and tested_sha == local_head:
            return True

        return False
    except Exception:
        return False


# ============================================================================
# Main Flow (Step 6)
# ============================================================================

def run_one(
    pr: int,
    branch: str,
    worktree: str,
    config: MergeQueueConfig,
    no_claude: bool = False,
    config_path: Optional[Path] = None,
) -> MergeResult:
    """
    Run a single enqueue with a single outcome.

    Returns MergeResult with one of: MERGED, KICKBACK, PUSHED_NOT_MERGED, REFUSED, INTERNAL_ERROR.

    Possible outcomes:
    - MERGED: PR was successfully merged
    - KICKBACK: PR failed gate or had conflicts; user must re-push
    - PUSHED_NOT_MERGED: Push succeeded but gh merge failed
    - REFUSED: PR was refused at preflight (config, duplicate, stale, etc.)
    - INTERNAL_ERROR: Unexpected infrastructure failure

    Args:
        no_claude: If True, skip Claude integration (reserved for future use).
    """
    try:
        # Enqueue preflight (no lock)
        preflight_result = _preflight_check(pr, branch, worktree, config)
        if preflight_result:
            return preflight_result

        # Acquire ticket
        try:
            ticket_num, ticket_fd = acquire_ticket(pr, branch, worktree)
        except Exception as e:
            return MergeResult(
                outcome=MergeOutcome.REFUSED,
                pr=pr,
                branch=branch,
                worktree=worktree,
                reason=f"Failed to acquire ticket: {e}",
            )

        append_queue_log({"event": "enqueued", "pr": pr, "ticket": ticket_num, "time": time.time()})

        # Wait turn
        try:
            merge_lock_fd = wait_turn(ticket_num)
        except Exception as e:
            # M17: Pass ticket_fd to release_ticket so it gets closed
            release_ticket(ticket_num, -1, ticket_fd)
            return MergeResult(
                outcome=MergeOutcome.INTERNAL_ERROR,
                pr=pr,
                branch=branch,
                worktree=worktree,
                reason=f"Failed to wait for turn: {e}",
            )

        # In the lock
        inherit_lock_fd = merge_lock_fd if config.inherit_lock_fd else None
        try:
            result = _locked_flow(pr, branch, worktree, config, merge_lock_fd, inherit_lock_fd, config_path)
            return result
        finally:
            # M17: Pass ticket_fd to release_ticket so both fds are properly closed
            release_ticket(ticket_num, merge_lock_fd, ticket_fd)

    except Exception as e:
        return MergeResult(
            outcome=MergeOutcome.INTERNAL_ERROR,
            pr=pr,
            branch=branch,
            worktree=worktree,
            reason=f"Unexpected error: {e}",
        )


def _preflight_check(
    pr: int,
    branch: str,
    worktree: str,
    config: MergeQueueConfig,
) -> Optional[MergeResult]:
    """
    Preflight checks (no lock).

    Returns a REFUSED outcome if any check fails, None otherwise.
    """
    cwd = Path(worktree).resolve()

    # Check PR state
    try:
        pr_data = git.pr_view_json(branch, ["state", "baseRefName", "headRefOid", "number"], cwd=cwd)
        if not pr_data:
            return MergeResult(
                outcome=MergeOutcome.REFUSED,
                pr=pr,
                branch=branch,
                worktree=worktree,
                reason="Could not fetch PR state",
            )
        # Verify PR number matches provided argument (M5)
        if pr_data.get("number") != pr:
            return MergeResult(
                outcome=MergeOutcome.REFUSED,
                pr=pr,
                branch=branch,
                worktree=worktree,
                reason=f"PR number mismatch: provided {pr}, but branch targets PR #{pr_data.get('number')}",
            )
        if pr_data.get("state") != "OPEN":
            return MergeResult(
                outcome=MergeOutcome.REFUSED,
                pr=pr,
                branch=branch,
                worktree=worktree,
                reason=f"PR is not OPEN (state: {pr_data.get('state')})",
            )
        if pr_data.get("baseRefName") != config.base:
            return MergeResult(
                outcome=MergeOutcome.REFUSED,
                pr=pr,
                branch=branch,
                worktree=worktree,
                reason=f"PR base is not the default branch (base: {pr_data.get('baseRefName')})",
            )

        # Check push-completeness: headRefOid must match local HEAD
        # or local HEAD must match tested_sha of last KICKBACK in result.json (Decision 5)
        head_oid = pr_data.get("headRefOid")
        if head_oid:
            try:
                local_head = Runner.run_git(["rev-parse", "HEAD"], cwd=cwd).strip()
            except Exception as e:
                return MergeResult(
                    outcome=MergeOutcome.REFUSED,
                    pr=pr,
                    branch=branch,
                    worktree=worktree,
                    reason=f"Failed to check push-completeness: {e}",
                )
            if head_oid != local_head:
                # Allow fallback: local HEAD == tested_sha of last KICKBACK (Decision 5)
                if not _check_result_json_kickback_state(local_head, cwd=cwd):
                    return MergeResult(
                        outcome=MergeOutcome.REFUSED,
                        pr=pr,
                        branch=branch,
                        worktree=worktree,
                        reason=f"Local branch is not pushed; push before enqueue (HEAD: {local_head[:8]}, pushed: {head_oid[:8]})",
                    )
    except Exception as e:
        return MergeResult(
            outcome=MergeOutcome.REFUSED,
            pr=pr,
            branch=branch,
            worktree=worktree,
            reason=f"Failed to check PR state: {e}",
        )

    # Check tree is clean
    try:
        if not _is_tree_clean(cwd):
            return MergeResult(
                outcome=MergeOutcome.REFUSED,
                pr=pr,
                branch=branch,
                worktree=worktree,
                reason="Worktree tree is not clean",
            )
    except Exception as e:
        return MergeResult(
            outcome=MergeOutcome.REFUSED,
            pr=pr,
            branch=branch,
            worktree=worktree,
            reason=f"Failed to check tree cleanliness: {e}",
        )

    # Check no rebase/merge in progress
    try:
        if _has_rebase_or_merge_in_progress(cwd):
            return MergeResult(
                outcome=MergeOutcome.REFUSED,
                pr=pr,
                branch=branch,
                worktree=worktree,
                reason="Rebase or merge is in progress; run appropriate abort command",
            )
    except Exception as e:
        return MergeResult(
            outcome=MergeOutcome.REFUSED,
            pr=pr,
            branch=branch,
            worktree=worktree,
            reason=f"Failed to check for rebase/merge: {e}",
        )

    # Check remote-only commits
    try:
        if not _no_remote_only_commits(branch, config.base, cwd):
            return MergeResult(
                outcome=MergeOutcome.REFUSED,
                pr=pr,
                branch=branch,
                worktree=worktree,
                reason="Branch has remote-only commits",
            )
    except Exception as e:
        return MergeResult(
            outcome=MergeOutcome.REFUSED,
            pr=pr,
            branch=branch,
            worktree=worktree,
            reason=f"Failed to check for remote-only commits: {e}",
        )

    return None


# ============================================================================
# _locked_flow helpers and phase functions
# ============================================================================

def _kickback(
    pr: int,
    branch: str,
    worktree: str,
    orig_head: str,
    reason: str,
    tested_sha: Optional[str] = None,
    details: Optional[str] = None,
    failing_step: Optional[str] = None,
    log_path: Optional[str] = None,
    restore_failed: Optional[str] = None,
) -> MergeResult:
    """Builder for KICKBACK MergeResult; collapses repeated boilerplate."""
    return MergeResult(
        outcome=MergeOutcome.KICKBACK,
        pr=pr,
        branch=branch,
        worktree=worktree,
        orig_head=orig_head,
        tested_sha=tested_sha,
        reason=reason,
        details=details,
        failing_step=failing_step,
        log_path=log_path,
        restore_failed=restore_failed,
    )


def _phase_validate_tree_and_rebase_state(
    pr: int,
    branch: str,
    worktree: str,
    orig_head: str,
    cwd: Path,
) -> Optional[MergeResult]:
    """
    Phase 1: Re-check tree is clean and no rebase/merge in progress.
    Returns KICKBACK if validation fails, else None.
    """
    if not _is_tree_clean(cwd):
        return _kickback(
            pr, branch, worktree, orig_head,
            "Worktree tree is not clean (re-check in lock)"
        )

    if _has_rebase_or_merge_in_progress(cwd):
        return _kickback(
            pr, branch, worktree, orig_head,
            "Rebase or merge is in progress; run appropriate abort command"
        )

    return None


def _phase_validate_config(
    pr: int,
    branch: str,
    worktree: str,
    orig_head: str,
    config: MergeQueueConfig,
    config_path: Optional[Path],
) -> Tuple[Optional[MergeResult], MergeQueueConfig]:
    """
    Phase 2: Re-validate config.
    Returns (KICKBACK if validation fails, updated config) or (None, updated config).
    """
    try:
        if config_path is None:
            config_path = resolve_config_path()
        config = load_and_validate_config(config_path)
        return None, config
    except Exception as e:
        return _kickback(
            pr, branch, worktree, orig_head,
            f"Config changed or invalid: {e}"
        ), config


def _phase_validate_pr(
    pr: int,
    branch: str,
    worktree: str,
    orig_head: str,
    config: MergeQueueConfig,
    cwd: Path,
) -> Optional[MergeResult]:
    """
    Phase 3: Re-validate PR state.
    Returns KICKBACK if validation fails, else None.
    """
    try:
        pr_data = git.pr_view_json(
            branch,
            ["state", "baseRefName", "headRefName", "headRefOid"],
            cwd=cwd
        )
        if not pr_data or pr_data.get("state") != "OPEN":
            return _kickback(
                pr, branch, worktree, orig_head,
                "PR is no longer OPEN"
            )
        if pr_data.get("baseRefName") != config.base:
            return _kickback(
                pr, branch, worktree, orig_head,
                "PR base has changed"
            )
        if pr_data.get("headRefName") != branch:
            return _kickback(
                pr, branch, worktree, orig_head,
                "PR branch has changed"
            )

        # Check push-completeness: headRefOid must match local HEAD
        # or local HEAD must match tested_sha of last KICKBACK in result.json (Decision 5)
        head_oid = pr_data.get("headRefOid")
        if head_oid and head_oid != orig_head:
            # Allow fallback: local HEAD == tested_sha of last KICKBACK (Decision 5)
            if not _check_result_json_kickback_state(orig_head, cwd=cwd):
                return _kickback(
                    pr, branch, worktree, orig_head,
                    f"Local branch is not in sync with pushed branch; push to update (local: {orig_head[:8]}, pushed: {head_oid[:8]})"
                )
        return None
    except Exception as e:
        return _kickback(
            pr, branch, worktree, orig_head,
            f"Failed to re-validate PR: {e}"
        )


def _phase_fetch_and_verify_lease(
    pr: int,
    branch: str,
    worktree: str,
    orig_head: str,
    config: MergeQueueConfig,
    cwd: Path,
) -> Tuple[Optional[MergeResult], Optional[str], Optional[str]]:
    """
    Phase 4: Fetch and record lease_sha, base_sha.
    Returns (KICKBACK if fails, lease_sha, base_sha) or (None, lease_sha, base_sha).
    """
    # Fetch with retry on timeout (M2)
    try:
        Runner.run_git(["fetch", "origin"], cwd=cwd, timeout=config.mutation_timeout_secs)
    except RuntimeError as e:
        if "timed out" in str(e):
            try:
                # Re-query to ensure we have current state
                Runner.run_git(["fetch", "origin"], cwd=cwd, timeout=config.mutation_timeout_secs)
            except Exception:
                pass  # Proceed with whatever we have
        else:
            return _kickback(
                pr, branch, worktree, orig_head,
                f"Failed to fetch: {e}"
            ), None, None
    except Exception as e:
        return _kickback(
            pr, branch, worktree, orig_head,
            f"Failed to fetch: {e}"
        ), None, None

    # M7: Pin lease to orig_head, not to post-fetch tip
    # Verify that the remote tip matches orig_head (KICKBACK if it changed)
    try:
        remote_tip = Runner.run_git(["rev-parse", f"origin/{branch}"], cwd=cwd).strip()
        if remote_tip != orig_head:
            # Remote has moved; someone else pushed to this branch
            return _kickback(
                pr, branch, worktree, orig_head,
                "Base moved: remote branch changed; retry after sync",
                tested_sha=remote_tip,
                details=f"Expected orig_head={orig_head[:8]}, found remote={remote_tip[:8]}"
            ), None, None
        lease_sha = orig_head
        base_sha = Runner.run_git(["rev-parse", f"origin/{config.base}"], cwd=cwd).strip()
        return None, lease_sha, base_sha
    except Exception as e:
        return _kickback(
            pr, branch, worktree, orig_head,
            f"Failed to parse shas: {e}"
        ), None, None


def _phase_verify_base(
    pr: int,
    branch: str,
    worktree: str,
    orig_head: str,
    base_sha: str,
    config: MergeQueueConfig,
    lock_fd_to_inherit: Optional[int],
    cwd: Path,
) -> Optional[MergeResult]:
    """
    Phase 5: Poison check and base verification.
    Returns KICKBACK if verification fails, else None.
    """
    poison_ok, poison_shas = scan_base(base_sha, config.allow_unverified, cwd=cwd)
    if not poison_ok or poison_shas:
        state_dir = ensure_state_dir()
        verified_path = state_dir / "base-verified" / base_sha
        failed_path = state_dir / "base-failed" / base_sha

        # Check if already verified
        if verified_path.exists():
            # Base is verified; proceed
            return None
        elif failed_path.exists():
            # Base already failed verification; don't re-run gate
            return _kickback(
                pr, branch, worktree, orig_head,
                "Base previously failed gate; run 'merge-queue reverify' to retry",
                details=f"Unverified: {', '.join(poison_shas[:3])}..."
            )
        else:
            # Try to verify the base by running gate in scratch worktree
            # M9: Thread lock_fd_to_inherit through to _verify_base_in_scratch
            verify_outcome = _verify_base_in_scratch(base_sha, config, lock_fd_to_inherit=lock_fd_to_inherit)
            if verify_outcome == VerifyBaseOutcome.VERIFIED:
                # Gate passed; write verified record
                os.makedirs(verified_path.parent, exist_ok=True)
                _touch_record(verified_path)
                return None
            elif verify_outcome == VerifyBaseOutcome.INFRA_ERROR:
                # Infrastructure error during verification; return INTERNAL_ERROR
                return MergeResult(
                    outcome=MergeOutcome.INTERNAL_ERROR,
                    pr=pr,
                    branch=branch,
                    worktree=worktree,
                    orig_head=orig_head,
                    reason="Base verification infrastructure failed; retry later",
                )
            else:
                # Gate failed; write failed record only on first failure
                if not failed_path.exists():
                    os.makedirs(failed_path.parent, exist_ok=True)
                    _touch_record(failed_path)

                return _kickback(
                    pr, branch, worktree, orig_head,
                    "Base has unverified commits; run 'merge-queue bootstrap' or 'merge-queue reverify'",
                    details=f"Unverified: {', '.join(poison_shas[:3])}..."
                )

    return None


def _restore_worktree_to_orig_head(cwd: Path, orig_head: str) -> None:
    """
    Reset the worktree's branch back to orig_head after a post-rebase kickback.

    Best-effort: a failure here shouldn't mask the underlying kickback reason, and the
    worst case (local branch left rewritten but unpushed) is exactly the pre-fix
    behavior, not a regression.
    """
    try:
        Runner.run_git(["reset", "--hard", orig_head], cwd=cwd)
    except Exception:
        pass


def _phase_rebase(
    pr: int,
    branch: str,
    worktree: str,
    orig_head: str,
    config: MergeQueueConfig,
    cwd: Path,
) -> Optional[MergeResult]:
    """
    Phase 6: Rebase on origin/base.
    Returns KICKBACK if rebase fails, else None.
    """
    try:
        Runner.run_git(["rebase", f"origin/{config.base}"], cwd=cwd, timeout=config.mutation_timeout_secs)
    except RuntimeError as e:
        # M2: Handle rebase timeout
        if "timed out" in str(e):
            # Try to abort the rebase
            try:
                Runner.run_git(["rebase", "--abort"], cwd=cwd, timeout=config.mutation_timeout_secs)
            except Exception as abort_err:
                restore_failed = f"rebase --abort failed: {abort_err}"
                return _kickback(
                    pr, branch, worktree, orig_head,
                    f"Rebase timed out after {config.mutation_timeout_secs}s (abort also failed)",
                    restore_failed=restore_failed
                )
            return _kickback(
                pr, branch, worktree, orig_head,
                f"Rebase timed out after {config.mutation_timeout_secs}s"
            )
        else:
            # Non-timeout error; likely a conflict
            try:
                Runner.run_git(["rebase", "--abort"], cwd=cwd, timeout=config.mutation_timeout_secs)
            except Exception as abort_err:
                restore_failed = f"rebase --abort failed: {abort_err}"
                return _kickback(
                    pr, branch, worktree, orig_head,
                    "Rebase conflict (abort failed)",
                    restore_failed=restore_failed
                )
            return _kickback(
                pr, branch, worktree, orig_head,
                f"Rebase conflict: {e}"
            )
    except Exception as e:
        # Conflict; abort and kick back
        try:
            Runner.run_git(["rebase", "--abort"], cwd=cwd, timeout=config.mutation_timeout_secs)
        except Exception as abort_err:
            restore_failed = f"rebase --abort failed: {abort_err}"
            return _kickback(
                pr, branch, worktree, orig_head,
                "Rebase conflict (abort failed)",
                restore_failed=restore_failed
            )

        return _kickback(
            pr, branch, worktree, orig_head,
            f"Rebase conflict: {e}"
        )

    return None


def _phase_run_gate_and_assertions(
    pr: int,
    branch: str,
    worktree: str,
    orig_head: str,
    config: MergeQueueConfig,
    lock_fd_to_inherit: Optional[int],
    cwd: Path,
) -> Tuple[Optional[MergeResult], Optional[str]]:
    """
    Phase 7: Record tested_sha, run gate steps, and re-assert HEAD/tree.
    Returns (KICKBACK if fails, tested_sha) or (None, tested_sha).
    """
    # Record tested_sha
    try:
        tested_sha = Runner.run_git(["rev-parse", "HEAD"], cwd=cwd).strip()
    except Exception as e:
        return _kickback(
            pr, branch, worktree, orig_head,
            f"Failed to record tested_sha: {e}"
        ), None

    # Run gate steps
    state_dir = ensure_state_dir()
    for i, step in enumerate(config.steps):
        step_name = f"step-{i}"
        log_path = state_dir / "logs" / f"{pr}-{step_name}.log"

        step_outcome = run_step(
            step.cmd,
            cwd,
            log_path,
            timeout_secs=step.timeout_secs if step.timeout_secs is not None else config.mutation_timeout_secs,
            lock_fd_to_inherit=lock_fd_to_inherit,
        )

        if not step_outcome.success:
            return _kickback(
                pr, branch, worktree, orig_head,
                f"Step {step_name} failed",
                tested_sha=tested_sha,
                failing_step=step_name,
                log_path=str(log_path),
                details=step_outcome.error
            ), tested_sha

    # Re-assert HEAD == tested_sha and clean tree
    try:
        current_head = Runner.run_git(["rev-parse", "HEAD"], cwd=cwd).strip()
        if current_head != tested_sha:
            return _kickback(
                pr, branch, worktree, orig_head,
                "HEAD moved during gate steps",
                tested_sha=tested_sha
            ), tested_sha
        if not _is_tree_clean(cwd):
            return _kickback(
                pr, branch, worktree, orig_head,
                "Tree became dirty during gate steps",
                tested_sha=tested_sha
            ), tested_sha
    except Exception as e:
        return _kickback(
            pr, branch, worktree, orig_head,
            f"Failed to re-assert HEAD: {e}",
            tested_sha=tested_sha
        ), tested_sha

    return None, tested_sha


def _phase_push(
    pr: int,
    branch: str,
    worktree: str,
    orig_head: str,
    tested_sha: str,
    lease_sha: str,
    config: MergeQueueConfig,
    cwd: Path,
) -> Optional[MergeResult]:
    """
    Phase 8: Push changes with force-push-tested.
    Returns KICKBACK if push fails, else None.
    """
    try:
        push_ok = Runner.force_push_tested(branch, tested_sha, lease_sha, cwd=cwd, timeout=config.mutation_timeout_secs)
        if not push_ok:
            return _kickback(
                pr, branch, worktree, orig_head,
                "Push rejected by lease (another clone pushed to branch)",
                tested_sha=tested_sha
            )
    except RuntimeError as e:
        # M2: Handle push timeout by re-querying actual state
        if "timed out" in str(e):
            try:
                actual_remote_tip = Runner.run_git(["rev-parse", f"origin/{branch}"], cwd=cwd, timeout=config.mutation_timeout_secs).strip()
                if actual_remote_tip == tested_sha:
                    # Push actually succeeded (maybe succeeded then timeout on confirmation)
                    # Continue to merge step
                    return None
                else:
                    # Push timed out and remote tip is not what we tested
                    return _kickback(
                        pr, branch, worktree, orig_head,
                        f"Push timed out after {config.mutation_timeout_secs}s; could not verify success",
                        tested_sha=tested_sha
                    )
            except Exception:
                # Could not re-query; treat as timeout failure
                return _kickback(
                    pr, branch, worktree, orig_head,
                    f"Push timed out after {config.mutation_timeout_secs}s and could not re-query state: {e}",
                    tested_sha=tested_sha
                )
        else:
            # Non-timeout RuntimeError; treat as failure
            return _kickback(
                pr, branch, worktree, orig_head,
                f"Push failed: {e}",
                tested_sha=tested_sha
            )
    except Exception as e:
        return _kickback(
            pr, branch, worktree, orig_head,
            f"Push failed: {e}",
            tested_sha=tested_sha
        )

    return None


def _phase_verify_base_unchanged(
    pr: int,
    branch: str,
    worktree: str,
    orig_head: str,
    tested_sha: str,
    base_sha: str,
    config: MergeQueueConfig,
    cwd: Path,
) -> Optional[MergeResult]:
    """
    Phase 9: Verify base hasn't moved during gate (fresh fetch).
    Returns KICKBACK if base has moved, else None.
    """
    try:
        Runner.run_git(["fetch", "origin", config.base], cwd=cwd, timeout=config.mutation_timeout_secs)
        new_base_sha = Runner.run_git(["rev-parse", f"origin/{config.base}"], cwd=cwd, timeout=config.mutation_timeout_secs).strip()
        if new_base_sha != base_sha:
            return _kickback(
                pr, branch, worktree, orig_head,
                f"Base moved during gate (new: {new_base_sha[:8]})",
                tested_sha=tested_sha
            )
    except RuntimeError as e:
        # M2: Timeout checking base; treat as KICKBACK (can retry)
        if "timed out" in str(e):
            return _kickback(
                pr, branch, worktree, orig_head,
                f"Base check timed out after {config.mutation_timeout_secs}s",
                tested_sha=tested_sha
            )
        else:
            return _kickback(
                pr, branch, worktree, orig_head,
                f"Failed to verify base unchanged: {e}",
                tested_sha=tested_sha
            )
    except Exception as e:
        return _kickback(
            pr, branch, worktree, orig_head,
            f"Failed to verify base unchanged: {e}",
            tested_sha=tested_sha
        )

    return None


def _phase_merge(
    pr: int,
    branch: str,
    worktree: str,
    orig_head: str,
    tested_sha: str,
    base_sha: str,
    config: MergeQueueConfig,
    cwd: Path,
) -> MergeResult:
    """
    Phase 10: Merge the PR.
    Returns appropriate MergeResult (MERGED, PUSHED_NOT_MERGED, etc).
    """
    try:
        merge_outcome = _merge_pr(pr, tested_sha, base_sha, config, cwd=cwd)
        if merge_outcome == MergePROutcome.SUCCESS:
            return MergeResult(
                outcome=MergeOutcome.MERGED,
                pr=pr,
                branch=branch,
                worktree=worktree,
                orig_head=orig_head,
                tested_sha=tested_sha,
                reason="Merged successfully",
            )
        elif merge_outcome == MergePROutcome.POLL_TIMEOUT:
            return MergeResult(
                outcome=MergeOutcome.PUSHED_NOT_MERGED,
                pr=pr,
                branch=branch,
                worktree=worktree,
                orig_head=orig_head,
                tested_sha=tested_sha,
                reason="Push succeeded but PR metadata did not converge for merge",
            )
        elif merge_outcome == MergePROutcome.GH_MERGE_REJECTED:
            return MergeResult(
                outcome=MergeOutcome.PUSHED_NOT_MERGED,
                pr=pr,
                branch=branch,
                worktree=worktree,
                orig_head=orig_head,
                tested_sha=tested_sha,
                reason="Push succeeded but gh merge was rejected",
            )
        else:
            # GH_MERGE_ERROR or ALREADY_MERGED_OTHER_HEAD
            return MergeResult(
                outcome=MergeOutcome.PUSHED_NOT_MERGED,
                pr=pr,
                branch=branch,
                worktree=worktree,
                orig_head=orig_head,
                tested_sha=tested_sha,
                reason=f"Push succeeded but merge failed: {merge_outcome.value}",
            )
    except Exception as e:
        return MergeResult(
            outcome=MergeOutcome.PUSHED_NOT_MERGED,
            pr=pr,
            branch=branch,
            worktree=worktree,
            orig_head=orig_head,
            tested_sha=tested_sha,
            reason=f"Merge failed: {e}",
        )


def _locked_flow(
    pr: int,
    branch: str,
    worktree: str,
    config: MergeQueueConfig,
    merge_lock_fd: int,
    lock_fd_to_inherit: Optional[int],
    config_path: Optional[Path] = None,
) -> MergeResult:
    """
    Locked flow (steps 3-10 of the plan).

    Precondition: merge.lock must be held (merge_lock_fd must be valid and locked).
    This function executes the main merge gate with the lock held, ensuring serialized access.
    """
    cwd = Path(worktree).resolve()

    # Captured up front so every KICKBACK constructed below (even before the fetch/rev-parse
    # section further down) satisfies MergeResult's outcome invariant (KICKBACK requires
    # orig_head). If HEAD itself can't be resolved, that's a genuine internal error, not a
    # kickback, so let it propagate to run_one's outer handler.
    orig_head = Runner.run_git(["rev-parse", "HEAD"], cwd=cwd).strip()
    tested_sha: Optional[str] = None

    try:
        # Phase 1: Validate tree and rebase state
        result = _phase_validate_tree_and_rebase_state(pr, branch, worktree, orig_head, cwd)
        if result:
            return result

        # Phase 2: Validate config
        result, config = _phase_validate_config(pr, branch, worktree, orig_head, config, config_path)
        if result:
            return result

        # Phase 3: Validate PR
        result = _phase_validate_pr(pr, branch, worktree, orig_head, config, cwd)
        if result:
            return result

        # Phase 4: Fetch and verify lease/base
        result, lease_sha, base_sha = _phase_fetch_and_verify_lease(pr, branch, worktree, orig_head, config, cwd)
        if result:
            return result
        # lease_sha and base_sha are guaranteed to be non-None here
        assert lease_sha is not None and base_sha is not None

        # Phase 5: Verify base
        result = _phase_verify_base(pr, branch, worktree, orig_head, base_sha, config, lock_fd_to_inherit, cwd)
        if result:
            return result

        # Phase 6: Rebase
        result = _phase_rebase(pr, branch, worktree, orig_head, config, cwd)
        if result:
            return result

        # Phase 7: Run gate and post-gate assertions
        result, tested_sha = _phase_run_gate_and_assertions(pr, branch, worktree, orig_head, config, lock_fd_to_inherit, cwd)
        if result:
            # Rebase (phase 6) already moved local HEAD to tested_sha, but nothing was
            # ever pushed. Restore the worktree to orig_head so it matches origin/<branch>
            # again; otherwise the next enqueue's lease check (phase 4) sees local HEAD
            # diverged from remote and misreports it as "base moved".
            _restore_worktree_to_orig_head(cwd, orig_head)
            return result
        # tested_sha is guaranteed to be non-None here
        assert tested_sha is not None

        # Phase 8: Push
        result = _phase_push(pr, branch, worktree, orig_head, tested_sha, lease_sha, config, cwd)
        if result:
            # Push never landed (rejected/failed/unconfirmed-timeout); same restore
            # rationale as phase 7 above.
            _restore_worktree_to_orig_head(cwd, orig_head)
            return result

        # Phase 9: Verify base unchanged
        result = _phase_verify_base_unchanged(pr, branch, worktree, orig_head, tested_sha, base_sha, config, cwd)
        if result:
            return result

        # Phase 10: Merge
        return _phase_merge(pr, branch, worktree, orig_head, tested_sha, base_sha, config, cwd)

    except Exception as e:
        return MergeResult(
            outcome=MergeOutcome.INTERNAL_ERROR,
            pr=pr,
            branch=branch,
            worktree=worktree,
            orig_head=orig_head,
            tested_sha=tested_sha,
            reason=f"Locked flow error: {e}",
        )


# ============================================================================
# Helper Functions
# ============================================================================

def _is_tree_clean(cwd: Path) -> bool:
    """Check if the tree is clean (no modifications)."""
    try:
        status = Runner.run_git(["status", "--porcelain"], cwd=cwd).strip()
        return not status
    except Exception:
        return False


def _has_rebase_or_merge_in_progress(cwd: Path) -> bool:
    """Check if a rebase or merge is in progress."""
    try:
        git_dir = Path(Runner.run_git(["rev-parse", "--git-dir"], cwd=cwd).strip()).resolve()
    except Exception:
        return False
    return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists() or (git_dir / "MERGE_HEAD").exists()


def _no_remote_only_commits(branch: str, base: str, cwd: Optional[Path] = None) -> bool:
    """
    Check if there are remote-only commits.

    Passes if:
    - origin/<branch> is an ancestor or equal to HEAD
    - git cherry shows no '+' lines
    - origin/<branch> matches recorded result.json orig_head with tested_sha ancestor
    """
    try:
        # Check ancestor
        if git.is_ancestor(f"origin/{branch}", "HEAD", cwd=cwd):
            return True

        # Check cherry
        cherry_output = Runner.run_git(["cherry", "HEAD", f"origin/{branch}"], cwd=cwd).strip()
        if not cherry_output or all(not line.startswith("+") for line in cherry_output.split("\n")):
            return True

        # Check result.json
        state_dir = get_state_dir()
        result_path = state_dir / "result.json"
        if result_path.exists():
            try:
                with open(result_path) as f:
                    result_data = json.load(f)
                    if result_data.get("orig_head") == Runner.run_git(["rev-parse", f"origin/{branch}"], cwd=cwd).strip():
                        if result_data.get("tested_sha"):
                            if git.is_ancestor(result_data["tested_sha"], "HEAD", cwd=cwd):
                                return True
            except Exception:
                pass

        return False
    except Exception:
        return False


def _scratch_step_env(config: MergeQueueConfig) -> Dict[str, str]:
    """Environment for steps run in the scratch checkout: the current process env minus
    config.scratch_env_strip, then config.scratch_env laid on top. The stripped variables are
    removed and NOT replaced — a step that needs a Compose project or a database must set its
    own (docker compose -p, a top-level `name:` in the compose file, an inline VAR=... assignment,
    or `scratch_env`)."""
    env = {k: v for k, v in os.environ.items() if k not in set(config.scratch_env_strip)}
    env.update(config.scratch_env)
    return env


def _verify_base_in_scratch(base_sha: str, config: MergeQueueConfig, lock_fd_to_inherit: Optional[int] = None) -> VerifyBaseOutcome:
    """
    Verify the base by running the main gate in a scratch worktree.

    Creates or reuses a scratch worktree, checks out base_sha, runs setup commands,
    and runs all gate steps.

    Returns:
    - VERIFIED: base verified (all gates passed)
    - GATE_FAILED: base failed gate (gate step or setup failed)
    - INFRA_ERROR: infrastructure error (worktree creation, clone, etc. failed)
    """
    state_dir = ensure_state_dir()
    scratch_dir = get_scratch_dir()
    step_env = _scratch_step_env(config)

    try:
        # Create or update scratch worktree
        if not (scratch_dir / ".git").exists():
            scratch_dir.mkdir(parents=True, exist_ok=True)
            try:
                # Try to create a new worktree
                Runner.run_git(["worktree", "add", "--detach", str(scratch_dir), base_sha])
            except Exception:
                # Fallback: clone from origin with a generous timeout
                try:
                    origin_url = Runner.run_git(["config", "--get", "remote.origin.url"]).strip()
                    # Use a 5-minute timeout for clone (generous for large repos)
                    subprocess.run(
                        ["git", "clone", "--shared", origin_url, str(scratch_dir)],
                        check=True,
                        capture_output=True,
                        cwd=str(state_dir),
                        timeout=300,  # 5 minutes
                    )
                    Runner.run_git(["checkout", base_sha], cwd=scratch_dir)
                except subprocess.TimeoutExpired:
                    # Infrastructure timeout; treat as infra error
                    return VerifyBaseOutcome.INFRA_ERROR
                except Exception:
                    # Infrastructure error (clone failed)
                    return VerifyBaseOutcome.INFRA_ERROR
        else:
            # Update existing worktree: reset/clean to avoid dirty state between uses
            try:
                Runner.run_git(["fetch", "origin"], cwd=scratch_dir, timeout=config.mutation_timeout_secs)
                # Detach and clean before checking out new sha
                Runner.run_git(["checkout", "--detach"], cwd=scratch_dir)
                Runner.run_git(["reset", "--hard", base_sha], cwd=scratch_dir)
                Runner.run_git(["clean", "-ffdx"], cwd=scratch_dir)
                Runner.run_git(["checkout", base_sha], cwd=scratch_dir)
            except RuntimeError as e:
                # M2: Fetch timeout; treat as infra error (can retry)
                if "timed out" in str(e):
                    return VerifyBaseOutcome.INFRA_ERROR
                else:
                    return VerifyBaseOutcome.INFRA_ERROR
            except Exception:
                # Infrastructure error (git operations failed)
                return VerifyBaseOutcome.INFRA_ERROR

        # Run setup commands
        for setup_cmd in config.scratch_setup:
            log_path = state_dir / "logs" / "setup.log"
            # M9: Thread lock_fd_to_inherit through to run_step
            outcome = run_step(setup_cmd, scratch_dir, log_path, timeout_secs=config.mutation_timeout_secs, lock_fd_to_inherit=lock_fd_to_inherit, env=step_env)
            if not outcome.success:
                # Setup failed; this is a gate failure, not infra error
                return VerifyBaseOutcome.GATE_FAILED

        # Run gate steps
        for i, step in enumerate(config.steps):
            log_path = state_dir / "logs" / f"base-verify-step-{i}.log"
            timeout_secs = step.timeout_secs if step.timeout_secs is not None else config.mutation_timeout_secs
            # M9: Thread lock_fd_to_inherit through to run_step
            outcome = run_step(step.cmd, scratch_dir, log_path, timeout_secs=timeout_secs, lock_fd_to_inherit=lock_fd_to_inherit, env=step_env)
            if not outcome.success:
                # Gate step failed; this is a gate failure, not infra error
                return VerifyBaseOutcome.GATE_FAILED

        return VerifyBaseOutcome.VERIFIED

    except Exception:
        # Unexpected exception; treat as infra error to be safe
        return VerifyBaseOutcome.INFRA_ERROR


def _merge_pr(pr: int, tested_sha: str, base_sha: str, config: MergeQueueConfig, cwd: Optional[Path] = None) -> MergePROutcome:
    """
    Merge the PR.

    Polls until headRefOid == tested_sha, then runs gh pr merge.
    Returns MergePROutcome indicating success or specific failure reason.

    Poll budget: config.pr_merge_poll_secs seconds at ~1s intervals.
    This allows time for GitHub to compute mergeability and converge PR metadata.
    """
    # Poll for headRefOid match
    deadline = time.monotonic() + config.pr_merge_poll_secs
    converged = False
    pr_title = f"PR #{pr}"
    while time.monotonic() < deadline:
        try:
            pr_data = git.pr_view_json(str(pr), ["headRefOid", "mergeable", "title"], cwd=cwd)
            if pr_data and pr_data.get("title"):
                pr_title = str(pr_data["title"])
            if pr_data and pr_data.get("headRefOid") == tested_sha and pr_data.get("mergeable") != "UNKNOWN":
                converged = True
                break
        except (git.GitCommandError, json.JSONDecodeError):
            # GitHub API temporary error; retry
            pass
        time.sleep(POLL_SLEEP_INTERVAL)

    # If polling didn't converge, return poll timeout
    if not converged:
        return MergePROutcome.POLL_TIMEOUT

    # Merge
    try:
        trailer = build_trailer(base_sha, tested_sha)
        # git interpret-trailers --parse only recognizes a trailer block that
        # follows a non-trailer paragraph and a blank line; a body consisting
        # only of the trailer line is silently dropped by parse_trailer later.
        body = f"Tested by merge-queue.\n\n{trailer}"
        Runner.run_gh(
            [
                "pr",
                "merge",
                str(pr),
                "--squash",
                f"--match-head-commit={tested_sha}",
                "--subject",
                f"{pr_title} (#{pr})",
                "--body",
                body,
            ],
            cwd=cwd,
            timeout=config.mutation_timeout_secs,
        )
        # C1: Re-query PR state after gh pr merge succeeds to confirm it's actually MERGED
        try:
            pr_data = git.pr_view_json(str(pr), ["state"], cwd=cwd)
            if pr_data and pr_data.get("state") == "MERGED":
                return MergePROutcome.SUCCESS
            else:
                # Merge exit 0 but PR is not actually merged; treat as error
                return MergePROutcome.GH_MERGE_ERROR
        except (git.GitCommandError, json.JSONDecodeError):
            # Can't verify the merge; treat as unverified rather than assuming success
            return MergePROutcome.GH_MERGE_UNVERIFIED
    except RuntimeError as e:
        # M2: gh pr merge timed out; re-query state to see if it actually merged
        if "timed out" in str(e):
            try:
                pr_data = git.pr_view_json(str(pr), ["state", "headRefOid"], cwd=cwd)
                if pr_data and pr_data.get("state") == "MERGED":
                    # Verify that the merged head matches what we tested
                    merged_oid = pr_data.get("headRefOid")
                    if merged_oid and merged_oid == tested_sha:
                        return MergePROutcome.SUCCESS
                    else:
                        # Merged but with different head
                        return MergePROutcome.ALREADY_MERGED_OTHER_HEAD
                else:
                    # Timed out and not merged
                    return MergePROutcome.GH_MERGE_ERROR
            except (git.GitCommandError, json.JSONDecodeError):
                # Can't verify; treat as unverified
                return MergePROutcome.GH_MERGE_UNVERIFIED
        else:
            # Other RuntimeError; treat as merge error
            return MergePROutcome.GH_MERGE_ERROR
    except git.GitCommandError as e:
        # Re-query PR state to confirm if it was already merged
        try:
            pr_data = git.pr_view_json(str(pr), ["state", "headRefOid", "mergeCommit"], cwd=cwd)
            if pr_data and pr_data.get("state") == "MERGED":
                # Verify that the merged head matches what we tested
                merged_oid = pr_data.get("headRefOid")
                if merged_oid and merged_oid == tested_sha:
                    return MergePROutcome.SUCCESS
                else:
                    # Merged but with different head
                    return MergePROutcome.ALREADY_MERGED_OTHER_HEAD
        except (git.GitCommandError, json.JSONDecodeError):
            pass
        # Determine failure type based on error message
        error_msg = str(e).lower()
        if "rejected" in error_msg or "denied" in error_msg:
            return MergePROutcome.GH_MERGE_REJECTED
        return MergePROutcome.GH_MERGE_ERROR


# ============================================================================
# CLI Entry Point (Step 9)
# ============================================================================

def main(argv: Optional[Sequence[str]] = None) -> int:
    """
    Main entry point.

    Subcommands:
      enqueue (default) [--no-claude] [--config PATH] -- queue a PR
      status -- show queue status
      reverify [SHA] -- clear base-failed record
      bootstrap [--yes] -- anchor base history
      resume [--pr N] -- resume a failed Claude hand-off

    Exit codes:
      0 MERGED (or cleanup succeeded)
      2 KICKBACK / PUSHED_NOT_MERGED
      3 REFUSED (config, stacked, preflight, duplicate)
      1 INTERNAL_ERROR
    """
    if argv is None:
        argv = sys.argv[1:]
    else:
        argv = list(argv)

    if not argv or argv[0] in ("enqueue", "--no-claude", "--config", "--pr"):
        # Dispatch enqueue, skipping the "enqueue" token if present
        enqueue_argv = argv[1:] if argv and argv[0] == "enqueue" else argv
        return _cmd_enqueue(enqueue_argv)
    elif argv[0] == "status":
        return _cmd_status(argv[1:])
    elif argv[0] == "reverify":
        return _cmd_reverify(argv[1:])
    elif argv[0] == "bootstrap":
        return _cmd_bootstrap(argv[1:])
    elif argv[0] == "resume":
        return _cmd_resume(argv[1:])
    else:
        print(f"Unknown subcommand: {argv[0]}", file=sys.stderr)
        return 1


def _cmd_enqueue(argv: List[str]) -> int:
    """Enqueue a PR for merge."""
    parser = argparse.ArgumentParser(description="Enqueue a PR for merge")
    parser.add_argument("--no-claude", action="store_true", help="Reserved for future Claude integration (not yet implemented)")
    parser.add_argument("--config", type=str, help="Config file path")
    parser.add_argument("--pr", type=int, help="PR number (auto-detected if not provided)")

    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 1

    try:
        # Get worktree path
        try:
            worktree = Runner.run_git(["rev-parse", "--show-toplevel"]).strip()
        except Exception as e:
            print(f"Failed to get worktree path: {e}", file=sys.stderr)
            return 3

        # Detect or use provided PR number and branch
        if args.pr:
            pr = args.pr
            # Still need current branch
            current_branch = git.get_current_branch()
            if not current_branch:
                print("Failed to detect current branch", file=sys.stderr)
                return 3
        else:
            # Detect PR and branch from current worktree
            current_branch = git.get_current_branch()
            if not current_branch:
                print("Failed to detect current branch", file=sys.stderr)
                return 3

            # Get PR number
            try:
                pr_data = git.pr_view_json(current_branch, ["number"], cwd=Path(worktree))
                if not pr_data or "number" not in pr_data:
                    print("Failed to detect PR number", file=sys.stderr)
                    return 3
                pr = pr_data["number"]
            except Exception as e:
                print(f"Failed to get PR info: {e}", file=sys.stderr)
                return 3

        # Load config
        try:
            config_path = resolve_config_path(args.config)
            config = load_and_validate_config(config_path)
        except Exception as e:
            print(f"Config error: {e}", file=sys.stderr)
            return 3

        # Run merge queue
        try:
            result = run_one(pr, current_branch, worktree, config, no_claude=args.no_claude, config_path=config_path)
        except KeyboardInterrupt:
            # Still record the interrupted attempt, but let the interrupt actually
            # terminate the process rather than being silently absorbed.
            write_result_json(MergeResult(
                outcome=MergeOutcome.INTERNAL_ERROR,
                pr=pr,
                branch=current_branch,
                worktree=worktree,
                reason="Interrupted",
            ))
            raise
        except BaseException as e:
            # Catch even SystemExit to ensure write_result_json is called
            result = MergeResult(
                outcome=MergeOutcome.INTERNAL_ERROR,
                pr=pr,
                branch=current_branch,
                worktree=worktree,
                reason=f"Unexpected error: {e}",
            )

        # Write result JSON
        write_result_json(result)

        # Print summary
        print(f"PR #{pr}: {result.outcome.value}")
        if result.reason:
            print(f"  {result.reason}")

        # Return appropriate exit code
        if result.outcome == MergeOutcome.MERGED:
            return 0
        elif result.outcome in (MergeOutcome.KICKBACK, MergeOutcome.PUSHED_NOT_MERGED):
            return 2
        elif result.outcome == MergeOutcome.REFUSED:
            return 3
        else:  # INTERNAL_ERROR
            return 1

    except Exception as e:
        print(f"Enqueue failed: {e}", file=sys.stderr)
        return 1


def _cmd_status(argv: List[str]) -> int:
    """Show queue status."""
    try:
        state_dir = ensure_state_dir()

        # Get current ticket holder (if any)
        tickets_dir = state_dir / "tickets"
        live_tickets = []
        if tickets_dir.exists():
            for ticket_file in sorted(tickets_dir.iterdir()):
                if ticket_file.is_file():
                    try:
                        with open(ticket_file) as f:
                            data = json.load(f)
                            live_tickets.append((int(ticket_file.name), data.get("pr")))
                    except Exception:
                        pass

        # Print queue depth
        print(f"Queue depth: {len(live_tickets)}")
        if live_tickets:
            for ticket_num, pr in live_tickets:
                print(f"  Ticket {ticket_num}: PR #{pr}")

        # Print last result if exists
        result_path = state_dir / "result.json"
        if result_path.exists():
            try:
                with open(result_path) as f:
                    result_data = json.load(f)
                    print(f"Last result: PR #{result_data.get('pr')} - {result_data.get('outcome')}")
            except Exception:
                pass

        return 0

    except Exception as e:
        print(f"Status check failed: {e}", file=sys.stderr)
        return 1


def _cmd_reverify(argv: List[str]) -> int:
    """Clear a base-failed record."""
    parser = argparse.ArgumentParser(description="Clear a base-failed record")
    parser.add_argument("sha", nargs="?", help="SHA to reverify (default: base tip)")

    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 1

    try:
        state_dir = ensure_state_dir()

        # Get SHA from args, or use current base branch tip
        if args.sha:
            sha = args.sha
            # Validate sha format (40-hex)
            if not _is_valid_sha40(sha):
                print(f"Invalid SHA format: {sha} (must be 40 hex digits)", file=sys.stderr)
                return 3
        else:
            try:
                default_branch, err = git.get_default_branch()
                if err or not default_branch:
                    print("Failed to get default branch", file=sys.stderr)
                    return 1
                sha = Runner.run_git(["rev-parse", f"origin/{default_branch}"]).strip()
            except Exception as e:
                print(f"Failed to get base tip: {e}", file=sys.stderr)
                return 1

        # Delete base-failed/<sha> if it exists
        failed_path = state_dir / "base-failed" / sha
        if failed_path.exists():
            failed_path.unlink()
            print(f"Cleared base-failed record for {sha[:8]}")
        else:
            print(f"No base-failed record found for {sha[:8]}")

        return 0

    except Exception as e:
        print(f"Reverify failed: {e}", file=sys.stderr)
        return 1


def _cmd_bootstrap(argv: List[str]) -> int:
    """Anchor base history."""
    try:
        # Get default branch
        default_branch, err = git.get_default_branch()
        if err or not default_branch:
            print("Failed to get default branch", file=sys.stderr)
            return 1

        # Get current tip of base branch
        try:
            base_sha = Runner.run_git(["rev-parse", f"origin/{default_branch}"]).strip()
        except Exception as e:
            print(f"Failed to get base tip: {e}", file=sys.stderr)
            return 1

        # Load config
        try:
            config_path = resolve_config_path()
            config = load_and_validate_config(config_path)
        except Exception as e:
            print(f"Config error: {e}", file=sys.stderr)
            return 1

        # Acquire merge.lock to prevent concurrent scratch-worktree operations
        merge_lock_path = get_merge_lock_path()
        ensure_state_dir()
        merge_lock_fd = os.open(
            str(merge_lock_path),
            os.O_CREAT | os.O_RDWR | os.O_CLOEXEC,
            0o600
        )
        try:
            fcntl.flock(merge_lock_fd, fcntl.LOCK_EX)

            # Verify base in scratch worktree
            print(f"Verifying base {base_sha[:8]}...")
            # M9: Thread lock fd if configured to inherit
            lock_fd_to_pass = merge_lock_fd if config.inherit_lock_fd else None
            verify_outcome = _verify_base_in_scratch(base_sha, config, lock_fd_to_inherit=lock_fd_to_pass)
            if verify_outcome == VerifyBaseOutcome.VERIFIED:
                # Write base-verified record
                state_dir = ensure_state_dir()
                verified_path = state_dir / "base-verified" / base_sha
                os.makedirs(verified_path.parent, exist_ok=True)
                _touch_record(verified_path)
                print(f"Success: base {base_sha[:8]} is verified")
                return 0
            elif verify_outcome == VerifyBaseOutcome.INFRA_ERROR:
                print("Infrastructure error: base verification failed due to setup/infrastructure issues", file=sys.stderr)
                return 1
            else:
                print(f"Failed: base {base_sha[:8]} did not pass gate", file=sys.stderr)
                return 1
        finally:
            try:
                fcntl.flock(merge_lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(merge_lock_fd)

    except Exception as e:
        print(f"Bootstrap failed: {e}", file=sys.stderr)
        return 1


def _cmd_resume(argv: List[str]) -> int:
    """Resume a failed Claude hand-off."""
    try:
        state_dir = ensure_state_dir()

        # Check if there's a result.json with INTERNAL_ERROR or similar
        result_path = state_dir / "result.json"
        if not result_path.exists():
            print("Nothing to resume", file=sys.stderr)
            # M15: Exit non-zero; resume is not yet implemented
            return 1

        try:
            with open(result_path) as f:
                result_data = json.load(f)
                outcome = result_data.get("outcome")
                if outcome in ("internal_error", "kickback", "pushed_not_merged"):
                    print(f"Found pending result: PR #{result_data.get('pr')} - {outcome}")
                    print("Resume logic not yet implemented", file=sys.stderr)
                    # M15: Exit non-zero; stub is unimplemented
                    return 1
                else:
                    print("Nothing to resume", file=sys.stderr)
                    # M15: Exit non-zero when there's nothing to resume
                    return 1
        except Exception:
            print("Nothing to resume", file=sys.stderr)
            # M15: Exit non-zero on error
            return 1

    except Exception as e:
        print(f"Resume failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
