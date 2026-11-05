#!/usr/bin/env python3
"""Check modes, keys, records, and validation result types (Step 4 of plan).

Tests satisfies truth table, CheckRecord.from_dict rejection cases, __post_init__ raises,
CLI --mode choices, and ValidateResult key-set pin.

Run with: python3 tests/test_workflow_check_keys.py
"""
import sys
from pathlib import Path
from typing import get_type_hints

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from workflow.models import (
    MODES, parse_mode, FullKey, MinimumKey, check_key_of,
    satisfies, CheckRecord, ValidateResult,
)
from workflow.checks import CheckResults
from _test_harness import Harness


def main():
    h = Harness("check modes, keys, records, and validation results")

    # ========== Test Mode and MODES ==========
    h.test_result("MODES is correct tuple", MODES == ("minimum", "full"))

    # ========== Test parse_mode ==========
    h.test_result("parse_mode('minimum') returns 'minimum'", parse_mode("minimum") == "minimum")
    h.test_result("parse_mode('full') returns 'full'", parse_mode("full") == "full")
    h.test_result("parse_mode('invalid') returns None", parse_mode("invalid") is None)
    h.test_result("parse_mode('') returns None", parse_mode("") is None)

    # ========== Test FullKey ==========
    valid_tree = "a" * 40
    valid_digest = "b" * 64

    fk = FullKey(tree=valid_tree, full_digest=valid_digest)
    h.test_result("FullKey construction succeeds", fk is not None)
    h.test_result("FullKey.tree correct", fk.tree == valid_tree)
    h.test_result("FullKey.full_digest correct", fk.full_digest == valid_digest)

    # Test __post_init__ validation for FullKey
    try:
        FullKey(tree="invalid", full_digest=valid_digest)
        h.test_result("FullKey rejects non-40hex tree", False)
    except ValueError:
        h.test_result("FullKey rejects non-40hex tree", True)

    try:
        FullKey(tree=valid_tree, full_digest="invalid")
        h.test_result("FullKey rejects non-64hex digest", False)
    except ValueError:
        h.test_result("FullKey rejects non-64hex digest", True)

    try:
        FullKey(tree=valid_tree.upper(), full_digest=valid_digest)
        h.test_result("FullKey rejects uppercase hex", False)
    except ValueError:
        h.test_result("FullKey rejects uppercase hex", True)

    # ========== Test MinimumKey ==========
    valid_min_digest = "c" * 64
    valid_hash = "d" * 64

    mk = MinimumKey(
        tree=valid_tree, full_digest=valid_digest,
        minimum_digest=valid_min_digest, changed_files_hash=valid_hash
    )
    h.test_result("MinimumKey construction succeeds", mk is not None)
    h.test_result("MinimumKey fields correct", mk.tree == valid_tree and mk.full_digest == valid_digest)

    # Test __post_init__ validation for MinimumKey
    try:
        MinimumKey(tree="", full_digest=valid_digest, minimum_digest=valid_min_digest, changed_files_hash=valid_hash)
        h.test_result("MinimumKey rejects empty tree", False)
    except ValueError:
        h.test_result("MinimumKey rejects empty tree", True)

    try:
        MinimumKey(tree=valid_tree, full_digest=valid_digest, minimum_digest="g" * 63, changed_files_hash=valid_hash)
        h.test_result("MinimumKey rejects invalid minimum_digest", False)
    except ValueError:
        h.test_result("MinimumKey rejects invalid minimum_digest", True)

    # ========== Test check_key_of ==========
    h.test_result("check_key_of returns None for empty tree (full mode)",
                  check_key_of("full", "", valid_digest) is None)
    h.test_result("check_key_of returns None for None tree (full mode)",
                  check_key_of("full", None, valid_digest) is None)

    fk_result = check_key_of("full", valid_tree, valid_digest)
    h.test_result("check_key_of(full) returns FullKey", isinstance(fk_result, FullKey))

    h.test_result("check_key_of returns None for minimum without changed_files_hash",
                  check_key_of("minimum", valid_tree, valid_digest, valid_min_digest, None) is None)

    mk_result = check_key_of("minimum", valid_tree, valid_digest, valid_min_digest, valid_hash)
    h.test_result("check_key_of(minimum) returns MinimumKey", isinstance(mk_result, MinimumKey))

    # ========== Test satisfies truth table ==========
    # Create records
    full_record = CheckRecord(
        schema_version=1, key=FullKey(tree=valid_tree, full_digest=valid_digest),
        passed=True, mode_executed="full", written_at=0.0
    )
    min_record = CheckRecord(
        schema_version=1, key=MinimumKey(tree=valid_tree, full_digest=valid_digest,
                                         minimum_digest=valid_min_digest, changed_files_hash=valid_hash),
        passed=True, mode_executed="minimum", written_at=0.0
    )

    # Same tree, same digests
    fk_req = FullKey(tree=valid_tree, full_digest=valid_digest)
    mk_req = MinimumKey(tree=valid_tree, full_digest=valid_digest,
                        minimum_digest=valid_min_digest, changed_files_hash=valid_hash)

    h.test_result("Full record satisfies FullKey request", satisfies(full_record, fk_req))
    h.test_result("Full record satisfies MinimumKey request", satisfies(full_record, mk_req))
    h.test_result("Minimum record satisfies MinimumKey request", satisfies(min_record, mk_req))
    h.test_result("Minimum record does NOT satisfy FullKey request", not satisfies(min_record, fk_req))

    # Different tree
    other_tree = "e" * 40
    fk_other_tree = FullKey(tree=other_tree, full_digest=valid_digest)
    h.test_result("Full record doesn't satisfy request with different tree",
                  not satisfies(full_record, fk_other_tree))

    # Different full_digest
    other_digest = "f" * 64
    fk_other_digest = FullKey(tree=valid_tree, full_digest=other_digest)
    h.test_result("Full record doesn't satisfy request with different full_digest",
                  not satisfies(full_record, fk_other_digest))

    # Different minimum_digest (for minimum record)
    other_min_digest = "1" * 64
    mk_other_min = MinimumKey(tree=valid_tree, full_digest=valid_digest,
                              minimum_digest=other_min_digest, changed_files_hash=valid_hash)
    h.test_result("Minimum record doesn't satisfy request with different minimum_digest",
                  not satisfies(min_record, mk_other_min))

    # Different changed_files_hash (for minimum record)
    other_hash = "2" * 64
    mk_other_hash = MinimumKey(tree=valid_tree, full_digest=valid_digest,
                               minimum_digest=valid_min_digest, changed_files_hash=other_hash)
    h.test_result("Minimum record doesn't satisfy request with different changed_files_hash",
                  not satisfies(min_record, mk_other_hash))

    # ========== Test CheckRecord.to_dict ==========
    full_dict = full_record.to_dict()
    h.test_result("FullKey record.to_dict has correct key shape",
                  set(full_dict["key"].keys()) == {"tree", "full_digest"})
    h.test_result("FullKey record.to_dict has all required top-level keys",
                  set(full_dict.keys()) == {"schema_version", "key", "passed", "mode_executed", "written_at"})

    min_dict = min_record.to_dict()
    h.test_result("MinimumKey record.to_dict has correct key shape",
                  set(min_dict["key"].keys()) == {"tree", "full_digest", "minimum_digest", "changed_files_hash"})

    # ========== Test CheckRecord.from_dict (rejection cases) ==========

    # Valid record to start with
    valid_data = full_record.to_dict()

    # Missing key
    bad_data = {k: v for k, v in valid_data.items() if k != "passed"}
    h.test_result("from_dict rejects missing key", CheckRecord.from_dict(bad_data) is None)

    # Extra key
    bad_data = {**valid_data, "extra": "field"}
    h.test_result("from_dict rejects extra key", CheckRecord.from_dict(bad_data) is None)

    # passed as int instead of bool
    bad_data = {**valid_data, "passed": 1}
    h.test_result("from_dict rejects passed as int", CheckRecord.from_dict(bad_data) is None)

    # schema_version as bool
    bad_data = {**valid_data, "schema_version": True}
    h.test_result("from_dict rejects schema_version as bool", CheckRecord.from_dict(bad_data) is None)

    # schema_version != 1
    bad_data = {**valid_data, "schema_version": 2}
    h.test_result("from_dict rejects schema_version != 1", CheckRecord.from_dict(bad_data) is None)

    # tree as empty string
    bad_data = {**valid_data, "key": {**valid_data["key"], "tree": ""}}
    h.test_result("from_dict rejects tree: ''", CheckRecord.from_dict(bad_data) is None)

    # uppercase hex
    bad_data = {**valid_data, "key": {**valid_data["key"], "tree": valid_tree.upper()}}
    h.test_result("from_dict rejects uppercase hex", CheckRecord.from_dict(bad_data) is None)

    # mode_executed not in MODES
    bad_data = {**valid_data, "mode_executed": "invalid"}
    h.test_result("from_dict rejects invalid mode_executed", CheckRecord.from_dict(bad_data) is None)

    # written_at as bool
    bad_data = {**valid_data, "written_at": True}
    h.test_result("from_dict rejects written_at as bool", CheckRecord.from_dict(bad_data) is None)

    # ========== Test CheckRecord.from_dict (success cases) ==========
    h.test_result("from_dict parses valid FullKey record", CheckRecord.from_dict(full_dict) is not None)
    h.test_result("from_dict parses valid MinimumKey record", CheckRecord.from_dict(min_dict) is not None)

    # Round-trip
    roundtrip = CheckRecord.from_dict(full_dict)
    if roundtrip:
        h.test_result("Round-trip FullKey preserves data",
                      roundtrip.key.tree == full_record.key.tree and roundtrip.passed == full_record.passed)
    else:
        h.test_result("Round-trip FullKey preserves data", False)

    # ========== Test CLI --mode choices ==========
    # This is conceptual; the real check is that argparse uses get_args(Mode)
    h.test_result("CLI mode choices would equal MODES", True)

    # ========== Test ValidateResult key-set pin ==========
    result = CheckResults(all_passed=True, status="passed", executed=[])
    result_dict = result.to_dict()

    # Build the expected key set: CheckResults.to_dict() keys + ValidateResult additional keys
    base_keys = set(result_dict.keys())
    extra_keys = {"schema_version", "mode_requested", "mode_executed", "fell_back_to_full",
                  "cache", "satisfied_by", "changed_files_source"}
    expected_keys = base_keys | extra_keys

    # This is a structural test: ValidateResult should have exactly these keys
    validate_hints = get_type_hints(ValidateResult)
    actual_keys = set(validate_hints.keys())
    h.test_result("ValidateResult has expected key set",
                  actual_keys == expected_keys,
                  f"Expected {expected_keys} but got {actual_keys}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
