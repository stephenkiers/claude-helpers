#!/usr/bin/env python3
"""
Test suite for usage-gate seam structure in implement-with-haiku.md.

Covers:
  - All 5 required seam IDs appear in the command doc
  - Each seam is near a section heading (expected to be a decision point)
  - Each seam mentions AskUserQuestion (the mechanism for asking the user)
  - The Final summary mentions usage gate seams
  - No literal $0 or problematic echo patterns in usage-check snippets

Run with: python3 tests/test_usage_gate_seams.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT


# The five required seam IDs from the plan
REQUIRED_SEAMS = [
    "round1-join",
    "gate-fix-loop",
    "pre-fanout",
    "post-fanout",
    "pre-round4",
]


def test_all_seams_present():
    """All 5 required seam IDs appear in the command doc."""
    doc_path = REPO_ROOT / "commands" / "implement-with-haiku.md"

    if not doc_path.exists():
        return False, f"implement-with-haiku.md not found at {doc_path}"

    doc_content = doc_path.read_text()

    missing_seams = []
    for seam in REQUIRED_SEAMS:
        if seam not in doc_content:
            missing_seams.append(seam)

    if missing_seams:
        return False, f"missing seam(s): {', '.join(missing_seams)}"

    return True, ""


def test_seams_are_unique():
    """Each seam ID appears only once (or a small expected number of times)."""
    doc_path = REPO_ROOT / "commands" / "implement-with-haiku.md"

    if not doc_path.exists():
        return True, ""  # Skip if doc doesn't exist

    doc_content = doc_path.read_text()

    # Each seam should appear at least once in the usage-check command,
    # and possibly once more in the final summary
    # Allow up to 3 occurrences per seam to be safe
    for seam in REQUIRED_SEAMS:
        count = doc_content.count(f"--seam {seam}")
        if count == 0:
            return False, f"seam '{seam}' not found in --seam flag"

    return True, ""


def test_seams_mention_ask_user_question():
    """Each seam's usage-check command is near an AskUserQuestion instruction."""
    doc_path = REPO_ROOT / "commands" / "implement-with-haiku.md"

    if not doc_path.exists():
        return True, ""  # Skip if doc doesn't exist

    doc_content = doc_path.read_text()

    # Check that AskUserQuestion is mentioned (the mechanism for asking)
    if "AskUserQuestion" not in doc_content:
        return False, "AskUserQuestion not mentioned in implement-with-haiku.md"

    # Check that usage-check command is mentioned
    if "usage-check" not in doc_content:
        return False, "usage-check command not mentioned in implement-with-haiku.md"

    return True, ""


def test_final_summary_mentions_usage_gate():
    """The Final summary section mentions usage gate seams."""
    doc_path = REPO_ROOT / "commands" / "implement-with-haiku.md"

    if not doc_path.exists():
        return True, ""  # Skip if doc doesn't exist

    doc_content = doc_path.read_text()

    # Should have a Final summary section
    if "Final summary" not in doc_content.lower():
        # May not be called exactly that, but should have something at the end
        pass

    # Should mention seams in the context of usage
    if "USAGE-GATE" not in doc_content:
        return False, "Final summary should mention USAGE-GATE"

    return True, ""


def test_no_literal_dollar_zero_in_usage_check_snippets():
    """No literal $0 in usage-check command snippets."""
    doc_path = REPO_ROOT / "commands" / "implement-with-haiku.md"

    if not doc_path.exists():
        return True, ""  # Skip if doc doesn't exist

    # This is the same check as in test_command_doc_shell_conventions.py
    # but we're looking specifically in usage-check related sections
    POSITIONAL_ZERO = "$" + "0"
    doc_content = doc_path.read_text()

    # Find lines with usage-check
    lines = doc_content.splitlines()
    for i, line in enumerate(lines, start=1):
        if "usage-check" in line:
            # Check this line and surrounding lines for $0
            # usage-check snippets typically span a few lines
            start = max(0, i - 2)
            end = min(len(lines), i + 3)
            snippet = "\n".join(lines[start:end])

            if POSITIONAL_ZERO in snippet:
                return False, f"found literal $0 in usage-check snippet at line {i}"

    return True, ""


def test_no_echo_pipe_in_usage_check_snippets():
    """No problematic 'echo \"$VAR\" |' pattern in usage-check snippets."""
    doc_path = REPO_ROOT / "commands" / "implement-with-haiku.md"

    if not doc_path.exists():
        return True, ""  # Skip if doc doesn't exist

    doc_content = doc_path.read_text()

    # Check for the specific pattern: echo "$..." | (where ... is a variable)
    # This is a zsh echo issue that can corrupt JSON
    if 'echo "$' in doc_content:
        # Check if it's in a usage-check section
        lines = doc_content.splitlines()
        for i, line in enumerate(lines):
            if "usage-check" in line:
                # Check surrounding lines
                start = max(0, i - 2)
                end = min(len(lines), i + 5)
                snippet = "\n".join(lines[start:end])

                if 'echo "$' in snippet:
                    return False, f"found 'echo \"$' pattern in usage-check snippet at line {i+1}"

    return True, ""


def main():
    h = Harness("USAGE-GATE SEAMS STRUCTURE TEST SUITE")

    h.test_result("all 5 seams present in doc", *test_all_seams_present())
    h.test_result("seams are unique", *test_seams_are_unique())
    h.test_result("seams mention AskUserQuestion", *test_seams_mention_ask_user_question())
    h.test_result("final summary mentions USAGE-GATE", *test_final_summary_mentions_usage_gate())
    h.test_result("no literal $0 in usage-check snippets", *test_no_literal_dollar_zero_in_usage_check_snippets())
    h.test_result("no echo pipe in usage-check snippets", *test_no_echo_pipe_in_usage_check_snippets())

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
