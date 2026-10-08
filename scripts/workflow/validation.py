"""
Post-merge validation for /cleanup: verdict enum, env isolation, retry logic, queue proof, and renderer.

The env scrub is isolation, not a sandbox. Check commands still run arbitrary repo code
(they receive the main worktree as cwd). The scrub limits the inherited env to a fixed
allowlist plus direnv-exported keys, protecting against poisoned caller env (e.g.
DATABASE_URL=postgres://attacker, COMPOSE_PROJECT_NAME=wrong-project).

Note: validation_lock uses os.open with O_NOFOLLOW, which is POSIX-only.
"""

import re
import json
import os
import fcntl
import math
import shutil
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import (
    Optional, Mapping, Dict, List, Tuple, FrozenSet, NamedTuple, Union, Sequence, Any, NoReturn, Iterator, Callable
)

from . import checks, git, check_diagnostics, merge_queue


class ValidationVerdict(str, Enum):
    """The four possible validation outcomes."""
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    SKIPPED = "skipped"

    @staticmethod
    def parse(raw: Any) -> "ValidationVerdict":
        """
        Parse a validation verdict from an object.

        Lenient: accepts any case (lowercases before lookup). Unknown values,
        None, non-strings, and missing keys all map to FAIL.
        """
        if not isinstance(raw, str):
            return ValidationVerdict.FAIL
        try:
            return ValidationVerdict(raw.lower())
        except ValueError:
            return ValidationVerdict.FAIL


class AttemptOutcome(Enum):
    """Outcome of a single check attempt."""
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


class InfraRule(NamedTuple):
    """Rule for classifying infra/environment errors."""
    pattern: Optional["re.Pattern[str]"]
    exit_codes: FrozenSet[int]
    reason: str


# Pre-compiled infra rules table.
# WARNING: ELSPROBLEMS and "Cannot find module" rules can hide transient regressions,
# since npm/module issues may mask real failures. 126/127 (command not found) similarly
# cannot distinguish permission denied from missing command without parsing stderr.
_INFRA_RULES: Tuple[InfraRule, ...] = (
    InfraRule(
        pattern=re.compile(r"^Cannot connect to the Docker daemon", re.MULTILINE),
        exit_codes=frozenset(),
        reason="Docker daemon unavailable"
    ),
    InfraRule(
        pattern=re.compile(r"^error during connect:", re.MULTILINE),
        exit_codes=frozenset(),
        reason="Docker daemon unavailable"
    ),
    InfraRule(
        pattern=re.compile(r"^Error.*port is already allocated", re.MULTILINE),
        exit_codes=frozenset(),
        reason="port in use"
    ),
    InfraRule(
        pattern=re.compile(r"^Error: listen EADDRINUSE", re.MULTILINE),
        exit_codes=frozenset(),
        reason="port in use"
    ),
    InfraRule(
        pattern=re.compile(r"^npm ERR! code ELSPROBLEMS", re.MULTILINE),
        exit_codes=frozenset(),
        reason="npm dependencies conflict"
    ),
    InfraRule(
        pattern=re.compile(r"^Error: Cannot find module", re.MULTILINE),
        exit_codes=frozenset(),
        reason="missing module"
    ),
    InfraRule(
        pattern=re.compile(r"^CHECK-GUARD-ABORT:", re.MULTILINE),
        exit_codes=frozenset(),
        reason="check guard aborted"
    ),
    InfraRule(
        pattern=None,
        exit_codes=frozenset({126, 127}),
        reason="exit 126/127: command not found or permission denied (stderr parsing needed to distinguish)"
    ),
)


def classify(result: checks.CheckResult) -> Tuple[AttemptOutcome, str]:
    """
    Classify a check attempt into a verdict and reason.

    Dispatches in this order:
    1. success=True → PASS
    2. Timeout → INCONCLUSIVE
    3. Spawn exception → INCONCLUSIVE
    4. Exit codes 126/127 or pattern match → INCONCLUSIVE or FAIL
    5. Unknown failure → FAIL
    """
    if result.success:
        return AttemptOutcome.PASS, ""

    if result.error and result.error.startswith(checks.TIMEOUT_ERROR_PREFIX):
        return AttemptOutcome.INCONCLUSIVE, "check timed out"

    if result.error is not None and result.returncode is None:
        return AttemptOutcome.INCONCLUSIVE, "could not start check"

    # Check exit code table
    for rule in _INFRA_RULES:
        if rule.exit_codes and result.returncode in rule.exit_codes:
            return AttemptOutcome.INCONCLUSIVE, rule.reason

    # Check pattern table
    combined_output = "\n".join([result.stdout or "", result.stderr or ""])
    for rule in _INFRA_RULES:
        if rule.pattern and rule.pattern.search(combined_output):
            return AttemptOutcome.INCONCLUSIVE, rule.reason

    # Unknown failure
    return AttemptOutcome.FAIL, ""


