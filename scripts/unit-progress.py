#!/usr/bin/env python3
"""
Unit progress tracker for parallel/sequential/single execution.

The only code that writes units.json and progress.json, similar to how flow-ledger.py
is the only writer of human.jsonl.

Record files:
- units.json: schema_version, plan_sha256, anchor_sha, base_sha, split_basis,
  single_reason, partial_work, sizing result, and ordered unit list.
- progress.json: schema_version, plus one entry per unit with unit_id, sha, and status.

Subcommands:
  init: Write both files from a unit list on stdin as JSON.
  mark: Update a unit's status.
  check: Consistency check for re-runs.
  pending: List pending or failed unit ids.
  tip: Print the chain tip SHA.

Exit codes:
  0: success
  1: refusal/mismatch (one-line reason on stderr)
  2: usage error
"""

import argparse
import json
import os
import subprocess
import re
import sys
import tempfile
from pathlib import Path
from typing import (
    Any, Dict, List, Literal, NoReturn, Optional, Set, Tuple, TypedDict, Union, cast, get_args,
)

# Add scripts directory to path for sibling imports
sys.path.insert(0, str(Path(__file__).parent))
from flow_log import validate_flow_dir as validate_flow_dir_lib  # type: ignore

# Type definitions for statuses and bases
UnitStatus = Literal["pending", "committed", "failed", "skipped"]
PartialWorkMode = Literal["none", "keep", "reset"]
SplitBasis = Literal["parallel", "sequential", "single"]

UNIT_STATUSES: Tuple[UnitStatus, ...] = get_args(UnitStatus)
PARTIAL_WORK_MODES: Tuple[PartialWorkMode, ...] = get_args(PartialWorkMode)
SPLIT_BASES: Tuple[SplitBasis, ...] = get_args(SplitBasis)


class Unit(TypedDict):
    """A unit in the plan."""
    unit_id: str
    sub_task: str
    owned_files: List[str]
    order: int


class UnitProgress(TypedDict):
    """Progress entry for a unit."""
    unit_id: str
    sha: Optional[str]
    status: UnitStatus


class UnitsJson(TypedDict):
    """Schema for units.json."""
    schema_version: int
    plan_sha256: str
    anchor_sha: str
    base_sha: str
    split_basis: SplitBasis
    single_reason: Optional[str]
    partial_work: PartialWorkMode
    partial_work_reason: str
    sizing: Dict[str, Any]
    units: List[Unit]


class ProgressJson(TypedDict):
    """Schema for progress.json."""
    schema_version: int
    progress: List[UnitProgress]


def error(msg: str) -> NoReturn:
    """Print error to stderr and exit with code 1."""
    print(msg, file=sys.stderr)
    sys.exit(1)


def validate_record_dir(path: str, step: str, scope: Optional[str] = None, is_flow_dir: bool = False) -> Path:
    """Validate and return the units/<step>/ or units/<step>/<scope>/ directory path.

    Args:
        path: Either a flow directory (absolute) or scratch path.
        step: The step name (implement or fix).
        scope: Optional scope name for scoped records.
        is_flow_dir: True if this is a flow directory, False for scratch paths.

    Returns:
        The validated record directory path.

    Raises:
        SystemExit(1) on validation failure.
    """
    if not path:
        error("unit-progress: --flow-dir or --scratch is required")

    if is_flow_dir:
        # Use flow_log validation for flow directories
        try:
            base = validate_flow_dir_lib(path)
        except Exception as e:  # type: ignore
            error(f"unit-progress: {e}")
    else:
        # For scratch paths, just resolve and use as-is
        base = Path(path).expanduser().resolve()

    # Build the record directory path
    record_dir = base / "units" / step
    if scope:
        # The scope is the full directory name (e.g. "scoped-a2"); never prefix it,
        # and refuse anything that could escape units/<step>/ or alias the main record.
        if not re.fullmatch(r"scoped-a[0-9]+", scope):
            error(f"unit-progress: --scope must look like 'scoped-a<N>', got {scope!r}")
        record_dir = record_dir / scope

    # Create directory if it doesn't exist (only for actual writes)
    return record_dir


