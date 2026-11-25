#!/usr/bin/env python3
"""Tests for scripts/flow3/state.py: flow directory, locked updates, events, call directories."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_TMP = tempfile.mkdtemp(prefix="flow3-test-")
os.environ["FLOW3_HOME"] = _TMP

from _test_harness import Harness  # noqa: E402

from concurrent.futures import ThreadPoolExecutor  # noqa: E402

from scripts.flow3 import paths, state  # noqa: E402
from scripts.flow3.types import FlowState, make_stop  # noqa: E402

h = Harness("flow3 state")
root = Path(_TMP) / "repo-a"

h.test_result("re-exports paths", state.flow_home is paths.flow_home and state.repo_dir is paths.repo_dir)
h.test_result("slugify", state.slugify("Add Foo: Bar!!") == "add-foo-bar" and state.slugify("***") == "flow"
              and len(state.slugify("x" * 100)) == 40)

flow = state.FlowDir.create("repo-a", 7, "Add Foo: Bar", root)
doc = flow.load()
h.test_result("create writes top-level fields",
              doc["version"] == 1 and doc["repo"] == "repo-a" and doc["issue"] == 7 and doc["slug"] == "add-foo-bar"
              and doc["worktree"] == "" and doc["branch"] == "" and doc["base"] == "" and doc["pr"] is None
              and doc["data"] == {} and doc["directions"] == [] and doc["protected"] == []
              and doc["flow"]["stage"] == "worktree")
h.test_result("create makes subdirs", all((flow.path / d).is_dir() for d in ("calls", "steps", "human", "decisions", "checks")))
h.test_result("name", flow.name == "7-add-foo-bar")
try:
    state.FlowDir.create("repo-a", 7, "Other title", root)
    dup = False
except FileExistsError:
    dup = True
h.test_result("create twice raises FileExistsError", dup)
found = state.FlowDir.find("repo-a", 7)
h.test_result("find", found is not None and found.path == flow.path and state.FlowDir.find("repo-a", 8) is None)
state.FlowDir.create("repo-a", 3, "Three", root)
state.FlowDir.create("repo-b", 1, "Other repo", root)
h.test_result("all for a repo, sorted", [f.name for f in state.FlowDir.all("repo-a")] == ["3-three", "7-add-foo-bar"])
h.test_result("all repos", len(state.FlowDir.all()) == 3)

# get/set/data
flow.set("branch", "feature/x")
h.test_result("get/set", flow.get("branch") == "feature/x" and flow.get("nope", 5) == 5)
flow.set_data("plan", {"k": 1})
h.test_result("data", flow.data("plan") == {"k": 1} and flow.data("other") == {})

# flow state round trip including a stop
stop = make_stop("check", "lint failed", "fix", "resume it", evidence="e.log", tried=["a", "b"])
st = FlowState(stage="fix", status="stopped", review_round=2, sendbacks=1, stop=stop)
flow.set_flow_state(st)
h.test_result("flow_state round trip", flow.flow_state() == st)
flow.set_flow_state(FlowState())
h.test_result("flow_state default", flow.flow_state() == FlowState() and flow.flow_state().stop is None)

# concurrent update
flow.set("counter", 0)


def bump(_: int) -> None:
    def fn(d: dict) -> None:
        d["counter"] = d["counter"] + 1
    flow.update(fn)


with ThreadPoolExecutor(max_workers=8) as pool:
    list(pool.map(bump, range(80)))
h.test_result("concurrent update loses nothing", flow.get("counter") == 80, str(flow.get("counter")))

# events
flow.event("call", role="plan", cost_usd=0.5)
flow.event("call", role="review", cost_usd=1.25)
flow.event("stop", reason="x")
evs = flow.events()
h.test_result("events round trip", [e["kind"] for e in evs] == ["call", "call", "stop"] and evs[0]["role"] == "plan"
              and "ts" in evs[0] and evs[0]["ts"].endswith("+00:00"))
h.test_result("total_cost", abs(flow.total_cost() - 1.75) < 1e-9)
h.test_result("events empty before any", state.FlowDir.find("repo-a", 3).events() == [])  # type: ignore[union-attr]


def emit(i: int) -> None:
    flow.event("par", i=i)


with ThreadPoolExecutor(max_workers=8) as pool:
    list(pool.map(emit, range(40)))
h.test_result("concurrent events all intact", len([e for e in flow.events() if e["kind"] == "par"]) == 40)

# next_call_dir
with ThreadPoolExecutor(max_workers=8) as pool:
    dirs = list(pool.map(lambda _: flow.next_call_dir("role"), range(8)))
names = sorted(d.name for d in dirs)
h.test_result("8 threads, 8 distinct dirs", len(set(names)) == 8 and all(d.is_dir() for d in dirs), str(names))
h.test_result("numbered three digits", names[0] == "001-role" and names[-1] == "008-role", str(names))
h.test_result("next after those", flow.next_call_dir("x").name == "009-x")

h.summarize_and_exit()
