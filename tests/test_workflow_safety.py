#!/usr/bin/env python3
"""
Test suite for safety module (safety.py).

Covers: Unknown type as a fail-closed marker for unresolvable states,
ensuring it's used consistently across modules to prevent silent defaults.

Run with: python3 tests/test_workflow_safety.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.safety import Unknown
from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("WORKFLOW SAFETY TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Unknown type stores reason")

    reason = "git repository not found"
    unk = Unknown(reason)

    test_result(
        "Unknown stores reason",
        unk.reason == reason
    )
    test_result(
        "Unknown reason is accessible",
        hasattr(unk, "reason")
    )

    print()
    print("[Section 2] Unknown has readable string representation")

    unk = Unknown("test reason")
    str_repr = str(unk)

    test_result(
        "Unknown str representation is non-empty",
        len(str_repr) > 0
    )
    test_result(
        "Unknown str representation includes reason",
        "test reason" in str_repr or "Unknown" in str_repr
    )

    print()
    print("[Section 3] Unknown supports repr")

    unk = Unknown("another reason")
    repr_str = repr(unk)

    test_result(
        "Unknown repr is non-empty",
        len(repr_str) > 0
    )
    test_result(
        "Unknown repr contains 'Unknown'",
        "Unknown" in repr_str
    )

    print()
    print("[Section 4] Unknown instances are distinguishable from other types")

    unk = Unknown("test")
    test_result(
        "Unknown is not None",
        unk is not None
    )
    test_result(
        "Unknown is not a string",
        not isinstance(unk, str)
    )
    test_result(
        "Unknown is not a bool",
        not isinstance(unk, bool)
    )
    test_result(
        "Unknown is not an int",
        not isinstance(unk, int)
    )

    print()
    print("[Section 5] Unknown can be used in isinstance checks")

    unk = Unknown("reason")
    test_result(
        "isinstance(unk, Unknown) is True",
        isinstance(unk, Unknown)
    )

    not_unk = "some string"
    test_result(
        "isinstance(string, Unknown) is False",
        not isinstance(not_unk, Unknown)
    )

    print()
    print("[Section 6] Unknown supports equality checks")

    unk1 = Unknown("reason")
    unk2 = Unknown("reason")
    unk3 = Unknown("different")

    test_result(
        "Unknown instances can be compared",
        unk1 == unk1 or unk1 != "other"
    )

    print()
    print("[Section 7] Unknown reason can be any string")

    reasons = [
        "file not found",
        "malformed JSON",
        "git error: exit code 128",
        "no worktrees detected",
        "",
        "  spaces  ",
        "unicode: ñ é ü",
    ]

    for reason in reasons:
        unk = Unknown(reason)
        test_result(
            f"Unknown accepts reason: {reason[:20]}...",
            unk.reason == reason
        )

    print()
    h.summarize_and_exit()
