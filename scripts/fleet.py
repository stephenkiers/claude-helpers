#!/usr/bin/env python3
"""Dispatcher mechanics for /expert-fleet: fleet state, background ticket runs, status, inbox.

The fleet session (the model running /expert-fleet) decides; this script does the bookkeeping that
must be deterministic. Each ticket runs as its own headless `claude -p` process driving
`/expert-flow <N> --fleet`; a ticket that needs a human parks by writing `fleet-ask.json` into its
flow directory and exiting. This script never answers a question and never edits a flow's files:
it only reads `~/.claude/flows/` and writes under the fleet directory.

Usage:
  fleet.py init --repo OWNER/NAME --main-worktree DIR --epic URL --tickets FILE [--max K]
                [--permission-mode MODE]                         -> prints FLEET_DIR
  fleet.py status FLEET_DIR [--json]                             -> one row per ticket
  fleet.py ready FLEET_DIR                                       -> issues to launch now, one per line
  fleet.py inbox FLEET_DIR                                       -> JSON list of open parked questions
  fleet.py run FLEET_DIR ISSUE [--message-file FILE]             -> blocking; run it in the background
  fleet.py wait RECEIPT [--timeout SECS]                         -> blocks until the receipt settles

`wait` is for the ticket orchestrator, not the fleet session. A headless `claude -p` session aborts
its background agents about ten minutes after its own turn goes idle, so in fleet mode the
orchestrator never idles while a step agent runs: after every step launch or `SendMessage` it calls
`wait` in the foreground (repeatedly, each call under the Bash tool's 600s cap). `wait` returns
`settled` once the receipt has changed since the call started and carries a `Decision:` line or an
awaiting marker, else `timeout`.

`run` without a message starts or continues the ticket's flow (fresh run from the main worktree,
then the post-hand-off `--resume` inside the ticket worktree). With `--message-file` it resumes the
ticket's last Claude session in the same cwd with that message (an answer to a parked question).

Ticket states (derived, never stored): running, pending, blocked, handoff, parked, held, done,
failed, stalled (flow mid-step but no process), stopped (paused for a human outside the fleet).
"""
import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

FLEET_ROOT = Path.home() / ".claude" / "fleets"
FLOWS_ROOT = Path.home() / ".claude" / "flows"
LAUNCHABLE = ("pending", "handoff")
SETTLED = ("held", "done", "failed", "stopped")


def now_iso() -> str:
    """Current UTC time as ISO-8601 with a Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def repo_key(repo: str) -> str:
    """Flow-store key for OWNER/NAME, as /expert-flow builds it."""
    return repo.replace("/", "_")


# ---------------------------------------------------------------------------------------------
# fleet.json (read-modify-write under an exclusive lock; concurrent `run` processes share it)
# ---------------------------------------------------------------------------------------------

def _lock_path(fleet_dir: Path) -> Path:
    return fleet_dir / ".fleet.lock"


def load_fleet(fleet_dir: Path) -> Dict[str, Any]:
    """Read fleet.json."""
    with open(fleet_dir / "fleet.json") as f:
        data: Dict[str, Any] = json.load(f)
    return data


def _write_fleet(fleet_dir: Path, data: Dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(fleet_dir), prefix=".fleet.", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, fleet_dir / "fleet.json")


def update_fleet(fleet_dir: Path, fn: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    """Apply fn to fleet.json under the fleet lock and write it back atomically."""
    with open(_lock_path(fleet_dir), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = load_fleet(fleet_dir)
        fn(data)
        data["updated"] = now_iso()
        _write_fleet(fleet_dir, data)
        return data


def ticket(data: Dict[str, Any], issue: int) -> Dict[str, Any]:
    """The fleet.json entry for an issue, or SystemExit if it is not in the fleet."""
    for t in data["tickets"]:
        if t["issue"] == issue:
            return t  # type: ignore[no-any-return]
    raise SystemExit(f"ERROR: #{issue} is not in this fleet")


# ---------------------------------------------------------------------------------------------
# Flow discovery (mirrors /expert-flow's shared newest-flow lookup)
# ---------------------------------------------------------------------------------------------

def find_flow(repo: str, issue: int, flows_root: Optional[Path] = None) -> Optional[Path]:
    """Newest flow dir for the issue: highest state.json `updated`, ties by directory name."""
    root = (flows_root or FLOWS_ROOT) / repo_key(repo)
    best: Optional[Tuple[str, str, Path]] = None
    if not root.is_dir():
        return None
    for d in root.glob(f"{issue}-*"):
        st = read_json(d / "state.json")
        if not st or str(st.get("issue")) != str(issue):
            continue
        key = (str(st.get("updated") or ""), d.name, d)
        if best is None or key[:2] > best[:2]:
            best = key
    return best[2] if best else None


def read_json(path: Path) -> Optional[Dict[str, Any]]:
    """Parse a JSON object file; None when missing, unreadable, or not an object."""
    try:
        with open(path) as f:
            v = json.load(f)
    except (OSError, ValueError):
        return None
    return v if isinstance(v, dict) else None


def is_handoff(st: Dict[str, Any]) -> bool:
    """The exact post-worktree hand-off state /expert-flow's bare run resumes silently."""
    return (st.get("step") == "plan" and st.get("status") == "paused" and st.get("live") is None
            and int((st.get("attempts") or {}).get("plan") or 0) == 0)


