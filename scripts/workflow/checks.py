"""
Check execution for /shipit, /merge-and-cleanup, and /cleanup.

Shared check ordering (CHECK_ORDER) prevents silent skips: every gate
verifies the same checks in the same order, and none can pass without
executing something (unless explicitly configured to skip all checks,
which is a hard error rather than a silent pass).
"""

import contextlib
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Dict, Iterator, Optional, TextIO, Tuple, Any, Mapping, Final, Literal

from .safety import Unknown, fail_closed
from .cache import read_repo_cache
from .deps import atomic_write_text, find_lockfile, read_json_object
from .models import (
    validate_repo_cache, valid_timeout_secs, Mode, parse_mode, CheckKey, check_key_of, CheckRecord,
    FullKey, MinimumKey, satisfies, parse_check_record, record_filename_hash, CHECK_RECORD_SCHEMA_VERSION,
    ValidateResult,
    ValidateErrorResult,
    CheckResultsDict,
)

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:
    yaml = None  # type: ignore[assignment]

try:
    import fcntl
except ImportError:  # non-POSIX: the config lock degrades to no lock
    fcntl = None  # type: ignore[assignment]


CHECK_ORDER = ("format", "check", "lint", "typecheck", "vet", "test", "build")
NON_CHECK_COMMANDS = frozenset({"install", "check_minimum"})
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
    timeout: int = DEFAULT_CHECK_TIMEOUT_SECS,
    env: Optional[Mapping[str, str]] = None,
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
        check_result = execute_check(cmd, cwd=repo_root, timeout=timeout, env=env)

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


PACKAGE_JSON_PROVENANCE = "package-json"
PACKAGE_JSON_LABEL = "repo-cache (package-json)"
_CLAUDE_MD_CHECK_RE = re.compile(r"`((?:npm|pnpm|yarn|bun) run check|just check|make check)`")
_TOP_LEVEL_COMMANDS_RE = re.compile(r"^commands\s*:(.*)$")
_TOP_LEVEL_OPT_OUT_RE = re.compile(r"^checkOptOut\s*:")
_CHILD_CHECK_RE = re.compile(r"^(\s+)check\s*:")


def _repo_cache_label(path: Path) -> str:
    """
    The source label for a check read from repo-cache.json.

    A check written by `checks ensure-check` from package.json carries top-level
    commandSources.check == "package-json" and gets its own label, so the provenance is visible
    in resolve-check output. Any other repo-cache check keeps the plain label.
    """
    data = read_json_object(path)
    sources = data.get("commandSources") if data is not None else None
    if isinstance(sources, dict) and sources.get("check") == PACKAGE_JSON_PROVENANCE:
        return PACKAGE_JSON_LABEL
    return "repo-cache"


def detect_package_json_check(worktree: Path) -> Optional[str]:
    """
    Derive "<manager> run check" from a worktree's package.json, or None.

    Only a single unambiguous lockfile (see deps.find_lockfile) and a non-empty scripts.check
    string produce a command. Ambiguity or a missing script yields None; nothing is guessed.
    """
    lockfile, ambiguous = find_lockfile(worktree)
    if ambiguous is not None or lockfile is None:
        return None
    manifest = read_json_object(worktree / "package.json")
    if manifest is None:
        return None
    scripts = manifest.get("scripts")
    if not isinstance(scripts, dict):
        return None
    script = scripts.get("check")
    if not isinstance(script, str) or not script.strip():
        return None
    return f"{lockfile.manager} run check"


PACKAGE_JSON_UNDERIVED_NOTE = " (package-json check not re-derived for this worktree; cached value ignored)"


def _repo_cache_check(
    cache_file: Path, check_cmd: str, worktree: Path, scope: ConfigScope
) -> Tuple[Optional["CheckResolution"], str]:
    """
    Turn a non-empty repo-cache check into a CheckResolution, or explain why it is not trusted.

    A check with package-json provenance was copied from some branch's package.json into the shared
    cache, so it is not trusted as written: detection is re-run on the worktree being resolved and
    that result is used. If that worktree derives no check, the cached value is ignored.

    Returns (resolution, note). resolution is None only in the ignored case, and note then names it.
    """
    label = _repo_cache_label(cache_file)
    if label != PACKAGE_JSON_LABEL:
        return CheckResolution(check_cmd.strip(), label, cache_file, None, source_scope=scope), ""
    detected = detect_package_json_check(worktree)
    if detected is None:
        return None, PACKAGE_JSON_UNDERIVED_NOTE
    return CheckResolution(detected, PACKAGE_JSON_LABEL, cache_file, None, source_scope=scope), ""


def suggest_from_claude_md(worktree: Path) -> Optional[str]:
    """
    A check command named in the worktree's CLAUDE.md, for display only.

    The suggestion is never written to any config and is labelled unverified by the caller.
    """
    try:
        text = (worktree / "CLAUDE.md").read_text()
    except OSError:
        return None
    match = _CLAUDE_MD_CHECK_RE.search(text)
    return match.group(1) if match else None


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
    # True when minimum mode was requested but check_minimum was not found, so full was used instead
    fell_back_to_full: bool = False

    def __post_init__(self) -> None:
        if self.source == OPT_OUT_SOURCE:
            if self.command is not None or self.opt_out_reason is None or self.failure_reason is None:
                raise ValueError("opt-out resolution needs an opt_out_reason and failure_reason, and no command")
        else:
            if self.opt_out_reason is not None:
                raise ValueError("opt_out_reason is only valid with source 'opt-out'")
            if self.command is not None and (self.source is None or self.failure_reason is not None):
                raise ValueError("a resolved command needs a real source and no failure_reason")


