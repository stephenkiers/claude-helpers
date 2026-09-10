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

    # Use shared parser to extract tokens and metadata
    try:
        parse_result = telemetry_schema.parse_transcript_tokens(transcript_path)
    except (OSError, FileNotFoundError) as e:
        print(f"Error: could not read transcript: {e}", file=sys.stderr)
        sys.exit(1)

    lines_parsed = parse_result["lines_parsed"]
    lines_skipped = parse_result["lines_skipped"]
    turns = parse_result["turns"]
    tokens = parse_result["tokens"]
    cost_state = parse_result["cost_state"]
    first_assistant_event = parse_result["first_assistant_event"]

    # Extract session_id and agent_id from args or from first event
    session_id = args.session_id if args.session_id else telemetry_schema.UNKNOWN
    agent_id = args.agent_id if args.agent_id else telemetry_schema.UNKNOWN

    # If no session_id provided, try to get it from first assistant event
    if session_id == telemetry_schema.UNKNOWN and first_assistant_event and "sessionId" in first_assistant_event:
        session_id = first_assistant_event["sessionId"]

    # Build output
    result = {
        "transcript": str(args.transcript),
        "session_id": session_id,
        "agent_id": agent_id,
        "lines_parsed": lines_parsed,
        "lines_skipped": lines_skipped,
        "turns": turns,
        "tokens": tokens,
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