def aggregate(finals: Sequence[Union[AttemptOutcome, Tuple[AttemptOutcome, str]]]) -> ValidationVerdict:
    """
    Aggregate multiple attempt outcomes into a single verdict.

    Precedence: FAIL > INCONCLUSIVE > PASS. SKIPPED is never produced here.
    Accepts both bare outcomes and (outcome, reason) tuples.
    """
    # Extract just the outcomes from tuples
    outcomes = [
        (outcome if isinstance(outcome, AttemptOutcome) else outcome[0])
        for outcome in finals
    ]
    if any(outcome == AttemptOutcome.FAIL for outcome in outcomes):
        return ValidationVerdict.FAIL
    if any(outcome == AttemptOutcome.INCONCLUSIVE for outcome in outcomes):
        return ValidationVerdict.INCONCLUSIVE
    return ValidationVerdict.PASS


def _assert_never(x: NoReturn) -> NoReturn:
    """Helper for exhaustiveness checking in verdict-to-string mapping."""
    raise AssertionError(f"Unhandled value: {x}")


@dataclass
class EnvDerivation:
    """Result of deriving a validation environment."""
    env: Optional[Dict[str, str]]
    dropped_names: List[str]
    inconclusive_reason: Optional[str]
    notes: List[str]


def _fallback(
    is_compose: bool,
    inconclusive_msg: str,
    note_msg: str,
    base_env: Dict[str, str],
    dropped_names: List[str],
    notes: List[str]
) -> EnvDerivation:
    """Helper to return a fallback EnvDerivation when env derivation fails."""
    if is_compose:
        return EnvDerivation(
            env=None,
            dropped_names=dropped_names,
            inconclusive_reason=inconclusive_msg,
            notes=notes
        )
    else:
        notes.append(note_msg)
        return EnvDerivation(
            env=base_env,
            dropped_names=dropped_names,
            inconclusive_reason=None,
            notes=notes
        )


