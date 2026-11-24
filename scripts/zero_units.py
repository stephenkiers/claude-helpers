"""zero_units.py: the single source for the zero-unit run shape (#526).

A zero-unit run is a plan whose only deliverable is new tests: no Round 1 units, UNITS: 0, and
SPLIT_BASIS: zero-units. The basis token, the plan headings, the Targets-line grammar and the Targets
path rules live here; plan-test-paths.py (plan time) and flow-verify-receipt.py (receipt time)
both import this module, so the two checks cannot drift. Leaf module: standard library only.
"""

import re
import subprocess
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, List, Optional

ZERO_UNITS_BASIS = "zero-units"

# The Implementation Steps heading must be a whole line of its own; the section ends at the next
# level-2 heading ("## X", but not "### X").
IMPL_HEADING_RE = re.compile(r"^## Implementation Steps[ \t]*$", re.M)
NEXT_H2_RE = re.compile(r"^## (?!#)", re.M)
STEP_SPLIT_RE = re.compile(r"^###\s+Step\s+(\d+)\b", re.M)
# The Tests subsection heading, matched as a whole line; it ends at the next heading of level 1 to 3.
TESTS_HEADING = "### Tests (written spec-blind in Round 2)"
TESTS_HEADING_RE = re.compile(rf"^{re.escape(TESTS_HEADING)}[ \t]*$", re.M)
NEXT_H1_TO_H3_RE = re.compile(r"^#{1,3}[ \t]", re.M)
# A Targets line: "Targets: `path`, `path`", optionally as a list item.
TARGETS_LINE_RE = re.compile(r"^[ \t]*(?:[-*][ \t]+)?Targets:[ \t]*(.*)$", re.M)
BACKTICK_RE = re.compile(r"`([^`\n]+)`")


def parse_tests_section(text: str) -> Optional[str]:
    """Return the body of the Tests subsection, or None when the plan has no such heading."""
    heading = TESTS_HEADING_RE.search(text)
    if heading is None:
        return None
    next_heading = NEXT_H1_TO_H3_RE.search(text, heading.end())
    end = next_heading.start() if next_heading else len(text)
    return text[heading.end():end]


def parse_targets(section: str) -> Optional[List[str]]:
    """Return the backticked paths on the Targets line(s) of a Tests section body.

    None means no Targets line at all; an empty list means a Targets line with no backticked path.
    """
    lines = TARGETS_LINE_RE.findall(section)
    if not lines:
        return None
    paths: List[str] = []
    for line in lines:
        paths.extend(match.strip() for match in BACKTICK_RE.findall(line))
    return paths


def has_nonblank_tests_content(section: str) -> bool:
    """True when the Tests section has non-blank content other than Targets lines."""
    stripped = TARGETS_LINE_RE.sub("", section)
    return any(line.strip() for line in stripped.splitlines())


def repo_root() -> Path:
    """The git top level of the current directory, falling back to the current directory.

    Targets are repo-relative, so they resolve against this root, never against whatever
    subdirectory the check happens to run from.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return Path.cwd()
    top = result.stdout.strip()
    return Path(top) if result.returncode == 0 and top else Path.cwd()


def target_problem(target: str, root: Path) -> Optional[str]:
    """Why a Targets path is not acceptable, or None. Messages start with 'Targets path ...'."""
    if PurePosixPath(target).is_absolute() or PureWindowsPath(target).is_absolute() or target.startswith("~"):
        return f"Targets path is absolute: {target}"
    if ".." in PurePosixPath(target.replace("\\", "/")).parts:
        return f"Targets path contains '..': {target}"
    resolved = root / target
    if not resolved.exists():
        return f"Targets path missing: {target}"
    if not resolved.is_file():
        return f"Targets path is not a file: {target}"
    return None


def zero_unit_plan_problem(
    text: str,
    is_test_path: Callable[[str], bool],
    step_file_paths: Callable[[str], List[str]],
    root: Path,
) -> Optional[str]:
    """Why a plan is not a complete zero-unit plan, or None. Reasons start 'zero-unit-plan-incomplete: '."""
    prefix = "zero-unit-plan-incomplete: "
    impl = IMPL_HEADING_RE.search(text)
    if impl is None:
        return prefix + "no Implementation Steps heading"
    next_h2 = NEXT_H2_RE.search(text, impl.end())
    impl_section = text[impl.start(): next_h2.start() if next_h2 else len(text)]
    for step_content in STEP_SPLIT_RE.split(impl_section)[2::2]:
        for file_path in step_file_paths(step_content):
            if not is_test_path(file_path):
                return prefix + f"plan lists non-test file in Implementation Steps: {file_path}"

    section = parse_tests_section(text)
    if section is None or not has_nonblank_tests_content(section):
        return prefix + "no Round 2 tests"
    targets = parse_targets(section)
    if targets is None or not targets:
        return prefix + "no Targets"
    for target in targets:
        if is_test_path(target):
            return prefix + f"Targets path is a test path: {target}"
        problem = target_problem(target, root)
        if problem is not None:
            return prefix + problem
    return None
