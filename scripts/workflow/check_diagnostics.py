"""
Diagnostics for post-merge check commands run by /cleanup.

A failing check (e.g. `just check` -> `cargo test`) prints the failing test names and panics to
stdout, which the one-line failure message used to drop. This module persists each check's full
output to a log file, builds a bounded failure excerpt for the message, and snapshots the
environment on failure. Everything here is best-effort and never raises: diagnostics must not mask
or change a check result.
"""

import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .checks import CheckResult

LOG_DIR_PREFIX = "cleanup-checks-"
EXCERPT_TAIL_LINES = 40
EXCERPT_MAX_CHARS = 2000
EXCERPT_MAX_MATCH_LINES = 40
EXCERPT_LINE_PATTERN = re.compile(r"FAILED|panicked|failures:|error")
SNAPSHOT_PROCESS_PATTERN = re.compile(r"\b(cargo|rustc|just)\b")
SNAPSHOT_CMD_TIMEOUT_SECS = 10


def make_log_dir() -> Optional[Path]:
    """Create a fresh per-run log directory under the system temp dir; None on failure."""
    try:
        return Path(tempfile.mkdtemp(prefix=LOG_DIR_PREFIX))
    except Exception:
        return None


def build_failure_excerpt(stdout: str, stderr: str) -> str:
    """
    Short, size-bounded excerpt of combined output: lines matching
    FAILED|panicked|failures:|error if any (first EXCERPT_MAX_MATCH_LINES), otherwise the last
    EXCERPT_TAIL_LINES lines. Truncated to EXCERPT_MAX_CHARS. Never raises.
    """
    try:
        lines = [ln.rstrip() for ln in f"{stdout or ''}\n{stderr or ''}".splitlines() if ln.strip()]
        matched = [ln for ln in lines if EXCERPT_LINE_PATTERN.search(ln)]
        chosen = matched[:EXCERPT_MAX_MATCH_LINES] if matched else lines[-EXCERPT_TAIL_LINES:]
        excerpt = "\n".join(chosen)
        if len(excerpt) > EXCERPT_MAX_CHARS:
            excerpt = excerpt[:EXCERPT_MAX_CHARS] + "...[truncated]"
        return excerpt
    except Exception:
        return ""


def _run_best_effort(argv: List[str], cwd: Optional[Path] = None) -> str:
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=SNAPSHOT_CMD_TIMEOUT_SECS,
        )
        return (proc.stdout or "") + (proc.stderr or "")
    except Exception as e:
        return f"<unavailable: {e}>"


def environment_snapshot(main_worktree: Optional[Path]) -> str:
    """Best-effort snapshot: main HEAD SHA, git status, cargo/rustc/just processes, free disk."""
    parts = []
    try:
        parts.append("--- main HEAD ---\n" + _run_best_effort(["git", "rev-parse", "HEAD"], main_worktree))
        parts.append("--- git status --porcelain ---\n" + _run_best_effort(["git", "status", "--porcelain"], main_worktree))
        ps_out = _run_best_effort(["ps", "-axo", "pid,etime,command"])
        procs = [ln for ln in ps_out.splitlines() if SNAPSHOT_PROCESS_PATTERN.search(ln)]
        parts.append("--- concurrent cargo/rustc/just processes ---\n" + "\n".join(procs) + "\n")
        try:
            usage = shutil.disk_usage(str(main_worktree) if main_worktree else ".")
            disk = f"free {usage.free // (1024 ** 2)} MiB of {usage.total // (1024 ** 2)} MiB\n"
        except Exception as e:
            disk = f"<unavailable: {e}>\n"
        parts.append("--- free disk space ---\n" + disk)
    except Exception as e:
        parts.append(f"<snapshot failed: {e}>\n")
    return "\n".join(parts)


def write_check_log(
    log_dir: Optional[Path],
    index: int,
    cmd: str,
    result: CheckResult,
    started_at: datetime,
    duration_secs: float,
    main_worktree: Optional[Path],
) -> Optional[Path]:
    """
    Write the full output of one check run to <log_dir>/check-<index>.log. Environment snapshot is
    appended only when the check failed. Returns the log path, or None if it could not be written.
    Never raises.
    """
    if log_dir is None:
        return None
    try:
        path = log_dir / f"check-{index}.log"
        header = (
            f"command: {cmd}\n"
            f"started: {started_at.astimezone(timezone.utc).isoformat()}\n"
            f"duration_secs: {duration_secs:.2f}\n"
            f"exit_code: {result.returncode}\n"
            f"success: {result.success}\n"
            f"error: {result.error}\n"
        )
        body = f"\n=== stdout ===\n{result.stdout or ''}\n=== stderr ===\n{result.stderr or ''}\n"
        snapshot = ""
        if not result.success:
            snapshot = "\n=== environment snapshot ===\n" + environment_snapshot(main_worktree)
        path.write_text(header + body + snapshot, encoding="utf-8", errors="replace")
        return path
    except Exception:
        return None
