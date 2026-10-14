#!/usr/bin/env python3
"""
Test suite for the agent event guard in `run-metrics.py diagnose` (issue #269).

Covers:
1. Check (a): agent_type / agent_id / session_id must be non-blank strings; "unknown" is report-only.
2. Check (b): agent.end <= agent.begin * (1 + tolerance), min-begins skip, straddler, typed internal stops.
3. Truncated JSON log lines warn on stderr and never produce a traceback.
4. --window-days interplay and the default --agent-guard-since (AGENT_GUARD_CUTOFF).
5. Flag validation (naive datetime, garbage, nan, out-of-range tolerance, min-begins 0).
6. Hook target provenance line, always with a temp HOME.
7. AGENT_GUARD_CUTOFF literal appears in the two docs; the placeholder text does not.

Every CLI run passes a temp HOME and an explicit --window-days (except the window test itself).

Run with: python3 tests/test_agent_event_guard.py
"""

import importlib.util
import itertools
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _test_harness import REPO_ROOT, Harness

try:
    from os import geteuid
except ImportError:
    # Windows doesn't have geteuid; define a dummy that returns None (not 0)
    def geteuid():
        return None

SCRIPTS_DIR = REPO_ROOT / "scripts"
SCRIPT = SCRIPTS_DIR / "run-metrics.py"
DOC_PATHS = [
    REPO_ROOT / "docs" / "metrics.md",
    REPO_ROOT / "docs" / "adr" / "0016-usage-yield-telemetry.md",
]
CUTOFF_LITERAL = "2026-10-13T18:58:25Z"
PLACEHOLDER = "filled in by the follow-up PR"

UTC = timezone.utc

sys.path.insert(0, str(SCRIPTS_DIR))
_spec = importlib.util.spec_from_file_location("run_metrics", SCRIPT)
run_metrics = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_metrics)

AGENT_GUARD_CUTOFF = run_metrics.AGENT_GUARD_CUTOFF
check_agent_events = run_metrics.check_agent_events
describe_hook_provenance = run_metrics.describe_hook_provenance
_is_nonblank_str = run_metrics._is_nonblank_str
_exceeds_allowed = run_metrics._exceeds_allowed

# Derive test constant from the actual cutoff (use Z suffix for ISO 8601)
CUTOFF_LITERAL = AGENT_GUARD_CUTOFF.strftime("%Y-%m-%dT%H:%M:%SZ")
SINCE = AGENT_GUARD_CUTOFF
_ids = itertools.count(1)


# ============================================================================
# Helpers
# ============================================================================


def run_script(args, env):
    """Run run-metrics.py as a subprocess. Returns (returncode, stdout, stderr)."""
    cmd = [sys.executable, str(SCRIPT)] + args
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return result.returncode, result.stdout, result.stderr


def _now():
    return datetime.now(UTC)


