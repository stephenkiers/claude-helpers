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

import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple, Sequence, Union

from . import git


# ============================================================================
# Config (Step 1)
# ============================================================================

@dataclass
class MergeQueueConfig:
    """Configuration for the merge queue."""
    base: str
    steps: List[Union[str, Dict[str, Any]]]
    cleanup: bool = False
    scratch_setup: List[str] = field(default_factory=list)
    inherit_lock_fd: bool = False
    allow_unverified: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MergeQueueConfig":
        """Construct from parsed JSON dict."""
        field_names = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in field_names})


def resolve_config_path(config_flag: Optional[str] = None) -> Path:
    """
    Resolve the merge-queue config location.

    Precedence:
    1. --config <path> flag
    2. MERGE_QUEUE_CONFIG env var
    3. Default: <container>/merge-queue.json where <container> is parent of worktrees/

    Raises if the layout doesn't match and no override is provided.
    """
    if config_flag:
        return Path(config_flag).resolve()

    if env_path := os.environ.get("MERGE_QUEUE_CONFIG"):
        return Path(env_path).resolve()

    try:
        git_common_dir = Path(git.get_git_common_dir()).resolve()
    except Exception as e:
        raise RuntimeError(
            f"Could not determine default config location: {e}\n"
            f"Use --config <path> or set MERGE_QUEUE_CONFIG"
        )

    try:
        current_worktree = git_common_dir.parent
        if current_worktree.name != "worktrees":
            raise RuntimeError(
                f"Layout does not match: git-common-dir parent is {current_worktree.name}, not 'worktrees'\n"
                f"Use --config <path> or set MERGE_QUEUE_CONFIG"
            )
        container = current_worktree.parent
        return container / "merge-queue.json"
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(
            f"Could not derive default config location: {e}\n"
            f"Use --config <path> or set MERGE_QUEUE_CONFIG"
        )


