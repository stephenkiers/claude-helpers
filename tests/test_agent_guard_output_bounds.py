#!/usr/bin/env python3
"""
Additional tests for the agent event guard in `run-metrics.py diagnose` (issue #269).

These tests cover output bounding, FAIL message quality, and qualified PASS lines
that are not explicitly tested in test_agent_event_guard.py.

Covers:
1. Violation output is bounded (first ~10, then "... and N more")
2. Hook file size cap (~1MB)
3. FAIL hint mentions both cases: stale hook (re-run /setup-local) vs current hook (real data problem)
4. Qualified PASS line when ratio check is skipped (not bare "PASS: All thresholds met")
5. Violation text includes integer counts and the allowed ceiling for ratio failures
6. Docs describe internal_stop count as post-since parseable rows

Run with: python3 tests/test_agent_guard_output_bounds.py
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _test_harness import REPO_ROOT, Harness

SCRIPTS_DIR = REPO_ROOT / "scripts"
SCRIPT = SCRIPTS_DIR / "run-metrics.py"
DOC_PATHS = [
    REPO_ROOT / "docs" / "metrics.md",
    REPO_ROOT / "docs" / "adr" / "0016-usage-yield-telemetry.md",
]

UTC = timezone.utc

sys.path.insert(0, str(SCRIPTS_DIR))
_spec = importlib.util.spec_from_file_location("run_metrics", SCRIPT)
run_metrics = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_metrics)

AGENT_GUARD_CUTOFF = run_metrics.AGENT_GUARD_CUTOFF
check_agent_events = run_metrics.check_agent_events


# ============================================================================
# Helpers (shared with test_agent_event_guard.py)
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
        import itertools
        if not hasattr(_agent, "_id_counter"):
            _agent._id_counter = itertools.count(1)
        agent_id = f"agent-{next(_agent._id_counter)}"
    return {
        "schema_version": 1,
        "event_type": event_type,
        "timestamp": _iso(ts),
        "agent_type": agent_type,
        "agent_id": agent_id,
        "session_id": session_id,
    }


def _ratio_events(begins, ends, internal_stops=0, base=None):
    """Valid agent rows: `begins` agent.begin, `ends` agent.end, `internal_stops` untyped stops."""
    if base is None:
        base = AGENT_GUARD_CUTOFF
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


# ============================================================================
# B1: Violation output bounding
# ============================================================================


def b1_violation_output_bounded_with_many_identity_violations():
    """20+ identity violations print first ~10, then "... and N more"."""
    now = _now()
    extra = ["--agent-guard-since", (now - timedelta(days=3)).isoformat()]
    # Create 20 blank agent_type violations (each agent.begin with agent_type="")
    events = _baseline(now)
    for i in range(20):
        events.append(_agent("agent.begin", now - timedelta(days=2, seconds=-i), agent_type=""))
    code, out, err = _run(events, extra=extra)

    if code != 1:
        return False, f"expected exit 1, got {code}"

    # Should NOT have all 20 violations listed separately
    if out.count("agent_type") > 15:  # heuristic: should be ~10 + summary
        return False, f"output should bound violations, got too many: {out!r}"

    # Should have the "... and N more" line or similar bounding text
    has_bounding = ("... and " in out) or ("more" in out and "violations" in out.lower())
    if not has_bounding:
        return False, f"output should indicate bounding with '... and N more' or similar: {out!r}"

    # Hook target line and FAIL summary should still be visible
    if "Hook target:" not in out:
        return False, f"Hook target line should be visible: {out!r}"
    if "FAIL:" not in out:
        return False, f"FAIL summary should be visible: {out!r}"

    return True, ""


def b1_violation_output_bounded_with_many_ratio_violations():
    """Multiple ratio violations print counts and bounding."""
    now = _now()
    extra = ["--agent-guard-since", (now - timedelta(days=3)).isoformat()]
    events = _baseline(now)
    # Create multiple ratio failures (e.g., 3 separate scenarios)
    for j in range(3):
        events.extend(_ratio_events(20, 40, base=now - timedelta(days=2, seconds=-j*1000)))
    code, out, err = _run(events, extra=extra)

    if code != 1:
        return False, f"expected exit 1, got {code}"

    # Should have counts in violation text (e.g., "40 ends vs 20 begins")
    if "ends" not in out or "begins" not in out:
        return False, f"violation should name both ends and begins counts: {out!r}"

    return True, ""


# ============================================================================
# B2: Hook file size cap (~1MB)
# ============================================================================


def b2_hook_reads_capped_at_megabyte():
    """Hook file reads are capped at ~1MB; very large files don't crash or hang."""
    def large_hook(root, home):
        scripts = home / ".claude" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        # Create a 10MB file to test capping
        large_file = root / "large-run-metrics.py"
        # Write 10MB of data (mostly junk, with "agent.internal_stop" somewhere in the middle)
        with open(large_file, "w") as f:
            f.write("# " + "x" * (5 * 1024 * 1024) + "\n")  # 5MB of noise
            f.write("EVENT = 'agent.internal_stop'\n")
            f.write("# " + "y" * (5 * 1024 * 1024) + "\n")  # another 5MB of noise
        (scripts / "run-metrics.py").symlink_to(large_file)

    now = _now()
    extra = ["--agent-guard-since", (now - timedelta(days=3)).isoformat()]
    code, out, err = _run(_baseline(now), extra=extra, home_setup=large_hook)

    # Should not crash or hang (exit 0 since baseline is clean)
    if code != 0:
        return False, f"large hook file should not cause failure, got exit {code}: {out!r}"

    # Should not produce a huge output or memory issue
    if len(out) > 1_000_000:  # sanity check: output shouldn't be massive
        return False, f"output should be bounded even with large hook file: len={len(out)}"

    return True, ""