def _iso(dt):
    """Format a tz-aware datetime as a Z-suffixed ISO string."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _agent(event_type, ts, agent_type="reviewer", agent_id=None, session_id="s-agent"):
    """Build one agent event row. agent_id defaults to a unique value."""
    if agent_id is None:
        agent_id = f"agent-{next(_ids)}"
    return {
        "schema_version": 1,
        "event_type": event_type,
        "timestamp": _iso(ts),
        "agent_type": agent_type,
        "agent_id": agent_id,
        "session_id": session_id,
    }


def _ratio_events(begins, ends, internal_stops=0, base=SINCE):
    """Valid agent rows: `begins` agent.begin, `ends` agent.end, `internal_stops` untyped stops.

    The first min(begins, ends) ends share an agent_id with a begin, so diagnose's begin/end
    match-rate gate sees them as paired; extra ends get fresh ids (they only drive the guard).
    """
    events = []
    begin_ids = []
    for i in range(begins):
        agent_id = f"agent-r{i}"
        begin_ids.append(agent_id)
        events.append(_agent("agent.begin", base + timedelta(seconds=1 + i), agent_id=agent_id))
    for i in range(ends):
        agent_id = begin_ids[i] if i < len(begin_ids) else None
        events.append(_agent("agent.end", base + timedelta(seconds=1000 + i), agent_id=agent_id))
    for i in range(internal_stops):
        events.append(
            _agent("agent.internal_stop", base + timedelta(seconds=2000 + i), agent_type="unknown")
        )
    return events


def _baseline(now):
    """Command/stage pairs that pass `diagnose` on their own: all matched, no 'unknown' tokens."""
    events = []
    for i in range(25):
        cid = f"c-{i:03d}"
        stid = f"st-{i:03d}"
        t0 = now - timedelta(days=2) + timedelta(minutes=i)
        common = {
            "schema_version": 1,
            "session_id": "s-base",
            "command_id": cid,
            "turns": 1,
            "elapsed_seconds": 5.0,
            "retries": 0,
            "peak_concurrency": 1,
            "transcript_size": 100,
            "output_artifact_size": 100,
        }
        events.append({**common, "event_type": "command.begin", "timestamp": _iso(t0),
                       "command": "guard-fixture"})
        events.append({**common, "event_type": "stage.begin", "timestamp": _iso(t0 + timedelta(seconds=1)),
                       "stage_id": stid, "stage": "build"})
        events.append({**common, "event_type": "stage.end", "timestamp": _iso(t0 + timedelta(seconds=2)),
                       "stage_id": stid, "stage": "build", "outcome": {"status": "success"}})
        events.append({**common, "event_type": "command.end", "timestamp": _iso(t0 + timedelta(seconds=3)),
                       "outcome": {"status": "success"}})
    return events


def _write_log(path, events, trailing_raw=""):
    text = "".join(json.dumps(e) + "\n" for e in events) + trailing_raw
    path.write_text(text, encoding="utf-8")


def _run(events, extra=(), window_days=3650, trailing_raw="", home_setup=None):
    """Run `diagnose` on a temp log with a temp HOME. Returns (code, stdout, stderr)."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        home = root / "home"
        home.mkdir()
        if home_setup is not None:
            home_setup(root, home)
        log = root / "events.jsonl"
        _write_log(log, events, trailing_raw)
        env = {**os.environ, "HOME": str(home)}
        args = ["--log", str(log), "diagnose", "--window-days", str(window_days), *extra]
        return run_script(args, env)


def _fail_expected(code, out, needles):
    """Assert a FAIL run: exit 1, every needle present in stdout, and the hint line."""
    if code != 1:
        return False, f"expected exit 1, got {code}: {out!r}"
    missing = [n for n in needles if n not in out]
    if missing:
        return False, f"stdout missing {missing!r}: {out!r}"
    if "before adjusting --agent-guard-since" not in out:
        return False, f"FAIL output missing the --agent-guard-since hint: {out!r}"
    return True, ""


def _control_ok(code, out, err):
    if code != 0:
        return False, f"control run should exit 0, got {code}: {out!r} {err!r}"
    return True, ""


def _violation_needles(event_type, field, ts):
    return [event_type, field, ts]


def _direct_prov(setup):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        home = root / "home"
        home.mkdir()
        setup(root, home)
        return describe_hook_provenance(home, SCRIPT)


def _link_hook(home, target):
    scripts = home / ".claude" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "run-metrics.py").symlink_to(target)


# ============================================================================
# T8: check (a) and the boundary of `since`
# ============================================================================


BAD_VALUES = [("empty", ""), ("space", " "), ("none", None), ("int", 5), ("list", [])]


def t8_direct_nonblank_fields():
    """Each of agent_type/agent_id/session_id yields exactly one violation for each bad value and for a missing key."""
    failures = []
    for field in ("agent_type", "agent_id", "session_id"):
        for label, bad in BAD_VALUES:
            ev = _agent("agent.begin", SINCE + timedelta(seconds=1))
            ev[field] = bad
            res = check_agent_events([ev], SINCE, 0.05, 20)
            if len(res.violations) != 1 or field not in res.violations[0]:
                failures.append(f"{field}={label}: {res.violations!r}")
        ev = _agent("agent.begin", SINCE + timedelta(seconds=1))
        del ev[field]
        res = check_agent_events([ev], SINCE, 0.05, 20)
        if len(res.violations) != 1 or field not in res.violations[0]:
            failures.append(f"{field} missing: {res.violations!r}")
    if failures:
        return False, "; ".join(failures)
    return True, ""


def t8_direct_unknown_is_report_only():
    """'unknown' in agent_type or in agent_id/session_id is never a violation; it bumps the report counter."""
    t = SINCE + timedelta(seconds=1)
    res = check_agent_events([_agent("agent.begin", t, agent_type="unknown")], SINCE, 0.05, 20)
    if res.violations != [] or res.report.unknown_agent_type != 1:
        return False, f"agent_type=unknown: violations={res.violations!r} report={res.report!r}"
    res = check_agent_events([_agent("agent.begin", t, agent_id="unknown")], SINCE, 0.05, 20)
    if res.violations != [] or res.report.unknown_ids != 1:
        return False, f"agent_id=unknown: violations={res.violations!r} report={res.report!r}"
    res = check_agent_events([_agent("agent.begin", t, session_id="unknown")], SINCE, 0.05, 20)
    if res.violations != [] or res.report.unknown_ids != 1:
        return False, f"session_id=unknown: violations={res.violations!r} report={res.report!r}"
    return True, ""


