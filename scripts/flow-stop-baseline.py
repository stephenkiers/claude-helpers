#!/usr/bin/env python3
"""
Layer 0 stop-attribution baseline for the v1 /expert-flow orchestrator (ADR-0026 Step 3).

Read-only. Walks FLOWS_ROOT/<repo>/<flow>/state.json and each flow's step receipts (steps/*.md) and
reports aggregate counts only. It never writes under the flows root, never takes a lock and makes no
network call. Its output carries no flow ids, issue numbers, titles, repo names or paths.

Reports:
- flows started on or after --since, and how many reached the merge or done step;
- every recorded stop, classified by the RULES table as orchestrator, step-agent, environment or
  ambiguous. A stop whose reason matches no rule is ambiguous, as is a stop status with no reason
  recorded anywhere; ambiguous rows are grouped so a human can label them;
- first-turn token cost, reported as unavailable (v1 state has no link to per-agent telemetry rows).

Usage:
  flow-stop-baseline.py [--flows-root DIR] [--since YYYY-MM-DD] [--format json|md]

Exit codes:
  0: report printed
  2: usage error (bad --since, or the flows root is not a directory)
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

ORCHESTRATOR = "orchestrator"
STEP_AGENT = "step-agent"
ENVIRONMENT = "environment"
AMBIGUOUS = "ambiguous"
CLASS_ORDER: Tuple[str, ...] = (ORCHESTRATOR, STEP_AGENT, ENVIRONMENT, AMBIGUOUS)

# "Reached merge or done" is measured by the step a flow is on now (state.json "step").
MERGE_OR_DONE_STEPS: Tuple[str, ...] = ("merge", "done")
# Statuses that mean the flow stopped or is waiting on a human, so a stop record is expected.
STOP_STATUSES: Tuple[str, ...] = ("awaiting", "failed")

# Explicit rule table, first match wins. Each row: (regex matched against the stop reason, class, label).
# The regexes are case-insensitive. Anything that matches no row is ambiguous and listed for a human.
RULES: List[Tuple[str, str, str]] = [
    (r"ledger unavailable", ORCHESTRATOR, "fidelity audit: ledger unavailable"),
    (r"^anti-tamper gate", ORCHESTRATOR, "anti-tamper gate"),
    (r"^gate-not-converged", ORCHESTRATOR, "gate not converged"),
    (r"^unit-record-mismatch", ORCHESTRATOR, "unit record mismatch"),
    (r"^primary-checkout-drift", ENVIRONMENT, "primary checkout drift"),
    (r"^check-not-configured", ENVIRONMENT, "check not configured"),
    (r"^check source mismatch", ENVIRONMENT, "check source mismatch"),
    (r"^check-not-run", ENVIRONMENT, "check not run (timeout or stale base)"),
    (r"^queue kickback", ENVIRONMENT, "merge queue kickback"),
    (r"^skill invocation failed", ENVIRONMENT, "skill invocation failed"),
    (r"^check (gate )?failed", STEP_AGENT, "check failed"),
    (r"^(malformed receipt|no receipt written|receipt:|assess receipt)", STEP_AGENT, "receipt defect"),
    (r"^(implementer-shortfall|implement run incomplete)", STEP_AGENT, "implementer shortfall"),
    (r"^reduced run", STEP_AGENT, "reduced run (fidelity audit)"),
    (r"^fidelity audit failed", STEP_AGENT, "fidelity audit failed (other)"),
]

# Leading generic words of an unmatched reason, used as its grouping key. Free text is never echoed.
_SHAPE_RE = re.compile(r"[a-z][a-z-]*(?: [a-z][a-z-]*){0,2}")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DECISION_FAILED_RE = re.compile(r"^Decision: FAILED\s*$", re.MULTILINE)
_REASON_RE = re.compile(r"^Reason: (.+)$", re.MULTILINE)

TOKEN_COST_REASON = (
    "v1 flow state has no link to per-agent telemetry rows, so first-turn tokens per flow cannot be "
    "computed from existing data; v1 is not instrumented for this metric"
)

DEFINITION_LINES: List[str] = [
    "Window: flows whose state.json `started` date is on or after --since (all dated and undated flows when --since is absent).",
    "Reached merge or done: the flow's current `step` is `merge` or `done`. This is a point-in-time read, so a flow that reached merge and was later moved back is not counted.",
    "Stop record: each non-empty `stop_reason` entry in state.json, whatever the flow's status; plus, for a flow whose status is `awaiting` or `failed`, its current step when no `stop_reason` entry exists for that step.",
    "Stop reason source: the state.json `stop_reason` entry, or else the `Reason:` line of the current step's receipt when that receipt says `Decision: FAILED`.",
    "Classification: the RULES table, first match wins. Unmatched reasons, and stop statuses with no reason recorded anywhere, are ambiguous.",
    "Not counted as stops: `paused` (a human choice at a gate), `abandoned`, and `running` flows with no stop_reason entry.",
]


@dataclass(frozen=True)
class Stop:
    """One recorded stop. The reason text is not kept, only its class, label and where it came from."""

    stop_class: str
    label: str
    source: str  # "state", "receipt" or "none"


def classify_stop_reason(reason: str) -> Tuple[str, str]:
    """Return (class, label) for a non-empty stop reason. A reason that matches no rule is ambiguous."""
    text = reason.strip()
    for pattern, stop_class, label in RULES:
        if re.search(pattern, text, re.IGNORECASE):
            return stop_class, label
    match = _SHAPE_RE.match(text.lower())
    shape = match.group(0) if match else "(other)"
    return AMBIGUOUS, "unmatched reason: " + shape


def classify_without_reason(step: str, status: str) -> Tuple[str, str]:
    """Return (class, label) for a stop status with no reason recorded anywhere. Always ambiguous."""
    return AMBIGUOUS, "no reason recorded (" + step + "/" + status + ")"


def _read_text(path: Path) -> Optional[str]:
    """The file's text, or None when it cannot be read. Read-only."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def read_state(flow_dir: Path) -> Optional[Dict[str, Any]]:
    """The parsed state.json object, or None when absent, unreadable, unparseable or not an object."""
    text = _read_text(flow_dir / "state.json")
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def receipt_reason(flow_dir: Path, step: str) -> Optional[str]:
    """The Reason line of the step's latest receipt, when that receipt records Decision: FAILED."""
    steps_dir = flow_dir / "steps"
    if not steps_dir.is_dir():
        return None
    receipts = sorted(steps_dir.glob("*-" + step + ".md"))
    if not receipts:
        return None
    text = _read_text(receipts[-1])
    if text is None or _DECISION_FAILED_RE.search(text) is None:
        return None
    match = _REASON_RE.search(text)
    if match is None:
        return None
    reason = match.group(1).strip()
    return reason or None


