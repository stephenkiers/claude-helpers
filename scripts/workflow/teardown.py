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
    runner: Optional[Callable[..., Any]] = None,
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

    # Build gh pr list command (F36: explicit cwd for gh command)
    argv = ["gh", "pr", "list", "--head", branch, "--state", "all", "--json", "state,headRefOid,number"]
    if repo:
        argv = ["gh", "-C", repo, "pr", "list", "--head", branch, "--state", "all", "--json", "state,headRefOid,number"]

    # Run the command
    returncode, stdout, stderr = runner(argv, timeout=30)

    # Check for failures
    if returncode != 0:
        result = (UNKNOWN, None, f"gh pr list failed: {stderr}")
        # F40: Do not cache UNKNOWN PR results
        return result

    if "rate limit" in stderr.lower():
        result = (UNKNOWN, None, "gh rate limit")
        # F40: Do not cache UNKNOWN PR results
        return result

    # Parse JSON output
    try:
        pr_list = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        result = (UNKNOWN, None, "unparseable gh output")
        # F40: Do not cache UNKNOWN PR results
        return result

    # Validate structure: should be a list
    if not isinstance(pr_list, list):
        result = (UNKNOWN, None, "gh output not a list")
        # F40: Do not cache UNKNOWN PR results
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
        # F40: Do not cache UNKNOWN PR results
        return result

    # Exactly one MERGED PR: check headRefOid
    merged_pr = merged_prs[0]
    merged_oid = merged_pr.get("headRefOid")

    if not isinstance(merged_oid, str):
        result = (UNKNOWN, None, "MERGED PR headRefOid not a string")
        # F40: Do not cache UNKNOWN PR results
        return result

    # Compare headRefOid to local_tip
    if merged_oid == local_tip:
        cached_result: Tuple[str, Optional[str], str] = (MERGED, merged_oid, f"MERGED PR #{merged_pr.get('number')}")
        cache[cache_key] = cached_result
        return cached_result
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


# Constants for teardown (F37: Named constants for magic numbers)
GRACE_PERIOD_SECONDS = 2
PS_TIMEOUT_SECS = 10
LSOF_TIMEOUT_SECS = 10
GH_PR_LIST_TIMEOUT_SECS = 30
PS_SIGNAL_TIMEOUT_SECS = 5
BUILD_DIR_REMOVAL_TIMEOUT_SECS = 30


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
    # Process identity recorded at discovery time (F4)
    recorded_lstart: Optional[str] = None  # Full lstart (5 tokens)
    recorded_comm: Optional[str] = None  # Command name
    recorded_uid: Optional[int] = None  # User ID
    discovery_cwd: Optional[str] = None  # CWD at discovery time


@dataclass
class TeardownPlan:
    """A plan for tearing down a ticket's resources."""
    items: List[TeardownItem] = field(default_factory=list)
    in_flow: bool = False
    pr_signal_state: str = UNKNOWN  # MERGED, NOT_MERGED, or UNKNOWN
    pr_signal_detail: str = ""
    plan_hash: Optional[str] = None
    warnings: List[str] = field(default_factory=list)  # F22: Collect non-fatal issues

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "items": [asdict(item) for item in self.items],
            "in_flow": self.in_flow,
            "pr_signal_state": self.pr_signal_state,
            "pr_signal_detail": self.pr_signal_detail,
            "plan_hash": self.plan_hash,
            "warnings": self.warnings,
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
            warnings=data.get("warnings", []),
        )


