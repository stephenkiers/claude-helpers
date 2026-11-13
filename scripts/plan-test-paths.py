#!/usr/bin/env python3
"""plan-test-paths.py: enforce test file separation in plans and unit records.

Usage:
    plan-test-paths.py plan --plan <path>
    plan-test-paths.py units --units <path>

The script enforces that test files are a Round 2 (spec-blind) deliverable and never
listed in plan steps or unit owned_files. It uses the anti-cheat-scan.py predicate
(is_test_path) and the unit-sizing.py Files extractor for consistency.

Modes:
  plan   Parse a synthesized plan.md: scan the Implementation Steps section for test
         files in step Files lines. This script only reports. The one bounded
         synthesis repair attempt is performed by the calling command (it owns the
         retry), not here; this script never rewrites the plan.
  units  Parse units.json: check each unit's owned_files list for test files.

Exit codes:
  0: no test files found (clean)
  1: test files found (enforcement failure)
  2: error (bad arguments, missing files, parse errors, malformed plan or units,
     a plan with no Implementation Steps heading or zero parsed steps)
"""

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, List, Tuple

# The Implementation Steps heading must be a whole line of its own. Anchoring it stops
# a heading like "## Implementation Steps (draft)" or a "### " subheading from matching.
_IMPL_HEADING_RE = re.compile(r"^## Implementation Steps[ \t]*$", re.M)
# The section ends at the next level-2 heading ("## X", but not "### X").
_NEXT_H2_RE = re.compile(r"^## (?!#)", re.M)


def _load_module(module_name: str, file_path: Path) -> ModuleType:
    """Load a Python module from an absolute path using importlib.util.

    Raises RuntimeError (chained to the original exception) if the module cannot be
    loaded, so the exception text reaches the caller's exit-2 message.
    """
    try:
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"no import spec for {file_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:
        raise RuntimeError(f"failed to load {file_path}: {e}") from e
    return module


def _get_is_test_path() -> Callable[..., Any]:
    """Load and return the is_test_path function from anti-cheat-scan.py.

    Resolves the path relative to this script's real path to work through symlinks.
    """
    script_dir = Path(__file__).resolve().parent
    scan_path = script_dir / "anti-cheat-scan.py"
    if not scan_path.exists():
        raise RuntimeError(f"{scan_path} not found")
    module = _load_module("anti_cheat_scan", scan_path)
    if not hasattr(module, "is_test_path"):
        raise RuntimeError(f"{scan_path} has no is_test_path function")
    func: Callable[..., Any] = module.is_test_path
    return func


def _get_step_file_paths() -> Callable[..., Any]:
    """Load and return the step_file_paths function from unit-sizing.py.

    Resolves the path relative to this script's real path to work through symlinks.
    """
    script_dir = Path(__file__).resolve().parent
    sizing_path = script_dir / "unit-sizing.py"
    if not sizing_path.exists():
        raise RuntimeError(f"{sizing_path} not found")
    module = _load_module("unit_sizing", sizing_path)
    if not hasattr(module, "step_file_paths"):
        raise RuntimeError(f"{sizing_path} has no step_file_paths function")
    func: Callable[..., Any] = module.step_file_paths
    return func


def check_plan(
    plan_path: Path,
    is_test_path_func: Callable[..., Any],
    step_file_paths_func: Callable[..., Any],
) -> Tuple[List[str], int]:
    """Check plan.md for test files in Implementation Steps.

    Scans only the Implementation Steps section, extracting Files from each step
    and checking against is_test_path. Returns (error messages, number of steps scanned).

    Raises RuntimeError when the file cannot be read, when there is no
    Implementation Steps heading, or when the section parses to zero steps. A plan that
    yields no steps must fail closed rather than pass as clean.
    """
    try:
        text = plan_path.read_text(encoding="utf-8")
    except Exception as e:
        raise RuntimeError(f"failed to read {plan_path}: {e}") from e

    heading = _IMPL_HEADING_RE.search(text)
    if heading is None:
        raise RuntimeError(
            f"{plan_path} has no '## Implementation Steps' heading; refusing to report it clean"
        )

    next_heading = _NEXT_H2_RE.search(text, heading.end())
    impl_end = next_heading.start() if next_heading else len(text)
    impl_section = text[heading.start():impl_end]

    # Split the section into steps by ### Step N headings
    steps = re.split(r"^###\s+Step\s+(\d+)\b", impl_section, flags=re.M)

    # steps[0] is text before first heading, then [1] is step number, [2] is step content, etc.
    step_count = (len(steps) - 1) // 2
    if step_count == 0:
        raise RuntimeError(
            f"{plan_path}: scanned 0 steps in Implementation Steps; expected '### Step N' headings"
        )

    errors: List[str] = []

    for i in range(1, len(steps), 2):
        step_num = steps[i]
        step_content = steps[i + 1] if i + 1 < len(steps) else ""

        # The split leaves the heading remainder at the start of step_content, e.g.
        # ": Setup\n- **Files**: ...". Take the text after the colon on that first line.
        title_match = re.match(r":[ \t]*([^\n]*)", step_content)
        step_title = title_match.group(1).strip() if title_match else ""
        if not step_title:
            step_title = f"Step {step_num}"

        # Get file paths for this step
        file_paths = step_file_paths_func(step_content)

        # Check each file path
        for file_path in file_paths:
            if is_test_path_func(file_path):
                errors.append(
                    f"plan-test-paths: Step {step_num} ({step_title}) lists test file {file_path}; "
                    f"tests are a Round 2 deliverable, move this to \"Tests (written spec-blind in Round 2)\""
                )

    return errors, step_count


