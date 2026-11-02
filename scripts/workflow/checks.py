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
from typing import List, Dict, Optional, Tuple, Any, Mapping, Final, Literal

from .safety import Unknown, fail_closed
from .cache import read_repo_cache
from .models import validate_repo_cache, valid_timeout_secs  # noqa: F401  (re-exported)

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:
    yaml = None  # type: ignore[assignment]


CHECK_ORDER = ("format", "check", "lint", "typecheck", "vet", "test", "build")
NON_CHECK_COMMANDS = frozenset({"install"})
SUPERSEDED_BY_CHECK = ("lint", "typecheck")
TIMEOUT_ERROR_PREFIX = "timed out after"
DEFAULT_CHECK_TIMEOUT_SECS: Final = 300
# The source value recorded when an explicit checkOptOut applies. Callers compare against this
# constant (flow-verify-receipt.py does), never against a bare "opt-out" literal.
OPT_OUT_SOURCE: Final = "opt-out"

# The config file kinds that may carry checkTimeoutSecs or commands.check. Explicit so that the
# parser is chosen by kind, not by inspecting the file name.
ConfigKind = Literal["project.yaml", "repo-cache"]
_CONFIG_FILENAME: Dict[str, str] = {"project.yaml": "project.yaml", "repo-cache": "repo-cache.json"}
# Whether a config file came from the step's own worktree or from the main worktree fallback.
ConfigScope = Literal["worktree", "main"]

# Shown when no source yields a check command and no checkOptOut is set. Callers and tests
# compare against this constant; the "sources tried" line is appended after it on its own line.
NO_CHECK_CONFIGURED_MESSAGE = (
    "no check command configured — add commands.check to .claude/project.yaml\n"
    "(or run /shipit once to seed .claude/repo-cache.json).\n"
    "To opt out of the implement/fix check gate only, set checkOptOut: \"<reason>\" "
    "in .claude/project.yaml.\n"
    "An /expert-flow run will still stop at verify and ship, which require a check."
)
CHECK_OPT_OUT_INVALID_NOTE = (
    "note: checkOptOut must be a non-empty string; the invalid value was ignored"
)
NO_CHECK_KEY_NOTE = "no commands.check found in project.yaml"
REPO_CACHE_NO_KEY_SUFFIX = " (no check key)"
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


def env_timeout_secs() -> Optional[int]:
    """SHIPIT_CHECK_TIMEOUT_SECS as a valid positive int, or None when unset or invalid. Never raises."""
    raw = os.environ.get("SHIPIT_CHECK_TIMEOUT_SECS")
    if not raw:
        return None
    try:
        return valid_timeout_secs(int(raw))
    except ValueError:
        return None


def execute_check(
    cmd: str, cwd: Optional[Path], timeout: int = DEFAULT_CHECK_TIMEOUT_SECS, env: Optional[Mapping[str, str]] = None
) -> CheckResult:
    """
    Execute a shell check command, capturing output. Never raises.

    Runs in its own process group so a timeout can kill the whole
    process tree (a check command that spawns children would otherwise
    orphan them on TimeoutExpired).

    Args:
        cmd: Shell command to execute.
        cwd: Working directory for the command.
        timeout: Timeout per command in seconds (default DEFAULT_CHECK_TIMEOUT_SECS).
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
            return CheckResult(
                success=False,
                error=(
                    f"{TIMEOUT_ERROR_PREFIX} {timeout}s "
                    "(checkTimeoutSecs or SHIPIT_CHECK_TIMEOUT_SECS sets the limit)"
                ),
            )
        return CheckResult(success=proc.returncode == 0, returncode=proc.returncode, stdout=stdout, stderr=stderr)
    except Exception as e:
        return CheckResult(success=False, error=str(e))


@fail_closed
def run_checks(
    commands: Dict[str, Optional[str]],
    repo_root: Path,
    timeout: int = DEFAULT_CHECK_TIMEOUT_SECS
) -> Tuple[CheckResults, Optional[Unknown]]:
    """
    Execute checks in canonical order, stopping at first failure.

    Never reports success without executing at least one check.
    Cached check commands execute via shell=True (intentional for shell syntax like && and pipes).
    Trust boundary: .claude/repo-cache.json content is repo-committer-controlled, not PR/attacker input.

    Args:
        commands: Dict of command_type → command_string (from cache).
        repo_root: Path to repo root for cwd of executed commands.
        timeout: Timeout per command in seconds (default DEFAULT_CHECK_TIMEOUT_SECS).

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