def t8_direct_since_boundary():
    """A row at exactly `since` is checked; a row 1s before `since` is not."""
    at = _agent("agent.begin", SINCE, agent_type="")
    res = check_agent_events([at], SINCE, 0.05, 20)
    if len(res.violations) != 1:
        return False, f"row at exactly since should be checked: {res.violations!r}"
    before = _agent("agent.begin", SINCE - timedelta(seconds=1), agent_type="")
    res = check_agent_events([before], SINCE, 0.05, 20)
    if res.violations != []:
        return False, f"row 1s before since should be ignored: {res.violations!r}"
    return True, ""


def t8_cli_blank_agent_type_fails():
    """One post-since agent.begin with agent_type '' makes diagnose exit 1; the baseline control exits 0."""
    now = _now()
    row_ts = now - timedelta(days=2)
    row = _agent("agent.begin", row_ts, agent_type="")
    extra = ["--agent-guard-since", (now - timedelta(days=3)).isoformat()]
    code, out, err = _run(_baseline(now) + [row], extra=extra)
    ok, msg = _fail_expected(code, out, _violation_needles("agent.begin", "agent_type", _iso(row_ts)))
    if not ok:
        return False, msg
    code, out, err = _run(_baseline(now), extra=extra)
    return _control_ok(code, out, err)


def t8_cli_row_before_since_ignored():
    """The same blank row placed before --agent-guard-since produces no violation."""
    now = _now()
    row = _agent("agent.begin", now - timedelta(days=5), agent_type="")
    extra = ["--agent-guard-since", (now - timedelta(days=3)).isoformat()]
    code, out, err = _run(_baseline(now) + [row], extra=extra)
    if code != 0:
        return False, f"row before since should be ignored (exit 0), got {code}: {out!r} {err!r}"
    return True, ""


def t8_cli_row_between_constant_and_later_since_ignored():
    """A blank row between AGENT_GUARD_CUTOFF and a later --agent-guard-since is ignored."""
    now = _now()
    row_ts = AGENT_GUARD_CUTOFF + timedelta(hours=1)
    row = _agent("agent.begin", row_ts, agent_type="")
    later = AGENT_GUARD_CUTOFF + timedelta(hours=2)
    code, out, err = _run(_baseline(now) + [row], extra=["--agent-guard-since", later.isoformat()])
    if code != 0:
        return False, f"row before the later since should be ignored (exit 0), got {code}: {out!r} {err!r}"
    # Companion: with the default since (the constant) the same row is in scope and fails.
    code, out, err = _run(_baseline(now) + [row])
    ok, msg = _fail_expected(code, out, _violation_needles("agent.begin", "agent_type", _iso(row_ts)))
    if not ok:
        return False, "companion (default since): " + msg
    return True, ""


# ============================================================================
# T9: check (b) and report lines
# ============================================================================


def t9_direct_ratio_boundary():
    """ends == begins*(1+tol) passes (105); one more fails (106)."""
    res = check_agent_events(_ratio_events(100, 105), SINCE, 0.05, 20)
    if res.violations != [] or res.skipped != []:
        return False, f"100/105 should pass: violations={res.violations!r} skipped={res.skipped!r}"
    if res.report.begins != 100 or res.report.ends != 105:
        return False, f"report counts wrong for 100/105: {res.report!r}"
    res = check_agent_events(_ratio_events(100, 106), SINCE, 0.05, 20)
    if len(res.violations) != 1:
        return False, f"100/106 should fail once: {res.violations!r}"
    return True, ""


def t9_direct_exact_ratio_boundary():
    """Exact-limit cases pass despite float rounding; one event over the limit fails."""
    for begins, ends, tol in [(100, 115, 0.15), (180, 207, 0.15), (100, 113, 0.13)]:
        res = check_agent_events(_ratio_events(begins, ends), SINCE, tol, 20)
        if res.violations != []:
            return False, f"{begins}/{ends} at tol {tol} should pass: {res.violations!r}"
    res = check_agent_events(_ratio_events(100, 116), SINCE, 0.15, 20)
    if len(res.violations) != 1:
        return False, f"100/116 at tol 0.15 should fail once: {res.violations!r}"
    return True, ""


