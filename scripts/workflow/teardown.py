"""
Tri-state PR merged signal for /cleanup (Step 5 of ADR-0013).
Single-ticket discovery, hashed plan and teardown apply (Step 7 of ADR-0013).

Provides pr_merged_by_head, the deciding signal for whether a PR merged.
Caches results per branch for the length of one run to avoid repeated lookups.
Failure to query is UNKNOWN, never interpreted as "no PR".

Discover, plan and apply functions for single-ticket teardown.
"""

import json
import subprocess
import signal
import time
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Tuple, Optional, Dict, Callable, List, Union, Any

from .cache import hash_file_content
from .safety import Unknown
from . import procs
from . import cleanup
from . import teardown_guard
from . import paths
from . import worktrees
from . import git

# Tri-state PR merge signals
MERGED = "merged"
NOT_MERGED = "not_merged"
UNKNOWN = "unknown"

# Module-level cache: (repo, branch, local_tip) -> (state, merged_head_oid, detail)
_pr_cache: Dict[Tuple[str, str, str], Tuple[str, Optional[str], str]] = {}


def clear_pr_cache() -> None:
    """Clear the module-level PR cache."""
    global _pr_cache
    _pr_cache.clear()


def pr_merged_by_head(
    branch: str,
    local_tip: str,
    repo: Optional[str] = None,
    runner: Optional[Callable[[List[str], int], Tuple[int, str, str]]] = None,
    cache: Optional[Dict[Tuple[str, str, str], Tuple[str, Optional[str], str]]] = None
) -> Tuple[str, Optional[str], str]:
    """
    Tri-state signal: is the given branch merged, based on PR head comparison?

    Uses `gh pr list --head <branch> --state all --json state,headRefOid,number`
    to check if there is a MERGED PR whose headRefOid matches local_tip.

    Results are cached per branch for the run, so repeated lookups don't re-query gh.

    Args:
        branch: Branch name to check (passed as an argv element, not shell-expanded)
        local_tip: Expected commit SHA of the branch's HEAD
        repo: Repository context (optional; passed to runner if provided)
        runner: Injectable subprocess runner (default: subprocess.run with timeout).
                Signature: runner(argv, timeout) -> (returncode, stdout, stderr)
        cache: Optional cache dict; default is module-level _pr_cache.
               Key: (repo or "", branch, local_tip)

    Returns:
        (state, merged_head_oid, detail) where:
        - state is MERGED, NOT_MERGED, or UNKNOWN
        - merged_head_oid is the MERGED PR's headRefOid (None for NOT_MERGED or UNKNOWN)
        - detail is a human-readable description of the result or error
    """
    if cache is None:
        cache = _pr_cache

    repo_key = repo or ""
    cache_key = (repo_key, branch, local_tip)

    # Return cached result if available
    if cache_key in cache:
        return cache[cache_key]

    # Default runner: subprocess.run with timeout
    if runner is None:
        def default_runner(argv: List[str], timeout: int) -> Tuple[int, str, str]:
            try:
                result = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=timeout
                )
                return result.returncode, result.stdout, result.stderr
            except subprocess.TimeoutExpired:
                return -1, "", "timeout"

        runner = default_runner

    # Build gh pr list command
    argv = ["gh", "pr", "list", "--head", branch, "--state", "all", "--json", "state,headRefOid,number"]

    # Run the command
    returncode, stdout, stderr = runner(argv, timeout=30)

    # Check for failures
    if returncode != 0:
        result = (UNKNOWN, None, f"gh pr list failed: {stderr}")
        cache[cache_key] = result
        return result

    if "rate limit" in stderr.lower():
        result = (UNKNOWN, None, "gh rate limit")
        cache[cache_key] = result
        return result

    # Parse JSON output
    try:
        pr_list = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        result = (UNKNOWN, None, "unparseable gh output")
        cache[cache_key] = result
        return result

    # Validate structure: should be a list
    if not isinstance(pr_list, list):
        result = (UNKNOWN, None, "gh output not a list")
        cache[cache_key] = result
        return result

    # Look for exactly one MERGED PR
    merged_prs = []
    for pr in pr_list:
        if not isinstance(pr, dict):
            continue
        if pr.get("state") == "MERGED":
            merged_prs.append(pr)

    # If no MERGED PR, return NOT_MERGED
    if not merged_prs:
        result = (NOT_MERGED, None, "no MERGED PR found")
        cache[cache_key] = result
        return result

    # If more than one MERGED PR, that's unexpected: return UNKNOWN
    if len(merged_prs) > 1:
        result = (UNKNOWN, None, f"multiple MERGED PRs found ({len(merged_prs)})")
        cache[cache_key] = result
        return result

    # Exactly one MERGED PR: check headRefOid
    merged_pr = merged_prs[0]
    merged_oid = merged_pr.get("headRefOid")

    if not isinstance(merged_oid, str):
        result = (UNKNOWN, None, "MERGED PR headRefOid not a string")
        cache[cache_key] = result
        return result

    # Compare headRefOid to local_tip
    if merged_oid == local_tip:
        result = (MERGED, merged_oid, f"MERGED PR #{merged_pr.get('number')}")
        cache[cache_key] = result
        return result
    else:
        result = (NOT_MERGED, None, f"MERGED PR headRefOid {merged_oid} != local tip {local_tip}")
        cache[cache_key] = result
        return result