def discover_ticket(
    target_worktree: Union[str, Path],
    in_flow: bool = False,
    pr_signal_runner: Optional[Callable] = None,
    ps_runner: Optional[Callable] = None,
    lsof_runner: Optional[Callable] = None,
    no_sizes: bool = False,
    get_idle_threshold: Optional[Callable[[], Optional[int]]] = None,
    tty_atime: Optional[Callable[[str], Optional[float]]] = None,
    check_fleet: bool = False
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
        check_fleet: If True, discover fleet supervisors for this repo

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
                # F18: Find descendants in full process table, not claude-only list
                descendants_result = procs.descendants(proc.pid, all_procs=None, runner=ps_runner)
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

                # F3, F4: Record full process identity at discovery time
                item = TeardownItem(
                    number=item_number,
                    kind="session",
                    group=1,
                    name=name,
                    path=cwd,
                    selectable=not reason_not_selectable,
                    reason=reason_not_selectable,
                    status="ok" if cwd else "unknown",
                    pids=[proc.pid] + [d.pid for d in descendants_list],
                    recorded_lstart=proc.lstart,  # F3: Full lstart with 5 tokens
                    recorded_comm=proc.comm,
                    recorded_uid=proc.uid,
                    discovery_cwd=cwd  # F4: Record discovery cwd
                )
                if should_preselect and not reason_not_selectable:
                    # Mark pre-selected via selectable flag (will be handled by ask logic)
                    pass
                plan.items.append(item)
                item_number += 1

    # 2. Discover worktrees (ticket, haiku-*, agent-*)
    if parent_path:
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

        # Check if caller chain is Unknown; if so, downgrade liveness to list-only
        if isinstance(caller_pids, Unknown):
            liveness = procs.UNKNOWN

        # Probe dirty/unpushed state for ticket worktree
        dirty_state, dirty_detail = cleanup.worktree_dirty_state(ticket_wt)
        dirty = dirty_state == "dirty"

        # Probe unpushed state (F13: pass tip and repo)
        try:
            tip = git.get_head_sha(cwd=target_path)
        except Exception:
            tip = "unknown"
        unpushed_state_val, unpushed_detail = cleanup.unpushed_state(tip, repo=target_path)
        unpushed = unpushed_state_val == cleanup.UNPUSHED

        # Build reason list for selectability
        reasons = []
        if liveness == procs.LIVE:
            reasons.append("has live session(s)")
        elif liveness == procs.UNKNOWN:
            reasons.append("unknown liveness")
        if dirty:
            reasons.append("has uncommitted changes")
        if unpushed:
            reasons.append("has unpushed commits")

        # Also mark non-selectable if dirty/unpushed state is UNKNOWN (can't verify)
        if dirty_state == "unknown":
            reasons.append("dirty state unknown")
        if unpushed_state_val == "unknown":
            reasons.append("unpushed state unknown")

        item = TeardownItem(
            number=item_number,
            kind="worktree",
            group=2,
            name=f"Ticket worktree ({ticket_wt.name})",
            path=str(ticket_wt),
            dirty=dirty,
            unpushed=unpushed,
            not_merged=(plan.pr_signal_state != MERGED),
            selectable=liveness != procs.LIVE and liveness != procs.UNKNOWN and not dirty and not unpushed and dirty_state != "unknown" and unpushed_state_val != "unknown",
            reason=" + ".join(reasons) if reasons else "",
            status="ok" if liveness != procs.UNKNOWN and dirty_state != "unknown" and unpushed_state_val != "unknown" else "unknown"
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

    # 4b. Discover fleet supervisors (if check_fleet is True)
    if check_fleet:
        try:
            project_root = worktrees.detect_project_root(cwd=target_path)
            if project_root:
                # Try to extract repo name for fleet lookup
                repo_name = Path(project_root).name
                fleet_tickets = procs.fleet_live_tickets(repo_name, ps_runner=ps_runner)
                if not isinstance(fleet_tickets, Unknown):
                    for issue_num, (state, detail, pid_started) in fleet_tickets.items():
                        item = TeardownItem(
                            number=item_number,
                            kind="fleet_supervisor",
                            group=4,
                            name=f"Fleet supervisor for issue #{issue_num} ({state})",
                            path=None,
                            selectable=False,
                            reason=f"fleet supervisor: {detail}",
                            status="ok"
                        )
                        plan.items.append(item)
                        item_number += 1
        except Exception:
            pass

    # Get PR signal for the ticket branch (F14: compute after PR signal is known)
    try:
        branch = git.get_current_branch(cwd=target_path)
        tip = git.get_head_sha(cwd=target_path)
        signal_state, merged_oid, detail = pr_merged_by_head(branch, tip, runner=pr_signal_runner)
        plan.pr_signal_state = signal_state
        plan.pr_signal_detail = detail
    except Exception as e:
        plan.pr_signal_state = UNKNOWN
        plan.pr_signal_detail = str(e)

    # F5: Compute plan hash EXCLUDING the hash field itself
    plan_dict = plan.to_dict()
    plan_dict_for_hash = {k: v for k, v in plan_dict.items() if k != "plan_hash"}
    plan.plan_hash = cleanup._hash_plan(json.dumps(plan_dict_for_hash, sort_keys=True, separators=(",", ":")))

    return plan


def _verify_and_signal_process(
    pid: int,
    recorded_lstart: Optional[str],
    recorded_comm: Optional[str],
    recorded_uid: Optional[int],
    discovery_cwd: Optional[str],
    sig: int,
    ps_runner: Optional[Callable] = None,
    lsof_runner: Optional[Callable] = None,
    kill_fn: Optional[Callable[[int, int], None]] = None,
    caller_pids: Optional[Union[List[int], Unknown]] = None,
) -> Tuple[bool, str]:
    """
    F32: Helper to verify process identity and send signal (duplicated block extracted).

    Returns (sent: bool, reason: str).
    """
    if not recorded_lstart or recorded_comm is None:
        return False, "missing recorded process identity"

    if recorded_uid is None:
        recorded_uid = os.getuid()

    # Verify using RECORDED identity
    is_valid = procs.verify_process(
        pid,
        recorded_lstart,
        recorded_comm,
        recorded_uid,
        ["/"],
        ps_runner=ps_runner,
        lsof_runner=lsof_runner,
        caller_pids=caller_pids if isinstance(caller_pids, list) else None,
        discovery_cwd=discovery_cwd
    )

    if not is_valid:
        return False, "process identity mismatch or in caller chain"

    # Send signal
    return procs.signal_process(
        pid,
        sig,
        recorded_lstart,
        recorded_comm,
        recorded_uid,
        ["/"],
        verify_runner=procs.verify_process,
        kill_runner=kill_fn,
        caller_pids=caller_pids if isinstance(caller_pids, list) else None,
        discovery_cwd=discovery_cwd
    )


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

    # F5: Verify plan hash EXCLUDING the hash field itself
    plan_dict = plan.to_dict()
    plan_dict_for_hash = {k: v for k, v in plan_dict.items() if k != "plan_hash"}
    current_hash = cleanup._hash_plan(json.dumps(plan_dict_for_hash, sort_keys=True, separators=(",", ":")))
    if not plan.plan_hash:
        # F5: Hard failure if hash is absent
        return {
            "success": False,
            "error": "Plan missing hash (not properly prepared)",
            "done": [],
            "skipped": [],
            "failed": [],
            "caller_cwd_removed": False,
            "bytes_freed": 0,
            "rss_freed": 0,
        }
    if plan.plan_hash != current_hash:
        # F5: Hard failure if hash doesn't match
        return {
            "success": False,
            "error": "Plan has been modified since discovery (hash mismatch)",
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

            # F4: Use recorded identity from discovery time, not live ps output
            if not item.recorded_lstart or item.recorded_comm is None:
                failed.append(f"Session PID {item.pids[0] if item.pids else '?'}: missing recorded process identity")
                continue

            # Signal descendants first, then the session
            for pid in reversed(item.pids):
                try:
                    # F32: Use extracted helper for verify-and-signal
                    sent, reason = _verify_and_signal_process(
                        pid,
                        item.recorded_lstart,
                        item.recorded_comm,
                        item.recorded_uid,
                        item.discovery_cwd,
                        signal.SIGTERM,
                        ps_runner=ps_runner,
                        lsof_runner=lsof_runner,
                        kill_fn=kill_fn,
                        caller_pids=caller_pids
                    )

                    if sent:
                        done.append(f"Signaled session PID {pid}")
                    else:
                        skipped.append(f"Could not signal session PID {pid}: {reason}")
                except TypeError as e:
                    # F22: Specific exception handling
                    failed.append(f"Type error signaling session PID {pid}: {e}")
                    plan.warnings.append(f"Type error signaling session PID {pid}: {e}")
                except OSError as e:
                    # F22: Specific exception handling
                    failed.append(f"OS error signaling session PID {pid}: {e}")
                    plan.warnings.append(f"OS error signaling session PID {pid}: {e}")
                except Exception as e:
                    # F22: Specific exception handling
                    failed.append(f"Unexpected error signaling session PID {pid}: {e}")
                    plan.warnings.append(f"Unexpected error signaling session PID {pid}: {e}")

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
        except (OSError, RuntimeError) as e:
            # F22: Specific exception handling
            worktree_records = []
            plan.warnings.append(f"Could not list worktrees: {e}")
        except Exception as e:
            # F22: Specific exception handling
            worktree_records = []
            plan.warnings.append(f"Unexpected error listing worktrees: {e}")

        # Track which worktrees were successfully removed (DECISION 3: gate branch deletion on worktree removal)
        successfully_removed_worktrees = set()
        attempted_worktrees: set = set()

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
                    # Validate target
                    if not item.path:
                        continue

                    # Detect primary worktree and compute trash root
                    primary_wt = None
                    try:
                        primary_wt = worktrees.detect_main_worktree()
                    except (OSError, RuntimeError):
                        pass
                    except Exception:
                        pass

                    worktrees_parent = str(Path(item.path).parent) if item.path else ""
                    trash_root = str(teardown_guard.trash_root_for(worktrees_parent)) if worktrees_parent else ""

                    ctx = teardown_guard.GuardContext(
                        worktrees_parent=worktrees_parent,
                        primary_worktree=primary_wt or "",
                        registered=[str(p) for p in [rec[0] for rec in worktree_records]],
                        plan_paths=[i.path for i in plan.items if i.path],
                        trash_root=trash_root
                    )

                    attempted_worktrees.add(item.path)
                    result = cleanup.remove_worktree(
                        item.path,
                        ctx,
                        confirmed,
                        caller_pids=caller_pids
                    )

                    if result.status == cleanup.REMOVED:
                        done.append(f"Removed {item.name}")
                        successfully_removed_worktrees.add(item.path)
                    elif result.status == cleanup.ALREADY_GONE:
                        done.append(f"Already removed {item.name}")
                        successfully_removed_worktrees.add(item.path)
                    else:
                        skipped.append(f"{item.name}: {result.reason}")
                except (OSError, RuntimeError) as e:
                    # F22: Specific exception handling
                    failed.append(f"{item.name}: {e}")
                    plan.warnings.append(f"Failed to remove {item.name}: {e}")
                except Exception as e:
                    # F22: Specific exception handling
                    failed.append(f"{item.name}: {e}")
                    plan.warnings.append(f"Unexpected error removing {item.name}: {e}")

            elif item.kind == "build_dir":
                # F10: Implement build_dir removal
                if item.path and item.selectable:
                    try:
                        rm_result = subprocess.run(
                            ["rm", "-rf", item.path],
                            capture_output=True,
                            text=True,
                            timeout=BUILD_DIR_REMOVAL_TIMEOUT_SECS
                        )
                        if rm_result.returncode == 0:
                            done.append(f"Removed build directory {item.path}")
                        else:
                            skipped.append(f"Could not remove build directory {item.path}: {rm_result.stderr}")
                    except subprocess.TimeoutExpired:
                        # F22: Specific exception handling
                        skipped.append(f"Build directory removal timed out: {item.path}")
                        plan.warnings.append(f"Build directory removal timed out: {item.path}")
                    except Exception as e:
                        # F22: Specific exception handling
                        failed.append(f"Error removing build directory {item.path}: {e}")
                        plan.warnings.append(f"Error removing build directory {item.path}: {e}")

            elif item.kind == "branch":
                # F10, DECISION 3: Branch deletion uses -d (not -D) unless MERGED, gate on worktree removal
                if item.path and item.selectable:
                    # DECISION 3: gate on worktree outcome. A worktree that was attempted but not
                    # removed (refused, failed, skipped) may still hold this branch, so leave every
                    # branch alone rather than delete underneath a live worktree.
                    unremoved = attempted_worktrees - successfully_removed_worktrees
                    if unremoved:
                        skipped.append(
                            f"Kept branch {item.path}: worktree removal did not complete "
                            f"({', '.join(sorted(unremoved))})"
                        )
                        continue
                    try:
                        # DECISION 3: Use -d unless PR was MERGED
                        force = (plan.pr_signal_state == MERGED)
                        success, err = git.delete_branch(item.path, force=force)
                        if success:
                            done.append(f"Deleted branch {item.path}")
                        elif err:
                            skipped.append(f"Could not delete branch {item.path}: {err}")
                    except (OSError, RuntimeError) as e:
                        # F22: Specific exception handling
                        failed.append(f"Error deleting branch {item.path}: {e}")
                        plan.warnings.append(f"Error deleting branch {item.path}: {e}")
                    except Exception as e:
                        # F22: Specific exception handling
                        failed.append(f"Unexpected error deleting branch {item.path}: {e}")
                        plan.warnings.append(f"Unexpected error deleting branch {item.path}: {e}")

        return {
            "success": len(failed) == 0,
            "done": done,
            "skipped": skipped,
            "failed": failed,
            "caller_cwd_removed": caller_cwd_removed,
            "bytes_freed": bytes_freed,
            "rss_freed": rss_freed,
        }
    else:
        # Should never reach here due to guard clause at line 778, but mypy requires it
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


def apply_sigkill(
    plan_json: str,
    pids: List[int],
    ps_runner: Optional[Callable] = None,
    lsof_runner: Optional[Callable] = None,
    kill_fn: Optional[Callable[[int, int], None]] = None,
    in_flight: bool = False,  # F9: Caller-provided but validated against plan
) -> Dict[str, Any]:
    """
    Apply SIGKILL to specified processes after re-verification.

    Args:
        plan_json: JSON string representation of TeardownPlan
        pids: List of pids to SIGKILL (F9: must be in verified plan)
        ps_runner: Injectable ps runner
        lsof_runner: Injectable lsof runner
        kill_fn: Injectable kill function
        in_flight: Caller argument (F9: ignored, derived from plan server-side)

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

    # F5: Verify plan hash EXCLUDING the hash field itself
    plan_dict = plan.to_dict()
    plan_dict_for_hash = {k: v for k, v in plan_dict.items() if k != "plan_hash"}
    current_hash = cleanup._hash_plan(json.dumps(plan_dict_for_hash, sort_keys=True, separators=(",", ":")))
    if plan.plan_hash and plan.plan_hash != current_hash:
        return {
            "success": False,
            "error": "Plan has been modified since discovery (hash mismatch)",
        }

    # F9: Require pids to be in the verified plan
    plan_pids = set()
    for item in plan.items:
        if item.kind == "session" and item.pids:
            plan_pids.update(item.pids)

    for pid in pids:
        if pid not in plan_pids:
            return {
                "success": False,
                "error": f"PID {pid} is not in the verified plan",
            }

    # F9: Derive in-flight flag server-side from plan (not from caller argument)
    in_flight_derived = any(item.kind == "session" for item in plan.items if any(p in pids for p in item.pids))

    if kill_fn is None:
        kill_fn = os.kill

    # Get caller chain for verification
    caller_chain_result = procs.caller_chain(runner=ps_runner)
    caller_pids: Union[List[int], Unknown] = Unknown("failed to get caller chain")
    if not isinstance(caller_chain_result, Unknown):
        caller_pids = [p.pid for p in caller_chain_result]

    signalled: List[str] = []
    failed: List[str] = []

    # F9: Find the session items corresponding to these pids
    session_items_by_pid = {}
    for item in plan.items:
        if item.kind == "session":
            for pid in item.pids:
                session_items_by_pid[pid] = item

    for pid in pids:
        try:
            # F4, F9: Use recorded identity from plan, not live ps output
            session_item = session_items_by_pid.get(pid)
            if not session_item:
                failed.append(f"Process {pid}: not found in plan")
                continue

            # F32: Use extracted helper for verify-and-signal
            sent, reason = _verify_and_signal_process(
                pid,
                session_item.recorded_lstart,
                session_item.recorded_comm,
                session_item.recorded_uid,
                session_item.discovery_cwd,
                signal.SIGKILL,
                ps_runner=ps_runner,
                lsof_runner=lsof_runner,
                kill_fn=kill_fn,
                caller_pids=caller_pids
            )

            if sent:
                signalled.append(f"SIGKILL sent to {pid}")
            else:
                failed.append(f"Could not send SIGKILL to {pid}: {reason}")
        except TypeError as e:
            # F22: Specific exception handling
            failed.append(f"Type error sending SIGKILL to {pid}: {e}")
        except OSError as e:
            # F22: Specific exception handling
            failed.append(f"OS error sending SIGKILL to {pid}: {e}")
        except Exception as e:
            # F22: Specific exception handling
            failed.append(f"Unexpected error sending SIGKILL to {pid}: {e}")

    return {
        "success": len(failed) == 0,
        "signalled": signalled,
        "failed": failed,
        "in_flight": in_flight_derived,
    }
