"""
Merge plan and apply (Phase 2 of ADR-0013).

Ports the deterministic merge logic from /merge-and-cleanup into a plan/apply pattern:
- plan_merge: resolve PR/worktree, run push gate checks
- apply_merge: execute the 3-path merge gate and write cache
"""

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple, Literal

from . import git
from . import merge_queue
from .cache import read_repo_cache, write_cache
from .safety import Unknown, fail_closed


# Default timeout for 'just merge' execution (seconds).
# Override with MERGE_APPLY_TIMEOUT_SECS environment variable.
DEFAULT_MERGE_APPLY_TIMEOUT_SECS = 1800


@dataclass
class QueueDetection:
    """Detection result for merge queue configuration."""
    state: Literal["configured", "absent", "unknown"]
    path: Optional[str] = None
    source: Optional[Literal["flag", "env", "default"]] = None
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return asdict(self)


@dataclass
class QueueGuardDecision:
    """Decision result from the merge queue guard."""
    decision: Literal["proceed", "refuse"]
    detection: QueueDetection
    pr_base: Optional[str] = None
    queue_base: Optional[str] = None
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return asdict(self)


def detect_merge_queue(cwd: Path, config_flag: Optional[str] = None) -> QueueDetection:
    """
    Detect the merge queue configuration state for a given worktree.

    Returns:
    - QueueDetection with state="configured" if queue config is found and is a regular file
    - QueueDetection with state="absent" if no queue config exists (normal for non-queue repos)
    - QueueDetection with state="unknown" if there's an error or ambiguity

    Decision table:
    - Flag or env set, resolver OK, path.is_file() → configured (source flag or env)
    - Flag or env set, path missing → unknown (message names the missing path)
    - Flag or env set, resolver raises (e.g. trust-boundary) → unknown (exception text)
    - No override, LayoutMismatchError → absent (reason "layout-not-queue-capable")
    - No override, any other exception → unknown
    - No override, default path is_file() → configured (source default)
    - No override, default path does not exist → absent (path = where the config would go,
      reason "no-config")
    - Path exists but is not regular file, or stat raises → unknown
    - Catch-all: unexpected exceptions → unknown
    """
    try:
        # Try to resolve config path with the queue's own resolver
        try:
            config_path = merge_queue.resolve_config_path(config_flag=config_flag, cwd=cwd)
        except merge_queue.LayoutMismatchError:
            # No override set and layout doesn't match worktrees/ structure
            if config_flag or os.environ.get("MERGE_QUEUE_CONFIG"):
                # If an override was set, re-raise as unknown
                raise
            # No override + layout mismatch = absent (layout-not-queue-capable)
            return QueueDetection(
                state="absent",
                reason="layout-not-queue-capable"
            )
        except Exception as e:
            # Resolver raised an exception (could be flag/env related or git failure)
            if config_flag:
                return QueueDetection(
                    state="unknown",
                    reason=str(e)
                )
            elif os.environ.get("MERGE_QUEUE_CONFIG"):
                env_val = os.environ.get("MERGE_QUEUE_CONFIG")
                return QueueDetection(
                    state="unknown",
                    reason=f"MERGE_QUEUE_CONFIG is set to {env_val} but {str(e)}"
                )
            else:
                return QueueDetection(
                    state="unknown",
                    reason=str(e)
                )

        # At this point, config_path was successfully resolved
        # Determine the source
        source: Literal["flag", "env", "default"]
        if config_flag:
            source = "flag"
        elif os.environ.get("MERGE_QUEUE_CONFIG"):
            source = "env"
        else:
            source = "default"

        # Check if the resolved path exists and is a regular file
        if not config_path.exists():
            if config_flag:
                return QueueDetection(
                    state="unknown",
                    path=str(config_path),
                    source=source,
                    reason=f"--config is set to {config_path} but no file exists there"
                )
            elif os.environ.get("MERGE_QUEUE_CONFIG"):
                env_val = os.environ.get("MERGE_QUEUE_CONFIG")
                return QueueDetection(
                    state="unknown",
                    path=str(config_path),
                    source=source,
                    reason=f"MERGE_QUEUE_CONFIG is set to {env_val} but no file exists there"
                )
            else:
                # Default path doesn't exist = absent. Carry the would-be path so
                # /merge-and-cleanup can offer to create the config there.
                return QueueDetection(
                    state="absent",
                    path=str(config_path),
                    source=source,
                    reason="no-config"
                )

        # Path exists; check if it's a regular file
        try:
            if not config_path.is_file():
                return QueueDetection(
                    state="unknown",
                    path=str(config_path),
                    source=source,
                    reason=f"Config path exists but is not a regular file: {config_path}"
                )
        except (OSError, PermissionError) as e:
            return QueueDetection(
                state="unknown",
                path=str(config_path),
                source=source,
                reason=f"Could not check config file: {e}"
            )

        # Config is configured
        return QueueDetection(
            state="configured",
            path=str(config_path),
            source=source
        )

    except Exception as e:
        # Catch-all: any unexpected exception → unknown
        return QueueDetection(
            state="unknown",
            reason=f"Unexpected error in queue detection: {e}"
        )


