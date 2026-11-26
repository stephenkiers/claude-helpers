"""
The inbox (PLAN.md section 10): every flow's state, cost and elapsed time, and everything that is
waiting for the human, grouped as questions, merge gates and stops, oldest first.

`collect` gathers plain data (what `inbox --json` prints); `render` turns it into text. Nothing here
changes a flow.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from scripts.flow3 import questions, record
from scripts.flow3.state import FlowDir
from scripts.workflow.fsutil import LockHeld, nonblocking_flock

DRIVER = "python3 -m scripts.flow3"


def is_running(flow: FlowDir) -> bool:
    """True when a live process holds the flow's run lock (the engine takes it while driving)."""
    try:
        with nonblocking_flock(flow.path / ".run.lock"):
            return False
    except LockHeld:
        return True
    except OSError:
        return False


def _parse(ts: Any) -> Optional[datetime]:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def fmt_secs(secs: float) -> str:
    """A short duration: 45s, 12m, 3h05m."""
    total = int(max(0, secs))
    if total < 90:
        return "%ds" % total
    if total < 3600:
        return "%dm" % (total // 60)
    return "%dh%02dm" % (total // 3600, (total % 3600) // 60)


def _last_ts(events: List[Dict[str, Any]], kind: str) -> str:
    for row in reversed(events):
        if row.get("kind") == kind and row.get("ts"):
            return str(row["ts"])
    return ""


def flow_row(flow: FlowDir, events: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """One flow's summary: stage, status, cost, elapsed, and whether it is over its soft budget."""
    rows = flow.events() if events is None else events
    doc = flow.load()
    st = flow.flow_state()
    created = _parse(doc.get("created"))
    last = _parse(rows[-1].get("ts")) if rows else None
    elapsed = (last - created).total_seconds() if created and last else 0.0
    soft = None
    for row in rows:
        if row.get("kind") == "budget_soft":
            soft = row
    return {
        "name": flow.name, "repo": str(doc.get("repo") or ""), "issue": int(doc.get("issue") or 0),
        "title": str(doc.get("title") or ""), "stage": st.stage, "status": st.status,
        "review_round": st.review_round, "sendbacks": st.sendbacks,
        "cost_usd": round(flow.total_cost(), 4), "elapsed_secs": round(elapsed, 1),
        "created": str(doc.get("created") or ""), "last_activity": str(rows[-1].get("ts")) if rows else "",
        "alive": is_running(flow),
        "over_soft_budget": bool(doc.get("budget_soft_noted")) or soft is not None,
        "soft_usd": soft.get("soft_usd") if soft else None,
    }


def collect(repo: Optional[str], now: Optional[datetime] = None) -> Dict[str, Any]:
    """Everything the inbox shows, as plain data. `repo` None means every repo."""
    moment = now or datetime.now(timezone.utc)
    today = moment.astimezone(timezone.utc).date().isoformat()
    flows: List[Dict[str, Any]] = []
    waiting_q: List[Dict[str, Any]] = []
    gates: List[Dict[str, Any]] = []
    stops: List[Dict[str, Any]] = []
    today_cost = 0.0
    today_flows = 0
    for flow in FlowDir.all(repo):
        events = flow.events()
        row = flow_row(flow, events)
        flows.append(row)
        spent = 0.0
        for ev in events:
            if ev.get("kind") == "call" and str(ev.get("ts") or "").startswith(today):
                try:
                    spent += float(ev.get("cost_usd") or 0.0)
                except (TypeError, ValueError):
                    continue
        today_cost += spent
        if spent > 0:
            today_flows += 1
        base = {"flow": row["name"], "repo": row["repo"], "issue": row["issue"], "title": row["title"],
                "stage": row["stage"]}
        if row["status"] == "waiting_human":
            batch = questions.open_batch(flow)
            item: Dict[str, Any] = dict(base)
            if batch is None:
                item.update(since=_last_ts(events, "park") or row["last_activity"], cards=[], markdown="",
                            note="parked, but no open question batch is recorded")
            else:
                cards = [c for c in batch.get("cards") or [] if isinstance(c, dict) and c.get("status") == "open"]
                note = "" if cards else "every question is answered; the flow continues when the answer is sent"
                item.update(since=str(batch.get("created") or "") or row["last_activity"], cards=cards,
                            markdown=questions.render_md(batch), note=note)
            waiting_q.append(item)
        elif row["status"] == "merge_gate":
            item = dict(base)
            item.update(since=_last_ts(events, "merge_gate") or row["last_activity"],
                        see="%s gate %d" % (DRIVER, row["issue"]))
            gates.append(item)
        elif row["status"] == "stopped":
            stop = record.read_stop(flow) or {}
            item = dict(base)
            item.update(since=_last_ts(events, "stop") or row["last_activity"], cls=str(stop.get("cls") or ""),
                        reason=str(stop.get("reason") or ""), evidence=stop.get("evidence"),
                        resume_command=str(stop.get("resume_command") or ""))
            stops.append(item)
    for group in (waiting_q, gates, stops):
        group.sort(key=lambda r: str(r.get("since") or ""))
    return {"generated": moment.isoformat(), "repo": repo, "flows": flows, "questions": waiting_q,
            "merge_gates": gates, "stops": stops,
            "today": {"date": today, "cost_usd": round(today_cost, 4), "flows": today_flows}}


def _flow_line(row: Dict[str, Any]) -> str:
    status = row["status"]
    if status == "running" and not row["alive"]:
        status = "running (no live process: resume it)"
    line = "  %-40s %-10s %-26s $%.2f  %s" % ("%s/%s" % (row["repo"], row["name"]), row["stage"], status, row["cost_usd"],
                                             fmt_secs(row["elapsed_secs"]))
    if row["over_soft_budget"]:
        soft = row.get("soft_usd")
        line += "\n    over its soft budget%s (the flow keeps going)" % (" of $%.2f" % float(soft) if soft else "")
    return line


def status_table(data: Dict[str, Any]) -> str:
    """The flow list and the day's total (what `status` prints)."""
    lines = ["FLOWS"]
    lines += [_flow_line(r) for r in data["flows"]] or ["  (none)"]
    today = data["today"]
    lines.append("")
    lines.append("Today (%s UTC): $%.2f across %d flow(s)" % (today["date"], today["cost_usd"], today["flows"]))
    return "\n".join(lines)


def render(data: Dict[str, Any]) -> str:
    """The inbox as text."""
    out: List[str] = [status_table(data), ""]
    qs, gates, stops = data["questions"], data["merge_gates"], data["stops"]
    if not (qs or gates or stops):
        out.append("Nothing is waiting for you.")
        return "\n".join(out) + "\n"
    out.append("QUESTIONS (%d)" % len(qs))
    for q in qs:
        out += ["", "=== #%s %s (waiting since %s) ===" % (q["issue"], q["title"], q["since"][:19]), ""]
        if q["markdown"]:
            out.append(q["markdown"].rstrip())
        if q["note"]:
            out.append("(%s)" % q["note"])
        out.append("")
        out.append("Answer with: %s answer %d   (your words on stdin, or `<card id>: <option id>` lines)"
                   % (DRIVER, q["issue"]))
    if not qs:
        out.append("  (none)")
    out += ["", "MERGE GATES (%d)" % len(gates)]
    for g in gates:
        out.append("  #%s %s (waiting since %s)" % (g["issue"], g["title"], g["since"][:19]))
        out.append("    see the gate screen: %s" % g["see"])
        out.append("    then: %s merge %d --merge | --send-back NOTES | --stop" % (DRIVER, g["issue"]))
    if not gates:
        out.append("  (none)")
    out += ["", "STOPS (%d)" % len(stops)]
    for s in stops:
        out.append("  #%s %s (stopped %s)" % (s["issue"], s["title"], s["since"][:19]))
        out.append("    class: %s" % s["cls"])
        out.append("    why:   %s" % s["reason"])
        out.append("    evidence: %s" % (s["evidence"] or "none recorded"))
        out.append("    resume: %s" % s["resume_command"])
    if not stops:
        out.append("  (none)")
    return "\n".join(out) + "\n"