def _text(value: Any) -> str:
    """A non-empty string field, or 'unknown'."""
    return value if isinstance(value, str) and value else "unknown"


def started_date(state: Dict[str, Any]) -> Optional[str]:
    """The YYYY-MM-DD part of the state's started timestamp, or None when it is absent or malformed."""
    started = state.get("started")
    if isinstance(started, str) and _DATE_RE.match(started):
        return started[:10]
    return None


def flow_stops(flow_dir: Path, state: Dict[str, Any]) -> List[Stop]:
    """Every stop recorded for one flow, classified. Reads state.json's stop_reason and the step receipts."""
    status = _text(state.get("status"))
    step = _text(state.get("step"))
    stops: List[Stop] = []
    recorded = state.get("stop_reason")
    reasons: Dict[str, Any] = recorded if isinstance(recorded, dict) else {}
    steps_with_reason: Set[str] = set()
    for step_key in sorted(reasons):
        value = reasons[step_key]
        if isinstance(value, str) and value.strip():
            stop_class, label = classify_stop_reason(value)
            stops.append(Stop(stop_class, label, "state"))
            steps_with_reason.add(step_key)
    if status in STOP_STATUSES and step not in steps_with_reason:
        fallback = receipt_reason(flow_dir, step)
        if fallback is not None:
            stop_class, label = classify_stop_reason(fallback)
            stops.append(Stop(stop_class, label, "receipt"))
        else:
            stop_class, label = classify_without_reason(step, status)
            stops.append(Stop(stop_class, label, "none"))
    return stops


def _bump(counter: Dict[str, int], key: str) -> None:
    """Add one to counter[key]."""
    counter[key] = counter.get(key, 0) + 1


def build_report(flows_root: Path, since: Optional[str]) -> Dict[str, Any]:
    """Aggregate counts, stop classes and ambiguous rows over flows_root/*/*/state.json. Read-only."""
    counts: Dict[str, int] = {
        "flows_in_window": 0,
        "reached_merge_or_done": 0,
        "outside_window": 0,
        "undated_excluded": 0,
        "unreadable_state": 0,
    }
    by_status: Dict[str, int] = {}
    by_step: Dict[str, int] = {}
    stop_by_class: Dict[str, int] = {stop_class: 0 for stop_class in CLASS_ORDER}
    stop_by_source: Dict[str, int] = {"state": 0, "receipt": 0, "none": 0}
    stop_rows: Dict[Tuple[str, str], int] = {}

    for state_path in sorted(flows_root.glob("*/*/state.json")):
        flow_dir = state_path.parent
        state = read_state(flow_dir)
        if state is None:
            counts["unreadable_state"] += 1
            continue
        if since is not None:
            started = started_date(state)
            if started is None:
                counts["undated_excluded"] += 1
                continue
            if started < since:
                counts["outside_window"] += 1
                continue
        counts["flows_in_window"] += 1
        status = _text(state.get("status"))
        step = _text(state.get("step"))
        _bump(by_status, status)
        _bump(by_step, step)
        if step in MERGE_OR_DONE_STEPS:
            counts["reached_merge_or_done"] += 1
        for stop in flow_stops(flow_dir, state):
            stop_by_class[stop.stop_class] += 1
            stop_by_source[stop.source] += 1
            key = (stop.stop_class, stop.label)
            stop_rows[key] = stop_rows.get(key, 0) + 1

    rows: List[Dict[str, Any]] = [
        {"class": stop_class, "label": label, "count": count}
        for (stop_class, label), count in sorted(
            stop_rows.items(),
            key=lambda item: (CLASS_ORDER.index(item[0][0]), -item[1], item[0][1]),
        )
    ]
    return {
        "definition": DEFINITION_LINES,
        "since": since,
        "counts": counts,
        "by_status": by_status,
        "by_step": by_step,
        "stops": {
            "total": sum(stop_by_class.values()),
            "by_class": stop_by_class,
            "by_source": stop_by_source,
        },
        "stop_table": [row for row in rows if row["class"] != AMBIGUOUS],
        "ambiguous": [row for row in rows if row["class"] == AMBIGUOUS],
        "token_cost": {"status": "unavailable", "reason": TOKEN_COST_REASON},
    }