def run_git_cmd(cmd: List[str]) -> Tuple[int, str]:
    """Run a git command and return (returncode, stdout).

    Args:
        cmd: Command list (e.g., ['git', 'rev-parse', 'HEAD']).

    Returns:
        Tuple of (exit code, stdout).
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode, result.stdout.strip()
    except subprocess.TimeoutExpired:
        error("unit-progress: git command timed out")
    except OSError as e:
        error(f"unit-progress: failed to run git: {e}")


def is_ancestor(ancestor_sha: str, descendant_sha: str) -> bool:
    """Check if ancestor_sha is an ancestor of descendant_sha using git merge-base."""
    returncode, _ = run_git_cmd(["git", "merge-base", "--is-ancestor", ancestor_sha, descendant_sha])
    return returncode == 0


def get_current_head() -> str:
    """Get the current HEAD SHA."""
    returncode, sha = run_git_cmd(["git", "rev-parse", "HEAD"])
    if returncode != 0:
        error("unit-progress: failed to get current HEAD SHA")
    return sha


def fsync_dir(directory: Path) -> None:
    """Flush a directory entry. Best effort."""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_json_file(file_path: Path, data: Union[UnitsJson, ProgressJson]) -> None:
    """Write JSON to file via temp file + os.replace.

    Args:
        file_path: Target file path.
        data: Data to write.

    Raises:
        SystemExit(1) on write failure.
    """
    temp_path_str: Optional[str] = None
    try:
        temp_fd, temp_path_str = tempfile.mkstemp(
            dir=file_path.parent,
            prefix=f".{file_path.name}-",
            suffix=".tmp",
        )
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, separators=(",", ":"))
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            os.close(temp_fd)
            raise
        os.chmod(temp_path_str, 0o600)
        os.replace(temp_path_str, str(file_path))
        fsync_dir(file_path.parent)
    except (OSError, IOError) as e:
        if temp_path_str:
            try:
                os.unlink(temp_path_str)
            except OSError:
                pass
        error(f"unit-progress: write failed: {e}")


def read_json_file(file_path: Path) -> Union[UnitsJson, ProgressJson]:
    """Read and parse JSON file.

    Args:
        file_path: File to read.

    Returns:
        Parsed JSON data.

    Raises:
        SystemExit(1) on read/parse failure.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        error(f"unit-progress: failed to read {file_path.name}: {e}")


