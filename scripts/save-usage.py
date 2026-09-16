#!/usr/bin/env python3
"""
Deterministic parser for Claude Code's /usage (or /cost) panel output.

Takes a pasted usage-panel block on stdin, extracts real $ cost, API/wall duration,
code-change counts, and per-model token/cost breakdown via regex, and appends one JSON
line to ~/.claude/telemetry/usage-log.jsonl. Purely mechanical — no LLM judgment, no
network calls. Opt-in and human-initiated, same as scripts/reviewer-yield.py; never read
by any command's own logic.

Input format (stdin): an optional first line is treated as a label for this run (e.g.
"expert-plan-v3-effort2-issue184"); everything after is the pasted usage-panel text.
If the first line doesn't look like a label (contains no letters, or matches a usage-panel
field), no label is used and a timestamp-based one is generated instead.
"""

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

USAGE_LOG_PATH = Path.home() / ".claude" / "telemetry" / "usage-log.jsonl"

# Lines that mark this as real usage-panel content, not a label
PANEL_MARKERS = ("total cost", "total duration", "usage by model", "total code changes")


def parse_count(raw: str) -> float:
    """Convert '4.2k' -> 4200, '2.8m' -> 2_800_000, '925' -> 925."""
    raw = raw.strip()
    if raw.endswith("k"):
        return float(raw[:-1]) * 1_000
    if raw.endswith("m"):
        return float(raw[:-1]) * 1_000_000
    return float(raw)


def parse_duration(raw: str) -> int:
    """Convert '1h 19m 46s' / '3m 27s' / '46s' -> total seconds."""
    total = 0
    for value, unit in re.findall(r"(\d+)\s*([hms])", raw):
        value = int(value)
        if unit == "h":
            total += value * 3600
        elif unit == "m":
            total += value * 60
        else:
            total += value
    return total


def looks_like_label(first_line: str) -> bool:
    stripped = first_line.strip().lower()
    if not stripped:
        return False
    return not any(marker in stripped for marker in PANEL_MARKERS)


def parse_usage_block(text: str) -> dict:
    record = {}

    cost_match = re.search(r"Total cost:\s*\$?([0-9.]+)", text, re.IGNORECASE)
    if not cost_match:
        raise ValueError("no 'Total cost:' line found — doesn't look like a usage panel paste")
    record["total_cost_usd"] = float(cost_match.group(1))

    api_match = re.search(r"Total duration \(API\):\s*([0-9hms ]+)", text, re.IGNORECASE)
    record["duration_api_seconds"] = parse_duration(api_match.group(1)) if api_match else None

    wall_match = re.search(r"Total duration \(wall\):\s*([0-9hms ]+)", text, re.IGNORECASE)
    record["duration_wall_seconds"] = parse_duration(wall_match.group(1)) if wall_match else None

    code_match = re.search(
        r"Total code changes:\s*(\d+) lines added,\s*(\d+) lines removed", text, re.IGNORECASE
    )
    if code_match:
        record["code_lines_added"] = int(code_match.group(1))
        record["code_lines_removed"] = int(code_match.group(2))
    else:
        record["code_lines_added"] = None
        record["code_lines_removed"] = None

    model_pattern = re.compile(
        r"([\w.\-]+):\s*"
        r"([\d.]+k?m?) input,\s*"
        r"([\d.]+k?m?) output,\s*"
        r"([\d.]+k?m?) cache read,\s*"
        r"([\d.]+k?m?) cache write\s*"
        r"\(\$([0-9.]+)\)",
        re.IGNORECASE,
    )
    models = []
    for name, inp, out, cache_read, cache_write, cost in model_pattern.findall(text):
        models.append(
            {
                "model": name,
                "input_tokens": int(parse_count(inp)),
                "output_tokens": int(parse_count(out)),
                "cache_read_tokens": int(parse_count(cache_read)),
                "cache_write_tokens": int(parse_count(cache_write)),
                "cost_usd": float(cost),
            }
        )
    record["models"] = models

    return record


def detect_repo_key() -> str:
    try:
        out = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().replace("/", "-")
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip()).name
    except Exception:
        pass
    return "unknown"


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        print("ERROR: no input on stdin — paste the /usage or /cost panel text", file=sys.stderr)
        return 1

    lines = raw.splitlines()
    label = None
    body = raw
    if lines and looks_like_label(lines[0]):
        label = lines[0].strip()
        body = "\n".join(lines[1:])

    try:
        parsed = parse_usage_block(body)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if not label:
        label = f"unlabeled-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "label": label,
        "repo_key": detect_repo_key(),
        **parsed,
    }

    USAGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with USAGE_LOG_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")

    model_summary = ", ".join(f"{m['model']} (${m['cost_usd']:.4f})" for m in parsed["models"]) or "no per-model lines parsed"
    print(f"Saved usage for '{label}': ${parsed['total_cost_usd']:.4f} total, {model_summary}")
    print(f"-> {USAGE_LOG_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
