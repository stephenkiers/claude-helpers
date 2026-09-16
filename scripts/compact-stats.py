#!/usr/bin/env python3
"""
Compaction cost-benefit + daily session/usage report.

Claude Code already writes a `compactMetadata` block (trigger, preTokens, postTokens,
durationMs) into every session transcript whenever a compaction happens, and a `usage`
block (input/output/cache tokens, model) onto every assistant turn -- no hook or telemetry
wiring is needed to capture either. This script scans `~/.claude/projects/*/*.jsonl` once
and aggregates both, plus a per-day distinct-session count.

Three things are tracked, and they are deliberately kept as separate numbers rather than
blended into one "savings" figure:
  - wall-clock tax: durationMs summed per compaction. This blocks the session while it runs.
  - context reduction: preTokens - postTokens per compaction. This is a proxy for tokens
    avoided on *subsequent* turns (fewer tokens re-sent from context), not a dollar figure --
    turning it into real cost savings would require knowing how many follow-up turns happened
    before the next compaction, which this script does not attempt to model.
  - raw token usage per day (input/output/cache_read/cache_creation, by model). This is the
    actual token spend, and is the number to watch to see if a threshold change is making
    sessions cheaper -- but it is NOT converted to a dollar figure here, since that requires
    a per-model pricing table this script has no authoritative source for. Supply one via
    --pricing if you want a $ column.

A session file's own top-level events (isSidechain: false) and any Task-tool subagent
turns embedded in it (isSidechain: true) are counted together per file/session -- this
environment's transcripts did not show a reliable field to separately tag forked background
agents from top-level interactive sessions, so "sessions/day" below means distinct session
files touched that day, not a clean interactive-vs-subagent split.

Read-only, opt-in, run manually. Never wired into hooks or expert-review.
"""

import argparse
import glob
import json
import os
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional, TypedDict


class CompactEvent(TypedDict):
    timestamp: str
    trigger: str
    pre_tokens: int
    post_tokens: int
    duration_ms: int
    cwd: Optional[str]
    file: str


class UsageEvent(TypedDict):
    timestamp: str
    session_id: Optional[str]
    model: Optional[str]
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int


class ScanResult(TypedDict):
    compact_events: list
    usage_events: list


def scan(projects_dir: str) -> ScanResult:
    compact_events: list = []
    usage_events: list = []
    for path in glob.glob(os.path.join(projects_dir, "*", "*.jsonl")):
        try:
            with open(path, errors="ignore") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    m = d.get("compactMetadata")
                    if m and m.get("preTokens") is not None and m.get("postTokens") is not None:
                        compact_events.append(CompactEvent(
                            timestamp=d.get("timestamp", ""),
                            trigger=m.get("trigger", "unknown"),
                            pre_tokens=m["preTokens"],
                            post_tokens=m["postTokens"],
                            duration_ms=m.get("durationMs", 0),
                            cwd=d.get("cwd"),
                            file=os.path.basename(path),
                        ))

                    if d.get("type") == "assistant":
                        usage = d.get("message", {}).get("usage")
                        if usage:
                            usage_events.append(UsageEvent(
                                timestamp=d.get("timestamp", ""),
                                session_id=d.get("sessionId"),
                                model=d.get("message", {}).get("model"),
                                input_tokens=usage.get("input_tokens", 0),
                                output_tokens=usage.get("output_tokens", 0),
                                cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                                cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
                            ))
        except OSError as e:
            print(f"warning: could not read {path}: {e}", file=sys.stderr)
    return ScanResult(compact_events=compact_events, usage_events=usage_events)


def parse_since(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def fmt_table(rows: list, headers: list) -> str:
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) if rows else len(str(h))
              for i, h in enumerate(headers)]
    out = ["  ".join(h.ljust(w) for h, w in zip(headers, widths))]
    out.append("  ".join("-" * w for w in widths))
    for r in rows:
        out.append("  ".join(str(c).ljust(w) for c, w in zip(r, widths)))
    return "\n".join(out)


def summarize(events: list, label: str) -> None:
    if not events:
        print(f"{label}: no events")
        return
    pres = [e["pre_tokens"] for e in events]
    dropped = [e["pre_tokens"] - e["post_tokens"] for e in events]
    durs = [e["duration_ms"] for e in events]
    total_wallclock_s = sum(durs) / 1000
    total_dropped = sum(dropped)
    print(f"{label} (n={len(events)})")
    print(f"  preTokens:   median={st.median(pres):,.0f}  mean={st.mean(pres):,.0f}  "
          f"min={min(pres):,}  max={max(pres):,}")
    print(f"  dropped:     median={st.median(dropped):,.0f}  mean={st.mean(dropped):,.0f}  "
          f"total={total_dropped:,}")
    print(f"  wall-clock:  median={st.median(durs) / 1000:.1f}s  mean={st.mean(durs) / 1000:.1f}s  "
          f"total={total_wallclock_s:,.1f}s ({total_wallclock_s / 60:.1f}min)")
    if total_wallclock_s > 0:
        print(f"  efficiency:  {total_dropped / total_wallclock_s:,.0f} tokens dropped per second spent compacting")


