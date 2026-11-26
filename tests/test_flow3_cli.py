#!/usr/bin/env python3
"""The Flow v3 command line, driven in-process with fake stages (no model calls)."""
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
TMP = Path(tempfile.mkdtemp(prefix="flow3-cli-")).resolve()
os.environ["FLOW3_HOME"] = str(TMP / "home")
os.environ["HOME"] = str(TMP / "userhome")  # no personal principles file leaks in
(TMP / "userhome").mkdir()
PLAN = TMP / "fake-plan.json"
os.environ["FLOW3_FAKE_PLAN"] = str(PLAN)

from _test_harness import Harness  # noqa: E402

from scripts.flow3 import cli, fleet, principles, repoconfig  # noqa: E402
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


h = Harness("Flow v3 cli")

# ---- init, doctor ---------------------------------------------------------------------------
bare = make_repo("bare", with_config=False)
rc, out, err = call(["init", "--repo-root", str(bare)])
h.test_result("init without a detectable check still writes, loudly, exit 0",
              rc == 0 and repoconfig.external_config_file(bare).exists() and "check.full" in out, out + err)
h.test_result("init writes outside the repo by default, leaving the checkout untouched",
              not (bare / ".claude").exists() and sh(["git", "status", "--porcelain"], bare) == "", out)
inrepo = make_repo("inrepo", with_config=False)
rc, out, err = call(["init", "--repo-root", str(inrepo), "--in-repo", "--set", "check.full=true"])
h.test_result("init --in-repo writes .claude/flow.yaml in the repo",
              rc == 0 and (inrepo / ".claude/flow.yaml").exists()
              and not repoconfig.external_config_file(inrepo).exists(), out + err)
rc, out, err = call(["init", "--repo-root", str(bare)])
h.test_result("init refuses to overwrite", rc == 1 and "--force" in err, err)
rc, out, err = call(["init", "--repo-root", str(bare), "--force", "--set", "check.full=true",
                     "--set", "protected_paths=db/migrations,package-lock.json", "--set", "provider=local"])
cfg = repoconfig.load(bare)
h.test_result("init --force --set writes a file that parses, with list and dotted keys",
              rc == 0 and cfg.check_full == "true" and cfg.protected_paths == ("db/migrations", "package-lock.json"),
              out + err)
rc, out, err = call(["init", "--repo-root", str(bare), "--force", "--set", "nope=1"])
h.test_result("init rejects an unknown --set key", rc == 1 and "KEY=VALUE" in err, err)

proj = make_repo("proj")
REPO = repoconfig.repo_name(proj)
rc, out, err = call(["run", "1", "--repo-root", str(proj)])
h.test_result("run is refused until doctor has passed", rc == 1 and "doctor" in err, err)
rc, out, err = call(["doctor", "--repo-root", str(proj)])
h.test_result("doctor passes on a good config", rc == 0 and "passed" in out, out + err)

# ---- run to the gate --------------------------------------------------------------------------
plan({})
rc, out, err = call(["run", "1", "--repo-root", str(proj)])
fl = flow_of(REPO, 1)
h.test_result("run takes the title from the provider and reaches the merge gate",
              rc == 0 and fl is not None and fl.get("title") == "Ticket 1" and status_of(REPO, 1) == "merge_gate"
              and "gate 1" in out, out + err)
rc, out, err = call(["run", "1", "--repo-root", str(proj)])
h.test_result("run twice is refused", rc == 1 and "exists" in err, err)
rc, out, err = call(["run", "99", "--repo-root", str(proj)])
h.test_result("run for a missing issue is one error line", rc == 1 and err.count("\n") == 1, err)

# ---- merge decisions ---------------------------------------------------------------------------
rc, out, err = call(["gate", "1", "--repo", REPO])
h.test_result("gate prints the merge screen", rc == 0 and "MERGE GATE" in out, out + err)
rc, out, err = call(["merge", "1", "--repo", REPO, "--send-back", "  "])
h.test_result("send-back with empty notes is refused", rc == 1 and "notes" in err, err)
rc, out, err = call(["merge", "1", "--repo", REPO])
h.test_result("merge with no choice is a usage error", rc == 1, err)
rc, out, err = call(["merge", "1", "--repo", REPO, "--merge", "--stop"])
h.test_result("merge choices are mutually exclusive", rc == 1, err)