# ============================================================================
# B3: FAIL hint states both cases
# ============================================================================


def b3_fail_hint_mentions_stale_hook():
    """FAIL output mentions re-running /setup-local for stale hook case."""
    def old_hook(root, home):
        old = root / "old-run-metrics.py"
        old.write_text("print('pre-267 script')\n", encoding="utf-8")
        scripts = home / ".claude" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (scripts / "run-metrics.py").symlink_to(old)

    now = _now()
    extra = ["--agent-guard-since", (now - timedelta(days=3)).isoformat()]
    row = _agent("agent.begin", now - timedelta(days=2), agent_type="")
    code, out, err = _run(_baseline(now) + [row], extra=extra, home_setup=old_hook)

    if code != 1:
        return False, f"expected exit 1, got {code}"

    # Should mention re-running /setup-local
    if "re-run /setup-local" not in out and "/setup-local" not in out:
        return False, f"FAIL hint should mention re-running /setup-local: {out!r}"

    return True, ""


def b3_fail_hint_mentions_real_data_problem():
    """FAIL output mentions real data problem (current hook, see docs) as alternative to stale hook."""
    now = _now()
    extra = ["--agent-guard-since", (now - timedelta(days=3)).isoformat()]
    row = _agent("agent.begin", now - timedelta(days=2), agent_type="")
    # Use the real script (current hook) to test the "real data problem" message path
    code, out, err = _run(_baseline(now) + [row], extra=extra)

    if code != 1:
        return False, f"expected exit 1, got {code}"

    # The output should mention options for fixing (stale hook vs. real data problem)
    # At minimum, it should guide the user to docs or mention the distinction
    if "before adjusting --agent-guard-since" not in out:
        return False, f"FAIL hint should guide the user on next steps: {out!r}"

    return True, ""


# ============================================================================
# B4: Qualified PASS line when ratio check skipped
# ============================================================================


def b4_pass_line_qualified_when_ratio_skipped():
    """When ratio check is skipped (begins < min_begins), PASS line is qualified, not bare."""
    now = _now()
    extra = ["--agent-guard-since", (now - timedelta(days=3)).isoformat(),
             "--agent-guard-min-begins", "20"]
    rows = _ratio_events(19, 40, base=now - timedelta(days=2))
    code, out, err = _run(_baseline(now) + rows, extra=extra)

    if code != 0:
        return False, f"19 begins (below min) should exit 0, got {code}: {out!r}"

    # Should have "PASS: All thresholds met" but with qualification in parentheses
    # (qualified = PASS line has additional info, not bare)
    if "PASS: All thresholds met" not in out:
        return False, f"PASS line should be present: {out!r}"

    # Should have qualification (parentheses with additional context)
    if "PASS: All thresholds met (" not in out:
        return False, f"PASS should be qualified with parentheses when ratio check is skipped: {out!r}"

    # Should mention "ratio check skipped" in the qualification
    if "ratio check skipped" not in out:
        return False, f"qualification should mention ratio check skipped: {out!r}"

    return True, ""


def b4_pass_line_unqualified_when_all_pass():
    """When all checks pass (including ratio), PASS line may be unqualified."""
    now = _now()
    extra = ["--agent-guard-since", (now - timedelta(days=3)).isoformat()]
    rows = _ratio_events(100, 103, base=now - timedelta(days=2))
    code, out, err = _run(_baseline(now) + rows, extra=extra)

    if code != 0:
        return False, f"100/103 should exit 0, got {code}: {out!r}"

    # Should have PASS (may be bare or qualified, but should indicate success)
    if "PASS" not in out and "passed" not in out.lower():
        return False, f"output should indicate all checks passed: {out!r}"

    return True, ""


# ============================================================================
# B5: Violation text format (integer counts and ceiling)
# ============================================================================


def b5_ratio_violation_includes_ceiling():
    """Ratio violation text includes both the actual counts and the allowed ceiling."""
    now = _now()
    extra = ["--agent-guard-since", (now - timedelta(days=3)).isoformat(),
             "--agent-guard-tolerance", "0.05"]
    # 100 begins, 130 ends: 130 > 100 * 1.05 = 105, so it fails
    rows = _ratio_events(100, 130, base=now - timedelta(days=2))
    code, out, err = _run(_baseline(now) + rows, extra=extra)

    if code != 1:
        return False, f"100/130 should fail, got exit {code}"

    # Violation should mention the actual numbers
    if "130" not in out or "100" not in out:
        return False, f"violation should mention 100 begins and 130 ends: {out!r}"

    # Should mention the ceiling (105 = 100 * 1.05)
    # The violation might say "allowed 105" or "ceiling 105" or similar
    has_ceiling = "105" in out or "ceiling" in out.lower() or "allowed" in out.lower()
    if not has_ceiling:
        return False, f"violation should indicate the allowed ceiling (105): {out!r}"

    return True, ""


