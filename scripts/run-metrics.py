#!/usr/bin/env python3
"""
CLI for recording telemetry events to the local log.

Each subcommand reads hook payloads from stdin or environment, builds a telemetry event
via telemetry_schema, and appends it atomically to the log file. Designed to be called
from hook scripts and command docs.

Never writes prompts, code, diffs, or output content — only metadata. Explicitly discards
sensitive fields like last_assistant_message.
"""

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add scripts/ to path so we can import telemetry_schema
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import telemetry_schema

# Defense-in-depth constants
MAX_STDIN_BYTES = 1_048_576  # 1 MiB — hook payloads are small metadata blobs, never larger
MAX_FIELD_LEN = 4096  # defense-in-depth cap on any single pass-through metadata field


def _bounded(value, max_len=MAX_FIELD_LEN):
    """Truncate a string value to max_len chars; pass through non-strings unchanged."""
    if isinstance(value, str) and len(value) > max_len:
        return value[:max_len]
    return value


def read_stdin_json():
    """Read and parse JSON from stdin. Returns dict, or None on error (with message to stderr)."""
    try:
        raw = sys.stdin.buffer.read(MAX_STDIN_BYTES)
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Error: could not parse JSON from stdin: {e}", file=sys.stderr)
        return None
    except EOFError:
        print("Error: expected JSON on stdin, got EOF", file=sys.stderr)
        return None


def cmd_session_begin(args):
    """Record a session.begin event from a SessionStart hook payload on stdin."""
    payload = read_stdin_json()
    if payload is None:
        sys.exit(1)

    session_id = _bounded(payload.get("session_id", telemetry_schema.UNKNOWN))
    cwd = _bounded(payload.get("cwd", telemetry_schema.UNKNOWN))
    repo = os.path.basename(cwd.rstrip("/")) if cwd != telemetry_schema.UNKNOWN else telemetry_schema.UNKNOWN
    timestamp = datetime.now(timezone.utc).isoformat()

    event = telemetry_schema.build_event(
        "session.begin",
        session_id=session_id,
        timestamp=timestamp,
        cwd=cwd,
        repo=repo,
    )
    telemetry_schema.append_event(args.log, event)
    print("session.begin recorded", file=sys.stderr)


def cmd_session_end(args):
    """Record a session.end event from a SessionEnd hook payload on stdin."""
    payload = read_stdin_json()
    if payload is None:
        sys.exit(1)

    session_id = _bounded(payload.get("session_id", telemetry_schema.UNKNOWN))
    cwd = _bounded(payload.get("cwd", telemetry_schema.UNKNOWN))
    repo = os.path.basename(cwd.rstrip("/")) if cwd != telemetry_schema.UNKNOWN else telemetry_schema.UNKNOWN
    timestamp = datetime.now(timezone.utc).isoformat()

    event = telemetry_schema.build_event(
        "session.end",
        session_id=session_id,
        timestamp=timestamp,
        cwd=cwd,
        repo=repo,
        outcome=telemetry_schema.outcome_success(),
    )
    telemetry_schema.append_event(args.log, event)
    print("session.end recorded", file=sys.stderr)


def cmd_agent_begin(args):
    """Record an agent.begin event from a SubagentStart hook payload on stdin."""
    payload = read_stdin_json()
    if payload is None:
        sys.exit(1)

    session_id = _bounded(payload.get("session_id", telemetry_schema.UNKNOWN))
    agent_id = _bounded(payload.get("agent_id", telemetry_schema.UNKNOWN))
    agent_type = _bounded(payload.get("agent_type", telemetry_schema.UNKNOWN))
    cwd = _bounded(payload.get("cwd", telemetry_schema.UNKNOWN))
    repo = os.path.basename(cwd.rstrip("/")) if cwd != telemetry_schema.UNKNOWN else telemetry_schema.UNKNOWN
    timestamp = datetime.now(timezone.utc).isoformat()

    event = telemetry_schema.build_event(
        "agent.begin",
        session_id=session_id,
        timestamp=timestamp,
        agent_id=agent_id,
        agent_type=agent_type,
        cwd=cwd,
        repo=repo,
    )
    telemetry_schema.append_event(args.log, event)
    print("agent.begin recorded", file=sys.stderr)


