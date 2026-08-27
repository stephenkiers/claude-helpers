#!/usr/bin/env python3
"""
Read-only tool: parse Claude Code transcript JSONL files and extract usage metrics.

Does not write to the telemetry log (pure read/report tool). Emits a single JSON object
to stdout with turn counts, token usage, and cost-state if present. Tolerant of malformed
lines (skips them with a report) since transcript format can change between Claude Code versions.
"""

import argparse
import json
import sys
from pathlib import Path

# Add scripts/ to path so we can import telemetry_schema
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import telemetry_schema


def cmd_parse(args):
    """Parse a transcript JSONL file and emit metrics as JSON to stdout."""
    transcript_path = Path(args.transcript)

    if not transcript_path.exists():
        print(f"Error: transcript file not found: {transcript_path}", file=sys.stderr)
        sys.exit(1)

    lines_parsed = 0
    lines_skipped = 0
    seen_message_ids = {}  # message_id -> (lines_parsed index, event dict)
    cost_state = None
    turns = 0

    try:
        with open(transcript_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                try:
                    event = json.loads(line)
                    lines_parsed += 1

                    # Collect cost-state (only one expected per transcript)
                    if event.get("type") == "cost-state":
                        cost_state = event

                    # Deduplicate assistant lines by message.id
                    if event.get("type") == "assistant":
                        msg = event.get("message")
                        if msg and isinstance(msg, dict):
                            msg_id = msg.get("id")
                            if msg_id:
                                seen_message_ids[msg_id] = event
                except (json.JSONDecodeError, ValueError):
                    lines_skipped += 1
    except OSError as e:
        print(f"Error: could not read transcript: {e}", file=sys.stderr)
        sys.exit(1)

    # Sum tokens from deduplicated assistant events
    total_input = telemetry_schema.UNKNOWN
    total_output = telemetry_schema.UNKNOWN
    total_cache_read = telemetry_schema.UNKNOWN
    total_cache_creation = telemetry_schema.UNKNOWN

    input_sum = 0
    output_sum = 0
    cache_read_sum = 0
    cache_creation_sum = 0
    has_input = False
    has_output = False
    has_cache_read = False
    has_cache_creation = False

    for msg_id, event in seen_message_ids.items():
        msg = event.get("message", {})
        usage = msg.get("usage", {})

        if "input_tokens" in usage:
            input_sum += usage["input_tokens"]
            has_input = True
        if "output_tokens" in usage:
            output_sum += usage["output_tokens"]
            has_output = True
        if "cache_read_input_tokens" in usage:
            cache_read_sum += usage["cache_read_input_tokens"]
            has_cache_read = True
        if "cache_creation_input_tokens" in usage:
            cache_creation_sum += usage["cache_creation_input_tokens"]
            has_cache_creation = True

    if has_input:
        total_input = input_sum
    if has_output:
        total_output = output_sum
    if has_cache_read:
        total_cache_read = cache_read_sum
    if has_cache_creation:
        total_cache_creation = cache_creation_sum

    # Count turns (unique message ids)
    turns = len(seen_message_ids)

    # Extract session_id and agent_id from args or from first event
    session_id = args.session_id if args.session_id else telemetry_schema.UNKNOWN
    agent_id = args.agent_id if args.agent_id else telemetry_schema.UNKNOWN

    # If no session_id provided, try to get it from the first event
    if session_id == telemetry_schema.UNKNOWN and lines_parsed > 0:
        for event in seen_message_ids.values():
            if "sessionId" in event:
                session_id = event["sessionId"]
                break

    # Build output
    result = {
        "transcript": str(args.transcript),
        "session_id": session_id,
        "agent_id": agent_id,
        "lines_parsed": lines_parsed,
        "lines_skipped": lines_skipped,
        "turns": turns,
        "tokens": {
            "input": total_input,
            "output": total_output,
            "cache_read": total_cache_read,
            "cache_creation": total_cache_creation,
        },
        "token_confidence": "low",
    }

    if cost_state is not None:
        result["cost_state"] = cost_state

    print(json.dumps(result, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(
        description="Parse a Claude Code transcript JSONL and extract metrics.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True, help="Subcommand")

    # parse
    sp_parse = subparsers.add_parser("parse", help="Parse a transcript file")
    sp_parse.add_argument("--transcript", required=True, help="Path to transcript JSONL file")
    sp_parse.add_argument("--session-id", default=None, help="Override session ID")
    sp_parse.add_argument("--agent-id", default=None, help="Override agent ID")
    sp_parse.set_defaults(func=cmd_parse)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
