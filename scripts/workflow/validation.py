"""
Post-merge validation for /cleanup: verdict enum, env isolation, retry logic, queue proof, and renderer.

The env scrub is isolation, not a sandbox. Check commands still run arbitrary repo code
(they receive the main worktree as cwd). The scrub limits the inherited env to a fixed
allowlist plus direnv-exported keys, protecting against poisoned caller env (e.g.
DATABASE_URL=postgres://attacker, COMPOSE_PROJECT_NAME=wrong-project).
"""

import re
import json
import fcntl
import shutil
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import (
    Optional, Mapping, Dict, List, Tuple, FrozenSet, NamedTuple, Union, Sequence, Any, NoReturn, Iterator
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

        Fail-closed: unknown values, None, non-strings, wrong case, and missing keys
        all map to FAIL.
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
    pattern: Optional[re.Pattern[str]]
    exit_codes: FrozenSet[int]
    reason: str


# Pre-compiled infra rules table.
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
    combined_output = (result.stdout or "") + (result.stderr or "")
    for rule in _INFRA_RULES:
        if rule.pattern and rule.pattern.search(combined_output):
            return AttemptOutcome.INCONCLUSIVE, rule.reason

    # Unknown failure
    return AttemptOutcome.FAIL, ""


def aggregate(finals: Sequence[AttemptOutcome]) -> ValidationVerdict:
    """
    Aggregate multiple attempt outcomes into a single verdict.

    Precedence: FAIL > INCONCLUSIVE > PASS. SKIPPED is never produced here.
    """
    if any(outcome == AttemptOutcome.FAIL for outcome in finals):
        return ValidationVerdict.FAIL
    if any(outcome == AttemptOutcome.INCONCLUSIVE for outcome in finals):
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


def build_validation_env(
    main_worktree: Path,
    caller_env: Mapping[str, str]
) -> EnvDerivation:
    """
    Build a validation environment from the main worktree's direnv export and a fixed allowlist.

    Returns EnvDerivation. Never raises; exceptions are captured as inconclusive reasons or notes.
    """
    # Base allowlist
    allowlist_keys = {
        "PATH", "HOME", "USER", "SHELL", "TERM", "LANG", "TMPDIR",
        "NVM_DIR", "VOLTA_HOME"
    }
    # Add all LC_* keys
    for key in caller_env:
        if key.startswith("LC_"):
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
            if is_compose_repo:
                inconclusive_reason = "env could not be re-derived: direnv not found"
            else:
                notes.append("direnv not found; using plain scrubbed env")
            return EnvDerivation(
                env=base_env if not inconclusive_reason else None,
                dropped_names=dropped_names,
                inconclusive_reason=inconclusive_reason,
                notes=notes
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
            if is_compose_repo:
                inconclusive_reason = "env could not be re-derived: direnv export failed or timed out (timeout)"
            else:
                notes.append("direnv export timed out; using plain scrubbed env")
            return EnvDerivation(
                env=base_env if not inconclusive_reason else None,
                dropped_names=dropped_names,
                inconclusive_reason=inconclusive_reason,
                notes=notes
            )

        if proc.returncode != 0:
            if is_compose_repo:
                inconclusive_reason = "env could not be re-derived: direnv reports .envrc blocked"
            else:
                notes.append(f"direnv export failed (exit {proc.returncode}); using plain scrubbed env")
            return EnvDerivation(
                env=base_env if not inconclusive_reason else None,
                dropped_names=dropped_names,
                inconclusive_reason=inconclusive_reason,
                notes=notes
            )

        # Parse direnv export JSON
        try:
            direnv_export = json.loads(proc.stdout)
        except json.JSONDecodeError:
            if is_compose_repo:
                inconclusive_reason = "env could not be re-derived: direnv export failed or timed out (unparsable)"
            else:
                notes.append("direnv export was not valid JSON; using plain scrubbed env")
            return EnvDerivation(
                env=base_env if not inconclusive_reason else None,
                dropped_names=dropped_names,
                inconclusive_reason=inconclusive_reason,
                notes=notes
            )

        if not isinstance(direnv_export, dict):
            if is_compose_repo:
                inconclusive_reason = "env could not be re-derived: direnv export was not a JSON object"
            else:
                notes.append("direnv export was not a JSON object; using plain scrubbed env")
            return EnvDerivation(
                env=base_env if not inconclusive_reason else None,
                dropped_names=dropped_names,
                inconclusive_reason=inconclusive_reason,
                notes=notes
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
                if is_compose_repo:
                    inconclusive_reason = "env could not be re-derived: direnv export contains non-string value"
                else:
                    notes.append("direnv export contained non-string value; using plain scrubbed env")
                return EnvDerivation(
                    env=base_env if not inconclusive_reason else None,
                    dropped_names=dropped_names,
                    inconclusive_reason=inconclusive_reason,
                    notes=notes
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


def _is_compose_repo(main_worktree: Path) -> bool:  # type: ignore[name-defined]
    """Check if the worktree is a Compose repo (has compose files or .envrc)."""
    try:
        # Check for compose files via git ls-files
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
                if re.search(r"compose.*\.ya?ml|docker-compose.*\.ya?ml", ls_files, re.IGNORECASE):
                    return True
        except (subprocess.TimeoutExpired, Exception):
            pass

        # Check for compose files at the worktree root
        for pattern in ("compose*.yaml", "compose*.yml", "docker-compose*.yaml", "docker-compose*.yml"):
            if any(main_worktree.glob(pattern)):
                return True

        # Check for .envrc in main worktree or parent worktrees/
        if (main_worktree / ".envrc").exists():
            return True
        if (main_worktree.parent / ".envrc").exists():
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
    try:
        try:
            git_common_dir = git.abs_git_common_dir(main_worktree)  # type: ignore[attr-defined]
            if git_common_dir is None:
                yield "could not determine git-common-dir"
                return
        except Exception as e:
            yield f"could not determine git-common-dir: {e}"
            return

        lock_path = git_common_dir / "cleanup-validation.lock"

        # Ensure directory exists
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            lock_file = open(str(lock_path), "w")
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_acquired = True
            yield None
        except BlockingIOError:
            raise
        except OSError as e:
            yield f"could not acquire validation lock: {e}"
    except BlockingIOError:
        yield "another cleanup is validating main"
    except Exception as e:
        yield f"could not acquire validation lock: {e}"
    finally:
        if lock_file:
            try:
                if lock_acquired:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()
            except Exception:
                pass


def run_validation(
    check_commands: List[str],
    main_worktree: Path,
    env: Optional[Dict[str, str]],
    timeout: int,
    log_dir: Optional[Path],
    dropped_names: List[str],
    execute: Optional[Any] = None
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

    finals: List[AttemptOutcome] = []
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
            finals.append(AttemptOutcome.PASS)
            continue

        # Check if we can retry
        fp_after = git.tracked_fingerprint(main_worktree)

        if fp_before is None or fp_after is None:
            finals.append(AttemptOutcome.INCONCLUSIVE)
            failure_lines.append(
                f"Check inconclusive (could not fingerprint main worktree): {cmd}"
            )
            continue

        if fp_before.head != fp_after.head:
            finals.append(AttemptOutcome.INCONCLUSIVE)
            failure_lines.append(
                f"Check inconclusive (main moved during validation): {cmd}"
            )
            continue

        if fp_before.status != fp_after.status or fp_before.diff_sha != fp_after.diff_sha:
            finals.append(AttemptOutcome.FAIL)
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

        if outcome_2 == AttemptOutcome.PASS:
            finals.append(AttemptOutcome.PASS)
            note_lines.append(
                f"check {index} flaky: failed on attempt 1 ({outcome_reason}) and passed on retry"
            )
        else:
            finals.append(outcome_2)
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
        # Use first inconclusive reason found
        for line in failure_lines:
            if "inconclusive" in line.lower():
                reason = line.split("Check inconclusive (")[1].split("):")[0] if "Check inconclusive (" in line else ""
                break
    elif verdict == ValidationVerdict.FAIL:
        reason = f"{sum(1 for o in finals if o == AttemptOutcome.FAIL)} check(s) failed"

    return ValidationRun(
        verdict=verdict,
        reason=reason,
        failures=failure_lines,
        notes=note_lines
    )


def _extract_modified_paths(fp_before: git.Fingerprint, fp_after: git.Fingerprint) -> List[str]:
    """Extract the paths of modified tracked files from fingerprints."""
    # This is a simple stub; a full implementation would parse git status output
    # For now, return a generic message
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

        # Try to load queue config for steps
        steps: List[str] = []
        try:
            config_path = merge_queue.resolve_config_path(cwd=main_worktree)
            if config_path:
                config = merge_queue.load_and_validate_config(config_path)
                steps = [step.cmd for step in config.steps]
        except Exception:
            # Cannot load steps, but tree already proved; continue
            pass

        return Proved(tree=main_tree, steps=steps)

    except Exception as e:
        return CannotProve(f"proof check failed: {e}")


def _is_valid_sha40(s: str) -> bool:
    """Check if a string is a valid 40-character hex SHA."""
    return len(s) == 40 and all(c in "0123456789abcdef" for c in s.lower())


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
