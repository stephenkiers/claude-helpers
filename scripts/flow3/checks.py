"""
Run a repo's commands (install, check, hooks) and keep what is worth knowing about them. The exit code
is the fact; no model is asked whether a check passed.
"""

import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from scripts.flow3 import gitops
from scripts.flow3.paths import repo_dir
from scripts.flow3.types import CheckRun
from scripts.workflow.fsutil import atomic_write_text, read_json_object

_INTERESTING = re.compile(r"FAIL|Error|error:|Traceback")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# Lines a test runner or compiler writes to mark a failure, as opposed to lines that mention one.
_STRONG = re.compile(
    r"^\s*(\(fail\)|✗|×|FAIL(ED)?\b|not ok\b|--- FAIL|error(\[\w+\])?:|Error:|\w*(Error|Exception):|Traceback|E   |"
    r"Expected:|Received:|AssertionError|assert\w* failed)|\berror TS\d+")
_FAILED_TEST = re.compile(r"^\s*(?:\(fail\)|✗|×|FAIL(?:ED)?:?|not ok(?: \d+)?(?: -)?|--- FAIL:)\s+(\S.*?)(?:\s+\[[\d.]+ ?m?s\])?\s*$")
_MAX_LINE = 300
_CONTEXT = 8  # lines kept before a failed test's name


def run(command: str, cwd: Path, mode: str, timeout_secs: int, out_path: Path,
        env: Optional[Mapping[str, str]] = None) -> CheckRun:
    """
    Run `command` through the shell in cwd, streaming stdout and stderr to out_path. The process runs
    in its own session; on timeout the whole group is killed. The tree id is read before the run.
    """
    try:
        tree = gitops.tree_id(cwd)
    except (gitops.GitError, OSError):
        tree = ""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    child_env = dict(os.environ)
    if env:
        child_env.update(env)
    timed_out = False
    start = time.monotonic()
    with open(str(out_path), "wb") as sink:
        proc = subprocess.Popen(command, shell=True, cwd=str(cwd), stdin=subprocess.DEVNULL,
                                stdout=sink, stderr=subprocess.STDOUT, env=child_env,
                                start_new_session=True)
        try:
            proc.wait(timeout=timeout_secs)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait()
    duration = time.monotonic() - start
    code = proc.returncode if proc.returncode is not None else -1
    output = out_path.read_text(encoding="utf-8", errors="replace")
    return CheckRun(ok=(code == 0 and not timed_out), command=command, mode=mode, exit_code=code,
                    timed_out=timed_out, duration_secs=duration, tree=tree, output_path=str(out_path),
                    trimmed=trim(output), reused=False)


def strip_ansi(text: str) -> str:
    """Text without terminal colour and cursor codes, so patterns match what a person would read."""
    return _ANSI.sub("", text)


def failures(output: str, limit: int = 5) -> List[str]:
    """
    The names of the tests (or the first words of the errors) a check output reports as failed, in
    order, without repeats, at most `limit`. Empty when the output has no line a test runner marks
    as a failure. Used to name the failure in a stop and to rank lines in `trim`.
    """
    out: List[str] = []
    for ln in strip_ansi(output).splitlines():
        m = _FAILED_TEST.match(ln)
        if not m:
            continue
        name = " ".join(m.group(1).split())[:160]
        if name and name not in out:
            out.append(name)
            if len(out) >= limit:
                break
    return out


