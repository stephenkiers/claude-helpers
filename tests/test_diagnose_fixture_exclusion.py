#!/usr/bin/env python3
"""
Test suite for the known-fixture session_id exclusion in `run-metrics.py diagnose` (issue #280).

Covers: exact excluded count, exact unknown-token percentage and match-rate counts over real data
only, near-miss ids still counted, `Excluded fixture rows: 0` printed on a clean log, the exclusion
running after the window filter, the agent guard ignoring fixture rows after an explicit
--agent-guard-since, and the constant's pinned contents. Nothing asserts a direction of change, only
hand-computed exact values.

Run with: python3 tests/test_diagnose_fixture_exclusion.py
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _test_harness import REPO_ROOT, Harness

SCRIPT = REPO_ROOT / "scripts" / "run-metrics.py"
sys.path.insert(0, str(REPO_ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location("run_metrics_fixture_excl", SCRIPT)
run_metrics = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_metrics)

UTC = timezone.utc
KNOWN_IDS = {
    "floor-latch-sess", "next-ask-sess", "fresh-budget-sess",
    "smoketest1", "smoketest2", "smoketest3", "smoketest4", "smoketest5",
}
KNOWN_TOKENS = {"input": 10, "output": 20, "cache_read": 5, "cache_creation": 5}
MIXED_TOKENS = {"input": 10, "output": "unknown", "cache_read": 5, "cache_creation": 5}
UNKNOWN_TOKENS = {k: "unknown" for k in KNOWN_TOKENS}


def _iso(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(event_type, ts, session_id, agent_id, tokens=None, agent_type="reviewer"):
    row = {
        "schema_version": 1, "event_type": event_type, "timestamp": _iso(ts),
        "agent_type": agent_type, "agent_id": agent_id, "session_id": session_id,
    }
    if tokens is not None:
        row["tokens"] = tokens
    return row


def _run(events, extra=()):
    """Run `diagnose` over the given events. Returns (stdout, context) where context names the
    child's returncode and stderr, for use in failure messages."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        home = root / "home"
        home.mkdir()
        log = root / "events.jsonl"
        log.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        cmd = [sys.executable, str(SCRIPT), "--log", str(log), "diagnose", *extra]
        r = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, "HOME": str(home)})
        return r.stdout, f"(child rc={r.returncode}, stderr={r.stderr!r})"


def _excluded_count(out):
    """The integer after `Excluded fixture rows:`, or None if the line is absent."""
    m = re.search(r"Excluded fixture rows: (\d+)\b", out)
    return int(m.group(1)) if m else None


def _mixed_log():
    now = datetime.now(UTC)
    t = now - timedelta(days=1)
    old = now - timedelta(days=40)
    return [
        # Real: r1 matched (1 of 4 token fields unknown), r2 unmatched.
        _row("agent.begin", t, "real-sess", "r1"),
        _row("agent.end", t + timedelta(seconds=5), "real-sess", "r1", MIXED_TOKENS),
        _row("agent.begin", t, "real-sess", "r2"),
        # Near-miss ids are real data: matched, 4 known token fields.
        _row("agent.begin", t, "smoketest6", "n1"),
        _row("agent.end", t + timedelta(seconds=5), "smoketest6", "n1", KNOWN_TOKENS),
        _row("agent.begin", t, "floor-latch-sess-2", "n2"),
        _row("agent.begin", t, "agent1", "n3"),
        # Fixture rows in window: 3 excluded.
        _row("agent.begin", t, "floor-latch-sess", "f1"),
        _row("agent.end", t + timedelta(seconds=5), "floor-latch-sess", "f1", UNKNOWN_TOKENS),
        _row("agent.begin", t, "smoketest1", "f2"),
        # Fixture row outside the 30-day window: must not count toward N.
        _row("agent.begin", old, "smoketest2", "f3"),
    ]