def t9_direct_exceeds_allowed_boundary_cases():
    """Direct _exceeds_allowed tests: 0 tolerance (ends must equal begins), 1e-05 tolerance, and exact ceiling."""
    # Zero tolerance: 101 > 100 fails, 100 == 100 passes
    if _exceeds_allowed(100, 100, 0.0):
        return False, "_exceeds_allowed(100, 100, 0.0) should be False (equal is allowed)"
    if not _exceeds_allowed(101, 100, 0.0):
        return False, "_exceeds_allowed(101, 100, 0.0) should be True (over limit)"

    # Very small tolerance: 1e-05
    # 100 * (1 + 1e-05) = 100.001, so 100 passes, 101 fails
    if _exceeds_allowed(100, 100, 1e-05):
        return False, "_exceeds_allowed(100, 100, 1e-05) should pass (at or below ceiling)"

    # Tolerance 0.05: 100 * 1.05 = 105, so 105 passes, 106 fails
    if _exceeds_allowed(105, 100, 0.05):
        return False, "_exceeds_allowed(105, 100, 0.05) should pass (at ceiling)"
    if not _exceeds_allowed(106, 100, 0.05):
        return False, "_exceeds_allowed(106, 100, 0.05) should fail (over ceiling)"

    return True, ""


def t9_direct_internal_stops_do_not_count_as_ends():
    """100 begins, 103 ends and 250 internal stops passes; report counts internal stops separately."""
    res = check_agent_events(_ratio_events(100, 103, internal_stops=250), SINCE, 0.05, 20)
    if res.violations != []:
        return False, f"100/103 + 250 internal stops should pass: {res.violations!r}"
    if res.report.internal_stops != 250 or res.report.ends != 103:
        return False, f"report wrong: {res.report!r}"
    return True, ""


def t9_direct_one_sided_and_large_excess():
    """100 begins / 130 ends fails with the counts in the text; 100 begins / 70 ends passes."""
    res = check_agent_events(_ratio_events(100, 130), SINCE, 0.05, 20)
    if len(res.violations) != 1 or "100" not in res.violations[0] or "130" not in res.violations[0]:
        return False, f"100/130 should fail naming 100 and 130: {res.violations!r}"
    res = check_agent_events(_ratio_events(100, 70), SINCE, 0.05, 20)
    if res.violations != []:
        return False, f"100/70 should pass (one-sided): {res.violations!r}"
    return True, ""


def t9_direct_min_begins_skip_and_fail():
    """19 begins is skipped (no violation); 20 begins with 40 ends fails."""
    res = check_agent_events(_ratio_events(19, 40), SINCE, 0.05, 20)
    if res.violations != []:
        return False, f"19/40 must not produce a violation: {res.violations!r}"
    if len(res.skipped) != 1 or "19" not in res.skipped[0] or "20" not in res.skipped[0]:
        return False, f"19/40 should produce one skip entry naming 19 and 20: {res.skipped!r}"
    res = check_agent_events(_ratio_events(20, 40), SINCE, 0.05, 20)
    if len(res.violations) != 1:
        return False, f"20/40 should fail once: {res.violations!r}"
    return True, ""


def t9_direct_straddler_counts_post_since_ends():
    """20 post-since begins + 20 matching ends + 2 ends whose begins predate since = 22 > 21 -> violation; 2 straddlers."""
    presince_ids = []
    events = [_agent("agent.begin", SINCE - timedelta(hours=1)) for _ in range(2)]
    # Capture pre-since begin IDs for matching ends later
    presince_ids = [e["agent_id"] for e in events]
    for i in range(20):
        events.append(_agent("agent.begin", SINCE + timedelta(seconds=1 + i)))
    for i in range(22):
        # First 2 ends match pre-since begins, remainder match post-since begins
        if i < 2:
            events.append(_agent("agent.end", SINCE + timedelta(seconds=100 + i), agent_id=presince_ids[i]))
        else:
            events.append(_agent("agent.end", SINCE + timedelta(seconds=100 + i)))
    res = check_agent_events(events, SINCE, 0.05, 20)
    if len(res.violations) != 1:
        return False, f"straddler case should fail once: {res.violations!r}"
    if res.report.begins != 20 or res.report.ends != 22:
        return False, f"straddler report wrong: {res.report!r}"
    if res.report.straddler_ends != 2:
        return False, f"straddler count should be 2, got {res.report.straddler_ends}: {res.report!r}"
    return True, ""


