"""
Check execution for /shipit, /merge-and-cleanup, and /cleanup.

Shared check ordering (CHECK_ORDER) prevents silent skips: every gate
verifies the same checks in the same order, and none can pass without
executing something (unless explicitly configured to skip all checks,
which is a hard error rather than a silent pass).
"""

import json
import os
import signal
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from .safety import Unknown, fail_closed


CHECK_ORDER = ("format", "check", "lint", "typecheck", "vet", "test", "build")
NON_CHECK_COMMANDS = frozenset({"install"})
SUPERSEDED_BY_CHECK = ("lint", "typecheck")


@dataclass
class SkippedCheckReason:
    """Reason a check was skipped."""
    command_type: str
    reason: str  # null_command, superseded_by_check, not_a_check, not_reached


def build_check_order(commands: Dict[str, Optional[str]]) -> Tuple[List[str], List[SkippedCheckReason]]:
    """
    Build the canonical check execution order.

    Pure function that determines which commands to run and which to skip,
    producing the single source of truth for all gates (shipit, merge, cleanup).

    Args:
        commands: Dict of command_type → command_string (from cache).

    Returns:
        (order, skipped) where:
        - order: list of command types to execute (non-null, in CHECK_ORDER)
        - skipped: list of SkippedCheckReason explaining each skipped command
    """
    order = []
    skipped = []
    extras = {}

    for cmd_type in CHECK_ORDER:
        if cmd_type not in commands:
            continue

        cmd_value = commands[cmd_type]

        if cmd_value is None:
            skipped.append(SkippedCheckReason(cmd_type, "null_command"))
        elif cmd_type in NON_CHECK_COMMANDS:
            skipped.append(SkippedCheckReason(cmd_type, "not_a_check"))
        elif cmd_type in SUPERSEDED_BY_CHECK and "check" in commands and commands["check"] is not None:
            skipped.append(SkippedCheckReason(cmd_type, "superseded_by_check"))
        else:
            order.append(cmd_type)

    for cmd_type in sorted(commands.keys()):
        if cmd_type not in CHECK_ORDER:
            cmd_value = commands[cmd_type]
            if cmd_value is None:
                skipped.append(SkippedCheckReason(cmd_type, "null_command"))
            elif cmd_type in NON_CHECK_COMMANDS:
                skipped.append(SkippedCheckReason(cmd_type, "not_a_check"))
            else:
                extras[cmd_type] = cmd_value

    if extras:
        if "test" in order:
            insert_idx = order.index("test") + 1
        else:
            insert_idx = len(order)
        for idx, cmd_type in enumerate(sorted(extras.keys())):
            order.insert(insert_idx + idx, cmd_type)

    return order, skipped


@dataclass
class CheckResult:
    """Result of executing a check command."""
    success: bool
    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None


@dataclass
class CheckStepResult:
    """Result of a single check step in the execution sequence."""
    command_type: str
    command: str
    success: bool
    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None


@dataclass
class CheckResults:
    """Results of executing all checks for a gate."""
    results: List[CheckStepResult] = field(default_factory=list)
    all_passed: bool = True
    failed_at: Optional[str] = None
    planned: List[str] = field(default_factory=list)
    executed: List[str] = field(default_factory=list)
    skipped: List[SkippedCheckReason] = field(default_factory=list)
    status: str = "passed"  # "passed", "failed", "no_checks_ran"

    def to_dict(self):
        """Convert to dict for JSON serialization."""
        return {
            "results": [asdict(r) for r in self.results],
            "all_passed": self.all_passed,
            "failed_at": self.failed_at,
            "planned": self.planned,
            "executed": self.executed,
            "skipped": [asdict(s) for s in self.skipped],
            "status": self.status
        }


def execute_check(cmd: str, cwd: Optional[Path], timeout: int = 300) -> CheckResult:
    """
    Execute a shell check command, capturing output. Never raises.

    Runs in its own process group so a timeout can kill the whole
    process tree (a check command that spawns children would otherwise
    orphan them on TimeoutExpired).
    """
    try:
        proc = subprocess.Popen(
            cmd, shell=True, cwd=cwd, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            return CheckResult(success=False, error=f"timed out after {timeout}s")
        return CheckResult(success=proc.returncode == 0, returncode=proc.returncode, stdout=stdout, stderr=stderr)
    except Exception as e:
        return CheckResult(success=False, error=str(e))


@fail_closed
def run_checks(
    commands: Dict[str, Optional[str]],
    repo_root: Path,
    timeout: int = 300
) -> Tuple[CheckResults, Optional[Unknown]]:
    """
    Execute checks in canonical order, stopping at first failure.

    Never reports success without executing at least one check.
    Cached check commands execute via shell=True (intentional for shell syntax like && and pipes).
    Trust boundary: .claude/repo-cache.json content is repo-committer-controlled, not PR/attacker input.

    Args:
        commands: Dict of command_type → command_string (from cache).
        repo_root: Path to repo root for cwd of executed commands.
        timeout: Timeout per command in seconds (default 300).

    Returns:
        (CheckResults, None) on normal execution (pass, fail, or no_checks_ran).
        (CheckResults, Unknown(...)) if coverage assertion fails (executed ∪ skipped != non-null set).
    """
    results = CheckResults()
    planned_order, skip_reasons = build_check_order(commands)
    results.planned = planned_order
    results.skipped = skip_reasons

    non_null_keys = {k for k, v in commands.items() if v is not None}

    for cmd_type in planned_order:
        cmd = commands[cmd_type]
        check_result = execute_check(cmd, cwd=repo_root, timeout=timeout)

        results.executed.append(cmd_type)
        step_result = CheckStepResult(
            command_type=cmd_type,
            command=cmd,
            success=check_result.success,
            returncode=check_result.returncode,
            stdout=check_result.stdout,
            stderr=check_result.stderr,
            error=check_result.error
        )
        results.results.append(step_result)

        if not check_result.success:
            results.all_passed = False
            results.failed_at = cmd_type
            results.status = "failed"
            not_reached = planned_order[planned_order.index(cmd_type) + 1:]
            results.skipped.extend(
                SkippedCheckReason(remaining, "not_reached") for remaining in not_reached
            )
            break

    if not results.executed:
        results.all_passed = False
        results.status = "no_checks_ran"
        reason = "empty_cache" if not non_null_keys else "all_commands_null"
        return results, Unknown(f"run_checks: {reason}")

    executed_or_skipped = set(results.executed) | {s.command_type for s in results.skipped}
    if executed_or_skipped != non_null_keys:
        return results, Unknown(
            f"run_checks: coverage violation — executed/skipped {executed_or_skipped} != non-null {non_null_keys}"
        )

    if results.status == "passed":
        results.status = "passed"

    return results, None