def pid_alive(pid: Optional[int]) -> bool:
    """True when a process with this pid exists."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def open_ask(flow_dir: Path) -> Optional[Dict[str, Any]]:
    """The flow's parked question, when one is open."""
    ask = read_json(flow_dir / "fleet-ask.json")
    return ask if ask and ask.get("status") == "open" else None


def flow_status(t: Dict[str, Any], flow_dir: Optional[Path]) -> str:
    """Fleet-level state of a ticket from its process and its flow's state.json (deps aside)."""
    if pid_alive(t.get("pid")):
        return "running"
    if flow_dir is None:
        return "pending"
    st = read_json(flow_dir / "state.json") or {}
    status = st.get("status")
    if status == "done":
        return "done"
    if open_ask(flow_dir):
        return "parked"
    if status == "failed":
        return "failed"
    if status == "paused" and st.get("step") == "merge" and st.get("pr"):
        return "held"
    if is_handoff(st):
        return "handoff"
    if status in ("running", "awaiting"):
        return "stalled"
    return "stopped"


# ---------------------------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------------------------

def parse_dep(dep: Any, repo: str) -> Tuple[str, int]:
    """A dependency as (repo, issue): an int is in-repo; 'owner/name#N' or '#N' strings also work."""
    if isinstance(dep, int):
        return repo, dep
    s = str(dep).strip()
    if "#" in s:
        r, _, n = s.rpartition("#")
        return (r or repo), int(n)
    return repo, int(s)