def _load_project_yaml(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Parse a project.yaml once, returning (mapping, note).

    mapping is None when the file is absent, unreadable, malformed, or PyYAML is missing; note then
    says why. A file that parses to something other than a mapping yields an empty mapping.
    """
    if yaml is None:
        return None, "PyYAML not installed"
    try:
        with open(path, "r") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        return None, f"malformed YAML: {e}"
    except OSError as e:
        return None, f"read error: {e}"
    return (data if isinstance(data, dict) else {}), None


def _check_command_from_mapping(data: Optional[Dict[str, Any]]) -> Optional[str]:
    """The stripped commands.check string from a parsed project.yaml, or None."""
    if data is None:
        return None
    commands = data.get("commands", {})
    if isinstance(commands, dict):
        check_cmd = commands.get("check")
        if isinstance(check_cmd, str) and check_cmd.strip():
            return check_cmd.strip()
    return None


def _opt_out_from_mapping(data: Dict[str, Any]) -> Tuple[Optional[str], bool]:
    """
    Read the top-level checkOptOut from a parsed project.yaml.

    Returns (reason, invalid). reason is the non-empty string with internal whitespace and newlines
    collapsed to single spaces, so it stays on one line; or None. invalid is True when checkOptOut is
    set (not null) but is not a non-empty string; that value is ignored.
    """
    if data.get("checkOptOut") is None:
        return None, False
    value = data["checkOptOut"]
    if isinstance(value, str) and value.strip():
        return " ".join(value.split()), False
    return None, True


def _resolve_opt_out(
    worktree_yaml: Optional[Dict[str, Any]], main_yaml: Optional[Dict[str, Any]]
) -> Tuple[Optional[str], bool]:
    """
    Return (reason, invalid) for an explicit checkOptOut, worktree project.yaml first, then main.

    reason is the first valid reason found, or None. invalid is True when any consulted mapping set
    checkOptOut to a value that is not a non-empty string.
    """
    reason: Optional[str] = None
    invalid = False
    for mapping in (worktree_yaml, main_yaml):
        if mapping is None:
            continue
        mapping_reason, mapping_invalid = _opt_out_from_mapping(mapping)
        invalid = invalid or mapping_invalid
        if reason is None:
            reason = mapping_reason
    return reason, invalid


def _load_repo_cache_mapping(path: Path) -> Optional[Dict[str, Any]]:
    """Parse and schema-check a repo-cache.json as a raw mapping; None if absent, unreadable, or invalid."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not validate_repo_cache(data):
        return None
    return data


def _read_timeout_value(path: Path, kind: ConfigKind) -> Tuple[Optional[int], bool]:
    """
    Read checkTimeoutSecs from one config file, parsed according to its explicit kind.

    Returns (value, ignored). value is the valid timeout, or None. ignored is True when the key is
    set (not null) but invalid under valid_timeout_secs, so the caller can name the ignored file.
    An absent, unreadable, or malformed file yields (None, False).
    """
    if kind == "project.yaml":
        data, _note = _load_project_yaml(path)
    else:
        data = _load_repo_cache_mapping(path)
    if data is None:
        return None, False
    raw = data.get("checkTimeoutSecs")
    if raw is None:
        return None, False
    value = valid_timeout_secs(raw)
    return value, value is None


@dataclass
class CheckResolution:
    """
    Detailed outcome of check resolution.

    source_file is the config file that supplied the command (None for the justfile
    recipe or when nothing resolved). opt_out_reason is set only when the check was
    explicitly opted out via checkOptOut and no command resolved.

    Invariants (enforced in __post_init__):
    - source == OPT_OUT_SOURCE iff an opt-out applies: then command is None, opt_out_reason and
      failure_reason are set.
    - opt_out_reason is set only with source OPT_OUT_SOURCE.
    - a command is set only with a real source (not None, not OPT_OUT_SOURCE) and no failure_reason.
    """
    command: Optional[str]
    source: Optional[str]
    source_file: Optional[Path]
    failure_reason: Optional[str]
    opt_out_reason: Optional[str] = None
    # "worktree" or "main" when source_file is set; None otherwise (justfile, opt-out, not configured).
    source_scope: Optional[ConfigScope] = None

    def __post_init__(self) -> None:
        if self.source == OPT_OUT_SOURCE:
            if self.command is not None or self.opt_out_reason is None or self.failure_reason is None:
                raise ValueError("opt-out resolution needs an opt_out_reason and failure_reason, and no command")
        else:
            if self.opt_out_reason is not None:
                raise ValueError("opt_out_reason is only valid with source 'opt-out'")
            if self.command is not None and (self.source is None or self.failure_reason is not None):
                raise ValueError("a resolved command needs a real source and no failure_reason")


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
    always wins over checkOptOut. A checkOptOut that is set but is not a non-empty string
    is ignored, and the not-configured failure then carries a note saying so.

    Each project.yaml is parsed once and its mapping is reused for the command and the opt-out.

    Files are seeded from main at worktree creation, so the main fallback
    applies only when the worktree file has no check command (not just when
    the file is absent). This implements snapshot semantics: the seeded files
    at creation time are the source of truth for that worktree.

    Trust model: project.yaml is local, per-machine state (gitignored per
    CLAUDE.md) and is trusted the same as repo-cache.json. Both are
    repository-committer-controlled at their source.
    """
    sources_tried: List[str] = []

    # 1. Try repo-cache.json in worktree
    cache_file = worktree / ".claude" / "repo-cache.json"
    if cache_file.exists():
        cache_data, read_err = read_repo_cache(cache_file)
        sources_tried.append("worktree repo-cache.json")
        if not read_err and cache_data:
            check_cmd = cache_data.commands.get("check")
            if isinstance(check_cmd, str) and check_cmd.strip():
                return CheckResolution(check_cmd.strip(), "repo-cache", cache_file, None, source_scope="worktree")
            else:
                sources_tried[-1] += REPO_CACHE_NO_KEY_SUFFIX
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
                    return CheckResolution(check_cmd.strip(), "repo-cache", main_cache_file, None, source_scope="main")
                else:
                    sources_tried[-1] += REPO_CACHE_NO_KEY_SUFFIX
            else:
                sources_tried[-1] += f" ({read_err})" if read_err else " (validation failed)"
        else:
            sources_tried.append("main repo-cache.json (not found)")

    # 3. Try project.yaml in worktree (parsed once; the mapping is reused for the opt-out below)
    project_yaml = worktree / ".claude" / "project.yaml"
    worktree_yaml: Optional[Dict[str, Any]] = None
    if project_yaml.exists():
        worktree_yaml, load_note = _load_project_yaml(project_yaml)
        check_cmd = _check_command_from_mapping(worktree_yaml)
        if check_cmd:
            return CheckResolution(check_cmd, "project.yaml", project_yaml, None, source_scope="worktree")
        sources_tried.append(f"worktree project.yaml ({load_note or NO_CHECK_KEY_NOTE})")
    else:
        sources_tried.append("worktree project.yaml (not found)")

    # 4. Try project.yaml in main worktree if worktree had no check
    main_yaml: Optional[Dict[str, Any]] = None
    if main_worktree:
        main_project_yaml = main_worktree / ".claude" / "project.yaml"
        if main_project_yaml.exists():
            main_yaml, load_note = _load_project_yaml(main_project_yaml)
            check_cmd = _check_command_from_mapping(main_yaml)
            if check_cmd:
                return CheckResolution(check_cmd, "project.yaml", main_project_yaml, None, source_scope="main")
            sources_tried.append(f"main project.yaml ({load_note or NO_CHECK_KEY_NOTE})")
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
    opt_out_reason, opt_out_invalid = _resolve_opt_out(worktree_yaml, main_yaml)
    if opt_out_reason is not None:
        return CheckResolution(
            None,
            OPT_OUT_SOURCE,
            None,
            OPT_OUT_MESSAGE_TEMPLATE.format(reason=opt_out_reason),
            opt_out_reason,
        )

    # Resolution exhausted: the sources tried go on their own line.
    failure = NO_CHECK_CONFIGURED_MESSAGE + "\nsources tried: " + ", ".join(sources_tried)
    if opt_out_invalid:
        failure += "\n" + CHECK_OPT_OUT_INVALID_NOTE
    return CheckResolution(None, None, None, failure)


def resolve_check_command(
    worktree: Path, main_worktree: Optional[Path] = None
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Resolve the check command. Thin 3-tuple view of resolve_check_detail().

    Args:
        worktree: Worktree whose .claude/ files and justfile are consulted first.
        main_worktree: Main worktree used as the fallback source (optional).

    Returns:
        (command, source, failure_reason) tuple:
        - command: resolved check command string, or None
        - source: "repo-cache", "project.yaml", "justfile", OPT_OUT_SOURCE ("opt-out"), or None
        - failure_reason: descriptive reason if resolution failed (including opt-out), or None

    Opt-out invariant: when an explicit checkOptOut applies and no command resolves, source is
    "opt-out", command is None, and failure_reason is the opt-out message. A resolved command always
    wins, so "opt-out" never appears alongside a command.

    Note: a non-None failure_reason is returned for both opt-out and not-configured.
    Callers that must tell them apart read source ("opt-out") rather than inferring from failure.
    """
    resolution = resolve_check_detail(worktree, main_worktree)
    return resolution.command, resolution.source, resolution.failure_reason


def _with_ignored(base: str, ignored: List[str]) -> str:
    """Append the config files whose invalid checkTimeoutSecs was ignored, if any."""
    if not ignored:
        return base
    return f"{base} (ignored invalid checkTimeoutSecs in {', '.join(ignored)})"


def resolve_check_timeout(
    worktree: Path,
    main_worktree: Optional[Path],
    command_source_file: Optional[Path],
    command_source_kind: ConfigKind,
    command_source_scope: Optional[ConfigScope],
) -> Tuple[int, str]:
    """
    Resolve the per-check timeout in seconds, following the command's source.

    Order:
    1. SHIPIT_CHECK_TIMEOUT_SECS env var, if set and valid (same override checks run honours)
    2. checkTimeoutSecs in the file the command was resolved from (project.yaml or repo-cache)
    3. checkTimeoutSecs in worktree repo-cache.json
    4. checkTimeoutSecs in main repo-cache.json
    5. DEFAULT_CHECK_TIMEOUT_SECS (300)

    A value is used only if valid_timeout_secs accepts it. Anything else (true, "900", 0, negative,
    float, list) is ignored and the next source is tried. The env var is ignored silently on the
    same rule.

    Args:
        worktree: Worktree whose repo-cache.json is consulted.
        main_worktree: Main worktree whose repo-cache.json is the last fallback (optional).
        command_source_file: The file the command was resolved from, or None.
        command_source_kind: The kind of command_source_file ("project.yaml" or "repo-cache").
            Required: the parser is chosen by kind, never by the file name.
        command_source_scope: "worktree" or "main", the scope the command's source file came from
            (CheckResolution.source_scope). Required when command_source_file is set; None otherwise.

    Returns:
        (seconds, timeout_source) where timeout_source names where the value came from. When a
        config file set checkTimeoutSecs to an invalid value and it was ignored, that file is named,
        e.g. "default (ignored invalid checkTimeoutSecs in worktree repo-cache.json)".
    """
    parsed_env = env_timeout_secs()
    if parsed_env is not None:
        return parsed_env, "env:SHIPIT_CHECK_TIMEOUT_SECS"

    candidates: List[Tuple[Path, str, ConfigKind]] = []
    if command_source_file is not None:
        if command_source_scope is None:
            raise ValueError("command_source_scope is required when command_source_file is set")
        command_filename = _CONFIG_FILENAME[command_source_kind]
        candidates.append(
            (command_source_file, f"{command_source_scope} {command_filename}", command_source_kind)
        )
    candidates.append((worktree / ".claude" / "repo-cache.json", "worktree repo-cache.json", "repo-cache"))
    if main_worktree is not None:
        candidates.append((main_worktree / ".claude" / "repo-cache.json", "main repo-cache.json", "repo-cache"))

    ignored: List[str] = []
    seen = set()
    for path, label, kind in candidates:
        if path in seen:
            continue
        seen.add(path)
        value, was_ignored = _read_timeout_value(path, kind)
        if was_ignored:
            ignored.append(label)
        if value is not None:
            return value, _with_ignored(label, ignored)
    return DEFAULT_CHECK_TIMEOUT_SECS, _with_ignored("default", ignored)


# --- Check receipt: lets /merge-and-cleanup trust the gate /shipit just passed ---------------------
#
# /shipit records a check attestation before running checks, capturing the tree of the full non-ignored
# working state via a temp git index. After checks pass, write_check_receipt validates the attestation
# and writes .claude/check-receipt.json. /merge-and-cleanup's repo-cache gate skips re-running the
# checks only when the receipt still describes exactly what would be merged: same HEAD tree, same
# configured commands, and the base branch tip has not moved since (a moved base means a rebase may
# be needed, which changes the tree the checks would see). Any doubt -- missing/corrupt attestation,
# stale receipt, fetch failure, differing value -- means "not fresh", and the caller runs the checks
# as before. Both files are gitignored per-worktree state, the same trust tier as repo-cache.json.

ATTESTATION_RELPATH: Final = Path(".claude") / "check-attestation.json"
ATTESTATION_SCHEMA_VERSION: Final = 1
RECEIPT_RELPATH: Final = Path(".claude") / "check-receipt.json"
RECEIPT_SCHEMA_VERSION: Final = 1


def worktree_tree(worktree: Path) -> Optional[str]:
    """
    Get the tree of the full non-ignored working state without touching the real index.

    Creates a temp git index file, reads the current HEAD tree, stages all changes via
    `git add -A`, and captures the result tree with `git write-tree`. Deletes the temp
    file on exit. Returns the tree SHA (40-hex), or None on any error.
    """
    import tempfile

    try:
        # Create temp index file
        fd, tmp_index_path = tempfile.mkstemp(prefix="git-index-", dir=worktree / ".git")
        os.close(fd)

        try:
            env = os.environ.copy()
            env["GIT_INDEX_FILE"] = str(tmp_index_path)

            # Seed temp index with HEAD
            subprocess.run(
                ["git", "read-tree", "HEAD"],
                cwd=worktree,
                env=env,
                capture_output=True,
                check=True,
            )

            # Stage all changes
            subprocess.run(
                ["git", "add", "-A"],
                cwd=worktree,
                env=env,
                capture_output=True,
                check=True,
            )

            # Get tree SHA
            result = subprocess.run(
                ["git", "write-tree"],
                cwd=worktree,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()
        finally:
            # Clean up temp file
            try:
                os.remove(tmp_index_path)
            except OSError:
                pass
    except Exception:
        return None


def _commands_digest(commands: Mapping[str, Optional[str]]) -> str:
    import hashlib

    canonical = json.dumps({k: commands[k] for k in sorted(commands)}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _resolve_sha(rev: str, worktree: Path) -> Optional[str]:
    from . import git

    try:
        return git.run_git_command(
            ["--no-optional-locks", "rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}"],
            cwd=worktree,
        )
    except Exception:
        return None


def record_check_attestation(
    worktree: Path, commands: Mapping[str, Optional[str]], base_ref: str, result: Optional[CheckResults] = None
) -> Tuple[bool, Optional[Unknown]]:
    """
    Record a check attestation capturing pre-run and post-run trees and result status.

    Captures the working-state tree before checks run, resolves base_ref SHA, runs the
    checks, and captures the tree after. Writes attestation with passed=True only if all
    passed and the tree was unchanged. Always overwrites any prior attestation at the start.

    Returns (True, None) on successful attestation write, (False, Unknown(...)) on failure.
    """
    import time

    try:
        path = worktree / ATTESTATION_RELPATH
        path.parent.mkdir(parents=True, exist_ok=True)

        pending: Optional[Dict[str, Any]] = None
        if result is not None:
            # Finishing call: the pending attestation written before the run holds the gate-time tree and base.
            try:
                loaded = json.loads(path.read_text())
                pending = loaded if isinstance(loaded, dict) else None
            except (OSError, ValueError):
                pending = None

        # Always remove any prior attestation so a crashed or interrupted run leaves none that passed
        try:
            path.unlink()
        except FileNotFoundError:
            pass

        tree_now = worktree_tree(worktree)
        if result is None:
            tree_before = tree_now
            base_sha = _resolve_sha(base_ref, worktree)
            passed = False
        else:
            tree_before = pending.get("tree") if pending else None
            base_sha = pending.get("base_sha") if pending else None
            passed = bool(
                result.all_passed
                and result.status == "passed"
                and tree_before
                and tree_before == tree_now
                and base_sha
            )

        attestation = {
            "schema_version": ATTESTATION_SCHEMA_VERSION,
            "tree": tree_before or "",
            "base_ref": base_ref,
            "base_sha": base_sha or "",
            "commands_digest": _commands_digest(commands),
            "passed": passed,
            "written_at": int(time.time()),
        }

        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(attestation, indent=2) + "\n")
        os.replace(tmp, path)
        return True, None
    except OSError as e:
        return False, Unknown(f"record_check_attestation: {e}")
    except Exception as e:
        return False, Unknown(f"record_check_attestation: {e}")


def write_check_receipt(
    worktree: Path, commands: Mapping[str, Optional[str]]
) -> Tuple[bool, Optional[Unknown]]:
    """
    Record that checks passed, reading values from the attestation.

    Refuses unless the attestation exists, passed is True, tree and base_sha are non-empty
    strings, commands_digest matches current, and HEAD tree matches the attested tree.

    Returns (True, None) on success, (False, Unknown(...)) on refusal.
    """
    import time
    from . import git

    path = worktree / ATTESTATION_RELPATH
    try:
        attestation = json.loads(path.read_text())
    except FileNotFoundError:
        return False, Unknown("write_check_receipt: no check attestation")
    except (OSError, ValueError):
        return False, Unknown("write_check_receipt: unreadable or malformed attestation")

    # Validate attestation schema and content
    if not isinstance(attestation, dict) or attestation.get("schema_version") != ATTESTATION_SCHEMA_VERSION:
        return False, Unknown("write_check_receipt: unrecognized attestation schema")

    if not attestation.get("passed"):
        return False, Unknown("write_check_receipt: attested run did not pass")

    tree = attestation.get("tree")
    base_sha = attestation.get("base_sha")
    if not isinstance(tree, str) or not tree or not isinstance(base_sha, str) or not base_sha:
        return False, Unknown("write_check_receipt: attestation has empty or missing tree/base_sha")

    if attestation.get("commands_digest") != _commands_digest(commands):
        return False, Unknown("write_check_receipt: configured check commands changed since attestation")

    head_tree = git.tree_of("HEAD", worktree)
    if head_tree != tree:
        return False, Unknown("write_check_receipt: HEAD tree differs from attested tree")

    # Write receipt from attestation values
    base_ref = attestation.get("base_ref", "")
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "tree": tree,
        "base_ref": base_ref,
        "base_sha": base_sha,
        "commands_digest": attestation.get("commands_digest", ""),
        "written_at": int(time.time()),
    }
    try:
        receipt_path = worktree / RECEIPT_RELPATH
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = receipt_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(receipt, indent=2) + "\n")
        os.replace(tmp, receipt_path)
    except OSError as e:
        return False, Unknown(f"write_check_receipt: {e}")
    return True, None