def build_validation_env(
    main_worktree: Path,
    caller_env: Mapping[str, str]
) -> EnvDerivation:
    """
    Build a validation environment from the main worktree's direnv export and a fixed allowlist.

    Returns EnvDerivation. Never raises; exceptions are captured as inconclusive reasons or notes.
    """
    # Base allowlist: core vars + common toolchain vars
    allowlist_keys = {
        "PATH", "HOME", "USER", "SHELL", "TERM", "LANG", "TMPDIR",
        "NVM_DIR", "VOLTA_HOME", "RUSTUP_HOME", "RUSTUP_TOOLCHAIN", "CARGO_HOME",
        "GO_HOME", "GOROOT", "GOPATH", "JAVA_HOME", "NODE_OPTIONS"
    }
    # Add all LC_* and npm_config_* keys
    for key in caller_env:
        if key.startswith("LC_") or key.startswith("npm_config_"):
            allowlist_keys.add(key)

    # Track dropped vars (names only)
    dropped_names = [k for k in caller_env if k not in allowlist_keys]

    # Start with base allowlist
    base_env: Dict[str, str] = {}
    for key in allowlist_keys:
        if key in caller_env:
            base_env[key] = caller_env[key]

    notes: List[str] = []
    inconclusive_reason: Optional[str] = None

    try:
        # Detect if it's a Compose repo
        is_compose_repo = _is_compose_repo(main_worktree)

        # Try to run direnv export json
        direnv_path = shutil.which("direnv", path=base_env.get("PATH", ""))
        if not direnv_path:
            return _fallback(
                is_compose_repo,
                "env could not be re-derived: direnv not found",
                "direnv not found; using plain scrubbed env",
                base_env,
                dropped_names,
                notes
            )

        # Build direnv subprocess env (base allowlist + XDG vars for direnv only)
        direnv_env = base_env.copy()
        for xdg_key in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "DIRENV_CONFIG"):
            if xdg_key in caller_env:
                direnv_env[xdg_key] = caller_env[xdg_key]

        try:
            proc = subprocess.run(
                [direnv_path, "export", "json"],
                cwd=str(main_worktree),
                env=direnv_env,
                capture_output=True,
                text=True,
                timeout=30
            )
        except subprocess.TimeoutExpired:
            return _fallback(
                is_compose_repo,
                "env could not be re-derived: direnv export failed or timed out (timeout)",
                "direnv export timed out; using plain scrubbed env",
                base_env,
                dropped_names,
                notes
            )

        if proc.returncode != 0:
            return _fallback(
                is_compose_repo,
                "env could not be re-derived: direnv reports .envrc blocked",
                f"direnv export failed (exit {proc.returncode}); using plain scrubbed env",
                base_env,
                dropped_names,
                notes
            )

        # Parse direnv export JSON
        try:
            direnv_export = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return _fallback(
                is_compose_repo,
                "env could not be re-derived: direnv export failed or timed out (unparsable)",
                "direnv export was not valid JSON; using plain scrubbed env",
                base_env,
                dropped_names,
                notes
            )

        if not isinstance(direnv_export, dict):
            return _fallback(
                is_compose_repo,
                "env could not be re-derived: direnv export was not a JSON object",
                "direnv export was not a JSON object; using plain scrubbed env",
                base_env,
                dropped_names,
                notes
            )

        # Apply direnv export on top of base
        result_env = base_env.copy()
        for key, value in direnv_export.items():
            if value is None:
                # null means unset
                result_env.pop(key, None)
            elif isinstance(value, str):
                result_env[key] = value
            else:
                # Non-string value (other than null) is unparsable
                return _fallback(
                    is_compose_repo,
                    "env could not be re-derived: direnv export contains non-string value",
                    "direnv export contained non-string value; using plain scrubbed env",
                    base_env,
                    dropped_names,
                    notes
                )

        # Compose guard: COMPOSE_PROJECT_NAME must be set and not equal to basename("main")
        if is_compose_repo:
            project_name = result_env.get("COMPOSE_PROJECT_NAME", "").strip()
            if not project_name:
                inconclusive_reason = "env could not be re-derived: COMPOSE_PROJECT_NAME missing after direnv export"
            elif project_name == "main":
                inconclusive_reason = f"env could not be re-derived: COMPOSE_PROJECT_NAME is '{project_name}' (bare basename)"

        if inconclusive_reason:
            return EnvDerivation(
                env=None,
                dropped_names=dropped_names,
                inconclusive_reason=inconclusive_reason,
                notes=notes
            )

        # Remove XDG/DIRENV_CONFIG vars from the check env (they were for direnv only)
        for xdg_key in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "DIRENV_CONFIG"):
            result_env.pop(xdg_key, None)

        return EnvDerivation(
            env=result_env,
            dropped_names=dropped_names,
            inconclusive_reason=None,
            notes=notes
        )

    except Exception as e:
        # Any unexpected exception becomes inconclusive for Compose, or a note otherwise
        if is_compose_repo:
            inconclusive_reason = f"env could not be re-derived: {type(e).__name__}: {e}"
        else:
            notes.append(f"env derivation error ({type(e).__name__}); using plain scrubbed env")
        return EnvDerivation(
            env=base_env if not inconclusive_reason else None,
            dropped_names=dropped_names,
            inconclusive_reason=inconclusive_reason,
            notes=notes
        )


def _is_compose_repo(main_worktree: Path) -> bool:
    """Check if the worktree is a Compose repo (has compose files)."""
    try:
        # Check for compose files via git ls-files (compose.yaml/yml only)
        try:
            proc = subprocess.run(
                ["git", "ls-files"],
                cwd=str(main_worktree),
                capture_output=True,
                text=True,
                timeout=5
            )
            if proc.returncode == 0:
                ls_files = proc.stdout
                if re.search(r"compose\.ya?ml", ls_files, re.IGNORECASE):
                    return True
        except (subprocess.TimeoutExpired, Exception):
            pass

        # Check for compose files at the worktree root (compose.yaml/yml only)
        for pattern in ("compose.yaml", "compose.yml"):
            if any(main_worktree.glob(pattern)):
                return True

        return False
    except Exception:
        return False


@dataclass(frozen=True)
class ValidationRun:
    """Result of validation run."""
    verdict: ValidationVerdict = ValidationVerdict.INCONCLUSIVE
    reason: str = ""
    failures: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


