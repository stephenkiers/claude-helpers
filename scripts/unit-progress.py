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
import hashlib
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
    commit_seq: Optional[int]  # Monotonic sequence for parallel mode chain tips


class CeilingsInfo(TypedDict):
    """Ceilings info for a single unit."""
    enforced: bool
    steps: Optional[int]
    files: Optional[int]
    findings: Optional[int]


class UnitsJson(TypedDict):
    """Schema for units.json."""
    schema_version: int
    plan_sha256: Optional[str]  # None if no plan was provided
    anchor_sha: str
    base_sha: str
    split_basis: SplitBasis
    single_reason: Optional[str]
    partial_work: PartialWorkMode
    partial_work_reason: str
    sizing: Dict[str, Any]
    ceilings: Dict[str, Any]  # Per-unit provisional ceilings
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


def is_valid_hex_sha(sha: str) -> bool:
    """Check if a SHA is a valid hex string (7-40 characters)."""
    return bool(re.fullmatch(r"^[0-9a-f]{7,40}$", sha))


def is_ancestor(ancestor_sha: str, descendant_sha: str, git_dir: Optional[str] = None) -> bool:
    """Check if ancestor_sha is an ancestor of descendant_sha using git merge-base.

    Args:
        ancestor_sha: The potential ancestor SHA.
        descendant_sha: The potential descendant SHA.
        git_dir: Optional git repository directory.

    Returns:
        True if ancestor_sha is an ancestor of descendant_sha, False otherwise.
        Raises SystemExit on git error.
    """
    if not is_valid_hex_sha(ancestor_sha) or not is_valid_hex_sha(descendant_sha):
        error("unit-progress: invalid SHA format")

    cmd = ["git"]
    if git_dir:
        cmd.extend(["-C", git_dir])
    cmd.extend(["merge-base", "--is-ancestor", ancestor_sha, descendant_sha])

    returncode, _ = run_git_cmd(cmd)
    # Return code 0: is ancestor
    # Return code 1: not ancestor
    # Return code 128+: error (e.g., object doesn't exist) - treat as not ancestor
    return returncode == 0


def get_current_head(git_dir: Optional[str] = None) -> str:
    """Get the current HEAD SHA.

    Args:
        git_dir: Optional git repository directory.

    Returns:
        The current HEAD SHA.
    """
    cmd = ["git"]
    if git_dir:
        cmd.extend(["-C", git_dir])
    cmd.extend(["rev-parse", "HEAD"])

    returncode, sha = run_git_cmd(cmd)
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
            try:
                os.unlink(temp_path_str)
            except OSError:
                pass
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


def read_units(file_path: Path) -> UnitsJson:
    """Read and validate units.json structure.

    Args:
        file_path: Path to units.json.

    Returns:
        Validated UnitsJson data.

    Raises:
        SystemExit(1) on validation failure.
    """
    data = read_json_file(file_path)
    if not isinstance(data, dict):
        error("unit-progress: units.json is not a dict")

    # Validate required fields
    required = ["schema_version", "anchor_sha", "base_sha", "split_basis", "units"]
    for field in required:
        if field not in data:
            error(f"unit-progress: units.json missing {field}")

    # Validate schema version
    if data.get("schema_version") != 1:
        error("unit-progress: unknown or missing schema_version in units.json")

    # Validate units list
    units_list = data.get("units")
    if not isinstance(units_list, list):
        error("unit-progress: units is not a list")

    for unit in units_list:
        if not isinstance(unit, dict):
            error("unit-progress: unit is not a dict")
        if not isinstance(unit.get("unit_id"), str) or not unit.get("unit_id"):
            error("unit-progress: all units must have non-empty unit_id")
        if not isinstance(unit.get("owned_files"), list):
            error("unit-progress: unit owned_files must be a list")

    return cast(UnitsJson, data)


def read_progress(file_path: Path) -> ProgressJson:
    """Read and validate progress.json structure.

    Args:
        file_path: Path to progress.json.

    Returns:
        Validated ProgressJson data.

    Raises:
        SystemExit(1) on validation failure.
    """
    data = read_json_file(file_path)
    if not isinstance(data, dict):
        error("unit-progress: progress.json is not a dict")

    if "progress" not in data:
        error("unit-progress: progress.json missing 'progress' field")

    # Validate schema version
    if data.get("schema_version") != 1:
        error("unit-progress: unknown or missing schema_version in progress.json")

    progress_list = data.get("progress")
    if not isinstance(progress_list, list):
        error("unit-progress: progress is not a list")

    for entry in progress_list:
        if not isinstance(entry, dict):
            error("unit-progress: progress entry is not a dict")

    return cast(ProgressJson, data)


