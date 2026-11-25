"""
Delete guard and trash routing for full-ticket teardown (Step 4 of ADR-0013).

Pure functions validating deletion targets and a guarded rmtree.
All operations are fail-closed: a validation failure returns a reason string,
and a deletion refusal returns without modifying the filesystem.
"""

import os
import shutil
import stat
import uuid
from pathlib import Path
from typing import Tuple, Optional, NamedTuple, Iterable


# Constants for deletion kind
KIND_WORKTREE = "worktree"
KIND_BUILD_DIR = "build_dir"
KIND_TRASH = "trash"

# Allowed basenames for build directories
BUILD_DIR_ALLOWLIST = {"target", "node_modules", "dist", ".next", "build"}

# Trash directory name under the worktrees parent
TRASH_DIRNAME = ".cleanup-trash"


class GuardContext(NamedTuple):
    """
    Validation context for deletion targets.

    Attributes:
        worktrees_parent: Absolute path of the worktrees parent directory.
        primary_worktree: Absolute path of the primary worktree.
        registered: Iterable of worktree paths from the act-time snapshot.
        plan_paths: Iterable of paths persisted in the plan.
        trash_root: Absolute path of the trash root directory.
        home: Path.home() by default; injectable for tests.
        claude_dir: ~/.claude path by default; injectable for tests.
    """
    worktrees_parent: str
    primary_worktree: str
    registered: Iterable[str]
    plan_paths: Iterable[str]
    trash_root: str
    home: Optional[Path] = None
    claude_dir: Optional[Path] = None


def _has_symlink_component(path: str, root: str) -> bool:
    """
    Check if path or any ancestor under root contains a symlink.

    Returns True if path itself or any ancestor from root downwards is a symlink.
    Returns False if path is not under root or no symlinks are found.
    Does not resolve symlinks: uses lstat and realpath comparisons.
    """
    real_root = os.path.realpath(root).rstrip("/")
    real_path = os.path.realpath(path)

    # Check if path is even under root (using realpath)
    if not real_path.startswith(real_root + "/") and real_path != real_root:
        return False

    # Walk from root down to path, checking each component for symlinks
    current = real_root
    while current != real_path:
        # Find next component
        rel = real_path[len(current):].lstrip("/")
        if not rel:
            break
        parts = rel.split("/", 1)
        next_part = parts[0]
        current = os.path.join(current, next_part)

        # Use lstat to check if this component is a symlink (without following it)
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return True
        except (OSError, ValueError):
            # If lstat fails, treat as unknown (conservative)
            pass

    return False


def validate_target(kind: str, path: str, ctx: GuardContext) -> Tuple[bool, str]:
    """
    Validate a deletion target before removal.

    Pure function that checks:
    - Target is not /, $HOME, or ~/.claude
    - Target is within or equal to worktrees parent (or agent-* subdir of primary)
    - Target is not the primary worktree itself
    - Target and ancestors have no symlinks
    - Target is in the persisted plan
    - Target is registered or in the act-time snapshot

    Args:
        kind: One of KIND_WORKTREE, KIND_BUILD_DIR, KIND_TRASH
        path: Absolute path to validate
        ctx: GuardContext with validation configuration

    Returns:
        (True, "") if target passes all checks
        (False, reason) otherwise
    """
    if not os.path.isabs(path):
        return False, "path must be absolute"

    # Reject dangerous paths
    home_path = ctx.home if ctx.home is not None else Path.home()
    home_str = str(home_path)
    claude_dir_path = ctx.claude_dir if ctx.claude_dir is not None else (home_path / ".claude")
    claude_dir_str = str(claude_dir_path)

    if path in ("/", home_str, claude_dir_str):
        return False, f"refusing to delete {path}"

    # Realpath everything for comparison
    real_path = os.path.realpath(path)
    real_worktrees_parent = os.path.realpath(ctx.worktrees_parent).rstrip("/")
    real_primary = os.path.realpath(ctx.primary_worktree).rstrip("/")

    # Check if path is the primary worktree
    if real_path == real_primary:
        return False, "refusing to delete primary worktree"

    # Check for symlinked target or ancestor
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            return False, "refusing to delete symlinked target"
    except (OSError, ValueError):
        pass

    if _has_symlink_component(path, real_worktrees_parent):
        return False, "refusing to delete path with symlinked ancestor"

    # Validate by kind
    if kind == KIND_WORKTREE:
        # Strictly inside worktrees parent, or under primary/.claude/worktrees/agent-*
        if real_path.startswith(real_worktrees_parent + "/"):
            # Inside worktrees parent: valid
            pass
        elif real_path.startswith(real_primary + "/.claude/worktrees/agent-"):
            # Inside agent-* under primary: validate the agent-* directory structure
            # F16: Check the immediate parent is agent-* and has no symlinks
            agent_parent = os.path.dirname(real_path)
            agent_dir_name = os.path.basename(agent_parent)

            # agent_dir_name must be exactly "agent-*" (the worktree name itself)
            # or be under an agent-* directory. Check that path is at most one level deep under agent-*
            if agent_dir_name.startswith("agent-"):
                # Immediate parent is agent-*, check for symlinks in the path
                if _has_symlink_component(agent_parent, real_primary):
                    return False, "agent worktree path contains symlinked component"
                # Valid agent worktree
                pass
            elif real_path == agent_parent:
                # Path is the agent-* directory itself
                if os.path.basename(real_path).startswith("agent-"):
                    if _has_symlink_component(real_path, real_primary):
                        return False, "agent worktree path contains symlinked component"
                    pass
                else:
                    return False, f"worktree {path} not in allowed locations"
            else:
                return False, f"worktree {path} not in allowed locations"
        else:
            return False, f"worktree {path} not in allowed locations"

    elif kind == KIND_BUILD_DIR:
        # Must be directly under a worktree that passed validation
        parent = os.path.dirname(real_path)
        basename = os.path.basename(real_path)
        if basename not in BUILD_DIR_ALLOWLIST:
            return False, f"build dir basename {basename} not in allowlist"
        # Parent must pass worktree validation
        ok, reason = validate_target(KIND_WORKTREE, parent, ctx)
        if not ok:
            return False, f"build_dir parent fails validation: {reason}"

    elif kind == KIND_TRASH:
        # Directly under trash root
        parent = os.path.dirname(real_path)
        if os.path.realpath(parent) != os.path.realpath(ctx.trash_root):
            return False, f"trash item {path} not directly under trash root"

    else:
        return False, f"unknown kind: {kind}"

    # Check if path is in the persisted plan
    plan_paths_set = {os.path.realpath(p) for p in ctx.plan_paths}
    if real_path not in plan_paths_set:
        return False, f"path {path} not in persisted plan"

    # Check if path is registered or in the snapshot
    registered_set = {os.path.realpath(p) for p in ctx.registered}
    if real_path not in registered_set:
        return False, f"path {path} not registered or in snapshot"

    return True, ""


