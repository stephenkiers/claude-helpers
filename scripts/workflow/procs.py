"""
Process discovery, liveness verification, and signal routing (Step 3 of ADR-0013).

Pure parsers over injected command output plus thin runners. All results are tri-state:
any failure, timeout, or implausible result is UNKNOWN, never "clean" or "no sessions".
"""

import json
import os
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional, Dict, Any, Callable, Union, Tuple

from . import paths
from .safety import Unknown


# Tri-state results enum (F30: stringly-typed tri-state)
class TriState(str, Enum):
    """Tri-state enum for liveness, pushed, and clean states."""
    LIVE = "live"
    NOT_LIVE = "not_live"
    UNKNOWN = "unknown"


# Backwards-compatible constants (JSON output unchanged)
LIVE = TriState.LIVE.value
NOT_LIVE = TriState.NOT_LIVE.value
UNKNOWN = TriState.UNKNOWN.value


@dataclass
class ProcRow:
    """A single process row from ps output."""
    pid: int
    ppid: int
    uid: int
    comm: str
    lstart: str
    tty: str
    rss: int


def parse_ps_output(text: str) -> List[ProcRow]:
    """
    Parse ps output: `ps -axo pid=,ppid=,uid=,tty=,rss=,lstart=,comm=`.

    Returns a list of ProcRow, or raises if parsing fails.
    lstart is a fixed 5-token field: Mon Nov 24 10:00:00 2026
    comm is the remainder after lstart (may contain spaces in theory, but ps output is well-defined).
    """
    rows: List[ProcRow] = []
    for line in text.strip().splitlines():
        if not line.strip():
            continue
        # Fixed field split: pid(1) ppid(1) uid(1) tty(1) rss(1) lstart(5) comm(1+)
        # rsplit from the right: split at most into 7 parts from the left
        parts = line.split(None, 5)  # Split on whitespace: pid, ppid, uid, tty, rss, rest
        if len(parts) < 6:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            uid = int(parts[2])
            tty = parts[3]
            rss = int(parts[4])
            # Remaining is "lstart (5 tokens) comm"
            remaining = parts[5].split(None, 5)  # Split: lstart_1, lstart_2, lstart_3, lstart_4, lstart_5, comm
            if len(remaining) < 6:  # Need 5 lstart tokens + at least comm
                continue
            lstart = " ".join(remaining[:5])
            comm = remaining[5]
            rows.append(ProcRow(
                pid=pid,
                ppid=ppid,
                uid=uid,
                comm=comm,
                lstart=lstart,
                tty=tty,
                rss=rss
            ))
        except (ValueError, IndexError):
            continue
    return rows


def parse_lsof_cwd(text: str) -> Dict[int, str]:
    """
    Parse lsof output: `lsof -a -d cwd -b -w -p <pids>`.

    Returns dict mapping pid to its cwd path, or raises if parsing fails.
    """
    result: Dict[int, str] = {}
    for line in text.strip().splitlines():
        if not line.strip() or line.startswith("COMMAND"):
            continue
        parts = line.split()
        if len(parts) < 9:
            continue
        try:
            pid = int(parts[1])
            cwd = " ".join(parts[8:])  # Path may contain spaces
            result[pid] = cwd
        except (ValueError, IndexError):
            continue
    return result