def resolve_check_detail(worktree: Path, main_worktree: Optional[Path] = None, mode: str = "full") -> CheckResolution:
    """
    Resolve the check command with fallback logic, reporting the supplying file.

    When mode is "minimum", tries to resolve check_minimum first, then falls back to
    the full check command if check_minimum is not found, setting fell_back_to_full=True.
    When mode is "full" (default), resolves the full check command as usual.

    Resolution order (for mode="full"):
    1. Worktree repo-cache.json commands.check
    2. Main worktree repo-cache.json commands.check (if main_worktree provided)
    3. Worktree project.yaml commands.check
    4. Main worktree project.yaml commands.check (if main_worktree provided)
    5. Worktree justfile check recipe

    Resolution order (for mode="minimum"):
    1. Worktree repo-cache.json commands.check_minimum
    2. Main worktree repo-cache.json commands.check_minimum (if main_worktree provided)
    3. Worktree project.yaml commands.check_minimum
    4. Main worktree project.yaml commands.check_minimum (if main_worktree provided)
    5. Fall back to full check command (if found) with fell_back_to_full=True

    A repo-cache check with package-json provenance is not returned as cached: detection is re-run
    on the worktree being resolved (see _repo_cache_check), so a branch's package.json script is not
    trusted from the shared cache. If the worktree derives nothing, that cache step is skipped.

    If all sources fail, an explicit opt-out is consulted: a non-empty top-level
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
    check_key = "check_minimum" if mode == "minimum" else "check"
    fell_back_to_full = False

    # 1. Try repo-cache.json in worktree
    cache_file = worktree / ".claude" / "repo-cache.json"
    if cache_file.exists():
        cache_data, read_err = read_repo_cache(cache_file)
        sources_tried.append(f"worktree repo-cache.json ({check_key})")
        if not read_err and cache_data:
            check_cmd = cache_data.commands.get(check_key)
            if isinstance(check_cmd, str) and check_cmd.strip():
                resolution, note = _repo_cache_check(cache_file, check_cmd, worktree, "worktree")
                if resolution is not None:
                    resolution.fell_back_to_full = fell_back_to_full
                    return resolution
                sources_tried[-1] += note
            else:
                sources_tried[-1] += REPO_CACHE_NO_KEY_SUFFIX
        else:
            sources_tried[-1] += f" ({read_err})" if read_err else " (validation failed)"
    else:
        sources_tried.append(f"worktree repo-cache.json ({check_key}, not found)")

    # 2. Try repo-cache.json in main worktree if worktree had no check
    if main_worktree:
        main_cache_file = main_worktree / ".claude" / "repo-cache.json"
        if main_cache_file.exists():
            cache_data, read_err = read_repo_cache(main_cache_file)
            sources_tried.append(f"main repo-cache.json ({check_key})")
            if not read_err and cache_data:
                check_cmd = cache_data.commands.get(check_key)
                if isinstance(check_cmd, str) and check_cmd.strip():
                    resolution, note = _repo_cache_check(main_cache_file, check_cmd, worktree, "main")
                    if resolution is not None:
                        resolution.fell_back_to_full = fell_back_to_full
                        return resolution
                    sources_tried[-1] += note
                else:
                    sources_tried[-1] += REPO_CACHE_NO_KEY_SUFFIX
            else:
                sources_tried[-1] += f" ({read_err})" if read_err else " (validation failed)"
        else:
            sources_tried.append(f"main repo-cache.json ({check_key}, not found)")

    # 3. Try project.yaml in worktree (parsed once; the mapping is reused for the opt-out below)
    project_yaml = worktree / ".claude" / "project.yaml"
    worktree_yaml: Optional[Dict[str, Any]] = None
    if project_yaml.exists():
        worktree_yaml, load_note = _load_project_yaml(project_yaml)
        check_cmd = (worktree_yaml.get("commands") or {}).get(check_key) if worktree_yaml else None
        if isinstance(check_cmd, str) and check_cmd.strip():
            resolution = CheckResolution(check_cmd, "project.yaml", project_yaml, None, source_scope="worktree")
            resolution.fell_back_to_full = fell_back_to_full
            return resolution
        sources_tried.append(f"worktree project.yaml ({load_note or NO_CHECK_KEY_NOTE})")
    else:
        sources_tried.append(f"worktree project.yaml ({check_key}, not found)")

    # 4. Try project.yaml in main worktree if worktree had no check
    main_yaml: Optional[Dict[str, Any]] = None
    if main_worktree:
        main_project_yaml = main_worktree / ".claude" / "project.yaml"
        if main_project_yaml.exists():
            main_yaml, load_note = _load_project_yaml(main_project_yaml)
            check_cmd = (main_yaml.get("commands") or {}).get(check_key) if main_yaml else None
            if isinstance(check_cmd, str) and check_cmd.strip():
                resolution = CheckResolution(check_cmd, "project.yaml", main_project_yaml, None, source_scope="main")
                resolution.fell_back_to_full = fell_back_to_full
                return resolution
            sources_tried.append(f"main project.yaml ({load_note or NO_CHECK_KEY_NOTE})")
        else:
            sources_tried.append(f"main project.yaml ({check_key}, not found)")

    # If minimum mode and nothing found, fall back to full check
    if mode == "minimum":
        fell_back_to_full = True
        # Re-try the same sources but for "check" instead
        check_key = "check"

        # Try repo-cache in worktree
        cache_file = worktree / ".claude" / "repo-cache.json"
        if cache_file.exists():
            cache_data, read_err = read_repo_cache(cache_file)
            if not read_err and cache_data:
                check_cmd = cache_data.commands.get("check")
                if isinstance(check_cmd, str) and check_cmd.strip():
                    resolution, note = _repo_cache_check(cache_file, check_cmd, worktree, "worktree")
                    if resolution is not None:
                        resolution.fell_back_to_full = True
                        return resolution

        # Try repo-cache in main
        if main_worktree:
            main_cache_file = main_worktree / ".claude" / "repo-cache.json"
            if main_cache_file.exists():
                cache_data, read_err = read_repo_cache(main_cache_file)
                if not read_err and cache_data:
                    check_cmd = cache_data.commands.get("check")
                    if isinstance(check_cmd, str) and check_cmd.strip():
                        resolution, note = _repo_cache_check(main_cache_file, check_cmd, worktree, "main")
                        if resolution is not None:
                            resolution.fell_back_to_full = True
                            return resolution

        # Try project.yaml in worktree
        if worktree_yaml:
            check_cmd = _check_command_from_mapping(worktree_yaml)
            if check_cmd:
                resolution = CheckResolution(check_cmd, "project.yaml", project_yaml, None, source_scope="worktree")
                resolution.fell_back_to_full = True
                return resolution

        # Try project.yaml in main
        if main_yaml:
            check_cmd = _check_command_from_mapping(main_yaml)
            if check_cmd:
                resolution = CheckResolution(check_cmd, "project.yaml", main_project_yaml, None, source_scope="main")
                resolution.fell_back_to_full = True
                return resolution

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
                resolution = CheckResolution("just check", "justfile", None, None)
                resolution.fell_back_to_full = fell_back_to_full
                return resolution
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
            fell_back_to_full=fell_back_to_full,
        )

    # Resolution exhausted: the sources tried go on their own line.
    failure = NO_CHECK_CONFIGURED_MESSAGE + "\nsources tried: " + ", ".join(sources_tried)
    if opt_out_invalid:
        failure += "\n" + CHECK_OPT_OUT_INVALID_NOTE
    return CheckResolution(None, None, None, failure, fell_back_to_full=fell_back_to_full)


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
        - source: "repo-cache", "repo-cache (package-json)" (a cache check re-derived from package.json,
          see PACKAGE_JSON_LABEL), "project.yaml", "justfile", OPT_OUT_SOURCE ("opt-out"), or None
        - failure_reason: descriptive reason if resolution failed (including opt-out), or None

    Opt-out invariant: when an explicit checkOptOut applies and no command resolves, source is
    "opt-out", command is None, and failure_reason is the opt-out message. A resolved command always
    wins, so "opt-out" never appears alongside a command.

    Note: a non-None failure_reason is returned for both opt-out and not-configured.
    Callers that must tell them apart read source ("opt-out") rather than inferring from failure.
    """
    resolution = resolve_check_detail(worktree, main_worktree)
    return resolution.command, resolution.source, resolution.failure_reason