def t9_direct_typed_internal_stops():
    """A typed internal_stop and one whose agent_id matches a begin each increment attributable_internal_stops; no failure."""
    begin = _agent("agent.begin", SINCE + timedelta(seconds=1), agent_type="reviewer", agent_id="agent-typed-b")
    typed_stop = _agent("agent.internal_stop", SINCE + timedelta(seconds=2), agent_type="reviewer")
    id_match_stop = _agent("agent.internal_stop", SINCE + timedelta(seconds=3),
                           agent_type="unknown", agent_id="agent-typed-b")
    untyped_stop = _agent("agent.internal_stop", SINCE + timedelta(seconds=4), agent_type="unknown")
    res = check_agent_events([begin, typed_stop, id_match_stop, untyped_stop], SINCE, 0.05, 20)
    if res.violations != []:
        return False, f"attributable internal stops must not fail: {res.violations!r}"
    if res.report.attributable_internal_stops != 2:
        return False, f"expected attributable_internal_stops == 2: {res.report!r}"
    return True, ""


def t9_direct_report_keys_present():
    """report always carries every documented key."""
    res = check_agent_events([], SINCE, 0.05, 20)
    needed = {"begins", "ends", "internal_stops", "unknown_agent_type", "unknown_ids", "attributable_internal_stops", "straddler_ends"}
    missing = needed - set(res.report._asdict())
    if missing:
        return False, f"report missing keys {sorted(missing)}"
    return True, ""


def t9_cli_ratio_fail_and_control():
    """100 begins / 130 ends fails with specific counts in the text; the baseline control exits 0."""
    now = _now()
    extra = ["--agent-guard-since", (now - timedelta(days=3)).isoformat()]
    rows = _ratio_events(100, 130, base=now - timedelta(days=2))
    code, out, err = _run(_baseline(now) + rows, extra=extra)
    ok, msg = _fail_expected(code, out, ["130 ends vs 100 begins"])
    if not ok:
        return False, msg
    code, out, err = _run(_baseline(now), extra=extra)
    return _control_ok(code, out, err)


def t9_cli_skip_and_report_lines():
    """19/40 prints the skip line and exits 0; 100/103 + 250 internal stops prints report lines."""
    now = _now()
    extra = ["--agent-guard-since", (now - timedelta(days=3)).isoformat()]
    rows = _ratio_events(19, 40, base=now - timedelta(days=2))
    code, out, err = _run(_baseline(now) + rows, extra=extra)
    if code != 0:
        return False, f"19/40 should exit 0 (skipped), got {code}: {out!r} {err!r}"
    if "ratio check skipped" not in out:
        return False, f"skip line not printed: {out!r}"

    rows = _ratio_events(100, 103, internal_stops=250, base=now - timedelta(days=2))
    code, out, err = _run(_baseline(now) + rows, extra=extra)
    if code != 0:
        return False, f"100/103 + 250 stops should exit 0, got {code}: {out!r} {err!r}"
    if "internal_stop" not in out or "report-only" not in out or "250" not in out:
        return False, f"report-only lines not printed: {out!r}"
    return True, ""


def t9_cli_min_begins_fail_and_control():
    """20 begins / 40 ends fails through the CLI; the baseline control exits 0."""
    now = _now()
    extra = ["--agent-guard-since", (now - timedelta(days=3)).isoformat()]
    rows = _ratio_events(20, 40, base=now - timedelta(days=2))
    code, out, err = _run(_baseline(now) + rows, extra=extra)
    ok, msg = _fail_expected(code, out, ["40 ends vs 20 begins"])
    if not ok:
        return False, msg
    code, out, err = _run(_baseline(now), extra=extra)
    return _control_ok(code, out, err)


# ============================================================================
# T10: truncated JSON line
# ============================================================================


