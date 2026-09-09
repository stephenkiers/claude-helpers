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

    # Test 2: ExitPlanMode call in Pivot Detection is properly gated by IN_PLAN_MODE
    # Find the actual call site in Pivot Detection step 7 and verify it checks IN_PLAN_MODE
    pivot_section_start = doc_text.find("### Step 4d: Execute Pivot")
    if pivot_section_start == -1:
        h.test_result(
            "Pivot Detection section (Step 4d) exists",
            False,
            "Section not found",
        )
    else:
        # Extract the section until the next ### heading
        next_section_start = doc_text.find("\n### ", pivot_section_start + 1)
        pivot_section = doc_text[pivot_section_start:next_section_start] if next_section_start != -1 else doc_text[pivot_section_start:]

        # Look for the specific ExitPlanMode call site in this section
        # It should be preceded by "If IN_PLAN_MODE=1" or similar condition
        has_call = "ExitPlanMode" in pivot_section
        has_condition_check = "IN_PLAN_MODE=1" in pivot_section or "IN_PLAN_MODE = 1" in pivot_section
        has_conditional_structure = "if" in pivot_section.lower() and "IN_PLAN_MODE" in pivot_section

        is_properly_gated = has_call and has_condition_check and has_conditional_structure
        h.test_result(
            "ExitPlanMode call in Pivot Detection is gated by explicit IN_PLAN_MODE=1 check",
            is_properly_gated,
            f"call={has_call}, condition={has_condition_check}, structure={has_conditional_structure}",
        )

    # Test 3: Do NOT unconditionally call ExitPlanMode when IN_PLAN_MODE=0
    # Verify that the Pivot Detection section explicitly handles the IN_PLAN_MODE=0 case
    # without calling ExitPlanMode (printing handoff block instead)
    if pivot_section_start != -1:
        next_section_start = doc_text.find("\n### ", pivot_section_start + 1)
        pivot_section = doc_text[pivot_section_start:next_section_start] if next_section_start != -1 else doc_text[pivot_section_start:]

        # Check for the "If IN_PLAN_MODE=0" branch
        has_zero_case = "IN_PLAN_MODE=0" in pivot_section or "IN_PLAN_MODE = 0" in pivot_section
        # The zero case should print the handoff block, not call ExitPlanMode
        has_handoff_in_zero_context = False
        if has_zero_case:
            # Look for the pattern where IN_PLAN_MODE=0 leads to handoff printing
            zero_case_pattern = r"If\s+`IN_PLAN_MODE=0`.*?print(?:s)?\s+the\s+standard\s+handoff"
            has_handoff_in_zero_context = bool(re.search(zero_case_pattern, pivot_section, re.IGNORECASE | re.DOTALL))

        is_properly_negated = has_zero_case and has_handoff_in_zero_context
        h.test_result(
            "Pivot Detection explicitly handles IN_PLAN_MODE=0 (prints handoff, does NOT call ExitPlanMode)",
            is_properly_negated,
            f"zero_case={has_zero_case}, handoff_in_zero={has_handoff_in_zero_context}",
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
        # Use re.search for keyword checks to handle regex patterns properly
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
        missing_conditions = []
        for description, keywords in conditions_to_check:
            found = False
            for kw in keywords:
                try:
                    # Try as regex pattern first (for patterns like "write.*plan")
                    if re.search(kw, error_section, re.IGNORECASE):
                        found = True
                        break
                except re.error:
                    # Fall back to substring match if regex is invalid
                    if kw.lower() in error_section.lower():
                        found = True
                        break
            found_conditions.append(found)
            if not found:
                missing_conditions.append(description)

        found_count = sum(found_conditions)
        h.test_result(
            f"Error Handling section covers all 8 required conditions (with per-condition reporting)",
            found_count == 8,  # Must be exactly 8/8
            f"Found {found_count}/8 conditions. Missing: {', '.join(missing_conditions) if missing_conditions else 'none'}",
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

    # Test 9: Entry mode dispatch produces ENTRY_MODE and IN_PLAN_MODE variables
    # The "After you've made the judgment above, record it" section should echo both variables
    dispatch_output_section = doc_text.find("echo \"ENTRY_MODE=$ENTRY_MODE IN_PLAN_MODE=$IN_PLAN_MODE\"")
    has_entry_mode_output = dispatch_output_section != -1
    h.test_result(
        "Entry mode dispatch section explicitly echoes both ENTRY_MODE and IN_PLAN_MODE variables",
        has_entry_mode_output,
        "Expected echo statement not found in dispatch section",
    )

    # Test 10: Shell conventions test file exists and would validate this command
    shell_conventions_file = REPO_ROOT / "tests" / "test_command_doc_shell_conventions.py"
    file_exists = shell_conventions_file.is_file()
    h.test_result(
        "test_command_doc_shell_conventions.py exists to validate this command",
        file_exists,
        str(shell_conventions_file) if not file_exists else "",
    )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