launched = []
real_launch = fleet.launch_detached
fleet.launch_detached = lambda args, log: launched.append((list(args), str(log))) or 4242
rc, out, err = call(["merge", "1", "--repo", REPO, "--send-back", "tighten the docs"])
fl = flow_of(REPO, 1)
h.test_result("send-back saves the direction and the notes, then detaches a drive child",
              rc == 0 and launched and launched[-1][0][:4] == ["drive", REPO, "1", "merge_decision"]
              and "send_back" in launched[-1][0] and "tighten the docs" in launched[-1][0]
              and fl.data("sendback") == {"notes": "tighten the docs", "handled": False}
              and any("tighten the docs" in str(d.get("text")) for d in fl.get("directions")),
              "%s %s" % (launched, out + err))
fleet.launch_detached = real_launch
rc, out, err = call(["merge", "1", "--repo", REPO, "--send-back", "tighten the docs", "--wait"])
fl = flow_of(REPO, 1)
h.test_result("send-back --wait drives in this process, back to the gate",
              rc == 0 and status_of(REPO, 1) == "merge_gate" and fl.flow_state().sendbacks == 1, out + err)
rc, out, err = call(["merge", "1", "--repo", REPO, "--stop", "not now", "--wait"])
h.test_result("--stop stops with the human's words and exits 2", rc == 2 and "not now" in out
              and status_of(REPO, 1) == "stopped", out + err)
rc, out, err = call(["merge", "1", "--repo", REPO, "--merge"])
h.test_result("merge when not at the gate is refused", rc == 1 and "not at the merge gate" in err, err)
rc, out, err = call(["resume", REPO, "1"])
h.test_result("resume of a human stop at the gate returns to the gate", rc == 0 and status_of(REPO, 1) == "merge_gate",
              out + err)
rc, out, err = call(["merge", "1", "--repo", REPO, "--merge", "--wait"])
h.test_result("--merge finishes the flow", rc == 0 and status_of(REPO, 1) == "done" and "done" in out, out + err)

# ---- stop, resume -----------------------------------------------------------------------------
plan({"2": {"plan": {"mode": "stop"}}})
rc, out, err = call(["run", "2", "--repo-root", str(proj)])
h.test_result("a stage stop prints the stop screen and exits 2", rc == 2 and "STOPPED" in out
              and "fake stop" in out, out + err)
rc, out, err = call(["stop-screen", "2", "--repo", REPO])
h.test_result("stop-screen shows the stop", rc == 0 and "fake stop at plan" in out, out)
plan({})
rc, out, err = call(["resume", "2", "--repo-root", str(proj)])
h.test_result("resume by issue alone (cwd repo) continues to the gate", rc == 0 and status_of(REPO, 2) == "merge_gate",
              out + err)
rc, out, err = call(["resume", REPO, "777"])
h.test_result("resume of an unknown flow is exit 1", rc == 1, err)
rc, out, err = call(["why", "2", "nothing", "--repo", REPO])
h.test_result("why on an unknown query answers, exit 0", rc == 0 and "No decision" in out, out)

# ---- parking, answering ----------------------------------------------------------------------------
plan({"3": {"plan": {"mode": "needs_human"}}, "4": {"plan": {"mode": "needs_human"}}})
rc3, out3, _ = call(["run", "3", "--repo-root", str(proj)])
rc4, out4, _ = call(["run", "4", "--repo-root", str(proj)])
h.test_result("a park exits 0 and points at the inbox", rc3 == 0 and rc4 == 0 and "inbox" in out3
              and status_of(REPO, 3) == "waiting_human", out3 + out4)
