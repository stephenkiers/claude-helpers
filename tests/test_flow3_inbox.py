#!/usr/bin/env python3
"""The Flow v3 inbox: grouping, ordering, full cards, stops, totals (fake stages, no model calls)."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="flow3-inbox-")).resolve()
os.environ["FLOW3_HOME"] = str(TMP / "home")
os.environ["HOME"] = str(TMP / "userhome")  # no personal principles file leaks in
(TMP / "userhome").mkdir()
PLAN = TMP / "fake-plan.json"
os.environ["FLOW3_FAKE_PLAN"] = str(PLAN)

from _test_harness import Harness  # noqa: E402

from datetime import datetime, timedelta, timezone  # noqa: E402

from scripts.flow3 import cli, inbox, repoconfig  # noqa: E402
from scripts.flow3.state import FlowDir  # noqa: E402
from tests import flow3_fake_stages  # noqa: E402

STAGES = flow3_fake_stages.REGISTRY


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


h = Harness("Flow v3 inbox")

proj = make_repo("proj")
REPO = repoconfig.repo_name(proj)
call(["doctor", "--repo-root", str(proj)])

h.test_result("an empty inbox says so", "Nothing is waiting for you." in inbox.render(inbox.collect(REPO)))

plan({"1": {"plan": {"mode": "needs_human"}}, "2": {"plan": {"mode": "stop"}}, "3": {"plan": {"mode": "needs_human"}},
      "4": {"plan": {"mode": "stop"}}})
call(["run", "1", "--repo-root", str(proj)])
call(["run", "2", "--repo-root", str(proj)])
call(["run", "3", "--repo-root", str(proj)])
call(["run", "4", "--repo-root", str(proj)])
plan({})
call(["run", "5", "--repo-root", str(proj)])  # straight to the merge gate
call(["run", "6", "--repo-root", str(proj)])

data = inbox.collect(REPO)
h.test_result("questions, merge gates and stops are grouped",
              [q["issue"] for q in data["questions"]] == [1, 3]
              and [g["issue"] for g in data["merge_gates"]] == [5, 6]
              and [s["issue"] for s in data["stops"]] == [2, 4], json.dumps({k: [x["issue"] for x in v] for k, v in data.items() if k in ("questions", "merge_gates", "stops")}))
h.test_result("each group is oldest first",
              all(g[i]["since"] <= g[i + 1]["since"] for g in (data["questions"], data["merge_gates"], data["stops"])
                  for i in range(len(g) - 1)))
text = inbox.render(data)
h.test_result("a question shows its whole card, not a pointer",
              "Ship the fake plan step for #1?" in text and "Go ahead" in text and "The flow continues" in text
              and "q-1-plan" in text and "answer 1" in text, text)
h.test_result("a stop shows class, reason, and the resume command",
              "class: runner" in text and "fake stop at plan for #2" in text
              and "python3 -m scripts.flow3 resume %s 2" % REPO in text, text)
h.test_result("a merge gate says how to see and decide it", "gate 5" in text and "merge 5 --merge" in text, text)
h.test_result("section headers carry counts", "QUESTIONS (2)" in text and "MERGE GATES (2)" in text and "STOPS (2)" in text)

# a batch with every card answered but not yet driven is not shown as an open question card
flow3 = FlowDir.find(REPO, 3)
cli_rc = call(["answer", "3", "--repo", REPO, "--wait"], "q-3-plan: go\n")[0]
h.test_result("answered flows leave the question group", cli_rc == 0
              and [q["issue"] for q in inbox.collect(REPO)["questions"]] == [1])

# costs, totals, soft budget
f1 = FlowDir.find(REPO, 1)
f1.event("call", role="planner", cost_usd=1.25)
f1.event("call", role="planner", cost_usd=0.5)
f5 = FlowDir.find(REPO, 5)
f5.event("call", role="x", cost_usd=2.0)
f5.set("budget_soft_noted", True)
f5.event("budget_soft", soft_usd=5.0)
data = inbox.collect(REPO)
rows = {r["issue"]: r for r in data["flows"]}
h.test_result("flow rows carry cost, and the soft-budget flag",
              abs(rows[1]["cost_usd"] - 1.75) < 1e-6 and rows[5]["over_soft_budget"] and not rows[1]["over_soft_budget"])
h.test_result("today's total sums call costs from today", abs(data["today"]["cost_usd"] - 3.75) < 1e-6
              and data["today"]["flows"] == 2, str(data["today"]))
later = datetime.now(timezone.utc) + timedelta(days=2)
h.test_result("a different day has its own total", inbox.collect(REPO, now=later)["today"]["cost_usd"] == 0.0)
text = inbox.render(data)
h.test_result("the soft budget is noted without stopping anything", "over its soft budget of $5.00" in text, text)
h.test_result("elapsed time formats", inbox.fmt_secs(5) == "5s" and inbox.fmt_secs(600) == "10m"
              and inbox.fmt_secs(3 * 3600 + 300) == "3h05m")

# a flow that says running but has no process is called out
fl7 = FlowDir.create(REPO, 7, "Orphan", proj)
h.test_result("a running flow with no live process is flagged",
              "no live process" in inbox.render(inbox.collect(REPO)))
import fcntl  # noqa: E402
fd = os.open(str(fl7.path / ".run.lock"), os.O_WRONLY | os.O_CREAT)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
h.test_result("is_running sees a held run lock", inbox.is_running(fl7))
os.close(fd)
h.test_result("is_running is false once it is released", not inbox.is_running(fl7))

# cli: inbox --json and the repo filter
rc, out, err = call(["inbox", "--repo", REPO, "--json"])
doc = json.loads(out)
h.test_result("inbox --json is the same data", rc == 0 and [q["issue"] for q in doc["questions"]] == [1]
              and doc["stops"][0]["resume_command"].endswith("resume %s 2" % REPO), out[:200])
rc, out, err = call(["inbox"])
h.test_result("inbox with no --repo covers every repo", rc == 0 and "STOPS (2)" in out, out[:200])
other = make_repo("other")
call(["doctor", "--repo-root", str(other)])
call(["run", "1", "--repo-root", str(other)])
rc, out, err = call(["inbox", "--repo", repoconfig.repo_name(other), "--json"])
h.test_result("--repo narrows the inbox to one repo", json.loads(out)["stops"] == []
              and len(json.loads(out)["merge_gates"]) == 1, out[:200])
rc, out, err = call(["inbox"])
h.test_result("all-repos inbox sees both", "MERGE GATES (4)" in out, out)

h.summarize_and_exit()