@contextmanager
def validation_lock(main_worktree: Path) -> Iterator[Optional[str]]:
    """
    Context manager for non-blocking validation lock.

    Acquires a lock on <git-common-dir>/cleanup-validation.lock before the caller
    proceeds, holding it until exit. If the lock is already held, yields an error string.
    """
    lock_file = None
    lock_acquired = False
    error: Optional[str] = None

    try:
        try:
            git_common_dir = git.abs_git_common_dir(main_worktree)
            if git_common_dir is None:
                error = "could not determine git-common-dir"
        except Exception as e:
            error = f"could not determine git-common-dir: {e}"

        if error:
            yield error
            return

        lock_path = git_common_dir / "cleanup-validation.lock"

        # Ensure directory exists
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Use os.open with O_NOFOLLOW for security (POSIX-only)
            fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o644)
            lock_file = os.fdopen(fd, "w")
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_acquired = True
        except BlockingIOError:
            error = "another cleanup is validating main"
        except OSError as e:
            error = f"could not acquire validation lock: {e}"

        try:
            yield error
        finally:
            if lock_file:
                try:
                    if lock_acquired:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    lock_file.close()
                except Exception:
                    pass
    except Exception as e:
        # Unexpected exception not caught above
        yield f"could not acquire validation lock: {e}"


def run_validation(
    check_commands: List[str],
    main_worktree: Path,
    env: Optional[Dict[str, str]],
    timeout: int,
    log_dir: Optional[Path],
    dropped_names: List[str],
    execute: Optional[Callable] = None
) -> ValidationRun:
    """
    Run validation checks with per-check retry and fingerprint guarding.

    Args:
        check_commands: List of shell commands to run
        main_worktree: Path to main worktree for cwd
        env: Environment to pass to checks (None inherits caller env)
        timeout: Timeout per check in seconds
        log_dir: Log directory path
        dropped_names: List of dropped env variable names (for diagnostics)
        execute: Custom execute_check callable (default: resolved at call time)

    Returns:
        ValidationRun with verdict, reason, and failure/note lists
    """
    if execute is None:
        execute = checks.execute_check

    if not check_commands:
        return ValidationRun(
            verdict=ValidationVerdict.PASS,
            reason="",
            failures=[],
            notes=["no checks configured"]
        )

    finals: List[Tuple[AttemptOutcome, str]] = []  # Track (outcome, reason) pairs
    failure_lines: List[str] = []
    note_lines: List[str] = []

    for index, cmd in enumerate(check_commands):
        fp_before = git.tracked_fingerprint(main_worktree)

        # Attempt 1
        started_at = datetime.now(timezone.utc)
        started_mono = time.monotonic()
        check_result = execute(cmd, cwd=main_worktree, timeout=timeout, env=env)
        duration_secs = time.monotonic() - started_mono
        outcome, outcome_reason = classify(check_result)

        try:
            check_diagnostics.write_check_log(
                log_dir, index, cmd, check_result, started_at, duration_secs,
                main_worktree, attempt=1, env_used=env, dropped_env_names=dropped_names
            )
        except Exception as e:
            note_lines.append(f"failed to write attempt 1 log for check {index}: {e}")

        if outcome == AttemptOutcome.PASS:
            # Check main moved during PASS (don't retry)
            fp_after = git.tracked_fingerprint(main_worktree)
            if fp_before is not None and fp_after is not None:
                if fp_before.head != fp_after.head:
                    finals.append((AttemptOutcome.INCONCLUSIVE, "main moved during validation"))
                    failure_lines.append(
                        f"Check inconclusive (main moved during validation): {cmd}"
                    )
                    continue
            finals.append((AttemptOutcome.PASS, ""))
            continue

        # Timeouts are still retried: the existing timeout contract tests pin this behavior.
        fp_after = git.tracked_fingerprint(main_worktree)

        if fp_before is None or fp_after is None:
            finals.append((AttemptOutcome.INCONCLUSIVE, "could not fingerprint main worktree"))
            failure_lines.append(
                f"Check inconclusive (could not fingerprint main worktree): {cmd}"
            )
            continue

        if fp_before.head != fp_after.head:
            finals.append((AttemptOutcome.INCONCLUSIVE, "main moved during validation"))
            failure_lines.append(
                f"Check inconclusive (main moved during validation): {cmd}"
            )
            continue

        if fp_before.status != fp_after.status or fp_before.diff_sha != fp_after.diff_sha:
            # Attempt 1 modified the worktree; preserve INCONCLUSIVE if it was already inconclusive
            if outcome == AttemptOutcome.INCONCLUSIVE:
                finals.append((outcome, outcome_reason))
                failure_lines.append(
                    f"Check inconclusive ({outcome_reason}): {cmd}"
                )
            else:
                finals.append((AttemptOutcome.FAIL, "attempt 1 modified the main worktree"))
                modified_paths = _extract_modified_paths(fp_before, fp_after)
                failure_lines.append(
                    f"Check failed (attempt 1 modified the main worktree: {', '.join(modified_paths)}): {cmd}"
                )
            continue

        # Attempt 2
        started_at = datetime.now(timezone.utc)
        started_mono = time.monotonic()
        check_result_2 = execute(cmd, cwd=main_worktree, timeout=timeout, env=env)
        duration_secs = time.monotonic() - started_mono
        outcome_2, outcome_reason_2 = classify(check_result_2)

        try:
            check_diagnostics.write_check_log(
                log_dir, index, cmd, check_result_2, started_at, duration_secs,
                main_worktree, attempt=2, env_used=env, dropped_env_names=dropped_names
            )
        except Exception as e:
            note_lines.append(f"failed to write attempt 2 log for check {index}: {e}")

        # Check fingerprints after attempt 2
        fp_after_2 = git.tracked_fingerprint(main_worktree)

        if outcome_2 == AttemptOutcome.PASS:
            # Check if attempt 2 modified the worktree (side effects)
            if fp_after is not None and fp_after_2 is not None:
                if fp_after.status != fp_after_2.status or fp_after.diff_sha != fp_after_2.diff_sha:
                    # Attempt 2 had side effects; downgrade to INCONCLUSIVE
                    finals.append((AttemptOutcome.INCONCLUSIVE, "attempt 2 had side effects"))
                    failure_lines.append(
                        f"Check inconclusive (attempt 2 had side effects): {cmd}"
                    )
                    continue
            finals.append((AttemptOutcome.PASS, ""))
            note_lines.append(
                f"check {index} flaky: failed on attempt 1 ({outcome_reason}) and passed on retry"
            )
        else:
            finals.append((outcome_2, outcome_reason_2))
            if outcome_2 == AttemptOutcome.FAIL:
                failure_lines.append(
                    f"Check command failed (attempt 2 also failed): {cmd}"
                )
            elif outcome_2 == AttemptOutcome.INCONCLUSIVE:
                failure_lines.append(
                    f"Check inconclusive ({outcome_reason_2}): {cmd}"
                )

    verdict = aggregate(finals)
    reason = ""
    if verdict == ValidationVerdict.INCONCLUSIVE:
        # Use first inconclusive reason found (from tracked pairs)
        for outcome, outcome_reason in finals:
            if outcome == AttemptOutcome.INCONCLUSIVE and outcome_reason:
                reason = outcome_reason
                break
    elif verdict == ValidationVerdict.FAIL:
        fail_count = sum(1 for o, _ in finals if o == AttemptOutcome.FAIL)
        reason = f"{fail_count} check(s) failed"

    return ValidationRun(
        verdict=verdict,
        reason=reason,
        failures=failure_lines,
        notes=note_lines
    )