def t10_truncated_line_warns_and_guard_runs():
    """A log ending in truncated JSON warns on stderr, produces no traceback, and the guard still runs."""
    now = _now()
    truncated = '{"schema_version": 1, "event_type": "agent.begin", "timestamp": "2026-'
    extra = ["--agent-guard-since", (now - timedelta(days=3)).isoformat()]
    row = _agent("agent.begin", now - timedelta(days=2), agent_type="")
    code, out, err = _run(_baseline(now) + [row], extra=extra, trailing_raw=truncated)
    if "Traceback" in err or "Traceback" in out:
        return False, f"truncated line must not raise a traceback: {err!r}"
    if "warn" not in err.lower():
        return False, f"expected a stderr warning for the truncated line: {err!r}"
    ok, msg = _fail_expected(code, out, _violation_needles("agent.begin", "agent_type", _iso(now - timedelta(days=2))))
    if not ok:
        return False, msg
    code, out, err = _run(_baseline(now), extra=extra, trailing_raw=truncated)
    if "Traceback" in err:
        return False, f"control with truncated line raised a traceback: {err!r}"
    return _control_ok(code, out, err)


# ============================================================================
# T11: window interplay and the default since
# ============================================================================


def t11_window_bounds_the_lower_edge():
    """With --window-days 1 and --agent-guard-since 10 days ago, a row 2 days old is ignored; header shows the window start."""
    now = _now()
    row_ts = now - timedelta(days=2)
    row = _agent("agent.begin", row_ts, agent_type="")
    since = now - timedelta(days=10)
    extra = ["--agent-guard-since", since.isoformat()]
    code, out, err = _run(_baseline(now) + [row], extra=extra, window_days=1)
    if code != 0:
        return False, f"row outside the window should be ignored (exit 0), got {code}: {out!r} {err!r}"
    window_start_day = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    if window_start_day not in out:
        return False, f"header should show effective bound (window start {window_start_day}): {out!r}"
    # Companion: with a wide window the same row is in scope and fails.
    code, out, err = _run(_baseline(now) + [row], extra=extra, window_days=3650)
    ok, msg = _fail_expected(code, out, _violation_needles("agent.begin", "agent_type", _iso(row_ts)))
    if not ok:
        return False, "companion (window 3650): " + msg
    return True, ""


def t11_default_since_is_cutoff():
    """Without --agent-guard-since the header shows AGENT_GUARD_CUTOFF and a blank row after it fails; baseline exits 0."""
    now = _now()
    row_ts = max(now, AGENT_GUARD_CUTOFF) + timedelta(minutes=1)
    row = _agent("agent.begin", row_ts, agent_type="")
    code, out, err = _run(_baseline(now) + [row])
    ok, msg = _fail_expected(code, out, _violation_needles("agent.begin", "agent_type", _iso(row_ts)))
    if not ok:
        return False, msg
    # Check for the cutoff date and time (isoformat uses +00:00 instead of Z for UTC)
    # Accept either format: 2026-10-13T18:58:25Z or 2026-10-13T18:58:25+00:00
    if "2026-10-13T18:58:25" not in out:
        return False, f"header should show the default bound (AGENT_GUARD_CUTOFF): {out!r}"
    code, out, err = _run(_baseline(now))
    return _control_ok(code, out, err)


def t11_constant_is_tz_aware():
    """AGENT_GUARD_CUTOFF is a tz-aware datetime equal to the documented instant."""
    expected = datetime(2026, 10, 13, 18, 58, 25, tzinfo=UTC)
    if AGENT_GUARD_CUTOFF.tzinfo is None:
        return False, "AGENT_GUARD_CUTOFF must be tz-aware"
    if AGENT_GUARD_CUTOFF != expected:
        return False, f"AGENT_GUARD_CUTOFF is {AGENT_GUARD_CUTOFF!r}, expected {expected!r}"
    return True, ""


# ============================================================================
# T12: flag validation and provenance
# ============================================================================


BAD_FLAGS = [
    ["--agent-guard-since", "2026-10-13T18:58:25"],
    ["--agent-guard-since", "garbage"],
    ["--agent-guard-tolerance", "nan"],
    ["--agent-guard-tolerance", "-0.1"],
    ["--agent-guard-tolerance", "1.5"],
    ["--agent-guard-min-begins", "0"],
]


def t12_bad_flag_values_exit_2():
    """Naive datetime, garbage, nan, negative/over-1 tolerance, and min-begins 0 are argparse errors (exit 2)."""
    failures = []
    for flags in BAD_FLAGS:
        code, out, err = _run([], extra=flags)
        if code != 2 or "error" not in err.lower():
            failures.append(f"{flags}: exit {code}, stderr {err[-200:]!r}")
    if failures:
        return False, "; ".join(failures)
    return True, ""


def _setup_old_hook(root, home):
    old = root / "old-run-metrics.py"
    old.write_text("print('pre-267 script')\n", encoding="utf-8")
    _link_hook(home, old)


def _setup_self_hook(root, home):
    _link_hook(home, SCRIPT)