EnsureAction = Literal["resolved", "detected", "opt-out", "unresolved", "error"]


@dataclass
class EnsureCheckResult:
    """
    Outcome of ensure_check.

    action is "resolved", "detected", "opt-out", "unresolved" or "error". source is the CHECK_SOURCE
    label (None for unresolved and error). error is set only for "error".
    """
    action: EnsureAction
    source: Optional[str]
    command: Optional[str] = None
    opt_out_reason: Optional[str] = None
    suggestion: Optional[str] = None
    error: Optional[str] = None


def _config_lock_path(main_worktree: Path) -> Path:
    """
    Lock file for the main worktree's config writes.

    It lives under the system temp directory, keyed by the main worktree path, not in .claude/: the
    .claude/ directory holds only the config files themselves.
    """
    digest = hashlib.sha256(str(main_worktree.resolve()).encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"claude-checks-{digest}.lock"


@contextlib.contextmanager
def _main_config_lock(main_worktree: Path) -> Iterator[None]:
    """
    Hold an exclusive lock across a read-modify-write (and any rollback) of main config files.

    Degrades to no lock, without failing, when fcntl is unavailable, the lock file cannot be opened,
    or flock itself fails. Writes stay atomic either way; only interleaving goes unguarded.
    """
    if fcntl is None:
        yield
        return
    lock_file: Optional[TextIO] = None
    try:
        lock_file = open(_config_lock_path(main_worktree), "a")
    except OSError:
        lock_file = None
    if lock_file is None:
        yield
        return
    with lock_file:
        held = False
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            held = True
        except OSError:
            held = False
        try:
            yield
        finally:
            if held:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _write_detected_check(main_worktree: Path, command: str) -> Optional[str]:
    """
    Write a package.json-derived check into the MAIN repo-cache.json, keeping its other keys.

    The read-modify-write runs under the main config lock. A corrupt or schema-invalid existing file
    is refused rather than overwritten. Returns an error message, or None on success.
    """
    with _main_config_lock(main_worktree):
        return _write_detected_check_locked(main_worktree, command)


def _write_detected_check_locked(main_worktree: Path, command: str) -> Optional[str]:
    """The read-modify-write behind _write_detected_check; the caller holds the main config lock."""
    cache_path = main_worktree / ".claude" / "repo-cache.json"
    data: Dict[str, Any] = {"version": 1}
    if cache_path.exists():
        try:
            loaded = json.loads(cache_path.read_text())
        except (OSError, ValueError) as e:
            return f"refusing to overwrite unreadable main repo-cache.json: {e}"
        if not isinstance(loaded, dict) or not validate_repo_cache(loaded):
            return "refusing to overwrite invalid main repo-cache.json"
        data = loaded
    commands = data.get("commands", {})
    if not isinstance(commands, dict):
        return "refusing to overwrite: main repo-cache.json commands is not an object"
    commands["check"] = command
    data["commands"] = commands
    sources = data.get("commandSources", {})
    if not isinstance(sources, dict):
        sources = {}
    sources["check"] = PACKAGE_JSON_PROVENANCE
    data["commandSources"] = sources
    try:
        atomic_write_text(cache_path, json.dumps(data, indent=2) + "\n")
    except OSError as e:
        return f"could not write main repo-cache.json: {e}"
    return None


def ensure_check(worktree: Path, main_worktree: Path) -> EnsureCheckResult:
    """
    Make the check resolvable without asking, where that is safe.

    An existing resolution (including an explicit opt-out) is reported as-is. Otherwise a single
    unambiguous package.json "check" script is written into the MAIN repo-cache.json with
    commandSources.check = "package-json". If nothing can be derived, the result is "unresolved"
    and CLAUDE.md may offer an unverified suggestion, which is never written anywhere.
    """
    resolution = resolve_check_detail(worktree, main_worktree)
    if resolution.source == OPT_OUT_SOURCE:
        return EnsureCheckResult("opt-out", OPT_OUT_SOURCE, opt_out_reason=resolution.opt_out_reason)
    if resolution.command is not None:
        return EnsureCheckResult("resolved", resolution.source, command=resolution.command)
    detected = detect_package_json_check(worktree)
    if detected is None:
        return EnsureCheckResult("unresolved", None, suggestion=suggest_from_claude_md(worktree))
    error = _write_detected_check(main_worktree, detected)
    if error is not None:
        return EnsureCheckResult("error", None, error=error)
    after = resolve_check_detail(worktree, main_worktree)
    if after.command != detected:
        return EnsureCheckResult(
            "error", None,
            error=f"wrote {detected} to repo-cache.json but resolution reports {after.command!r} "
                  f"from {after.source}; check the cache and resolve by hand",
        )
    return EnsureCheckResult("detected", PACKAGE_JSON_LABEL, command=detected)


def _indent_of(line: str) -> str:
    """The leading whitespace of a line."""
    return line[: len(line) - len(line.lstrip())]


def _with_check_line(text: str, command: str) -> Tuple[str, str, Optional[str]]:
    """
    Return (new_text, snippet, refusal) with commands.check added to project.yaml text.

    refusal is set when the edit is unsafe: flow-style commands, an existing check key, or more than
    one top-level commands key. The snippet is the YAML the human would add by hand.
    """
    value_line = f"check: {json.dumps(command)}"
    lines = text.splitlines()
    commands_idx = [i for i, line in enumerate(lines) if _TOP_LEVEL_COMMANDS_RE.match(line)]
    if len(commands_idx) > 1:
        return text, "", (
            "refusing: project.yaml has more than one top-level commands key; add this line by hand "
            f"under the one you mean: {value_line}"
        )
    base = text if (text == "" or text.endswith("\n")) else text + "\n"
    if not commands_idx:
        snippet = f"commands:\n  {value_line}"
        return base + snippet + "\n", snippet, None

    idx = commands_idx[0]
    match = _TOP_LEVEL_COMMANDS_RE.match(lines[idx])
    rest = match.group(1).strip() if match else ""
    if rest and not rest.startswith("#"):
        return text, "", (
            "refusing: commands is in flow style; add this line by hand as an indented child of "
            f"commands: {value_line}"
        )
    indent: Optional[str] = None
    for line in lines[idx + 1:]:
        if line.strip() == "" or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            break
        if indent is None:
            indent = _indent_of(line)
        # Only a key at the first child's indent is commands.check; a deeper key named check is not.
        if _CHILD_CHECK_RE.match(line) and _indent_of(line) == indent:
            return text, "", (
                "refusing: commands.check already exists in project.yaml; replace that line by hand with: "
                f"{indent or '  '}{value_line}"
            )
    child_indent = indent if indent is not None else "  "
    lines.insert(idx + 1, f"{child_indent}{value_line}")
    snippet = f"{child_indent}{value_line}"
    return "\n".join(lines) + "\n", snippet, None


def _with_opt_out_line(text: str, reason: str) -> Tuple[str, str, Optional[str]]:
    """Return (new_text, snippet, refusal) with a top-level checkOptOut appended to project.yaml text."""
    if any(_TOP_LEVEL_OPT_OUT_RE.match(line) for line in text.splitlines()):
        return text, "", "refusing: project.yaml already has a checkOptOut line; edit it by hand"
    snippet = f"checkOptOut: {json.dumps(reason)}"
    base = text if (text == "" or text.endswith("\n")) else text + "\n"
    return base + snippet + "\n", snippet, None


def record_check_answer(
    main_worktree: Path, command: Optional[str] = None, opt_out: Optional[str] = None
) -> Tuple[bool, str]:
    """
    Record a human's check answer in the MAIN worktree's project.yaml, then verify it resolves.

    Exactly one of command / opt_out must be given. The text edit is refused for shapes it cannot
    safely make. The result is re-resolved against the main worktree; if it does not resolve to the
    answer, the file is restored to its original bytes (or removed if it did not exist) and the
    message names the YAML to add by hand. Returns (ok, message).
    """
    answer, problem = _answer_or_error(command, opt_out)
    if problem:
        return False, problem
    is_command = command is not None
    path = main_worktree / ".claude" / "project.yaml"

    with _main_config_lock(main_worktree):
        raw, read_error = _read_project_yaml_raw(path)
        if read_error:
            return False, read_error
        new_text, snippet, refusal = _edited_project_yaml(raw, answer, is_command)
        if refusal is not None:
            return False, refusal
        try:
            atomic_write_text(path, new_text)
        except OSError as e:
            return False, f"could not write main project.yaml: {e}"
        if _answer_resolves(main_worktree, answer, is_command):
            return True, "CHECK_RECORDED: " + snippet.replace("\n", " | ")
        return False, _roll_back_project_yaml(path, raw, snippet)


def _answer_or_error(command: Optional[str], opt_out: Optional[str]) -> Tuple[str, str]:
    """
    Return (answer, error) for the --command / --opt-out pair. error is empty when the answer is usable.

    A command is stripped. An opt-out reason is whitespace-normalized, so the value written, echoed and
    verified is the normalized one the resolver reads back.
    """
    if (command is None) == (opt_out is None):
        return "", "exactly one of --command or --opt-out is required"
    raw = command if command is not None else opt_out
    if raw is None or "\n" in raw or "\r" in raw or not raw.strip():
        return "", "the answer must be a single non-empty line"
    if command is not None:
        return raw.strip(), ""
    return " ".join(raw.split()), ""


def _read_project_yaml_raw(path: Path) -> Tuple[Optional[str], str]:
    """
    Return (raw_text, error) for the main project.yaml.

    raw_text keeps the file's own line endings (newline="" stops CRLF being folded to LF), so a rollback
    restores the original bytes. raw_text is None when the file is absent.
    """
    if not path.exists():
        return None, ""
    try:
        with open(path, "r", newline="") as f:
            return f.read(), ""
    except OSError as e:
        return None, f"cannot read main project.yaml: {e}"


def _edited_project_yaml(raw: Optional[str], answer: str, is_command: bool) -> Tuple[str, str, Optional[str]]:
    """
    Return (new_text, snippet, refusal) with the answer applied to the project.yaml raw text.

    The edit runs on LF-normalized text; the file's own line ending (CRLF when the original uses it)
    is restored on the way out, so a CRLF file stays CRLF.
    """
    original = raw or ""
    eol = "\r\n" if "\r\n" in original else "\n"
    text = original.replace("\r\n", "\n")
    if is_command:
        new_text, snippet, refusal = _with_check_line(text, answer)
    else:
        new_text, snippet, refusal = _with_opt_out_line(text, answer)
    return new_text.replace("\n", eol), snippet, refusal


def _answer_resolves(main_worktree: Path, answer: str, is_command: bool) -> bool:
    """True when re-resolving against the main worktree yields exactly the recorded answer."""
    resolution = resolve_check_detail(main_worktree, None)
    if is_command:
        return resolution.command == answer
    return resolution.source == OPT_OUT_SOURCE and resolution.opt_out_reason == answer


def _roll_back_project_yaml(path: Path, raw: Optional[str], snippet: str) -> str:
    """
    Restore project.yaml to its original bytes (removing it if it did not exist) and return the message.

    The message names the YAML to add by hand, and says so when the rollback itself fails.
    """
    try:
        if raw is None:
            path.unlink()
        else:
            atomic_write_text(path, raw)
    except OSError as e:
        return f"verification failed and rollback failed ({e}); add this YAML by hand: {snippet}"
    return f"verification failed: the answer did not resolve against main; add this YAML by hand: {snippet}"


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
    1. SHIPIT_CHECK_TIMEOUT_SECS env var, if set and valid (same override checks validate honours)
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


def worktree_tree(worktree: Path) -> Optional[str]:
    """
    Get the tree of the full non-ignored working state without touching the real index.

    Uses git rev-parse --git-dir to resolve the git dir (works in linked worktrees where
    .git is a file). Creates a temp git index file under tempfile.gettempdir(), reads the
    current HEAD tree, stages all changes via `git add -A`, and captures the result tree
    with `git write-tree`. Deletes the temp file in a finally block. Returns the tree SHA
    (40-hex), or None on any error. Logs "idempotency disabled: no tree: <reason>" on failure.
    """
    try:
        # Resolve git dir with git rev-parse --git-dir (works with linked worktrees)
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=True,
        )
        git_dir_raw = result.stdout.strip()
        if not git_dir_raw:
            reason = "git rev-parse --git-dir returned empty"
            logging.warning(f"idempotency disabled: no tree: {reason}")
            return None

        # Create temp index file in system temp directory, not in the git dir
        fd, tmp_index_path = tempfile.mkstemp(prefix="git-index-", dir=tempfile.gettempdir())
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
            tree = result.stdout.strip()
            # Never return an empty tree: it cannot key a record
            if not tree:
                reason = "git write-tree returned empty"
                logging.warning(f"idempotency disabled: no tree: {reason}")
                return None
            return tree
        finally:
            # Clean up temp file; a killed process leaks a small temp file (accepted)
            try:
                os.remove(tmp_index_path)
            except OSError:
                pass
    except Exception as e:
        reason = str(e)
        logging.warning(f"idempotency disabled: no tree: {reason}")
        return None