def _extract_modified_paths(fp_before: "git.Fingerprint", fp_after: "git.Fingerprint") -> List[str]:
    """Extract the paths of modified tracked files from fingerprints."""
    # Fingerprints don't contain individual paths; return a generic message.
    # Full implementation would require parsing git status output in git.py.
    return ["tracked files"]


class Proved(NamedTuple):
    """Queue-tested tree matches main."""
    tree: str
    steps: List[str]


class CannotProve(NamedTuple):
    """Cannot prove queue-tested tree matches main."""
    reason: str


def queue_proof(
    pr: Optional[int],
    queue_started_at: Optional[float],
    main_worktree: Path
) -> Union[Proved, CannotProve]:
    """
    Prove locally that the landed tree matches the queue-tested tree.

    Returns Proved if all checks pass; CannotProve if any check fails or is missing.
    """
    if pr is None or queue_started_at is None:
        return CannotProve("no queue context")

    # Check queue_started_at is a valid finite positive number
    if not isinstance(queue_started_at, (int, float)) or math.isnan(queue_started_at) or math.isinf(queue_started_at) or queue_started_at < 0:
        return CannotProve("queue_started_at is not a valid positive finite timestamp")

    try:
        result = merge_queue.read_pr_result(pr, main_worktree)
        if result is None:
            return CannotProve("no queue result found")

        merge_result, timestamp = result

        # Check invariants
        if merge_result.pr != pr:
            return CannotProve(f"PR mismatch: result has PR {merge_result.pr}, expected {pr}")

        if merge_result.outcome.value != "merged":
            return CannotProve(f"outcome is {merge_result.outcome.value}, not merged")

        if timestamp < queue_started_at:
            return CannotProve("result is stale (timestamp < queue start)")

        sha = merge_result.tested_sha
        if sha is None or not _is_valid_sha40(sha):
            return CannotProve("tested_sha is not a valid 40-hex SHA")

        tested_tree = git.tree_of(sha, main_worktree)
        if tested_tree is None:
            return CannotProve("tested_sha tree is not accessible")

        main_tree = git.tree_of("HEAD", main_worktree)
        if main_tree is None:
            return CannotProve("main tree is not accessible")

        if tested_tree != main_tree:
            return CannotProve(f"tree mismatch: tested {tested_tree[:8]}..., main {main_tree[:8]}...")

        # Load queue config steps
        steps: List[str] = []
        try:
            config_path = merge_queue.resolve_config_path(cwd=main_worktree)
            if config_path:
                config = merge_queue.load_and_validate_config(config_path)
                steps = [step.cmd for step in config.steps]
        except Exception:
            # Cannot load steps; fail closed (cannot prove without knowing what was tested)
            return CannotProve("could not load queue config steps")

        # Require that queue steps cover repo-cache checks.
        # Try to get the repo-cache check commands and verify superset coverage.
        try:
            repo_cache_checks = _get_repo_cache_checks(main_worktree)
            if repo_cache_checks and not _steps_cover_checks(steps, repo_cache_checks):
                return CannotProve("queue steps do not cover all repo-cache checks")
        except Exception:
            # If we can't determine repo-cache checks, fail closed
            return CannotProve("could not verify queue steps cover repo-cache checks")

        return Proved(tree=main_tree, steps=steps)

    except Exception as e:
        return CannotProve(f"proof check failed: {e}")


