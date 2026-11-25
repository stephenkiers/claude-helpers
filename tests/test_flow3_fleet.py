#!/usr/bin/env python3
"""
The Flow v3 fleet: parsing, the --max cap, parking, ordering, blocking (fake stages, no model calls).

SERIAL ONLY: the supervisor launches real `python3 -m scripts.flow3` child processes and polls, so this
file needs real wall-clock time (about 30 seconds) and is timing-sensitive under heavy parallel load.
"""
import contextlib
import io
import json
import os
import time
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="flow3-fleet-")).resolve()
os.environ["FLOW3_HOME"] = str(TMP / "home")
os.environ["HOME"] = str(TMP / "userhome")  # no personal principles file leaks in
(TMP / "userhome").mkdir()
PLAN = TMP / "fake-plan.json"
os.environ["FLOW3_FAKE_PLAN"] = str(PLAN)
LOG = TMP / "fake.log"
os.environ["FLOW3_FAKE_LOG"] = str(LOG)
os.environ["FLOW3_FLEET_POLL"] = "0.2"

from _test_harness import Harness  # noqa: E402

from scripts.flow3 import cli, fleet, repoconfig  # noqa: E402
from scripts.workflow.fsutil import nonblocking_flock  # noqa: E402
from scripts.flow3.state import FlowDir  # noqa: E402
from tests import flow3_fake_stages  # noqa: E402

STAGES = flow3_fake_stages.REGISTRY
REAL_STAGE_ENV = "tests.flow3_fake_stages"


def sh(args, cwd):
    return subprocess.run(args, cwd=str(cwd), check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True).stdout.strip()


def make_repo(name, with_config=True):
    origin = TMP / (name + "-origin.git")
    clone = TMP / name
    sh(["git", "init", "--bare", "-q", "-b", "main", str(origin)], TMP)
    sh(["git", "clone", "-q", str(origin), str(clone)], TMP)
    sh(["git", "config", "user.name", "t"], clone)
    sh(["git", "config", "user.email", "t@example.invalid"], clone)
    (clone / "README.md").write_text("hi\n")
    sh(["git", "add", "-A"], clone)
    sh(["git", "commit", "-q", "-m", "init"], clone)
    sh(["git", "push", "-q", "origin", "main"], clone)
    sh(["git", "remote", "set-head", "origin", "main"], clone)
    if with_config:
        cfg = clone / ".claude" / "flow.yaml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text('provider: "local"\ncheck:\n  full: "true"\n')
    issues = origin / "flow3" / "issues"
    issues.mkdir(parents=True)
    for n in range(1, 9):
        (issues / ("%d.md" % n)).write_text("# Ticket %d\n\nBody of %d.\n" % (n, n))
    return clone


def call(argv, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    old = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv, stages=STAGES)
    finally:
        sys.stdin = old
    return rc, out.getvalue(), err.getvalue()


def plan(doc):
    PLAN.write_text(json.dumps(doc))


def flow_of(repo, issue):
    return FlowDir.find(repo, issue)


def status_of(repo, issue):
    return flow_of(repo, issue).flow_state().status



class Done(Exception):
    """Raised from the supervisor's sleep hook when the scenario has what it wanted."""


def run_fleet(fleet_id, until, on_tick=None, limit=90.0):
    """Supervise in this process (children are real subprocesses) until `until()` is true."""
    t0 = time.time()

    def hook(secs):
        time.sleep(secs)
        if on_tick:
            on_tick()
        if until():
            raise Done()
        if time.time() - t0 > limit:
            raise AssertionError("fleet scenario timed out")

    try:
        return fleet.supervise(fleet_id, sleep=hook)
    except Done:
        return None


def status(repo, issue):
    f = FlowDir.find(repo, issue)
    return f.flow_state().status if f else None


def spans():
    """issue -> list of (start, end) for every stage run, from the fake log."""
    open_at, out = {}, {}
    for line in LOG.read_text().splitlines():
        t, issue, stage, what = line.split()
        key = (int(issue), stage)
        if what == "start":
            open_at[key] = float(t)
        else:
            out.setdefault(int(issue), []).append((open_at.pop(key), float(t)))
    return out


def max_overlap(per_issue):
    events = []
    for issue, rows in per_issue.items():
        events += [(min(s for s, _ in rows), 1), (max(e for _, e in rows), -1)]
    events.sort(key=lambda e: (e[0], e[1]))
    cur = best = 0
    for _, d in events:
        cur += d
        best = max(best, cur)
    return best