def test_exclusion_counts_exact():
    out, ctx = _run(_mixed_log())
    excluded = _excluded_count(out)
    if excluded != 3:
        return False, f"expected exactly 3 excluded (out-of-window fixture not counted), got {excluded} {ctx}: {out!r}"
    # Real begins: r1, r2, n1, n2, n3 = 5; matched: r1, n1 = 2.
    if "Match rate: 40.0% (2/5 begin/end pairs matched)" not in out:
        return False, f"match-rate counts wrong {ctx}: {out!r}"
    # Token fields over r1 end (4, 1 unknown) + n1 end (4, 0 unknown) = 1/8.
    if "Unknown token fields: 12.5%" not in out:
        return False, f"unknown-token percentage wrong {ctx}: {out!r}"
    return True, ""


def test_near_miss_ids_still_counted():
    # n1/n2/n3 are near misses; if they were excluded the begin total would be 2, not 5.
    out, ctx = _run(_mixed_log())
    m = re.search(r"\((\d+)/(\d+) begin/end pairs matched\)", out)
    if not m or m.group(2) != "5":
        return False, f"near-miss rows should be counted (5 begins) {ctx}: {out!r}"
    return True, ""


def test_zero_excluded_printed_on_clean_log():
    t = datetime.now(UTC) - timedelta(days=1)
    out, ctx = _run([
        _row("agent.begin", t, "real-sess", "r1"),
        _row("agent.end", t + timedelta(seconds=5), "real-sess", "r1", KNOWN_TOKENS),
    ])
    excluded = _excluded_count(out)
    if excluded != 0:
        return False, f"count must be printed as 0 even on a clean log, got {excluded} {ctx}: {out!r}"
    return True, ""


def test_agent_guard_ignores_fixture_rows_after_explicit_since():
    # With an explicit --agent-guard-since, fixture rows inside the window must still be dropped
    # before the guard runs. The blank-agent_id begin is a check (a) violation if it is counted, and
    # the fixture end is paired with a real begin, so an unpaired real begin also stays real data.
    now = datetime.now(UTC)
    t = now - timedelta(days=1)
    since = now - timedelta(days=3)
    events = [
        _row("agent.begin", t, "real-sess", "r1"),
        _row("agent.end", t + timedelta(seconds=5), "smoketest1", "r1", KNOWN_TOKENS),
        _row("agent.begin", t, "floor-latch-sess", "", agent_type="reviewer"),
    ]
    out, ctx = _run(events, extra=["--agent-guard-since", since.isoformat()])
    excluded = _excluded_count(out)
    if excluded != 2:
        return False, f"expected 2 fixture rows excluded before the guard, got {excluded} {ctx}: {out!r}"
    if "VIOLATION" in out:
        return False, f"guard must ignore fixture rows after --agent-guard-since {ctx}: {out!r}"
    if "Agent guard: events from " + since.isoformat()[:16] not in out:
        return False, f"explicit --agent-guard-since not reflected in guard header {ctx}: {out!r}"
    return True, ""


def test_constant_pinned():
    if set(run_metrics.KNOWN_FIXTURE_SESSION_IDS) != KNOWN_IDS:
        return False, f"KNOWN_FIXTURE_SESSION_IDS changed: {sorted(run_metrics.KNOWN_FIXTURE_SESSION_IDS)}"
    if not isinstance(run_metrics.KNOWN_FIXTURE_SESSION_IDS, frozenset):
        return False, "constant must be a frozenset"
    return True, ""


def main():
    h = Harness("DIAGNOSE FIXTURE EXCLUSION TEST SUITE")
    h.test_result("exact excluded count, match-rate, unknown-token", *test_exclusion_counts_exact())
    h.test_result("near-miss ids still counted", *test_near_miss_ids_still_counted())
    h.test_result("zero excluded printed on clean log", *test_zero_excluded_printed_on_clean_log())
    h.test_result("agent guard ignores fixture rows after explicit --agent-guard-since",
                  *test_agent_guard_ignores_fixture_rows_after_explicit_since())
    h.test_result("constant contents pinned", *test_constant_pinned())
    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