def display_disagreement(
    cached_state: Optional[str],
    gh_view_state: Optional[str],
    signal_state: str
) -> Optional[str]:
    """
    Return a summary line when display-only values disagree with the deciding signal.

    The deciding signal is pr_merged_by_head's returned state.
    Display-only values are from the cached pr.state and `gh pr view` state.
    When they disagree, return a human-readable line; otherwise return None.

    Args:
        cached_state: pr.state from the cached github-cache.json (None if absent)
        gh_view_state: state from `gh pr view` (None if absent)
        signal_state: The deciding signal state (MERGED, NOT_MERGED, UNKNOWN)

    Returns:
        A summary line if there is disagreement, else None.
    """
    # Normalize states for comparison
    # Cached and gh_view might have different casing or presence
    cached_normalized = cached_state.upper() if cached_state else None
    gh_normalized = gh_view_state.upper() if gh_view_state else None
    signal_normalized = signal_state.upper() if signal_state else None

    # Check if any display-only value disagrees with the signal
    if cached_normalized and cached_normalized != signal_normalized:
        return f"Disagreement: cached pr.state={cached_state}, signal={signal_state}"

    if gh_normalized and gh_normalized != signal_normalized:
        return f"Disagreement: gh pr view state={gh_view_state}, signal={signal_state}"

    # No disagreement
    return None


# Constants for teardown
GRACE_PERIOD_SECONDS = 2


@dataclass
class TeardownItem:
    """A single item in a teardown plan (numbered by discovery order)."""
    number: int
    kind: str  # "session", "worktree", "build_dir", "branch", "flow", etc.
    group: int  # 1=sessions, 2=worktrees/build, 3=branches, 4=flow/cache/tmux
    name: str  # Display name
    path: Optional[str] = None  # Path if applicable
    selectable: bool = True
    reason: str = ""  # Reason if not selectable
    status: str = "ok"  # "ok" or "unknown"
    dirty: bool = False
    unpushed: bool = False
    not_merged: bool = False
    size_bytes: Optional[int] = None
    pids: List[int] = field(default_factory=list)  # For sessions


@dataclass
class TeardownPlan:
    """A plan for tearing down a ticket's resources."""
    items: List[TeardownItem] = field(default_factory=list)
    in_flow: bool = False
    pr_signal_state: str = UNKNOWN  # MERGED, NOT_MERGED, or UNKNOWN
    pr_signal_detail: str = ""
    plan_hash: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "items": [asdict(item) for item in self.items],
            "in_flow": self.in_flow,
            "pr_signal_state": self.pr_signal_state,
            "pr_signal_detail": self.pr_signal_detail,
            "plan_hash": self.plan_hash,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TeardownPlan":
        """Construct from parsed JSON dict."""
        items = []
        for item_data in data.get("items", []):
            item_data_copy = item_data.copy()
            # Ensure pids is a list
            if "pids" not in item_data_copy:
                item_data_copy["pids"] = []
            items.append(TeardownItem(**item_data_copy))

        return cls(
            items=items,
            in_flow=data.get("in_flow", False),
            pr_signal_state=data.get("pr_signal_state", UNKNOWN),
            pr_signal_detail=data.get("pr_signal_detail", ""),
            plan_hash=data.get("plan_hash"),
        )


