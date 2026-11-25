#!/usr/bin/env python3
"""Tests for scripts/flow3/engine.py with fake stage functions (no model calls)."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_TMP = tempfile.mkdtemp(prefix="flow3-test-")
os.environ["FLOW3_HOME"] = _TMP

from _test_harness import Harness  # noqa: E402

from typing import Any, Dict, List  # noqa: E402

from scripts.flow3 import cli, engine, record, repoconfig  # noqa: E402
from scripts.flow3.state import FlowDir  # noqa: E402
from scripts.flow3.types import Event, FlowStop, StageResult  # noqa: E402

h = Harness("flow3 engine")
ROOT = Path(_TMP) / "proj"
calls: List[str] = []


def ctx(flow: FlowDir) -> Any:
    return flow


def make_stages(behaviour: Dict[str, Any]) -> Dict[str, Any]:
    def mk(name: str) -> Any:
        def fn(c: Any) -> StageResult:
            calls.append(name)
            b = behaviour.get(name)
            if callable(b):
                return b(c)  # type: ignore[no-any-return]
            return StageResult("done")
        return fn
    return {s: mk(s) for s in ("worktree", "plan", "spec_tests", "implement", "review", "fix", "ship", "merge")}


# 1. Full run: park -> answered -> merge_gate -> merge_decision -> finish
flow = FlowDir.create("proj", 1, "Full run", ROOT)
plan_runs = {"n": 0}


def plan(c: Any) -> StageResult:
    plan_runs["n"] += 1
    return StageResult("needs_human") if plan_runs["n"] == 1 else StageResult("done")


stages = make_stages({"plan": plan})
act = engine.run_flow(flow, Event("start"), stages, ctx)
h.test_result("parks at plan", act.kind == "park" and calls == ["worktree", "plan"] and flow.flow_state().status == "waiting_human", str(calls))
act = engine.run_flow(flow, Event("answered"), stages, ctx)
h.test_result("reaches merge_gate", act.kind == "merge_gate" and flow.flow_state().status == "merge_gate"
              and calls == ["worktree", "plan", "plan", "spec_tests", "implement", "review", "ship"], str(calls))
act = engine.run_flow(flow, Event("merge_decision", decision="merge"), stages, ctx)
h.test_result("finishes", act.kind == "finish" and flow.flow_state().status == "done" and calls[-1] == "merge")
kinds = [e["kind"] for e in flow.events()]
h.test_result("events: start/end, park, merge_gate, finish",
              kinds.count("stage_start") == kinds.count("stage_end") == 8 and "park" in kinds and "merge_gate" in kinds and "finish" in kinds, str(kinds))
ends = [e for e in flow.events() if e["kind"] == "stage_end"]
h.test_result("stage_end carries outcome", ends[1]["stage"] == "plan" and ends[1]["outcome"] == "needs_human" and ends[-1]["outcome"] == "done")
j = (flow.path / "journal.md").read_text()
h.test_result("journal entries", "Parked" in j and "merge gate" in j and "finished" in j)

# 2. Review/fix loop order, cap of three reviews
calls.clear()
flow = FlowDir.create("proj", 2, "Loop", ROOT)
stages = make_stages({"review": lambda c: StageResult("done", {"fix_needed": True})})
act = engine.run_flow(flow, Event("start"), stages, ctx)
h.test_result("three reviews then ship", calls == ["worktree", "plan", "spec_tests", "implement", "review", "fix", "review", "fix", "review", "ship"]
              and act.kind == "merge_gate", str(calls))
# send back: fix, one more review, ship
calls.clear()
act = engine.run_flow(flow, Event("merge_decision", decision="send_back"), stages, ctx)
h.test_result("send_back: fix, review, ship", calls == ["fix", "review", "ship"] and act.kind == "merge_gate" and flow.flow_state().sendbacks == 1, str(calls))
act = engine.run_flow(flow, Event("merge_decision", decision="stop", notes="hold"), stages, ctx)
stop = record.read_stop(flow)
h.test_result("gate stop writes stop.json", act.kind == "stop" and stop is not None and stop["cls"] == "human" and stop["reason"] == "hold")

# 3. FlowStop -> stop.json -> resume reruns same stage
calls.clear()
flow = FlowDir.create("proj", 3, "Stopper", ROOT)
attempts = {"n": 0}


def impl(c: Any) -> StageResult:
    attempts["n"] += 1
    if attempts["n"] == 1:
        raise FlowStop("check", "lint is red", evidence="checks/001.log", tried=["fixer once", "fixer twice"])
    return StageResult("done")


stages = make_stages({"implement": impl})
act = engine.run_flow(flow, Event("start"), stages, ctx)
stop = record.read_stop(flow)
h.test_result("stop action", act.kind == "stop" and flow.flow_state().status == "stopped" and flow.flow_state().stage == "implement")
h.test_result("stop.json contents", stop is not None and stop["cls"] == "check" and stop["reason"] == "lint is red"
              and stop["evidence"] == "checks/001.log" and stop["tried"] == ["fixer once", "fixer twice"]
              and stop["resume_command"] == "python3 -m scripts.flow3 resume proj 3" and stop["stage"] == "implement", str(stop))
h.test_result("stop event and journal", any(e["kind"] == "stop" for e in flow.events()) and "lint is red" in (flow.path / "journal.md").read_text())
before = list(calls)
act = engine.run_flow(flow, Event("resume"), stages, ctx)
h.test_result("resume reruns implement and clears stop", record.read_stop(flow) is None and calls[len(before):][0] == "implement"
              and act.kind == "merge_gate", str(calls))

# 4. Unexpected exception -> runner stop with traceback file
flow = FlowDir.create("proj", 4, "Crasher", ROOT)


def boom(c: Any) -> StageResult:
    raise KeyError("missing")


act = engine.run_flow(flow, Event("start"), make_stages({"plan": boom}), ctx)
stop = record.read_stop(flow)
h.test_result("exception becomes runner stop", act.kind == "stop" and stop is not None and stop["cls"] == "runner"
              and stop["reason"].startswith("KeyError") and stop["stage"] == "plan", str(stop))
ev = Path(stop["evidence"]) if stop and stop["evidence"] else None
h.test_result("traceback file", ev is not None and ev.exists() and ev.parent.name == "errors" and ev.name.startswith("plan-")
              and "Traceback" in ev.read_text() and "boom" in ev.read_text())

# 5. Missing stage
flow = FlowDir.create("proj", 5, "Missing", ROOT)
stages = make_stages({})
del stages["plan"]
act = engine.run_flow(flow, Event("start"), stages, ctx)
stop = record.read_stop(flow)
h.test_result("missing stage -> runner stop", act.kind == "stop" and stop is not None and stop["cls"] == "runner" and "plan" in stop["reason"])

# 6. Invalid event propagates and leaves state alone
flow = FlowDir.create("proj", 6, "Invalid", ROOT)
try:
    engine.run_flow(flow, Event("answered"), make_stages({}), ctx)
    raised = False
except ValueError:
    raised = True
h.test_result("invalid event raises, state unchanged", raised and flow.flow_state().stage == "worktree" and flow.flow_state().status == "running")

# 7. resume_command, cli
h.test_result("resume_command", engine.resume_command(flow) == "python3 -m scripts.flow3 resume proj 6")
ROOT.mkdir(parents=True, exist_ok=True)
repoconfig.doctor_passed = lambda root: True  # type: ignore[assignment]  # the cli gate; doctor has its own tests
rc = cli.main(["start", "9", "--repo-root", str(ROOT), "--title", "From cli"], stages=make_stages({}), build_ctx=ctx)
h.test_result("cli start reaches gate, exit 0", rc == 0)
rc = cli.main(["start", "9", "--repo-root", str(ROOT)], stages=make_stages({}), build_ctx=ctx)
h.test_result("cli start twice is a usage error", rc == 1)
rc = cli.main(["start", "10", "--repo-root", str(ROOT), "--title", "Stops"], stages=make_stages({"plan": boom}), build_ctx=ctx)
h.test_result("cli stop exits 2", rc == 2)
h.test_result("cli bad usage exits 1", cli.main(["bogus"]) == 1 and cli.main([]) == 1)
h.test_result("cli resume unknown flow", cli.main(["resume", "proj", "999"]) == 1)
h.test_result("cli status/stop-screen/why", cli.main(["status", "--repo", "proj"]) == 0 and cli.main(["stop-screen", "10", "--repo", "proj"]) == 0
              and cli.main(["why", "10", "nothing", "--repo", "proj"]) == 0)
rc = cli.main(["resume", "proj", "10"], stages=make_stages({}), build_ctx=ctx)
h.test_result("cli resume completes", rc == 0 and record.read_stop(FlowDir.find("proj", 10)) is None)  # type: ignore[arg-type]
h.summarize_and_exit()
