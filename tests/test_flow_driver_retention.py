#!/usr/bin/env python3
"""
Tests for scripts/flow_driver/retention.py.

Tests retention behavior: live flows are never deleted, v1-root flows are never deleted,
expired flows are removed but human.jsonl, steps/, and receipts survive, and retention
errors do not crash the driver.

Run with: python3 tests/test_flow_driver_retention.py
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from _flow_driver_loop_helpers import make_home, run_cli  # noqa: E402
from _test_harness import Harness  # noqa: E402

from scripts.flow_driver import retention  # noqa: E402


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check(h: Harness, name: str, fn: Callable[[], None]) -> None:
    """Run one named check. A crash inside a check is a named failure, not a suite abort."""
    try:
        fn()
    except AssertionError as exc:
        h.test_result(name, False, str(exc) or "assertion failed")
    except Exception as exc:
        h.test_result(name, False, f"{type(exc).__name__}: {exc}")
    else:
        h.test_result(name, True)


def test_retention_survives_missing_flows_dir() -> None:
    """retention.sweep() handles missing flows directory without crashing. [WEAK_ASSERTION]"""
    with tempfile.TemporaryDirectory() as tmp:
        repo_dir = Path(tmp)
        # No flows directory

        now = datetime(2026, 2, 15, tzinfo=timezone.utc)
        # Should not crash
        try:
            result = retention.sweep(repo_dir, now, days=30)
            expect(isinstance(result, list), f"sweep returned {type(result).__name__}, not list")
            # Strengthen: verify that missing flows dir returns empty list
            expect(result == [], f"sweep returned {result} for missing flows dir, expected []")
        except Exception as exc:
            raise AssertionError(f"sweep crashed on missing flows dir: {exc}")


def _make_flow(home: Path, name: str, status: str, updated: str) -> Path:
    """A real v2 flow dir (created by the CLI's init) with its state.json status and `updated` stamp forced."""
    flow = home / ".claude" / "flows-v2" / "owner_name" / name
    code, _, err = run_cli(home, "init", "--flow", str(flow), "--worktree", str(home / "wt"), "--repo", "owner/name")
    expect(code == 0, f"init exited {code}: {err}")
    state_path = flow / "state.json"
    data = json.loads(state_path.read_text(encoding="utf-8"))
    data["status"] = status
    data["updated"] = updated
    state_path.write_text(json.dumps(data), encoding="utf-8")
    return flow


def _populate(flow: Path) -> None:
    for rel in ("prompts/p.md", "directives/d.json", "questions/q.json", "answers/a.json", "driver-log.jsonl",
                "human.jsonl", "steps/03-implement.md", "receipt.md", "retro.md"):
        target = flow / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")


def test_sweep_prunes_expired_but_keeps_human_steps_and_receipts() -> None:
    """An expired done flow loses prompts/directives (and, with retro.md, questions/answers/log); human.jsonl, steps/, retro.md and state.json survive."""
    with tempfile.TemporaryDirectory() as tmp:
        home = make_home(Path(tmp))
        flow = _make_flow(home, "1-old-aaa111", "done", "2026-01-01T00:00:00Z")
        _populate(flow)
        pruned = retention.sweep(flow.parent, datetime(2026, 3, 1, tzinfo=timezone.utc), days=30)
        expect(pruned == [flow], f"sweep pruned {pruned}, expected [{flow}]")
        for gone in ("prompts", "directives", "questions", "answers", "driver-log.jsonl"):
            expect(not (flow / gone).exists(), f"{gone} survived the prune")
        for kept in ("state.json", "human.jsonl", "steps/03-implement.md", "retro.md", "receipt.md"):
            expect((flow / kept).exists(), f"{kept} was pruned")
        again = retention.sweep(flow.parent, datetime(2026, 3, 1, tzinfo=timezone.utc), days=30)
        expect((flow / "human.jsonl").exists(), "a second sweep pruned human.jsonl")
        expect(isinstance(again, list), "second sweep did not return a list")


def test_sweep_keeps_retro_inputs_before_retro_exists() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = make_home(Path(tmp))
        flow = _make_flow(home, "1-old-bbb222", "abandoned", "2026-01-01T00:00:00Z")
        _populate(flow)
        (flow / "retro.md").unlink()
        retention.sweep(flow.parent, datetime(2026, 3, 1, tzinfo=timezone.utc), days=30)
        expect(not (flow / "prompts").exists(), "prompts survived")
        for kept in ("questions/q.json", "answers/a.json", "driver-log.jsonl"):
            expect((flow / kept).exists(), f"{kept} was pruned before retro.md existed")


def test_sweep_spares_live_recent_and_kept_flows() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = make_home(Path(tmp))
        now = datetime(2026, 3, 1, tzinfo=timezone.utc)
        live = _make_flow(home, "2-live-ccc333", "running", "2026-01-01T00:00:00Z")
        failed = _make_flow(home, "3-failed-ddd444", "failed", "2026-01-01T00:00:00Z")
        recent = _make_flow(home, "4-recent-eee555", "done", "2026-02-27T00:00:00Z")
        kept = _make_flow(home, "5-kept-fff666", "done", "2026-01-01T00:00:00Z")
        for flow in (live, failed, recent, kept):
            _populate(flow)
        pruned = retention.sweep(live.parent, now, days=30, keep=kept)
        expect(pruned == [], f"sweep pruned {pruned}, expected nothing")
        for flow in (live, failed, recent, kept):
            expect((flow / "prompts").exists(), f"{flow.name} was pruned")


def test_sweep_spares_v1_root_flows() -> None:
    """A flow under the v1 flows root is never swept, even when its state says done and old."""
    with tempfile.TemporaryDirectory() as tmp:
        home = make_home(Path(tmp))
        v1 = home / ".claude" / "flows" / "owner_name" / "9-v1-aaa999"
        v1.mkdir(parents=True)
        (v1 / "state.json").write_text(json.dumps({"status": "done", "updated": "2026-01-01T00:00:00Z"}), encoding="utf-8")
        (v1 / "prompts").mkdir()
        old = os.environ.get("HOME")
        os.environ["HOME"] = str(home)
        try:
            pruned = retention.sweep(v1.parent, datetime(2026, 3, 1, tzinfo=timezone.utc), days=30)
        finally:
            if old is None:
                del os.environ["HOME"]
            else:
                os.environ["HOME"] = old
        expect(pruned == [], f"sweep pruned a v1 flow: {pruned}")
        expect((v1 / "prompts").exists(), "a v1 flow's files were pruned")


def test_sweep_boundary_age_is_expired() -> None:
    """A flow exactly `days` old is expired; one a second younger is not."""
    with tempfile.TemporaryDirectory() as tmp:
        home = make_home(Path(tmp))
        now = datetime(2026, 3, 1, tzinfo=timezone.utc)
        exact = _make_flow(home, "6-exact-aaa111", "done", "2026-01-30T00:00:00Z")
        younger = _make_flow(home, "7-younger-bbb222", "done", "2026-01-30T00:00:01Z")
        for flow in (exact, younger):
            _populate(flow)
        pruned = retention.sweep(exact.parent, now, days=30)
        expect(pruned == [exact], f"sweep pruned {pruned}, expected only the exactly-30-day-old flow")
        expect(not (exact / "prompts").exists(), "the boundary-age flow was not pruned")
        expect((younger / "prompts").exists(), "a flow younger than the boundary was pruned")


def test_sweep_unreadable_repo_dir_prunes_nothing() -> None:
    """An OSError while listing the repo dir prunes nothing and does not raise (D22)."""
    with tempfile.TemporaryDirectory() as tmp:
        repo_dir = Path(tmp) / "repo"
        repo_dir.mkdir()
        (repo_dir / "1-x-aaa").mkdir()
        os.chmod(repo_dir, 0)
        try:
            if os.access(repo_dir, os.R_OK):
                return  # running as a user that ignores permissions; cannot simulate
            pruned = retention.sweep(repo_dir, datetime(2026, 3, 1, tzinfo=timezone.utc), days=30)
        finally:
            os.chmod(repo_dir, 0o700)
        expect(pruned == [], f"sweep returned {pruned} for an unreadable dir")


SECTIONS = [
    ("[Retention basic behavior]", [
        ("3: retention handles missing flows directory", test_retention_survives_missing_flows_dir),
        ("4: expired flow pruned, human.jsonl/steps/receipts/state survive", test_sweep_prunes_expired_but_keeps_human_steps_and_receipts),
        ("5: retro inputs kept until retro.md exists", test_sweep_keeps_retro_inputs_before_retro_exists),
        ("6: live, failed, recent and kept flows spared", test_sweep_spares_live_recent_and_kept_flows),
        ("7: v1-root flows spared", test_sweep_spares_v1_root_flows),
        ("8: a flow exactly at the retention age is pruned, one second younger is not", test_sweep_boundary_age_is_expired),
        ("9: an unreadable repo dir prunes nothing", test_sweep_unreadable_repo_dir_prunes_nothing),
    ]),
]


def main() -> None:
    h = Harness("FLOW DRIVER RETENTION")
    with tempfile.TemporaryDirectory(prefix="flow-driver-retention-"):
        for title, checks in SECTIONS:
            print(title)
            for name, fn in checks:
                check(h, name, fn)
            print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
