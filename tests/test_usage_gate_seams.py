#!/usr/bin/env python3
"""
Test suite for usage-gate seam placement in /implement-with-haiku (commands/implement-with-haiku.md).

Covers:
- All five seams appear in correct sections
- Each seam includes AskUserQuestion mention and stop-state copy
- Final summary section includes USAGE-GATE-SEAMS documentation
- No hardcoded threshold strings appear in AskUserQuestion option text
- No shell-convention violations in seam snippets

Run with: python3 tests/test_usage_gate_seams.py
"""

import sys
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT

# Seam definitions: (seam_id, section_heading_contains_pattern, stop_state_keywords)
SEAMS = [
    ("round1-join", "Step 4d", ["merged", "committed", "nothing is pending"]),
    ("gate-fix-loop", "Gate step 4", ["failures", "unresolved"]),
    ("pre-fanout", "Post-gate fan-out", ["Round 1 is committed", "no tests"]),
    ("post-fanout", "After the fan-out", ["Round 2", "tests are committed", "will be lost"]),
    ("pre-round4", "4.0 Applicability", ["last stop-or-continue", "no subagent"]),
]

POSITIONAL_ZERO = "$" + "0"
ECHO_PIPE_PATTERN = r'echo\s+"\$[A-Z_]+"\s+\|'


def read_file(path):
    """Read file and return text and lines."""
    text = path.read_text()
    lines = text.splitlines()
    return text, lines


def find_section_bounds(lines, section_pattern):
    """
    Find start and end line numbers of a section matching section_pattern.
    Returns (start_line_no, end_line_no) or None if not found.
    Section ends at the next heading of same or higher level (##).
    """
    start = None
    for i, line in enumerate(lines):
        if section_pattern.lower() in line.lower():
            start = i + 1  # Convert to 1-indexed line number
            break

    if start is None:
        return None

    # Find end: next line that starts with ## (excluding the current line's heading level)
    # Count the # characters in the starting line to determine its level
    heading_line = lines[start - 1]
    start_level = len(heading_line) - len(heading_line.lstrip("#"))

    end = len(lines)
    for i in range(start, len(lines)):
        line = lines[i]
        if line.startswith("#"):
            # Count the # characters
            line_level = len(line) - len(line.lstrip("#"))
            # Stop at same level or higher (lower number = higher in hierarchy)
            if line_level <= start_level:
                end = i + 1  # Convert to 1-indexed
                break

    return start, end