def _md_table(headers: List[str], rows: List[List[str]]) -> List[str]:
    """A markdown table as lines."""
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return lines


def _counts_map_rows(mapping: Dict[str, int]) -> List[List[str]]:
    """Rows of (key, count) from a counter, sorted by key."""
    return [[key, str(mapping[key])] for key in sorted(mapping)]


def render_md(report: Dict[str, Any]) -> str:
    """The aggregate report as markdown. Contains counts and generic labels only."""
    counts: Dict[str, int] = report["counts"]
    stops: Dict[str, Any] = report["stops"]
    since = report["since"]
    window = ("started on or after " + since) if since else "all flows with a readable state.json"
    lines: List[str] = [
        "# v1 expert-flow stop baseline",
        "",
        "Window: " + window + ".",
        "",
        "## Definition",
        "",
    ]
    lines.extend("- " + item for item in report["definition"])
    lines.extend([
        "",
        "## Counts",
        "",
    ])
    lines.extend(_md_table(["Measure", "Count"], [
        ["Flows in window", str(counts["flows_in_window"])],
        ["Reached merge or done", str(counts["reached_merge_or_done"])],
        ["Outside window (excluded)", str(counts["outside_window"])],
        ["Undated (excluded)", str(counts["undated_excluded"])],
        ["Unreadable state (excluded)", str(counts["unreadable_state"])],
        ["Stops recorded", str(stops["total"])],
    ]))
    lines.extend(["", "### Flows by status", ""])
    lines.extend(_md_table(["Status", "Flows"], _counts_map_rows(report["by_status"])))
    lines.extend(["", "### Flows by current step", ""])
    lines.extend(_md_table(["Step", "Flows"], _counts_map_rows(report["by_step"])))
    lines.extend(["", "## Stops by class", ""])
    lines.extend(_md_table(["Class", "Stops"], [
        [stop_class, str(stops["by_class"][stop_class])] for stop_class in CLASS_ORDER
    ]))
    lines.extend(["", "Stops by source: " + ", ".join(
        key + " " + str(stops["by_source"][key]) for key in sorted(stops["by_source"])
    ) + ".", ""])
    lines.extend(["## Stop reason classes", ""])
    lines.extend(_md_table(["Class", "Label", "Stops"], [
        [row["class"], row["label"], str(row["count"])] for row in report["stop_table"]
    ]) if report["stop_table"] else ["None."])
    lines.extend(["", "## Ambiguous rows (for a human to label)", ""])
    lines.extend(_md_table(["Class", "Label", "Stops"], [
        [row["class"], row["label"], str(row["count"])] for row in report["ambiguous"]
    ]) if report["ambiguous"] else ["None."])
    lines.extend([
        "",
        "## Reconciliation",
        "",
        "This report stands on its own definition above: " + str(counts["reached_merge_or_done"]) + " of "
        + str(counts["flows_in_window"]) + " flows in the window reached the merge or done step. Figures quoted "
        "for the same ticket from other sources can differ by definition only. The differences are the window, "
        "the population (for example only finished runs, or only runs with a telemetry row), and the end state "
        "(for example done alone, not merge). The current-step read also undercounts flows that reached merge "
        "and were later moved back. None of these differences is a data error, and this count is the one the "
        "baseline stands on.",
        "",
        "## First-turn token cost",
        "",
        "Status: " + report["token_cost"]["status"] + ". " + report["token_cost"]["reason"] + ".",
        "",
    ])
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point. Returns the exit code."""
    parser = argparse.ArgumentParser(
        description="Stop-attribution baseline for v1 expert-flow (read-only, aggregate counts only)."
    )
    parser.add_argument("--flows-root", default="~/.claude/flows",
                        help="flows root to read (default: ~/.claude/flows)")
    parser.add_argument("--since", default=None,
                        help="count only flows started on or after this YYYY-MM-DD date (default: all)")
    parser.add_argument("--format", dest="fmt", choices=("json", "md"), default="md",
                        help="output format (default: md)")
    args = parser.parse_args(argv)

    since: Optional[str] = None
    if args.since is not None:
        try:
            datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            parser.error("--since must be a YYYY-MM-DD date")
        since = args.since

    root = Path(args.flows_root).expanduser()
    if not root.is_dir():
        sys.stderr.write("flows root is not a directory\n")
        return 2

    report = build_report(root, since)
    if args.fmt == "json":
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_md(report) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