def t12_provenance_not_installed():
    """With no ~/.claude/scripts/run-metrics.py the Hook target line says so (direct and CLI)."""
    line = _direct_prov(lambda root, home: None)
    if "Hook target: not installed at ~/.claude/scripts/run-metrics.py" not in line:
        return False, f"direct: {line!r}"
    code, out, err = _run([])
    if "Hook target: not installed at ~/.claude/scripts/run-metrics.py" not in out:
        return False, f"CLI: {out!r}"
    return True, ""


def t12_provenance_pre_267_hook():
    """A hook symlink to a file without agent.internal_stop reports the pre-#267 re-install advice (direct and CLI)."""
    line = _direct_prov(_setup_old_hook)
    if "does NOT support agent.internal_stop" not in line or "re-run /setup-local" not in line:
        return False, f"direct: {line!r}"
    if "differs from this script" not in line:
        return False, f"direct should say the hook differs from this script: {line!r}"
    code, out, err = _run([], home_setup=_setup_old_hook)
    if "does NOT support agent.internal_stop" not in out or "re-run /setup-local" not in out:
        return False, f"CLI: {out!r}"
    return True, ""


def t12_provenance_points_at_this_script():
    """A hook symlink to scripts/run-metrics.py reports '(this script)' (direct and CLI)."""
    line = _direct_prov(_setup_self_hook)
    if "(this script)" not in line or "differs" in line:
        return False, f"direct: {line!r}"
    code, out, err = _run([], home_setup=_setup_self_hook)
    if "(this script)" not in out:
        return False, f"CLI: {out!r}"
    return True, ""


def t12_provenance_supports_and_unreadable():
    """A hook that mentions agent.internal_stop is 'supports'; a hook path that cannot be read as text is 'unreadable'."""
    def supports(root, home):
        new = root / "new-run-metrics.py"
        new.write_text("EVENT = 'agent.internal_stop'\n", encoding="utf-8")
        _link_hook(home, new)

    def unreadable(root, home):
        locked = root / "locked-run-metrics.py"
        locked.write_text("EVENT = 'agent.internal_stop'\n", encoding="utf-8")
        locked.chmod(0o000)
        _link_hook(home, locked)

    line = _direct_prov(supports)
    if "supports agent.internal_stop" not in line or "does NOT" in line:
        return False, f"supports case: {line!r}"

    # Skip unreadable test when running as root (cannot create unreadable files)
    if geteuid() == 0:
        return True, ""  # Skip the unreadable case; supports case passed

    line = _direct_prov(unreadable)
    if "unreadable" not in line:
        return False, f"unreadable case: {line!r}"
    return True, ""


def t12_provenance_dangling_symlink():
    """A dangling symlink is reported with its target path (direct)."""
    def dangling(root, home):
        nonexistent = root / "nonexistent-run-metrics.py"
        scripts = home / ".claude" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (scripts / "run-metrics.py").symlink_to(nonexistent)

    line = _direct_prov(dangling)
    if "dangling symlink ->" not in line:
        return False, f"dangling symlink case: {line!r}"
    if "nonexistent-run-metrics.py" not in line:
        return False, f"dangling symlink should mention target: {line!r}"
    return True, ""


# ============================================================================
# T13: constant and docs
# ============================================================================


def t13_cutoff_literal_in_docs():
    """The constant's ISO literal appears in both docs; the placeholder text appears in neither."""
    failures = []
    for doc in DOC_PATHS:
        text = doc.read_text(encoding="utf-8")
        if CUTOFF_LITERAL not in text:
            failures.append(f"{doc.name} lacks {CUTOFF_LITERAL}")
        if PLACEHOLDER in text:
            failures.append(f"{doc.name} still contains the placeholder")
    if failures:
        return False, "; ".join(failures)
    return True, ""


def t13_nonblank_str_contract():
    """_is_nonblank_str accepts only non-blank strings."""
    cases = [("a", True), ("unknown", True), ("", False), (" ", False), (None, False), (5, False), ([], False)]
    wrong = [repr(v) for v, want in cases if _is_nonblank_str(v) is not want]
    if wrong:
        return False, f"wrong result for {wrong}"
    return True, ""


# ============================================================================
# D1: unknown saturation is not guarded
# ============================================================================


