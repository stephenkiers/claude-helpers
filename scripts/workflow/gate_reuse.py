"""
Gate reuse for the merge queue's Phase 7 (ADR-0021 amendment, #491).

When a passing full-mode check record already covers the exact tree, command and toolchain the queue
would test, the queue may skip its repeat run of the gate steps. The record is a cache hint that the queue
verifies, never an authority. decide_gate_reuse re-derives the expected key with the same checks.py code
`checks validate` uses, and every guard below is unconditional: no config can relax them. The first failing
guard wins, and any exception inside the decision is a miss, never a reuse.

This module must not import merge_queue at load time, because merge_queue imports this module. The one
runtime use of merge_queue (_is_tree_clean) is a lazy import inside the decision.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, List, Literal, Optional, Union

from . import git
from .checks import (
    DigestMismatch,
    ExpectedFullKey,
    Found,
    MinimumOnly,
    NoRecord,
    RecordsUnsafe,
    RecordUnparseable,
    _never,
    classify_full_record,
    expected_full_key,
    resolve_check_detail,
    worktree_tree,
)

if TYPE_CHECKING:
    from .merge_queue import MergeQueueConfig, Step


# Every reason a reuse decision can miss. Ordered as the status output lists them.
MissReason = Literal[
    "disabled",
    "steps-unmappable",
    "command-mismatch",
    "tracked-state",
    "dirty-tree",
    "tree-mismatch",
    "stale-base",
    "digest-uncomputable",
    "records-unsafe",
    "record-unparseable",
    "minimum-record-only",
    "digest-mismatch",
    "no-record",
    "audit-write-failed",
    "error",
]

# Paths whose presence in the tested tree refuses reuse (Q2): committed config or committed check records
# could forge the record the queue would trust.
_GUARDED_PATHS = (".claude/repo-cache.json", ".claude/project.yaml")
_GUARDED_PREFIX = ".claude/check-record"


@dataclass(frozen=True)
class Reuse:
    """The gate may be skipped: a passing full record covers the expected key."""
    record_name: str
    written_at: float
    expected: ExpectedFullKey


@dataclass(frozen=True)
class Miss:
    """The gate must run. `reason` is one of MissReason; `detail` is for humans and never branched on."""
    reason: MissReason
    detail: str


GateReuse = Union[Reuse, Miss]


def queue_gate_step(config: "MergeQueueConfig") -> Optional["Step"]:
    """
    The one gate step, when the queue has exactly one step and none were dropped while loading the config.

    Returns None otherwise. A queue with dropped entries cannot prove what its gate was, so it cannot map.
    """
    if len(config.steps) == 1 and config.steps_dropped == 0:
        return config.steps[0]
    return None


def decide_gate_reuse(
    config: "MergeQueueConfig",
    worktree: Path,
    main_worktree: Path,
    tested_sha: str,
    base_sha: str,
) -> GateReuse:
    """
    Decide whether the queue's gate steps may be skipped for tested_sha.

    The guards run in this fixed order and the first failure wins: kill switch, single step, command equal to
    the resolved full check, tracked-state refusal, clean tree, worktree tree equal to tested_sha^{tree}, a fresh
    origin/<base> equal to base_sha and an ancestor of tested_sha, the expected full key, then the record lookup.
    Any exception returns Miss("error"); there is no path that turns an error into Reuse.
    """
    try:
        return _decide(config, worktree, main_worktree, tested_sha, base_sha)
    except Exception as e:
        return Miss("error", f"{type(e).__name__}: {e}")


def _decide(
    config: "MergeQueueConfig",
    worktree: Path,
    main_worktree: Path,
    tested_sha: str,
    base_sha: str,
) -> GateReuse:
    """The guarded decision body; decide_gate_reuse wraps it so that any exception is a miss."""
    # Lazy: merge_queue imports this module at load time.
    from .merge_queue import _is_tree_clean

    # 1. Kill switch. Reuse stays enabled only for the exact boolean False (the default when the key is absent).
    # Any other value, including a malformed one such as "yes", is a forced miss.
    if config.gate_reuse_disabled is not False:
        return Miss("disabled", "gate_reuse_disabled is set in merge-queue.json")

    # 2. Exactly one step, none dropped.
    step = queue_gate_step(config)
    if step is None:
        return Miss(
            "steps-unmappable",
            f"{len(config.steps)} step(s) configured, {config.steps_dropped} dropped while loading",
        )

    # 3. The queue step must be byte-equal to the resolved full check command. No normalisation.
    resolved = resolve_check_detail(worktree, main_worktree, mode="full").command
    if step.cmd != resolved:
        return Miss("command-mismatch", "queue step is not the resolved full check command")

    # 4. Nothing that keys or forges a record may be tracked at the tested commit.
    tracked = _tracked_guarded_paths(worktree, tested_sha)
    if tracked:
        return Miss("tracked-state", f"tracked at tested_sha: {', '.join(tracked[:3])}")

    # 5. The PR worktree is clean.
    if not _is_tree_clean(worktree):
        return Miss("dirty-tree", "worktree has uncommitted or untracked changes")

    # 6. The worktree's full working tree is exactly the tested tree.
    tree = worktree_tree(worktree)
    tested_tree = git.run_git_command(["rev-parse", f"{tested_sha}^{{tree}}"], cwd=worktree).strip()
    if tree is None or tree != tested_tree:
        return Miss("tree-mismatch", "worktree tree does not equal tested_sha^{tree}")

    # 7. Fresh base: origin/<base> is the base Phase 4 pinned, and it is an ancestor of the tested commit.
    try:
        git.run_git_command(["fetch", "origin", config.base], cwd=worktree, timeout=config.mutation_timeout_secs)
        fresh_base = git.run_git_command(["rev-parse", f"origin/{config.base}"], cwd=worktree).strip()
    except Exception as e:
        return Miss("stale-base", f"fetch of origin/{config.base} failed: {e}")
    if fresh_base != base_sha or not git.is_ancestor(base_sha, tested_sha, cwd=worktree):
        return Miss("stale-base", "origin/base moved or is not an ancestor of tested_sha")

    # 8. The expected full key, at the effective step timeout (the same value Phase 7 hands run_step).
    step_timeout = step.timeout_secs if step.timeout_secs is not None else config.mutation_timeout_secs
    expected = expected_full_key(worktree, main_worktree, timeout=step_timeout)
    if expected is None or expected.tree != tree:
        return Miss("digest-uncomputable", "expected full key could not be computed for this tree")

    # 9. The record lookup, mapped one-to-one.
    lookup = classify_full_record(worktree, expected.key)
    if isinstance(lookup, Found):
        return Reuse(record_name=lookup.name, written_at=lookup.record.written_at, expected=expected)
    if isinstance(lookup, RecordUnparseable):
        return Miss("record-unparseable", "the record at the expected name does not parse as a passing full record")
    if isinstance(lookup, MinimumOnly):
        return Miss("minimum-record-only", "only a minimum-mode record exists for this tree and digest")
    if isinstance(lookup, DigestMismatch):
        return Miss("digest-mismatch", "a full record exists for this tree under a different digest")
    if isinstance(lookup, NoRecord):
        return Miss("no-record", "no full record exists for the expected key")
    if isinstance(lookup, RecordsUnsafe):
        return Miss("records-unsafe", "the records directory or a record file is a symlink")
    _never(lookup)


def _tracked_guarded_paths(worktree: Path, tested_sha: str) -> List[str]:
    """Guarded paths present in the tested commit's tree (ls-tree lists literal paths, so filter in Python)."""
    listing = git.run_git_command(["ls-tree", "-r", "-z", "--name-only", tested_sha], cwd=worktree)
    return [
        path
        for path in listing.split("\0")
        if path in _GUARDED_PATHS or path.startswith(_GUARDED_PREFIX)
    ]