def guarded_rmtree(kind: str, path: str, ctx: GuardContext) -> Tuple[bool, str]:
    """
    Remove a directory tree only if it passes validation.

    Calls validate_target immediately before shutil.rmtree. If shutil.rmtree.avoids_symlink_attacks
    is False, refuses and reports the item as list-only. Never shells out to rm -rf.

    Args:
        kind: One of KIND_WORKTREE, KIND_BUILD_DIR, KIND_TRASH
        path: Absolute path to remove
        ctx: GuardContext with validation configuration

    Returns:
        (True, "") if removal succeeds
        (False, reason) if validation fails or removal is refused
    """
    # Validate first
    ok, reason = validate_target(kind, path, ctx)
    if not ok:
        return False, reason

    # Check if shutil.rmtree has symlink-attack protection
    if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
        return False, f"shutil.rmtree lacks symlink-attack protection; {path} list-only"

    # Try to remove
    try:
        shutil.rmtree(path)
        return True, ""
    except Exception as e:
        # F42: Report partial state (which paths remain) instead of plain failure
        remaining_items = []
        try:
            if os.path.exists(path):
                for root, dirs, files in os.walk(path):
                    for d in dirs:
                        remaining_items.append(os.path.join(root, d))
                    for f in files:
                        remaining_items.append(os.path.join(root, f))
                # Limit to first 10 items in report
                if remaining_items:
                    remaining_str = "; ".join(remaining_items[:10])
                    if len(remaining_items) > 10:
                        remaining_str += f" ... and {len(remaining_items) - 10} more"
                    return False, f"rmtree failed: {e}; remaining items: {remaining_str}"
        except Exception:
            pass
        return False, f"rmtree failed: {e}"


def trash_root_for(worktrees_parent: str) -> Path:
    """
    Compute the trash root path for a worktrees parent.

    Args:
        worktrees_parent: Absolute path of the worktrees parent

    Returns:
        Path of the trash root (not created)
    """
    return Path(worktrees_parent) / TRASH_DIRNAME


def move_to_trash(path: str, ctx: GuardContext, kind: str = KIND_WORKTREE) -> Tuple[bool, str, Optional[str]]:
    """
    Move a path to trash via atomic rename.

    Checks that source and trash root have the same st_dev before moving.
    Creates the trash root with mode 0o700 if missing.
    Validates the source with validate_target before moving.

    Args:
        path: Absolute path to move to trash
        ctx: GuardContext with trash configuration
        kind: Deletion kind for validation (default KIND_WORKTREE)

    Returns:
        (True, "", new_path) if move succeeds
        (False, reason, None) if validation, device check, or move fails
    """
    source_path = Path(path)
    trash_root = Path(ctx.trash_root)

    # Validate source
    ok, reason = validate_target(kind, path, ctx)
    if not ok:
        return False, reason, None

    # Check same device
    try:
        source_stat = os.stat(str(source_path))
        trash_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        trash_stat = os.stat(str(trash_root))

        if source_stat.st_dev != trash_stat.st_dev:
            return False, "source and trash root on different devices", None
    except (OSError, ValueError) as e:
        return False, f"device check failed: {e}", None

    # Move with unique suffix
    unique_id = uuid.uuid4().hex[:12]
    new_name = f"{source_path.name}-{unique_id}"
    new_path = trash_root / new_name

    try:
        os.rename(str(source_path), str(new_path))
        return True, "", str(new_path)
    except OSError as e:
        return False, f"move to trash failed: {e}", None
