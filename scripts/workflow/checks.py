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
DEFAULT_CHECK_TIMEOUT_SECS = 300

# Shown when no source yields a check command and no checkOptOut is set. Callers and tests
# compare against this constant; the sources-tried list is appended after it.
NO_CHECK_CONFIGURED_MESSAGE = (
    "no check command configured — add commands.check to .claude/project.yaml "
    "(or run /shipit once to seed .claude/repo-cache.json); to opt out of the "
    "implement/fix check gate only, set checkOptOut: \"<reason>\" in .claude/project.yaml "
    "(an /expert-flow run will still stop at verify and ship, which require a check)"
)
OPT_OUT_MESSAGE_TEMPLATE = (
    "check opted out: {reason} (implement/fix gate only; verify and /shipit still require a check)"
)


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


def _read_project_yaml_mapping(path: Path) -> Optional[Dict[str, Any]]:
    """Load a project.yaml as a dict, or None if it is absent, unreadable, or not a mapping."""
    if yaml is None:
        return None
    try:
        with open(path, "r") as f:
            data = yaml.safe_load(f)
    except (yaml.YAMLError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _read_project_yaml_opt_out(path: Path) -> Optional[str]:
    """Return the stripped top-level checkOptOut reason, or None if unset/empty/non-string."""
    data = _read_project_yaml_mapping(path)
    if data is None:
        return None
    value = data.get("checkOptOut")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _valid_timeout(value: Any) -> Optional[int]:
    """Positive integers only; bool is an int subclass and is rejected explicitly."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _read_timeout_from_file(path: Path) -> Optional[int]:
    """Read checkTimeoutSecs from a project.yaml or repo-cache.json; None if absent or invalid."""
    if path.name == "project.yaml":
        data = _read_project_yaml_mapping(path)
        if data is None:
            return None
        return _valid_timeout(data.get("checkTimeoutSecs"))
    cache_data, read_err = read_repo_cache(path)
    if read_err or cache_data is None:
        return None
    return _valid_timeout(cache_data.check_timeout_secs)


def _file_label(path: Path, worktree: Path) -> str:
    """Human label for a config file: 'worktree <name>' or 'main <name>'."""
    where = "worktree" if path.parent.parent == worktree else "main"
    return f"{where} {path.name}"


@dataclass
class CheckResolution:
    """
    Detailed outcome of check resolution.

    source_file is the config file that supplied the command (None for the justfile
    recipe or when nothing resolved). opt_out_reason is set only when the check was
    explicitly opted out via checkOptOut and no command resolved.
    """
    command: Optional[str]
    source: Optional[str]
    source_file: Optional[Path]
    failure_reason: Optional[str]
    opt_out_reason: Optional[str] = None


def resolve_check_detail(worktree: Path, main_worktree: Optional[Path] = None) -> CheckResolution:
    """
    Resolve the check command with fallback logic, reporting the supplying file.

    Resolution order:
    1. Worktree repo-cache.json commands.check
    2. Main worktree repo-cache.json commands.check (if main_worktree provided)
    3. Worktree project.yaml commands.check
    4. Main worktree project.yaml commands.check (if main_worktree provided)
    5. Worktree justfile check recipe

    If all five fail, an explicit opt-out is consulted: a non-empty top-level
    checkOptOut in worktree project.yaml, then main project.yaml. A resolved command
    always wins over checkOptOut.

    Files are seeded from main at worktree creation, so the main fallback
    applies only when the worktree file has no check command (not just when
    the file is absent). This implements snapshot semantics: the seeded files
    at creation time are the source of truth for that worktree.

    Trust model: project.yaml is local, per-machine state (gitignored per
    CLAUDE.md) and is trusted the same as repo-cache.json. Both are
    repository-committer-controlled at their source.
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
                return CheckResolution(check_cmd.strip(), "repo-cache", cache_file, None)
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
                    return CheckResolution(check_cmd.strip(), "repo-cache", main_cache_file, None)
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
            return CheckResolution(check_cmd, "project.yaml", project_yaml, None)
        sources_tried.append(f"worktree project.yaml ({note})")
    else:
        sources_tried.append("worktree project.yaml (not found)")

    # 4. Try project.yaml in main worktree if worktree had no check
    if main_worktree:
        main_project_yaml = main_worktree / ".claude" / "project.yaml"
        if main_project_yaml.exists():
            check_cmd, note = _read_project_yaml_check(main_project_yaml)
            if check_cmd:
                return CheckResolution(check_cmd, "project.yaml", main_project_yaml, None)
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
                return CheckResolution("just check", "justfile", None, None)
            sources_tried[-1] += " (check recipe not found)"
        except subprocess.TimeoutExpired:
            sources_tried[-1] += " (timed out)"
        except FileNotFoundError:
            sources_tried[-1] += " (just not found)"
        except Exception as e:
            sources_tried[-1] += f" (error: {e})"
    else:
        sources_tried.append("justfile (not found)")

    # No command resolved: explicit opt-out (worktree first, then main) before failing closed.
    opt_out_reason = _read_project_yaml_opt_out(project_yaml)
    if opt_out_reason is None and main_worktree:
        opt_out_reason = _read_project_yaml_opt_out(main_worktree / ".claude" / "project.yaml")
    if opt_out_reason is not None:
        return CheckResolution(
            None,
            "opt-out",
            None,
            OPT_OUT_MESSAGE_TEMPLATE.format(reason=opt_out_reason),
            opt_out_reason,
        )

    # Resolution exhausted
    sources_str = "; tried " + ", ".join(sources_tried)
    return CheckResolution(None, None, None, NO_CHECK_CONFIGURED_MESSAGE + sources_str)


def resolve_check_command(
    worktree: Path, main_worktree: Optional[Path] = None
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Resolve the check command. Thin 3-tuple view of resolve_check_detail().

    Returns:
        (command, source, failure_reason) tuple:
        - command: resolved check command string, or None
        - source: "repo-cache", "project.yaml", "justfile", "opt-out", or None
        - failure_reason: descriptive reason if resolution failed (including opt-out), or None

    Note: a non-None failure_reason is returned for both opt-out and not-configured.
    Callers that must tell them apart read source ("opt-out") rather than inferring from failure.
    """
    resolution = resolve_check_detail(worktree, main_worktree)
    return resolution.command, resolution.source, resolution.failure_reason


def resolve_check_timeout(
    worktree: Path, main_worktree: Optional[Path], command_source_file: Optional[Path]
) -> Tuple[int, str]:
    """
    Resolve the per-check timeout in seconds, following the command's source.

    Order:
    1. SHIPIT_CHECK_TIMEOUT_SECS env var, if set (same override checks run honours)
    2. checkTimeoutSecs in the file the command was resolved from (project.yaml or repo-cache)
    3. checkTimeoutSecs in worktree repo-cache.json
    4. checkTimeoutSecs in main repo-cache.json
    5. 300

    Returns:
        (seconds, timeout_source) where timeout_source names where the value came from.
    """
    env_timeout = os.environ.get("SHIPIT_CHECK_TIMEOUT_SECS")
    if env_timeout:
        try:
            parsed_env: Optional[int] = _valid_timeout(int(env_timeout))
        except ValueError:
            parsed_env = None
        if parsed_env is not None:
            return parsed_env, "env:SHIPIT_CHECK_TIMEOUT_SECS"

    candidates: List[Path] = []
    if command_source_file is not None:
        candidates.append(command_source_file)
    candidates.append(worktree / ".claude" / "repo-cache.json")
    if main_worktree is not None:
        candidates.append(main_worktree / ".claude" / "repo-cache.json")

    seen = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        value = _read_timeout_from_file(path)
        if value is not None:
            return value, _file_label(path, worktree)
    return DEFAULT_CHECK_TIMEOUT_SECS, "default"