def queue_guard(pr_number: int, target_worktree: Path) -> QueueGuardDecision:
    """
    Guard against merging PRs that belong to the local merge queue.

    - absent → proceed, with no gh call (repos without a queue see no change).
    - configured → queue base is the config's `base` key; unknown → the default branch
      (ADR-0021 requires config.base to equal the default branch).
    - PR base == queue base → refuse; PR base != queue base → proceed (stacked PRs etc.).
    - PR base or queue base undeterminable → refuse (fail closed).

    On refuse, `message` names the state, path and source (or the reason for unknown)
    and points at `/queued-merge <PR>`.
    """
    detection = detect_merge_queue(target_worktree)

    if detection.state == "absent":
        return QueueGuardDecision(decision="proceed", detection=detection)

    pr_base: Optional[str] = None
    try:
        pr_data = git.pr_view_json(str(pr_number), ["baseRefName"], cwd=target_worktree)
        raw_pr_base = pr_data.get("baseRefName") if pr_data else None
        pr_base = raw_pr_base if isinstance(raw_pr_base, str) and raw_pr_base else None
    except Exception:
        pr_base = None

    queue_base: Optional[str] = None
    if detection.state == "configured" and detection.path:
        try:
            config_data = json.loads(Path(detection.path).read_text())
            raw_base = config_data.get("base") if isinstance(config_data, dict) else None
            queue_base = raw_base if isinstance(raw_base, str) and raw_base else None
        except Exception:
            queue_base = None
    if queue_base is None:
        try:
            queue_base, _ = git.get_default_branch(cwd=target_worktree)
        except Exception:
            queue_base = None

    if detection.state == "configured":
        state_desc = f"A merge queue is configured (path: {detection.path}, source: {detection.source})."
    else:
        state_desc = f"Merge queue configuration state is unknown: {detection.reason}"
    recovery = ""
    if detection.state == "unknown":
        recovery = (
            "\nIf the queue is broken, fix the config, or merge manually with "
            f"`gh pr merge {pr_number}` outside this tooling (there is no bypass flag)."
        )

    if pr_base is None or queue_base is None:
        missing = "PR base" if pr_base is None else "queue base"
        return QueueGuardDecision(
            decision="refuse",
            detection=detection,
            pr_base=pr_base,
            queue_base=queue_base,
            message=(
                f"{state_desc} Refusing because the {missing} could not be determined.\n"
                f"Use `/queued-merge {pr_number}` to merge through the queue.{recovery}"
            ),
        )

    if pr_base == queue_base:
        return QueueGuardDecision(
            decision="refuse",
            detection=detection,
            pr_base=pr_base,
            queue_base=queue_base,
            message=(
                f"{state_desc} This PR targets {pr_base}, the merge queue's base branch.\n"
                f"Use `/queued-merge {pr_number}` to merge through the queue.{recovery}"
            ),
        )

    return QueueGuardDecision(
        decision="proceed",
        detection=detection,
        pr_base=pr_base,
        queue_base=queue_base,
        message=f"The merge queue does not take PRs targeting {pr_base} (queue base: {queue_base}); proceeding.",
    )