def gh_issue_closed(repo: str, issue: int) -> bool:
    """True when GitHub reports the issue CLOSED; any lookup failure counts as not closed."""
    try:
        out = subprocess.run(["gh", "issue", "view", str(issue), "-R", repo, "--json", "state", "-q", ".state"],
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0 and out.stdout.strip() == "CLOSED"


def unmet_deps(t: Dict[str, Any], data: Dict[str, Any], states: Dict[int, str],
               closed: Callable[[str, int], bool]) -> List[str]:
    """Dependencies not yet satisfied: an in-fleet dep must be done; any dep may be closed on GitHub."""
    repo = data["repo"]
    unmet = []
    for dep in t.get("deps") or []:
        r, n = parse_dep(dep, repo)
        if r == repo and states.get(n) == "done":
            continue
        if closed(r, n):
            continue
        unmet.append(f"#{n}" if r == repo else f"{r}#{n}")
    return unmet


def compute(data: Dict[str, Any], flows_root: Optional[Path] = None,
            closed: Optional[Callable[[str, int], bool]] = None) -> List[Dict[str, Any]]:
    """One row per ticket: issue, title, state, step, pr, flow_dir, unmet deps, ask kind."""
    closed_fn = closed or gh_issue_closed
    cache: Dict[Tuple[str, int], bool] = {}

    def closed_cached(r: str, n: int) -> bool:
        if (r, n) not in cache:
            cache[(r, n)] = closed_fn(r, n)
        return cache[(r, n)]

    rows = []
    states: Dict[int, str] = {}
    for t in data["tickets"]:
        fd = find_flow(data["repo"], t["issue"], flows_root)
        st = (read_json(fd / "state.json") if fd else None) or {}
        state = flow_status(t, fd)
        states[t["issue"]] = state
        ask = open_ask(fd) if fd else None
        rows.append({"issue": t["issue"], "title": t.get("title", ""), "state": state,
                     "step": st.get("step"), "pr": st.get("pr"), "flow_dir": str(fd) if fd else None,
                     "worktree": st.get("worktree"), "ask": ask.get("kind") if ask else None, "unmet": []})
    for row, t in zip(rows, data["tickets"]):
        if row["state"] in LAUNCHABLE:
            row["unmet"] = unmet_deps(t, data, states, closed_cached)
            if row["unmet"] and row["state"] == "pending":
                row["state"] = "blocked"
    return rows


def ready(data: Dict[str, Any], rows: List[Dict[str, Any]]) -> List[int]:
    """Launchable tickets with every dep met, capped by the free slots under max_parallel."""
    running = sum(1 for r in rows if r["state"] == "running")
    slots = max(0, int(data.get("max_parallel", 3)) - running)
    cands = [r["issue"] for r in rows if r["state"] in LAUNCHABLE and not r["unmet"]]
    return cands[:slots]


# ---------------------------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------------------------

def inbox(data: Dict[str, Any], flows_root: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Every open parked question, with the question file or report inlined (read as data)."""
    items = []
    for t in data["tickets"]:
        fd = find_flow(data["repo"], t["issue"], flows_root)
        ask = open_ask(fd) if fd else None
        if not fd or not ask:
            continue
        item: Dict[str, Any] = {"issue": t["issue"], "title": t.get("title", ""), "flow_dir": str(fd), **ask}
        qf = ask.get("question_file")
        if qf:
            item["question_data"] = read_json(fd / qf)
        rf = ask.get("report_file")
        if rf:
            item["report_path"] = str(fd / rf)
        items.append(item)
    return items


# ---------------------------------------------------------------------------------------------
# run: one ticket, blocking, meant for the dispatcher's background Bash
# ---------------------------------------------------------------------------------------------

def parse_claude_output(text: str) -> Tuple[Optional[str], str, bool]:
    """(session_id, result text, is_error) from `claude -p --output-format json` stdout."""
    try:
        v = json.loads(text)
    except ValueError:
        return None, text[-2000:], True
    events = v if isinstance(v, list) else [v]
    res = [e for e in events if isinstance(e, dict) and e.get("type") == "result"] or \
          [e for e in events if isinstance(e, dict) and "session_id" in e]
    if not res:
        return None, "", True
    last = res[-1]
    return last.get("session_id"), str(last.get("result") or ""), bool(last.get("is_error"))


def claude_cmd(prompt: str, mode: str, session: Optional[str] = None) -> List[str]:
    """The headless claude argv for a fresh prompt or a session resume."""
    cmd = ["claude", "-p"]
    if session:
        cmd += ["--resume", session]
    return cmd + ["--permission-mode", mode, "--output-format", "json", prompt]


def _launch(fleet_dir: Path, issue: int, cmd: List[str], cwd: str, label: str) -> Tuple[Optional[str], str, int]:
    runs = fleet_dir / "runs"
    runs.mkdir(exist_ok=True)
    seq = len(list(runs.glob(f"{issue}-*.json"))) + 1
    out_p, err_p = runs / f"{issue}-{seq:02d}.json", runs / f"{issue}-{seq:02d}.err"
    started = now_iso()
    with open(out_p, "w") as out, open(err_p, "w") as err:
        rc = subprocess.run(cmd, cwd=cwd, stdout=out, stderr=err, stdin=subprocess.DEVNULL).returncode
    sid, result, is_err = parse_claude_output(out_p.read_text())
    rec = {"seq": seq, "label": label, "cwd": cwd, "session": sid, "exit": rc, "is_error": is_err,
           "started": started, "ended": now_iso(), "result_tail": result[-600:]}

    def upd(d: Dict[str, Any]) -> None:
        t = ticket(d, issue)
        t.setdefault("runs", []).append(rec)
        if sid:
            t["session"], t["session_cwd"] = sid, cwd
    update_fleet(fleet_dir, upd)
    return sid, result, rc


def run(fleet_dir: Path, issue: int, message_file: Optional[Path]) -> int:
    """Drive one ticket until its flow parks, holds, stops, or finishes. Returns an exit code."""
    locks = fleet_dir / "locks"
    locks.mkdir(exist_ok=True)
    tlock = open(locks / f"{issue}.lock", "a")
    try:
        fcntl.flock(tlock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"FLEET #{issue} already running")
        return 2

    def set_pid(pid: Optional[int]) -> Callable[[Dict[str, Any]], None]:
        def f(d: Dict[str, Any]) -> None:
            ticket(d, issue)["pid"] = pid
        return f

    data = update_fleet(fleet_dir, set_pid(os.getpid()))
    repo, main_wt, mode = data["repo"], data["main_worktree"], data.get("permission_mode", "auto")
    try:
        if message_file:
            t = ticket(data, issue)
            if not t.get("session") or not t.get("session_cwd"):
                print(f"FLEET #{issue} error: no session to resume")
                return 1
            msg = message_file.read_text()
            _launch(fleet_dir, issue, claude_cmd(msg, mode, t["session"]), t["session_cwd"], "answer")
        else:
            for _ in range(3):
                fd = find_flow(repo, issue)
                st = (read_json(fd / "state.json") if fd else None) or {}
                if fd is None:
                    # Fresh run from Main: worktree step, then hand-off. Serialized per fleet so two
                    # tickets never run `git worktree add` against the main checkout at once.
                    with open(locks / "main.lock", "a") as ml:
                        fcntl.flock(ml, fcntl.LOCK_EX)
                        _launch(fleet_dir, issue, claude_cmd(f"/expert-flow {issue} --fleet", mode), main_wt, "fresh")
                    fd2 = find_flow(repo, issue)
                    st2 = (read_json(fd2 / "state.json") if fd2 else None) or {}
                    if fd2 and is_handoff(st2) and st2.get("worktree") and Path(st2["worktree"]).is_dir():
                        continue
                    break
                if st.get("status") == "done" or open_ask(fd):
                    break
                cwd = st.get("worktree") if st.get("worktree") and Path(st["worktree"]).is_dir() else main_wt
                _launch(fleet_dir, issue, claude_cmd(f"/expert-flow --resume {issue} --fleet", mode), cwd, "resume")
                break
    finally:
        update_fleet(fleet_dir, set_pid(None))
    rows = compute(load_fleet(fleet_dir))
    row = next(r for r in rows if r["issue"] == issue)
    extra = f" ({row['ask']} at {row['step']})" if row["ask"] else (f" at {row['step']}" if row["step"] else "")
    print(f"FLEET #{issue} {row['state']}{extra}" + (f" pr={row['pr']}" if row["pr"] else ""))
    return 0


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------

def cmd_init(a: argparse.Namespace) -> int:
    tickets = json.loads(Path(a.tickets).read_text())
    if not isinstance(tickets, list) or not tickets:
        raise SystemExit("ERROR: --tickets must be a non-empty JSON list")
    norm = []
    for t in tickets:
        if not isinstance(t, dict) or not isinstance(t.get("issue"), int):
            raise SystemExit("ERROR: every ticket needs an integer 'issue'")
        for d in t.get("deps") or []:
            parse_dep(d, a.repo)  # validates
        norm.append({"issue": t["issue"], "title": str(t.get("title", "")), "deps": t.get("deps") or [],
                     "pid": None, "session": None, "session_cwd": None, "runs": []})
    epic_n = a.epic.rstrip("/").rsplit("/", 1)[-1]
    fleet_dir = FLEET_ROOT / repo_key(a.repo) / f"{epic_n}-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}"
    fleet_dir.mkdir(parents=True)
    _write_fleet(fleet_dir, {"repo": a.repo, "main_worktree": str(Path(a.main_worktree).resolve()),
                             "epic": a.epic, "max_parallel": a.max, "permission_mode": a.permission_mode,
                             "created": now_iso(), "updated": now_iso(), "tickets": norm})
    print(fleet_dir)
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    data = load_fleet(Path(a.fleet_dir))
    rows = compute(data)
    if a.json:
        print(json.dumps({"rows": rows, "ready": ready(data, rows)}, indent=2))
        return 0
    for r in rows:
        bits = [f"#{r['issue']}", r["state"]]
        if r["step"]:
            bits.append(f"step={r['step']}")
        if r["ask"]:
            bits.append(f"ask={r['ask']}")
        if r["unmet"]:
            bits.append("waits-on=" + ",".join(r["unmet"]))
        if r["pr"]:
            bits.append(r["pr"])
        print("\t".join(bits) + f"\t{r['title']}")
    print("ready: " + (" ".join(f"#{n}" for n in ready(data, rows)) or "none"))
    return 0


def _receipt_settled(text: str) -> bool:
    return not text.startswith("STUB") and (
        any(line.startswith("Decision:") for line in text.splitlines()) or "<!-- awaiting-answers:" in text)


def wait(receipt: Path, timeout: float, interval: float = 5.0) -> str:
    def read() -> str:
        try:
            return receipt.read_text()
        except OSError:
            return ""
    start = read()
    deadline = time.monotonic() + timeout
    while True:
        cur = read()
        if cur != start and _receipt_settled(cur):
            return "settled"
        if time.monotonic() >= deadline:
            return "timeout"
        time.sleep(interval)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="fleet.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init")
    i.add_argument("--repo", required=True)
    i.add_argument("--main-worktree", required=True)
    i.add_argument("--epic", required=True)
    i.add_argument("--tickets", required=True)
    i.add_argument("--max", type=int, default=3)
    i.add_argument("--permission-mode", default="auto",
                   choices=["auto", "acceptEdits", "bypassPermissions", "dontAsk"])
    s = sub.add_parser("status")
    s.add_argument("fleet_dir")
    s.add_argument("--json", action="store_true")
    r = sub.add_parser("ready")
    r.add_argument("fleet_dir")
    b = sub.add_parser("inbox")
    b.add_argument("fleet_dir")
    u = sub.add_parser("run")
    u.add_argument("fleet_dir")
    u.add_argument("issue", type=int)
    u.add_argument("--message-file")
    w = sub.add_parser("wait")
    w.add_argument("receipt")
    w.add_argument("--timeout", type=float, default=540)
    a = p.parse_args(argv)
    if a.cmd == "wait":
        print(wait(Path(a.receipt), a.timeout))
        return 0
    if a.cmd == "init":
        return cmd_init(a)
    if a.cmd == "status":
        return cmd_status(a)
    if a.cmd == "ready":
        data = load_fleet(Path(a.fleet_dir))
        for n in ready(data, compute(data)):
            print(n)
        return 0
    if a.cmd == "inbox":
        print(json.dumps(inbox(load_fleet(Path(a.fleet_dir))), indent=2))
        return 0
    return run(Path(a.fleet_dir), a.issue, Path(a.message_file) if a.message_file else None)


if __name__ == "__main__":
    sys.exit(main())
