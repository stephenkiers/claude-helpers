#!/usr/bin/env python3
"""Dispatcher mechanics for /expert-fleet: fleet state, background ticket runs, status, inbox.

The fleet session (the model running /expert-fleet) decides; this script does the bookkeeping that
must be deterministic. Each ticket runs as its own headless `claude -p` process driving
`/expert-flow <N> --fleet`; a ticket that needs a human parks by writing `fleet-ask.json` into its
flow directory and exiting. This script never answers a question and never edits a flow's files:
it only reads `~/.claude/flows/` and writes under the fleet directory. The fleet never edits a
flow's files or the flow's git directory, and the bounded tool writes named in ADR-0025 are the
only ticket-worktree writes.

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
import hashlib
import json
import os
import re
import signal
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


# Closed sets for the heal record. Anything outside them is treated as failed / take-over.
# These mirror scripts/workflow/heal.py (HEAL_STEPS = KNOWN_STEPS, HEAL_STATUSES = _STATUS_VALUES,
# HEAL_TOOL_CAUSES = {tool.name: tool.cause for tool in TOOLS}) and are pinned by tests/test_fleet_invariants.py.
HEAL_STEPS = ("worktree", "plan", "track", "implement", "review", "fix", "verify", "assess", "ship", "merge")
HEAL_TOOL_CAUSES = {"install": "missing-deps"}
HEAL_STATUSES = ("no-op", "healed", "refused", "failed")
HEAL_RECOMMENDATIONS = ("retry-recommended", "take-over")
HEAL_PROBE_TIMEOUT_SECS = 30
# The run call's wall-clock budget is the probe's timeout_secs plus this margin. timeout_secs is capped at
# HEAL_MAX_TIMEOUT_SECS, so one run is bounded by at most 900 + 60 = 960 seconds.
HEAL_RUN_MARGIN_SECS = 60
HEAL_TERM_GRACE_SECS = 15
HEAL_MAX_TIMEOUT_SECS = 900  # mirrors deps.DEPS_TIMEOUT_SECS; a larger probe value is rejected


def current_stop_key(flow_dir: Optional[Path]) -> Optional[str]:
    """
    The identity of a flow's current stop, independent of any probe.

    This mirrors scripts/workflow/heal.py stop_key_for and MUST stay identical to it: an open ask is
    the sha256 of the bytes of fleet-ask.json; otherwise the sha256 of step, status and stop_reason[step] joined by newlines
    from state.json; both truncated to 16 hex digits. fleet.py is stdlib-only and cannot import heal.py,
    so heal records and notes are matched to the current stop by recomputing this key and comparing it
    with the key the probe stored.
    """
    if flow_dir is None:
        return None
    try:
        st = read_json(flow_dir / "state.json")
        if not st:
            return None
        ask_path = flow_dir / "fleet-ask.json"
        if ask_path.exists():
            raw = ask_path.read_bytes()
            v = json.loads(raw)
            if isinstance(v, dict) and v.get("status") == "open":
                return hashlib.sha256(raw).hexdigest()[:16]
        step = st.get("step")
        # A non-string step cannot index stop_reason (dict.get on a list raises TypeError); it gets no reason.
        reasons = st.get("stop_reason")
        reason = reasons.get(step) if isinstance(reasons, dict) and isinstance(step, str) else ""
        text = f"{step}\n{st.get('status')}\n{reason if isinstance(reason, str) else ''}"
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    except (OSError, TypeError, ValueError):
        return None


def _fixed_line(value: Any) -> str:
    """A display-safe single line: only printable characters, bounded. Never a ticket-controlled string."""
    if not isinstance(value, str):
        return "Heal diagnosis unavailable."
    return "".join(c for c in value if c.isprintable())[:160] or "Heal diagnosis unavailable."


def heal_write_ahead(t: Dict[str, Any], cause: str, step: str, tool: str = "", stop_key: str = "") -> bool:
    """
    Spend the heal cap for a cause: False when the cause already has any entry (even a malformed one),
    otherwise write an `attempted` entry and return True. Call under update_fleet.
    """
    heals = t.get("heals")
    if not isinstance(heals, dict):
        heals = {}
        t["heals"] = heals
    if cause in heals:
        return False
    heals[cause] = {"tool": tool, "step": step if step in HEAL_STEPS else "", "at": now_iso(),
                    "outcome": "attempted", "recommendation": "", "stop_key": stop_key}
    return True


def heal_finish(t: Dict[str, Any], cause: str, tool: str, outcome: str, recommendation: str,
                stop_key: str) -> None:
    """Finish an `attempted` entry; any other entry is left untouched. Call under update_fleet."""
    heals = t.get("heals")
    entry = heals.get(cause) if isinstance(heals, dict) else None
    if isinstance(entry, dict) and entry.get("outcome") == "attempted":
        entry.update({"tool": tool, "outcome": outcome, "recommendation": recommendation,
                      "stop_key": stop_key})


def heal_set_note(t: Dict[str, Any], stop_key: Optional[str], line: str) -> None:
    """Overwrite the ticket's heal_note. Spends no cap. Call under update_fleet."""
    t["heal_note"] = {"stop_key": stop_key, "at": now_iso(), "line": _fixed_line(line)}


