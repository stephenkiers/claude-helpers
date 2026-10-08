"""
Diagnostics for post-merge check commands run by /cleanup.

A failing check (e.g. `just check` -> `cargo test`) prints the failing test names and panics to
stdout, which the one-line failure message used to drop. This module persists each check's full
output to a log file, builds a bounded failure excerpt for the message, and snapshots the
environment on failure. Everything here is best-effort and never raises: diagnostics must not mask
or change a check result.
"""

import glob
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Mapping, Sequence

from .checks import CheckResult

LOG_DIR_PREFIX = "cleanup-checks-"
EXCERPT_TAIL_LINES = 40
EXCERPT_MAX_CHARS = 2000
EXCERPT_MAX_MATCH_LINES = 40
EXCERPT_LINE_PATTERN = re.compile(r"FAILED|panicked|failures:|error")
SNAPSHOT_PROCESS_PATTERN = re.compile(r"\b(cargo|rustc|just)\b")
SNAPSHOT_CMD_TIMEOUT_SECS = 10
LOG_DIR_RETENTION_COUNT = 20
# Pattern to match common secret/token/key forms: token=VALUE, Authorization: Bearer VALUE, etc.
# Case-insensitive to catch variations like Token, TOKEN, token, etc.
# Handles "Bearer" as part of the key specification (e.g., "Authorization: Bearer TOKEN").
SECRET_PATTERN = re.compile(
    r"(?i)((?:token|key|password|authorization|secret|api[_-]?key|bearer)[=:\s]+(?:bearer\s+)?)([^\s,'\"\n]+)",
    re.IGNORECASE | re.MULTILINE,
)
# Pattern for JSON-shaped secrets with quoted keys, e.g., "password": "secret123"
JSON_SECRET_PATTERN = re.compile(
    r'(?i)("[^"]*(?:password|token|key|secret|api[_-]?key|authorization)"\s*:\s*)"?([^\s,\n"]+)"?',
    re.IGNORECASE | re.MULTILINE,
)
# Pattern for bare token shapes: GitHub tokens and AWS access keys
BARE_TOKEN_PATTERN = re.compile(
    r"((?:ghp_|ghs_|ghu_|github_pat_)[A-Za-z0-9_]+|AKIA[0-9A-Z]{16})",
    re.MULTILINE,
)


def _redact(text: str) -> str:
    """
    Redact common secret/token/key patterns from text.

    Matches patterns like 'token=VALUE', 'Authorization: Bearer VALUE', 'password=VALUE',
    JSON-shaped secrets like '"password": "secret123"', and bare token shapes like GitHub
    tokens (ghp_, ghs_, ghu_, github_pat_) and AWS access keys (AKIA...).
    All matched values are replaced by '<redacted>'. Case-insensitive. Never raises.
    """
    try:
        # Apply keyword-based pattern (token=value, password: value, etc.)
        text = SECRET_PATTERN.sub(r"\1<redacted>", text)
        # Apply JSON-shaped pattern ("password": "value", etc.)
        text = JSON_SECRET_PATTERN.sub(r"\1<redacted>", text)
        # Apply bare token shape pattern (GitHub tokens, AWS keys, etc.)
        text = BARE_TOKEN_PATTERN.sub(r"<redacted>", text)
        return text
    except Exception:
        return text


def make_log_dir() -> Optional[Path]:
    """
    Create a fresh per-run log directory under the system temp dir; None on failure.

    Before creating a new dir, performs best-effort age-based pruning: deletes old
    cleanup-checks-* dirs (keeping the most recent LOG_DIR_RETENTION_COUNT). Pruning
    failures never block directory creation.
    """
    # Prune old log dirs (best-effort)
    try:
        temp_root = tempfile.gettempdir()
        pattern = str(Path(temp_root) / f"{LOG_DIR_PREFIX}*")
        old_dirs = sorted(
            glob.glob(pattern),
            key=lambda p: Path(p).stat().st_mtime,
        )
        # Keep the most recent LOG_DIR_RETENTION_COUNT - 1 dirs, so after creating the new one,
        # total count is LOG_DIR_RETENTION_COUNT (e.g., 19 old + 1 new = 20)
        to_delete = old_dirs[:-(LOG_DIR_RETENTION_COUNT - 1)]
        for dir_path in to_delete:
            shutil.rmtree(dir_path, ignore_errors=True)
    except Exception:
        # Pruning failures do not block creating the new dir
        pass

    try:
        return Path(tempfile.mkdtemp(prefix=LOG_DIR_PREFIX))
    except Exception:
        return None


def remove_log_dir(log_dir: Optional[Path]) -> None:
    """Delete a log directory once every check passed; best-effort, never raises."""
    if log_dir is None:
        return
    shutil.rmtree(log_dir, ignore_errors=True)


