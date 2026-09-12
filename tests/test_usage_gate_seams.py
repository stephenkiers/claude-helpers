#!/usr/bin/env python3
"""
Test suite for usage-gate seam structure in implement-with-haiku.md.

Covers:
  - Only the round1-join seam remains as a live usage-check call + unconditional stop (per
    issue #174 / ADR-0019's 2026-09-12 amendment); the other four seams that used to exist
    (gate-fix-loop, pre-fanout, post-fanout, pre-round4) were removed outright, not skipped
  - round1-join never routes through AskUserQuestion or a computed proceed/stop decision
  - The Final summary still mentions USAGE-GATE (round1-join + final)
  - No literal $0 or problematic echo patterns in usage-check snippets

Run with: python3 tests/test_usage_gate_seams.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT

# Only this seam still runs usage-check mid-run (plus "final" at the very end).
REQUIRED_SEAM = "round1-join"

# These were removed outright in the 2026-09-12 amendment — must not reappear as --seam flags.
REMOVED_SEAMS = ["gate-fix-loop", "pre-fanout", "post-fanout", "pre-round4"]


def test_round1_join_seam_present():
    """The round1-join seam still appears as a --seam flag in the command doc."""
    doc_path = REPO_ROOT / "commands" / "implement-with-haiku.md"

    if not doc_path.exists():
        return False, f"implement-with-haiku.md not found at {doc_path}"

    doc_content = doc_path.read_text()

    if f"--seam {REQUIRED_SEAM}" not in doc_content:
        return False, f"seam '{REQUIRED_SEAM}' not found in --seam flag"

    return True, ""


def test_removed_seams_absent():
    """The four dropped seams no longer appear as --seam flags (removed, not just skipped)."""
    doc_path = REPO_ROOT / "commands" / "implement-with-haiku.md"

    if not doc_path.exists():
        return True, ""  # Skip if doc doesn't exist

    doc_content = doc_path.read_text()

    still_present = [s for s in REMOVED_SEAMS if f"--seam {s}" in doc_content]
    if still_present:
        return False, f"removed seam(s) still invoked via --seam: {', '.join(still_present)}"

    return True, ""


def test_round1_join_is_unconditional_checkpoint():
    """round1-join is an unconditional stop-and-wait checkpoint, not a gated AskUserQuestion ask.

    Per ADR-0019's 2026-09-12 amendment: round1-join is the only seam left, and it always stops
    unconditionally — no AskUserQuestion, no proceed/stop decision computed by the orchestrator,
    the DECISION: field is informational only.
    """
    doc_path = REPO_ROOT / "commands" / "implement-with-haiku.md"

    if not doc_path.exists():
        return True, ""  # Skip if doc doesn't exist

    doc_content = doc_path.read_text()

    if "do not call `AskUserQuestion`" not in doc_content:
        return False, "expected 'do not call `AskUserQuestion`' instruction not found"

    if "usage-check" not in doc_content:
        return False, "usage-check command not mentioned in implement-with-haiku.md"

    if "Checkpoint — Round 1 join" not in doc_content:
        return False, "missing the single remaining checkpoint marker for round1-join"

    return True, ""


def test_final_summary_mentions_usage_gate():
    """The Final summary section mentions usage gate seams."""
    doc_path = REPO_ROOT / "commands" / "implement-with-haiku.md"

    if not doc_path.exists():
        return True, ""  # Skip if doc doesn't exist

    doc_content = doc_path.read_text()

    if "USAGE-GATE" not in doc_content:
        return False, "Final summary should mention USAGE-GATE"

    return True, ""


def test_no_literal_dollar_zero_in_usage_check_snippets():
    """No literal $0 in usage-check command snippets."""
    doc_path = REPO_ROOT / "commands" / "implement-with-haiku.md"

    if not doc_path.exists():
        return True, ""  # Skip if doc doesn't exist

    POSITIONAL_ZERO = "$" + "0"
    doc_content = doc_path.read_text()

    lines = doc_content.splitlines()
    for i, line in enumerate(lines, start=1):
        if "usage-check" in line:
            start = max(0, i - 2)
            end = min(len(lines), i + 3)
            snippet = "\n".join(lines[start:end])

            if POSITIONAL_ZERO in snippet:
                return False, f"found literal $0 in usage-check snippet at line {i}"

    return True, ""


def test_no_echo_pipe_in_usage_check_snippets():
    """No problematic 'echo "$VAR" |' pattern in usage-check snippets."""
    doc_path = REPO_ROOT / "commands" / "implement-with-haiku.md"

    if not doc_path.exists():
        return True, ""  # Skip if doc doesn't exist

    doc_content = doc_path.read_text()

    if 'echo "$' in doc_content:
        lines = doc_content.splitlines()
        for i, line in enumerate(lines):
            if "usage-check" in line:
                start = max(0, i - 2)
                end = min(len(lines), i + 5)
                snippet = "\n".join(lines[start:end])

                if 'echo "$' in snippet:
                    return False, f"found 'echo \"$' pattern in usage-check snippet at line {i+1}"

    return True, ""


def main():
    h = Harness("USAGE-GATE SEAMS STRUCTURE TEST SUITE")

    h.test_result("round1-join seam present", *test_round1_join_seam_present())
    h.test_result("removed seams stay absent", *test_removed_seams_absent())
    h.test_result("round1-join is unconditional checkpoint", *test_round1_join_is_unconditional_checkpoint())
    h.test_result("final summary mentions USAGE-GATE", *test_final_summary_mentions_usage_gate())
    h.test_result("no literal $0 in usage-check snippets", *test_no_literal_dollar_zero_in_usage_check_snippets())
    h.test_result("no echo pipe in usage-check snippets", *test_no_echo_pipe_in_usage_check_snippets())

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