def compute_tip(units_data: UnitsJson, progress_list: List[UnitProgress]) -> str:
    """Compute the chain tip SHA for a progress record.

    For parallel mode: the committed entry with the highest commit_seq.
    For sequential mode: the last committed unit in declared order, or base if none committed.

    Args:
        units_data: The units.json data.
        progress_list: The progress list from progress.json.

    Returns:
        The tip SHA.

    Raises:
        SystemExit(1) on validation failure.
    """
    tip_sha = units_data.get("base_sha")
    split_basis = units_data.get("split_basis", "parallel")
    units_list_raw = units_data.get("units", [])

    if not isinstance(units_list_raw, list):
        error("unit-progress: units is not a list")

    if not units_list_raw:
        return tip_sha or error("unit-progress: no base_sha")

    if split_basis == "parallel":
        # Find committed entry with highest commit_seq
        max_seq: Optional[int] = None
        max_sha: Optional[str] = None

        for entry in progress_list:
            if entry.get("status") == "committed":
                seq = entry.get("commit_seq")
                if seq is not None and (max_seq is None or seq > max_seq):
                    max_seq = seq
                    max_sha = entry.get("sha")

        if max_sha:
            return max_sha
    else:
        # Sequential: iterate through declared order
        units_list = cast(List[Dict[str, Any]], units_list_raw)
        for unit in units_list:
            unit_id = unit.get("unit_id")
            for entry in progress_list:
                if entry.get("unit_id") == unit_id and entry.get("status") == "committed":
                    tip_sha = entry.get("sha")
                    break

    return tip_sha or error("unit-progress: no base_sha")


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
        plan_path: Path to plan file for SHA256 (required; store null if not provided).
        sizing_path: Path to sizing JSON (optional; can come from stdin).
        supersede: Whether to archive existing records.
        partial_work_mode: partial_work mode (none/keep/reset).
        partial_work_reason: Reason for partial work mode.

    Raises:
        SystemExit(1) on validation failure.
    """
    # Validate SHA formats
    if not is_valid_hex_sha(anchor_sha):
        error("unit-progress: init: invalid anchor_sha format")
    if not is_valid_hex_sha(base_sha):
        error("unit-progress: init: invalid base_sha format")

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
        if min_units < 1:
            error(f"unit-progress: init: sizing.min_units must be >= 1, got {min_units}")
        if len(units_input) < min_units:
            error(f"unit-progress: init: {len(units_input)} units < min_units {min_units}")

    # Validation: unit structure (all units must be dicts with required fields)
    for unit_item in units_input:
        if not isinstance(unit_item, dict):
            error("unit-progress: init: all units must be dicts")
        if not isinstance(unit_item.get("unit_id"), str) or not unit_item.get("unit_id"):
            error("unit-progress: init: all units must have non-empty unit_id")
        if not isinstance(unit_item.get("owned_files"), list):
            error("unit-progress: init: all units must have owned_files as a list")

    # Validation: unit_id uniqueness
    unit_ids = [u.get("unit_id") for u in units_input]
    if len(unit_ids) != len(set(unit_ids)):
        error("unit-progress: init: unit_ids must be unique")

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

    # Read plan SHA256 from file (required)
    plan_sha256: Optional[str]
    if plan_path:
        try:
            with open(plan_path, "r", encoding="utf-8") as f:
                plan_content = f.read()
            plan_sha256 = hashlib.sha256(plan_content.encode("utf-8")).hexdigest()
        except (OSError, UnicodeDecodeError) as e:
            error(f"unit-progress: init: failed to read plan: {e}")
    else:
        # Store null if no plan provided
        plan_sha256 = None

    # Check for existing records
    units_file = record_dir / "units.json"
    progress_file = record_dir / "progress.json"

    existing_units: Optional[UnitsJson] = None
    existing_progress: Optional[ProgressJson] = None

    if units_file.exists() or progress_file.exists():
        if not supersede:
            error("unit-progress: init: record exists; use --supersede to replace")

        # Read existing records (tolerate missing half)
        if units_file.exists():
            existing_units = read_units(units_file)
        if progress_file.exists():
            existing_progress = read_progress(progress_file)

        # Validate: check if any units are committed (before archiving)
        if existing_progress:
            progress_entries = existing_progress.get("progress", [])
            for progress_entry in progress_entries:
                if progress_entry.get("status") == "committed":
                    error("unit-progress: init: --supersede only allowed when no units are committed")

        # Archive the existing pair (only if both exist or if we have something to save)
        if existing_units or existing_progress:
            archive_num = 0
            while True:
                archive_num += 1
                archive_units = record_dir / f"units.a{archive_num}.json"
                archive_progress = record_dir / f"progress.a{archive_num}.json"
                if not archive_units.exists() and not archive_progress.exists():
                    break

            record_dir.mkdir(parents=True, exist_ok=True)
            if existing_units:
                write_json_file(archive_units, existing_units)
            if existing_progress:
                write_json_file(archive_progress, existing_progress)

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

    # Build ceilings info (provisional, not enforced)
    ceilings_info: Dict[str, Any] = {
        "enforced": False,
        "steps": 8,
        "files": 12,
        "findings": 16,
    }

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
        "ceilings": ceilings_info,
        "units": units_list,
    }

    # Create progress.json
    progress_list: List[UnitProgress] = [
        {"unit_id": unit["unit_id"], "sha": None, "status": "pending", "commit_seq": None}
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

    if status != "committed" and sha:
        error("unit-progress: mark: --sha is only valid with committed status")

    if status == "committed" and sha:
        if not is_valid_hex_sha(sha):
            error("unit-progress: mark: invalid sha format")
        rc, _ = run_git_cmd(["git", "cat-file", "-e", f"{sha}^{{commit}}"])
        if rc != 0:
            error(f"unit-progress: mark: {sha} is not a commit in this repository")

    # Check for units.json (required)
    units_file = record_dir / "units.json"
    if not units_file.exists():
        error("unit-progress: mark: units.json does not exist")

    progress_file = record_dir / "progress.json"
    if not progress_file.exists():
        error("unit-progress: mark: progress.json does not exist")

    # Read and validate the records
    units_data = read_units(units_file)
    progress_data = read_progress(progress_file)

    # Find and update the unit
    progress_list_raw = progress_data.get("progress", [])
    if not isinstance(progress_list_raw, list):
        error("unit-progress: mark: progress is not a list")
    progress_list = cast(List[UnitProgress], progress_list_raw)

    if status in ("committed", "failed", "skipped"):
        if units_data.get("split_basis") == "sequential":
            units_list = units_data.get("units", [])
            order = [u.get("unit_id") for u in cast(List[Dict[str, Any]], units_list)]
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

            entry["status"] = cast(UnitStatus, status)

            # Only store sha for committed status; clear it for others
            if status == "committed" and sha:
                entry["sha"] = sha
                # Assign commit_seq: max existing + 1
                max_seq = 0
                for other_entry in progress_list:
                    other_seq = other_entry.get("commit_seq")
                    if isinstance(other_seq, int) and other_seq > max_seq:
                        max_seq = other_seq
                entry["commit_seq"] = max_seq + 1
            else:
                entry["sha"] = None
                entry["commit_seq"] = None
            break

    if not found:
        error(f"unit-progress: mark: unit {unit_id} not found")

    # Write updated progress
    write_json_file(progress_file, progress_data)


def cmd_check(
    record_dir: Path,
    plan_path: str,
    anchor_sha: str,
    head_sha: str,
    git_dir: Optional[str] = None,
) -> None:
    """Consistency check for re-runs.

    Args:
        record_dir: Directory containing units.json and progress.json.
        plan_path: Path to plan file for SHA256 comparison.
        anchor_sha: Expected anchor SHA.
        head_sha: Expected HEAD SHA (can be passed or auto-detected).
        git_dir: Optional git repository directory for ancestor checks.

    Raises:
        SystemExit(3) if no record exists.
        SystemExit(1) on any mismatch.
    """
    units_file = record_dir / "units.json"
    progress_file = record_dir / "progress.json"

    # Exit 3 if no record exists (fresh run)
    if not units_file.exists() or not progress_file.exists():
        print("unit-progress: check: no record files found", file=sys.stderr)
        sys.exit(3)

    # Read and validate the records
    units_data = read_units(units_file)
    progress_data = read_progress(progress_file)

    # Validate required fields exist
    for field in ["anchor_sha", "base_sha", "split_basis", "units"]:
        if field not in units_data:
            error(f"unit-progress: check: missing field {field} in units.json")

    # Validate SHA formats
    if not is_valid_hex_sha(anchor_sha):
        error("unit-progress: check: invalid anchor_sha format")
    if not is_valid_hex_sha(head_sha):
        error("unit-progress: check: invalid head_sha format")

    # Check plan SHA256 (if plan_sha256 is set)
    stored_plan_sha = units_data.get("plan_sha256")
    if stored_plan_sha is not None:
        try:
            with open(plan_path, "r", encoding="utf-8") as f:
                plan_content = f.read()
            current_plan_sha = hashlib.sha256(plan_content.encode("utf-8")).hexdigest()
        except (OSError, UnicodeDecodeError) as e:
            error(f"unit-progress: check: failed to read plan: {e}")

        if stored_plan_sha != current_plan_sha:
            error("unit-progress: check: plan_sha256 mismatch")

    # Check anchor SHA
    if units_data.get("anchor_sha") != anchor_sha:
        error("unit-progress: check: anchor_sha mismatch")

    # A scoped record must chain from the main record: same anchor, and its base is on HEAD's history.
    if re.fullmatch(r"scoped-a[0-9]+", record_dir.name):
        main_units = record_dir.parent / "units.json"
        if not main_units.exists():
            error("unit-progress: check: scoped record has no main units.json to chain from")
        main_data = read_units(main_units)
        if main_data.get("anchor_sha") != units_data.get("anchor_sha"):
            error("unit-progress: check: scoped anchor_sha does not match main record")
        scoped_base = units_data.get("base_sha")
        if not isinstance(scoped_base, str):
            error("unit-progress: check: scoped base_sha is not a string")
        if not is_ancestor(scoped_base, head_sha, git_dir):
            error("unit-progress: check: scoped base_sha is not an ancestor of HEAD")

    # Check committed SHAs are reachable from HEAD
    progress_list = progress_data.get("progress", [])

    for entry in progress_list:
        if entry.get("status") == "committed":
            commit_sha = entry.get("sha")
            if commit_sha:
                if not is_valid_hex_sha(commit_sha):
                    error("unit-progress: check: invalid commit SHA format")
                if not is_ancestor(commit_sha, head_sha, git_dir):
                    error(f"unit-progress: check: committed SHA {commit_sha} not reachable from HEAD")

    # Validate unit id-set equality between units.json and progress.json
    unit_ids_from_units = set(u.get("unit_id") for u in units_data.get("units", []))
    unit_ids_from_progress = set(e.get("unit_id") for e in progress_list)
    if unit_ids_from_units != unit_ids_from_progress:
        error("unit-progress: check: unit id mismatch between units.json and progress.json")

    # Compute tip SHA using helper
    tip_sha = compute_tip(units_data, progress_list)

    # Check tip is ancestor of (or equal to) HEAD
    if tip_sha != head_sha and not is_ancestor(tip_sha, head_sha, git_dir):
        error(f"unit-progress: check: chain tip {tip_sha} is not an ancestor of HEAD {head_sha}")


def cmd_pending(record_dir: Path) -> None:
    """List pending or failed unit IDs.

    Args:
        record_dir: Directory containing progress.json and units.json.

    Raises:
        SystemExit(1) if records are missing.
    """
    progress_file = record_dir / "progress.json"
    units_file = record_dir / "units.json"

    if not progress_file.exists() or not units_file.exists():
        error("unit-progress: pending: record files do not exist")

    progress_data = read_progress(progress_file)
    units_data = read_units(units_file)

    progress_list = progress_data.get("progress", [])
    units_list = units_data.get("units", [])

    # Validate unit id-set equality
    unit_ids_from_units = set(u.get("unit_id") for u in units_list)
    unit_ids_from_progress = set(e.get("unit_id") for e in progress_list)
    if unit_ids_from_units != unit_ids_from_progress:
        error("unit-progress: pending: unit id mismatch between units.json and progress.json")

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

    units_data = read_units(units_file)
    progress_data = read_progress(progress_file)

    progress_list = progress_data.get("progress", [])
    tip_sha = compute_tip(units_data, progress_list)

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
            # Use passed --head if provided, otherwise detect from current git repo
            head_sha = args.head
            git_dir = None  # Use current directory for git commands

            # If head_sha looks like it might need git lookup (or we want current HEAD),
            # we could pass git_dir to cmd_check. For now, use what was passed.
            cmd_check(
                record_dir=record_dir,
                plan_path=args.plan,
                anchor_sha=args.anchor,
                head_sha=head_sha,
                git_dir=git_dir,
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
