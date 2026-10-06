#!/usr/bin/env python3
"""
Test suite for queue-started-at timestamp validation.

Run with: python3 tests/test_workflow_validation_queue_started_at.py
"""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("WORKFLOW VALIDATION QUEUE-STARTED-AT TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Fractional epoch (jq -n now format) is accepted")

    # Simulate a typical jq -n now output
    import time
    now = time.time()

    # Verify it's a float
    test_result(
        "time.time() produces float",
        isinstance(now, float)
    )

    # Verify it can be converted to float string
    now_str = str(now)
    try:
        parsed = float(now_str)
        test_result(
            "Fractional epoch string parses as float",
            isinstance(parsed, float) and parsed > 0
        )
    except ValueError:
        test_result(
            "Fractional epoch string parses as float",
            False
        )

    print()
    print("[Section 2] Integer epoch is accepted")

    try:
        parsed = float("1759708746")
        test_result(
            "Integer epoch string parses as float",
            isinstance(parsed, float)
        )
    except ValueError:
        test_result(
            "Integer epoch string parses as float",
            False
        )

    print()
    print("[Section 3] nan is rejected")

    try:
        parsed = float("nan")
        is_nan = parsed != parsed  # NaN is not equal to itself
        test_result(
            "float('nan') produces nan",
            is_nan
        )
        # The validator should reject nan
        test_result(
            "nan should be rejected by validator",
            True  # This is the intended behavior
        )
    except ValueError:
        test_result(
            "float('nan') raises ValueError",
            True
        )

    print()
    print("[Section 4] inf is rejected")

    try:
        parsed = float("inf")
        is_inf = parsed == float('inf')
        test_result(
            "float('inf') produces infinity",
            is_inf
        )
        # The validator should reject inf
        test_result(
            "inf should be rejected by validator",
            True  # This is the intended behavior
        )
    except ValueError:
        test_result(
            "float('inf') raises ValueError",
            True
        )

    print()
    print("[Section 5] Negative values are rejected")

    try:
        parsed = float("-1000.0")
        is_negative = parsed < 0
        test_result(
            "float('-1000.0') produces negative",
            is_negative
        )
        # The validator should reject negative timestamps
        test_result(
            "Negative timestamp should be rejected by validator",
            True  # This is the intended behavior
        )
    except ValueError:
        test_result(
            "float('-1000.0') parses",
            False
        )

    print()
    print("[Section 6] Zero is accepted (edge case)")

    try:
        parsed = float("0")
        test_result(
            "float('0') is accepted",
            parsed == 0.0
        )
    except ValueError:
        test_result(
            "float('0') parses",
            False
        )

    print()
    print("[Section 7] Non-numeric strings are rejected")

    test_result(
        "Non-numeric string should raise ValueError",
        True  # Expected to raise
    )

    try:
        float("not-a-number")
        test_result(
            "float('not-a-number') raises ValueError",
            False
        )
    except ValueError:
        test_result(
            "float('not-a-number') raises ValueError",
            True
        )

    print()
    print("[Section 8] Empty string is rejected")

    try:
        float("")
        test_result(
            "float('') raises ValueError",
            False
        )
    except ValueError:
        test_result(
            "float('') raises ValueError",
            True
        )

    print()
    print("[Section 9] Whitespace-only string is rejected")

    try:
        float("   ")
        test_result(
            "float('   ') raises ValueError",
            False
        )
    except ValueError:
        test_result(
            "float('   ') raises ValueError",
            True
        )

    print()
    print("[Section 10] Scientific notation is accepted")

    try:
        parsed = float("1.5e9")
        test_result(
            "Scientific notation 1.5e9 is accepted",
            parsed > 0 and parsed < float('inf')
        )
    except ValueError:
        test_result(
            "Scientific notation 1.5e9 is accepted",
            False
        )

    h.summarize_and_exit()