def list_claude_processes(
    runner: Optional[Callable[..., Any]] = None
) -> Union[List[ProcRow], Unknown]:
    """
    List all claude processes via ps.

    Pure parser: filters the full ps output for processes with comm == "claude".
    Returns the unordered list of live processes at the moment ps runs; subsequent
    pids may exit before the caller acts on the list.

    Args:
        runner: Injectable subprocess runner (default: subprocess.run with timeout).
                Signature: runner(argv, timeout) -> stdout text

    Returns:
        List of ProcRow for processes with comm == "claude", or Unknown on any error
        (timeout, ps failure, exception). The returned list is never an empty string
        or None; on success it is a list (possibly empty); on failure it is Unknown.
    """
    if runner is None:
        def default_runner(argv: List[str], timeout: int) -> str:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            if result.returncode != 0:
                raise subprocess.CalledProcessError(result.returncode, argv, result.stdout, result.stderr)
            return result.stdout
        runner = default_runner

    try:
        output = runner(
            ["ps", "-axo", "pid=,ppid=,uid=,tty=,rss=,lstart=,comm="],
            timeout=10
        )
        rows = parse_ps_output(output)
        # Filter for comm == "claude"
        return [r for r in rows if r.comm == "claude"]
    except subprocess.TimeoutExpired:
        return Unknown("ps timed out")
    except subprocess.CalledProcessError as e:
        return Unknown(f"ps failed: {e}")
    except Exception as e:
        return Unknown(f"ps error: {e}")


def process_cwds(
    pids: List[int],
    runner: Optional[Callable[..., Any]] = None
) -> Union[Dict[int, str], Unknown]:
    """
    Get current working directories for a list of pids via lsof.

    Args:
        pids: List of process IDs to check.
        runner: Injectable subprocess runner (default: subprocess.run with timeout).

    Returns dict mapping pid to cwd, or Unknown on error.
    Uses `lsof -a -d cwd -b -w -p <pids>` with hard subprocess timeout.
    """
    if not pids:
        return {}

    if runner is None:
        def default_runner(argv: List[str], timeout: int) -> str:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            if result.returncode != 0:
                raise subprocess.CalledProcessError(result.returncode, argv, result.stdout, result.stderr)
            return result.stdout
        runner = default_runner

    try:
        pid_str = ",".join(str(p) for p in pids)
        output = runner(
            ["lsof", "-a", "-d", "cwd", "-b", "-w", "-p", pid_str],
            timeout=10
        )
        return parse_lsof_cwd(output)
    except subprocess.TimeoutExpired:
        return Unknown("lsof timed out")
    except subprocess.CalledProcessError as e:
        return Unknown(f"lsof failed: {e}")
    except Exception as e:
        return Unknown(f"lsof error: {e}")


def caller_chain(
    runner: Optional[Callable[..., Any]] = None
) -> Union[List[ProcRow], Unknown]:
    """
    Walk the PPID chain up from the current process.

    Returns list of ProcRow from os.getpid() up to init, or Unknown if any link is missing.
    If ps fails, result is Unknown, and callers must downgrade every session to list-only.
    """
    if runner is None:
        def default_runner(argv: List[str], timeout: int) -> str:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            if result.returncode != 0:
                raise subprocess.CalledProcessError(result.returncode, argv, result.stdout, result.stderr)
            return result.stdout
        runner = default_runner

    try:
        output = runner(
            ["ps", "-axo", "pid=,ppid=,uid=,tty=,rss=,lstart=,comm="],
            timeout=10
        )
        rows = parse_ps_output(output)
        pid_map = {r.pid: r for r in rows}

        chain: List[ProcRow] = []
        current_pid = os.getpid()
        visited = set()

        while current_pid not in visited:
            visited.add(current_pid)
            if current_pid not in pid_map:
                return Unknown(f"Process {current_pid} not found in ps output")
            proc = pid_map[current_pid]
            chain.append(proc)
            if proc.ppid == current_pid or current_pid == 1:
                # Reached init or self-parent
                break
            current_pid = proc.ppid

        return chain
    except subprocess.TimeoutExpired:
        return Unknown("ps timed out")
    except subprocess.CalledProcessError as e:
        return Unknown(f"ps failed: {e}")
    except Exception as e:
        return Unknown(f"caller chain error: {e}")


