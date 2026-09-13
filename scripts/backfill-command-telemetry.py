#!/usr/bin/env python3
"""
Best-effort backfill of command/stage telemetry from a session transcript.

command.begin/end and stage.begin/end events are only emitted by literal `run-metrics.py`
bash calls embedded in commands/*.md bodies — nothing forces an agent to actually execute
them while following the doc's natural-language flow, so gaps happen (a command can finish
successfully while emitting zero stage events). session.*/agent.* events don't have this
problem because they're emitted by real Claude Code hooks (SessionStart/SessionEnd/
SubagentStart/SubagentStop), which the harness fires unconditionally.

This script runs as a second SessionEnd hook, alongside run-metrics.py session-end (not
instead of it), and recovers two independent signals from the transcript, each tagged
source="derived-from-transcript" so a reader can always tell a live-emitted event from a
backfilled one:

1. Command identity: Claude Code records every slash-command invocation in the transcript
   as a user turn containing `<command-name>/foo</command-name>` (+ `<command-args>`).
   If a session logged zero command.* events at all (a full telemetry blackout — the
   observed failure mode), this recovers *which* command doc(s) were actually invoked,
   filtered to slash commands with a real backing file in ~/.claude/commands/ so /clear,
   /compact, etc. don't add noise. This does not know whether the command finished or
   what stages it reached — it is strictly weaker than live stage telemetry, just better
   than nothing.

2. Stage/command markers whose Bash tool_use call happened but never made it into the log
   (e.g. the invocation's own `2>/dev/null || true` masked a real failure to append). Only
   backfills markers not already present for this session, so it never duplicates a live
   event.

Read-only with respect to the transcript; never re-executes anything, never touches code,
never calls the model. All failures are swallowed — this must never block or fail a session.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import telemetry_schema

MAX_STDIN_BYTES = 1_048_576

COMMAND_INVOCATION_RE = re.compile(r"<command-name>/([^<\s]+)</command-name>")

MARKER_RE = re.compile(
    r"run-metrics\.py\s+(command-begin|command-end|stage-begin|stage-end)\b([^\n]*)"
)
OPT_RE = re.compile(r"--(command-id|stage-id|command|stage)[= ]([^\s\"']+)")

SUBCOMMAND_TO_EVENT_TYPE = {
    "command-begin": "command.begin",
    "command-end": "command.end",
    "stage-begin": "stage.begin",
    "stage-end": "stage.end",
}

DEFAULT_COMMANDS_DIR = Path.home() / ".claude" / "commands"


def read_stdin_json():
    try:
        raw = sys.stdin.buffer.read(MAX_STDIN_BYTES)
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError, EOFError):
        return {}


def iter_transcript_events(transcript_path):
    """Yield parsed JSON objects from a transcript JSONL, skipping unparseable lines."""
    with open(transcript_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue


def find_invoked_commands(transcript_path, commands_dir):
    """Return an ordered list of (command_name, args, timestamp) for slash commands
    invoked in this transcript that have a real backing file in commands_dir.

    command_name excludes the leading slash. Deduplicates consecutive repeats but keeps
    distinct invocations (e.g. the same command run twice at different times).
    """
    found = []
    for event in iter_transcript_events(transcript_path):
        if event.get("type") != "user":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        text = content if isinstance(content, str) else None
        if text is None and isinstance(content, list):
            text = "\n".join(
                block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"
            )
        if not text:
            continue
        for m in COMMAND_INVOCATION_RE.finditer(text):
            name = m.group(1)
            if not (commands_dir / f"{name}.md").is_file():
                continue
            args_match = re.search(r"<command-args>([^<]*)</command-args>", text)
            args = args_match.group(1).strip() if args_match else ""
            found.append((name, args, event.get("timestamp")))
    return found


def find_run_metrics_markers(transcript_path):
    """Return an ordered list of {subcommand, options, ts} for run-metrics.py CLI
    invocations found inside Bash tool_use calls in the transcript."""
    markers = []
    for event in iter_transcript_events(transcript_path):
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("name") not in ("Bash", "bash"):
                continue
            command_text = (block.get("input") or {}).get("command")
            if not isinstance(command_text, str):
                continue
            for m in MARKER_RE.finditer(command_text):
                options = dict(OPT_RE.findall(m.group(2)))
                markers.append({"subcommand": m.group(1), "options": options, "ts": event.get("timestamp")})
    return markers


def existing_session_events(log_path, session_id):
    """Return the set of event_type values already logged for this session, and a
    finer-grained set of (event_type, name) keys where name is the command/stage value."""
    event_types = set()
    named_keys = set()
    try:
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if e.get("session_id") != session_id:
                    continue
                et = e.get("event_type")
                if et is None:
                    continue
                event_types.add(et)
                if et in ("command.begin", "command.end"):
                    named_keys.add((et, e.get("command")))
                elif et in ("stage.begin", "stage.end"):
                    named_keys.add((et, e.get("stage")))
    except (OSError, FileNotFoundError):
        pass
    return event_types, named_keys


def append_derived(log_path, event_type, session_id, timestamp, **kwargs):
    if not timestamp:
        timestamp = datetime.now(timezone.utc).isoformat()
    event = telemetry_schema.build_event(event_type, session_id=session_id, timestamp=timestamp, **kwargs)
    event["source"] = "derived-from-transcript"
    try:
        telemetry_schema.append_event(log_path, event)
    except ValueError:
        pass


def main():
    payload = read_stdin_json()
    session_id = payload.get("session_id", telemetry_schema.UNKNOWN)
    transcript_path = payload.get("transcript_path")
    if not transcript_path or session_id == telemetry_schema.UNKNOWN:
        return

    transcript_file = Path(transcript_path)
    if not transcript_file.is_file():
        return

    log_path = telemetry_schema.default_log_path()
    event_types, named_keys = existing_session_events(log_path, session_id)

    # Signal 1: command identity, only when live telemetry has zero command.* events at all
    # (the observed full-blackout failure mode) — otherwise live data already covers this.
    if "command.begin" not in event_types and "command.end" not in event_types:
        for name, args, ts in find_invoked_commands(transcript_file, DEFAULT_COMMANDS_DIR):
            key = ("command.begin", name)
            if key in named_keys:
                continue
            named_keys.add(key)
            append_derived(
                log_path,
                "command.begin",
                session_id,
                ts,
                command=name,
                cwd=payload.get("cwd"),
            )

    # Signal 2: stage/command markers whose Bash call ran but never made it into the log.
    for marker in find_run_metrics_markers(transcript_file):
        event_type = SUBCOMMAND_TO_EVENT_TYPE.get(marker["subcommand"])
        if event_type is None:
            continue
        opts = marker["options"]
        name = opts.get("command") if event_type.startswith("command.") else opts.get("stage")
        key = (event_type, name)
        if key in named_keys:
            continue
        named_keys.add(key)

        kwargs = {}
        if event_type.startswith("command."):
            kwargs["command"] = name
            if "command-id" in opts:
                kwargs["command_id"] = opts["command-id"]
        else:
            kwargs["stage"] = name
            if "stage-id" in opts:
                kwargs["stage_id"] = opts["stage-id"]
        if event_type.endswith(".end"):
            kwargs["outcome"] = telemetry_schema.outcome_success()

        append_derived(log_path, event_type, session_id, marker["ts"], **kwargs)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never let a parsing/schema surprise fail the SessionEnd hook.
        pass
