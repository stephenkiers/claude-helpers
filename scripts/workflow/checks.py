"""
Check execution for /shipit, /merge-and-cleanup, and /cleanup.

Shared check ordering (CHECK_ORDER) prevents silent skips: every gate
verifies the same checks in the same order, and none can pass without
executing something (unless explicitly configured to skip all checks,
which is a hard error rather than a silent pass).
"""

import os
import signal
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any, Mapping

from .safety import Unknown, fail_closed
from .cache import read_repo_cache

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:
    yaml = None  # type: ignore[assignment]


CHECK_ORDER = ("format", "check", "lint", "typecheck", "vet", "test", "build")
NON_CHECK_COMMANDS = frozenset({"install"})
SUPERSEDED_BY_CHECK = ("lint", "typecheck")
TIMEOUT_ERROR_PREFIX = "timed out after"


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

    def to_dict(self) -> Dict[str, Any]:
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


def execute_check(cmd: str, cwd: Optional[Path], timeout: int = 300, env: Optional[Mapping[str, str]] = None) -> CheckResult:
    """
    Execute a shell check command, capturing output. Never raises.

    Runs in its own process group so a timeout can kill the whole
    process tree (a check command that spawns children would otherwise
    orphan them on TimeoutExpired).

    Args:
        cmd: Shell command to execute.
        cwd: Working directory for the command.
        timeout: Timeout per command in seconds (default 300).
        env: Optional environment variables for the subprocess. This is isolation, not a sandbox:
             check commands run arbitrary repo code either way. With None, the process inherits
             the caller env exactly as today.
    """
    try:
        proc = subprocess.Popen(
            cmd, shell=True, cwd=cwd, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
            env=env,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            return CheckResult(success=False, error=f"{TIMEOUT_ERROR_PREFIX} {timeout}s")
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
        (CheckResults, Unknown(...)) if coverage assertion fails (executed ∪ skipped != the full
        set of configured command keys — null-valued keys included, since build_check_order
        records a null_command skip entry for every key present in `commands`, not just non-null
        ones).
    """
    results = CheckResults()
    planned_order, skip_reasons = build_check_order(commands)
    results.planned = planned_order
    results.skipped = skip_reasons

    non_null_keys = {k for k, v in commands.items() if v is not None}

    for cmd_type in planned_order:
        cmd = commands[cmd_type]
        assert cmd is not None  # planned_order only includes non-null commands
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

    all_keys = set(commands.keys())
    executed_or_skipped = set(results.executed) | {s.command_type for s in results.skipped}
    if executed_or_skipped != all_keys:
        return results, Unknown(
            f"run_checks: coverage violation — executed/skipped {executed_or_skipped} != configured {all_keys}"
        )

    if results.status == "passed":
        results.status = "passed"

    return results, None


def _read_project_yaml_check(path: Path) -> Tuple[Optional[str], Optional[str]]:
    """
    Read commands.check from a project.yaml file.

    Returns:
        (command, note) where command is the check string (or None),
        and note is a failure reason string (or None on success).
    """
    if yaml is None:
        return None, "PyYAML not installed"

    try:
        with open(path, "r") as f:
            project_data = yaml.safe_load(f)
            if isinstance(project_data, dict):
                commands = project_data.get("commands", {})
                if isinstance(commands, dict):
                    check_cmd = commands.get("check")
                    if isinstance(check_cmd, str) and check_cmd.strip():
                        return check_cmd.strip(), None
        return None, "no commands.check found in project.yaml"
    except yaml.YAMLError as e:
        return None, f"malformed YAML: {e}"
    except OSError as e:
        return None, f"read error: {e}"


def resolve_check_command(
    worktree: Path, main_worktree: Optional[Path] = None
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Resolve the check command with fallback logic.

    Resolution order:
    1. Worktree repo-cache.json commands.check
    2. Main worktree repo-cache.json commands.check (if main_worktree provided)
    3. Worktree project.yaml commands.check
    4. Main worktree project.yaml commands.check (if main_worktree provided)
    5. Worktree justfile check recipe

    Files are seeded from main at worktree creation, so the main fallback
    applies only when the worktree file has no check command (not just when
    the file is absent). This implements snapshot semantics: the seeded files
    at creation time are the source of truth for that worktree.

    Trust model: project.yaml is local, per-machine state (gitignored per
    CLAUDE.md) and is trusted the same as repo-cache.json. Both are
    repository-committer-controlled at their source.

    Args:
        worktree: Path to the worktree to check
        main_worktree: Optional path to main worktree (for fallback); if
                      provided, falls back to main when worktree has no check

    Returns:
        (command, source, failure_reason) tuple:
        - command: resolved check command string, or None
        - source: source ("repo-cache", "project.yaml", "justfile"), or None
        - failure_reason: descriptive reason if resolution failed, or None
    """
    sources_tried = []

    # 1. Try repo-cache.json in worktree
    cache_file = worktree / ".claude" / "repo-cache.json"
    if cache_file.exists():
        cache_data, read_err = read_repo_cache(cache_file)
        sources_tried.append("worktree repo-cache.json")
        if not read_err and cache_data:
            check_cmd = cache_data.commands.get("check")
            if isinstance(check_cmd, str) and check_cmd.strip():
                return check_cmd.strip(), "repo-cache", None
            else:
                sources_tried[-1] += " (no check key)"
        else:
            sources_tried[-1] += f" ({read_err})" if read_err else " (validation failed)"
    else:
        sources_tried.append("worktree repo-cache.json (not found)")

    # 2. Try repo-cache.json in main worktree if worktree had no check
    if main_worktree:
        main_cache_file = main_worktree / ".claude" / "repo-cache.json"
        if main_cache_file.exists():
            cache_data, read_err = read_repo_cache(main_cache_file)
            sources_tried.append("main repo-cache.json")
            if not read_err and cache_data:
                check_cmd = cache_data.commands.get("check")
                if isinstance(check_cmd, str) and check_cmd.strip():
                    return check_cmd.strip(), "repo-cache", None
                else:
                    sources_tried[-1] += " (no check key)"
            else:
                sources_tried[-1] += f" ({read_err})" if read_err else " (validation failed)"
        else:
            sources_tried.append("main repo-cache.json (not found)")

    # 3. Try project.yaml in worktree
    project_yaml = worktree / ".claude" / "project.yaml"
    if project_yaml.exists():
        check_cmd, note = _read_project_yaml_check(project_yaml)
        if check_cmd:
            return check_cmd, "project.yaml", None
        sources_tried.append(f"worktree project.yaml ({note})")
    else:
        sources_tried.append("worktree project.yaml (not found)")

    # 4. Try project.yaml in main worktree if worktree had no check
    if main_worktree:
        main_project_yaml = main_worktree / ".claude" / "project.yaml"
        if main_project_yaml.exists():
            check_cmd, note = _read_project_yaml_check(main_project_yaml)
            if check_cmd:
                return check_cmd, "project.yaml", None
            sources_tried.append(f"main project.yaml ({note})")
        else:
            sources_tried.append("main project.yaml (not found)")

    # 5. Try justfile check recipe
    justfile = worktree / "justfile"
    if justfile.exists():
        sources_tried.append("justfile check")
        try:
            summary = subprocess.run(
                ["just", "-f", str(justfile), "--summary"],
                cwd=str(worktree), capture_output=True, text=True, timeout=5
            )
            if summary.returncode == 0 and "check" in summary.stdout.split():
                return "just check", "justfile", None
            sources_tried[-1] += " (check recipe not found)"
        except subprocess.TimeoutExpired:
            sources_tried[-1] += " (timed out)"
        except FileNotFoundError:
            sources_tried[-1] += " (just not found)"
        except Exception as e:
            sources_tried[-1] += f" (error: {e})"
    else:
        sources_tried.append("justfile (not found)")

    # Resolution exhausted
    sources_str = "; tried " + ", ".join(sources_tried)
    failure_msg = (
        "no check command configured — run /shipit once or add commands.check to "
        ".claude/project.yaml (commands.check only per ADR-0023 Decision 4)" + sources_str
    )
    return None, None, failure_msg