def cmd_init(
    record_dir: Path,
    anchor_sha: str,
    base_sha: str,
    plan_path: Optional[str],
    sizing_path: Optional[str],
    supersede: bool,
    partial_work_mode: Optional[str],
    partial_work_reason: Optional[str],
) -> None:
    """Initialize units and progress records.

    Args:
        record_dir: Directory to write records to.
        anchor_sha: Anti-tamper SHA.
        base_sha: Unit-branching base SHA.
        plan_path: Path to plan file for SHA256 (optional; can come from stdin).
        sizing_path: Path to sizing JSON (optional; can come from stdin).
        supersede: Whether to archive existing records.
        partial_work_mode: partial_work mode (none/keep/reset).
        partial_work_reason: Reason for partial work mode.

    Raises:
        SystemExit(1) on validation failure.
    """
    import hashlib

    # Read unit list from stdin
    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        error(f"unit-progress: init: invalid JSON on stdin: {e}")

    # Validate input structure
    if not isinstance(input_data, dict):
        error("unit-progress: init: input must be a dict")

    # Accept both "split_basis" and "basis" for backwards compatibility
    split_basis: str = input_data.get("split_basis") or input_data.get("basis", "")
    single_reason_input: Optional[str] = input_data.get("single_reason")
    units_input: List[Any] = input_data.get("units", [])

    if split_basis not in SPLIT_BASES:
        error(f"unit-progress: init: split_basis must be one of {SPLIT_BASES}")

    # Read sizing JSON from file or stdin
    sizing_data: Dict[str, Any]
    if sizing_path:
        try:
            with open(sizing_path, "r", encoding="utf-8") as f:
                sizing_data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            error(f"unit-progress: init: failed to read sizing: {e}")
    else:
        # Try to read sizing from stdin
        stdin_sizing = input_data.get("sizing")
        if stdin_sizing is None:
            error("unit-progress: init: sizing must be provided via --sizing or in stdin")
        sizing_data = stdin_sizing

    # Validate sizing has required fields
    if not isinstance(sizing_data, dict):
        error("unit-progress: init: sizing must be a dict")

    min_units_raw = sizing_data.get("min_units")
    if min_units_raw is not None and not isinstance(min_units_raw, int):
        if min_units_raw != "unknown":
            error("unit-progress: init: sizing.min_units must be int or 'unknown'")

    # Validation: min_units checks
    min_units: Optional[int] = None
    if isinstance(min_units_raw, int):
        min_units = min_units_raw
        if len(units_input) < min_units:
            error(f"unit-progress: init: {len(units_input)} units < min_units {min_units}")

    # Validation: single split basis
    if split_basis == "single":
        if min_units is not None and min_units > 1:
            error(f"unit-progress: init: single basis with min_units {min_units}")
        if not single_reason_input:
            error("unit-progress: init: single basis requires single_reason")
        if len(units_input) != 1:
            error("unit-progress: init: single basis requires exactly 1 unit")

    # Validation: sequential split basis
    if split_basis == "sequential":
        if len(units_input) <= 1:
            error("unit-progress: init: sequential basis requires > 1 unit")

    # Validation: parallel split basis - check for overlapping owned_files
    if split_basis == "parallel":
        seen_files: Set[str] = set()
        for unit_item in units_input:
            owned_files_raw = unit_item.get("owned_files", [])
            if not isinstance(owned_files_raw, list):
                error("unit-progress: init: owned_files must be a list")
            owned_files: List[str] = cast(List[str], owned_files_raw)
            for file_path_item in owned_files:
                if file_path_item in seen_files:
                    error(f"unit-progress: init: parallel basis: file {file_path_item} owned by multiple units")
                seen_files.add(file_path_item)

    # Read plan SHA256 from file or compute from stdin
    if plan_path:
        try:
            with open(plan_path, "r", encoding="utf-8") as f:
                plan_content = f.read()
            plan_sha256 = hashlib.sha256(plan_content.encode("utf-8")).hexdigest()
        except (OSError, UnicodeDecodeError) as e:
            error(f"unit-progress: init: failed to read plan: {e}")
    else:
        # Compute from stdin JSON for testing
        plan_sha256 = hashlib.sha256(json.dumps(input_data, sort_keys=True).encode("utf-8")).hexdigest()

    # Check for existing records
    units_file = record_dir / "units.json"
    progress_file = record_dir / "progress.json"

    if units_file.exists() or progress_file.exists():
        if not supersede:
            error("unit-progress: init: record exists; use --supersede to replace")
        # Archive existing records
        existing_units: Union[UnitsJson, ProgressJson] = read_json_file(units_file)
        existing_progress: Union[UnitsJson, ProgressJson] = read_json_file(progress_file)

        # Check if any units are committed
        if isinstance(existing_progress, dict) and "progress" in existing_progress:
            progress_entries = existing_progress.get("progress", [])  # type: ignore
            if isinstance(progress_entries, list):
                for progress_entry in progress_entries:
                    if isinstance(progress_entry, dict) and progress_entry.get("status") == "committed":
                        error("unit-progress: init: --supersede only allowed when no units are committed")

        # Archive the existing pair
        archive_num = 0
        while True:
            archive_num += 1
            archive_units = record_dir / f"units.a{archive_num}.json"
            archive_progress = record_dir / f"progress.a{archive_num}.json"
            if not archive_units.exists() and not archive_progress.exists():
                break

        record_dir.mkdir(parents=True, exist_ok=True)
        write_json_file(archive_units, existing_units)  # type: ignore
        write_json_file(archive_progress, existing_progress)  # type: ignore

    # Build units with order
    units_list: List[Unit] = []
    for order, unit_data in enumerate(units_input):
        unit_rec: Unit = {
            "unit_id": unit_data.get("unit_id", ""),
            "sub_task": unit_data.get("sub_task", ""),
            "owned_files": unit_data.get("owned_files", []),
            "order": order,
        }
        if not unit_rec["unit_id"]:
            error("unit-progress: init: all units must have unit_id")
        units_list.append(unit_rec)

    # Determine partial_work values
    pw_mode: PartialWorkMode = cast(PartialWorkMode, partial_work_mode or "none")
    pw_reason = partial_work_reason or ""
    if pw_mode not in PARTIAL_WORK_MODES:
        error(f"unit-progress: init: --partial-work must be one of {PARTIAL_WORK_MODES}")

    # Create units.json
    record_dir.mkdir(parents=True, exist_ok=True)
    units_json: UnitsJson = {
        "schema_version": 1,
        "plan_sha256": plan_sha256,
        "anchor_sha": anchor_sha,
        "base_sha": base_sha,
        "split_basis": cast(SplitBasis, split_basis),
        "single_reason": single_reason_input,
        "partial_work": pw_mode,
        "partial_work_reason": pw_reason,
        "sizing": sizing_data,
        "units": units_list,
    }

    # Create progress.json
    progress_list: List[UnitProgress] = [
        {"unit_id": unit["unit_id"], "sha": None, "status": "pending"}
        for unit in units_list
    ]
    progress_json: ProgressJson = {
        "schema_version": 1,
        "progress": progress_list,
    }

    # Write files
    write_json_file(units_file, units_json)
    write_json_file(progress_file, progress_json)