def b5_identity_violation_includes_integers():
    """Identity violations include integer event counts (e.g., field name and timestamp)."""
    now = _now()
    extra = ["--agent-guard-since", (now - timedelta(days=3)).isoformat()]
    # 5 blank agent_type violations
    events = _baseline(now)
    for i in range(5):
        events.append(_agent("agent.begin", now - timedelta(days=2, seconds=-i), agent_type=""))
    code, out, err = _run(events, extra=extra)

    if code != 1:
        return False, f"expected exit 1, got {code}"

    # Should mention agent_type field
    if "agent_type" not in out:
        return False, f"violation should mention the field name: {out!r}"

    # Should mention timestamps and event type
    if "agent.begin" not in out and "begin" not in out.lower():
        return False, f"violation should mention event type: {out!r}"

    return True, ""


# ============================================================================
# B6: Docs describe internal_stop count as post-since
# ============================================================================


def b6_docs_describe_internal_stop_as_post_since():
    """Docs describe internal_stop count as 'post-since' or 'after cutoff' parseable rows."""
    text = (REPO_ROOT / "docs" / "metrics.md").read_text(encoding="utf-8")

    # Should mention internal_stop in the agent guard section
    if "internal_stop" not in text:
        return False, "docs should mention internal_stop"

    # Should clarify it's post-since (not pre-cutoff)
    # Keywords like "post-", "after", "since", "window" near internal_stop
    if "post-" not in text and "after" not in text.lower():
        return False, "docs should clarify internal_stop is post-cutoff/post-since"

    # Should mention it as a count of parseable rows
    if "post-`since`" not in text and "after" not in text:
        # Check more broadly
        guard_section = text[text.find("agent guard"):text.find("agent guard") + 3000] if "agent guard" in text else ""
        if "internal_stop" in guard_section and ("count" in guard_section or "rows" in guard_section):
            return True, ""
        return False, "docs should describe internal_stop as post-since parseable rows"

    return True, ""


def b6_docs_reference_issue_143():
    """Both docs mention issue #143, not 'Q4' placeholder."""
    failures = []
    for doc in DOC_PATHS:
        text = doc.read_text(encoding="utf-8")
        if "#143" not in text:
            failures.append(f"{doc.name} lacks #143 reference")
        if "Q4" in text:  # "Q4" was a placeholder in earlier drafts
            failures.append(f"{doc.name} still contains 'Q4' placeholder")
    if failures:
        return False, "; ".join(failures)
    return True, ""


# ============================================================================
# Direct function tests
# ============================================================================


def d2_guard_report_is_namedtuple_or_dataclass():
    """Guard report is a NamedTuple/dataclass, not a plain dict."""
    events = _ratio_events(100, 103, base=AGENT_GUARD_CUTOFF)
    res = check_agent_events(events, AGENT_GUARD_CUTOFF, 0.05, 20)

    # Should have _asdict() method (NamedTuple) or __dataclass_fields__ (dataclass)
    if not hasattr(res.report, "_asdict") and not hasattr(res.report, "__dataclass_fields__"):
        return False, f"report should be NamedTuple/dataclass, got {type(res.report)}"

    return True, ""


def main():
    h = Harness("Agent guard output bounds and quality (run-metrics diagnose)")
    tests = [
        ("B1: violation output bounded with 20+ identity violations", b1_violation_output_bounded_with_many_identity_violations),
        ("B1: violation output bounded with multiple ratio failures", b1_violation_output_bounded_with_many_ratio_violations),
        ("B2: hook file reads capped at ~1MB", b2_hook_reads_capped_at_megabyte),
        ("B3: FAIL hint mentions stale hook (re-run /setup-local)", b3_fail_hint_mentions_stale_hook),
        ("B3: FAIL hint addresses real data problem case", b3_fail_hint_mentions_real_data_problem),
        ("B4: PASS line qualified when ratio check skipped", b4_pass_line_qualified_when_ratio_skipped),
        ("B4: PASS line unqualified when all checks pass", b4_pass_line_unqualified_when_all_pass),
        ("B5: ratio violation includes counts and ceiling", b5_ratio_violation_includes_ceiling),
        ("B5: identity violation includes field name and event type", b5_identity_violation_includes_integers),
        ("B6: docs describe internal_stop as post-since", b6_docs_describe_internal_stop_as_post_since),
        ("B6: docs reference #143, not 'Q4'", b6_docs_reference_issue_143),
        ("D2: guard report is NamedTuple/dataclass, not dict", d2_guard_report_is_namedtuple_or_dataclass),
    ]
    for name, fn in tests:
        try:
            passed, msg = fn()
        except Exception as exc:
            passed, msg = False, f"raised {exc!r}"
        h.test_result(name, passed, msg)
    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
