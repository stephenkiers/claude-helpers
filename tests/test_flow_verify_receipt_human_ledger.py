#!/usr/bin/env python3
"""
Tests for human ledger support in scripts/flow-verify-receipt.py (#288).

Covers parse_addressed, owed_seqs, references_seq (D2 matching), and constants.
Tests the core logic by importing functions directly from the verifier.

Run with: python3 tests/test_flow_verify_receipt_human_ledger.py
"""

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

VERIFIER = REPO_ROOT / "scripts" / "flow-verify-receipt.py"

# Import the verifier module to access functions directly
spec = importlib.util.spec_from_file_location("flow_verify_receipt", VERIFIER)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)

# Import the functions and types we need
parse_addressed = verifier.parse_addressed
references_seq = verifier.references_seq
AddressedAbsent = verifier.AddressedAbsent
AddressedEmpty = verifier.AddressedEmpty
AddressedSeqs = verifier.AddressedSeqs
AddressedMalformed = verifier.AddressedMalformed
DIRECTION_SCOPE = verifier.DIRECTION_SCOPE
LedgerRecord = verifier.LedgerRecord


def make_record(seq, text, kind="direction", status="pending", step="plan"):
    """Create a LedgerRecord for testing."""
    return LedgerRecord(seq=seq, ts="2026-10-01T00:00:00Z", kind=kind, step=step,
                       status=status, text=text, supersedes=None)


def main():
    h = Harness("FLOW-VERIFY-RECEIPT HUMAN LEDGER TESTS (#288)")

    print("[Section 1] parse_addressed: basic cases and edge cases")

    result = parse_addressed("Decision: OK\n<!-- step-end -->\n")
    h.test_result("absent Addressed line returns AddressedAbsent",
                  isinstance(result, AddressedAbsent), str(type(result)))

    result = parse_addressed("Decision: OK\nAddressed: none\n<!-- step-end -->\n")
    h.test_result("'Addressed: none' returns AddressedEmpty",
                  isinstance(result, AddressedEmpty), str(type(result)))

    result = parse_addressed("Decision: OK\nAddressed: 1\n<!-- step-end -->\n")
    h.test_result("'Addressed: 1' returns AddressedSeqs with {1}",
                  isinstance(result, AddressedSeqs) and result.seqs == frozenset([1]),
                  f"result: {result}")

    result = parse_addressed("Decision: OK\nAddressed: 1, 2, 3\n<!-- step-end -->\n")
    h.test_result("'Addressed: 1, 2, 3' returns AddressedSeqs with {1, 2, 3}",
                  isinstance(result, AddressedSeqs) and result.seqs == frozenset([1, 2, 3]),
                  f"result: {result}")

    result = parse_addressed("Decision: OK\nAddressed: 1, 1, 2\n<!-- step-end -->\n")
    h.test_result("'Addressed: 1, 1, 2' deduplicates to {1, 2}",
                  isinstance(result, AddressedSeqs) and result.seqs == frozenset([1, 2]),
                  f"result: {result}")

    result = parse_addressed("Decision: OK\nAddressed: xyz\n<!-- step-end -->\n")
    h.test_result("'Addressed: xyz' (non-integer) returns AddressedMalformed",
                  isinstance(result, AddressedMalformed), str(type(result)))

    result = parse_addressed("Decision: OK\nAddressed: 1\nAddressed: 2\n<!-- step-end -->\n")
    h.test_result("two 'Addressed:' lines returns AddressedMalformed",
                  isinstance(result, AddressedMalformed), str(type(result)))

    result = parse_addressed("```\nAddressed: 1\n```\nDecision: OK\n<!-- step-end -->\n")
    h.test_result("'Addressed:' inside fenced code is ignored (treated as absent)",
                  isinstance(result, AddressedAbsent), str(type(result)))

    print()
    print("[Section 2] DIRECTION_SCOPE constant")

    h.test_result("DIRECTION_SCOPE includes plan, implement, review, fix",
                  DIRECTION_SCOPE == ("plan", "implement", "review", "fix"),
                  str(DIRECTION_SCOPE))

    print()
    print("[Section 3] references_seq: D2 matching")

    decisions_text = "# Decisions\n## Decided\n- seq 1: a direction\n- seq 13: another\n"

    result = references_seq(decisions_text, make_record(1, "a direction"))
    h.test_result("'seq 1' in decisions matches seq 1 record (word boundary)",
                  result == True, f"result: {result}")

    result = references_seq(decisions_text, make_record(3, "something"))
    h.test_result("'seq 1' does not match seq 3",
                  result == False, f"result: {result}")

    result = references_seq(decisions_text, make_record(13, "another"))
    h.test_result("'seq 13' in decisions matches seq 13 record (word boundary)",
                  result == True, f"result: {result}")

    # Test quote matching - short quote
    decisions_with_quote = "## Decisions\n## Decided\n- seq 1: \"exact match\"\n"
    result = references_seq(decisions_with_quote, make_record(1, "exact match"))
    h.test_result("short quote (under 20 chars) matches exactly",
                  result == True, f"result: {result}")

    # Test quote matching - long quote
    decisions_with_long = "## Decisions\n## Decided\n- seq 2: \"this is a very long quote with more text\"\n"
    result = references_seq(decisions_with_long, make_record(2, "this is a very long quote with more text and extra"))
    h.test_result("long quote (20+ chars) matches on 20-char substring",
                  result == True, f"result: {result}")

    # Test that quote outside a decision entry doesn't match
    no_decisions = "Some random text\nthis is a quote that is longer than 20 chars\n"
    result = references_seq(no_decisions, make_record(1, "this is a quote that is longer than 20 chars"))
    h.test_result("quote outside ## Decided section does not match",
                  result == False, f"result: {result}")

    # Test emphasis normalization
    emphasis_decisions = "## Decisions\n## Decided\n- seq 4: **bold text** _italic_\n"
    result = references_seq(emphasis_decisions, make_record(4, "bold text italic"))
    h.test_result("emphasis markers are stripped before matching",
                  result == True, f"result: {result}")

    # Test boundary: exactly 20-character quote (mutation boundary)
    exact_20_decisions = "## Decisions\n## Decided\n- seq 5: \"12345678901234567890\"\n"
    result = references_seq(exact_20_decisions, make_record(5, "12345678901234567890"))
    h.test_result("exactly 20-char quote matches (boundary between whole and substring rule)",
                  result == True, f"result: {result}")

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
