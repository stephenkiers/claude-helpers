"""
The fleet (PLAN.md section 10): run several tickets at once, at most `max` actively running.

A fleet is a small JSON file plus one detached supervisor process. The supervisor launches
`run`/`resume` children (each its own `flow3` process holding its flow's run lock) and nothing else:
it never answers a question, never decides a merge, and never edits a flow. A ticket that parks for the
human is not "running", so it frees its slot while the others continue.

Spec syntax: `12,13,14` (independent) and `12>13>14` (13 starts only once 12 is done), mixed freely
with commas: `12>13,14`.
"""

import errno
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from scripts.flow3 import inbox
from scripts.flow3.paths import flow_home, repo_dir
from scripts.flow3.state import FlowDir
from scripts.workflow.fsutil import LockHeld, atomic_write_text, nonblocking_flock

# Seconds between supervisor passes. FLOW3_FLEET_POLL overrides it (a test seam; not for production).
POLL_SECS = 5.0
MAX_LAUNCHES = 3
CHECKOUT_ROOT = Path(__file__).resolve().parent.parent.parent


class FleetError(Exception):
    """A bad fleet spec, a missing fleet, or a refused start."""


@dataclass
class Ticket:
    issue: int
    after: List[int] = field(default_factory=list)


def parse_specs(text: str) -> List[Ticket]:
    """`12,13>14` -> tickets 12, 13, and 14 (after 13). Raises FleetError on a bad or cyclic spec."""
    order: List[int] = []
    after: Dict[int, List[int]] = {}
    for group in [g.strip() for g in text.replace(" ", "").split(",")]:
        if not group:
            raise FleetError("empty ticket in %r" % text)
        prev: Optional[int] = None
        for part in group.split(">"):
            part = part.lstrip("#")
            if not part.isdigit() or int(part) < 1:
                raise FleetError("%r is not an issue number (in %r)" % (part, text))
            num = int(part)
            if num not in after:
                after[num] = []
                order.append(num)
            if prev is not None:
                if prev == num:
                    raise FleetError("#%d cannot wait for itself" % num)
                if prev not in after[num]:
                    after[num].append(prev)
            prev = num
    # a cycle shows as a ticket that can never become ready
    ready: List[int] = []
    left = list(order)
    while left:
        step = [n for n in left if all(a in ready for a in after[n])]
        if not step:
            raise FleetError("the ordering has a cycle among %s" % ", ".join("#%d" % n for n in left))
        ready += step
        left = [n for n in left if n not in step]
    return [Ticket(n, list(after[n])) for n in order]


# -- the fleet file --------------------------------------------------------------------------

def _poll() -> float:
    raw = os.environ.get("FLOW3_FLEET_POLL")
    try:
        return float(raw) if raw else POLL_SECS
    except ValueError:
        return POLL_SECS


def fleets_dir(repo: str) -> Path:
    path = repo_dir(repo) / "fleets"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _fleet_path(repo: str, fleet_id: str) -> Path:
    return fleets_dir(repo) / ("%s.json" % fleet_id)


def load_fleet(fleet_id: str) -> Dict[str, Any]:
    """Find a fleet by id across every repo. Raises FleetError when there is none."""
    for rdir in sorted(p for p in flow_home().iterdir() if p.is_dir()):
        path = rdir / "fleets" / ("%s.json" % fleet_id)
        if path.is_file():
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                raise FleetError("fleet file %s is not valid JSON" % path)
            if isinstance(doc, dict):
                return doc
    raise FleetError("no fleet %r" % fleet_id)