def _is_valid_sha40(s: str) -> bool:
    """Check if a string is a valid 40-character hex SHA."""
    return len(s) == 40 and all(c in "0123456789abcdef" for c in s.lower())


def _get_repo_cache_checks(main_worktree: Path) -> Optional[List[str]]:
    """
    Load check commands from .claude/repo-cache.json (check field).
    Returns None if not found or not parseable.
    """
    try:
        cache_path = main_worktree / ".claude" / "repo-cache.json"
        if not cache_path.exists():
            return None
        with open(cache_path, "r") as f:
            cache = json.load(f)
        # Extract check commands from the cache
        checks = cache.get("commands", {}).get("check")
        if isinstance(checks, str):
            return [checks]
        elif isinstance(checks, list):
            return checks
        return None
    except Exception:
        return None


def _steps_cover_checks(steps: List[str], checks: List[str]) -> bool:
    """
    Check if queue steps cover all repo-cache checks.
    Returns True if all checks appear (literally) in the steps list.
    Conservative: fail closed if comparison cannot be done reliably.
    """
    if not checks:
        return True  # No checks to cover
    # Simple literal matching: all checks must appear in steps
    steps_set = set(steps)
    checks_set = set(checks)
    return checks_set.issubset(steps_set)


def render_headline(
    verdict: ValidationVerdict,
    reason: str,
    notes: List[str],
    steps: List[str]
) -> str:
    """
    Render a single-line validation headline.

    Returns the VALIDATION=... line ready to print.
    """
    if verdict == ValidationVerdict.PASS:
        if any("no checks configured" in note for note in notes):
            return "VALIDATION=pass — no checks configured"
        flaky_note = any("flaky" in note.lower() for note in notes)
        line = "VALIDATION=pass — merged main is green"
        if flaky_note:
            check_count = sum(1 for note in notes if "flaky" in note.lower())
            line += f" ({check_count} check(s) passed only on retry; see notes)"
        return line
    elif verdict == ValidationVerdict.FAIL:
        return "VALIDATION=fail — REGRESSION on main; investigate separately."
    elif verdict == ValidationVerdict.INCONCLUSIVE:
        return f"VALIDATION=inconclusive — {reason}"
    elif verdict == ValidationVerdict.SKIPPED:
        line = f"VALIDATION=skipped — landed tree identical to queue-tested tree ({reason})"
        if steps:
            line += "\n  queue-tested steps: " + "; ".join(steps)
        return line
    else:
        _assert_never(verdict)