def cmd_mark(
    record_dir: Path,
    unit_id: str,
    status: str,
    sha: Optional[str],
) -> None:
    """Mark a unit with a new status.

    Args:
        record_dir: Directory containing progress.json.
        unit_id: The unit ID to update.
        status: New status (pending, committed, failed, skipped).
        sha: SHA (required for committed status).

    Raises:
        SystemExit(1) on validation failure.
    """
    if status not in UNIT_STATUSES:
        error(f"unit-progress: mark: status must be one of {UNIT_STATUSES}")

    if status == "committed" and not sha:
        error("unit-progress: mark: committed status requires --sha")

    progress_file = record_dir / "progress.json"
    if not progress_file.exists():
        error("unit-progress: mark: progress.json does not exist")

    progress_data: Union[UnitsJson, ProgressJson] = read_json_file(progress_file)
    if "progress" not in progress_data:
        error("unit-progress: mark: progress.json missing 'progress' field")

    # Find and update the unit
    progress_list_raw = progress_data.get("progress", [])  # type: ignore
    if not isinstance(progress_list_raw, list):
        error("unit-progress: mark: progress is not a list")
    progress_list = cast(List[Dict[str, Any]], progress_list_raw)

    if status in ("committed", "failed", "skipped"):
        units_file = record_dir / "units.json"
        if units_file.exists():
            units_rec = read_json_file(units_file)
            if units_rec.get("split_basis") == "sequential":
                order = [u.get("unit_id") for u in cast(List[Dict[str, Any]], units_rec.get("units", []))]
                if unit_id in order:
                    earlier = set(order[: order.index(unit_id)])
                    for prior in progress_list:
                        if prior.get("unit_id") in earlier and prior.get("status") not in ("committed", "skipped"):
                            error(
                                f"unit-progress: mark: sequential order: {prior.get('unit_id')} "
                                f"is {prior.get('status')}, must be committed or skipped before {unit_id}"
                            )

    found = False
    for entry in progress_list:
        if entry.get("unit_id") == unit_id:
            found = True
            current_status = entry.get("status")

            # Check allowed transitions
            allowed_from: Dict[UnitStatus, List[UnitStatus]] = {
                "pending": ["committed", "failed", "skipped", "pending"],
                "failed": ["committed", "failed", "skipped", "pending"],
                "committed": [],
                "skipped": [],
            }
            if current_status not in allowed_from or status not in allowed_from[current_status]:  # type: ignore
                error(f"unit-progress: mark: cannot transition from {current_status} to {status}")

            entry["status"] = status
            if sha:
                entry["sha"] = sha
            break

    if not found:
        error(f"unit-progress: mark: unit {unit_id} not found")

    # Write updated progress
    write_json_file(progress_file, progress_data)  # type: ignore