def _propose_queue_steps(target_worktree: Path) -> Tuple[List[str], Optional[str]]:
    """
    Pick gate steps for a new queue config from what the repo already runs.

    Prefers repo-cache `commands.check`, then a justfile `check` recipe. Never proposes
    `just merge`: that recipe may itself merge the PR, which the queue does on its own.
    Returns (steps, source) — ([], None) when nothing is detectable.
    """
    cache_file = target_worktree / ".claude" / "repo-cache.json"
    if cache_file.exists():
        cache_data, err = read_repo_cache(cache_file)
        if not err and cache_data:
            check_cmd = cache_data.commands.get("check")
            if isinstance(check_cmd, str) and check_cmd.strip():
                return [check_cmd.strip()], "repo-cache"
    justfile = target_worktree / "justfile"
    if justfile.exists():
        try:
            summary = subprocess.run(
                ["just", "-f", str(justfile), "--summary"],
                cwd=str(target_worktree), capture_output=True, text=True, timeout=5
            )
            if summary.returncode == 0 and "check" in summary.stdout.split():
                return ["just check"], "justfile"
        except Exception:
            pass
    return [], None


def queue_init(
    target_worktree: Path,
    steps: Optional[List[str]] = None,
    write: bool = False,
) -> Dict[str, Any]:
    """
    Propose (and, with write=True, create) a merge-queue config for a repo whose
    queue detection is "absent" with a known default path.

    The proposal is {base: <default branch>, steps, cleanup: true}. Explicit `steps`
    override the detected ones. The config is validated with the queue's own
    validator before anything is written, and the write is create-only (O_EXCL):
    an existing config is never overwritten.

    Raises RuntimeError when the repo is not queue-capable, a config already exists,
    no steps are available, or validation fails.
    """
    detection = detect_merge_queue(target_worktree)
    if detection.state != "absent" or not detection.path:
        if detection.state == "configured":
            raise RuntimeError(f"A merge-queue config already exists at {detection.path}")
        raise RuntimeError(
            f"Cannot set up a merge queue here (state: {detection.state}, "
            f"reason: {detection.reason or 'none'})"
        )
    config_path = Path(detection.path)

    chosen: List[str]
    steps_source: Optional[str]
    if steps:
        chosen, steps_source = [s for s in steps if s.strip()], "explicit"
    else:
        chosen, steps_source = _propose_queue_steps(target_worktree)

    default_branch, err = git.get_default_branch(cwd=target_worktree)
    if err or not default_branch:
        raise RuntimeError(f"Could not determine default branch: {err}")

    config: Dict[str, Any] = {"base": default_branch, "steps": chosen, "cleanup": True}
    result: Dict[str, Any] = {
        "path": str(config_path),
        "config": config,
        "steps_source": steps_source,
        "written": False,
    }
    if not chosen:
        if write:
            raise RuntimeError("No gate steps detected; pass --step <cmd> explicitly")
        return result

    try:
        merge_queue.validate_config_data(config, cwd=target_worktree)
    except ValueError as e:
        raise RuntimeError(f"Proposed config is invalid: {e}") from e

    if write:
        try:
            fd = os.open(str(config_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            raise RuntimeError(f"A merge-queue config already exists at {config_path}")
        with os.fdopen(fd, "w") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
        result["written"] = True
    return result


def merge_lock_path(target_worktree: str) -> Path:
    """
    Resolve the merge-lock path for a target worktree.

    Stored under ~/.claude/state/merge-locks/, keyed by a hash of the worktree's
    resolved absolute path, rather than inside the target worktree itself. The lock
    is a durable "already merged" guard (see apply_merge docstring) that is never
    auto-cleared, so writing it inside the target repo leaves a permanent untracked
    file there — tripping that repo's own dirty-tree push gate on the next
    /merge-and-cleanup run unless that repo's .gitignore is manually patched to
    exclude it (a fix that has to be repeated in every repo this runs against).
    Keeping the lock out of the repo entirely fixes this once, for all repos.
    Lock identity depends on the worktree's resolved absolute path at call time;
    if the worktree is later moved, renamed, or recreated at a different path, it
    will resolve to a different lock.
    """
    resolved = str(Path(target_worktree).resolve())
    digest = hashlib.sha256(resolved.encode()).hexdigest()[:16]
    return Path.home() / ".claude" / "state" / "merge-locks" / f"{digest}.lock"


@dataclass
class MergePlan:
    """Plan for merging a PR."""
    pr_number: int
    head_ref: str
    target_worktree: str
    blocking_failures: List[str] = field(default_factory=list)
    queue: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MergePlan":
        """Construct from parsed JSON dict."""
        field_names = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in field_names})