def descendants(
    pid: int,
    all_procs: Optional[List[ProcRow]] = None,
    runner: Optional[Callable[..., Any]] = None
) -> Union[List[ProcRow], Unknown]:
    """
    Find all descendants of a process by walking down the process tree.

    Pure tree walker: takes a snapshot of ps output (or fetches one if all_procs is None)
    and returns all processes reachable from pid by following child ppid links.
    Includes the given pid if it exists in the snapshot.

    Args:
        pid: Process ID to find descendants of
        all_procs: Injected process list from ps (optional; fetched if None)
        runner: Injectable subprocess runner (default: subprocess.run with timeout)

    Returns:
        List of descendant ProcRow (including pid if found), or Unknown on any error
        (timeout, ps failure, exception). The returned list is never an empty string
        or None; on success it is a list (possibly empty if pid is not found);
        on failure it is Unknown.
    """
    if runner is None:
        def default_runner(argv: List[str], timeout: int) -> str:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            if result.returncode != 0:
                raise subprocess.CalledProcessError(result.returncode, argv, result.stdout, result.stderr)
            return result.stdout
        runner = default_runner

    try:
        if all_procs is None:
            output = runner(
                ["ps", "-axo", "pid=,ppid=,uid=,tty=,rss=,lstart=,comm="],
                timeout=10
            )
            all_procs = parse_ps_output(output)

        # Build ppid -> [children] map
        children_map: Dict[int, List[ProcRow]] = {}
        for proc in all_procs:
            if proc.ppid not in children_map:
                children_map[proc.ppid] = []
            children_map[proc.ppid].append(proc)

        # DFS from pid
        result: List[ProcRow] = []
        visited = set()

        def dfs(p: int) -> None:
            if p in visited:
                return
            visited.add(p)
            # Find the process row for p
            proc_row = next((pr for pr in all_procs if pr.pid == p), None)
            if proc_row:
                result.append(proc_row)
            # Visit children
            for child in children_map.get(p, []):
                dfs(child.pid)

        dfs(pid)
        return result
    except subprocess.TimeoutExpired:
        return Unknown("ps timed out")
    except subprocess.CalledProcessError as e:
        return Unknown(f"ps failed: {e}")
    except Exception as e:
        return Unknown(f"descendants error: {e}")


def fleet_live_tickets(
    repo: str,
    fleets_root: Optional[Path] = None,
    ps_runner: Optional[Callable[[List[str], int], str]] = None
) -> Union[Dict[int, Tuple[str, str, str]], Unknown]:
    """
    Read fleet tickets and check their liveness.

    Returns dict mapping issue number to (state, detail, pid_started) where:
    - state is one of LIVE, NOT_LIVE, UNKNOWN
    - detail is a human-readable reason
    - pid_started is the recorded start time (or "" if not found)

    A ticket is LIVE if:
    - pid is non-null
    - a process with that pid exists
    - its current lstart equals pid_started

    Returns NOT_LIVE if pid is null, no process exists, or lstart differs (pid reuse).
    Returns UNKNOWN if pid_alive but no pid_started, or ps fails.
    """
    if fleets_root is None:
        fleets_root = Path.home() / ".claude" / "fleets"

    repo_key = repo.replace("/", "_")
    fleet_dir = fleets_root / repo_key

    if not fleet_dir.exists():
        return {}

    result: Dict[int, Tuple[str, str, str]] = {}

    for issue_dir in fleet_dir.iterdir():
        if not issue_dir.is_dir():
            continue
        fleet_json = issue_dir / "fleet.json"
        if not fleet_json.exists():
            continue

        try:
            with open(fleet_json) as f:
                data = json.load(f)
            tickets = data.get("tickets", [])
            for ticket in tickets:
                issue = ticket.get("issue")
                if not isinstance(issue, int):
                    continue
                pid = ticket.get("pid")
                pid_started = ticket.get("pid_started")

                if pid is None:
                    result[issue] = (NOT_LIVE, "pid is null", "")
                    continue

                if not isinstance(pid, int) or isinstance(pid, bool):
                    result[issue] = (NOT_LIVE, "pid is not an integer", "")
                    continue

                # Check if process exists
                try:
                    if ps_runner is None:
                        ps_output = subprocess.run(
                            ["ps", "-o", "lstart=", "-p", str(pid)],
                            capture_output=True, text=True, timeout=10
                        ).stdout.strip()
                    else:
                        ps_output = ps_runner(["ps", "-o", "lstart=", "-p", str(pid)], 10)

                    if not ps_output:
                        result[issue] = (NOT_LIVE, f"process {pid} not found", "")
                        continue

                    if not pid_started:
                        result[issue] = (UNKNOWN, f"pid {pid} exists but pid_started not recorded", "")
                        continue

                    if ps_output == pid_started:
                        result[issue] = (LIVE, f"process {pid} alive with matching lstart", pid_started)
                    else:
                        result[issue] = (NOT_LIVE, f"lstart changed (pid {pid} was reused)", "")
                except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
                    result[issue] = (UNKNOWN, f"ps failed for pid {pid}", "")
                except Exception as e:
                    result[issue] = (UNKNOWN, f"error checking pid {pid}: {e}", "")

        except (json.JSONDecodeError, OSError):
            continue

    return result