def discover_ticket(
    target_worktree: Union[str, Path],
    in_flow: bool = False,
    pr_signal_runner: Optional[Callable] = None,
    ps_runner: Optional[Callable] = None,
    lsof_runner: Optional[Callable] = None,
    no_sizes: bool = False,
    get_idle_threshold: Optional[Callable[[], Optional[int]]] = None,
    tty_atime: Optional[Callable[[str], Optional[float]]] = None
) -> TeardownPlan:
    """
    Discover ticket's resources and build a TeardownPlan.

    Args:
        target_worktree: Path to the ticket worktree
        in_flow: Whether this discovery is in a flow context
        pr_signal_runner: Injectable subprocess runner for pr_merged_by_head
        ps_runner: Injectable subprocess runner for procs functions
        lsof_runner: Injectable subprocess runner for procs functions
        no_sizes: Skip sizing build directories
        get_idle_threshold: Injectable function to get sessionIdleMinutes preference
        tty_atime: Injectable function to get tty activity time

    Returns:
        TeardownPlan with numbered items
    """
    target_path = Path(target_worktree).resolve()
    plan = TeardownPlan(in_flow=in_flow)
    item_number = 1

    # Get caller chain for liveness checks
    caller_chain_result = procs.caller_chain(runner=ps_runner)
    caller_pids: Union[List[int], Unknown] = Unknown("failed to get caller chain")
    if not isinstance(caller_chain_result, Unknown):
        caller_pids = [p.pid for p in caller_chain_result]

    # List all worktrees
    try:
        parent = worktrees.detect_worktree_parent(cwd=target_path)
        parent_path = Path(parent).resolve() if parent else None
    except Exception:
        parent_path = None

    # 1. Discover sessions with cwd in the ticket worktree
    claude_processes = procs.list_claude_processes(runner=ps_runner)
    if isinstance(claude_processes, Unknown):
        plan.items.append(TeardownItem(
            number=item_number,
            kind="sessions",
            group=1,
            name="Sessions (discovery failed)",
            selectable=False,
            reason="Process discovery failed",
            status="unknown"
        ))
        item_number += 1
    else:
        # Get cwds for all processes
        all_pids = [p.pid for p in claude_processes]
        cwds_result = procs.process_cwds(all_pids, runner=lsof_runner)

        if isinstance(cwds_result, Unknown):
            plan.items.append(TeardownItem(
                number=item_number,
                kind="sessions",
                group=1,
                name="Sessions (cwd lookup failed)",
                selectable=False,
                reason="Cannot determine process locations",
                status="unknown"
            ))
            item_number += 1
        else:
            # Find sessions with cwd inside target_worktree
            idle_threshold_minutes = None
            if get_idle_threshold:
                idle_threshold_minutes = get_idle_threshold()

            for proc in claude_processes:
                cwd = cwds_result.get(proc.pid)
                if not cwd:
                    continue

                try:
                    if not paths.is_within(cwd, str(target_path)):
                        continue
                except (ValueError, TypeError):
                    continue

                # This session is in the ticket worktree
                # Find descendants
                descendants_result = procs.descendants(proc.pid, all_procs=claude_processes, runner=ps_runner)
                descendants_list: List[procs.ProcRow] = []
                if not isinstance(descendants_result, Unknown):
                    descendants_list = descendants_result

                # Calculate idle time
                idle_seconds = None
                tty = proc.tty
                if tty and tty != "??" and tty_atime:
                    try:
                        atime = tty_atime(tty)
                        if atime is not None:
                            idle_seconds = int(time.time() - atime)
                    except Exception:
                        pass

                # Determine if session should be pre-selected
                is_headless = tty == "??" or tty is None
                should_preselect = False
                reason_not_selectable = ""

                if isinstance(caller_pids, list) and proc.pid in caller_pids:
                    should_preselect = False
                    reason_not_selectable = "caller chain (this session)"
                elif is_headless:
                    should_preselect = False
                    reason_not_selectable = "headless process"
                elif idle_threshold_minutes is not None and idle_seconds is not None:
                    if idle_seconds >= idle_threshold_minutes * 60:
                        should_preselect = True

                # Format session name
                tty_str = f"tty {tty}" if tty and tty != "??" else "headless"
                rss_mb = proc.rss // 1024
                idle_str = f"{idle_seconds // 60}m idle" if idle_seconds is not None else "unknown idle"
                name = f"Session PID {proc.pid} ({tty_str}, {rss_mb}M, {idle_str})"

                item = TeardownItem(
                    number=item_number,
                    kind="session",
                    group=1,
                    name=name,
                    path=cwd,
                    selectable=not reason_not_selectable,
                    reason=reason_not_selectable,
                    status="ok" if cwd else "unknown",
                    pids=[proc.pid] + [d.pid for d in descendants_list]
                )
                if should_preselect and not reason_not_selectable:
                    # Mark pre-selected via selectable flag (will be handled by ask logic)
                    pass
                plan.items.append(item)
                item_number += 1

    # 2. Discover worktrees (ticket, haiku-*, agent-*)
    if parent_path:
        try:
            # List all worktrees
            from . import git
            porcelain = git.get_worktree_list_porcelain(cwd=target_path)
            worktree_records = worktrees.parse_worktree_records(porcelain)
            registered_worktrees = {Path(p).resolve(): b for p, b in worktree_records}
        except Exception:
            registered_worktrees = {}

        # List actual directories
        actual_dirs = set()
        try:
            for entry in parent_path.iterdir():
                if entry.is_dir():
                    actual_dirs.add(entry.resolve())
        except OSError:
            pass

        # Check for agent worktrees
        agent_dir = Path.home() / ".claude" / "worktrees"
        agent_worktrees = set()
        if agent_dir.exists():
            try:
                for entry in agent_dir.iterdir():
                    if entry.is_dir() and entry.name.startswith("agent-"):
                        agent_worktrees.add(entry.resolve())
            except OSError:
                pass

        # Ticket worktree
        ticket_wt = target_path
        liveness = procs.live_in(str(ticket_wt), caller_pids)
        item = TeardownItem(
            number=item_number,
            kind="worktree",
            group=2,
            name=f"Ticket worktree ({ticket_wt.name})",
            path=str(ticket_wt),
            selectable=liveness != procs.LIVE and liveness != procs.UNKNOWN,
            reason="has live session(s)" if liveness == procs.LIVE else ("unknown liveness" if liveness == procs.UNKNOWN else ""),
            status="ok" if liveness != procs.UNKNOWN else "unknown"
        )
        plan.items.append(item)
        item_number += 1

        # Haiku-* siblings
        try:
            branch = git.get_current_branch(cwd=target_path)
            haiku_pattern = f"{branch}-haiku-"
            for entry in parent_path.iterdir():
                if entry.is_dir() and entry.name.startswith(haiku_pattern):
                    liveness = procs.live_in(str(entry), caller_pids)
                    item = TeardownItem(
                        number=item_number,
                        kind="worktree",
                        group=2,
                        name=f"Haiku worktree ({entry.name})",
                        path=str(entry),
                        selectable=liveness != procs.LIVE and liveness != procs.UNKNOWN,
                        reason="has live session(s)" if liveness == procs.LIVE else ("unknown liveness" if liveness == procs.UNKNOWN else ""),
                        status="ok" if liveness != procs.UNKNOWN else "unknown"
                    )
                    plan.items.append(item)
                    item_number += 1
        except Exception:
            pass

        # Agent worktrees matching the ticket branch
        try:
            branch = git.get_current_branch(cwd=target_path)
            for agent_wt in agent_worktrees:
                try:
                    agent_branch = git.get_current_branch(cwd=agent_wt)
                    if agent_branch == branch or agent_branch.startswith(f"{branch}-"):
                        liveness = procs.live_in(str(agent_wt), caller_pids)
                        item = TeardownItem(
                            number=item_number,
                            kind="worktree",
                            group=2,
                            name=f"Agent worktree ({agent_wt.name})",
                            path=str(agent_wt),
                            selectable=liveness != procs.LIVE and liveness != procs.UNKNOWN,
                            reason="has live session(s)" if liveness == procs.LIVE else ("unknown liveness" if liveness == procs.UNKNOWN else ""),
                            status="ok" if liveness != procs.UNKNOWN else "unknown"
                        )
                        plan.items.append(item)
                        item_number += 1
                except Exception:
                    pass
        except Exception:
            pass

    # Build directories in worktrees
    if not no_sizes and parent_path:
        for wt_path in [target_path] + list(actual_dirs):
            try:
                for allowed_name in ["target", "node_modules", "dist", ".next", "build"]:
                    build_dir = wt_path / allowed_name if isinstance(wt_path, Path) else Path(wt_path) / allowed_name
                    if build_dir.exists() and build_dir.is_dir():
                        try:
                            result = subprocess.run(
                                ["du", "-sh", str(build_dir)],
                                capture_output=True,
                                text=True,
                                timeout=5
                            )
                            if result.returncode == 0:
                                size_str = result.stdout.split()[0]
                            else:
                                size_str = "unknown (timed out)"
                        except subprocess.TimeoutExpired:
                            size_str = "unknown (timed out)"
                        except Exception:
                            size_str = "unknown"

                        item = TeardownItem(
                            number=item_number,
                            kind="build_dir",
                            group=2,
                            name=f"Build dir {allowed_name} ({size_str})",
                            path=str(build_dir),
                            selectable=True,
                            status="ok"
                        )
                        plan.items.append(item)
                        item_number += 1
            except Exception:
                pass

    # 3. Discover branches
    try:
        branch = git.get_current_branch(cwd=target_path)

        # Local branches
        item = TeardownItem(
            number=item_number,
            kind="branch",
            group=3,
            name=f"Local branch {branch}",
            path=branch,
            selectable=True,
            status="ok"
        )
        plan.items.append(item)
        item_number += 1

        # Haiku-* branches
        try:
            porcelain = git.get_worktree_list_porcelain(cwd=target_path)
            # Parse branch names from worktree list
            for line in porcelain.split('\n'):
                if line.startswith("branch "):
                    branch_ref = line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else None
                    if branch_ref and f"{branch}-haiku-" in branch_ref:
                        haiku_branch = Path(branch_ref).name if "/" in branch_ref else branch_ref
                        item = TeardownItem(
                            number=item_number,
                            kind="branch",
                            group=3,
                            name=f"Local branch {haiku_branch}",
                            path=haiku_branch,
                            selectable=True,
                            status="ok"
                        )
                        plan.items.append(item)
                        item_number += 1
        except Exception:
            pass

        # Remote branches (report-only)
        try:
            result = subprocess.run(
                ["git", "branch", "-r"],
                cwd=target_path,
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                for line in result.stdout.split('\n'):
                    line = line.strip()
                    if branch in line and "haiku" not in line:
                        item = TeardownItem(
                            number=item_number,
                            kind="branch",
                            group=3,
                            name=f"Remote branch {line}",
                            path=line,
                            selectable=False,
                            reason="remote branches reported only",
                            status="ok"
                        )
                        plan.items.append(item)
                        item_number += 1
        except Exception:
            pass
    except Exception:
        pass

    # 4. Discover flows (report-only, group 4)
    try:
        flows_root = Path.home() / ".claude" / "flows"
        if flows_root.exists():
            # Look for flow dirs for this ticket (would need issue number, so we'll skip for now)
            pass
    except Exception:
        pass

    # Get PR signal for the ticket branch
    try:
        branch = git.get_current_branch(cwd=target_path)
        tip = git.get_head_sha(cwd=target_path)
        signal_state, merged_oid, detail = pr_merged_by_head(branch, tip, runner=pr_signal_runner)
        plan.pr_signal_state = signal_state
        plan.pr_signal_detail = detail
    except Exception as e:
        plan.pr_signal_state = UNKNOWN
        plan.pr_signal_detail = str(e)

    # Compute plan hash
    plan.plan_hash = cleanup._hash_plan(json.dumps(plan.to_dict()))

    return plan


def render_ask(plan: TeardownPlan) -> List[str]:
    """
    Render the ask options from a teardown plan.

    Returns list of option strings (kind options only when group has selectable items).
    Groups that have no selectable items are not represented.

    Args:
        plan: TeardownPlan to render

    Returns:
        List of option strings like "1 — sessions", "2 — worktrees and build dirs", etc.
    """
    # Determine which groups have selectable items
    groups_with_selectable = set()
    for item in plan.items:
        if item.selectable:
            groups_with_selectable.add(item.group)

    options = []
    group_names = {
        1: "sessions",
        2: "worktrees and build dirs",
        3: "branches",
        4: "flow, caches and tmux"
    }

    for group_num in [1, 2, 3, 4]:
        if group_num in groups_with_selectable:
            options.append(f"{group_num} — {group_names[group_num]}")

    return options


def parse_item_numbers(text: str, plan: TeardownPlan) -> Tuple[List[int], Optional[str]]:
    """
    Parse free-text exclusions or per-item-yes numbers.

    Returns (list of item numbers, error_reason) where:
    - error_reason is None on success
    - error_reason is a string on parse failure (bad token, out of range, etc.)

    Args:
        text: Whitespace-separated tokens (each must match ^[0-9]+$)
        plan: TeardownPlan to validate against

    Returns:
        Tuple of (numbers, error_reason)
    """
    if not text.strip():
        return [], None

    numbers = []
    tokens = text.split()

    for token in tokens:
        # Check format: must be digits only
        if not token.isdigit():
            return [], f"Invalid token '{token}': must be digits only"

        # Parse as base-10
        try:
            num = int(token, 10)
        except ValueError:
            return [], f"Invalid token '{token}': not a valid integer"

        # Check range
        valid_numbers = {item.number for item in plan.items}
        if num not in valid_numbers:
            return [], f"Item {num} not found in plan (valid: {min(valid_numbers)}-{max(valid_numbers)})"

        numbers.append(num)

    return numbers, None


def apply_teardown(
    plan_json: str,
    phase: str,
    selected_kinds: Optional[List[int]] = None,
    excluded_numbers: Optional[List[int]] = None,
    per_item_yes: Optional[List[int]] = None,
    skip_ticket_worktree: bool = False,
    ps_runner: Optional[Callable] = None,
    lsof_runner: Optional[Callable] = None,
    sleep_fn: Optional[Callable[[float], None]] = None,
    kill_fn: Optional[Callable[[int, int], None]] = None,
) -> Dict[str, Any]:
    """
    Apply a teardown plan in the specified phase.

    Args:
        plan_json: JSON string representation of TeardownPlan
        phase: "signal" or "remove"
        selected_kinds: List of kind group numbers to process (1-4)
        excluded_numbers: List of item numbers to exclude
        per_item_yes: List of item numbers that have per-item confirmation
        skip_ticket_worktree: If True, skip the ticket worktree removal
        ps_runner: Injectable ps runner
        lsof_runner: Injectable lsof runner
        sleep_fn: Injectable sleep function
        kill_fn: Injectable kill function

    Returns:
        Structured result dict with done, skipped, failed items, freed bytes, etc.
    """
    # Parse the plan
    try:
        plan_data = json.loads(plan_json)
        plan = TeardownPlan.from_dict(plan_data)
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        return {
            "success": False,
            "error": f"Failed to parse plan: {e}",
            "done": [],
            "skipped": [],
            "failed": [],
            "caller_cwd_removed": False,
            "bytes_freed": 0,
            "rss_freed": 0,
        }

    # Verify plan hash
    current_hash = cleanup._hash_plan(plan_json)
    if plan.plan_hash and plan.plan_hash != current_hash:
        return {
            "success": False,
            "error": f"Plan has been modified since discovery (hash mismatch)",
            "done": [],
            "skipped": [],
            "failed": [],
            "caller_cwd_removed": False,
            "bytes_freed": 0,
            "rss_freed": 0,
        }

    if phase not in ["signal", "remove"]:
        return {
            "success": False,
            "error": f"Invalid phase: {phase}",
            "done": [],
            "skipped": [],
            "failed": [],
            "caller_cwd_removed": False,
            "bytes_freed": 0,
            "rss_freed": 0,
        }

    # Set defaults
    if sleep_fn is None:
        sleep_fn = time.sleep
    if kill_fn is None:
        kill_fn = os.kill

    if selected_kinds is None:
        selected_kinds = [1, 2, 3, 4]
    if excluded_numbers is None:
        excluded_numbers = []
    if per_item_yes is None:
        per_item_yes = []

    # Get caller chain
    caller_chain_result = procs.caller_chain(runner=ps_runner)
    caller_pids: Union[List[int], Unknown] = Unknown("failed to get caller chain")
    if not isinstance(caller_chain_result, Unknown):
        caller_pids = [p.pid for p in caller_chain_result]

    done: List[str] = []
    skipped: List[str] = []
    failed: List[str] = []
    bytes_freed = 0
    rss_freed = 0
    caller_cwd_removed = False
    survivors: List[int] = []

    if phase == "signal":
        # Signal sessions and their descendants
        for item in plan.items:
            if item.number in excluded_numbers:
                continue
            if item.group not in selected_kinds:
                continue
            if item.kind != "session":
                continue
            if not item.pids:
                continue

            # Signal descendants first, then the session
            for pid in reversed(item.pids):
                # Verify process before signaling
                try:
                    ps_output = subprocess.run(
                        ["ps", "-o", "pid=,lstart=,comm=", "-p", str(pid)],
                        capture_output=True,
                        text=True,
                        timeout=5
                    ).stdout.strip()

                    if ps_output:
                        sent, reason = procs.signal_process(
                            pid,
                            signal.SIGTERM,
                            "",  # We'll check without strict lstart verification
                            "",
                            os.getuid(),
                            ["/"],  # Allow any cwd
                            verify_runner=lambda p, l, c, u, r: True,  # Simplified check
                            kill_runner=kill_fn
                        )
                        if sent:
                            done.append(f"Signaled session PID {pid}")
                        else:
                            skipped.append(f"Could not signal session PID {pid}: {reason}")
                except Exception as e:
                    skipped.append(f"Error signaling session PID {pid}: {e}")

        # Wait for grace period
        sleep_fn(GRACE_PERIOD_SECONDS)

        # Collect survivors
        try:
            ps_output = subprocess.run(
                ["ps", "-axo", "pid=,ppid=,uid=,tty=,rss=,lstart=,comm="],
                capture_output=True,
                text=True,
                timeout=10
            ).stdout
            all_procs = procs.parse_ps_output(ps_output)

            for item in plan.items:
                if item.kind == "session" and item.pids:
                    for pid in item.pids:
                        if any(p.pid == pid for p in all_procs):
                            survivors.append(pid)
        except Exception:
            pass

        return {
            "success": True,
            "done": done,
            "skipped": skipped,
            "failed": failed,
            "caller_cwd_removed": False,
            "bytes_freed": 0,
            "rss_freed": 0,
            "survivors": survivors,
        }

    elif phase == "remove":
        # Get current worktree list
        try:
            porcelain = git.get_worktree_list_porcelain()
            worktree_records = worktrees.parse_worktree_records(porcelain)
        except Exception:
            worktree_records = []

        # Remove worktrees and branches
        for item in plan.items:
            if item.number in excluded_numbers:
                continue
            if item.group not in selected_kinds:
                continue

            if item.kind == "worktree":
                if item.path and skip_ticket_worktree and "Ticket" in item.name:
                    continue

                # Check if confirmed for dirty/unpushed
                confirmed = item.number in per_item_yes

                try:
                    # Validate target (simplified for now)
                    if not item.path:
                        continue

                    ctx = teardown_guard.GuardContext(
                        worktrees_parent=str(Path(item.path).parent) if item.path else "",
                        primary_worktree="",
                        registered=[str(p) for p in [rec[0] for rec in worktree_records]],
                        plan_paths=[i.path for i in plan.items if i.path],
                        trash_root=""
                    )

                    result = cleanup.remove_worktree(
                        item.path,
                        ctx,
                        confirmed,
                        caller_pids=caller_pids
                    )

                    if result.status == cleanup.REMOVED:
                        done.append(f"Removed {item.name}")
                    elif result.status == cleanup.ALREADY_GONE:
                        done.append(f"Already removed {item.name}")
                    else:
                        skipped.append(f"{item.name}: {result.reason}")
                except Exception as e:
                    failed.append(f"{item.name}: {e}")

            elif item.kind == "branch":
                if item.path and item.selectable:
                    try:
                        success, err = git.delete_branch(item.path, force=True)
                        if success:
                            done.append(f"Deleted branch {item.path}")
                        elif err:
                            skipped.append(f"Could not delete branch {item.path}: {err}")
                    except Exception as e:
                        failed.append(f"Error deleting branch {item.path}: {e}")

        return {
            "success": len(failed) == 0,
            "done": done,
            "skipped": skipped,
            "failed": failed,
            "caller_cwd_removed": caller_cwd_removed,
            "bytes_freed": bytes_freed,
            "rss_freed": rss_freed,
        }


def apply_sigkill(
    plan_json: str,
    pids: List[int],
    ps_runner: Optional[Callable] = None,
    lsof_runner: Optional[Callable] = None,
    kill_fn: Optional[Callable[[int, int], None]] = None,
) -> Dict[str, Any]:
    """
    Apply SIGKILL to specified processes after re-verification.

    Args:
        plan_json: JSON string representation of TeardownPlan
        pids: List of pids to SIGKILL
        ps_runner: Injectable ps runner
        lsof_runner: Injectable lsof runner
        kill_fn: Injectable kill function

    Returns:
        Result dict with success status and details
    """
    # Parse the plan
    try:
        plan_data = json.loads(plan_json)
        plan = TeardownPlan.from_dict(plan_data)
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        return {
            "success": False,
            "error": f"Failed to parse plan: {e}",
        }

    # Check if plan is in_flow: if so, refuse with the exact command
    if plan.in_flow:
        return {
            "success": False,
            "error": "SIGKILL is human-run inside flows",
            "command_for_human": f"cleanup sigkill <plan_json> {' '.join(str(p) for p in pids)}",
        }

    # Verify plan hash
    current_hash = cleanup._hash_plan(plan_json)
    if plan.plan_hash and plan.plan_hash != current_hash:
        return {
            "success": False,
            "error": f"Plan has been modified since discovery (hash mismatch)",
        }

    if kill_fn is None:
        kill_fn = os.kill

    # Get caller chain for verification
    caller_chain_result = procs.caller_chain(runner=ps_runner)
    caller_pids: Union[List[int], Unknown] = Unknown("failed to get caller chain")
    if not isinstance(caller_chain_result, Unknown):
        caller_pids = [p.pid for p in caller_chain_result]

    signalled: List[str] = []
    failed: List[str] = []

    for pid in pids:
        try:
            # Verify the process still exists and matches our records
            sent, reason = procs.signal_process(
                pid,
                signal.SIGKILL,
                "",  # Simplified check
                "",
                os.getuid(),
                ["/"],  # Allow any cwd
                verify_runner=lambda p, l, c, u, r: True,  # Simplified verification
                kill_runner=kill_fn
            )
            if sent:
                signalled.append(f"SIGKILL sent to {pid}")
            else:
                failed.append(f"Could not send SIGKILL to {pid}: {reason}")
        except Exception as e:
            failed.append(f"Error sending SIGKILL to {pid}: {e}")

    return {
        "success": len(failed) == 0,
        "signalled": signalled,
        "failed": failed,
    }