def cmd_check(
    record_dir: Path,
    plan_path: str,
    anchor_sha: str,
    head_sha: str,
) -> None:
    """Consistency check for re-runs.

    Args:
        record_dir: Directory containing units.json and progress.json.
        plan_path: Path to plan file for SHA256 comparison.
        anchor_sha: Expected anchor SHA.
        head_sha: Expected HEAD SHA.

    Raises:
        SystemExit(1) on any mismatch.
    """
    units_file = record_dir / "units.json"
    progress_file = record_dir / "progress.json"

    if not units_file.exists() or not progress_file.exists():
        error("unit-progress: check: record files do not exist")

    units_data: Union[UnitsJson, ProgressJson] = read_json_file(units_file)
    progress_data: Union[UnitsJson, ProgressJson] = read_json_file(progress_file)

    # Check schema versions
    if units_data.get("schema_version") != 1:
        error("unit-progress: check: unknown or missing schema_version in units.json")
    if progress_data.get("schema_version") != 1:
        error("unit-progress: check: unknown or missing schema_version in progress.json")

    # Validate required fields exist
    for field in ["plan_sha256", "anchor_sha", "base_sha", "split_basis", "units"]:
        if field not in units_data:
            error(f"unit-progress: check: missing field {field} in units.json")

    if "progress" not in progress_data:
        error("unit-progress: check: missing field 'progress' in progress.json")

    # Check plan SHA256
    try:
        with open(plan_path, "r", encoding="utf-8") as f:
            plan_content = f.read()
        import hashlib
        current_plan_sha = hashlib.sha256(plan_content.encode("utf-8")).hexdigest()
    except (OSError, UnicodeDecodeError) as e:
        error(f"unit-progress: check: failed to read plan: {e}")

    if units_data.get("plan_sha256") != current_plan_sha:
        error("unit-progress: check: plan_sha256 mismatch")

    # Check anchor SHA
    if units_data.get("anchor_sha") != anchor_sha:
        error("unit-progress: check: anchor_sha mismatch")

    # A scoped record must chain from the main record: same anchor, and its base is on HEAD's history.
    if re.fullmatch(r"scoped-a[0-9]+", record_dir.name):
        main_units = record_dir.parent / "units.json"
        if not main_units.exists():
            error("unit-progress: check: scoped record has no main units.json to chain from")
        main_data = read_json_file(main_units)
        if main_data.get("anchor_sha") != units_data.get("anchor_sha"):
            error("unit-progress: check: scoped anchor_sha does not match main record")
        scoped_base = units_data.get("base_sha")
        if not isinstance(scoped_base, str) or not is_ancestor(scoped_base, head_sha):
            error("unit-progress: check: scoped base_sha is not an ancestor of HEAD")

    # Check committed SHAs are reachable from HEAD
    progress_list_raw = progress_data.get("progress", [])  # type: ignore
    if not isinstance(progress_list_raw, list):
        error("unit-progress: check: progress is not a list")
    progress_list = cast(List[Dict[str, Any]], progress_list_raw)

    for entry in progress_list:
        if entry.get("status") == "committed":
            commit_sha = entry.get("sha")
            if commit_sha and not is_ancestor(commit_sha, head_sha):
                error(f"unit-progress: check: committed SHA {commit_sha} not reachable from HEAD")

    # Calculate tip SHA
    tip_sha = units_data.get("base_sha")
    units_list_raw = units_data.get("units", [])  # type: ignore
    if not isinstance(units_list_raw, list):
        error("unit-progress: check: units is not a list")
    units_list = cast(List[Dict[str, Any]], units_list_raw)

    if units_list:
        # Tip is the last committed unit's SHA, or base_sha if none committed
        for unit in units_list:
            unit_id = unit.get("unit_id")
            for entry in progress_list:
                if entry.get("unit_id") == unit_id and entry.get("status") == "committed":
                    tip_sha = entry.get("sha")

    # Check HEAD == tip
    if head_sha != tip_sha:
        error(f"unit-progress: check: HEAD {head_sha} != chain tip {tip_sha}")


def cmd_pending(record_dir: Path) -> None:
    """List pending or failed unit IDs.

    Args:
        record_dir: Directory containing progress.json.
    """
    progress_file = record_dir / "progress.json"
    if not progress_file.exists():
        return

    progress_data: Union[UnitsJson, ProgressJson] = read_json_file(progress_file)
    progress_list_raw = progress_data.get("progress", [])  # type: ignore
    if not isinstance(progress_list_raw, list):
        return
    progress_list = cast(List[Dict[str, Any]], progress_list_raw)

    # Get units in order
    units_file = record_dir / "units.json"
    if not units_file.exists():
        return

    units_data: Union[UnitsJson, ProgressJson] = read_json_file(units_file)
    units_list_raw = units_data.get("units", [])  # type: ignore
    if not isinstance(units_list_raw, list):
        return
    units_list = cast(List[Dict[str, Any]], units_list_raw)

    # Print pending or failed units in order
    for unit in units_list:
        unit_id = unit.get("unit_id")
        for entry in progress_list:
            if entry.get("unit_id") == unit_id:
                if entry.get("status") in ("pending", "failed"):
                    print(unit_id)
                break


def cmd_tip(record_dir: Path) -> None:
    """Print the chain tip SHA.

    Args:
        record_dir: Directory containing units.json and progress.json.
    """
    units_file = record_dir / "units.json"
    progress_file = record_dir / "progress.json"

    if not units_file.exists() or not progress_file.exists():
        error("unit-progress: tip: record files do not exist")

    units_data: Union[UnitsJson, ProgressJson] = read_json_file(units_file)
    progress_data: Union[UnitsJson, ProgressJson] = read_json_file(progress_file)

    tip_sha = units_data.get("base_sha")
    units_list_raw = units_data.get("units", [])  # type: ignore
    if not isinstance(units_list_raw, list):
        error("unit-progress: tip: units is not a list")
    units_list = cast(List[Dict[str, Any]], units_list_raw)

    progress_list_raw = progress_data.get("progress", [])  # type: ignore
    if not isinstance(progress_list_raw, list):
        error("unit-progress: tip: progress is not a list")
    progress_list = cast(List[Dict[str, Any]], progress_list_raw)

    if units_list:
        # Tip is the last committed unit's SHA, or base_sha if none committed
        for unit in units_list:
            unit_id = unit.get("unit_id")
            for entry in progress_list:
                if entry.get("unit_id") == unit_id and entry.get("status") == "committed":
                    tip_sha = entry.get("sha")

    print(tip_sha)