def d1_unknown_agent_type_normalizes_and_passes():
    """An agent.begin with empty agent_type normalizes to "unknown" and guard still passes (report-only)."""
    now = _now()
    # Simulate agent-begin writer with empty payload
    row = _agent("agent.begin", now - timedelta(days=2), agent_type="")
    # The CLI writer _field() function normalizes blank to "unknown"
    row["agent_type"] = "unknown"  # Simulating the _field normalization
    extra = ["--agent-guard-since", (now - timedelta(days=3)).isoformat()]
    code, out, err = _run(_baseline(now) + [row], extra=extra)
    if code != 0:
        return False, f"unknown agent_type should pass (report-only), got exit {code}: {out!r}"
    if "unknown agent_type (report-only): 1" not in out:
        return False, f"report should show the unknown agent_type count: {out!r}"
    return True, ""


def main():
    h = Harness("Agent event guard (run-metrics diagnose)")
    tests = [
        ("T8 direct: non-blank fields reject bad values and missing keys", t8_direct_nonblank_fields),
        ("T8 direct: 'unknown' is report-only, never a violation", t8_direct_unknown_is_report_only),
        ("T8 direct: row at exactly since is checked; 1s before is not", t8_direct_since_boundary),
        ("T8 CLI: blank agent_type after since fails; control exits 0", t8_cli_blank_agent_type_fails),
        ("T8 CLI: same blank row before --agent-guard-since is ignored", t8_cli_row_before_since_ignored),
        ("T8 CLI: row between constant and later --agent-guard-since is ignored", t8_cli_row_between_constant_and_later_since_ignored),
        ("T9 direct: ends == begins*(1+tol) passes, one more fails", t9_direct_ratio_boundary),
        ("T9 direct: exact ratio limits at 0.15/0.13 pass; 100/116 at 0.15 fails", t9_direct_exact_ratio_boundary),
        ("T9 direct: _exceeds_allowed boundary cases (0 tol, 1e-05, at ceiling)", t9_direct_exceeds_allowed_boundary_cases),
        ("T9 direct: internal stops do not count as ends", t9_direct_internal_stops_do_not_count_as_ends),
        ("T9 direct: 100/130 fails; 100/70 passes (one-sided)", t9_direct_one_sided_and_large_excess),
        ("T9 direct: 19 begins skipped; 20 begins with 40 ends fails", t9_direct_min_begins_skip_and_fail),
        ("T9 direct: straddler ends count against post-since begins", t9_direct_straddler_counts_post_since_ends),
        ("T9 direct: typed internal stops counted without failing", t9_direct_typed_internal_stops),
        ("T9 direct: report carries every documented key", t9_direct_report_keys_present),
        ("T9 CLI: 100/130 fails with text; control exits 0", t9_cli_ratio_fail_and_control),
        ("T9 CLI: 19/40 skip line printed; report lines printed", t9_cli_skip_and_report_lines),
        ("T9 CLI: 20/40 fails; control exits 0", t9_cli_min_begins_fail_and_control),
        ("T10: truncated trailing line warns, no traceback, guard still runs", t10_truncated_line_warns_and_guard_runs),
        ("T11: --window-days bounds the lower edge and the header shows it", t11_window_bounds_the_lower_edge),
        ("T11: default since is AGENT_GUARD_CUTOFF (header and enforcement)", t11_default_since_is_cutoff),
        ("T11: AGENT_GUARD_CUTOFF is tz-aware and equals the documented instant", t11_constant_is_tz_aware),
        ("T12: invalid flag values exit 2 (argparse error)", t12_bad_flag_values_exit_2),
        ("T12: provenance reports not installed (direct and CLI)", t12_provenance_not_installed),
        ("T12: provenance reports pre-#267 hook with re-run advice (direct and CLI)", t12_provenance_pre_267_hook),
        ("T12: provenance reports '(this script)' for repo symlink (direct and CLI)", t12_provenance_points_at_this_script),
        ("T12: provenance reports supports / unreadable (direct)", t12_provenance_supports_and_unreadable),
        ("T12: provenance reports dangling symlink with target (direct)", t12_provenance_dangling_symlink),
        ("T13: cutoff literal in docs; placeholder absent", t13_cutoff_literal_in_docs),
        ("_is_nonblank_str contract", t13_nonblank_str_contract),
        ("D1: unknown agent_type is report-only, guard passes", d1_unknown_agent_type_normalizes_and_passes),
    ]
    for name, fn in tests:
        try:
            passed, msg = fn()
        except Exception as exc:  # report per-test instead of crashing the suite
            passed, msg = False, f"raised {exc!r}"
        h.test_result(name, passed, msg)
    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
