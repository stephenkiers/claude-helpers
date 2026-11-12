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
         files in step Files lines. One bounded synthesis repair is allowed before
         failing the step.
  units  Parse units.json: check each unit's owned_files list for test files.

Exit codes:
  0: no test files found (clean)
  1: test files found (enforcement failure)
  2: error (bad arguments, missing files, parse errors)
"""

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import List, Optional


def _load_module(module_name: str, file_path: Path):  # type: ignore[no-untyped-def]
    """Load a Python module from an absolute path using importlib.util.

    Returns the loaded module or None on failure.
    """
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def _get_is_test_path() -> Optional[callable]:  # type: ignore[type-arg]
    """Load and return the is_test_path function from anti-cheat-scan.py.

    Resolves the path relative to this script's real path to work through symlinks.
    """
    script_dir = Path(__file__).resolve().parent
    scan_path = script_dir / "anti-cheat-scan.py"
    if not scan_path.exists():
        print(f"error: {scan_path} not found", file=sys.stderr)
        return None
    module = _load_module("anti_cheat_scan", scan_path)
    if module is None:
        print(f"error: failed to load {scan_path}", file=sys.stderr)
        return None
    if not hasattr(module, "is_test_path"):
        print(f"error: {scan_path} has no is_test_path function", file=sys.stderr)
        return None
    return module.is_test_path  # type: ignore[return-value]


def _get_step_file_paths() -> Optional[callable]:  # type: ignore[type-arg]
    """Load and return the step_file_paths function from unit-sizing.py.

    Resolves the path relative to this script's real path to work through symlinks.
    """
    script_dir = Path(__file__).resolve().parent
    sizing_path = script_dir / "unit-sizing.py"
    if not sizing_path.exists():
        print(f"error: {sizing_path} not found", file=sys.stderr)
        return None
    module = _load_module("unit_sizing", sizing_path)
    if module is None:
        print(f"error: failed to load {sizing_path}", file=sys.stderr)
        return None
    if not hasattr(module, "step_file_paths"):
        print(f"error: {sizing_path} has no step_file_paths function", file=sys.stderr)
        return None
    return module.step_file_paths  # type: ignore[return-value]


def check_plan(plan_path: Path, is_test_path_func: callable, step_file_paths_func: callable) -> List[str]:  # type: ignore[type-arg]
    """Check plan.md for test files in Implementation Steps.

    Scans only the Implementation Steps section, extracting Files from each step
    and checking against is_test_path. Returns a list of error messages.
    """
    try:
        text = plan_path.read_text(encoding="utf-8")
    except Exception as e:
        raise RuntimeError(f"failed to read {plan_path}: {e}")

    # Find Implementation Steps section (up to the next ## heading)
    impl_start = text.find("## Implementation Steps")
    if impl_start == -1:
        # No Implementation Steps section found; this is clean
        return []

    # Find the end of the section (next ## heading or end of text)
    impl_section_start = impl_start
    next_heading = re.search(r"\n##\s+", text[impl_start + len("## Implementation Steps"):])
    if next_heading:
        impl_end = impl_start + len("## Implementation Steps") + next_heading.start()
    else:
        impl_end = len(text)

    impl_section = text[impl_start:impl_end]

    # Split the section into steps by ### Step N headings
    steps = re.split(r"^###\s+Step\s+(\d+)\b", impl_section, flags=re.M)

    errors: List[str] = []

    # steps[0] is text before first heading, then [1] is step number, [2] is step content, etc.
    for i in range(1, len(steps), 2):
        step_num = steps[i]
        step_content = steps[i + 1] if i + 1 < len(steps) else ""

        # Extract the step title from the first line
        step_title_match = re.match(r"[^:\n]+:\s*([^\n]*)", step_content)
        step_title = step_title_match.group(1).strip() if step_title_match else f"Step {step_num}"

        # Get file paths for this step
        file_paths = step_file_paths_func(step_content)

        # Check each file path
        for file_path in file_paths:
            if is_test_path_func(file_path):
                errors.append(
                    f"plan-test-paths: Step {step_num} ({step_title}) lists test file {file_path}; "
                    f"tests are a Round 2 deliverable, move this to \"Tests (written spec-blind in Round 2)\""
                )

    return errors


def check_units(units_path: Path, is_test_path_func: callable) -> List[str]:  # type: ignore[type-arg]
    """Check units.json for test files in owned_files.

    Returns a list of error messages.
    """
    if not units_path.exists():
        raise RuntimeError(f"{units_path} not found")

    try:
        text = units_path.read_text(encoding="utf-8")
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"invalid JSON in {units_path}: {e}")
    except Exception as e:
        raise RuntimeError(f"failed to read {units_path}: {e}")

    if "units" not in data:
        raise RuntimeError(f"units.json missing 'units' key")

    if not isinstance(data["units"], list):
        raise RuntimeError(f"units.json 'units' is not a list")

    errors: List[str] = []

    for unit in data["units"]:
        if not isinstance(unit, dict):
            continue
        if "id" not in unit or "owned_files" not in unit:
            continue

        unit_id = unit["id"]
        owned_files = unit["owned_files"]
        if not isinstance(owned_files, list):
            continue

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

    # Load the shared predicates
    is_test_path_func = _get_is_test_path()
    if is_test_path_func is None:
        return 2

    step_file_paths_func = _get_step_file_paths()
    if step_file_paths_func is None:
        return 2

    try:
        if args.mode == "plan":
            plan_path = Path(args.plan)
            errors = check_plan(plan_path, is_test_path_func, step_file_paths_func)
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