def _commands_digest(commands: Mapping[str, Optional[str]]) -> str:
    import hashlib

    canonical = json.dumps({k: commands[k] for k in sorted(commands)}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def resolve_check_base(worktree: Path) -> Optional[str]:
    """
    Resolve the base ref (merge-base of HEAD with origin/<default>).

    Returns the base SHA, or None if detached, unresolvable, or the base does not exist.
    """
    try:
        # A detached HEAD has no branch to compare against the default branch
        detached = subprocess.run(
            ["git", "symbolic-ref", "-q", "HEAD"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
        )
        if detached.returncode != 0:
            return None

        # The default branch comes from the one shared resolver (origin/HEAD, then local main/master),
        # so the selector and the merge gate cannot disagree about it.
        from . import git

        default_branch, branch_err = git.get_default_branch(cwd=worktree)
        if branch_err is not None or not default_branch:
            return None

        # Get merge-base of HEAD with origin/<default_branch>
        result = subprocess.run(
            ["git", "merge-base", "HEAD", f"origin/{default_branch}"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=True,
        )
        base_sha = result.stdout.strip()
        if not base_sha:
            return None
        return base_sha
    except Exception:
        return None


def canonical_changed_files(worktree: Path, base: Optional[str]) -> Optional[List[str]]:
    """
    Compute the canonical list of changed files (committed, staged, working, untracked).

    Returns a sorted, unique list of file paths (both sides of renames, NUL-separated internally).
    Returns None when base is None or empty, or when a subprocess fails, so callers treat the changed
    set as unknown rather than as an empty set. An empty list means "nothing changed" and nothing else.
    """
    if not base:
        return None

    files_set: set = set()
    try:
        # Committed and working-tree changes (both rename sides)
        result = subprocess.run(
            ["git", "diff", "--name-only", "--no-renames", base],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=True,
        )
        files_set.update(f for f in result.stdout.strip().split("\n") if f)

        # Staged changes
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--no-renames"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=True,
        )
        files_set.update(f for f in result.stdout.strip().split("\n") if f)

        # Untracked non-ignored files
        result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "--full-name"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=True,
        )
        files_set.update(f for f in result.stdout.strip().split("\n") if f)
    except Exception:
        # On error the changed set is unknown (no hash, validate runs uncached)
        return None

    return sorted(files_set)


def changed_files_hash(files: List[str]) -> str:
    """
    Compute SHA256 of NUL-separated, sorted file list.

    The same list that canonical_changed_files returns.
    """
    # NUL-separated, sorted
    canonical = "\0".join(files)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _read_record(path: Path, filename_hash: str) -> Optional[CheckRecord]:
    """Parse one record file through the single fail-closed boundary; None on any doubt."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    record, _reason = parse_check_record(data, filename_hash)
    return record


def _lookup_satisfying_record(records_dir: Path, requested: CheckKey) -> Optional[Tuple[str, CheckRecord]]:
    """
    Find a passing record that satisfies `requested` (models.satisfies). A minimum request is also
    satisfied by the full record for the same tree and full digest, since a passing full run covers it.
    """
    candidates: List[CheckKey] = [requested]
    if isinstance(requested, MinimumKey):
        candidates.append(FullKey(tree=requested.tree, full_digest=requested.full_digest))
    for candidate in candidates:
        name = record_filename_hash(candidate)
        record = _read_record(records_dir / f"{name}.json", name)
        if record is not None and satisfies(record, requested):
            return name, record
    return None


def _write_record(records_dir: Path, key: CheckKey, mode_executed: Mode) -> str:
    """Write a passing record atomically (same-directory unique temp, then os.replace). Returns the stem."""
    import time

    name = record_filename_hash(key)
    records_dir.mkdir(parents=True, exist_ok=True)
    record = CheckRecord(
        schema_version=CHECK_RECORD_SCHEMA_VERSION,
        key=key,
        passed=True,
        mode_executed=mode_executed,
        written_at=time.time(),
    )
    fd, tmp_name = tempfile.mkstemp(dir=str(records_dir), prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(record.to_dict()) + "\n")
        os.replace(tmp_name, records_dir / f"{name}.json")
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_name)
        raise
    return name


def _evict_older_pass(records_dir: Path, key: CheckKey, run_started: float) -> None:
    """
    A failed run evicts only a passing record for this exact key written before the run started.

    The record is first moved aside with one atomic rename, then judged from the moved file, so the
    age check and the removal act on the same bytes: a pass written by a concurrent run after the
    rename lands at the original path and is never touched. If the moved record turns out to be
    fresh it is put back, unless a newer record already took its place. The only cost of the race is
    a transient miss (a re-run), never a deleted fresh pass or a wrong hit.
    """
    name = record_filename_hash(key)
    path = records_dir / f"{name}.json"
    aside = records_dir / f".{name}.evict.{os.getpid()}.tmp"
    try:
        os.replace(path, aside)
    except OSError:
        return  # nothing to evict, or it vanished under us
    try:
        record = _read_record(aside, name)
        if record is not None and record.passed and record.written_at < run_started:
            return  # stale pass: evicted (the aside copy is removed below)
        with contextlib.suppress(OSError):
            os.link(aside, path)  # fresh (or unreadable): restore; FileExistsError means a newer record won
    finally:
        with contextlib.suppress(OSError):
            os.remove(aside)


def _export_check_env(base: Optional[str], changed: List[str]) -> Tuple[Dict[str, str], Optional[str]]:
    """
    Environment for a check run. CHECK_BASE is the merge-base (empty when unresolved). CHECK_CHANGED_FILES
    is a scratch file outside the worktree holding the same list validate hashed. Returns (env, scratch).
    """
    env = os.environ.copy()
    env["CHECK_BASE"] = base or ""
    scratch: Optional[str] = None
    try:
        fd, scratch = tempfile.mkstemp(prefix="claude-check-files-", suffix=".txt")
        with os.fdopen(fd, "w") as fh:
            fh.write(f"{base or ''}\n" + "\0".join(changed))
    except OSError as e:
        if scratch is not None:
            with contextlib.suppress(OSError):
                os.remove(scratch)
        logging.warning(f"CHECK_CHANGED_FILES not exported: {e}")
        env.pop("CHECK_CHANGED_FILES", None)
        return env, None
    env["CHECK_CHANGED_FILES"] = scratch
    return env, scratch


def _result_fields(results: CheckResults) -> CheckResultsDict:
    """CheckResults.to_dict() as the typed base of a validate result, one explicit key at a time."""
    d = results.to_dict()
    return CheckResultsDict(
        results=d["results"],
        all_passed=d["all_passed"],
        failed_at=d["failed_at"],
        planned=d["planned"],
        executed=d["executed"],
        skipped=d["skipped"],
        status=d["status"],
    )


def _validate_error(message: str, requested: Mode) -> ValidateErrorResult:
    """A validate error result carrying every ValidateResult key plus `error`."""
    base = _result_fields(CheckResults())
    base["status"] = "error"
    base["all_passed"] = False
    return ValidateErrorResult(
        **base,
        schema_version=1,
        mode_requested=requested,
        mode_executed=requested,
        fell_back_to_full=False,
        cache="miss",
        satisfied_by=None,
        changed_files_source="not-computed",
        error=message,
    )


@dataclass
class _RecordedRun:
    """One keyed check run: the cache decision, the record it satisfied (if any), and the run result."""
    cache_action: Literal["hit", "miss", "forced"]
    satisfied_by: Optional[str]
    mode_executed: Mode
    result: CheckResults
    err: Optional[Unknown]


def _run_recorded(
    worktree: Path,
    commands: Mapping[str, Optional[str]],
    key: Optional[CheckKey],
    mode_executed: Mode,
    timeout: int,
    force: bool,
    exports: Optional[Tuple[Optional[str], List[str]]],
) -> _RecordedRun:
    """
    Look up a passing record for `key`; on a miss, execute `commands` in `worktree` and record a pass.

    A hit runs nothing and returns CheckResults() unchanged: all_passed True, status "passed", nothing
    planned or executed. A miss writes a record only after a pass with
    the tree unchanged across the run; a failed run evicts only an older passing record for the same key.
    `exports` is (base, changed files) and sets CHECK_BASE / CHECK_CHANGED_FILES for the run; None leaves
    the caller's environment untouched. `key=None` disables caching.
    """
    import time

    records_dir = worktree / ".claude" / "check-records"
    cache_action: Literal["hit", "miss", "forced"] = "forced" if force else "miss"
    satisfied_by: Optional[str] = None
    if key is not None and not force:
        hit = _lookup_satisfying_record(records_dir, key)
        if hit is not None:
            satisfied_by, record = hit
            return _RecordedRun("hit", satisfied_by, record.mode_executed, CheckResults(), None)

    run_started = time.time()
    env: Optional[Dict[str, str]] = None
    scratch: Optional[str] = None
    if exports is not None:
        env, scratch = _export_check_env(exports[0], exports[1])
    try:
        result, err = run_checks(
            commands=dict(commands),
            repo_root=worktree,
            timeout=timeout,
            env=env,
        )
    finally:
        if scratch is not None:
            with contextlib.suppress(OSError):
                os.remove(scratch)

    passed = bool(result.all_passed and result.status == "passed" and err is None)
    if key is not None:
        if passed:
            if worktree_tree(worktree) == key.tree:
                try:
                    satisfied_by = _write_record(records_dir, key, mode_executed)
                except OSError as e:
                    logging.warning(f"idempotency disabled: record not written: {e}")
            else:
                logging.warning("idempotency disabled: tree changed during the run; no record written")
        else:
            _evict_older_pass(records_dir, key, run_started)
    return _RecordedRun(cache_action, satisfied_by, mode_executed, result, err)


def validate(
    worktree: Path,
    mode: str = "full",
    force: bool = False,
    main_worktree: Optional[Path] = None,
    timeout: Optional[int] = None,
) -> ValidateResult:
    """
    Validate checks idempotently with per-key record caching (ADR-0013 Amendment 11).

    Returns the CheckResults dict plus: schema_version, mode_requested, mode_executed,
    fell_back_to_full, cache ("hit" | "miss" | "forced"), satisfied_by (record stem or None), and
    changed_files_source ("computed" | "base-unresolved" | "tree-unavailable").

    A hit runs nothing. A miss runs the resolved command with CHECK_BASE and CHECK_CHANGED_FILES
    exported, and writes a record only after a pass with the tree unchanged across the run. A failed
    run evicts only an older passing record for the same key. Minimum with no check_minimum runs, and
    is keyed as, the full check.
    """
    requested = parse_mode(mode)
    if requested is None:
        return _validate_error(f"invalid mode: {mode!r}", "full")

    resolution = resolve_check_detail(worktree, main_worktree, mode=requested)
    if resolution.failure_reason:
        return _validate_error(resolution.failure_reason, requested)

    check_command = resolution.command
    if not check_command:
        return _validate_error("No check command resolved", requested)

    if timeout is None:
        source_kind: ConfigKind = "project.yaml" if resolution.source == "project.yaml" else "repo-cache"
        timeout, _timeout_source = resolve_check_timeout(
            worktree, main_worktree, resolution.source_file, source_kind, resolution.source_scope,
        )

    mode_executed: Mode = "full" if resolution.fell_back_to_full else requested
    if resolution.fell_back_to_full:
        logging.warning("checks validate: check_minimum is not declared; running the full check instead")

    # Digests: the full digest covers the full-gate commands plus the resolved full command and its
    # source; the minimum digest covers check_minimum plus the resolved minimum command and its
    # source. A command that changes from any source (project.yaml, main worktree, justfile) with an
    # unchanged tree therefore misses instead of reusing a pass earned by a different command.
    cache_data, _ = read_repo_cache(worktree / ".claude" / "repo-cache.json")
    commands_dict = cache_data.commands if cache_data else {}
    full_resolution = (
        resolution if requested == "full" else resolve_check_detail(worktree, main_worktree, mode="full")
    )
    full_inputs: Dict[str, Optional[str]] = {k: v for k, v in commands_dict.items() if k != "check_minimum"}
    full_inputs["resolved check"] = full_resolution.command
    full_inputs["resolved check source"] = full_resolution.source
    # The effective timeout is part of the key: a pass under a looser limit does not prove a pass under
    # a stricter one (a check that needs 400s passes at 900s and must not satisfy a 300s request).
    full_inputs["run timeout secs"] = str(timeout)
    full_digest = _commands_digest(full_inputs)
    minimum_inputs: Dict[str, Optional[str]] = {k: v for k, v in commands_dict.items() if k == "check_minimum"}
    minimum_inputs["resolved check_minimum"] = None if resolution.fell_back_to_full else resolution.command
    minimum_inputs["resolved check_minimum source"] = None if resolution.fell_back_to_full else resolution.source
    minimum_digest = _commands_digest(minimum_inputs)

    # Changed files: one canonical computation, hashed exactly as exported. An unresolved base means no hash.
    tree = worktree_tree(worktree)
    base = resolve_check_base(worktree)
    changed: List[str] = []
    files_hash: Optional[str] = None
    changed_source = "computed"
    if base is None:
        changed_source = "base-unresolved"
        logging.warning("idempotency disabled: no base: merge-base with origin default is unresolved")
    else:
        computed = canonical_changed_files(worktree, base)
        if computed is None:
            base = None
            changed_source = "base-unresolved"
            logging.warning("idempotency disabled: no changed files: the list could not be computed")
        else:
            changed = computed
            files_hash = changed_files_hash(changed)
    if tree is None:
        changed_source = "tree-unavailable"

    key: Optional[CheckKey] = None
    if tree is not None:
        key = check_key_of(mode_executed, tree, full_digest, minimum_digest, files_hash)
        if key is None:
            logging.warning("idempotency disabled: no key: this check run is not cached")

    run = _run_recorded(
        worktree,
        {"check": check_command},
        key,
        mode_executed,
        timeout,
        force,
        exports=(base, changed),
    )
    mode_executed = run.mode_executed
    satisfied_by = run.satisfied_by

    result_dict = _result_fields(run.result)
    if run.cache_action == "hit":
        result_dict["status"] = "passed"
        result_dict["all_passed"] = True
    return ValidateResult(
        **result_dict,
        schema_version=1,
        mode_requested=requested,
        mode_executed=mode_executed,
        fell_back_to_full=resolution.fell_back_to_full,
        cache=run.cache_action,
        satisfied_by=satisfied_by,
        changed_files_source=changed_source,
    )