rc, out, err = call(["inbox", "--repo", REPO])
h.test_result("inbox shows both questions in full", rc == 0 and "q-3-plan" in out and "q-4-plan" in out
              and "Go ahead" in out and "answer 3" in out, out)
rc, out, err = call(["answer", "3", "--repo", REPO], "")
h.test_result("answer with nothing on stdin is refused", rc == 1 and "stdin" in err, err)
launched.clear()
fleet.launch_detached = lambda args, log: launched.append((list(args), str(log))) or 4243
rc, out, err = call(["answer", "3", "--repo", REPO], "q-3-plan: go\n")
fleet.launch_detached = real_launch
h.test_result("answer records, then detaches `drive ... answered` by default",
              rc == 0 and launched and launched[-1][0] == ["drive", REPO, "3", "answered"]
              and "All answered" in out, "%s %s" % (launched, out + err))
# what the detached child does, in process:
rc, out, err = call(["drive", REPO, "3", "answered"])
h.test_result("the drive child sends `answered` and the flow runs on to the gate",
              rc == 0 and status_of(REPO, 3) == "merge_gate", out + err)
rc, out, err = call(["drive", REPO, "3", "answered"])
h.test_result("a repeated drive event is a one-line error, not a crash", rc == 1 and err.count("\n") == 1, err)

# reply across two flows: 4 still parked; 5 parks fresh
plan({"4": {"plan": {"mode": "needs_human"}}, "5": {"plan": {"mode": "needs_human"}}})
call(["run", "5", "--repo-root", str(proj)])
blocks = cli.split_reply("#4: q-4-plan: go\nmore for four\n#5: q-5-plan: go")
h.test_result("split_reply gives each block its own text (rest of the header line included)",
              blocks == [(4, "q-4-plan: go\nmore for four"), (5, "q-5-plan: go")], str(blocks))
h.test_result("split_reply marks unheaded text as issue 0", cli.split_reply("hello\n") == [(0, "hello")])
rc, out, err = call(["reply", "--repo", REPO], "intro line\n#4: q-4-plan: go\n")
h.test_result("text before the first header is refused", rc == 1 and "header" in err, err)
rc, out, err = call(["reply", "--repo", REPO], "q-4-plan: go\n")
h.test_result("an unheaded reply with two open tickets lists them", rc == 1 and "#4" in err and "#5" in err, err)
rc, out, err = call(["reply", "--repo", REPO, "--wait"], "#4: q-4-plan: go\n#5: q-5-plan: go\n")
h.test_result("reply answers both tickets and drives both to the gate",
              rc == 0 and status_of(REPO, 4) == "merge_gate" and status_of(REPO, 5) == "merge_gate", out + err)
rc, out, err = call(["reply", "--repo", REPO], "#4: hi\n#4: again\n")
h.test_result("two blocks for one issue are refused", rc == 1, err)

# a partly answered batch stays parked
plan({"6": {"plan": {"mode": "needs_human"}}})
call(["run", "6", "--repo-root", str(proj)])
rc, out, err = call(["answer", "6", "--repo", REPO, "--wait"], "q-6-plan: wait\n")
h.test_result("a fast-path answer completes the batch and the flow moves on", rc == 0
              and status_of(REPO, 6) == "merge_gate", out + err)

# ---- say, overrule ------------------------------------------------------------------------------------
rc, out, err = call(["say", "6", "keep", "the", "diff", "small", "--repo", REPO])
h.test_result("say saves a binding direction",
              rc == 0 and any(d["text"] == "keep the diff small" for d in flow_of(REPO, 6).get("directions")), out + err)
rc, out, err = call(["answer", "6", "--repo", REPO, "--wait"], "also mention the tests\n")
h.test_result("answer with no open question is kept as a direction", rc == 0 and "direction" in out
              and any(d["text"].startswith("also mention") for d in flow_of(REPO, 6).get("directions")), out + err)