def trim(output: str, max_chars: int = 6000) -> str:
    """
    The part of an output worth showing a fixer or a human, within about max_chars, with a note of
    what was cut. Colour codes are removed. Kept, in this order of preference: the tail; each failed
    test's name with the few lines above it; other lines a test runner or compiler marks as a failure
    (Expected and Received, an `error:` at the start of a line, a traceback), each with the line
    after it; then lines that only mention an error. A long log full of passing tests that print the word Error must not crowd out
    the few lines that say what failed.
    """
    output = strip_ansi(output)
    if len(output) <= max_chars:
        return output
    lines = [ln if len(ln) <= _MAX_LINE else ln[:_MAX_LINE] + "..." for ln in output.splitlines()]
    tail_budget = int(max_chars * 0.4)
    used = 0
    start = len(lines)
    while start > 0 and used + len(lines[start - 1]) + 1 <= tail_budget:
        start -= 1
        used += len(lines[start]) + 1
    budget = max_chars - used - 200
    keep: Dict[int, bool] = {}

    def take(idx: int) -> bool:
        nonlocal budget
        if idx in keep or idx >= start:
            return True
        cost = len(lines[idx]) + 1
        if cost > budget:
            return False
        budget -= cost
        keep[idx] = True
        return True

    # First the lines that name a failed test, each with the lines just before it (runners print the
    # assertion, Expected and Received above the name) and the one after.
    for i in range(start):
        if _FAILED_TEST.match(lines[i]):
            if not take(i):
                break
            for j in range(i - 1, max(i - _CONTEXT, 0) - 1, -1):
                if not take(j):
                    break
            take(i + 1)
    for i in range(start):
        if i not in keep and _STRONG.search(lines[i]):
            if not take(i):
                break
            take(i + 1)
    for i in range(start):
        if i not in keep and _INTERESTING.search(lines[i]):
            if not take(i):
                break
    picked = [lines[i] for i in sorted(keep)]
    tail = lines[start:]
    kept = len(picked) + len(tail)
    note = "[output trimmed: kept %d of %d lines (%d matching lines plus the last %d)]" % (
        kept, len(lines), len(picked), len(tail))
    parts = [note]
    if picked:
        parts += picked
    parts.append("[... %d lines cut ...]" % (len(lines) - kept))
    parts += tail
    return "\n".join(parts)


def _record_path(repo: str) -> Path:
    return repo_dir(repo) / "check-passes.json"


def _key(tree: str, command: str) -> str:
    return hashlib.sha256((tree + "\0" + command).encode("utf-8")).hexdigest()


def passed_before(repo: str, tree: str, command: str) -> bool:
    """True when this command already passed on this exact tree (the merge gate skips re-running it)."""
    if not tree:
        return False
    data = read_json_object(_record_path(repo)) or {}
    return _key(tree, command) in data


def record_pass(repo: str, tree: str, command: str) -> None:
    """Record a pass keyed by tree and command. Written atomically under a lock."""
    if not tree:
        return
    path = _record_path(repo)
    with open(str(path) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data: Dict[str, Any] = read_json_object(path) or {}
        data[_key(tree, command)] = {"tree": tree, "command": command, "ts": time.time()}
        atomic_write_text(path, json.dumps(data, indent=1, sort_keys=True) + "\n")


# -- red results, kept per flow ----------------------------------------------------------------
#
# A pass is true of a tree for every flow in the repo. A red is kept per flow and dropped when a
# human resumes a stopped flow or a heal changes the environment, because a red can come from the
# machine and not from the tree.

def reds_path(flow_path: Path) -> Path:
    return flow_path / "check-reds.json"


def red_before(flow_path: Path, tree: str, command: str) -> Optional[Dict[str, Any]]:
    """The recorded failing result of this command on this exact tree in this flow, or None."""
    if not tree:
        return None
    row = (read_json_object(reds_path(flow_path)) or {}).get(_key(tree, command))
    if not isinstance(row, dict):
        return None
    # If the output file is recorded but unreadable, treat the red as absent and run the check fresh.
    out_path = str(row.get("output_path") or "")
    if out_path and not Path(out_path).exists():
        return None
    return row


def record_red(flow_path: Path, run: CheckRun) -> None:
    """Record a failing run keyed by its tree and command. The caller holds the flow's run lock."""
    if not run.tree:
        return
    path = reds_path(flow_path)
    data: Dict[str, Any] = read_json_object(path) or {}
    data[_key(run.tree, run.command)] = {"tree": run.tree, "command": run.command, "mode": run.mode,
                                         "exit_code": run.exit_code, "output_path": run.output_path,
                                         "ts": time.time()}
    atomic_write_text(path, json.dumps(data, indent=1, sort_keys=True) + "\n")


def clear_reds(flow_path: Path) -> None:
    """Forget this flow's recorded reds (the environment may have changed)."""
    try:
        reds_path(flow_path).unlink()
    except OSError:
        pass


def reused_red(row: Mapping[str, Any], mode: str) -> Optional[CheckRun]:
    """
    A CheckRun rebuilt from a recorded red, marked reused, or None when its output cannot be read: a
    red with no output would send a fixer in blind, so the caller runs the check fresh instead.
    """
    out_path = str(row.get("output_path") or "")
    if not out_path:
        return None
    try:
        output = Path(out_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return CheckRun(ok=False, command=str(row.get("command") or ""), mode=mode,
                    exit_code=int(row.get("exit_code") or 1), timed_out=False, duration_secs=0.0,
                    tree=str(row.get("tree") or ""), output_path=out_path, trimmed=trim(output), reused=True)