def load_and_validate_config(config_path: Path) -> MergeQueueConfig:
    """
    Load and validate the merge-queue config.

    Enforces strict schema:
    - base: non-empty string, must equal repo's default branch
    - steps: non-empty list of non-empty strings or {cmd: str, timeout_secs?: int}
    - cleanup: bool
    - scratch_setup: list of strings (optional)
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

    known_keys = {"base", "steps", "cleanup", "scratch_setup", "inherit_lock_fd", "allow_unverified"}
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
            if not isinstance(step.get("cmd"), str) or not step["cmd"]:
                raise ValueError("steps: each dict step must have a non-empty 'cmd' string")
            if "timeout_secs" in step:
                if not isinstance(step["timeout_secs"], int) or step["timeout_secs"] <= 0:
                    raise ValueError("steps: timeout_secs must be a positive integer")
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

    if "inherit_lock_fd" in data and not isinstance(data["inherit_lock_fd"], bool):
        raise ValueError("inherit_lock_fd must be a boolean")

    if "allow_unverified" in data:
        if not isinstance(data["allow_unverified"], list):
            raise ValueError("allow_unverified must be a list of 40-hex shas")
        for sha in data["allow_unverified"]:
            if not isinstance(sha, str) or not re.match(r"^[0-9a-f]{40}$", sha):
                raise ValueError(f"allow_unverified: invalid sha {sha} (must be 40-hex)")

    default_branch, err = git.get_default_branch()
    if err or not default_branch:
        raise RuntimeError(f"Could not determine default branch: {err}")
    if data["base"] != default_branch:
        raise ValueError(
            f"base '{data['base']}' does not equal default branch '{default_branch}'"
        )

    return MergeQueueConfig.from_dict(data)


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
        import fcntl
        fcntl.flock(alloc_lock_fd, fcntl.LOCK_EX)

        # List existing tickets and probe
        live_tickets = []
        try:
            ticket_files = sorted([f for f in tickets_dir.iterdir() if f.is_file()])
            for ticket_file in ticket_files:
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
                        live_tickets.append(int(ticket_file.name))
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

    import fcntl

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
                    n = int(ticket_file.name)
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
                    os.close(alloc_lock_fd)
                    break
                except BlockingIOError:
                    pass

            fcntl.flock(alloc_lock_fd, fcntl.LOCK_UN)
            os.close(alloc_lock_fd)
        except (OSError, BlockingIOError):
            pass

        time.sleep(1.0)

    # Now block on merge.lock
    try:
        fcntl.flock(merge_lock_fd, fcntl.LOCK_EX)
    except OSError:
        os.close(merge_lock_fd)
        raise
    return merge_lock_fd


def release_ticket(ticket_num: int, merge_lock_fd: int) -> None:
    """
    Release the ticket and merge.lock under alloc.lock.

    Unlink ticket, unlock merge.lock, close both.
    """
    state_dir = get_state_dir()
    tickets_dir = state_dir / "tickets"
    alloc_lock_path = get_alloc_lock_path()

    import fcntl

    alloc_lock_fd = os.open(str(alloc_lock_path), os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(alloc_lock_fd, fcntl.LOCK_EX)

        ticket_path = tickets_dir / f"{ticket_num:012d}"
        ticket_path.unlink(missing_ok=True)

        fcntl.flock(alloc_lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(alloc_lock_fd)

    try:
        fcntl.flock(merge_lock_fd, fcntl.LOCK_UN)
    except OSError:
        pass
    os.close(merge_lock_fd)


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
    inherit_lock_fd: Optional[int] = None,
) -> StepOutcome:
    """
    Run a step via subprocess.Popen with a new session.

    Uses shell=True (steps are maintainer-authored shell strings).
    Captures stdout/stderr to log_path.
    In finally: killpg(SIGTERM), wait 5s grace, then killpg(SIGKILL).

    If timeout_secs is set, kill the group on expiry and return timed_out=True.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)

    log_fd = os.open(str(log_path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    pgid: Optional[int] = None
    timed_out = False
    error: Optional[str] = None

    def term_handler(signum: int, frame: Any) -> None:
        nonlocal timed_out
        if signum in (signal.SIGTERM, signal.SIGINT):
            timed_out = True
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
        if inherit_lock_fd is not None:
            popen_kwargs["pass_fds"] = (inherit_lock_fd,)

        proc = subprocess.Popen(cmd, cwd=str(cwd), **popen_kwargs)
        pgid = os.getpgid(proc.pid)

        # Wait with timeout
        start = time.time()
        while True:
            try:
                proc.wait(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                if timeout_secs is not None and (time.time() - start) >= timeout_secs:
                    timed_out = True
                    raise TimeoutError(f"Step timed out after {timeout_secs}s")

    except TimeoutError as e:
        error = str(e)
    except Exception as e:
        error = f"Step failed: {e}"
    finally:
        # Kill the process group
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGTERM)
                time.sleep(5.0)
            except ProcessLookupError:
                pass

            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        os.close(log_fd)
        signal.signal(signal.SIGTERM, old_sigterm)
        signal.signal(signal.SIGINT, old_sigint)

    return StepOutcome(
        success=(error is None and not timed_out),
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
        if not branch or branch.startswith("-") or any(c in branch for c in " \t\n\r"):
            raise ValueError(f"Invalid branch name: {branch}")
        return branch

    @staticmethod
    def validate_ref(ref: str) -> str:
        """Validate a ref (40-hex sha or branch)."""
        if not ref or ref.startswith("-") or any(c in ref for c in " \t\n\r"):
            raise ValueError(f"Invalid ref: {ref}")
        return ref

    @staticmethod
    def run_git(args: List[str], cwd: Optional[Path] = None, check: bool = True) -> str:
        """Run git with argv list."""
        return git.run_git_command(args, cwd=cwd, check=check)

    @staticmethod
    def run_gh(args: List[str], cwd: Optional[Path] = None, check: bool = True) -> str:
        """Run gh with argv list."""
        return git.run_gh_command(args, cwd=cwd, check=check)

    @staticmethod
    def force_push_tested(
        branch: str,
        tested_sha: str,
        lease_sha: str,
        cwd: Optional[Path] = None,
    ) -> bool:
        """
        Force-push with pinned lease.

        The ONLY force-push call site in merge_queue.py.
        Asserts both shas match ^[0-9a-f]{40}$.
        Runs: git push --force-with-lease=<branch>:<lease_sha> origin <tested_sha>:refs/heads/<branch>

        Returns True if push succeeded, False if lease rejected (no-op).
        Raises on other errors.
        """
        if not re.match(r"^[0-9a-f]{40}$", tested_sha):
            raise ValueError(f"tested_sha is not 40-hex: {tested_sha}")
        if not re.match(r"^[0-9a-f]{40}$", lease_sha):
            raise ValueError(f"lease_sha is not 40-hex: {lease_sha}")

        branch = Runner.validate_branch(branch)

        try:
            args = [
                "push",
                f"--force-with-lease={branch}:{lease_sha}",
                "origin",
                f"{tested_sha}:refs/heads/{branch}",
            ]
            Runner.run_git(args, cwd=cwd)
            return True
        except git.GitCommandError as e:
            # Lease rejection typically contains "rejected" or "stale"
            if "rejected" in str(e) or "stale" in str(e).lower():
                return False
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

    Runs git interpret-trailers --parse and matches only the final trailer block.
    Text in the subject or earlier in the body does not count.
    The trailer is valid only if tested-base is an ancestor of the commit.

    Returns (tested_base, tested_head) if valid, (None, None) otherwise.
    """
    try:
        output = Runner.run_git(["interpret-trailers", "--parse", commit], cwd=cwd, check=False)
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
                # Verify tested-base is an ancestor
                if git.is_ancestor(base, commit, cwd=cwd):
                    return base, head
            return None, None

    return None, None


def scan_base(
    base_sha: str,
    allow_unverified: List[str],
    cwd: Optional[Path] = None,
) -> Tuple[bool, List[str]]:
    """
    Scan origin/<base> first-parent history for unverified commits.

    An anchor is a commit with a valid trailer or a sha with a base-verified record.
    Shas in allow_unverified are skipped (excused individually) but the scan continues,
    so an allowlisted commit can't hide an untrailered commit below it.

    Returns (has_anchor, unverified_commits).
    """
    state_dir = get_state_dir()
    verified_dir = state_dir / "base-verified"

    try:
        log_output = Runner.run_git(
            ["log", "--format=%H", "--first-parent", base_sha],
            cwd=cwd,
        )
    except Exception:
        return False, []

    commits = log_output.strip().split("\n")
    unverified: List[str] = []

    for commit in commits:
        commit = commit.strip()
        if not commit:
            continue

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


@dataclass
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


# ============================================================================
# Result JSON (Step 7)
# ============================================================================

def write_result_json(result: MergeResult, cwd: Optional[Path] = None) -> None:
    """Write result.json to state dir."""
    state_dir = ensure_state_dir()
    result_path = state_dir / "result.json"

    result_dict = {
        "outcome": result.outcome.value,
        "pr": result.pr,
        "branch": result.branch,
        "worktree": result.worktree,
        "orig_head": result.orig_head,
        "tested_sha": result.tested_sha,
        "reason": result.reason,
        "details": result.details,
        "failing_step": result.failing_step,
        "log_path": result.log_path,
        "timestamp": time.time(),
    }

    fd = os.open(str(result_path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(result_dict, indent=2).encode())
    finally:
        os.close(fd)

    # Also keep a copy in results/<pr>-<ts>.json
    ts = int(time.time() * 1000)
    copy_path = state_dir / "results" / f"{result.pr}-{ts}.json"
    copy_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(copy_path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(result_dict, indent=2).encode())
    finally:
        os.close(fd)


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

    Returns MergeResult with one of: MERGED, KICKBACK, PUSHED_NOT_MERGED, REFUSED.
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
            release_ticket(ticket_num, -1)
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
            release_ticket(ticket_num, merge_lock_fd)

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


def _locked_flow(
    pr: int,
    branch: str,
    worktree: str,
    config: MergeQueueConfig,
    merge_lock_fd: int,
    inherit_lock_fd: Optional[int],
    config_path: Optional[Path] = None,
) -> MergeResult:
    """
    Locked flow (steps 3-10 of the plan).
    """
    cwd = Path(worktree).resolve()

    try:
        # Re-check tree is clean and no rebase/merge
        if not _is_tree_clean(cwd):
            return MergeResult(
                outcome=MergeOutcome.KICKBACK,
                pr=pr,
                branch=branch,
                worktree=worktree,
                reason="Worktree tree is not clean (re-check in lock)",
            )

        if _has_rebase_or_merge_in_progress(cwd):
            return MergeResult(
                outcome=MergeOutcome.KICKBACK,
                pr=pr,
                branch=branch,
                worktree=worktree,
                reason="Rebase or merge is in progress; run appropriate abort command",
            )

        # Re-validate config
        try:
            if config_path is None:
                config_path = resolve_config_path()
            config = load_and_validate_config(config_path)
        except Exception as e:
            return MergeResult(
                outcome=MergeOutcome.KICKBACK,
                pr=pr,
                branch=branch,
                worktree=worktree,
                reason=f"Config changed or invalid: {e}",
            )

        # Re-validate PR
        try:
            pr_data = git.pr_view_json(
                branch,
                ["state", "baseRefName", "headRefName"],
                cwd=cwd
            )
            if not pr_data or pr_data.get("state") != "OPEN":
                return MergeResult(
                    outcome=MergeOutcome.KICKBACK,
                    pr=pr,
                    branch=branch,
                    worktree=worktree,
                    reason="PR is no longer OPEN",
                )
            if pr_data.get("baseRefName") != config.base:
                return MergeResult(
                    outcome=MergeOutcome.KICKBACK,
                    pr=pr,
                    branch=branch,
                    worktree=worktree,
                    reason="PR base has changed",
                )
            if pr_data.get("headRefName") != branch:
                return MergeResult(
                    outcome=MergeOutcome.KICKBACK,
                    pr=pr,
                    branch=branch,
                    worktree=worktree,
                    reason="PR branch has changed",
                )
        except Exception as e:
            return MergeResult(
                outcome=MergeOutcome.KICKBACK,
                pr=pr,
                branch=branch,
                worktree=worktree,
                reason=f"Failed to re-validate PR: {e}",
            )

        # Fetch and record lease_sha, base_sha, orig_head
        try:
            Runner.run_git(["fetch", "origin"], cwd=cwd)
        except Exception as e:
            return MergeResult(
                outcome=MergeOutcome.KICKBACK,
                pr=pr,
                branch=branch,
                worktree=worktree,
                reason=f"Failed to fetch: {e}",
            )

        try:
            lease_sha = Runner.run_git(["rev-parse", f"origin/{branch}"], cwd=cwd).strip()
            base_sha = Runner.run_git(["rev-parse", f"origin/{config.base}"], cwd=cwd).strip()
            orig_head = Runner.run_git(["rev-parse", "HEAD"], cwd=cwd).strip()
        except Exception as e:
            return MergeResult(
                outcome=MergeOutcome.KICKBACK,
                pr=pr,
                branch=branch,
                worktree=worktree,
                reason=f"Failed to parse shas: {e}",
            )

        # Poison check
        poison_ok, poison_shas = scan_base(base_sha, config.allow_unverified, cwd=cwd)
        if not poison_ok:
            state_dir = ensure_state_dir()
            verified_path = state_dir / "base-verified" / base_sha
            failed_path = state_dir / "base-failed" / base_sha

            # Check if already verified
            if verified_path.exists():
                # Base is verified; proceed
                poison_ok = True
            else:
                # Try to verify the base by running gate in scratch worktree
                if _verify_base_in_scratch(base_sha, config):
                    # Gate passed; write verified record
                    os.makedirs(verified_path.parent, exist_ok=True)
                    os.open(str(verified_path), os.O_CREAT | os.O_WRONLY, 0o600)
                    poison_ok = True
                else:
                    # Gate failed; write failed record only on first failure
                    if not failed_path.exists():
                        os.makedirs(failed_path.parent, exist_ok=True)
                        os.open(str(failed_path), os.O_CREAT | os.O_WRONLY, 0o600)

                    return MergeResult(
                        outcome=MergeOutcome.KICKBACK,
                        pr=pr,
                        branch=branch,
                        worktree=worktree,
                        orig_head=orig_head,
                        reason="Base has unverified commits; run 'merge-queue bootstrap' or 'merge-queue reverify'",
                        details=f"Unverified: {', '.join(poison_shas[:3])}...",
                    )

        # Rebase
        try:
            Runner.run_git(["rebase", f"origin/{config.base}"], cwd=cwd)
        except Exception as e:
            # Conflict; abort and kick back
            try:
                Runner.run_git(["rebase", "--abort"], cwd=cwd)
            except Exception as abort_err:
                restore_failed = f"rebase --abort failed: {abort_err}"
                return MergeResult(
                    outcome=MergeOutcome.KICKBACK,
                    pr=pr,
                    branch=branch,
                    worktree=worktree,
                    orig_head=orig_head,
                    reason="Rebase conflict (abort failed)",
                    restore_failed=restore_failed,
                )

            return MergeResult(
                outcome=MergeOutcome.KICKBACK,
                pr=pr,
                branch=branch,
                worktree=worktree,
                orig_head=orig_head,
                reason=f"Rebase conflict: {e}",
            )

        # Record tested_sha
        try:
            tested_sha = Runner.run_git(["rev-parse", "HEAD"], cwd=cwd).strip()
        except Exception as e:
            return MergeResult(
                outcome=MergeOutcome.KICKBACK,
                pr=pr,
                branch=branch,
                worktree=worktree,
                orig_head=orig_head,
                reason=f"Failed to record tested_sha: {e}",
            )

        # Run gate steps
        state_dir = ensure_state_dir()
        for i, step in enumerate(config.steps):
            if isinstance(step, str):
                step_cmd = step
                step_timeout = None
            else:
                step_cmd = step.get("cmd", "")
                step_timeout = step.get("timeout_secs")

            step_name = f"step-{i}"
            log_path = state_dir / "logs" / f"{pr}-{step_name}.log"

            step_outcome = run_step(
                step_cmd,
                cwd,
                log_path,
                timeout_secs=step_timeout,
                inherit_lock_fd=inherit_lock_fd,
            )

            if not step_outcome.success:
                return MergeResult(
                    outcome=MergeOutcome.KICKBACK,
                    pr=pr,
                    branch=branch,
                    worktree=worktree,
                    orig_head=orig_head,
                    tested_sha=tested_sha,
                    reason=f"Step {step_name} failed",
                    failing_step=step_name,
                    log_path=str(log_path),
                    details=step_outcome.error,
                )

        # Re-assert HEAD == tested_sha and clean tree
        try:
            current_head = Runner.run_git(["rev-parse", "HEAD"], cwd=cwd).strip()
            if current_head != tested_sha:
                return MergeResult(
                    outcome=MergeOutcome.KICKBACK,
                    pr=pr,
                    branch=branch,
                    worktree=worktree,
                    orig_head=orig_head,
                    tested_sha=tested_sha,
                    reason="HEAD moved during gate steps",
                )
            if not _is_tree_clean(cwd):
                return MergeResult(
                    outcome=MergeOutcome.KICKBACK,
                    pr=pr,
                    branch=branch,
                    worktree=worktree,
                    orig_head=orig_head,
                    tested_sha=tested_sha,
                    reason="Tree became dirty during gate steps",
                )
        except Exception as e:
            return MergeResult(
                outcome=MergeOutcome.KICKBACK,
                pr=pr,
                branch=branch,
                worktree=worktree,
                orig_head=orig_head,
                tested_sha=tested_sha,
                reason=f"Failed to re-assert HEAD: {e}",
            )

        # Push
        try:
            push_ok = Runner.force_push_tested(branch, tested_sha, lease_sha, cwd=cwd)
            if not push_ok:
                return MergeResult(
                    outcome=MergeOutcome.KICKBACK,
                    pr=pr,
                    branch=branch,
                    worktree=worktree,
                    orig_head=orig_head,
                    tested_sha=tested_sha,
                    reason="Push rejected by lease (another clone pushed to branch)",
                )
        except Exception as e:
            return MergeResult(
                outcome=MergeOutcome.KICKBACK,
                pr=pr,
                branch=branch,
                worktree=worktree,
                orig_head=orig_head,
                tested_sha=tested_sha,
                reason=f"Push failed: {e}",
            )

        # Base unchanged check (fresh fetch)
        try:
            Runner.run_git(["fetch", "origin", config.base], cwd=cwd)
            new_base_sha = Runner.run_git(["rev-parse", f"origin/{config.base}"], cwd=cwd).strip()
            if new_base_sha != base_sha:
                return MergeResult(
                    outcome=MergeOutcome.KICKBACK,
                    pr=pr,
                    branch=branch,
                    worktree=worktree,
                    orig_head=orig_head,
                    tested_sha=tested_sha,
                    reason=f"Base moved during gate (new: {new_base_sha[:8]})",
                )
        except Exception as e:
            return MergeResult(
                outcome=MergeOutcome.KICKBACK,
                pr=pr,
                branch=branch,
                worktree=worktree,
                orig_head=orig_head,
                tested_sha=tested_sha,
                reason=f"Failed to verify base unchanged: {e}",
            )

        # Merge
        try:
            merge_ok = _merge_pr(pr, tested_sha, base_sha, config, cwd=cwd)
            if not merge_ok:
                return MergeResult(
                    outcome=MergeOutcome.PUSHED_NOT_MERGED,
                    pr=pr,
                    branch=branch,
                    worktree=worktree,
                    orig_head=orig_head,
                    tested_sha=tested_sha,
                    reason="Push succeeded but merge failed",
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

        # Sync local branch (best-effort)
        try:
            Runner.run_git(["branch", "-f", branch, "@{u}"], cwd=cwd)
        except Exception:
            pass

        return MergeResult(
            outcome=MergeOutcome.MERGED,
            pr=pr,
            branch=branch,
            worktree=worktree,
            orig_head=orig_head,
            tested_sha=tested_sha,
            reason="Merged successfully",
        )

    except Exception as e:
        return MergeResult(
            outcome=MergeOutcome.INTERNAL_ERROR,
            pr=pr,
            branch=branch,
            worktree=worktree,
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
    git_dir = Path(cwd) / ".git"
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
            except (json.JSONDecodeError, Exception):
                pass

        return False
    except Exception:
        return False


def _verify_base_in_scratch(base_sha: str, config: MergeQueueConfig) -> bool:
    """
    Verify the base by running the main gate in a scratch worktree.

    Creates or reuses a scratch worktree, checks out base_sha, runs setup commands,
    and runs all gate steps. Returns True if all pass, False otherwise.
    """
    state_dir = ensure_state_dir()
    scratch_dir = state_dir / "scratch"

    try:
        # Create or update scratch worktree
        if not (scratch_dir / ".git").exists():
            scratch_dir.mkdir(parents=True, exist_ok=True)
            try:
                # Try to create a new worktree
                Runner.run_git(["worktree", "add", "--detach", str(scratch_dir), base_sha])
            except Exception:
                # Fallback: clone from origin
                try:
                    origin_url = Runner.run_git(["config", "--get", "remote.origin.url"]).strip()
                    subprocess.run(
                        ["git", "clone", "--shared", origin_url, str(scratch_dir)],
                        check=True,
                        capture_output=True,
                        cwd=str(state_dir),
                    )
                    Runner.run_git(["checkout", base_sha], cwd=scratch_dir)
                except Exception:
                    return False
        else:
            # Update existing worktree
            try:
                Runner.run_git(["fetch", "origin"], cwd=scratch_dir)
                Runner.run_git(["checkout", base_sha], cwd=scratch_dir)
            except Exception:
                return False

        # Run setup commands
        for setup_cmd in config.scratch_setup:
            log_path = state_dir / "logs" / "setup.log"
            outcome = run_step(setup_cmd, scratch_dir, log_path)
            if not outcome.success:
                return False

        # Run gate steps
        for i, step in enumerate(config.steps):
            if isinstance(step, str):
                step_cmd = step
                step_timeout = None
            else:
                step_cmd = step.get("cmd", "")
                step_timeout = step.get("timeout_secs")

            log_path = state_dir / "logs" / f"base-verify-step-{i}.log"
            outcome = run_step(step_cmd, scratch_dir, log_path, timeout_secs=step_timeout)
            if not outcome.success:
                return False

        return True

    except Exception:
        return False


def _merge_pr(pr: int, tested_sha: str, base_sha: str, config: MergeQueueConfig, cwd: Optional[Path] = None) -> bool:
    """
    Merge the PR.

    Polls until headRefOid == tested_sha, then runs gh pr merge.
    Returns True on success, False on failure.
    """
    # Poll for headRefOid match
    max_attempts = 60
    converged = False
    for _ in range(max_attempts):
        try:
            pr_data = git.pr_view_json(str(pr), ["headRefOid", "mergeable"], cwd=cwd)
            if pr_data and pr_data.get("headRefOid") == tested_sha and pr_data.get("mergeable") != "UNKNOWN":
                converged = True
                break
        except Exception:
            pass
        time.sleep(1.0)

    # If polling didn't converge, return False
    if not converged:
        return False

    # Merge
    try:
        trailer = build_trailer(base_sha, tested_sha)
        Runner.run_gh(
            [
                "pr",
                "merge",
                str(pr),
                "--squash",
                f"--match-head-commit={tested_sha}",
                "--subject",
                f"PR #{pr}",
                "--body",
                trailer,
            ],
            cwd=cwd,
        )
        return True
    except Exception:
        # Re-query PR state
        try:
            pr_data = git.pr_view_json(str(pr), ["state", "mergeCommit"], cwd=cwd)
            if pr_data and pr_data.get("state") == "MERGED":
                return True
        except Exception:
            pass
        return False


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

    if not argv or argv[0] in ("enqueue", "--no-claude", "--config"):
        return _cmd_enqueue(argv)
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
    _no_claude = False
    _config_path = None

    i = 0
    while i < len(argv):
        if argv[i] == "--no-claude":
            _no_claude = True
            i += 1
        elif argv[i] == "--config":
            if i + 1 >= len(argv):
                print("--config requires an argument", file=sys.stderr)
                return 1
            _config_path = argv[i + 1]
            i += 2
        else:
            i += 1

    try:
        # Detect PR and branch from current worktree
        current_branch = git.get_current_branch()
        if not current_branch:
            print("Failed to detect current branch", file=sys.stderr)
            return 3

        # Get worktree path
        try:
            worktree = Runner.run_git(["rev-parse", "--show-toplevel"]).strip()
        except Exception as e:
            print(f"Failed to get worktree path: {e}", file=sys.stderr)
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
            config_path = resolve_config_path(_config_path)
            config = load_and_validate_config(config_path)
        except Exception as e:
            print(f"Config error: {e}", file=sys.stderr)
            return 3

        # Run merge queue
        result = run_one(pr, current_branch, worktree, config, no_claude=_no_claude, config_path=config_path)

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
    try:
        state_dir = ensure_state_dir()

        # Get SHA from args, or use current base branch tip
        if argv:
            sha = argv[0]
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

        # Verify base in scratch worktree
        print(f"Verifying base {base_sha[:8]}...")
        if _verify_base_in_scratch(base_sha, config):
            # Write base-verified record
            state_dir = ensure_state_dir()
            verified_path = state_dir / "base-verified" / base_sha
            os.makedirs(verified_path.parent, exist_ok=True)
            os.open(str(verified_path), os.O_CREAT | os.O_WRONLY, 0o600)
            print(f"Success: base {base_sha[:8]} is verified")
            return 0
        else:
            print(f"Failed: base {base_sha[:8]} did not pass gate", file=sys.stderr)
            return 1

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
            return 0

        try:
            with open(result_path) as f:
                result_data = json.load(f)
                outcome = result_data.get("outcome")
                if outcome in ("internal_error", "kicked_back", "pushed_not_merged"):
                    print(f"Found pending result: PR #{result_data.get('pr')} - {outcome}")
                    print("Resume logic not yet implemented", file=sys.stderr)
                    return 0
                else:
                    print("Nothing to resume", file=sys.stderr)
                    return 0
        except Exception:
            print("Nothing to resume", file=sys.stderr)
            return 0

    except Exception as e:
        print(f"Resume failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