def cmd_agent_end(args):
    """Record an agent.end event from a SubagentStop hook payload on stdin.

    Explicitly discards last_assistant_message, background_tasks, and session_crons
    (never written to telemetry). SubagentStop hook carries no failure signal, so we
    record success as the default outcome.
    """
    payload = read_stdin_json()
    if payload is None:
        sys.exit(1)

    # Explicitly ignore sensitive fields
    _ = payload.get("last_assistant_message")  # never used
    _ = payload.get("background_tasks")        # never used
    _ = payload.get("session_crons")           # never used

    session_id = _bounded(payload.get("session_id", telemetry_schema.UNKNOWN))
    agent_id = _bounded(payload.get("agent_id", telemetry_schema.UNKNOWN))
    agent_type = _bounded(payload.get("agent_type", telemetry_schema.UNKNOWN))
    timestamp = datetime.now(timezone.utc).isoformat()

    event = telemetry_schema.build_event(
        "agent.end",
        session_id=session_id,
        timestamp=timestamp,
        agent_id=agent_id,
        agent_type=agent_type,
        outcome=telemetry_schema.outcome_success(),
    )
    telemetry_schema.append_event(args.log, event)
    print("agent.end recorded", file=sys.stderr)


def cmd_command_begin(args):
    """Record a command.begin event. Prints the command_id to stdout for callers to capture.

    Also opportunistically prunes stale state files (older than 24h) as a side effect.
    """
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", telemetry_schema.UNKNOWN)
    command_id = uuid.uuid4().hex
    cwd = os.getcwd()
    repo = os.path.basename(cwd.rstrip("/"))
    timestamp = datetime.now(timezone.utc).isoformat()

    # Opportunistic cleanup of stale state files (24h cutoff)
    try:
        telemetry_schema.prune_stale_state(args.state_dir)
    except (OSError, PermissionError) as e:
        print(f"telemetry: state access failed: {e}", file=sys.stderr)

    # Write state file if session_id is known
    if session_id != telemetry_schema.UNKNOWN:
        try:
            telemetry_schema.init_command_state(
                telemetry_schema.state_path(session_id, args.state_dir),
                command_id,
                args.command,
            )
        except (OSError, PermissionError) as e:
            print(f"telemetry: state access failed: {e}", file=sys.stderr)

    event = telemetry_schema.build_event(
        "command.begin",
        session_id=session_id,
        timestamp=timestamp,
        command_id=command_id,
        command=args.command,
        cwd=cwd,
        repo=repo,
    )
    telemetry_schema.append_event(args.log, event)
    # Print bare command_id to stdout so callers can capture it
    print(command_id)


def cmd_command_end(args):
    """Record a command.end event. Outcome is required; --command-id is optional (resolved from state).

    Falls back to `telemetry_schema.UNKNOWN` if state is also unavailable.
    """
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", telemetry_schema.UNKNOWN)
    timestamp = datetime.now(timezone.utc).isoformat()

    if args.outcome == "success":
        outcome = telemetry_schema.outcome_success()
    elif args.outcome == "failure":
        if not args.failure_class:
            print("Error: --failure-class is required when --outcome is failure", file=sys.stderr)
            sys.exit(1)
        outcome = telemetry_schema.outcome_failure(args.failure_class)
    elif args.outcome == "interrupted":
        outcome = telemetry_schema.outcome_interrupted()
    else:
        print(f"Error: invalid --outcome {args.outcome}", file=sys.stderr)
        sys.exit(1)

    command_id = telemetry_schema.UNKNOWN
    state_mismatch = None
    if session_id != telemetry_schema.UNKNOWN:
        try:
            command_id, state_mismatch, _ = telemetry_schema.resolve_and_clear_command_state(
                telemetry_schema.state_path(session_id, args.state_dir), args.command_id, args.command
            )
        except (OSError, PermissionError) as e:
            print(f"telemetry: state access failed: {e}", file=sys.stderr)
    if args.command_id:
        command_id = args.command_id
    if not command_id:
        command_id = telemetry_schema.UNKNOWN

    event = telemetry_schema.build_event(
        "command.end",
        session_id=session_id,
        timestamp=timestamp,
        command_id=command_id,
        command=args.command,
        outcome=outcome,
        state_mismatch=state_mismatch,
    )
    telemetry_schema.append_event(args.log, event)

    print("command.end recorded", file=sys.stderr)


