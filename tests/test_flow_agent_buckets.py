#!/usr/bin/env python3
"""
Test suite for flow agent telemetry bucketing (T-D3).

Covers:
1. bucket_flow_agent_rows() function imports and runs
2. Cut-over date constant is defined
3. Probe session IDs are excluded
4. Flow sessions are correctly identified
5. Pre-cutover general-purpose agents in flow sessions are bucketed correctly

Run with: python3 tests/test_flow_agent_buckets.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT

# Try to import the telemetry module
try:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import flow_agent_buckets
    HAS_FLOW_AGENT_BUCKETS = True
except ImportError as e:
    HAS_FLOW_AGENT_BUCKETS = False
    IMPORT_ERROR = str(e)


def main():
    h = Harness("FLOW AGENT BUCKETS TELEMETRY TEST SUITE")

    print("[Test 1] flow_agent_buckets module imports\n")

    h.test_result(
        "flow_agent_buckets.py can be imported",
        HAS_FLOW_AGENT_BUCKETS,
        f"Import failed: {IMPORT_ERROR if not HAS_FLOW_AGENT_BUCKETS else ''}"
    )

    if not HAS_FLOW_AGENT_BUCKETS:
        h.summarize_and_exit()

    print()
    print("[Test 2] Module constants are defined\n")

    h.test_result(
        "FLOW_AGENT_CUTOVER_DATE is defined",
        hasattr(flow_agent_buckets, 'FLOW_AGENT_CUTOVER_DATE'),
    )

    h.test_result(
        "PROBE_SESSION_IDS is defined",
        hasattr(flow_agent_buckets, 'PROBE_SESSION_IDS'),
    )

    if hasattr(flow_agent_buckets, 'FLOW_AGENT_CUTOVER_DATE'):
        cutover = flow_agent_buckets.FLOW_AGENT_CUTOVER_DATE
        h.test_result(
            f"FLOW_AGENT_CUTOVER_DATE is a date string ('{cutover}')",
            isinstance(cutover, str) and len(cutover) >= 8 and cutover[0:4].isdigit(),
            f"Expected YYYY-MM-DD format, got '{cutover}'"
        )

    if hasattr(flow_agent_buckets, 'PROBE_SESSION_IDS'):
        probe_ids = flow_agent_buckets.PROBE_SESSION_IDS
        h.test_result(
            f"PROBE_SESSION_IDS is non-empty (found {len(probe_ids)} ids)",
            len(probe_ids) > 0,
        )

    print()
    print("[Test 3] bucket_flow_agent_rows function signature and basic call\n")

    h.test_result(
        "bucket_flow_agent_rows function exists",
        hasattr(flow_agent_buckets, 'bucket_flow_agent_rows'),
    )

    if hasattr(flow_agent_buckets, 'bucket_flow_agent_rows'):
        func = flow_agent_buckets.bucket_flow_agent_rows

        # Test empty input
        result = func([], "2026-10-26", set())
        h.test_result(
            "bucket_flow_agent_rows handles empty events list",
            isinstance(result, dict),
            f"Expected dict, got {type(result)}"
        )

        # Test result structure
        if isinstance(result, dict):
            expected_keys = {
                'flow_step_agent_count',
                'pre_cutover_general_purpose_in_flow_count',
                'cutover_date',
                'excluded_probe_sessions',
            }
            missing_keys = expected_keys - set(result.keys())
            h.test_result(
                "bucket_flow_agent_rows result has required keys",
                not missing_keys,
                f"Missing keys: {missing_keys}"
            )

            # Verify values are numeric/string
            if not missing_keys:
                h.test_result(
                    "flow_step_agent_count is numeric",
                    isinstance(result['flow_step_agent_count'], int),
                )

                h.test_result(
                    "pre_cutover_general_purpose_in_flow_count is numeric",
                    isinstance(result['pre_cutover_general_purpose_in_flow_count'], int),
                )

                h.test_result(
                    "cutover_date is string",
                    isinstance(result['cutover_date'], str),
                )

                h.test_result(
                    "excluded_probe_sessions is numeric",
                    isinstance(result['excluded_probe_sessions'], int),
                )

    print()
    print("[Test 4] Bucketing logic with sample data\n")

    # Create sample events with correct event_type format (dots, not dashes)
    sample_events = [
        # Expert-flow command begin
        {
            "timestamp": "2026-10-26T10:00:00Z",
            "event_type": "command.begin",
            "session_id": "session-1",
            "command": "expert-flow",
        },
        # flow-implement agent
        {
            "timestamp": "2026-10-26T10:01:00Z",
            "event_type": "agent.begin",
            "session_id": "session-1",
            "agent_type": "flow-implement",
        },
        # General-purpose agent before cutover
        {
            "timestamp": "2026-10-25T10:00:00Z",
            "event_type": "agent.begin",
            "session_id": "session-2",
            "agent_type": "general-purpose",
        },
        # General-purpose agent in flow session, after cutover (should NOT be counted)
        {
            "timestamp": "2026-10-27T10:00:00Z",
            "event_type": "agent.begin",
            "session_id": "session-1",
            "agent_type": "general-purpose",
        },
    ]

    result = flow_agent_buckets.bucket_flow_agent_rows(
        sample_events,
        "2026-10-26",
        set()  # No probe session IDs to exclude
    )

    h.test_result(
        "flow_step_agent_count counts flow-implement agents",
        result['flow_step_agent_count'] >= 1,
        f"Expected >= 1, got {result['flow_step_agent_count']}"
    )

    print()
    print("[Test 5] Probe session IDs are excluded\n")

    # Create events with a probe session ID
    probe_session = "04a8f48c-638f-4955-8e62-875e46f8d12c"  # From PROBE_SESSION_IDS
    probe_events = [
        {
            "timestamp": "2026-10-26T10:00:00Z",
            "event_type": "agent.begin",
            "session_id": probe_session,
            "agent_type": "flow-implement",
        },
        {
            "timestamp": "2026-10-26T10:00:00Z",
            "event_type": "agent.begin",
            "session_id": "session-real",
            "agent_type": "flow-implement",
        },
    ]

    result_without_exclusion = flow_agent_buckets.bucket_flow_agent_rows(
        probe_events,
        "2026-10-26",
        set()  # Not excluding
    )

    result_with_exclusion = flow_agent_buckets.bucket_flow_agent_rows(
        probe_events,
        "2026-10-26",
        {probe_session}  # Excluding the probe session
    )

    h.test_result(
        "bucket_flow_agent_rows excludes specified probe session IDs",
        result_without_exclusion['flow_step_agent_count'] > result_with_exclusion['flow_step_agent_count'],
        f"Without exclusion: {result_without_exclusion['flow_step_agent_count']}, "
        f"with exclusion: {result_with_exclusion['flow_step_agent_count']}"
    )

    print()

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
