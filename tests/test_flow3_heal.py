#!/usr/bin/env python3
"""
Tests for scripts/flow3/heal.py: every row of the PLAN.md section 5 table, bounded and recorded.
No real model is called: FLOW3_CLAUDE_BIN is tests/flow3_fake_claude.py, scripted per scenario.

PLAN.md section 12 (last month's hard stops) mapped to the tests below (group names appear in the
test names printed by this file):

  Machinery (28%): malformed output / bad structured output
      schema: bad output then resume succeeds; schema: twice bad stops runner;
      transient: argument error stops at once; transient: unknown model (404) stops at once
  Environment: branch behind base blocking the check (base-stale)
      base-stale: behind and clean merges; base-stale: conflict resolved by the resolver and checked;
      base-stale: unresolved conflict stops and leaves the old head; base-stale: resolved but red check
  Environment: check timeout
      environment: timeout re-run once passes; environment: timeout twice stops
  Environment: cleanup failing after a merge
      cleanup: fails once; cleanup: fails twice gives a warning and never raises
  Environment: missing dependency
      environment: missing module runs install once; environment: no install configured stops
  Work failed: fix loop exhausted
      check-work: fixer round 1 fixes; round 2 fixes; neither fixes stops check; red run returned
  Work failed: implementer delivered part of a unit
      unit-shortfall: max turns keeps partial work and retries on the stronger model;
      unit-shortfall: second shortfall stops implement; unit-shortfall: status partial path
  Test-edit approval / out-of-unit edits (protected)
      protected: reverted and retried; protected: twice stops; protected: outside allowed counts
  No reason recorded: every stop raised in this suite has a non-empty reason (final tests), and
      tried is non-empty wherever a heal was attempted
  Also: limit, denied, check-flaky, budget (hard and soft), pure classification
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="flow3-test-")).resolve()
HOME = TMP / "home"
os.environ["FLOW3_HOME"] = str(HOME)
FAKE = ROOT / "tests" / "flow3_fake_claude.py"
os.environ["FLOW3_CLAUDE_BIN"] = str(FAKE)
os.environ.pop("FLOW3_LIMIT_CEILING_SECS", None)

from _test_harness import Harness  # noqa: E402

from scripts.flow3 import context, gitops, heal, runner, schemas  # noqa: E402
from scripts.flow3.state import FlowDir  # noqa: E402
from scripts.flow3.types import CallResult, CallSpec, CheckRun, FlowStop  # noqa: E402

h = Harness("flow3 heal")
SLEEPS: List[float] = []
RUNNER_SLEEPS: List[float] = []
heal._sleep = lambda s: SLEEPS.append(s)  # type: ignore[assignment]
runner._sleep = lambda s: RUNNER_SLEEPS.append(s)  # type: ignore[assignment]
STOPS: List[FlowStop] = []
_counter = {"n": 0}


def sh(args: List[str], cwd: Path) -> str:
    return subprocess.run(args, cwd=str(cwd), check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True).stdout.strip()


CHECK_PY = '''
import sys, time, os
d = sys.argv[1]
with open(d + "/runs", "a") as fh:
    fh.write("x\\n")
lines = []
if os.path.exists(d + "/results"):
    lines = [x for x in open(d + "/results").read().split("\\n") if x]
cur = lines.pop(0) if lines else "pass"
open(d + "/results", "w").write("\\n".join(lines) + "\\n")
if cur == "pass":
    sys.exit(0)
if cur == "fail":
    print("FAILED tests/test_a.py::test_one - assert 1 == 2")
    sys.exit(1)
if cur == "dep":
    print("ModuleNotFoundError: No module named 'foo'")
    sys.exit(1)
if cur == "slow":
    time.sleep(30)
if cur == "127":
    sys.exit(127)
sys.exit(1)
'''


class Box:
    """One sandbox: bare origin, clone (also the worktree), control dir for the scripted check and fake."""

    def __init__(self, name: str, files: Optional[Dict[str, str]] = None, install: bool = False,
                 timeout: int = 60, protected: Optional[List[str]] = None, soft: float = 15.0,
                 hard: float = 40.0) -> None:
        self.name = name
        self.ctl = TMP / (name + "-ctl")
        self.ctl.mkdir()
        (self.ctl / "check.py").write_text(CHECK_PY)
        (self.ctl / "install.sh").write_text('echo ran >> "%s/install.log"\n' % self.ctl)
        check = "python3 %s/check.py %s" % (self.ctl, self.ctl)
        yml = ["provider: local", "check:", "  fast: '%s'" % check, "  full: '%s'" % check,
               "  timeout_secs: %d" % timeout, "budget:", "  soft_usd: %s" % soft, "  hard_usd: %s" % hard]
        if install:
            yml.append("install: 'sh %s/install.sh'" % self.ctl)
        if protected:
            yml.append("protected_paths: [%s]" % ", ".join(protected))
        base = {".claude/flow.yaml": "\n".join(yml) + "\n", "README.md": "hi\n"}
        base.update(files or {})
        self.origin = TMP / (name + "-origin.git")
        self.clone = TMP / name
        sh(["git", "init", "--bare", "-q", "-b", "main", str(self.origin)], TMP)
        sh(["git", "clone", "-q", str(self.origin), str(self.clone)], TMP)
        sh(["git", "config", "user.name", "t"], self.clone)
        sh(["git", "config", "user.email", "t@example.invalid"], self.clone)
        for rel, text in base.items():
            p = self.clone / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        sh(["git", "add", "-A"], self.clone)
        sh(["git", "commit", "-q", "-m", "init"], self.clone)
        sh(["git", "push", "-q", "origin", "main"], self.clone)
        sh(["git", "remote", "set-head", "origin", "main"], self.clone)
        self.flow = FlowDir.create(name, 1, "Heal " + name, self.clone)
        self.flow.set("worktree", str(self.clone))
        self.flow.set("base", "main")
        self.ctx = context.build(self.flow)

    # -- the scripted check
    def results(self, *lines: str) -> None:
        (self.ctl / "results").write_text("\n".join(lines) + "\n")

    def runs(self) -> int:
        p = self.ctl / "runs"
        return len(p.read_text().split()) if p.exists() else 0

    def installs(self) -> int:
        p = self.ctl / "install.log"
        return len(p.read_text().split()) if p.exists() else 0

    # -- the scripted fake claude
    def script(self, calls: List[Dict[str, Any]], default: Optional[Dict[str, Any]] = None) -> None:
        _counter["n"] += 1
        path = self.ctl / ("script%d.json" % _counter["n"])
        doc: Dict[str, Any] = {"calls": calls}
        if default is not None:
            doc["default"] = default
        path.write_text(json.dumps(doc))
        os.environ["FLOW3_FAKE_SCRIPT"] = str(path)
        log = self.ctl / ("log%d.jsonl" % _counter["n"])
        os.environ["FLOW3_FAKE_LOG"] = str(log)
        self._log = log
        SLEEPS[:] = []
        RUNNER_SLEEPS[:] = []
        limit = HOME / "limit.json"
        if limit.exists():
            limit.unlink()

    def launches(self) -> List[Dict[str, Any]]:
        if not self._log.exists():
            return []
        return [json.loads(x) for x in self._log.read_text().splitlines() if x.strip()]

    def heals(self, cls: str) -> List[Dict[str, Any]]:
        return [e for e in self.flow.events() if e["kind"] == "heal" and e.get("cls") == cls]

    def head(self) -> str:
        return gitops.head_sha(self.clone)

    def log_subjects(self) -> List[str]:
        return sh(["git", "log", "--format=%s"], self.clone).splitlines()


def judge_spec(box: Box, **kw: Any) -> CallSpec:
    base: Dict[str, Any] = dict(role="judge", prompt="decide", schema=schemas.JUDGE, cwd=box.clone, label="t")
    base.update(kw)
    return CallSpec(**base)


def work_spec(box: Box, **kw: Any) -> CallSpec:
    base: Dict[str, Any] = dict(role="implementer", prompt="implement the unit", schema=schemas.IMPLEMENTER,
                                cwd=box.clone, writes=True, label="unit-1", tools=("Read", "Edit"))
    base.update(kw)
    return CallSpec(**base)


def expect_stop(fn: Callable[[], Any], cls: str, tried: bool = True) -> Optional[FlowStop]:
    try:
        fn()
    except FlowStop as stop:
        STOPS.append(stop)
        if stop.cls != cls:
            return None
        if tried and not stop.tried:
            return None
        return stop
    return None


GOOD_JUDGE = {"verdict": "code_wrong", "reason": "r"}
DONE = {"status": "done", "summary": "did it"}


# =============================================================================================
# Pure classification
# =============================================================================================

def res(**kw: Any) -> CallResult:
    base: Dict[str, Any] = dict(ok=False, role="judge", model="haiku", parsed=True, exit_code=1)
    base.update(kw)
    return CallResult(**base)


_spec_r = CallSpec(role="judge", prompt="p", schema={}, cwd=Path("."))
_spec_w = CallSpec(role="implementer", prompt="p", schema={}, cwd=Path("."), writes=True)


def cls_of(r: CallResult, spec: CallSpec = _spec_r) -> Optional[str]:
    f = heal.classify_call(r, spec)
    return f.cls if f else None


h.test_result("classify: good call is None", cls_of(res(ok=True, exit_code=0)) is None)
h.test_result("classify: good call with denials is None",
              cls_of(res(ok=True, exit_code=0, permission_denials=({"tool_name": "Bash"},))) is None)
den = heal.classify_call(res(permission_denials=({"tool_name": "Bash", "tool_input": {"command": "rm -rf x"}},)),
                         _spec_r)
h.test_result("classify: failed call with denial names the command",
              den is not None and den.cls == "denied" and "rm -rf x" in den.reason)
den2 = heal.classify_call(res(permission_denials=({"tool_name": "WebFetch", "tool_input": {}},)), _spec_r)
h.test_result("classify: denial falls back to tool name", den2 is not None and "WebFetch" in den2.reason)
h.test_result("classify: rate_limit status rejected is limit",
              cls_of(res(rate_limit={"status": "rejected"})) == "limit")
h.test_result("classify: allowed_warning alone is not a limit",
              cls_of(res(rate_limit={"status": "allowed_warning"}, parsed=False)) == "transient")
h.test_result("classify: 429 and 529 are limit",
              cls_of(res(api_error_status=429, is_error=True)) == "limit"
              and cls_of(res(api_error_status=529, is_error=True)) == "limit")
h.test_result("classify: limit wording in text or stderr",
              cls_of(res(result_text="You hit the Usage Limit", is_error=True)) == "limit"
              and cls_of(res(parsed=False, stderr="server overloaded")) == "limit")
argerr = heal.classify_call(res(parsed=False, stderr="Error: --json-schema is not valid JSON: x"), _spec_r)
h.test_result("classify: argument error is non-retryable transient",
              argerr is not None and argerr.cls == "transient" and argerr.detail.get("no-retry") is True)
f404 = heal.classify_call(res(is_error=True, api_error_status=404, terminal_reason="api_error"), _spec_r)
h.test_result("classify: 4xx other than 429 is non-retryable",
              f404 is not None and f404.cls == "transient" and f404.detail.get("no-retry") is True)
h.test_result("classify: max turns is shortfall for writers, transient for readers",
              cls_of(res(subtype="error_max_turns", is_error=True, terminal_reason="max_turns"), _spec_w)
              == "unit-shortfall"
              and cls_of(res(subtype="error_max_turns", is_error=True, terminal_reason="max_turns"))
              == "transient")
h.test_result("classify: timeout, signal exit, empty, no session, 5xx are transient",
              cls_of(res(timed_out=True, parsed=False)) == "transient"
              and cls_of(res(exit_code=143, parsed=False)) == "transient"
              and cls_of(res(parsed=False, exit_code=1)) == "transient"
              and cls_of(res(parsed=False, stderr="No conversation found with session ID: x")) == "transient"
              and cls_of(res(is_error=True, api_error_status=503)) == "transient")
h.test_result("classify: schema errors on a clean result are schema",
              cls_of(res(exit_code=0, schema_errors=("no structured output returned",))) == "schema")
h.test_result("classify: is_error beats a success subtype",
              cls_of(res(subtype="success", is_error=True, terminal_reason="api_error", api_error_status=404))
              == "transient")
h.test_result("backoff doubles and caps at 1800",
              [heal._limit_wait(res(), n) for n in range(7)] == [60, 120, 240, 480, 960, 1800, 1800])
h.test_result("path_violations: protected, outside allowed, empty allowed",
              heal.path_violations(["src/a.py", "locked/x", "docs/y"], ["src/"], ["locked/"]) == ["locked/x", "docs/y"]
              and heal.path_violations(["src/a.py", "locked/x"], [], ["locked/"]) == ["locked/x"]
              and heal.path_violations(["a", "b"], [], []) == [])

# =============================================================================================
# limit
# =============================================================================================

box = Box("lim")
past = int(time.time()) - 100
box.script([{"mode": "limit", "resets_at": past}, {"output": GOOD_JUDGE}])
r = heal.call(box.ctx, judge_spec(box))
h.test_result("limit: waits then succeeds (one wait, two launches)",
              r.ok and SLEEPS == [60] and len(box.launches()) == 2, str(SLEEPS))
h.test_result("limit: heal event recorded", len(box.heals("limit")) == 1)

box.script([{"mode": "limit", "resets_at": past}] * 3 + [{"output": GOOD_JUDGE}])
r = heal.call(box.ctx, judge_spec(box))
h.test_result("limit: backoff 60, 120, 240", r.ok and SLEEPS == [60, 120, 240] and len(box.launches()) == 4,
              str(SLEEPS))

box.script([{"mode": "limit", "resets_at": int(time.time()) + 500}, {"output": GOOD_JUDGE}])
r = heal.call(box.ctx, judge_spec(box))
h.test_result("limit: waits until resetsAt when it is in the future",
              r.ok and len(SLEEPS) == 1 and 400 < SLEEPS[0] <= 500, str(SLEEPS))

os.environ["FLOW3_LIMIT_CEILING_SECS"] = "100"
box.script([], default={"mode": "limit", "resets_at": past})
stop = expect_stop(lambda: heal.call(box.ctx, judge_spec(box)), "limit")
h.test_result("limit: exceeding the ceiling stops `limit` after bounded waits",
              stop is not None and SLEEPS == [60] and len(box.launches()) == 2 and stop.evidence is not None,
              "%s %s" % (SLEEPS, stop))
h.test_result("limit: note_limit wrote the shared back-off", runner.limit_wait_remaining() > 0)
os.environ.pop("FLOW3_LIMIT_CEILING_SECS", None)

# =============================================================================================
# transient (and section 12 "Machinery": argument and model errors)
# =============================================================================================

box.script([{"mode": "crash"}, {"output": GOOD_JUDGE}])
r = heal.call(box.ctx, judge_spec(box, resume_session="S0"))
lines = box.launches()
h.test_result("transient: crash then success, retried in a fresh session",
              r.ok and len(lines) == 2 and lines[0]["resume"] == "S0" and lines[1]["resume"] is None)

box.script([{"mode": "crash"}, {"mode": "crash"}, {"output": GOOD_JUDGE}])
stop = expect_stop(lambda: heal.call(box.ctx, judge_spec(box)), "runner")
h.test_result("transient: two crashes stop `runner` after exactly two launches",
              stop is not None and len(box.launches()) == 2 and stop.reason, str(stop))

box.script([{"mode": "empty", "exit": 1, "stderr": "Error: --json-schema is not valid JSON: x"},
            {"output": GOOD_JUDGE}])
stop = expect_stop(lambda: heal.call(box.ctx, judge_spec(box)), "runner", tried=False)
h.test_result("transient: argument error stops `runner` with one launch",
              stop is not None and len(box.launches()) == 1 and "bad argument" in stop.reason, str(stop))

box.script([{"mode": "api_error", "status": 404}, {"output": GOOD_JUDGE}])
stop = expect_stop(lambda: heal.call(box.ctx, judge_spec(box)), "runner", tried=False)
h.test_result("transient: unknown model (404) stops `runner` with one launch",
              stop is not None and len(box.launches()) == 1 and "404" in stop.reason, str(stop))

box.script([{"mode": "empty", "exit": 1, "stderr": "No conversation found with session ID: Z"},
            {"output": GOOD_JUDGE}])
r = heal.call(box.ctx, judge_spec(box, resume_session="Z"))
h.test_result("transient: unknown session on resume retries fresh",
              r.ok and box.launches()[1]["resume"] is None)

# =============================================================================================
# schema
# =============================================================================================

bad = {"verdict": "nonsense", "reason": "r"}
box.script([{"output": bad, "session_id": "S-bad"}, {"output": GOOD_JUDGE, "session_id": "S-bad"}])
r = heal.call(box.ctx, judge_spec(box))
lines = box.launches()
h.test_result("schema: bad output then a resume carrying the session id succeeds",
              r.ok and len(lines) == 2 and lines[1]["resume"] == "S-bad"
              and "did not match" in lines[1]["prompt"] and "verdict" in lines[1]["prompt"], str(lines[-1:]))

box.script([{"output": bad, "session_id": "S-bad"}, {"output": bad, "session_id": "S-bad"},
            {"output": GOOD_JUDGE}])
stop = expect_stop(lambda: heal.call(box.ctx, judge_spec(box)), "runner")
h.test_result("schema: twice bad stops `runner` after two launches",
              stop is not None and len(box.launches()) == 2, str(stop))

box.script([{"mode": "no_output"}, {"output": GOOD_JUDGE}])
r = heal.call(box.ctx, judge_spec(box))
h.test_result("schema: a success with no structured output is resumed once", r.ok and len(box.launches()) == 2)

# =============================================================================================
# denied
# =============================================================================================

box.script([{"mode": "denied", "output": bad}, {"output": GOOD_JUDGE}])
stop = expect_stop(lambda: heal.call(box.ctx, judge_spec(box)), "permission", tried=False)
h.test_result("denied: failed call with a denial stops `permission` naming the command, one launch",
              stop is not None and "rm -rf x" in stop.reason and len(box.launches()) == 1, str(stop))
box.script([{"mode": "denied", "output": GOOD_JUDGE}])
r = heal.call(box.ctx, judge_spec(box))
ev = [e for e in box.flow.events() if e["kind"] == "denied-tolerated"]
h.test_result("denied: a good call with a denial is tolerated and recorded",
              r.ok and len(ev) == 1 and ev[0]["commands"] == ["rm -rf x"], str(ev))

# =============================================================================================
# budget
# =============================================================================================

b2 = Box("budhard", hard=2.0, soft=1.0)
b2.flow.event("call", cost_usd=5.0)
b2.script([{"output": GOOD_JUDGE}])
stop = expect_stop(lambda: heal.call(b2.ctx, judge_spec(b2)), "budget", tried=False)
h.test_result("budget: hard ceiling stops `budget` with zero launches",
              stop is not None and len(b2.launches()) == 0 and stop.reason, str(stop))

b3 = Box("budsoft", hard=100.0, soft=0.5)
b3.flow.event("call", cost_usd=0.6)
b3.script([], default={"output": GOOD_JUDGE})
heal.call(b3.ctx, judge_spec(b3))
heal.call(b3.ctx, judge_spec(b3))
soft = [e for e in b3.flow.events() if e["kind"] == "budget_soft"]
h.test_result("budget: soft threshold writes exactly one event and continues",
              len(soft) == 1 and len(b3.launches()) == 2, str(soft))

# =============================================================================================
# environment
# =============================================================================================

e1 = Box("env1", install=True)
e1.results("dep", "pass")
run = heal.check(e1.ctx, "fast")
h.test_result("environment: missing module runs install once, re-runs, green",
              run.ok and e1.installs() == 1 and e1.runs() == 2 and len(e1.heals("environment")) >= 1)

e2 = Box("env2")
e2.results("dep", "pass")
stop = expect_stop(lambda: heal.check(e2.ctx, "fast"), "environment", tried=False)
h.test_result("environment: no install configured stops `environment` without re-running",
              stop is not None and e2.runs() == 1 and stop.evidence is not None and stop.reason, str(stop))

e3 = Box("env3", install=True)
e3.results("dep", "dep")
stop = expect_stop(lambda: heal.check(e3.ctx, "fast"), "environment")
h.test_result("environment: still failing after install stops (install ran once, check twice)",
              stop is not None and e3.installs() == 1 and e3.runs() == 2, str(stop))

e4 = Box("env4", timeout=1)
e4.results("slow", "pass")
run = heal.check(e4.ctx, "fast")
h.test_result("environment: timeout re-run once passes", run.ok and e4.runs() == 2)

e5 = Box("env5", timeout=1)
e5.results("slow", "slow", "pass")
stop = expect_stop(lambda: heal.check(e5.ctx, "fast"), "environment")
h.test_result("environment: timeout twice stops `environment` after exactly two runs",
              stop is not None and e5.runs() == 2 and "timed out" in stop.reason, str(stop))

e6 = Box("env6", install=True)
e6.results("127", "pass")
run = heal.check(e6.ctx, "fast")
h.test_result("environment: exit 127 is treated as a missing tool", run.ok and e6.installs() == 1)

# classify_check, pure
fr = CheckRun(ok=False, command="c", mode="fast", exit_code=1, timed_out=False, duration_secs=0.1, tree="t",
              output_path="/nonexistent", trimmed="FAILED tests/x.py::t1\nFAIL other\n")
okr = CheckRun(ok=True, command="c", mode="fast", exit_code=0, timed_out=False, duration_secs=0.1, tree="t",
               output_path="", trimmed="")
h.test_result("classify_check: flaky, work, timeout",
              heal.classify_check(fr, okr).cls == "check-flaky"
              and heal.classify_check(fr, None).cls == "check-work"
              and heal.classify_check(fr, fr).cls == "check-work"
              and heal.classify_check(CheckRun(**{**fr.__dict__, "timed_out": True}), None).cls == "environment"
              and heal.classify_check(fr, okr).detail["tests"] == ["tests/x.py::t1", "other"])

# =============================================================================================
# check-flaky, check-work, reuse
# =============================================================================================

f1 = Box("flaky")
f1.results("fail", "pass")
run = heal.check(f1.ctx, "fast")
fl = f1.heals("check-flaky")
h.test_result("check-flaky: fails once then passes, continues; event names the test",
              run.ok and f1.runs() == 2 and len(fl) == 1 and "tests/test_a.py::test_one" in fl[0]["tests"][0], str(fl))

run = heal.check(f1.ctx, "fast")
h.test_result("check: a recorded pass on a clean tree is reused without running",
              run.ok and run.reused and f1.runs() == 2
              and any(e["kind"] == "check_reused" for e in f1.flow.events()))

w1 = Box("work1")
w1.results("fail", "fail")
rounds: List[int] = []


def fixer_round1(run: CheckRun, rnd: int) -> None:
    rounds.append(rnd)
    w1.results("pass")


run = heal.check(w1.ctx, "fast", fixer=fixer_round1)
h.test_result("check-work: fixer round 1 fixes", run.ok and rounds == [1] and w1.runs() == 3)

w2 = Box("work2")
w2.results("fail", "fail")
rounds2: List[int] = []


def fixer_round2(run: CheckRun, rnd: int) -> None:
    rounds2.append(rnd)
    w2.results("fail" if rnd == 1 else "pass")


run = heal.check(w2.ctx, "fast", fixer=fixer_round2)
h.test_result("check-work: round 2 fixes", run.ok and rounds2 == [1, 2] and w2.runs() == 4)

w3 = Box("work3")
w3.results("fail", "fail", "fail", "fail")
rounds3: List[int] = []
stop = expect_stop(lambda: heal.check(w3.ctx, "fast", fixer=lambda r, n: rounds3.append(n)), "check")
h.test_result("check-work: neither round fixes it stops `check` (two rounds, four runs)",
              stop is not None and rounds3 == [1, 2] and w3.runs() == 4 and stop.evidence is not None
              and any("fixer round 2" in t for t in stop.tried), str(stop))

w4 = Box("work4")
w4.results("fail", "fail", "fail", "fail")
red = heal.check(w4.ctx, "fast", fixer=lambda r, n: None, raise_on_fail=False)
h.test_result("check-work: raise_on_fail=False returns the red run", not red.ok and w4.runs() == 4)

w5 = Box("work5")
w5.results("fail", "fail")
stop = expect_stop(lambda: heal.check(w5.ctx, "fast"), "check")
h.test_result("check-work: no fixer stops `check`", stop is not None and w5.runs() == 2, str(stop))

# =============================================================================================
# base-stale
# =============================================================================================


def behind_box(name: str, own: Dict[str, str], upstream: Dict[str, str]) -> Box:
    b = Box(name, files={"shared.txt": "base\n"})
    sh(["git", "checkout", "-q", "-b", "flow/x"], b.clone)
    for rel, text in own.items():
        (b.clone / rel).write_text(text)
    sh(["git", "add", "-A"], b.clone)
    sh(["git", "commit", "-q", "-m", "own work"], b.clone)
    other = TMP / (name + "-other")
    sh(["git", "clone", "-q", str(b.origin), str(other)], TMP)
    sh(["git", "config", "user.name", "o"], other)
    sh(["git", "config", "user.email", "o@example.invalid"], other)
    for rel, text in upstream.items():
        (other / rel).write_text(text)
    sh(["git", "add", "-A"], other)
    sh(["git", "commit", "-q", "-m", "upstream work"], other)
    sh(["git", "push", "-q", "origin", "main"], other)
    return b


s0 = Box("notbehind")
h.test_result("base-stale: not behind returns False", heal.sync_base(s0.ctx) is False)

s1 = behind_box("sb1", {"mine.txt": "m\n"}, {"theirs.txt": "t\n"})
s1.script([])
h.test_result("base-stale: behind and clean merges, True, no model call",
              heal.sync_base(s1.ctx) is True and (s1.clone / "theirs.txt").exists()
              and len(s1.launches()) == 0 and len(s1.heals("base-stale")) == 1)

s2 = behind_box("sb2", {"shared.txt": "mine\n"}, {"shared.txt": "theirs\n"})
s2.script([{"role": "conflict_resolver", "write_files": {"shared.txt": "mine and theirs\n"}, "output": DONE}])
ok = heal.sync_base(s2.ctx)
parents = sh(["git", "log", "-1", "--format=%p"], s2.clone).split()
h.test_result("base-stale: conflict resolved by the resolver, merged and checked",
              ok is True and (s2.clone / "shared.txt").read_text() == "mine and theirs\n"
              and len(s2.launches()) == 1 and s2.runs() == 1 and len(parents) == 2
              and gitops.changed_files(s2.clone) == [], str(s2.launches()))
h.test_result("base-stale: resolver ran as a writer with edit tools only",
              "Edit" in s2.launches()[0]["argv"][s2.launches()[0]["argv"].index("--tools") + 1])

s3 = behind_box("sb3", {"shared.txt": "mine\n"}, {"shared.txt": "theirs\n"})
pre = s3.head()
s3.script([{"role": "conflict_resolver", "output": DONE}])
stop = expect_stop(lambda: heal.sync_base(s3.ctx), "conflict")
h.test_result("base-stale: unresolved conflict stops `conflict`, worktree clean on the old head",
              stop is not None and "shared.txt" in stop.reason and s3.head() == pre
              and gitops.changed_files(s3.clone) == [] and len(s3.launches()) == 1
              and not (s3.clone / ".git" / "MERGE_HEAD").exists(), str(stop))

s4 = behind_box("sb4", {"shared.txt": "mine\n"}, {"shared.txt": "theirs\n"})
pre = s4.head()
s4.results("fail", "fail")
s4.script([{"role": "conflict_resolver", "write_files": {"shared.txt": "both\n"}, "output": DONE}])
stop = expect_stop(lambda: heal.sync_base(s4.ctx), "conflict")
h.test_result("base-stale: resolved but the check is red stops `conflict` and restores the old head",
              stop is not None and s4.head() == pre and gitops.changed_files(s4.clone) == []
              and stop.evidence is not None and "check" in stop.reason, str(stop))

# =============================================================================================
# unit-shortfall and protected (write_call)
# =============================================================================================

u1 = Box("short1", files={"src/keep.py": "k\n"})
partial_files = {"src/a.py": "partial\n"}
u1.script([{"mode": "max_turns", "write_files": partial_files},
           {"write_files": {"src/b.py": "rest\n"}, "output": DONE}])
r = heal.write_call(u1.ctx, work_spec(u1), ["src/"], [], "unit 1")
lines = u1.launches()
subjects = u1.log_subjects()
h.test_result("unit-shortfall: max turns keeps partial work, retries on the stronger model, succeeds",
              r.ok and len(lines) == 2 and lines[0]["model"] == "haiku" and lines[1]["model"] == "sonnet"
              and lines[1]["resume"] is None and "already committed" in lines[1]["prompt"]
              and "implement the unit" in lines[1]["prompt"]
              and subjects[:2] == ["unit 1", "unit 1 (partial)"] and gitops.changed_files(u1.clone) == []
              and (u1.clone / "src" / "a.py").exists(), "%s %s" % (subjects, [x["model"] for x in lines]))

u2 = Box("short2")
u2.script([{"mode": "max_turns", "write_files": {"src/a.py": "p1\n"}},
           {"mode": "max_turns", "write_files": {"src/b.py": "p2\n"}}, {"output": DONE}])
stop = expect_stop(lambda: heal.write_call(u2.ctx, work_spec(u2), ["src/"], [], "unit 2"), "implement")
h.test_result("unit-shortfall: second shortfall stops `implement`, partial work kept, two launches",
              stop is not None and len(u2.launches()) == 2 and u2.log_subjects()[0] == "unit 2 (partial)"
              and gitops.changed_files(u2.clone) == [], str(stop))

u3 = Box("short3")
u3.script([{"output": {"status": "partial", "summary": "half", "remaining": ["finish the parser"]},
            "write_files": {"src/a.py": "half\n"}},
           {"output": DONE, "write_files": {"src/b.py": "done\n"}}])
r = heal.write_call(u3.ctx, work_spec(u3), ["src/"], [], "unit 3")
lines = u3.launches()
h.test_result("unit-shortfall: status partial retries once on the stronger model with what remains",
              r.ok and len(lines) == 2 and lines[1]["model"] == "sonnet" and "finish the parser" in lines[1]["prompt"]
              and u3.log_subjects()[:2] == ["unit 3", "unit 3 (partial)"])

u3b = Box("short3b")
u3b.script([{"output": {"status": "partial", "summary": "half"}}, {"output": {"status": "partial", "summary": "h"}}])
stop = expect_stop(lambda: heal.write_call(u3b.ctx, work_spec(u3b), [], [], "unit"), "implement")
h.test_result("unit-shortfall: status partial twice stops `implement`", stop is not None and len(u3b.launches()) == 2)

n1 = Box("needs")
n1.script([{"output": {"status": "needs_answers", "summary": "s",
                       "questions": []}, "write_files": {"src/a.py": "v\n"}}])
r = heal.write_call(n1.ctx, work_spec(n1), [], [], "unit n")
h.test_result("write_call: needs_answers is returned as-is, valid work committed",
              r.output is not None and r.output["status"] == "needs_answers" and len(n1.launches()) == 1
              and n1.log_subjects()[0] == "unit n")

p1 = Box("prot1", files={"locked/secret.txt": "orig\n"}, protected=["locked/"])
p1.script([{"write_files": {"src/a.py": "x\n", "locked/secret.txt": "hacked\n"}, "output": DONE,
            "session_id": "S-p"},
           {"write_files": {"src/a.py": "x2\n"}, "output": DONE, "session_id": "S-p"}])
r = heal.write_call(p1.ctx, work_spec(p1), ["src/"], ["locked/"], "unit p")
lines = p1.launches()
h.test_result("protected: touched protected file is reverted, retried with the rule restated, succeeds",
              r.ok and len(lines) == 2 and lines[1]["resume"] == "S-p" and "locked/secret.txt" in lines[1]["prompt"]
              and (p1.clone / "locked" / "secret.txt").read_text() == "orig\n"
              and p1.log_subjects()[0] == "unit p" and len(p1.heals("protected")) == 1)

p2 = Box("prot2", files={"locked/secret.txt": "orig\n"}, protected=["locked/"])
p2.script([], default={"write_files": {"locked/secret.txt": "hacked\n"}, "output": DONE})
stop = expect_stop(lambda: heal.write_call(p2.ctx, work_spec(p2), [], ["locked/"], "unit p"), "protected")
h.test_result("protected: twice stops `protected`, naming the path, file unchanged",
              stop is not None and "locked/secret.txt" in stop.reason and len(p2.launches()) == 2
              and (p2.clone / "locked" / "secret.txt").read_text() == "orig\n"
              and gitops.changed_files(p2.clone) == [], str(stop))

p3 = Box("prot3")
p3.script([{"write_files": {"src/a.py": "x\n", "docs/outside.md": "o\n"}, "output": DONE},
           {"write_files": {"src/a.py": "x\n"}, "output": DONE}])
r = heal.write_call(p3.ctx, work_spec(p3), ["src/"], [], "unit o")
h.test_result("protected: a file outside `allowed` counts and is removed",
              r.ok and not (p3.clone / "docs" / "outside.md").exists() and len(p3.launches()) == 2
              and (p3.clone / "src" / "a.py").exists())

# =============================================================================================
# cleanup
# =============================================================================================

c1 = Box("clean")
attempts = {"n": 0}


def flaky_cleanup() -> None:
    attempts["n"] += 1
    if attempts["n"] < 2:
        raise RuntimeError("worktree busy")


warn = heal.after_merge(c1.ctx, "worktree removal", flaky_cleanup)
h.test_result("cleanup: fails once, retried, no warning", warn is None and attempts["n"] == 2)

attempts["n"] = 0


def broken_cleanup() -> None:
    attempts["n"] += 1
    raise RuntimeError("still busy")


try:
    warn = heal.after_merge(c1.ctx, "worktree removal", broken_cleanup)
    raised = False
except Exception:
    warn = None
    raised = True
h.test_result("cleanup: fails twice gives a warning string and never raises",
              not raised and warn is not None and "still busy" in warn and attempts["n"] == 2
              and any(e.get("cls") == "cleanup" for e in c1.flow.events() if e["kind"] == "heal"), str(warn))
h.test_result("cleanup: success first time is None with no retry",
              heal.after_merge(c1.ctx, "x", lambda: None) is None)

# =============================================================================================
# No stop without a reason
# =============================================================================================

h.test_result("every stop raised in this suite has a class, a reason, and (where healed) tried",
              len(STOPS) >= 15 and all(s.cls and s.reason.strip() for s in STOPS), str(len(STOPS)))
h.test_result("every heal stop carries a stop class from the table",
              all(s.cls in ("limit", "runner", "environment", "conflict", "check", "implement", "permission",
                            "protected", "budget") for s in STOPS))
h.test_result("every heal event is a heal with a cls and an action",
              all("cls" in e and "action" in e for b in (box, e1, w3, s2, u1, p1)
                  for e in b.flow.events() if e["kind"] == "heal"))

if __name__ == "__main__":
    h.summarize_and_exit()