def _last_run_ended(t: Dict[str, Any]) -> str:
    """The latest run's end time (ISO-8601 Z, so string order is time order); '' when there are no runs."""
    ended = [r.get("ended") for r in t.get("runs") or []
             if isinstance(r, dict) and isinstance(r.get("ended"), str)]
    return max(ended, default="")


def matching_heal(t: Dict[str, Any], key: Optional[str]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    (heal entry, heal_note) whose stop_key equals the current stop's key; (None, None) otherwise.

    The stop key cannot tell two identical failures apart, so a record also has to be newer than the
    ticket's latest run: any run after the heal (a retry, an answer) means the stop has been revisited
    and the record describes an earlier attempt. The heal cap itself is unaffected (one per cause).
    """
    if not key:
        return None, None
    last = _last_run_ended(t)

    def current(entry: Dict[str, Any]) -> bool:
        at = entry.get("at")
        return isinstance(at, str) and at >= last

    heal = None
    heals = t.get("heals")
    if isinstance(heals, dict):
        for entry in heals.values():
            if isinstance(entry, dict) and entry.get("stop_key") == key and current(entry):
                heal = entry
                break
    note = t.get("heal_note")
    if not (isinstance(note, dict) and note.get("stop_key") == key and current(note)):
        note = None
    return heal, note


def heal_bits(heal: Optional[Dict[str, Any]], note: Optional[Dict[str, Any]]) -> List[str]:
    """Short enum-only words for the status row and FLEET line. 'healed' is never shown: a retry decides."""
    bits: List[str] = []
    if heal:
        outcome = heal.get("outcome")
        if outcome == "attempted":
            bits.append("heal=interrupted")
        elif outcome in HEAL_STATUSES:
            bits.append("heal=attempted" if outcome == "healed" else f"heal={outcome}")
            if heal.get("recommendation") == "retry-recommended" and outcome == "healed":
                bits.append("heal: Retry recommended")
            elif heal.get("recommendation") == "take-over":
                bits.append("heal: Take over")
        else:
            bits.append("heal=unknown")
    if note and not heal:
        bits.append("heal=diagnosis")
    return bits


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


def _proc_start(pid: int) -> Optional[str]:
    """A process's start time from ps, which identifies a pid across reuse; None when ps cannot say."""
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = out.stdout.strip()
    return text if out.returncode == 0 and text else None


def heal_child_alive(t: Dict[str, Any]) -> bool:
    """
    True while the heal CLI recorded for this ticket may still be running (an installer may outlive a killed
    supervisor). A recorded pid that is alive but has a different start time was reused, so it does not count.
    Unknown identity counts as alive: refusing to resume is the safe side.
    """
    child = t.get("heal_child")
    if not isinstance(child, dict):
        return False
    pid = child.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or not pid_alive(pid):
        return False
    recorded = child.get("started")
    if not isinstance(recorded, str) or not recorded:
        return True
    now = _proc_start(pid)
    return now is None or now == recorded


def open_ask(flow_dir: Path) -> Optional[Dict[str, Any]]:
    """The flow's parked question, when one is open."""
    ask = read_json(flow_dir / "fleet-ask.json")
    return ask if ask and ask.get("status") == "open" else None


def _heals_dir(fleet_dir: Path) -> Optional[Path]:
    """FLEET/heals, created 0700 and chmod'ed 0700; None when it is a symlink or not a directory."""
    path = fleet_dir / "heals"
    if path.is_symlink():
        return None
    os.makedirs(path, mode=0o700, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        return None
    os.chmod(path, 0o700)
    return path


def _reserve_tail(heals: Path, issue: int) -> Optional[Path]:
    """Collision-free tail name: reserve `<issue>-<seq>.seq` with O_EXCL; the CLI then creates the .tail."""
    for seq in range(1, 1000):
        try:
            fd = os.open(str(heals / f"{issue}-{seq:02d}.seq"),
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return heals / f"{issue}-{seq:02d}.tail"
    return None


def _run_cli(argv: List[str], cwd: Path, timeout: float,
             on_spawn: Optional[Callable[[int], None]] = None) -> Optional[Dict[str, Any]]:
    """
    Run the heal CLI with a fixed argv and no shell. On timeout, SIGTERM first (so ensure_deps' cleanup
    kills the installer's process group), wait HEAL_TERM_GRACE_SECS, then SIGKILL. `on_spawn` receives the
    child's pid before anything else happens, so the caller can record it. Returns the parsed JSON object,
    or None on a timeout, OSError, non-zero exit, unparseable output or non-object JSON. Every wait after a
    kill is bounded: a grandchild that still holds stdout must not wedge the supervisor.
    """
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return None
    if on_spawn is not None:
        try:
            on_spawn(proc.pid)
        except Exception:
            proc.kill()  # never leave an unrecorded child running
            raise
    try:
        stdout, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.communicate(timeout=HEAL_TERM_GRACE_SECS)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=HEAL_TERM_GRACE_SECS)
            except subprocess.TimeoutExpired:
                pass  # a grandchild still holds stdout; give up on its output rather than block
        return None
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    try:
        v = json.loads(stdout)
    except ValueError:
        return None
    return v if isinstance(v, dict) else None


def _default_base(repo: str, main_worktree: Path) -> Optional[str]:
    """
    origin/<default branch> from `gh repo view`, or None. No fallback guess.

    The branch name comes from GitHub, but the ref returned is the local remote-tracking origin/<name> in the
    main worktree, which this function does not fetch. It can therefore lag the remote's default branch. The
    heal tools resolve the ref themselves and fail closed when it is missing (heal.py: "No base ref"), so a
    lagging ref changes what the heal compares against; it is never treated as the remote's current state.
    """
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        return None
    try:
        out = subprocess.run(["gh", "repo", "view", repo, "--json", "defaultBranchRef",
                              "-q", ".defaultBranchRef.name"],
                             cwd=str(main_worktree), capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    name = out.stdout.strip()
    if out.returncode != 0 or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._/-]*", name):
        return None
    return f"origin/{name}"


def _is_flow_stop_ask(ask: Dict[str, Any]) -> bool:
    gate = ask.get("gate")
    return ask.get("kind") == "gate" and isinstance(gate, dict) and gate.get("header") == "Flow stop"


def heal_pass(fleet_dir: Path, issue: int, repo: str, flow_dir: Optional[Path],
              main_worktree: Path) -> None:
    """
    Run the heal harness once for a ticket that has just stopped. Never raises: it must not prevent the
    pid clear or the FLEET line. A failure is reported on stderr with the stage it happened in, and the
    exception's message is left out (it may carry paths or tool output). Skips quickly for tickets that are
    done, running, or parked on anything other than a Flow stop gate.
    """
    if not flow_dir:
        return
    stage = ["start"]
    try:
        _heal_pass(fleet_dir, issue, repo, flow_dir, main_worktree, stage)
    except Exception as e:
        sys.stderr.write(f"FLEET #{issue} heal failed at stage {stage[0]} ({type(e).__name__})\n")


def _cap_spent(fleet_dir: Path, issue: int, cause: str) -> bool:
    """Read-only: True when the cause already has a heal entry. Checked before any tail is reserved."""
    heals = ticket(load_fleet(fleet_dir), issue).get("heals")
    return isinstance(heals, dict) and cause in heals


def _heal_pass(fleet_dir: Path, issue: int, repo: str, flow_dir: Path, main_worktree: Path,
               stage: List[str]) -> None:
    stage[0] = "precheck"
    st = read_json(flow_dir / "state.json") or {}
    if st.get("status") not in ("failed", "paused", "awaiting"):
        return
    ask = open_ask(flow_dir)
    if ask is not None and not _is_flow_stop_ask(ask):
        return
    step = st.get("step")
    if step not in HEAL_STEPS:
        return
    key = current_stop_key(flow_dir)

    def note(line: str, stop_key: Optional[str] = key) -> None:
        def upd(d: Dict[str, Any]) -> None:
            heal_set_note(ticket(d, issue), stop_key, line)
        update_fleet(fleet_dir, upd)

    worktree = st.get("worktree")
    if not isinstance(worktree, str) or not worktree or not Path(worktree).is_dir():
        note("Ticket worktree is not recorded.")
        return
    base = _default_base(repo, main_worktree)
    if base is None:
        note("Could not resolve the default branch.")
        return

    helpers_dir = Path(__file__).resolve().parent.parent
    common = ["--worktree", worktree, "--main-worktree", str(main_worktree), "--base", base,
              "--flow-dir", str(flow_dir), "--issue", str(issue)]
    cli = ["python3", "-m", "scripts.workflow.cli", "heal"]

    stage[0] = "probe"
    probe = _run_cli(cli + ["probe"] + common, helpers_dir, HEAL_PROBE_TIMEOUT_SECS)
    if probe is None:
        note("Heal probe failed.")
        return
    probe_key = probe.get("stop_key")
    if not isinstance(probe_key, str):
        probe_key = None
    tool = probe.get("tool")
    if tool is None:
        if "line" in probe:
            note(_fixed_line(probe.get("line")), probe_key or key)
        return
    timeout_secs = probe.get("timeout_secs")
    if (not isinstance(tool, str) or tool not in HEAL_TOOL_CAUSES or probe_key is None
            or isinstance(timeout_secs, bool) or not isinstance(timeout_secs, int)
            or not 1 <= timeout_secs <= HEAL_MAX_TIMEOUT_SECS):
        note("Heal probe returned an unusable nomination.")
        return
    if probe_key != key:
        note("The stop changed during the heal probe.")
        return
    cause = HEAL_TOOL_CAUSES[tool]

    # The cap is checked before anything is reserved, so a capped cause leaves no .seq behind.
    if _cap_spent(fleet_dir, issue, cause):
        return

    stage[0] = "reserve"
    heals = _heals_dir(fleet_dir)
    tail_path = _reserve_tail(heals, issue) if heals else None
    if tail_path is None:
        note("Heal tail directory is unsafe.")
        return

    stage[0] = "record"
    spent = [False]

    def upd_attempt(d: Dict[str, Any]) -> None:
        spent[0] = heal_write_ahead(ticket(d, issue), cause, str(step), tool, probe_key)
    update_fleet(fleet_dir, upd_attempt)
    if not spent[0]:
        # Lost a race for the cap after the check: release the reservation so it does not leak.
        tail_path.with_suffix(".seq").unlink(missing_ok=True)
        return

    child: List[int] = []

    def on_spawn(pid: int) -> None:
        child.append(pid)
        started = _proc_start(pid)

        def upd_child(d: Dict[str, Any]) -> None:
            ticket(d, issue)["heal_child"] = {"pid": pid, "started": started}
        update_fleet(fleet_dir, upd_child)

    outcome, recommendation = "failed", "take-over"
    stage[0] = "run"
    try:
        result = _run_cli(cli + ["run"] + common + ["--tool", tool, "--tail-file", str(tail_path)],
                          helpers_dir, timeout_secs + HEAL_RUN_MARGIN_SECS, on_spawn=on_spawn)
        if (result is not None and result.get("tool") == tool and result.get("status") in HEAL_STATUSES
                and result.get("recommendation") in HEAL_RECOMMENDATIONS):
            outcome = str(result["status"])
            recommendation = str(result["recommendation"])
            if recommendation == "retry-recommended" and outcome != "healed":
                recommendation = "take-over"
    finally:
        def upd_finish(d: Dict[str, Any]) -> None:
            heal_finish(ticket(d, issue), cause, tool, outcome, recommendation, probe_key)
        update_fleet(fleet_dir, upd_finish)
        # Clear the child record only once that child has exited; a live one keeps the ticket refusing resume.
        if child and not pid_alive(child[0]):
            def upd_clear(d: Dict[str, Any]) -> None:
                ticket(d, issue).pop("heal_child", None)
            update_fleet(fleet_dir, upd_clear)


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
    """One row per ticket: issue, title, state, step, pr, flow_dir, unmet deps, ask kind, heal info."""
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
        row: Dict[str, Any] = {"issue": t["issue"], "title": t.get("title", ""), "state": state,
                               "step": st.get("step"), "pr": st.get("pr"), "flow_dir": str(fd) if fd else None,
                               "worktree": st.get("worktree"), "ask": ask.get("kind") if ask else None, "unmet": []}
        # Heal record / note whose stop_key matches the CURRENT stop (never a stale one).
        if state in ("stopped", "parked", "failed", "stalled"):
            heal, hnote = matching_heal(t, current_stop_key(fd))
            if heal:
                row["heal"] = heal
            if hnote:
                row["heal_note"] = hnote
        rows.append(row)
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
        # A live run process owns the ticket and runs its heal pass inside itself, so a parked state is not final yet.
        if pid_alive(t.get("pid")):
            continue
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
        # Heal record / note only when its stop_key matches this open ask
        heal, hnote = matching_heal(t, current_stop_key(fd))
        if heal:
            item["heal"] = heal
        if hnote:
            item["heal_note"] = hnote
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
    # A supervisor killed mid-heal leaves its installer running. Resuming now would race that installer for
    # the same worktree, so the resume waits until the recorded heal process has exited.
    if heal_child_alive(ticket(load_fleet(fleet_dir), issue)):
        print(f"FLEET #{issue} heal still running; not resuming until it exits")
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
        # Run heal harness if ticket is stopped
        fd = find_flow(repo, issue)
        if fd:
            heal_pass(fleet_dir, issue, repo, fd, Path(main_wt))
    finally:
        update_fleet(fleet_dir, set_pid(None))
    rows = compute(load_fleet(fleet_dir))
    row = next(r for r in rows if r["issue"] == issue)
    extra = f" ({row['ask']} at {row['step']})" if row["ask"] else (f" at {row['step']}" if row["step"] else "")
    heal_suffix = "".join(" " + b for b in heal_bits(row.get("heal"), row.get("heal_note")))
    print(f"FLEET #{issue} {row['state']}{extra}{heal_suffix}" + (f" pr={row['pr']}" if row["pr"] else ""))
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
                     "pid": None, "session": None, "session_cwd": None, "runs": [], "heals": {}})
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
        bits.extend(heal_bits(r.get("heal"), r.get("heal_note")))
        if r.get("heal_note"):
            bits.append("note=" + _fixed_line(r["heal_note"].get("line")))
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