@dataclass
class MergeResult:
    """Result of applying a merge plan."""
    success: bool
    pr_merged: bool = False
    merge_gate_used: str = ""
    error: Optional[Unknown] = None
    cache_write_failed: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        d = asdict(self)
        if self.error:
            d["error"] = str(self.error)
        return d


@fail_closed
def plan_merge(
    arguments: Optional[str] = None,
    cwd: Optional[Path] = None
) -> Tuple[Optional[MergePlan], Optional[Unknown]]:
    """
    Plan a merge operation (push gate validation).

    Resolves PR/worktree from arguments (path mode or PR number), or auto-detects
    from the current linked worktree when arguments is empty/None. Then validates the push
    gate's 4 checks:
    1. Not detached HEAD
    2. No uncommitted/untracked changes
    3. Upstream tracking branch exists
    4. No unpushed commits

    Returns (MergePlan, None) if push gate passes.
    Returns (MergePlan with blocking_failures, None) if push gate fails.
    Returns (None, Unknown(...)) if PR/worktree resolution fails.
    """
    try:
        pr_number: Optional[int] = None
        head_ref: Optional[str] = None
        target_worktree: Optional[str] = None

        effective_cwd = (cwd or Path.cwd()).resolve()

        if not arguments or not arguments.strip():
            if not git.is_linked_worktree(cwd=effective_cwd):
                return None, Unknown(
                    "No argument provided and not in a linked worktree. "
                    "Run from the linked worktree you want to merge, or pass a PR number or worktree path."
                )
            target_worktree = str(effective_cwd)
            pr_number, head_ref, _ = _resolve_pr_from_worktree(target_worktree)
            if not pr_number or not head_ref:
                return None, Unknown(_unresolved_pr_message(target_worktree, is_current=True))

        elif Path(arguments).exists():
            target_worktree = str(Path(arguments).resolve())
            pr_number, head_ref, _ = _resolve_pr_from_worktree(target_worktree)
            if not pr_number or not head_ref:
                return None, Unknown(_unresolved_pr_message(target_worktree, is_current=False))

        else:
            pr_number, head_ref, target_worktree = _resolve_pr_from_number(arguments, cwd)
            if not pr_number or not head_ref or not target_worktree:
                return None, Unknown(f"Could not resolve PR from '{arguments}'")
            target_worktree = str(Path(target_worktree).resolve())

        plan = MergePlan(
            pr_number=pr_number,
            head_ref=head_ref,
            target_worktree=target_worktree
        )

        # Call the queue guard early for UX (refuse before the push gate)
        guard_decision = queue_guard(pr_number, Path(target_worktree))
        plan.queue = guard_decision.to_dict()

        # If refused, return early without running the push gate
        if guard_decision.decision == "refuse":
            return plan, None

        blocking_failures = _run_push_gate(target_worktree, head_ref, cwd)
        plan.blocking_failures = blocking_failures

        return plan, None

    except Exception as e:
        return None, Unknown(f"plan_merge failed: {e}")


