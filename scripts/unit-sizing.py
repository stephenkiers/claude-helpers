#!/usr/bin/env python3
"""Estimate parallelizable work units from plan or action-plan files.

Usage: unit-sizing.py <plan-file> --kind plan|action-plan [--only-units <ids>] [--project-root DIR]

Prints one JSON object with fields:
  schema_version: 1
  kind: "plan" or "action-plan"
  steps, files, findings: int or null (when unparseable)
  ceilings: dict of active per-unit ceilings
  min_units: int >= 1, or "unknown"
  status: "ok" or "unknown"
  reason: string (empty on success; error message otherwise)

Config cascade (key by key): built-in defaults < ~/.claude/unit-sizing-heuristic.yaml
< <project-root>/.claude/unit-sizing-heuristic.yaml. Never fails the caller: on any
error it prints status: "unknown" with the error in "reason" and exits 0.
"""
import argparse
import copy
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

DEFAULTS: Dict[str, Any] = {
    "steps_per_unit": 8,
    "files_per_unit": 12,
    "findings_per_unit": 16,
}
CONFIG_NAME = "unit-sizing-heuristic.yaml"


def _load(path: Path) -> Dict[str, Any]:
    """Load YAML config file, surfacing parse errors to stderr."""
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        print("warning: PyYAML not available; using defaults", file=sys.stderr)
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    except yaml.YAMLError as e:  # type: ignore[union-attr]
        print(f"warning: failed to parse {path}: {e}", file=sys.stderr)
        return {}
    except Exception as e:
        print(f"warning: failed to read {path}: {e}", file=sys.stderr)
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        print(
            f"warning: {path}: top level must be a mapping, got {type(data).__name__}; ignoring file",
            file=sys.stderr,
        )
        return {}
    return data


def load_config(project_root: Optional[str] = None, home: Optional[str] = None) -> Dict[str, Any]:
    """Load config with cascade: defaults < user-level < project-level.

    Validates: all values are positive integers. Warns on invalid values and keeps prior value.
    """
    cfg = copy.deepcopy(DEFAULTS)
    home_path: Path = Path(home) if home else Path.home()
    paths: List[Path] = [home_path / ".claude" / CONFIG_NAME]
    if project_root:
        paths.append(Path(project_root) / ".claude" / CONFIG_NAME)
    for p in paths:
        if not p.is_file():
            continue
        data = _load(p)
        for key in data:
            if key not in DEFAULTS:
                print(f"warning: {p}: unknown key {key!r}; ignored", file=sys.stderr)
        for key in DEFAULTS:
            if key in data:
                v = data[key]
                if type(v) is int and v > 0:  # noqa: E721
                    cfg[key] = v
                else:
                    print(
                        f"warning: {p}: {key} must be positive int, "
                        f"got {type(v).__name__}={v!r}; using default",
                        file=sys.stderr,
                    )
    return cfg


def count_plan_steps(text: str) -> Optional[int]:
    """Count ### Step N headings in plan text."""
    count = 0
    for line in text.split("\n"):
        if re.match(r"^###\s+Step\s+\d+\b", line):
            count += 1
    return count if count > 0 else None


def extract_files_from_step(step_text: str) -> set:  # type: ignore[type-arg]
    """Extract file paths from a step's **Files**: line."""
    files: set = set()  # type: ignore[assignment]
    for line in step_text.split("\n"):
        if line.strip().startswith("**Files**:"):
            # Extract everything after the colon
            content = line.split("**Files**:", 1)[1].strip()
            # Split on commas and parse paths
            for part in content.split(","):
                path = part.strip()
                if path and path not in ("", "none", "N/A", "n/a"):
                    files.add(path)
            break
    return files


def count_distinct_files(text: str) -> Optional[int]:
    """Count distinct file paths mentioned in **Files**: lines across all steps."""
    all_files: set = set()  # type: ignore[assignment]
    # Split into steps by ### Step N headings
    steps = re.split(r"^###\s+Step\s+\d+\b", text, flags=re.M)[1:]
    for step_text in steps:
        files = extract_files_from_step(step_text)
        all_files.update(files)
    return len(all_files) if len(all_files) > 0 else None


def count_action_plan_findings(text: str) -> Tuple[Optional[int], Optional[int]]:
    """Count directives in action-plan: D<n> from Doing it table plus decided/measured items.

    Returns: (total_count, count_or_none)
    """
    directive_count = 0

    # Count Doing it table rows (D1, D2, ...)
    in_doing_it = False
    table_started = False
    for line in text.split("\n"):
        # Match "## Doing it:" heading
        if re.match(r"^##\s+Doing it:", line):
            in_doing_it = True
            table_started = False
            continue

        # Stop at next ## heading
        if in_doing_it and re.match(r"^##\s+", line):
            break

        if in_doing_it:
            stripped = line.strip()
            # Check for table start (first line starting with |)
            if stripped.startswith("|") and not table_started:
                table_started = True
                continue

            # In table: count data rows, skip header and separator
            if table_started and stripped.startswith("|"):
                # Skip separator rows: all dashes/colons/pipes
                if not re.match(r"^\|\s*[-:\s|]+\|\s*$", stripped):
                    directive_count += 1
            elif table_started and not stripped.startswith("|"):
                # Table ended
                table_started = False

    # Count items with - **STATUS**: decided or - **STATUS**: measured
    for match in re.finditer(r"- \*\*STATUS\*\*:\s*(decided|measured)", text):
        directive_count += 1

    return (directive_count, directive_count if directive_count > 0 else None)