def build_failure_excerpt(stdout: str, stderr: str) -> str:
    """
    Short, size-bounded excerpt of combined output: lines matching
    FAILED|panicked|failures:|error if any (first EXCERPT_MAX_MATCH_LINES), otherwise the last
    EXCERPT_TAIL_LINES lines. Truncated to EXCERPT_MAX_CHARS. Redacts secrets before returning.
    Never raises.
    """
    try:
        combined = f"{stdout or ''}\n{stderr or ''}"
        # Redact secrets before processing
        combined = _redact(combined)
        lines = [ln.rstrip() for ln in combined.splitlines() if ln.strip()]
        matched = [ln for ln in lines if EXCERPT_LINE_PATTERN.search(ln)]
        chosen = matched[:EXCERPT_MAX_MATCH_LINES] if matched else lines[-EXCERPT_TAIL_LINES:]
        excerpt = "\n".join(chosen)
        if len(excerpt) > EXCERPT_MAX_CHARS:
            excerpt = excerpt[:EXCERPT_MAX_CHARS] + "...[truncated]"
        return excerpt
    except Exception:
        return ""


def _run_best_effort(argv: List[str], cwd: Optional[Path] = None) -> str:
    """Run a command and return its combined stdout/stderr; never raises; returns '<unavailable: {err}>' on failure."""
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=SNAPSHOT_CMD_TIMEOUT_SECS,
        )
        return (proc.stdout or "") + (proc.stderr or "")
    except Exception as e:
        return f"<unavailable: {e}>"


def environment_snapshot(main_worktree: Optional[Path]) -> str:
    """
    Best-effort snapshot: main HEAD SHA, git status, cargo/rustc/just processes, free disk.

    When main_worktree is None, git commands run with no explicit cwd (current working directory)
    and disk usage is checked against ".".
    """
    parts = []
    try:
        parts.append("--- main HEAD ---\n" + _redact(_run_best_effort(["git", "rev-parse", "HEAD"], main_worktree)))
        parts.append("--- git status --porcelain ---\n" + _redact(_run_best_effort(["git", "status", "--porcelain"], main_worktree)))
        ps_out = _run_best_effort(["ps", "-axo", "pid,etime,comm"])
        procs = [_redact(ln) for ln in ps_out.splitlines() if SNAPSHOT_PROCESS_PATTERN.search(ln)]
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
    attempt: int = 1,
    env_used: Optional[Mapping[str, str]] = None,
    dropped_env_names: Sequence[str] = (),
) -> Optional[Path]:
    """
    Write the full output of one check run to <log_dir>/check-<index>-attempt-<attempt>.log.
    Environment snapshot is appended only when the check failed. Returns the log path, or None
    if it could not be written. Never raises.

    The index parameter must be unique within a given log_dir across all attempts; attempt
    differentiates between retries of the same check. The filename is check-<index>-attempt-<attempt>.log.

    Args:
        log_dir: Log directory or None (returns None if None).
        index: Unique per-check index within the log_dir.
        cmd: The executed command.
        result: CheckResult from execute_check.
        started_at: Timestamp when the check started.
        duration_secs: Elapsed time in seconds.
        main_worktree: Path to main worktree (for snapshot), or None.
        attempt: Attempt number (default 1).
        env_used: Optional mapping of env variables actually passed to the subprocess (for diagnostics).
        dropped_env_names: Sequence of env variable names that were dropped (names only, never values).
    """
    if log_dir is None:
        return None
    try:
        path = log_dir / f"check-{index}-attempt-{attempt}.log"
        header = (
            f"command: {cmd}\n"
            f"started: {started_at.astimezone(timezone.utc).isoformat()}\n"
            f"duration_secs: {duration_secs:.2f}\n"
            f"exit_code: {result.returncode}\n"
            f"success: {result.success}\n"
            f"error: {result.error}\n"
        )
        # Redact secrets from stdout and stderr before writing to disk
        redacted_stdout = _redact(result.stdout or "")
        redacted_stderr = _redact(result.stderr or "")
        body = f"\n=== stdout ===\n{redacted_stdout}\n=== stderr ===\n{redacted_stderr}\n"
        snapshot = ""
        if not result.success:
            snapshot = "\n=== environment snapshot ===\n" + environment_snapshot(main_worktree)
            # Add env scrub information if provided
            if env_used is not None or dropped_env_names:
                snapshot += "\n=== environment scrub ===\n"
                if dropped_env_names:
                    snapshot += f"dropped variables: {', '.join(dropped_env_names)}\n"
                if env_used is not None:
                    # Show only a count for redaction purposes
                    snapshot += f"kept {len(env_used)} environment variables\n"
        path.write_text(header + body + snapshot, encoding="utf-8", errors="replace")
        return path
    except Exception:
        return None