@fail_closed
def apply_merge(plan_json: str, cwd: Optional[Path] = None) -> Tuple[MergeResult, Optional[Unknown]]:
    """
    Apply a merge plan (mutating).

    Executes the 3-path merge gate:
    1. Path 1: 'just merge' (if recipe exists)
    2. Path 2: repo-cache.json check gate + gh pr merge --squash
    3. Path 3: gh pr merge --squash (no gate, with warning marker)

    Note: 'just merge' (Path 1) and the repo-cache check command (Path 2) are deliberately
    NOT routed through mutations.check_mutation_allowed() — they're already-trusted,
    repo-configured content the maintainer wrote (same trust boundary the .md wrapper relied
    on before this CLI existed), not an argv shape this module constructed itself. The funnel
    only guards git/gh calls this module builds directly (gh pr merge, the reentrancy lock).

    Creates/preserves a merge lock file under ~/.claude/state/merge-locks/ (kept after a
    successful/uncertain merge; released when the merge fails and the PR is still OPEN;
    see merge_lock_path).

    Returns (MergeResult, None) with execution result.
    Returns (MergeResult, Unknown(...)) if a critical error occurs.
    """
    try:
        plan_data = json.loads(plan_json)
        plan = MergePlan.from_dict(plan_data)

        result = MergeResult(success=False)

        if plan.blocking_failures:
            result.error = Unknown(f"Push gate failed: {'; '.join(plan.blocking_failures)}")
            return result, result.error

        # Check the queue guard live (authoritative gate, ignoring plan.queue)
        guard_decision = queue_guard(plan.pr_number, Path(plan.target_worktree))
        if guard_decision.decision == "refuse":
            result.error = Unknown(guard_decision.message)
            result.pr_merged = False
            return result, result.error

        # gh/git calls always target the plan's worktree, never the caller's cwd: the CLI runs
        # from the claude-helpers checkout, and `gh pr merge <N>` resolves <N> against the repo
        # of whatever directory it runs in. `cwd` is accepted for signature compatibility only.
        effective_cwd = Path(plan.target_worktree)

        lock_file = merge_lock_path(plan.target_worktree)

        try:
            lock_file.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(lock_file.parent, 0o700)
            lock_content = f"PR #{plan.pr_number} locked by merge-and-cleanup at {time.time()}\n"
            lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(lock_fd, lock_content.encode())
            finally:
                os.close(lock_fd)
        except FileExistsError:
            try:
                existing_content = lock_file.read_text()
                result.error = Unknown(f"Merge lock file exists at {lock_file} (concurrent merge or prior failure): {existing_content}")
            except Exception:
                result.error = Unknown(f"Merge lock file exists at {lock_file} (concurrent merge or prior failure)")
            return result, result.error
        except OSError as e:
            result.error = Unknown(f"Failed to create lock file: {e}")
            return result, result.error

        merge_gate_used = ""
        merge_succeeded = False

        if _check_just_merge(plan.target_worktree):
            success, detail = _run_just_merge(plan.target_worktree)
            if success:
                merge_gate_used = "just merge"
                merge_succeeded = True
            else:
                result.error = Unknown(f"'just merge' failed: {detail}")
                _release_lock_if_still_open(lock_file, plan, effective_cwd)
                return result, result.error

        if not merge_succeeded:
            check_success, gate_applied, check_detail = _run_merge_gate_checks(Path(plan.target_worktree))
            if not check_success:
                if check_detail:
                    result.error = Unknown(f"Merge gate check failed: {check_detail}")
                else:
                    result.error = Unknown("Merge gate check failed")
                lock_file.unlink(missing_ok=True)  # gate runs before any merge: nothing merged
                return result, result.error
            if gate_applied:
                merge_gate_used = "repo-cache check"

        if not merge_succeeded:
            if merge_gate_used == "":
                merge_gate_used = "gh pr merge (no gate)"
            success, detail = _run_gh_pr_merge(plan.pr_number, effective_cwd)
            if success:
                merge_succeeded = True
            else:
                result.error = Unknown(f"gh pr merge failed: {detail}")
                _release_lock_if_still_open(lock_file, plan, effective_cwd)
                return result, result.error

        if merge_succeeded:
            cache_success, cache_err = _write_merge_cache(Path(plan.target_worktree))
            if not cache_success:
                result.cache_write_failed = str(cache_err) if cache_err else "unknown cache write failure"
            result.success = True
            result.pr_merged = True
            result.merge_gate_used = merge_gate_used

        return result, None

    except json.JSONDecodeError as e:
        return MergeResult(success=False, error=Unknown(f"Invalid plan JSON: {e}")), None
    except Exception as e:
        return MergeResult(success=False, error=Unknown(f"apply_merge failed: {e}")), None


def _release_lock_if_still_open(lock_file: Path, plan: MergePlan, cwd: Path) -> None:
    """
    Remove the merge lock after a failed merge attempt, but only if the PR is
    confirmed still OPEN. If state can't be confirmed (or the PR merged despite the
    error), the lock stays as the "already merged" guard.
    """
    try:
        pr_data = git.pr_view_json(str(plan.pr_number), ["state"], cwd=cwd)
        if pr_data and pr_data.get("state") == "OPEN":
            lock_file.unlink(missing_ok=True)
    except Exception:
        pass