def daily_breakdown(compact_events: list, usage_events: list, pricing: Optional[dict]) -> None:
    by_day = defaultdict(list)
    for e in compact_events:
        day = e["timestamp"][:10] if e["timestamp"] else "unknown"
        by_day[day].append(e)

    sessions_by_day: dict = defaultdict(set)
    usage_by_day: dict = defaultdict(lambda: defaultdict(int))
    for u in usage_events:
        day = u["timestamp"][:10] if u["timestamp"] else "unknown"
        if u["session_id"]:
            sessions_by_day[day].add(u["session_id"])
        usage_by_day[day]["input"] += u["input_tokens"]
        usage_by_day[day]["output"] += u["output_tokens"]
        usage_by_day[day]["cache_read"] += u["cache_read_tokens"]
        usage_by_day[day]["cache_creation"] += u["cache_creation_tokens"]

    all_days = sorted(set(by_day) | set(sessions_by_day) | set(usage_by_day))
    headers = ["date", "sessions", "c_auto", "c_manual", "auto_pre_median", "compact_wallclock",
               "dropped", "in_tok", "out_tok", "cache_read", "cache_write"]
    if pricing:
        headers.append("est_cost")
    rows = []
    for day in all_days:
        day_events = by_day.get(day, [])
        auto = [e for e in day_events if e["trigger"] == "auto"]
        manual = [e for e in day_events if e["trigger"] == "manual"]
        wallclock_s = sum(e["duration_ms"] for e in day_events) / 1000
        dropped = sum(e["pre_tokens"] - e["post_tokens"] for e in day_events)
        auto_pre_median = st.median([e["pre_tokens"] for e in auto]) if auto else 0
        u = usage_by_day.get(day, {})
        row = [day, len(sessions_by_day.get(day, set())), len(auto), len(manual),
               f"{auto_pre_median:,.0f}", f"{wallclock_s:.0f}s", f"{dropped:,}",
               f"{u.get('input', 0):,}", f"{u.get('output', 0):,}",
               f"{u.get('cache_read', 0):,}", f"{u.get('cache_creation', 0):,}"]
        if pricing:
            cost = (u.get("input", 0) * pricing.get("input", 0)
                    + u.get("output", 0) * pricing.get("output", 0)
                    + u.get("cache_read", 0) * pricing.get("cache_read", 0)
                    + u.get("cache_creation", 0) * pricing.get("cache_creation", 0))
            row.append(f"${cost:,.2f}")
        rows.append(row)
    print(fmt_table(rows, headers))


def usage_by_model(usage_events: list) -> None:
    by_model: dict = defaultdict(lambda: defaultdict(int))
    for u in usage_events:
        model = u["model"] or "unknown"
        by_model[model]["input"] += u["input_tokens"]
        by_model[model]["output"] += u["output_tokens"]
        by_model[model]["cache_read"] += u["cache_read_tokens"]
        by_model[model]["cache_creation"] += u["cache_creation_tokens"]
        by_model[model]["turns"] += 1
    rows = []
    for model in sorted(by_model):
        m = by_model[model]
        rows.append([model, f"{m['turns']:,}", f"{m['input']:,}", f"{m['output']:,}",
                     f"{m['cache_read']:,}", f"{m['cache_creation']:,}"])
    print(fmt_table(rows, ["model", "turns", "input", "output", "cache_read", "cache_write"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--projects-dir", default=os.path.expanduser("~/.claude/projects"),
                         help="Directory containing per-project session transcripts")
    parser.add_argument("--since", type=parse_since, default=None,
                         help="Only include events on/after this date (YYYY-MM-DD)")
    parser.add_argument("--project", default=None,
                         help="Only include events whose cwd contains this substring")
    parser.add_argument("--daily", action="store_true",
                         help="Print a per-day breakdown (sessions, compaction, token usage)")
    parser.add_argument("--by-model", action="store_true",
                         help="Print total token usage broken down by model")
    parser.add_argument("--pricing", default=None,
                         help="Path to a JSON file with {input,output,cache_read,cache_creation} "
                              "$-per-token rates, to add an est_cost column to --daily. No default "
                              "is baked in -- this script does not assume current pricing.")
    args = parser.parse_args()

    pricing = None
    if args.pricing:
        with open(args.pricing) as fh:
            pricing = json.load(fh)

    result = scan(args.projects_dir)
    compact_events = result["compact_events"]
    usage_events = result["usage_events"]

    if args.since:
        compact_events = [e for e in compact_events if e["timestamp"] and
                           datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")) >= args.since]
        usage_events = [e for e in usage_events if e["timestamp"] and
                         datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")) >= args.since]
    if args.project:
        compact_events = [e for e in compact_events if e["cwd"] and args.project in e["cwd"]]
        # usage events don't carry cwd; project-scoping usage would require joining on
        # session_id against a compact/cwd-bearing event from the same file, which isn't
        # reliable when a session has zero compactions -- so --project only scopes compaction
        # stats, not usage/session stats. Flagged here rather than silently mis-scoping usage.

    if not compact_events and not usage_events:
        print("No events found for the given filters.")
        return 0

    auto = [e for e in compact_events if e["trigger"] == "auto"]
    manual = [e for e in compact_events if e["trigger"] == "manual"]

    summarize(auto, "auto")
    print()
    summarize(manual, "manual")

    if args.by_model:
        print()
        usage_by_model(usage_events)

    if args.daily:
        print()
        daily_breakdown(compact_events, usage_events, pricing)

    return 0


if __name__ == "__main__":
    sys.exit(main())