def tail_logs():
    return "\n".join("%s: %s" % (p.name, p.read_text()[-400:]) for p in sorted(TMP.glob("home/*/fleets/*.log")))


def new_fleet(repo_root, spec, max_parallel):
    repo = repoconfig.repo_name(repo_root)
    return fleet.start(repo, repo_root, fleet.parse_specs(spec), max_parallel,
                       launch=lambda args, log: 0), repo


h = Harness("Flow v3 fleet")

# ---- parse_specs ------------------------------------------------------------------------------------
def shape(text):
    return [(t.issue, t.after) for t in fleet.parse_specs(text)]


def bad(text):
    try:
        fleet.parse_specs(text)
    except fleet.FleetError:
        return True
    return False


h.test_result("parse: independent tickets", shape("12,13,14") == [(12, []), (13, []), (14, [])])
h.test_result("parse: a chain", shape("12>13>14") == [(12, []), (13, [12]), (14, [13])])
h.test_result("parse: mixed, with spaces and #", shape("#12 > 13, 14") == [(12, []), (13, [12]), (14, [])])
h.test_result("parse: a ticket named twice keeps every prerequisite",
              shape("1>3,2>3") == [(1, []), (3, [1, 2]), (2, [])])
h.test_result("parse: junk, empty, zero, self and cycles are errors",
              bad("") and bad("a") and bad("1,,2") and bad("0") and bad("1>1") and bad("1>2,2>1") and bad("1>"))

# ---- the cap -------------------------------------------------------------------------------------------
proj = make_repo("proj")
REPO = repoconfig.repo_name(proj)
os.environ["FLOW3_STAGES"] = REAL_STAGE_ENV
call(["doctor", "--repo-root", str(proj)])
plan({str(i): {"plan": {"mode": "done", "secs": 1.5}} for i in (1, 2, 3)})
fid, _ = new_fleet(proj, "1,2,3", 2)
rc = run_fleet(fid, lambda: all(status(REPO, i) == "merge_gate" for i in (1, 2, 3)))
per = spans()
h.test_result("all three tickets reach the merge gate", all(status(REPO, i) == "merge_gate" for i in (1, 2, 3)),
              tail_logs())
h.test_result("never more than --max run at once, and two do run together", max_overlap(per) == 2, str(per))
h.test_result("the third waited for a slot", min(s for s, _ in per[3]) >= min(e for _, e in per[1] + per[2]) - 0.05)
text = fleet.status_text(fleet.load_fleet(fid))
h.test_result("fleet status shows each ticket's state and flow", "#1" in text and "merge_gate" in text, text)
rc, out, err = call(["fleet", "status", fid])
h.test_result("fleet status through the cli", rc == 0 and fid in out, out + err)
rc, out, err = call(["fleet", "status", "--repo", REPO])
h.test_result("fleet status lists the repo's fleets", rc == 0 and fid in out, out + err)

# ---- a parked ticket does not hold the others --------------------------------------------------------
proj2 = make_repo("proj2")
REPO2 = repoconfig.repo_name(proj2)
call(["doctor", "--repo-root", str(proj2)])
LOG.unlink()
plan({"1": {"plan": {"mode": "needs_human"}}, "2": {"plan": {"secs": 0.5}}, "3": {"plan": {"secs": 0.5}}})
fid, _ = new_fleet(proj2, "1,2,3", 1)
run_fleet(fid, lambda: status(REPO2, 2) == "merge_gate" and status(REPO2, 3) == "merge_gate")
h.test_result("with --max 1, a parked ticket frees its slot so 2 and 3 finish",
              status(REPO2, 1) == "waiting_human" and status(REPO2, 2) == "merge_gate"
              and status(REPO2, 3) == "merge_gate", tail_logs())
states = fleet.classify(fleet.load_fleet(fid))
h.test_result("the parked ticket is classified parked, not active", states[1] == "parked", str(states))
rc, out, err = call(["inbox", "--repo", REPO2])
h.test_result("its question is in the inbox", "q-1-plan" in out, out)

# ---- ordering: B starts only once A is done -----------------------------------------------------------
proj3 = make_repo("proj3")
REPO3 = repoconfig.repo_name(proj3)
call(["doctor", "--repo-root", str(proj3)])
LOG.unlink()
plan({})
fid, _ = new_fleet(proj3, "1>2", 3)
seen = {"waited": False, "merged": False}