def list_fleets(repo: Optional[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    rdirs = [repo_dir(repo)] if repo else sorted(p for p in flow_home().iterdir() if p.is_dir())
    for rdir in rdirs:
        for path in sorted((rdir / "fleets").glob("*.json")) if (rdir / "fleets").is_dir() else []:
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if isinstance(doc, dict):
                out.append(doc)
    return out


def _save(doc: Dict[str, Any]) -> None:
    atomic_write_text(_fleet_path(str(doc["repo"]), str(doc["id"])), json.dumps(doc, indent=2, sort_keys=True) + "\n")


# -- launching -------------------------------------------------------------------------------

def launch_detached(args: List[str], log_path: Path) -> int:
    """Start `python3 -m scripts.flow3 <args>` in its own session, output appended to log_path."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(log_path), "ab") as log:
        proc = subprocess.Popen([sys.executable, "-m", "scripts.flow3"] + args, cwd=str(CHECKOUT_ROOT),
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    return proc.pid


def _pid_alive(pid: int) -> bool:
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)  # reaps our own finished child
        if done == pid:
            return False
    except ChildProcessError:
        pass
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def start(repo: str, repo_root: Path, tickets: List[Ticket], max_parallel: int,
          launch: Optional[Callable[[List[str], Path], int]] = None) -> str:
    """Write the fleet file and start its supervisor. Returns the fleet id."""
    if max_parallel < 1:
        raise FleetError("--max must be at least 1")
    fleet_id = "%s-%s" % (datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:4])
    doc: Dict[str, Any] = {
        "id": fleet_id, "repo": repo, "repo_root": str(repo_root), "max": max_parallel,
        "created": datetime.now(timezone.utc).isoformat(),
        "tickets": [{"issue": t.issue, "after": t.after} for t in tickets], "launched": {},
    }
    _save(doc)
    (launch or launch_detached)(["fleet", "supervise", fleet_id], fleets_dir(repo) / ("%s.log" % fleet_id))
    return fleet_id


# -- supervising -----------------------------------------------------------------------------

def _flow_for(doc: Dict[str, Any], issue: int) -> Optional[FlowDir]:
    return FlowDir.find(str(doc["repo"]), issue)


def _status_of(doc: Dict[str, Any], issue: int) -> str:
    flow = _flow_for(doc, issue)
    return flow.flow_state().status if flow else "not_started"


def classify(doc: Dict[str, Any]) -> Dict[int, str]:
    """
    Each ticket's fleet state: done, active (a process is driving it), parked (waiting for the human or at
    the merge gate), stopped, blocked (behind a stopped ticket), waiting (behind an unfinished one),
    ready (can launch now).
    """
    launched = {int(k): int(v) for k, v in (doc.get("launched") or {}).items()}
    base: Dict[int, str] = {}
    for t in doc["tickets"]:
        issue = int(t["issue"])
        flow = _flow_for(doc, issue)
        if flow is None:
            base[issue] = "active" if issue in launched and _pid_alive(launched[issue]) else "fresh"
            continue
        status = flow.flow_state().status
        alive = inbox.is_running(flow) or (issue in launched and _pid_alive(launched[issue]))
        if status == "done":
            base[issue] = "done"
        elif status in ("waiting_human", "merge_gate"):
            base[issue] = "active" if alive else "parked"
        elif status == "stopped":
            base[issue] = "active" if alive else "stopped"
        else:
            base[issue] = "active" if alive else "fresh"  # running status, no live process: needs (re)launch
    deps = {int(t["issue"]): [int(x) for x in t["after"]] for t in doc["tickets"]}
    attempts = doc.get("attempts") or {}
    for issue, st in list(base.items()):
        if st == "fresh" and int(attempts.get(str(issue)) or 0) >= MAX_LAUNCHES:
            base[issue] = "stopped"  # launched repeatedly and never got going: a human looks at its log
    out = dict(base)
    changed = True
    while changed:
        changed = False
        for issue, st in list(out.items()):
            if st != "fresh":
                continue
            ds = [out.get(d, "done") for d in deps[issue]]
            if any(d in ("stopped", "blocked") for d in ds):
                out[issue] = "blocked"
                changed = True
            elif any(d != "done" for d in ds):
                out[issue] = "waiting"
                changed = True
    return {issue: ("ready" if st == "fresh" else st) for issue, st in out.items()}


def supervise(fleet_id: str, launch: Optional[Callable[[List[str], Path], int]] = None,
              sleep: Callable[[float], None] = time.sleep) -> int:
    """
    Run the fleet until every ticket has been launched and has finished its time as a running process,
    or nothing can move without the human. Returns 0. A second supervisor for the same fleet exits 0.
    """
    doc = load_fleet(fleet_id)
    repo = str(doc["repo"])
    do_launch = launch or launch_detached
    try:
        with nonblocking_flock(fleets_dir(repo) / ("%s.lock" % fleet_id)):
            return _supervise_locked(doc, do_launch, sleep)
    except LockHeld:
        print("fleet %s already has a supervisor" % fleet_id)
        return 0


def _supervise_locked(doc: Dict[str, Any], do_launch: Callable[[List[str], Path], int],
                      sleep: Callable[[float], None]) -> int:
    repo = str(doc["repo"])
    root = str(doc["repo_root"])
    cap = int(doc["max"])
    launched: Dict[str, int] = {str(k): int(v) for k, v in (doc.get("launched") or {}).items()}
    while True:
        doc["launched"] = launched
        states = classify(doc)
        active = [i for i, s in states.items() if s == "active"]
        ready = [t for t in doc["tickets"] if states[int(t["issue"])] == "ready"]
        room = cap - len(active)
        for t in ready[:max(0, room)]:
            issue = int(t["issue"])
            flow = _flow_for(doc, issue)
            if flow is None:
                args = ["run", str(issue), "--repo-root", root, "--repo", repo]
            else:
                args = ["resume", repo, str(issue)]
            attempts = doc.setdefault("attempts", {})
            attempts[str(issue)] = int(attempts.get(str(issue)) or 0) + 1
            launched[str(issue)] = do_launch(args, fleets_dir(repo) / ("%s-%d.log" % (doc["id"], issue)))
            doc["launched"] = launched
            _save(doc)
        states = classify(doc)
        # Stay while anything can still move: a process is driving, a ticket can launch, a ticket waits for
        # the human (parked), or one waits on those. Stopped and blocked tickets need a human restart.
        if any(s in ("active", "ready", "parked", "waiting") for s in states.values()):
            sleep(_poll())
            continue
        return 0


# -- status ----------------------------------------------------------------------------------

def status_text(doc: Dict[str, Any]) -> str:
    states = classify(doc)
    lines = ["FLEET %s  repo %s  max %s" % (doc["id"], doc["repo"], doc["max"])]
    for t in doc["tickets"]:
        issue = int(t["issue"])
        flow = _flow_for(doc, issue)
        extra = ""
        if flow is not None:
            row = inbox.flow_row(flow)
            extra = "  stage %s, flow %s, $%.2f" % (row["stage"], row["status"], row["cost_usd"])
        wait = "  after %s" % ", ".join("#%d" % a for a in t["after"]) if t["after"] else ""
        lines.append("  #%-6d %-9s%s%s" % (issue, states[issue], wait, extra))
        if states[issue] == "stopped" and flow is None:
            lines.append("          never started; see the fleet log next to the fleet file")
    return "\n".join(lines) + "\n"
