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
_MAX_LINE = 300
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\\\)")
# A line that marks one failed test (bun, jest, vitest, pytest, unittest, cargo, TAP).
FAILED_TEST = re.compile(
    r"^\s*(?:✗|✘|×|\(fail\)|FAILED\b|FAIL\b|ERROR:|not ok\b)|\.\.\. FAILED\s*$")
# A line that belongs to a failure's detail: an assertion, a compiler or lint error, a traceback.
_FAILURE_DETAIL = re.compile(
    r"^\s*(?:error(?:\[\w+\])?:|Expected:|Received:|AssertionError|Traceback \(most recent|E  \s|\d+:\d+\s+error\b)"
    r"|\berror TS\d+|panicked at")
_BEFORE, _AFTER = 14, 2


def strip_ansi(text: str) -> str:
    """Text without terminal colour and cursor codes, which hide line starts from every pattern here."""
    return _ANSI.sub("", text)


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


def _windows(lines: List[str], pattern: "re.Pattern[str]", upto: int, taken: List[bool], budget: int) -> int:
    """Mark each matching line below `upto`, with the lines around it, in `taken`. Returns matches left out."""
    used = sum(len(lines[i]) + 1 for i in range(upto) if taken[i])
    left_out = 0
    for i in range(upto):
        if not pattern.search(lines[i]):
            continue
        want = [j for j in range(max(0, i - _BEFORE), min(upto, i + _AFTER + 1)) if not taken[j]]
        cost = sum(len(lines[j]) + 1 for j in want)
        if used + cost > budget:
            left_out += 1
            continue
        for j in want:
            taken[j] = True
        used += cost
    return left_out


def trim(output: str, max_chars: int = 12000) -> str:
    """
    The part of an output worth showing a fixer or a human, within about max_chars, with a note of
    what was cut. Colour codes are removed first. When the output has failure blocks (a failed-test
    line, or failing that an assertion or a compiler error), those and the lines around them come
    first and the tail gets a quarter of the room. Otherwise the tail gets most of it, plus earlier lines that mention
    an error.
    """
    output = strip_ansi(output)
    if len(output) <= max_chars:
        return output
    lines = [ln if len(ln) <= _MAX_LINE else ln[:_MAX_LINE] + "..." for ln in output.splitlines()]
    focused = any(FAILED_TEST.search(ln) or _FAILURE_DETAIL.search(ln) for ln in lines)
    tail_budget = int(max_chars * (0.25 if focused else 0.6))
    used = 0
    start = len(lines)
    while start > 0 and used + len(lines[start - 1]) + 1 <= tail_budget:
        start -= 1
        used += len(lines[start]) + 1
    tail = lines[start:]
    head_budget = max_chars - used - 200
    taken = [False] * start
    left_out = 0
    if focused:
        # The blocks around failed-test lines are taken first: passing tests print errors of their own
        # (a rejected mock, a logged warning) that look like failure detail and would crowd them out.
        # Whatever room is left goes to other failure detail, since some runners print the detail far
        # from the line that names the failed test.
        left_out = _windows(lines, FAILED_TEST, start, taken, head_budget)
        more = _windows(lines, _FAILURE_DETAIL, start, taken, head_budget)
        if not any(FAILED_TEST.search(ln) for ln in lines):
            left_out += more
    else:
        used_head = 0
        for i in range(start):
            if _INTERESTING.search(lines[i]):
                if used_head + len(lines[i]) + 1 > head_budget:
                    break
                taken[i] = True
                used_head += len(lines[i]) + 1
    picked = sum(1 for t in taken if t)
    kept = picked + len(tail)
    note = "[output trimmed: kept %d of %d lines (%d %s plus the last %d)%s]" % (
        kept, len(lines), picked, "lines from failure blocks" if focused else "matching lines", len(tail),
        "; %d more failure lines did not fit, the full log has them" % left_out if left_out else "")
    parts = [note]
    prev = -1
    for i in range(start):
        if not taken[i]:
            continue
        if prev >= 0 and i != prev + 1:
            parts.append("[...]")
        parts.append(lines[i])
        prev = i
    parts.append("[... %d lines cut ...]" % (len(lines) - kept))
    parts += tail
    return "\n".join(parts) + "\n"


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