def tick3():
    if status(REPO3, 1) == "merge_gate" and not seen["merged"]:
        seen["waited"] = FlowDir.find(REPO3, 2) is None  # 1 is at the gate, not done: 2 must not exist yet
        time.sleep(0.6)
        seen["waited"] = seen["waited"] and FlowDir.find(REPO3, 2) is None
        seen["merged"] = True
        call(["merge", "1", "--repo", REPO3, "--merge", "--wait"])


run_fleet(fid, lambda: status(REPO3, 2) == "merge_gate", on_tick=tick3)
per = spans()
h.test_result("B did not start while A sat at the merge gate", seen["waited"], str(seen))
h.test_result("B started only after A was merged and done",
              status(REPO3, 1) == "done" and status(REPO3, 2) == "merge_gate"
              and min(s for s, _ in per[2]) >= max(e for _, e in per[1]) - 0.05, tail_logs())

# ---- a stopped prerequisite blocks its dependants; the supervisor exits on its own -------------------
proj4 = make_repo("proj4")
REPO4 = repoconfig.repo_name(proj4)
call(["doctor", "--repo-root", str(proj4)])
plan({"1": {"plan": {"mode": "stop"}}, "3": {"plan": {"mode": "stop"}}})
fid, _ = new_fleet(proj4, "1>2,3", 2)
rc = run_fleet(fid, lambda: False)
states = fleet.classify(fleet.load_fleet(fid))
h.test_result("the supervisor exits by itself when only stopped chains remain", rc == 0, str(rc))
h.test_result("1 is stopped, 2 is blocked behind it, 3 ran independently and stopped too",
              states == {1: "stopped", 2: "blocked", 3: "stopped"} and FlowDir.find(REPO4, 2) is None, str(states))
text = fleet.status_text(fleet.load_fleet(fid))
h.test_result("status names the blocked ticket and what it waits on", "blocked" in text and "after #1" in text, text)

# ---- one supervisor per fleet --------------------------------------------------------------------------
with nonblocking_flock(fleet.fleets_dir(REPO4) / ("%s.lock" % fid)):
    h.test_result("a second supervisor for the same fleet exits at once", fleet.supervise(fid) == 0)

# ---- a launch that never starts is retried, then called stopped --------------------------------------------
proj5 = make_repo("proj5")
REPO5 = repoconfig.repo_name(proj5)
launches = []


def dead_launch(args, log):
    launches.append(args)
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


fid, _ = new_fleet(proj5, "1", 1)
fleet.supervise(fid, launch=dead_launch, sleep=lambda s: None)
states = fleet.classify(fleet.load_fleet(fid))
h.test_result("a launch that never creates its flow is tried %d times, then stopped" % fleet.MAX_LAUNCHES,
              len(launches) == fleet.MAX_LAUNCHES and states == {1: "stopped"}, "%s %s" % (launches, states))
h.test_result("the first launch is `run`, with the repo root",
              launches[0][:2] == ["run", "1"] and str(proj5) in launches[0], str(launches))

# ---- the cli front door --------------------------------------------------------------------------------------
launched = []
fleet.launch_detached = lambda args, log: launched.append(list(args)) or 1
rc, out, err = call(["fleet", "start", "1,2", "--max", "2", "--repo-root", str(proj5)])
h.test_result("fleet start refuses before doctor has passed", rc == 1 and "doctor" in err, err)
call(["doctor", "--repo-root", str(proj5)])
rc, out, err = call(["fleet", "start", "1,2", "--max", "0", "--repo-root", str(proj5)])
h.test_result("fleet start rejects --max 0", rc == 1 and "--max" in err, err)
rc, out, err = call(["fleet", "start", "2>1,1>2", "--repo-root", str(proj5)])
h.test_result("fleet start rejects a cycle", rc == 1 and "cycle" in err, err)
rc, out, err = call(["fleet", "start", "1,2", "--max", "2", "--repo-root", str(proj5)])
h.test_result("fleet start launches one detached supervisor and says what to run next",
              rc == 0 and launched and launched[-1][:2] == ["fleet", "supervise"] and "fleet status" in out
              and "nothing is answered or merged for you" in out, "%s %s" % (launched, out + err))
rc, out, err = call(["fleet", "status", "no-such-fleet"])
h.test_result("fleet status of an unknown id is exit 1", rc == 1, err)

h.summarize_and_exit()