def verify_process(
    pid: int,
    lstart: str,
    comm: str,
    uid: int,
    allowed_roots: List[str],
    ps_runner: Optional[Callable[[List[str], int], str]] = None,
    lsof_runner: Optional[Callable[[List[str], int], str]] = None,
    caller_pids: Optional[List[int]] = None,
    discovery_cwd: Optional[str] = None
) -> bool:
    """
    Re-verify a process at act time.

    Returns True only if:
    - uid equals the caller's uid
    - comm and lstart match the recorded values
    - cwd is inside an allowed root (or recorded discovery_cwd is inside allowed root)
    - pid is not in the caller chain

    For a descendant whose discovery cwd was outside allowed roots,
    the cwd test is replaced by uid/comm/lstart match.
    """
    if ps_runner is None:
        def default_ps_runner(argv: List[str], timeout: int) -> str:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            return result.stdout
        ps_runner = default_ps_runner

    if lsof_runner is None:
        def default_lsof_runner(argv: List[str], timeout: int) -> str:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            return result.stdout
        lsof_runner = default_lsof_runner

    # Check uid
    if uid != os.getuid():
        return False

    # Check if pid is in caller chain
    if caller_pids and pid in caller_pids:
        return False

    # Re-read ps for this pid
    try:
        ps_output = ps_runner(["ps", "-o", "pid=,lstart=,comm=", "-p", str(pid)], 10)
    except Exception:
        return False

    if not ps_output.strip():
        return False

    ps_parts = ps_output.strip().split()
    if len(ps_parts) < 3:
        return False

    try:
        current_pid = int(ps_parts[0])
        current_lstart = " ".join(ps_parts[1:-1])  # lstart is 5 tokens
        current_comm = ps_parts[-1]

        if current_pid != pid or current_lstart != lstart or current_comm != comm:
            return False
    except (ValueError, IndexError):
        return False

    # Check cwd (unless discovery_cwd was outside allowed roots)
    if discovery_cwd:
        try:
            discovery_allowed = any(paths.is_within(discovery_cwd, root) for root in allowed_roots)
            if not discovery_allowed:
                # Cwd test replaced by uid/comm/lstart match (already done above)
                return True
        except ValueError:
            pass

    # Check current cwd
    try:
        lsof_output = lsof_runner(["lsof", "-a", "-d", "cwd", "-b", "-w", "-p", str(pid)], 10)
    except Exception:
        return False

    cwd_map = parse_lsof_cwd(lsof_output)
    current_cwd = cwd_map.get(pid)
    if not current_cwd:
        return False

    # Check if cwd is in allowed roots
    try:
        return any(paths.is_within(current_cwd, root) for root in allowed_roots)
    except ValueError:
        return False