def cmd_stage_begin(args):
    """Record a stage.begin event. Prints the stage_id to stdout for callers to capture.

    --command-id is optional; resolved from state file if not explicitly provided.
    """
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", telemetry_schema.UNKNOWN)
    stage_id = uuid.uuid4().hex
    timestamp = datetime.now(timezone.utc).isoformat()

    command_id = telemetry_schema.UNKNOWN
    if session_id != telemetry_schema.UNKNOWN:
        try:
            command_id = telemetry_schema.resolve_and_set_stage_state(
                telemetry_schema.state_path(session_id, args.state_dir), args.command_id, stage_id, args.stage
            )
        except (OSError, PermissionError) as e:
            print(f"telemetry: state access failed: {e}", file=sys.stderr)
    if not command_id:
        command_id = telemetry_schema.UNKNOWN

    event = telemetry_schema.build_event(
        "stage.begin",
        session_id=session_id,
        timestamp=timestamp,
        stage_id=stage_id,
        stage=args.stage,
        command_id=command_id,
    )
    telemetry_schema.append_event(args.log, event)

    # Print bare stage_id to stdout so callers can capture it
    print(stage_id)


def cmd_stage_end(args):
    """Record a stage.end event. Outcome and stage are required; IDs are optional (resolved from state).

    Falls back to `telemetry_schema.UNKNOWN` if state is also unavailable.
    """
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", telemetry_schema.UNKNOWN)
    timestamp = datetime.now(timezone.utc).isoformat()

    if args.outcome == "success":
        outcome = telemetry_schema.outcome_success()
    elif args.outcome == "failure":
        if not args.failure_class:
            print("Error: --failure-class is required when --outcome is failure", file=sys.stderr)
            sys.exit(1)
        outcome = telemetry_schema.outcome_failure(args.failure_class)
    elif args.outcome == "interrupted":
        outcome = telemetry_schema.outcome_interrupted()
    else:
        print(f"Error: invalid --outcome {args.outcome}", file=sys.stderr)
        sys.exit(1)

    command_id = telemetry_schema.UNKNOWN
    stage_id = telemetry_schema.UNKNOWN
    state_mismatch = None
    if session_id != telemetry_schema.UNKNOWN:
        try:
            command_id, stage_id, state_mismatch = telemetry_schema.resolve_and_clear_stage_state(
                telemetry_schema.state_path(session_id, args.state_dir), args.command_id, args.stage_id, args.stage
            )
        except (OSError, PermissionError) as e:
            print(f"telemetry: state access failed: {e}", file=sys.stderr)
    if not command_id:
        command_id = telemetry_schema.UNKNOWN
    if not stage_id:
        stage_id = telemetry_schema.UNKNOWN

    event = telemetry_schema.build_event(
        "stage.end",
        session_id=session_id,
        timestamp=timestamp,
        stage_id=stage_id,
        stage=args.stage,
        command_id=command_id,
        outcome=outcome,
        state_mismatch=state_mismatch,
    )
    telemetry_schema.append_event(args.log, event)

    print("stage.end recorded", file=sys.stderr)