def _unresolved_pr_message(target_worktree: str, is_current: bool) -> str:
    """Build the 'could not resolve PR' error message, worded correctly for the two call sites."""
    branch_desc = "current branch" if is_current else "worktree's checked-out branch"
    return (
        f"Could not resolve PR from worktree {target_worktree}: "
        f"no cached PR and {branch_desc} has no associated PR (has it been pushed with an open PR?)"
    )


def _resolve_pr_from_worktree(target_worktree: str) -> Tuple[Optional[int], Optional[str], str]:
    """Resolve PR number and head ref from a worktree path."""
    try:
        cache_file = Path(target_worktree) / ".claude" / "github-cache.json"
        if cache_file.exists():
            cache_data = json.loads(cache_file.read_text())
            pr_number = cache_data.get("pr", {}).get("number")
            if pr_number:
                pr_data = git.pr_view_json(str(pr_number), ["headRefName", "state"], cwd=Path(target_worktree))
                if pr_data:
                    return pr_number, pr_data.get("headRefName"), target_worktree

        head_ref = git.get_current_branch(cwd=Path(target_worktree))
        pr_data = git.pr_view_json(head_ref, ["number", "state"], cwd=Path(target_worktree))
        if pr_data:
            return pr_data.get("number"), head_ref, target_worktree

        return None, None, target_worktree
    except Exception:
        return None, None, target_worktree


def _resolve_pr_from_number(arguments: str, cwd: Optional[Path]) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """Resolve PR number and head ref from a PR number or URL."""
    try:
        pr_number = None
        if "/pull/" in arguments:
            match = re.search(r"/pull/(\d+)", arguments)
            if match:
                pr_number = int(match.group(1))
        else:
            match = re.search(r"\d+", arguments)
            if match:
                pr_number = int(match.group(0))

        if not pr_number:
            return None, None, None

        pr_data = git.pr_view_json(str(pr_number), ["headRefName", "state"], cwd=cwd)
        if not pr_data:
            return None, None, None

        head_ref = pr_data.get("headRefName")
        if not isinstance(head_ref, str):
            return None, None, None
        porcelain = git.get_worktree_list_porcelain(cwd=cwd)
        target_worktree = _find_worktree_by_branch(porcelain, head_ref)

        return pr_number, head_ref, target_worktree
    except Exception:
        return None, None, None


def _find_worktree_by_branch(porcelain_output: str, branch: str) -> Optional[str]:
    """Find worktree path that has the given branch checked out."""
    from .worktrees import parse_worktree_list
    worktree_list = parse_worktree_list(porcelain_output)
    for wt_path, wt_branch in worktree_list:
        if wt_branch == branch:
            return wt_path
    return None


def _run_push_gate(target_worktree: str, head_ref: str, cwd: Optional[Path]) -> List[str]:
    """Run push gate checks. Returns list of failure messages (empty if all pass)."""
    failures = []
    wt_path = Path(target_worktree)

    try:
        git.run_git_command(["symbolic-ref", "-q", "HEAD"], cwd=wt_path, check=True)
    except Exception:
        failures.append("Detached HEAD")

    try:
        status = git.run_git_command(["status", "--porcelain"], cwd=wt_path)
        if status:
            failures.append("Uncommitted or untracked changes")
    except Exception:
        failures.append("Could not check status")

    try:
        git.run_git_command(["rev-parse", "@{u}"], cwd=wt_path, check=True)
    except Exception:
        failures.append("No upstream tracking branch")

    unpushed_count = git.rev_list_count("@{u}..", cwd=wt_path)
    if unpushed_count == -1:
        failures.append("Could not determine unpushed commit count")
    elif unpushed_count > 0:
        failures.append(f"{unpushed_count} unpushed commits")

    return failures


