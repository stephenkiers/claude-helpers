#!/usr/bin/env python3
"""
A fake `claude` binary for Flow v3 tests. It accepts the argv `scripts/flow3/runner.py` builds and
prints what a real `claude -p --output-format json` printed in docs/v3/probes.md. No model is called.

Point `FLOW3_CLAUDE_BIN` at this file. Drive it with a script file named by `FLOW3_FAKE_SCRIPT`:

    {"calls": [ {entry}, {entry}, ... ], "default": {entry}}

Each invocation takes the next unconsumed entry in "calls" whose optional "role" equals $FLOW3_ROLE and
whose optional "label" equals $FLOW3_LABEL (the runner sets both). Consumption is recorded in a sidecar
file `<script>.consumed` under an flock, so parallel invocations each get a different entry. When no
entry matches, "default" is used (never consumed); with no default, the fake answers with a minimal
object generated from the `--json-schema` argument (required properties only, first enum value, ""
for strings, 0 for numbers, false for booleans, [] for arrays unless minItems, recursively).

Entry fields, all optional:
    role, label     matching conditions (above)
    mode            ok (default) | max_turns | api_error | limit | empty | denied | no_output | hang | crash
    output          the structured_output to return (default: generated from the schema)
    cost            total_cost_usd (default 0.001)
    session_id      session id to report (default "fake-session")
    status          api_error_status for mode api_error (default 404)
    resets_at       resetsAt for mode limit (default: now + 3600)
    exit, stderr    exit code and stderr text for mode empty (default 1, "")
    denials         permission_denials list for mode denied
    seconds         how long mode hang sleeps (default 60)
    sleep           seconds to sleep before answering in any mode (default 0)
    write_files     {relative path: content} written under the cwd before answering
    run             a shell command run in the cwd before answering

Exit codes follow probes.md: 0 for ok, denied and no_output; 1 for max_turns, api_error, limit and
empty (unless "exit" is given); 143 for crash (empty stdout).

When `FLOW3_FAKE_LOG` is set, one JSON line per invocation is appended:
{argv, role, label, cwd, model, resume, prompt, start, end}. A hang entry logs before it sleeps (no "end").
"""

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def _arg(argv: List[str], flag: str) -> Optional[str]:
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def generate(schema: Any) -> Any:
    """A minimal value conforming to the schema subset the driver uses."""
    if not isinstance(schema, dict):
        return None
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object":
        props = schema.get("properties", {})
        return {k: generate(props.get(k, {})) for k in schema.get("required", [])}
    if kind == "array":
        n = int(schema.get("minItems", 0))
        return [generate(schema.get("items", {})) for _ in range(n)]
    if kind == "string":
        return "x" * int(schema.get("minLength", 0))
    if kind in ("number", "integer"):
        return 0
    if kind == "boolean":
        return False
    return None


def take_entry(script: Path, role: str, label: str) -> Optional[Dict[str, Any]]:
    data = json.loads(script.read_text())
    side = script.with_name(script.name + ".consumed")
    with open(side, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        raw = fh.read().strip()
        used = set(json.loads(raw)) if raw else set()
        chosen: Optional[Dict[str, Any]] = None
        for i, entry in enumerate(data.get("calls", [])):
            if i in used:
                continue
            if entry.get("role") not in (None, role) or entry.get("label") not in (None, label):
                continue
            used.add(i)
            chosen = entry
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps(sorted(used)))
            break
        fcntl.flock(fh, fcntl.LOCK_UN)
    if chosen is not None:
        return chosen
    default = data.get("default")
    return default if isinstance(default, dict) else None


def main() -> int:
    argv = sys.argv[1:]
    role = os.environ.get("FLOW3_ROLE", "")
    label = os.environ.get("FLOW3_LABEL", "")
    prompt = argv[-1] if argv else ""
    schema_arg = _arg(argv, "--json-schema")
    schema = json.loads(schema_arg) if schema_arg else None
    script_env = os.environ.get("FLOW3_FAKE_SCRIPT")
    entry: Dict[str, Any] = {}
    if script_env:
        entry = take_entry(Path(script_env), role, label) or {}
    mode = entry.get("mode", "ok")
    start = time.time()
    log_path = os.environ.get("FLOW3_FAKE_LOG")

    def log(end: Optional[float]) -> None:
        if not log_path:
            return
        line: Dict[str, Any] = {"argv": argv, "role": role, "label": label, "cwd": os.getcwd(),
                                "model": _arg(argv, "--model"), "resume": _arg(argv, "--resume"),
                                "prompt": prompt, "start": start}
        if end is not None:
            line["end"] = end
        with open(log_path, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.write(json.dumps(line) + "\n")
            fcntl.flock(fh, fcntl.LOCK_UN)

    if mode == "hang":
        log(None)
        time.sleep(float(entry.get("seconds", 60)))
    if entry.get("sleep"):
        time.sleep(float(entry["sleep"]))
    for rel, content in (entry.get("write_files") or {}).items():
        target = Path(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    if entry.get("run"):
        subprocess.run(entry["run"], shell=True, check=False)

    session = entry.get("session_id", "fake-session")
    cost = float(entry.get("cost", 0.001))
    output = entry["output"] if "output" in entry else generate(schema)
    result: Dict[str, Any] = {
        "type": "result", "subtype": "success", "is_error": False, "terminal_reason": "completed",
        "structured_output": output, "result": json.dumps(output), "session_id": session,
        "num_turns": 2, "total_cost_usd": cost, "duration_ms": 12, "permission_denials": [],
        "api_error_status": None, "errors": [],
    }
    allowed = {"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed_warning", "resetsAt": int(start) + 3600, "rateLimitType": "seven_day",
        "utilization": 0.5}}
    limit_event: Optional[Dict[str, Any]] = allowed
    code = 0

    if mode == "crash":
        log(time.time())
        return 143
    if mode == "empty":
        log(time.time())
        sys.stderr.write(str(entry.get("stderr", "")))
        return int(entry.get("exit", 1))
    if mode == "max_turns":
        result.update(subtype="error_max_turns", is_error=True, terminal_reason="max_turns",
                      structured_output=None, result=None,
                      errors=["Reached maximum number of turns (1)"])
        code = 1
    elif mode == "api_error":
        status = int(entry.get("status", 404))
        result.update(is_error=True, terminal_reason="api_error", api_error_status=status,
                      total_cost_usd=0, structured_output=None,
                      result="API Error: %d model not found" % status)
        code = 1
    elif mode == "limit":
        resets = int(entry.get("resets_at", int(start) + 3600))
        limit_event = {"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "resetsAt": resets, "rateLimitType": "five_hour"}}
        result.update(is_error=True, terminal_reason="api_error", api_error_status=429,
                      structured_output=None, result="usage limit reached")
        code = 1
    elif mode == "denied":
        result["permission_denials"] = entry.get("denials") or [
            {"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "rm -rf x"}}]
    elif mode == "no_output":
        result.update(structured_output=None, result="")

    events: List[Dict[str, Any]] = [{"type": "system", "subtype": "init", "session_id": session}]
    if limit_event is not None:
        events.append(limit_event)
    events.append(result)
    sys.stdout.write(json.dumps(events))
    log(time.time())
    return code


if __name__ == "__main__":
    sys.exit(main())