def test_seam_presence():
    """Test that all five seams appear in the correct sections."""
    h = Harness("USAGE GATE SEAMS TEST SUITE")

    cmd_file = REPO_ROOT / "commands" / "implement-with-haiku.md"
    if not cmd_file.exists():
        h.test_result("commands/implement-with-haiku.md exists", False, str(cmd_file))
        h.summarize_and_exit()

    text, lines = read_file(cmd_file)
    h.test_result("file reads successfully", True)

    # Test each seam
    seam_locations = {}
    for seam_id, section_pattern, stop_keywords in SEAMS:
        section_bounds = find_section_bounds(lines, section_pattern)
        if not section_bounds:
            h.test_result(
                f"seam {seam_id} — section '{section_pattern}' found",
                False,
                f"section heading not found",
            )
            continue

        start, end = section_bounds
        section_text = "\n".join(lines[start - 1 : end])

        # Check seam ID appears in section
        seam_found = seam_id in section_text
        h.test_result(
            f"seam {seam_id} appears in section",
            seam_found,
            f"not found between lines {start}–{end}" if not seam_found else "",
        )

        if seam_found:
            seam_locations[seam_id] = (start, end)

        # Check for AskUserQuestion mention
        has_ask = "AskUserQuestion" in section_text
        h.test_result(
            f"seam {seam_id} — AskUserQuestion mentioned",
            has_ask,
            "" if has_ask else "mention not found near seam",
        )

        # Check for stop-state keywords
        has_stop_state = any(kw in section_text for kw in stop_keywords)
        h.test_result(
            f"seam {seam_id} — stop-state copy present",
            has_stop_state,
            f"none of {stop_keywords} found in section" if not has_stop_state else "",
        )

    # Test Final summary section for USAGE-GATE-SEAMS mention
    final_section_bounds = find_section_bounds(lines, "Final summary")
    if final_section_bounds:
        start, end = final_section_bounds
        final_text = "\n".join(lines[start - 1 : end])
        has_seams_doc = "USAGE-GATE-SEAMS" in final_text
        h.test_result(
            "Final summary includes USAGE-GATE-SEAMS documentation",
            has_seams_doc,
            "" if has_seams_doc else "USAGE-GATE-SEAMS not mentioned",
        )

        # Check for accounted/unaccounted tally
        has_tally = "agents=" in final_text or "accounted" in final_text
        h.test_result(
            "Final summary includes per-agent accounted/unaccounted tally",
            has_tally,
            "" if has_tally else "agent tally not found",
        )

        # Check for token_confidence caveat
        has_caveat = "token_confidence" in final_text or "low" in final_text
        h.test_result(
            "Final summary includes token_confidence caveat",
            has_caveat,
            "" if has_caveat else "caveat not found",
        )

    # Test: no hardcoded threshold strings in AskUserQuestion options
    threshold_patterns = [
        r'\+130[,_]?000',
        r'\+130k',
        r'\+\d+[,_]?\d+',
    ]
    offenders = []
    for match in re.finditer(r'AskUserQuestion.*?(?=\n\n|\Z)', text, re.DOTALL):
        question_block = match.group(0)
        for pattern in threshold_patterns:
            if re.search(pattern, question_block):
                # Double-check it's not in legitimate documentation
                if "option" in question_block.lower() or "label" in question_block.lower():
                    offenders.append(f"hardcoded threshold near line {text[:match.start()].count(chr(10))}")

    h.test_result(
        "no hardcoded threshold values in AskUserQuestion option text",
        len(offenders) == 0,
        "\n      " + "\n      ".join(offenders) if offenders else "",
    )

    # Test: no $0 in shell snippets within seams
    dollar_zero_offenders = []
    for seam_id in [s[0] for s in SEAMS]:
        if seam_id not in text:
            continue
        # Find context around seam
        idx = text.find(seam_id)
        # Look for shell snippets within 500 chars of seam mention
        snippet_start = max(0, idx - 200)
        snippet_end = min(len(text), idx + 500)
        snippet = text[snippet_start:snippet_end]

        if POSITIONAL_ZERO in snippet:
            line_no = text[:snippet_start].count("\n") + 1
            dollar_zero_offenders.append(f"seam {seam_id} (around line {line_no})")

    h.test_result(
        f"no shell snippets contain literal {POSITIONAL_ZERO}",
        len(dollar_zero_offenders) == 0,
        "\n      " + "\n      ".join(dollar_zero_offenders) if dollar_zero_offenders else "",
    )

    # Test: no echo "$VAR" | pattern in shell snippets within seams
    echo_pipe_offenders = []
    for seam_id in [s[0] for s in SEAMS]:
        if seam_id not in text:
            continue
        idx = text.find(seam_id)
        snippet_start = max(0, idx - 200)
        snippet_end = min(len(text), idx + 500)
        snippet = text[snippet_start:snippet_end]

        if re.search(ECHO_PIPE_PATTERN, snippet):
            line_no = text[:snippet_start].count("\n") + 1
            echo_pipe_offenders.append(f"seam {seam_id} (around line {line_no})")

    h.test_result(
        "no shell snippets pipe echo with unquoted variable through command",
        len(echo_pipe_offenders) == 0,
        "\n      " + "\n      ".join(echo_pipe_offenders) if echo_pipe_offenders else "",
    )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    test_seam_presence()