def main() -> None:
    """Parse arguments and dispatch to subcommand."""
    parser = argparse.ArgumentParser(
        description="Unit progress tracker for parallel/sequential/single execution"
    )

    # Global options
    parser.add_argument("--step", required=True, choices=["implement", "fix"], help="Step name")
    flow_group = parser.add_mutually_exclusive_group(required=True)
    flow_group.add_argument("--flow-dir", help="Flow directory")
    flow_group.add_argument("--scratch", help="Scratch path for non-flow runs")
    parser.add_argument("--scope", help="Scope name for scoped records")

    # Subcommands
    subparsers = parser.add_subparsers(dest="command", required=True, help="Subcommand")

    # init subcommand
    init_parser = subparsers.add_parser("init", help="Initialize units and progress records")
    init_parser.add_argument("--anchor", required=True, help="Anti-tamper SHA")
    init_parser.add_argument("--base", required=True, help="Unit-branching base SHA")
    init_parser.add_argument("--plan", help="Path to plan file (optional; can come from stdin)")
    init_parser.add_argument("--sizing", help="Path to sizing JSON (optional; can come from stdin)")
    init_parser.add_argument("--supersede", action="store_true", help="Archive existing records")
    init_parser.add_argument("--partial-work", choices=PARTIAL_WORK_MODES, help="Partial work mode")
    init_parser.add_argument("--partial-work-reason", help="Reason for partial work")

    # mark subcommand
    mark_parser = subparsers.add_parser("mark", help="Mark a unit with a new status")
    mark_parser.add_argument("--unit-id", required=True, help="Unit ID")
    mark_parser.add_argument("--status", required=True, choices=UNIT_STATUSES, help="New status")
    mark_parser.add_argument("--sha", help="SHA (required for committed status)")

    # check subcommand
    check_parser = subparsers.add_parser("check", help="Consistency check")
    check_parser.add_argument("--plan", required=True, help="Path to plan file")
    check_parser.add_argument("--anchor", required=True, help="Expected anchor SHA")
    check_parser.add_argument("--head", required=True, help="Expected HEAD SHA")

    # pending subcommand
    subparsers.add_parser("pending", help="List pending or failed unit IDs")

    # tip subcommand
    subparsers.add_parser("tip", help="Print the chain tip SHA")

    args = parser.parse_args()

    # Determine record directory
    flow_dir_arg: Optional[str] = getattr(args, "flow_dir", None)
    scratch_arg: Optional[str] = getattr(args, "scratch", None)
    dir_path = flow_dir_arg or scratch_arg
    if not dir_path:
        error("unit-progress: --flow-dir or --scratch is required")
    is_flow_dir = flow_dir_arg is not None
    record_dir = validate_record_dir(dir_path, args.step, args.scope, is_flow_dir=is_flow_dir)

    try:
        if args.command == "init":
            cmd_init(
                record_dir=record_dir,
                anchor_sha=args.anchor,
                base_sha=args.base,
                plan_path=args.plan,
                sizing_path=args.sizing,
                supersede=args.supersede,
                partial_work_mode=getattr(args, "partial_work", None),
                partial_work_reason=getattr(args, "partial_work_reason", None),
            )
        elif args.command == "mark":
            cmd_mark(
                record_dir=record_dir,
                unit_id=args.unit_id,
                status=args.status,
                sha=getattr(args, "sha", None),
            )
        elif args.command == "check":
            current_head = get_current_head()
            cmd_check(
                record_dir=record_dir,
                plan_path=args.plan,
                anchor_sha=args.anchor,
                head_sha=current_head,
            )
        elif args.command == "pending":
            cmd_pending(record_dir=record_dir)
        elif args.command == "tip":
            cmd_tip(record_dir=record_dir)
    except SystemExit:
        raise


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        error(f"unit-progress: unexpected error: {e}")
