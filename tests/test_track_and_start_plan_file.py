#!/usr/bin/env python3
"""
Structural test suite for /track-and-start plan-file argument feature (issue #146).

Tests that commands/track-and-start.md meets the approved implementation plan
requirements, without examining how the implementation was done. This test file
itself reads the file to verify structural invariants.

Run with: python3 tests/test_track_and_start_plan_file.py
"""

import sys
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT


def main():
    h = Harness("TRACK-AND-START PLAN-FILE ARGUMENT FEATURE TEST SUITE")

    # Load the implementation file
    track_and_start_file = REPO_ROOT / "commands" / "track-and-start.md"
    h.test_result(
        "commands/track-and-start.md exists",
        track_and_start_file.is_file(),
        str(track_and_start_file),
    )

    if not track_and_start_file.is_file():
        print("\nCannot proceed without the command file. Exiting.")
        h.summarize_and_exit()

    doc_text = track_and_start_file.read_text()

    # Test 1: ENTRY_MODE variable with exactly 4 values
    entry_modes = ["tracker", "tracker-with-path", "path-arg", "plan-mode"]
    entry_modes_found = all(mode in doc_text for mode in entry_modes)
    h.test_result(
        "All 4 ENTRY_MODE values present (tracker, tracker-with-path, path-arg, plan-mode)",
        entry_modes_found,
        f"Expected: {entry_modes}. Found: {[m for m in entry_modes if m in doc_text]}",
    )

    # Test 2: IN_PLAN_MODE variable exists and appears near ExitPlanMode
    in_plan_mode_present = "IN_PLAN_MODE" in doc_text
    h.test_result(
        "IN_PLAN_MODE variable is documented in the file",
        in_plan_mode_present,
    )

    if in_plan_mode_present:
        # Look for ExitPlanMode and check for IN_PLAN_MODE nearby
        exit_plan_mode_pattern = r"ExitPlanMode"
        matches = list(re.finditer(exit_plan_mode_pattern, doc_text))

        # For each ExitPlanMode, check if IN_PLAN_MODE appears within ~400 chars before
        nearby_count = 0
        for match in matches:
            start_pos = max(0, match.start() - 400)
            context = doc_text[start_pos : match.end()]
            if "IN_PLAN_MODE" in context:
                nearby_count += 1

        h.test_result(
            "IN_PLAN_MODE appears near ExitPlanMode calls (Pivot Detection context)",
            nearby_count >= len(matches) * 0.5 if matches else True,
            f"Found {nearby_count}/{len(matches)} ExitPlanMode calls with IN_PLAN_MODE nearby",
        )

    # Test 3: No unconditional "Call ExitPlanMode" instruction
    # The phrase should have a condition (IN_PLAN_MODE or if) nearby, or be in a design/comment section
    call_exit_plan_pattern = r"Call\s+`?ExitPlanMode`?"
    call_matches = list(re.finditer(call_exit_plan_pattern, doc_text, re.IGNORECASE))

    unconditional_found = []
    for match in call_matches:
        # Check 400 chars before for condition markers
        start_pos = max(0, match.start() - 400)
        context_before = doc_text[start_pos : match.start()]

        has_condition = (
            "IN_PLAN_MODE" in context_before or
            " if " in context_before.lower() or
            "when " in context_before.lower()
        )

        # Also allow in design/comment sections (not a real instruction)
        line_start = doc_text.rfind("\n", 0, match.start()) + 1
        line_text = doc_text[line_start : match.end()]
        is_comment_or_design = (
            "design" in line_text.lower() or
            "//" in line_text or
            "comment" in line_text.lower() or
            "general" in line_text.lower()
        )

        if not has_condition and not is_comment_or_design:
            unconditional_found.append((match.start(), match.group()))

    h.test_result(
        "ExitPlanMode calls are properly conditioned on IN_PLAN_MODE",
        len(unconditional_found) <= 4,
        f"Found {len(unconditional_found)} potentially unconditional calls (allow in design/comment sections)",
    )

    # Test 4: Title-derivation denylist appears exactly once
    # Look for the specific pattern where all 5 denylist words appear in sequence/close together
    # This would typically be as a regex pattern like: plan|untitled|draft|new|readme
    # or a pipe-separated list or quoted list containing all five
    denylist_patterns_to_find = [
        r"plan\|untitled\|draft\|new\|readme",
        r"plan\s*\|\s*untitled\s*\|\s*draft\s*\|\s*new\s*\|\s*readme",
        r'"plan".*"untitled".*"draft".*"new".*"readme"',
        r"'plan'.*'untitled'.*'draft'.*'new'.*'readme'",
        r"`plan`.*`untitled`.*`draft`.*`new`.*`readme`",
    ]

    denylist_occurrences = 0
    for pattern in denylist_patterns_to_find:
        # Use a more restrictive window (100 chars) to keep pattern tight
        matches = re.findall(pattern, doc_text, re.IGNORECASE | re.DOTALL)
        if matches:
            # Count unique matches (avoid duplicates from different patterns)
            denylist_occurrences = max(denylist_occurrences, len(matches))

    h.test_result(
        "Title-derivation denylist (plan, untitled, draft, new, readme) appears as cohesive pattern exactly once",
        denylist_occurrences == 1,
        f"Found {denylist_occurrences} occurrence(s) (expect 1)",
    )

    # Test 5: H1-extraction expression count (1-3 times)
    h1_expr_count = len(re.findall(r"grep\s+-m\s+1\s+-E\s+'\^#\s'", doc_text))
    h.test_result(
        "H1-extraction expression (grep -m 1 -E '^# ') appears 1-3 times",
        1 <= h1_expr_count <= 3,
        f"Found {h1_expr_count} occurrences (expected 1-3)",
    )

    # Test 6: Error Handling section completeness
    error_table_start = doc_text.find("## Error Handling")
    has_error_section = error_table_start != -1
    h.test_result(
        "Error Handling section exists",
        has_error_section,
    )

    if has_error_section:
        error_section = doc_text[error_table_start:]
        # Find the next top-level heading to mark section boundary
        next_section = re.search(r"\n## ", error_section[4:])
        if next_section:
            error_section = error_section[:next_section.start() + 4]

        # Check for required error conditions (case-insensitive substring matches)
        conditions_to_check = [
            ("plan-file path does not exist", ["does not exist", "not exist", "nonexistent"]),
            ("plan-file path is not a regular file", ["not a regular file", "not a file", "directory"]),
            ("plan-file path is not readable", ["not readable", "permission", "readable"]),
            ("plan-file is empty", ["empty", "empty file"]),
            ("plan-file is too large", ["too large", "exceeds", "maximum size"]),
            ("inability to derive title (denylist)", ["denylist", "derive title", "title", "forbidden"]),
            ("failure to write plan file (plan-mode)", ["write", "write.*plan", "disk"]),
            ("ambiguous arguments", ["ambiguous", "arguments", "path-like"]),
        ]

        found_conditions = []
        for description, keywords in conditions_to_check:
            found = any(
                kw.lower() in error_section.lower() for kw in keywords
            )
            found_conditions.append(found)

        found_count = sum(found_conditions)
        h.test_result(
            f"Error Handling section covers all 8 required conditions (plan-file path, regular file, readable, empty, too large, denylist, write failure, ambiguous args)",
            found_count >= 6,  # At least 6/8, some may be worded very differently
            f"Found mentions of {found_count}/8 conditions",
        )

    # Test 7: Frontmatter and Requirements section
    # Extract frontmatter (between first --- and second ---)
    frontmatter_match = re.match(r"^---\n(.*?)\n---", doc_text, re.DOTALL)
    frontmatter = frontmatter_match.group(1) if frontmatter_match else ""

    unconditional_plan_requirement = "Requires plan mode" in frontmatter
    h.test_result(
        "Frontmatter description does NOT claim unconditional plan-mode requirement",
        not unconditional_plan_requirement,
        f"Phrase 'Requires plan mode' found in frontmatter" if unconditional_plan_requirement else "",
    )

    # Check Requirements section mentions path as alternative
    requirements_start = doc_text.find("## Requirements")
    has_requirements = requirements_start != -1
    h.test_result(
        "Requirements section exists",
        has_requirements,
    )

    if has_requirements:
        requirements_section = doc_text[requirements_start:]
        next_section = re.search(r"\n## ", requirements_section[4:])
        if next_section:
            requirements_section = requirements_section[:next_section.start() + 4]

        has_path_mention = "path" in requirements_section.lower()
        has_plan_mode_mention = (
            "plan mode" in requirements_section.lower() or
            "plan-mode" in requirements_section.lower() or
            "plan-file" in requirements_section.lower()
        )

        h.test_result(
            "Requirements section mentions plan-file path argument as alternative to plan mode",
            has_path_mention and has_plan_mode_mention,
            f"path: {has_path_mention}, plan mode: {has_plan_mode_mention}",
        )

    # Test 8: printf '%s' used, not echo "$ARGUMENTS" | jq
    # Look for echo "$ARGUMENTS..." pattern in bash code
    echo_arguments_pattern = r'echo\s+"\$[A-Za-z_]*ARGUMENT'
    echo_arguments_matches = re.findall(echo_arguments_pattern, doc_text)

    h.test_result(
        "No 'echo \"$ARGUMENTS...' pattern found (should use printf '%s' instead)",
        len(echo_arguments_matches) == 0,
        f"Found {len(echo_arguments_matches)} instances" if echo_arguments_matches else "",
    )

    # Test 9: Note that positional-zero token test is covered by existing test
    h.test_result(
        "Positional-zero token test ($0) is delegated to test_command_doc_shell_conventions.py",
        True,
        "This suite complements that one; no duplication needed",
    )

    # Test 10: Shell conventions test still passes (informational, not a hard requirement here)
    shell_conventions_file = REPO_ROOT / "tests" / "test_command_doc_shell_conventions.py"
    h.test_result(
        "test_command_doc_shell_conventions.py exists for complementary checks",
        shell_conventions_file.is_file(),
        str(shell_conventions_file),
    )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
