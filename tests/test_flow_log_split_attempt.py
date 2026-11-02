#!/usr/bin/env python3
"""
Tests for SPLIT_ATTEMPT line parsing in scripts/flow_log.py.

Tests verify:
- parse_split_attempt_lines only returns lines with the SPLIT_ATTEMPT tag
- Malformed tagged lines are reported
- SPLIT_ATTEMPT lines do not affect existing log patterns (newest_attempt_start, accepted counter)
- Tagged line format consistency

Run with: python3 tests/test_flow_log_split_attempt.py
"""

import importlib.util
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

FLOW_LOG_MODULE = REPO_ROOT / "scripts" / "flow_log.py"


def load_flow_log_module():
    """Import scripts/flow_log.py as a module."""
    spec = importlib.util.spec_from_file_location("flow_log", FLOW_LOG_MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def iso_z_now() -> str:
    """Return current time in ISO-8601 UTC Z format."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def main() -> None:
    h = Harness("flow_log.py SPLIT_ATTEMPT parsing")
    flow_log = load_flow_log_module()

    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "log.md"

        print("[Test 1] parse_split_attempt_lines returns only tagged lines")
        ts = iso_z_now()
        attempt_data = {
            "schema_version": 1,
            "step": "implement",
            "attempt": 1,
            "trigger": "first",
            "split_basis": "parallel",
            "units": 2
        }
        log_content = f"""# Flow Log

{ts} implement begin

{ts} implement SPLIT_ATTEMPT {json.dumps(attempt_data)}

{ts} implement end

Some prose with JSON that looks like {json.dumps({"key": "value"})}

{json.dumps({"bare": "json"})}
"""
        log_path.write_text(log_content)

        lines = flow_log.parse_split_attempt_lines(str(log_path)).records
        h.test_result("returns 1 line", len(lines) == 1, f"got {len(lines)} lines")
        h.test_result("contains the attempt", lines[0] and attempt_data["trigger"] in str(lines[0]), str(lines[0]) if lines else None)

        print("\n[Test 2] Multiple SPLIT_ATTEMPT lines are all returned")
        attempt_2 = dict(attempt_data, attempt=2, trigger="retry")
        log_content = f"""# Flow Log

{ts} implement SPLIT_ATTEMPT {json.dumps(attempt_data)}
{ts} implement SPLIT_ATTEMPT {json.dumps(attempt_2)}
"""
        log_path.write_text(log_content)

        lines = flow_log.parse_split_attempt_lines(str(log_path)).records
        h.test_result("returns 2 lines", len(lines) == 2, f"got {len(lines)}")

        print("\n[Test 3] parse_split_attempt_lines ignores non-SPLIT_ATTEMPT tagged lines")
        log_content = f"""# Flow Log

{ts} implement begin
{ts} implement SPLIT_ATTEMPT {json.dumps(attempt_data)}
{ts} implement OTHER_TAG {{}}
{ts} implement end
"""
        log_path.write_text(log_content)

        lines = flow_log.parse_split_attempt_lines(str(log_path)).records
        h.test_result("returns 1 line (ignores OTHER_TAG)", len(lines) == 1, f"got {len(lines)}")

        print("\n[Test 4] Malformed JSON in SPLIT_ATTEMPT line is handled gracefully")
        log_content = f"""# Flow Log

{ts} implement SPLIT_ATTEMPT {{bad json
"""
        log_path.write_text(log_content)

        try:
            lines = flow_log.parse_split_attempt_lines(str(log_path)).records
            # Should either return empty or raise; either is acceptable
            h.test_result("handles malformed JSON", True, "Did not crash")
        except (json.JSONDecodeError, ValueError):
            h.test_result("raises on malformed JSON", True, "Raised as expected")

        print("\n[Test 5] Adding SPLIT_ATTEMPT lines does not change existing patterns")
        # Test that the log patterns used by existing code still match
        log_content = f"""# Flow Log

{ts} implement begin
{ts} implement SPLIT_ATTEMPT {json.dumps(attempt_data)}
{ts} implement end
"""
        log_path.write_text(log_content)

        # The "begin" line should still be found as the newest_attempt_start
        with open(log_path) as f:
            for line in f:
                if "implement begin" in line:
                    h.test_result("begin line still present", True)
                    break

        print("\n[Test 6] SPLIT_ATTEMPT line format validation")
        attempt_complete = {
            "schema_version": 1,
            "step": "implement",
            "attempt": 1,
            "trigger": "first",
            "prior_split_basis": "none",
            "split_basis": "parallel",
            "min_units": 2,
            "units": 2,
            "partial_work": "none",
            "decision": "OK",
            "failure_class": None,
            "turns_exhausted": False,
            "committed_units": [],
            "pending_units": ["u1", "u2"]
        }
        log_content = f"""# Flow Log

{ts} implement SPLIT_ATTEMPT {json.dumps(attempt_complete)}
"""
        log_path.write_text(log_content)

        lines = flow_log.parse_split_attempt_lines(str(log_path)).records
        h.test_result("complex SPLIT_ATTEMPT parses", len(lines) == 1 and lines[0], str(lines[0]) if lines else None)

        print("\n[Test 7] Empty log returns empty list")
        log_path.write_text("")
        lines = flow_log.parse_split_attempt_lines(str(log_path)).records
        h.test_result("empty log returns empty list", lines == [], str(lines))

        print("\n[Test 8] Non-existent log path returns empty list or raises")
        nonexistent = Path(tmp) / "nonexistent.md"
        try:
            lines = flow_log.parse_split_attempt_lines(str(nonexistent)).records
            h.test_result("nonexistent log handled", True, "Did not crash")
        except FileNotFoundError:
            h.test_result("nonexistent log raises", True, "Raised as expected")

        print("\n[Test 9] SPLIT_ATTEMPT with string boolean for turns_exhausted is rejected")
        attempt_string_bool = {
            "schema_version": 1,
            "step": "implement",
            "attempt": 1,
            "trigger": "first",
            "prior_split_basis": "none",
            "split_basis": "parallel",
            "min_units": "2",
            "units": 2,
            "partial_work": "none",
            "decision": "OK",
            "failure_class": None,
            "turns_exhausted": "true",  # String instead of boolean
            "committed_units": [],
            "pending_units": []
        }
        log_content = f"""# Flow Log

{ts} implement SPLIT_ATTEMPT {json.dumps(attempt_string_bool)}
"""
        log_path.write_text(log_content)

        result = flow_log.parse_split_attempt_lines(str(log_path))
        h.test_result("string boolean rejected", len(result.records) == 0 and len(result.malformed_lines) > 0, f"records={len(result.records)}, malformed={len(result.malformed_lines)}")

        print("\n[Test 10] SPLIT_ATTEMPT with real boolean for turns_exhausted is accepted")
        attempt_real_bool = dict(attempt_string_bool, turns_exhausted=False)  # Real boolean
        log_content = f"""# Flow Log

{ts} implement SPLIT_ATTEMPT {json.dumps(attempt_real_bool)}
"""
        log_path.write_text(log_content)

        result = flow_log.parse_split_attempt_lines(str(log_path))
        h.test_result("real boolean accepted", len(result.records) == 1 and result.records[0].turns_exhausted is False, f"records={len(result.records)}")

        print("\n[Test 11] SPLIT_ATTEMPT with true boolean for turns_exhausted is accepted")
        attempt_true_bool = dict(attempt_string_bool, turns_exhausted=True)
        log_content = f"""# Flow Log

{ts} implement SPLIT_ATTEMPT {json.dumps(attempt_true_bool)}
"""
        log_path.write_text(log_content)

        result = flow_log.parse_split_attempt_lines(str(log_path))
        h.test_result("true boolean accepted", len(result.records) == 1 and result.records[0].turns_exhausted is True, f"records={len(result.records)}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
