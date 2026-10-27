#!/usr/bin/env python3
"""
Telemetry bucketing for flow step agents.

Groups flow-implement and flow-review agent rows together with pre-cutover
general-purpose agent rows that fall inside a flow session.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Set
import sys
import os

# Add scripts/ to path so we can import telemetry_schema if needed
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import telemetry_schema

# Cut-over date: when flow-implement and flow-review agents replaced general-purpose for flow steps.
# IMPORTANT: Set to the merge date of ticket #358; must be set at merge time and never changed thereafter
# (changing it retroactively invalidates historical bucketing and trends). Current value: 2026-10-26.
FLOW_AGENT_CUTOVER_DATE = "2026-10-26"

# Session IDs from the Step 2 hermetic probes in the 2026-10-26 #358 ticket (7 distinct sessions:
# Steps 2.1–2.6 hook/permission/model probes; see ADR-0008 amendment). These test-fixture rows
# pollute telemetry; completely excluded from all bucketing (counted separately in
# excluded_probe_sessions output). List is append-only: older probe sessions are never removed,
# ensuring metrics remain stable as new probes run and preventing drift in leaderboard rankings.
PROBE_SESSION_IDS: Set[str] = frozenset({
    "04a8f48c-638f-4955-8e62-875e46f8d12c",
    "6f4a40a1-3ce5-43be-a1a6-5471cdc38681",
    "9edf70ee-c2fe-46e9-9c4f-4542d43a27a1",
    "d4642c82-4847-4f5d-87e2-858531c191be",
    "d95030c8-215a-4a42-ab98-3c5acf75f017",
    "ecc74372-41b5-4ae4-a75a-b52fca891e73",
    "ff4dabbe-3fdc-4c93-a9c4-8ea2190f5202",
})


def _parse_iso_timestamp(ts_str: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp string to datetime, or None if unparseable.

    Mirrors telemetry_schema._parse_iso_or_none logic for consistency.
    """
    if not isinstance(ts_str, str):
        return None
    try:
        # Try parsing with timezone
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return None
        return dt
    except (ValueError, TypeError):
        return None


def bucket_flow_agent_rows(
    events: List[Any],
    cutover_date: str,
    probe_session_ids: Set[str],
) -> Dict[str, Any]:
    """Bucket agent events into flow step agents and pre-cutover general-purpose in flow sessions.

    A flow session is one that contains either:
    - A command.begin event with command="expert-flow", OR
    - Any agent.begin event with agent_type in {flow-implement, flow-review}

    For each flow session, this function counts:
    - agent.begin events with agent_type in {flow-implement, flow-review}
    - agent.begin events with agent_type=general-purpose recorded BEFORE cutover_date

    Probe session IDs (from Step 2 hermetic probes) are completely excluded from all bucketing.

    Args:
        events: List of telemetry event dicts (must have fields: timestamp, event_type,
                session_id, agent_type (for agent events), command (for command events))
        cutover_date: ISO-8601 date string marking the cutover from general-purpose to flow-*
                      agents (e.g., "2026-10-26")
        probe_session_ids: Set of session IDs to exclude (test-fixture rows)

    Returns:
        A dict with keys:
        - flow_step_agent_count: Count of flow-implement and flow-review agent.begin events
        - pre_cutover_general_purpose_in_flow_count: Count of general-purpose agent.begin
                                                     events in flow sessions, before cutover
        - cutover_date: The cutover date string
        - excluded_probe_sessions: Count of probe sessions excluded
    """
    # Parse the cutover date to a datetime for comparison
    cutover_dt = _parse_iso_timestamp(cutover_date + "T00:00:00Z")
    if cutover_dt is None:
        # Fallback: treat as unparseable and return zeros
        return {
            "flow_step_agent_count": 0,
            "pre_cutover_general_purpose_in_flow_count": 0,
            "cutover_date": cutover_date,
            "excluded_probe_sessions": 0,
        }

    # Identify flow sessions: sessions that contain expert-flow command or flow-* agents
    flow_session_ids: Set[str] = set()
    general_purpose_agent_events: List[Dict[str, Any]] = []
    flow_agent_count = 0
    excluded_probe_count = 0

    # First pass: identify flow sessions and collect general-purpose agent events
    for event in events:
        if not isinstance(event, dict):
            continue

        session_id = event.get("session_id")
        if not isinstance(session_id, str) or session_id == telemetry_schema.UNKNOWN:
            continue

        # Count and exclude probe sessions
        if session_id in probe_session_ids:
            excluded_probe_count += 1
            continue

        event_type = event.get("event_type")

        # Mark flow sessions: those with expert-flow command
        if event_type == "command.begin":
            command = event.get("command")
            if command == "expert-flow":
                flow_session_ids.add(session_id)

        # Mark flow sessions: those with flow-* agent types
        if event_type == "agent.begin":
            agent_type = event.get("agent_type")
            if agent_type in {"flow-implement", "flow-review"}:
                flow_session_ids.add(session_id)
                flow_agent_count += 1

            # Collect general-purpose agent events for later filtering
            if agent_type == "general-purpose":
                general_purpose_agent_events.append(event)

    # Second pass: count pre-cutover general-purpose agents in flow sessions
    pre_cutover_general_purpose_count = 0
    for event in general_purpose_agent_events:
        session_id = event.get("session_id")
        if session_id not in flow_session_ids:
            continue

        ts_str = event.get("timestamp")
        ts = _parse_iso_timestamp(ts_str) if isinstance(ts_str, str) else None
        if ts is None:
            continue

        if ts < cutover_dt:
            pre_cutover_general_purpose_count += 1

    return {
        "flow_step_agent_count": flow_agent_count,
        "pre_cutover_general_purpose_in_flow_count": pre_cutover_general_purpose_count,
        "cutover_date": cutover_date,
        "excluded_probe_sessions": excluded_probe_count,
    }
