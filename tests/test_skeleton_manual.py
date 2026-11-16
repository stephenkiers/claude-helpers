#!/usr/bin/env python3
"""Tests for scripts/skeleton-manual.py: collect on a synthetic flow dir, double-spawn and orphan logic, and
teardown refusing non-skeleton paths. Everything runs under a temp HOME; the real HOME is never touched.

Run with: python3 tests/test_skeleton_manual.py
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import REPO_ROOT, Harness

_spec = importlib.util.spec_from_file_location("skeleton_manual", REPO_ROOT / "scripts" / "skeleton-manual.py")
sm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sm)

h = Harness("skeleton-manual tests")


def jl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def make_flow(home: Path, name="skeleton-20261112T000000Z-aa") -> Path:
    flow = home / ".claude" / "flows-v2" / "skeleton_measure" / name
    (flow / "receipts").mkdir(parents=True)
    return flow


def run(argv, home: Path):
    old = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = sm.main(argv)
    finally:
        if old is None:
            del os.environ["HOME"]
        else:
            os.environ["HOME"] = old
    return rc, out.getvalue(), err.getvalue()


with tempfile.TemporaryDirectory() as tmp:
    home = Path(tmp).resolve()
    flow = make_flow(home)
    jl(flow / "spawns.jsonl", [
        {"step": "skeleton", "attempt": 1, "utc": "2026-11-12T20:00:00Z"},
        {"step": "skeleton", "attempt": 1, "utc": "2026-11-12T20:00:05Z"},
        {"step": "skeleton", "attempt": 2, "utc": "2026-11-12T20:01:00Z"},
    ])
    jl(flow / "driver.log", [
        {"verb": "SPAWN", "attempt": 1, "after": "start", "events": []},
        {"verb": "WAIT", "attempt": 1, "after": "spawn-launched", "events": []},
        {"verb": "WAIT", "attempt": 1, "after": "notification", "events": []},
        {"subcommand": "clear-live", "reason": "x"},
        {"verb": "SPAWN", "attempt": 2, "after": "start", "events": [{"event": "late-receipt", "detail": "d"}]},
    ])
    (flow / "pending_spawn.json").write_text(json.dumps({"state": "spawned", "attempt": 3, "nonce": "n"}))
    (flow / "heartbeat-a1").write_text("2026-11-12T20:00:00Z\ndone\n")
    (flow / "heartbeat-a2").write_text("2026-11-12T20:01:00Z\n")
    old_t = time.mktime(time.strptime("2026-11-12T20:00:00", "%Y-%m-%dT%H:%M:%S"))
    os.utime(flow / "heartbeat-a1", (time.time(), time.time()))  # written "now", far after a2's spawn line
    os.utime(flow / "heartbeat-a2", (old_t, old_t))
    (flow / "receipts" / "skeleton-a2.md").write_text("Decision: done\n")

    a = sm.analyze(flow)
    h.test_result("double spawn flagged for attempt 1 only", a["double_spawn"] == ["skeleton/a1"], str(a["double_spawn"]))
    h.test_result("spawn counts", a["spawn_counts"] == {"skeleton/a1": 2, "skeleton/a2": 1})
    h.test_result("verb counts", a["verbs"] == {"SPAWN": 2, "WAIT": 2})
    h.test_result("excess WAIT counts only non-called-for --after", a["excess"]["WAIT"] == 1, str(a["excess"]))
    h.test_result("clear-live counted", a["clear_live"] == 1)
    h.test_result("phantom-live: attempt 3 has no spawn line and no receipt", a["phantom_live"] is True)
    h.test_result("orphan overlap found for a1", len(a["orphans"]) == 1 and "heartbeat-a1" in a["orphans"][0], str(a["orphans"]))
    h.test_result("heartbeat done detected", a["heartbeats"]["heartbeat-a1"]["done"] and not a["heartbeats"]["heartbeat-a2"]["done"])

    rc, out, _ = run(["collect", str(flow)], home)
    h.test_result("collect exits 0", rc == 0)
    h.test_result("collect scrubs home", str(home) not in out and "~/.claude/flows-v2" in out, out[:200])
    h.test_result("collect lists findings ids", "PHANTOM-LIVE" in out and "AC1-SPAWN" in out)
    h.test_result("collect prints receipt", "Decision: done" in out)
    rc, out2, _ = run(["collect", "--latest"], home)
    h.test_result("collect --latest finds the flow", rc == 0 and "skeleton-20261112T000000Z-aa" in out2)

    clean = make_flow(home, "skeleton-20261112T000001Z-bb")
    jl(clean / "spawns.jsonl", [{"step": "skeleton", "attempt": 1, "utc": "2026-11-12T20:00:00Z"}])
    c = sm.analyze(clean)
    h.test_result("clean flow: no double spawn, no orphan, no phantom",
                  not c["double_spawn"] and not c["orphans"] and not c["phantom_live"])

    # teardown refuses non-skeleton paths
    other = home / ".claude" / "flows-v2" / "skeleton_measure" / "keepme"
    other.mkdir()
    for bad in (other, home / ".claude" / "flows-v2" / "skeleton-x", home / "skeleton-y"):
        try:
            sm.check_removable_flow(home, bad)
            h.test_result(f"refuses {bad.name}", False, "no refusal")
        except sm.SkeletonError:
            h.test_result(f"refuses {bad.name}", True)
    h.test_result("skeleton_flow_dirs lists only skeleton-* dirs",
                  sorted(p.name for p in sm.skeleton_flow_dirs(home)) == ["skeleton-20261112T000000Z-aa", "skeleton-20261112T000001Z-bb"])

    # teardown removes only skeleton-* dirs and the correct symlinks
    for link, target in sm.link_targets(home):
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(str(target), str(link))
    rc, out, err = run(["teardown"], home)
    h.test_result("teardown exits 0", rc == 0, err)
    h.test_result("teardown removed skeleton dirs, kept other", not flow.exists() and not clean.exists() and other.exists())
    h.test_result("teardown removed symlinks", not any(os.path.lexists(str(lk)) for lk, _t in sm.link_targets(home)))

    # teardown refuses a wrong symlink and does not remove it
    link0 = sm.link_targets(home)[0][0]
    os.symlink(str(home), str(link0))
    rc, _, err = run(["teardown"], home)
    h.test_result("teardown refuses a foreign symlink", rc == 1 and os.path.islink(str(link0)), err)

    # setup refuses an existing target
    rc, _, err = run(["setup"], home)
    h.test_result("setup refuses when a target exists", rc == 1 and "already exists" in err)

    rc, out, _ = run(["cells"], home)
    h.test_result("cells mentions the flags and every cell",
                  rc == 0 and "--mode interactive --wait-mode end-turn --step-minutes 1" in out
                  and "K1-interrupt" in out and "K2-interrupt" in out and "R6" in out)

h.summarize_and_exit()