def check_units(units_path: Path, is_test_path_func: Callable[..., Any]) -> List[str]:
    """Check units.json for test files in owned_files.

    Returns a list of error messages. Raises RuntimeError for a malformed record: a
    non-object unit, or a unit without an owned_files list. A malformed unit must not
    be skipped, because skipping would let its test files through unchecked.
    """
    if not units_path.exists():
        raise RuntimeError(f"{units_path} not found")

    try:
        text = units_path.read_text(encoding="utf-8")
    except Exception as e:
        raise RuntimeError(f"failed to read {units_path}: {e}") from e

    try:
        data: object = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"invalid JSON in {units_path}: {e}") from e

    if not isinstance(data, dict):
        raise RuntimeError(f"{units_path}: top level must be a JSON object with a 'units' list")

    if "units" not in data:
        raise RuntimeError(f"{units_path} missing 'units' key")

    units = data["units"]
    if not isinstance(units, list):
        raise RuntimeError(f"{units_path}: 'units' is not a list")

    errors: List[str] = []

    for index, unit in enumerate(units):
        label = f"index {index}"
        if not isinstance(unit, dict):
            raise RuntimeError(f"{units_path}: unit at {label} is not an object")
        unit_id = unit.get("id", label)
        owned_files = unit.get("owned_files")
        if not isinstance(owned_files, list):
            raise RuntimeError(f"{units_path}: unit {unit_id} has no owned_files list")

        for file_path in owned_files:
            if isinstance(file_path, str) and is_test_path_func(file_path):
                errors.append(
                    f"plan-test-paths: unit {unit_id} owns test file {file_path}; "
                    f"tests are a Round 2 deliverable"
                )

    return errors


def main(argv: List[str]) -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Enforce test file separation in plans and unit records",
        prog="plan-test-paths.py",
    )
    subparsers = parser.add_subparsers(dest="mode", help="Mode: 'plan' or 'units'")

    plan_parser = subparsers.add_parser("plan", help="Check a plan.md file")
    plan_parser.add_argument(
        "--plan", required=True, type=str, help="Path to plan.md file"
    )

    units_parser = subparsers.add_parser("units", help="Check a units.json file")
    units_parser.add_argument(
        "--units", required=True, type=str, help="Path to units.json file"
    )

    args = parser.parse_args(argv[1:])

    if args.mode is None:
        parser.print_help()
        return 2

    try:
        # Load the shared predicates; a load failure raises and exits 2 below.
        is_test_path_func = _get_is_test_path()
        step_file_paths_func = _get_step_file_paths()

        if args.mode == "plan":
            plan_path = Path(args.plan)
            errors, scanned = check_plan(plan_path, is_test_path_func, step_file_paths_func)
            print(f"plan-test-paths: scanned {scanned} step(s) in Implementation Steps")
        elif args.mode == "units":
            units_path = Path(args.units)
            errors = check_units(units_path, is_test_path_func)
        else:
            print(f"error: unknown mode {args.mode}", file=sys.stderr)
            return 2

        if errors:
            for error in errors:
                print(error, file=sys.stderr)
            return 1

        return 0

    except RuntimeError as e:
        print(f"plan-test-paths: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # pylint: disable=broad-except
        print(f"plan-test-paths: internal error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