rc, out, err = call(["overrule", "6", "q-6-plan", "wait", "--repo", REPO])
h.test_result("overrule needs a reason", rc == 1 and "--reason" in err, err)
rc, out, err = call(["overrule", "6", "q-6-plan", "wait", "--repo", REPO, "--reason", "I changed my mind"])
rows = [r for r in principles.read_decisions(REPO) if r.get("overruled")]
h.test_result("overrule marks the decision and adds a direction",
              rc == 0 and rows and rows[-1]["answer"] == "wait" and "send-back" in out
              and any("Overruled" in d["text"] for d in flow_of(REPO, 6).get("directions")), out + err)

# ---- principles ---------------------------------------------------------------------------------------
rc, out, err = call(["principles", "init", "--repo-root", str(proj), "--themes", "docs-follow-code,P-fail-closed"])
ext_principles = repoconfig.external_principles_file(proj)
text = ext_principles.read_text() if ext_principles.exists() else ""
h.test_result("principles init writes outside the repo by default", not (proj / "docs/principles.md").exists())
goal, plist = principles.parse(text, "project")
h.test_result("principles init writes the chosen seeds and a placeholder goal",
              rc == 0 and [p.id for p in plist] == ["P-docs-follow-code", "P-fail-closed"] and "Edit the Goal" in out,
              out + err + text)
class _Ctx:
    repo_root = proj
    cfg = repoconfig.load(proj)


h.test_result("a flow reads the principles file kept outside the repo",
              [p.id for p in principles.load_for(_Ctx)[1]][:2] == ["P-docs-follow-code", "P-fail-closed"])
rc, out, err = call(["principles", "init", "--repo-root", str(proj)])
h.test_result("principles init never overwrites", rc == 1 and "never overwritten" in err, err)
rc, out, err = call(["principles", "init", "--repo-root", str(inrepo), "--in-repo", "--themes", "P-fail-closed"])
h.test_result("principles init --in-repo writes the repo's own file",
              rc == 0 and (inrepo / "docs/principles.md").exists(), out + err)
rc, out, err = call(["principles", "init", "--repo-root", str(inrepo)])
h.test_result("principles init refuses an outside file the repo's own would shadow",
              rc == 1 and "wins over" in err, err)
rc, out, err = call(["principles", "suggest", "--repo", REPO])
h.test_result("principles suggest works with little history", rc == 0, out + err)
rc, out, err = call(["principles", "init", "--repo-root", str(bare), "--themes", "bogus"])
h.test_result("principles init rejects an unknown theme", rc == 1 and "unknown theme" in err, err)

# ---- detach on run, status, usage -----------------------------------------------------------------------
launched.clear()
fleet.launch_detached = lambda args, log: launched.append((list(args), str(log))) or 4244
rc, out, err = call(["run", "7", "--repo-root", str(proj), "--detach"])
fleet.launch_detached = real_launch
h.test_result("run --detach creates the flow in the parent and launches `drive ... resume`",
              rc == 0 and flow_of(REPO, 7) is not None and launched[-1][0] == ["drive", REPO, "7", "resume"]
              and launched[-1][1].endswith("driver.log"), "%s %s" % (launched, out + err))
rc, out, err = call(["status", "--repo", REPO, "--json"])
doc = json.loads(out)
h.test_result("status --json lists every flow with cost and stage",
              rc == 0 and {f["issue"] for f in doc["flows"]} >= {1, 2, 3, 6, 7} and "today" in doc, out[:300])
rc, out, err = call(["status"])
h.test_result("status with no repo shows all repos", rc == 0 and "FLOWS" in out and "Today" in out, out)
h.test_result("bad usage exits 1", call(["bogus"])[0] == 1 and call([])[0] == 1 and call(["run"])[0] == 1)
h.test_result("--help exits 0", call(["--help"])[0] == 0)
h.test_result("fleet needs a subcommand", call(["fleet"])[0] == 1)
rc, out, err = call(["fleet", "start", "1>x", "--repo-root", str(proj)])
h.test_result("fleet start with a bad spec is one error line", rc == 1 and err.count("\n") == 1, err)

h.summarize_and_exit()
