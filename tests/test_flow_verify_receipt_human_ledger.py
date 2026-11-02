#!/usr/bin/env python3
"""
Tests for human ledger support in scripts/flow-verify-receipt.py (#288).

Covers parse_addressed, owed_seqs, references_seq (D2 matching), and constants.
Tests the core logic by importing functions directly from the verifier.

Run with: python3 tests/test_flow_verify_receipt_human_ledger.py
"""

import importlib.util
import sys
import time
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
owed_seqs = verifier.owed_seqs
AddressedAbsent = verifier.AddressedAbsent
AddressedEmpty = verifier.AddressedEmpty
AddressedSeqs = verifier.AddressedSeqs
AddressedMalformed = verifier.AddressedMalformed
DIRECTION_SCOPE = verifier.DIRECTION_SCOPE
LedgerRecord = verifier.LedgerRecord


def make_record(seq, text, kind="direction", status="pending", step="plan", ts=None, supersedes=None):
    """Create a LedgerRecord for testing. ts defaults to a float timestamp."""
    if ts is None:
        ts = 1727740800.0  # 2026-10-01T00:00:00Z as a float
    return LedgerRecord(seq=seq, ts=ts, kind=kind, step=step,
                       status=status, text=text, supersedes=supersedes)


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

    # Empty or comma-only Addressed: is malformed (not empty set)
    result = parse_addressed("Decision: OK\nAddressed: \n<!-- step-end -->\n")
    h.test_result("'Addressed: ' (empty value) returns AddressedMalformed",
                  isinstance(result, AddressedMalformed), str(type(result)))

    result = parse_addressed("Decision: OK\nAddressed: , , \n<!-- step-end -->\n")
    h.test_result("'Addressed: , , ' (comma-only) returns AddressedMalformed",
                  isinstance(result, AddressedMalformed), str(type(result)))

    print()
    print("[Section 2] DIRECTION_SCOPE constant")

    h.test_result("DIRECTION_SCOPE includes plan, implement, review, fix",
                  DIRECTION_SCOPE == ("plan", "implement", "review", "fix"),
                  str(DIRECTION_SCOPE))

    print()
    print("[Section 3] references_seq: D2 matching")

    # Test word-bounded seq matching - seq 1 must not match seq 12
    decisions_text = "# Decisions\n## Decided\n- seq 1: a direction\n- seq 12: another\n"

    result = references_seq(decisions_text, make_record(1, "a direction"))
    h.test_result("'seq 1:' matches seq 1 record (word boundary)",
                  result is True, f"result: {result}")

    result = references_seq(decisions_text, make_record(12, "another"))
    h.test_result("'seq 12:' matches seq 12 record (word boundary)",
                  result is True, f"result: {result}")

    # seq 1 should NOT match when seq 12 is the only entry (word boundary test)
    decisions_no_seq1 = "# Decisions\n## Decided\n- seq 12: another\n"
    result = references_seq(decisions_no_seq1, make_record(1, "a direction"))
    h.test_result("seq 1 does NOT match 'seq 12' (word boundary enforced)",
                  result is False, f"result: {result}")

    result = references_seq(decisions_no_seq1, make_record(3, "something"))
    h.test_result("seq 3 does not match decisions with seq 12",
                  result is False, f"result: {result}")

    # Test quote matching - short quote
    decisions_with_quote = "## Decisions\n## Decided\n- seq 3: \"exact match\"\n"
    result = references_seq(decisions_with_quote, make_record(3, "exact match"))
    h.test_result("short quote (under 20 chars) matches exactly",
                  result is True, f"result: {result}")

    # Test quote matching - long quote
    decisions_with_long = "## Decisions\n## Decided\n- seq 4: \"this is a very long quote with more text\"\n"
    result = references_seq(decisions_with_long, make_record(4, "this is a very long quote with more text and extra"))
    h.test_result("long quote (20+ chars) matches on 20-char substring",
                  result is True, f"result: {result}")

    # Test that quote outside a decision entry doesn't match
    no_decisions = "Some random text\nthis is a quote that is longer than 20 chars\n"
    result = references_seq(no_decisions, make_record(5, "this is a quote that is longer than 20 chars"))
    h.test_result("quote outside ## Decided section does not match",
                  result is False, f"result: {result}")

    # Test emphasis normalization
    emphasis_decisions = "## Decisions\n## Decided\n- seq 6: **bold text** _italic_\n"
    result = references_seq(emphasis_decisions, make_record(6, "bold text italic"))
    h.test_result("emphasis markers are stripped before matching",
                  result is True, f"result: {result}")

    # Test boundary: exactly 20-character quote (mutation boundary)
    exact_20_decisions = "## Decisions\n## Decided\n- seq 7: \"12345678901234567890\"\n"
    result = references_seq(exact_20_decisions, make_record(7, "12345678901234567890"))
    h.test_result("exactly 20-char quote matches (boundary between whole and substring rule)",
                  result is True, f"result: {result}")

    # Test curly quote normalization (smart quotes should normalize to straight quotes)
    # Use Unicode curly quotes: U+201C and U+201D
    curly_decisions = "## Decisions\n## Decided\n- seq 8: “smart quoted text”\n"
    result = references_seq(curly_decisions, make_record(8, "smart quoted text"))
    h.test_result("curly (smart) quotes normalize to straight quotes and match",
                  result is True, f"result: {result}")

    print()
    print("[Section 4] owed_seqs: D1 direction binding")

    # Create base timestamp
    base_ts = 1727740800.0  # 2026-10-01T00:00:00Z

    # Test 1: pending direction in snapshot window is owed
    records = (make_record(1, "a direction", kind="direction", status="pending", step="plan", ts=base_ts - 100),)
    result = owed_seqs(records, "plan", base_ts)
    h.test_result("pending direction in snapshot window is owed",
                  result == frozenset([1]), f"result: {result}")

    # Test 2: pending direction outside snapshot window is not owed
    records = (make_record(1, "a direction", kind="direction", status="pending", step="plan", ts=base_ts + 100),)
    result = owed_seqs(records, "plan", base_ts)
    h.test_result("pending direction after step_begin_ts is not owed",
                  result == frozenset(), f"result: {result}")

    # Test 3: addressed direction is not owed
    records = (make_record(1, "a direction", kind="direction", status="addressed", step="plan", ts=base_ts - 100),)
    result = owed_seqs(records, "plan", base_ts)
    h.test_result("addressed direction is not owed",
                  result == frozenset(), f"result: {result}")

    # Test 4: pending question is owed
    records = (make_record(1, "a question", kind="question", status="pending", step="plan", ts=base_ts - 100),)
    result = owed_seqs(records, "plan", base_ts)
    h.test_result("pending question in snapshot window is owed",
                  result == frozenset([1]), f"result: {result}")

    # Test 5: pending answer is not owed (not a direction or question)
    records = (make_record(1, "an answer", kind="answer", status="pending", step="plan", ts=base_ts - 100),)
    result = owed_seqs(records, "plan", base_ts)
    h.test_result("pending answer is not owed (not direction/question)",
                  result == frozenset(), f"result: {result}")

    # Test 6: direction from earlier step is owed at later step
    records = (make_record(1, "a direction", kind="direction", status="pending", step="plan", ts=base_ts - 100),)
    result = owed_seqs(records, "implement", base_ts)
    h.test_result("direction from plan step is owed at implement step",
                  result == frozenset([1]), f"result: {result}")

    # Test 7: direction from later step is not owed at earlier step
    records = (make_record(1, "a direction", kind="direction", status="pending", step="implement", ts=base_ts - 100),)
    result = owed_seqs(records, "plan", base_ts)
    h.test_result("direction from implement step is not owed at plan step",
                  result == frozenset(), f"result: {result}")

    # Test 8: correction kind uses superseded record's kind
    # A correction that supersedes a direction is treated as a direction
    records = (
        make_record(1, "original direction", kind="direction", status="superseded", step="plan", ts=base_ts - 200),
        make_record(2, "corrected direction", kind="correction", status="pending", step="plan", ts=base_ts - 100, supersedes=1),
    )
    # Note: we need to pass the full tuple with the correction
    result = owed_seqs(records, "plan", base_ts)
    h.test_result("correction (when supersedes direction) is owed as direction",
                  2 in result, f"result: {result}")

    # Test 9: multiple pending directions are all owed
    records = (
        make_record(1, "direction 1", kind="direction", status="pending", step="plan", ts=base_ts - 100),
        make_record(2, "direction 2", kind="direction", status="pending", step="plan", ts=base_ts - 50),
    )
    result = owed_seqs(records, "plan", base_ts)
    h.test_result("multiple pending directions are all owed",
                  result == frozenset([1, 2]), f"result: {result}")

    print()
    print("[Section 5] Edge cases and negative tests")

    # Negative test: empty records list
    result = owed_seqs((), "plan", base_ts)
    h.test_result("empty records list yields empty owed set",
                  result == frozenset(), f"result: {result}")

    # Negative test: all records are addressed
    records = (
        make_record(1, "dir 1", kind="direction", status="addressed", step="plan", ts=base_ts - 100),
        make_record(2, "dir 2", kind="direction", status="addressed", step="plan", ts=base_ts - 50),
    )
    result = owed_seqs(records, "plan", base_ts)
    h.test_result("all addressed records yields empty owed set",
                  result == frozenset(), f"result: {result}")

    print()
    print("[Section 6] Deep correction chains (Finding 6 fix)")

    # Test that deep correction chains resolve to base kind
    records = (
        make_record(1, "base direction", kind="direction", status="superseded", step="plan", ts=base_ts - 300),
        make_record(2, "correction 1", kind="correction", status="superseded", step="plan", ts=base_ts - 200, supersedes=1),
        make_record(3, "correction 2", kind="correction", status="pending", step="plan", ts=base_ts - 100, supersedes=2),
    )
    result = owed_seqs(records, "plan", base_ts)
    h.test_result("deep correction chain resolves to base direction kind",
                  3 in result, f"result: {result}")
    h.test_result("only the latest correction is in owed set",
                  result == frozenset([3]), f"result: {result}")

    print()
    print("[Section 7] Curly quote matching strengthening")

    # Strengthen curly quote test: verify that quote matching actually works by using a case
    # where the seq token doesn't match but the quote does
    curly_decisions_quote_only = "## Decisions\n## Decided\n- different decision with “smart quoted text”\n"
    result = references_seq(curly_decisions_quote_only, make_record(9, "smart quoted text"))
    h.test_result("curly quotes in decisions match via quote normalization (not seq token)",
                  result is True, f"result: {result}")

    # And verify the positive case: curly quotes in decisions match with straight quote text
    # Using Unicode escape sequences for curly quotes: U+201C and U+201D
    curly_decisions_yes = "## Decisions\n## Decided\n- seq 10: “smart quoted text”\n"
    result = references_seq(curly_decisions_yes, make_record(10, "smart quoted text"))
    h.test_result("curly quotes in decisions with seq token match straight quotes in record",
                  result is True, f"result: {result}")

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