def cmd_diagnose(args):
    """Read the log file, reconcile begin/end pairs, and report match rate and data quality.

    Stages/agents with an "unknown" id are excluded from match-rate accounting, and the
    report includes a per-stage-name match-rate breakdown alongside the overall rate.
    """
    log_path = Path(args.log)

    # Read all events from log
    events = []
    if log_path.exists():
        try:
            with open(log_path) as f:
                for line_no, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                        events.append(event)
                    except json.JSONDecodeError:
                        print(f"Warning: skipped unparseable line {line_no} in {log_path}", file=sys.stderr)
        except OSError as e:
            print(f"Error: could not read {log_path}: {e}", file=sys.stderr)
            sys.exit(1)

    # Filter by window (last N days)
    cutoff_timestamp = datetime.now(timezone.utc) - timedelta(days=args.window_days)

    filtered_events = []
    for event in events:
        ts_str = event.get("timestamp", "")
        try:
            ts_normalized = ts_str.replace("Z", "+00:00") if isinstance(ts_str, str) else ts_str
            ts = datetime.fromisoformat(ts_normalized)
            if ts >= cutoff_timestamp:
                filtered_events.append(event)
        except (ValueError, TypeError):
            # Skip events with unparseable timestamps
            pass

    # Reconcile begin/end pairs by type and correlating id
    sessions = {}  # session_id -> (begin, end or None)
    commands = {}  # command_id -> (begin, end or None)
    stages = {}    # stage_id -> (begin, end or None)
    agents = {}    # agent_id -> (begin, end or None)

    for event in filtered_events:
        event_type = event.get("event_type", "")

        if event_type == "session.begin":
            sid = event.get("session_id")
            if sid and sid != telemetry_schema.UNKNOWN:
                sessions[sid] = (event, None)
        elif event_type == "session.end":
            sid = event.get("session_id")
            if sid and sid != telemetry_schema.UNKNOWN and sid in sessions:
                sessions[sid] = (sessions[sid][0], event)

        elif event_type == "command.begin":
            cid = event.get("command_id")
            if cid and cid != telemetry_schema.UNKNOWN:
                commands[cid] = (event, None)
        elif event_type == "command.end":
            cid = event.get("command_id")
            if cid and cid != telemetry_schema.UNKNOWN and cid in commands:
                commands[cid] = (commands[cid][0], event)

        elif event_type == "stage.begin":
            sid = event.get("stage_id")
            if sid and sid != telemetry_schema.UNKNOWN:
                stages[sid] = (event, None)
        elif event_type == "stage.end":
            sid = event.get("stage_id")
            if sid and sid != telemetry_schema.UNKNOWN and sid in stages:
                stages[sid] = (stages[sid][0], event)

        elif event_type == "agent.begin":
            aid = event.get("agent_id")
            if aid and aid != telemetry_schema.UNKNOWN:
                agents[aid] = (event, None)
        elif event_type == "agent.end":
            aid = event.get("agent_id")
            if aid and aid != telemetry_schema.UNKNOWN and aid in agents:
                agents[aid] = (agents[aid][0], event)

    # Compute match rates
    all_begin_pairs = [
        (sessions, "session"),
        (commands, "command"),
        (stages, "stage"),
        (agents, "agent"),
    ]

    total_begins = 0
    total_matched = 0
    for mapping, _ in all_begin_pairs:
        for begin, end in mapping.values():
            if begin:
                total_begins += 1
                if end:
                    total_matched += 1

    match_rate = 1.0 if total_begins == 0 else total_matched / total_begins

    # Compute unknown_pct for token fields
    total_token_fields = 0
    unknown_token_fields = 0
    for event in filtered_events:
        tokens = event.get("tokens")
        if tokens and isinstance(tokens, dict):
            for value in tokens.values():
                total_token_fields += 1
                if value == telemetry_schema.UNKNOWN:
                    unknown_token_fields += 1

    unknown_pct = 0.0 if total_token_fields == 0 else unknown_token_fields / total_token_fields

    # Report
    print(f"Match rate: {match_rate:.1%} ({total_matched}/{total_begins} begin/end pairs matched)")
    print(f"Unknown token fields: {unknown_pct:.1%}")

    # Per-stage match rate breakdown
    stage_name_totals = {}  # stage_name -> [begins, matched]
    for stage_id, (begin, end) in stages.items():
        if not begin:
            continue
        stage_name = begin.get("stage", telemetry_schema.UNKNOWN)
        if stage_name == telemetry_schema.UNKNOWN:
            continue
        if stage_name not in stage_name_totals:
            stage_name_totals[stage_name] = [0, 0]
        stage_name_totals[stage_name][0] += 1
        if end:
            stage_name_totals[stage_name][1] += 1

    if stage_name_totals:
        print("Per-stage match rate:")
        for stage_name in sorted(stage_name_totals):
            begins, matched = stage_name_totals[stage_name]
            rate = 1.0 if begins == 0 else matched / begins
            print(f"  {stage_name}: {rate:.1%} ({matched}/{begins})")

    # Check thresholds
    passes = match_rate >= 0.95 and unknown_pct < 0.15
    if passes:
        print("PASS: All thresholds met")
        sys.exit(0)
    else:
        print("FAIL: One or more thresholds not met")
        if match_rate < 0.95:
            print(f"  - Match rate {match_rate:.1%} < 95%")
        if unknown_pct >= 0.15:
            print(f"  - Unknown token fields {unknown_pct:.1%} >= 15%")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Record telemetry events to the local log.",
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=telemetry_schema.default_log_path(),
        help="Path to telemetry log file (default: ~/.claude/telemetry/events.jsonl)",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=telemetry_schema.default_state_dir(),
        help="Path to state directory for session-scoped IDs (default: ~/.claude/telemetry/state)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True, help="Subcommand")

    # session-begin
    sp_session_begin = subparsers.add_parser("session-begin", help="Record a session.begin event")
    sp_session_begin.set_defaults(func=cmd_session_begin)

    # session-end
    sp_session_end = subparsers.add_parser("session-end", help="Record a session.end event")
    sp_session_end.set_defaults(func=cmd_session_end)

    # agent-begin
    sp_agent_begin = subparsers.add_parser("agent-begin", help="Record an agent.begin event")
    sp_agent_begin.set_defaults(func=cmd_agent_begin)

    # agent-end
    sp_agent_end = subparsers.add_parser("agent-end", help="Record an agent.end event")
    sp_agent_end.set_defaults(func=cmd_agent_end)

    # command-begin
    sp_command_begin = subparsers.add_parser("command-begin", help="Record a command.begin event")
    sp_command_begin.add_argument("--command", required=True, help="Command name")
    sp_command_begin.set_defaults(func=cmd_command_begin)

    # command-end
    sp_command_end = subparsers.add_parser("command-end", help="Record a command.end event")
    sp_command_end.add_argument("--command-id", required=False, default=None, help="Command ID (from command-begin); optional, resolved from state if omitted")
    sp_command_end.add_argument("--command", required=True, help="Command name")
    sp_command_end.add_argument(
        "--outcome",
        required=True,
        choices=["success", "failure", "interrupted"],
        help="Outcome status",
    )
    sp_command_end.add_argument(
        "--failure-class",
        help="Failure class (required if outcome=failure)",
    )
    sp_command_end.set_defaults(func=cmd_command_end)

    # stage-begin
    sp_stage_begin = subparsers.add_parser("stage-begin", help="Record a stage.begin event")
    sp_stage_begin.add_argument("--command-id", required=False, default=None, help="Command ID; optional, resolved from state if omitted")
    sp_stage_begin.add_argument("--stage", required=True, help="Stage name")
    sp_stage_begin.set_defaults(func=cmd_stage_begin)

    # stage-end
    sp_stage_end = subparsers.add_parser("stage-end", help="Record a stage.end event")
    sp_stage_end.add_argument("--stage-id", required=False, default=None, help="Stage ID (from stage-begin); optional, resolved from state if omitted")
    sp_stage_end.add_argument("--command-id", required=False, default=None, help="Command ID; optional, resolved from state if omitted")
    sp_stage_end.add_argument("--stage", required=True, help="Stage name")
    sp_stage_end.add_argument(
        "--outcome",
        required=True,
        choices=["success", "failure", "interrupted"],
        help="Outcome status",
    )
    sp_stage_end.add_argument(
        "--failure-class",
        help="Failure class (required if outcome=failure)",
    )
    sp_stage_end.set_defaults(func=cmd_stage_end)

    # diagnose
    sp_diagnose = subparsers.add_parser(
        "diagnose",
        help="Diagnose telemetry log: reconcile pairs and check data quality",
    )
    sp_diagnose.add_argument(
        "--window-days",
        type=int,
        default=30,
        help="Report on events from the last N days (default: 30)",
    )
    sp_diagnose.set_defaults(func=cmd_diagnose)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
