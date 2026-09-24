#!/usr/bin/env python3
"""
Test suite for orchestrator waiting wording per issue #216.

Ensures that old false premises about synchronous returns have been removed, that the
canonical pointer sentence is present where required, and that polling/waiting instructions
conform to the new harness-agnostic pattern.

Run with: python3 tests/test_orchestrator_wait_wording.py
"""

import sys
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT

# The exact pointer sentence that must appear identically in all four named files
POINTER_SENTENCE = (
    "**Waiting:** follow `~/.claude/prompts/join-barrier-pattern.md` § Waiting for the barrier "
    "— end your turn while any launched id is outstanding; never poll; at most one 1800s "
    "status-only `ScheduleWakeup` per phase."
)

# Forbidden wording patterns (these should NOT appear in the named files)
FORBIDDEN_PATTERNS = [
    r"returned by the time you continue",
    r"return by the time you continue",
    r"have all returned before",
    r"## Never poll\.",  # Case-sensitive heading (period required for heading match)
]

# The four named files that must contain the pointer sentence
NAMED_FILES = [
    "prompts/expert-review-panel.md",
    "commands/expert-review.md",
    "commands/expert-plan.md",
    "commands/implement-with-haiku.md",
]

# The barrier pattern file that must contain the canonical section
BARRIER_PATTERN_FILE = "prompts/join-barrier-pattern.md"


def main():
    h = Harness("ORCHESTRATOR WAITING WORDING TEST SUITE")

    # Check that all named files exist
    for file_path in NAMED_FILES:
        full_path = REPO_ROOT / file_path
        h.test_result(f"{file_path} exists", full_path.is_file(), str(full_path))

    # Check barrier pattern file exists
    barrier_file = REPO_ROOT / BARRIER_PATTERN_FILE
    h.test_result(f"{BARRIER_PATTERN_FILE} exists", barrier_file.is_file(), str(barrier_file))

    # Test each named file for forbidden wording and presence of pointer sentence
    for file_path in NAMED_FILES:
        full_path = REPO_ROOT / file_path
        if not full_path.is_file():
            continue

        content = full_path.read_text()

        # Check for forbidden patterns
        for pattern in FORBIDDEN_PATTERNS:
            matches = re.findall(pattern, content)
            h.test_result(
                f"{file_path} does not contain forbidden pattern: {pattern}",
                len(matches) == 0,
                f"found {len(matches)} match(es)" if matches else "",
            )

        # Check for presence of pointer sentence (with normalized whitespace)
        normalized_content = re.sub(r'\s+', ' ', content)
        normalized_pointer = re.sub(r'\s+', ' ', POINTER_SENTENCE)
        h.test_result(
            f"{file_path} contains the pointer sentence",
            normalized_pointer in normalized_content,
            "pointer sentence not found" if normalized_pointer not in normalized_content else "",
        )

        # Check that ScheduleWakeup mentions only appear inside the pointer sentence
        # Strip the pointer sentence from content before checking for remaining references
        content_without_pointer = content.replace(POINTER_SENTENCE, "")

        # Find all lines with ScheduleWakeup or sleep (case-insensitive for sleep) outside pointer
        lines_with_wake = []
        lines_with_sleep = []
        for lineno, line in enumerate(content_without_pointer.splitlines(), start=1):
            if "ScheduleWakeup" in line:
                lines_with_wake.append((lineno, line))
            if re.search(r"\bsleep\b", line, re.IGNORECASE):
                lines_with_sleep.append((lineno, line))

        # Flag any remaining ScheduleWakeup or sleep mentions outside the pointer
        for lineno, line in lines_with_wake + lines_with_sleep:
            h.test_result(
                f"{file_path}: ScheduleWakeup/sleep should only appear in pointer sentence",
                False,
                f"line: {line.strip()}",
            )

    # Test join-barrier-pattern.md for canonical section content
    if barrier_file.is_file():
        barrier_content = barrier_file.read_text()

        # Check for section heading
        h.test_result(
            f"{BARRIER_PATTERN_FILE} contains 'Waiting for the barrier' section",
            "Waiting for the barrier" in barrier_content,
        )

        # Check for key concepts in the section
        h.test_result(
            f"{BARRIER_PATTERN_FILE} mentions '1800' (for 1800s wakeup)",
            "1800" in barrier_content,
        )

        h.test_result(
            f"{BARRIER_PATTERN_FILE} mentions 'status report'",
            "status report" in barrier_content,
        )

        h.test_result(
            f"{BARRIER_PATTERN_FILE} mentions 'at most one'",
            "at most one" in barrier_content,
        )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