def check_receipt_fresh(
    worktree: Path, commands: Mapping[str, Optional[str]], fetch: bool = True
) -> Tuple[bool, str]:
    """
    Return (True, "") if the receipt still covers what would merge, else (False, reason).

    Args:
        worktree: Worktree directory.
        commands: Current configured check commands.
        fetch: If True, fetch base_ref before checking; returns not-fresh on fetch failure.
               If False, use the local ref as-is.

    Returns (False, reason) if the receipt is unreadable, corrupt, has empty tree/base_sha,
    commands differ, HEAD tree differs, or (if fetch=True) cannot be fetched or the base moved.
    """
    from . import git

    path = worktree / RECEIPT_RELPATH
    try:
        receipt = json.loads(path.read_text())
    except FileNotFoundError:
        return False, "no check receipt"
    except (OSError, ValueError):
        return False, "unreadable or malformed check receipt"
    if not isinstance(receipt, dict) or receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        return False, "unrecognized check receipt schema"

    # Require non-empty tree and base_sha
    tree = receipt.get("tree")
    base_sha = receipt.get("base_sha")
    if not isinstance(tree, str) or not tree:
        return False, "receipt has empty or missing tree"
    if not isinstance(base_sha, str) or not base_sha:
        return False, "receipt has empty or missing base_sha"

    base_ref = receipt.get("base_ref")
    if not isinstance(base_ref, str) or not base_ref:
        return False, "receipt has no base_ref"

    if fetch and "/" in base_ref:
        remote, _, branch = base_ref.partition("/")
        try:
            git.run_git_command(["fetch", remote, branch], cwd=worktree)
        except Exception:
            return False, f"could not fetch {base_ref}"

    if receipt.get("commands_digest") != _commands_digest(commands):
        return False, "configured check commands changed since the receipt"

    head_tree = git.tree_of("HEAD", worktree)
    if head_tree != tree:
        return False, "HEAD tree differs from the checked tree"

    resolved_base_sha = _resolve_sha(base_ref, worktree)
    if resolved_base_sha is None:
        return False, f"could not resolve {base_ref}"
    if resolved_base_sha != base_sha:
        return False, f"{base_ref} moved since the checks ran (rebase may be needed)"

    return True, ""