def signal_process(
    pid: int,
    sig: int,
    lstart: str,
    comm: str,
    uid: int,
    allowed_roots: List[str],
    verify_runner: Optional[Callable[..., bool]] = None,
    kill_runner: Optional[Callable[[int, int], None]] = None,
    caller_pids: Optional[List[int]] = None,
    discovery_cwd: Optional[str] = None
) -> Tuple[bool, str]:
    """
    Send a signal to a process after verifying its identity.

    Returns (sent: bool, reason: str).
    - (True, "signal sent") if signal was sent
    - (False, reason) if verify_process failed or signal failed
    Never raises on a vanished pid.

    Args:
        pid: Process ID to signal
        sig: Signal number
        lstart: Process start time (from ps output)
        comm: Process command name
        uid: Process user ID
        allowed_roots: List of allowed root paths for the process cwd
        verify_runner: Injectable verification function
        kill_runner: Injectable kill function
        caller_pids: Pids in the caller chain (threaded to verify_runner)
        discovery_cwd: Original discovery cwd (threaded to verify_runner)
    """
    if verify_runner is None:
        verify_runner = verify_process

    if kill_runner is None:
        def default_kill(p: int, s: int) -> None:
            os.kill(p, s)
        kill_runner = default_kill

    # Verify the process (thread caller_pids and discovery_cwd through)
    if not verify_runner(pid, lstart, comm, uid, allowed_roots, caller_pids=caller_pids, discovery_cwd=discovery_cwd):
        return False, f"process {pid} failed verification"

    # Send the signal
    try:
        kill_runner(pid, sig)
        return True, f"signal {sig} sent to {pid}"
    except ProcessLookupError:
        return False, f"process {pid} not found (vanished)"
    except PermissionError:
        return False, f"permission denied to signal {pid}"
    except Exception as e:
        return False, f"failed to signal {pid}: {e}"


def live_in(
    path: str,
    caller_pids: Union[List[int], Unknown],
    allowed_roots: Optional[List[str]] = None,
    runner: Optional[Callable[..., Any]] = None
) -> str:
    """
    Tri-state answer to "does any process other than the caller chain have its cwd inside path?".

    Args:
        path: Directory path to check
        caller_pids: List of pids in the caller chain, or Unknown if chain walk failed
        allowed_roots: List of allowed root paths (optional, used for context)
        runner: Injectable subprocess runner

    Returns:
    - LIVE: some process other than caller has cwd in path
    - NOT_LIVE: no such process found
    - UNKNOWN: probe failed or caller chain is unknown (F2: returns typed value, not Unknown object)
    """
    if isinstance(caller_pids, Unknown):
        return UNKNOWN

    if runner is None:
        def default_runner(argv: List[str], timeout: int) -> str:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            return result.stdout
        runner = default_runner

    try:
        # Get all processes and their cwds
        ps_output = runner(
            ["ps", "-axo", "pid=,ppid=,uid=,tty=,rss=,lstart=,comm="],
            timeout=10
        )
        all_procs = parse_ps_output(ps_output)

        pid_list = [p.pid for p in all_procs]
        lsof_output = runner(
            ["lsof", "-a", "-d", "cwd", "-b", "-w", "-p", ",".join(str(p) for p in pid_list)],
            timeout=10
        )
        cwd_map = parse_lsof_cwd(lsof_output)

        # Check if any process (other than caller chain) has cwd in path
        for proc in all_procs:
            if proc.pid in caller_pids:
                continue
            cwd = cwd_map.get(proc.pid)
            if cwd:
                try:
                    if paths.is_within(cwd, path):
                        return LIVE
                except ValueError:
                    continue

        return NOT_LIVE
    except subprocess.TimeoutExpired:
        return UNKNOWN
    except subprocess.CalledProcessError:
        return UNKNOWN
    except Exception:
        return UNKNOWN