def filter_only_units(
    text: str,
    kind: Literal["plan", "action-plan"],
    only_units: Optional[str],
) -> Optional[str]:
    """Filter to only specified units. Returns filtered text or None if no units match."""
    if not only_units:
        return text

    # Parse comma-separated unit ids (Step N or D<n>)
    requested_ids = [u.strip() for u in only_units.split(",")]
    filtered_lines: List[str] = []
    in_target_unit = False

    for line in text.split("\n"):
        # Check for step or directive
        step_match = re.match(r"^###\s+Step\s+(\d+)\b", line)
        if step_match:
            unit_id = f"Step {step_match.group(1)}"
            in_target_unit = unit_id in requested_ids
            if in_target_unit:
                filtered_lines.append(line)
            continue

        # Check for directive markers (D1, D2, etc.) if action-plan
        if kind == "action-plan":
            directive_match = re.match(r"^[*\-]\s+\*\*D(\d+)\*\*", line)
            if directive_match:
                unit_id = f"D{directive_match.group(1)}"
                in_target_unit = unit_id in requested_ids
                if in_target_unit:
                    filtered_lines.append(line)
                continue

        if in_target_unit:
            # Stop at next top-level heading
            if re.match(r"^##\s+", line):
                in_target_unit = False
            else:
                filtered_lines.append(line)

    result = "\n".join(filtered_lines)
    # Check if we got any matching content
    if not result.strip():
        return None
    return result


def main(argv: list) -> None:  # type: ignore[type-arg]
    """Main entry point. Parse args, count units, print JSON result."""
    parser = argparse.ArgumentParser(
        description="Estimate parallelizable work units from plan or action-plan",
        prog="unit-sizing.py",
    )
    parser.add_argument("plan_file", help="Path to plan or action-plan file")
    parser.add_argument(
        "--kind",
        required=True,
        choices=["plan", "action-plan"],
        help="File type: 'plan' or 'action-plan'",
    )
    parser.add_argument(
        "--only-units",
        default=None,
        help="Comma-separated unit ids to count (e.g., 'Step 1,Step 2,D1')",
    )
    parser.add_argument(
        "--project-root",
        default=None,
        help="Project root for config cascade",
    )

    try:
        args = parser.parse_args(argv[1:])
    except SystemExit:
        # argparse calls sys.exit on error; catch and print fallback
        print(
            json.dumps({
                "schema_version": 1,
                "kind": None,
                "steps": None,
                "files": None,
                "findings": None,
                "ceilings": {},
                "min_units": "unknown",
                "status": "unknown",
                "reason": "invalid arguments",
            })
        )
        return

    kind = args.kind  # type: ignore[attr-defined]
    project_root = args.project_root  # type: ignore[attr-defined]
    only_units = args.only_units  # type: ignore[attr-defined]

    try:
        plan_text = Path(args.plan_file).read_text(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception as e:
        print(
            json.dumps({
                "schema_version": 1,
                "kind": kind,
                "steps": None,
                "files": None,
                "findings": None,
                "ceilings": {},
                "min_units": "unknown",
                "status": "unknown",
                "reason": f"failed to read file: {e}",
            })
        )
        return

    # Filter to only-units if specified
    if only_units:
        filtered = filter_only_units(plan_text, kind, only_units)
        if filtered is None:
            print(
                json.dumps({
                    "schema_version": 1,
                    "kind": kind,
                    "steps": None,
                    "files": None,
                    "findings": None,
                    "ceilings": {},
                    "min_units": "unknown",
                    "status": "unknown",
                    "reason": f"no matching units found for: {only_units}",
                })
            )
            return
        plan_text = filtered

    cfg = load_config(project_root)
    ceilings_obj = {
        "steps_per_unit": cfg["steps_per_unit"],
        "files_per_unit": cfg["files_per_unit"],
        "findings_per_unit": cfg["findings_per_unit"],
    }

    # Count based on kind
    steps_count: Optional[int] = None
    files_count: Optional[int] = None
    findings_count: Optional[int] = None

    if kind == "plan":
        steps_count = count_plan_steps(plan_text)
        files_count = count_distinct_files(plan_text)
        # For plans, findings are not counted (N/A)
    elif kind == "action-plan":
        # For action-plan, steps are not relevant
        findings_count, _ = count_action_plan_findings(plan_text)

    # Calculate min_units
    units_by_dimension: List[int] = []
    if steps_count is not None:
        units_needed = math.ceil(steps_count / cfg["steps_per_unit"])
        units_by_dimension.append(units_needed)
    if files_count is not None:
        units_needed = math.ceil(files_count / cfg["files_per_unit"])
        units_by_dimension.append(units_needed)
    if findings_count is not None:
        units_needed = math.ceil(findings_count / cfg["findings_per_unit"])
        units_by_dimension.append(units_needed)

    if units_by_dimension:
        min_units = max(units_by_dimension)
        status = "ok"
        reason = ""
    else:
        # No recognizable structure
        if kind == "plan" and steps_count is None:
            reason = "no '### Step N' headings found in plan"
        elif kind == "action-plan" and findings_count is None:
            reason = "no directives found in action-plan"
        else:
            reason = "no counts could be determined"
        min_units = "unknown"  # type: ignore[assignment]
        status = "unknown"

    result = {
        "schema_version": 1,
        "kind": kind,
        "steps": steps_count,
        "files": files_count,
        "findings": findings_count,
        "ceilings": ceilings_obj,
        "min_units": min_units,
        "status": status,
        "reason": reason,
    }
    print(json.dumps(result))


if __name__ == "__main__":
    main(sys.argv)