def _get_merge_apply_timeout() -> int:
    """
    Resolve the timeout for 'just merge' execution from the environment.

    Reads MERGE_APPLY_TIMEOUT_SECS; returns the value if it's a valid positive
    integer, otherwise returns DEFAULT_MERGE_APPLY_TIMEOUT_SECS.

    Never raises; invalid values silently fall back to the default.
    """
    env_value = os.environ.get("MERGE_APPLY_TIMEOUT_SECS", "").strip()
    if not env_value:
        return DEFAULT_MERGE_APPLY_TIMEOUT_SECS

    try:
        timeout_secs = int(env_value)
        if timeout_secs > 0:
            return timeout_secs
    except (ValueError, TypeError):
        pass

    return DEFAULT_MERGE_APPLY_TIMEOUT_SECS


def _check_just_merge(target_worktree: str) -> bool:
    """Check if 'just merge' recipe exists."""
    try:
        justfile = Path(target_worktree) / "justfile"
        if not justfile.exists():
            return False
        summary = subprocess.run(
            ["just", "-f", str(justfile), "--summary"],
            cwd=target_worktree,
            capture_output=True,
            text=True,
            timeout=5
        )
        if summary.returncode == 0:
            return "merge" in summary.stdout.split()
        return False
    except Exception:
        return False


def _run_just_merge(target_worktree: str) -> Tuple[bool, Optional[str]]:
    """
    Run 'just merge' command.

    Returns (True, None) on success.
    Returns (False, diagnostic_message) on failure.
    """
    try:
        # /merge-and-cleanup now runs this step via a backgrounded Bash call (no harness
        # foreground timeout ceiling), so this timeout is the only remaining limiter — give
        # a full build + E2E boot real headroom instead of cutting it close at 600s.
        # Override with MERGE_APPLY_TIMEOUT_SECS environment variable (default: 1800s).
        timeout_secs = _get_merge_apply_timeout()
        subprocess.run(
            ["just", "merge"],
            cwd=target_worktree,
            timeout=timeout_secs,
            capture_output=True,
            text=True,
            check=True
        )
        return True, None
    except subprocess.CalledProcessError as e:
        return False, e.stderr or str(e)
    except Exception as e:
        return False, str(e)


def _run_merge_gate_checks(target_worktree: Path) -> Tuple[bool, bool, Optional[str]]:
    """
    Run repo-cache check gate via run_checks.

    Returns (True, False, None) if no cache exists (gate not applied).
    Returns (True, True, None) if gate passes.
    Returns (False, True, diagnostic_message) if gate fails.
    """
    from .checks import run_checks

    cache_file = target_worktree / ".claude" / "repo-cache.json"
    if not cache_file.exists():
        return True, False, None

    cache_data, err = read_repo_cache(cache_file)
    if err:
        return False, True, str(err)
    if not cache_data:
        return True, False, None

    timeout = _get_merge_apply_timeout()
    result, check_err = run_checks(
        commands=cache_data.commands,
        repo_root=target_worktree,
        timeout=timeout
    )

    if check_err:
        return False, True, str(check_err)

    if not result.all_passed:
        failed_cmd = result.failed_at or "unknown"
        return False, True, f"Check '{failed_cmd}' failed"

    if result.status == "no_checks_ran":
        return False, True, "No checks configured or all checks null"

    return True, True, None


def _run_gh_pr_merge(pr_number: int, cwd: Optional[Path]) -> Tuple[bool, Optional[str]]:
    """
    Run 'gh pr merge --squash' command via the mutation funnel.

    Returns (True, None) on success.
    Returns (False, diagnostic_message) on failure.
    """
    success, err = git.pr_merge_squash(pr_number, cwd=cwd)
    return success, str(err) if err else None


def _write_merge_cache(target_worktree: Path) -> Tuple[bool, Optional[Unknown]]:
    """
    Write merged state to cache using atomic write_cache().

    Returns (True, None) on success.
    Returns (False, Unknown(...)) on failure.
    """
    try:
        cache_file = target_worktree / ".claude" / "github-cache.json"
        cache_file.parent.mkdir(parents=True, exist_ok=True)

        existing = {}
        if cache_file.exists():
            existing = json.loads(cache_file.read_text())

        if "pr" not in existing:
            existing["pr"] = {}
        existing["pr"]["state"] = "MERGED"

        return write_cache(cache_file, existing)
    except Exception as e:
        return False, Unknown(f"Failed to prepare cache for writing: {e}")
