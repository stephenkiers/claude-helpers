#!/usr/bin/env python3
"""
Flow v3 end to end: the real CLI, the real stage modules, the real merge gate and the local provider, with
only the model replaced by tests/flow3_fake_claude.py. No FLOW3_STAGES override is used anywhere.
"""
import atexit
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="flow3-integ-")).resolve()
os.environ["FLOW3_HOME"] = str(TMP / "home")
os.environ["HOME"] = str(TMP / "userhome")
(TMP / "userhome").mkdir()


FLEET_IDS = []


def _cleanup():
    """Stop any fleet supervisor this run started (it lives in this sandbox's FLOW3_HOME), then drop the sandbox."""
    for fleet_id in FLEET_IDS:
        subprocess.run(["pkill", "-f", "fleet supervise " + fleet_id], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    shutil.rmtree(str(TMP), ignore_errors=True)


atexit.register(_cleanup)
os.environ["FLOW3_CLAUDE_BIN"] = str(ROOT / "tests" / "flow3_fake_claude.py")
os.environ.pop("FLOW3_STAGES", None)
SCRIPT = TMP / "fake-script.json"
LOG = TMP / "fake-log.jsonl"
os.environ["FLOW3_FAKE_SCRIPT"] = str(SCRIPT)
os.environ["FLOW3_FAKE_LOG"] = str(LOG)
os.environ["FLOW3_FLEET_POLL"] = "0.3"

from _test_harness import Harness  # noqa: E402

from scripts.flow3 import cli, heal, provider, repoconfig, runner  # noqa: E402
from scripts.flow3.state import FlowDir  # noqa: E402

heal._sleep = lambda s: None  # type: ignore[assignment]
runner._sleep = lambda s: None  # type: ignore[assignment]

NICK = "North Star Nick"
CHECK_PY = '''import glob, subprocess, sys
bad = 0
for f in sorted(glob.glob("tests/test_*.py")):
    r = subprocess.run([sys.executable, f], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True)
    if r.returncode:
        print("FAILED " + f)
        print(r.stderr[-400:])
        bad = 1
sys.exit(bad)
'''
SPEC_TEST = '''import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.calc import add
from src.cli import run
assert add(2, 3) == 5
assert run(1, 2) == 3
'''
CALC = "def add(a, b):\n    return a + b\n"
CLI = "from src.calc import add\n\n\ndef run(a, b):\n    return add(a, b)\n"
PRINCIPLES = ("# Goal\nA small calculator.\n\n## P-x: Reuse\nStatement: Reuse the helper.\n"
              "Spirit: One place to fix.\nRevisit if: The helper does not fit.\n")


def sh(args, cwd):
    return subprocess.run(args, cwd=str(cwd), check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True).stdout.strip()


def make_sandbox(name, issues=(1,), yaml_extra="", principles=True, red_base=False, dep_of=None):
    """A bare origin, a clone with a tiny project and a real check, flow.yaml, and local-provider issues."""
    origin = TMP / (name + "-origin.git")
    clone = TMP / name
    sh(["git", "init", "--bare", "-q", "-b", "main", str(origin)], TMP)
    sh(["git", "clone", "-q", str(origin), str(clone)], TMP)
    sh(["git", "config", "user.name", "t"], clone)
    sh(["git", "config", "user.email", "t@example.invalid"], clone)
    (clone / ".claude").mkdir()
    (clone / ".claude" / "flow.yaml").write_text(
        "provider: local\ncheck:\n  full: 'python3 check.py'\n  timeout_secs: 60\n" + yaml_extra)
    (clone / "check.py").write_text(CHECK_PY)
    (clone / "README.md").write_text("calculator\n")
    (clone / ".gitignore").write_text("__pycache__/\n")
    (clone / "tests").mkdir()
    if red_base:
        (clone / "tests" / "test_old.py").write_text("assert 1 == 2\n")
    if principles:
        (clone / "docs").mkdir()
        (clone / "docs" / "principles.md").write_text(PRINCIPLES)
    sh(["git", "add", "-A"], clone)
    sh(["git", "commit", "-q", "-m", "init"], clone)
    sh(["git", "push", "-q", "origin", "main"], clone)
    sh(["git", "remote", "set-head", "origin", "main"], clone)
    idir = origin / "flow3" / "issues"
    idir.mkdir(parents=True)
    for n in issues:
        dep = "Depends on: #%d\n\n" % dep_of[n] if dep_of and n in dep_of else ""
        (idir / ("%d.md" % n)).write_text("# Add calc %d\n\n%sAdd src/calc.py and src/cli.py.\n" % (n, dep))
    return clone, origin, repoconfig.repo_name(clone)


T00 = time.time()


def call(argv, stdin=""):
    _t0 = time.time()
    try:
        return _call(argv, stdin)
    finally:
        if os.environ.get('IT_TRACE'):
            sys.stderr.write('%.1f %.1fs %s\n' % (_t0 - T00, time.time() - _t0, ' '.join(argv[:3])))


def _call(argv, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    old = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv)
    finally:
        sys.stdin = old
    return rc, out.getvalue(), err.getvalue()


def script(calls, default=None):
    for p in (SCRIPT, Path(str(SCRIPT) + ".consumed"), LOG):
        if p.exists():
            p.unlink()
    doc = {"calls": calls}
    if default is not None:
        doc["default"] = default
    SCRIPT.write_text(json.dumps(doc))


def invocations(role=None):
    if not LOG.exists():
        return []
    rows = [json.loads(x) for x in LOG.read_text().splitlines() if x.strip()]
    return [r for r in rows if role is None or r["role"] == role]


def flow_of(repo, issue):
    return FlowDir.find(repo, issue)


def status_of(repo, issue):
    fl = flow_of(repo, issue)
    return fl.flow_state().status if fl else None


def describe(repo, issue):
    """Status plus, for a stopped flow, why it stopped, so a flaky failure names its cause."""
    fl = flow_of(repo, issue)
    if not fl:
        return "no flow"
    st = fl.flow_state()
    if st.stop:
        return "%s (stop %s at %s: %s)" % (st.status, st.stop.cls, st.stop.stage, st.stop.reason)
    return "%s at %s" % (st.status, st.stage)


def good_plan():
    return {
        "goal": "Add a calculator",
        "non_goals": [],
        "acceptance_criteria": [{"id": "c1", "text": "add works"}, {"id": "c2", "text": "run uses add"}],
        "units": [
            {"id": "u1", "title": "Core", "files": ["src/calc.py"], "steps": ["write add"], "acceptance": ["c1"]},
            {"id": "u2", "title": "Cli", "files": ["src/cli.py"], "steps": ["write run"], "acceptance": ["c2"],
             "after": ["u1"]},
        ],
        "interfaces": [],
        "test_spec": [{"id": "t1", "behaviour": "add and run work", "test_file": "tests/test_calc.py"}],
    }


def plan_calls(questions=None, extra=None):
    """Router, the experts, synthesis (asking `questions` first when given), and an auditor pass."""
    calls = [{"role": "plan_router", "output": {"experts": ["Rachel", "Tara TypeSafe"]}}]
    calls += [{"role": "plan_expert", "label": n, "output": {"advice": ["ADVICE-" + n]}}
              for n in (NICK, "Rachel", "Tara TypeSafe")]
    if questions:
        calls.append({"role": "plan_synthesis", "output": {"status": "needs_answers", "questions": questions}})
    calls += list(extra or [])
    calls.append({"role": "plan_synthesis", "output": {"status": "done", "plan": good_plan()}})
    calls.append({"role": "plan_auditor", "output": {"verdict": "pass"}})
    return calls


DONE = {"status": "done", "summary": "did it"}


def work_calls(tag="1"):
    # The plan always names tests/test_calc.py. Once an earlier ticket has merged that file, the
    # spec tests stage sends the author to a new sibling file instead (existing tests are not its to edit).
    spec_file = {"2": "tests/test_calc_spec.py", "3": "tests/test_calc_spec_3.py",
                 "4": "tests/test_calc_spec_4.py", "9": "tests/test_calc_spec.py"}.get(tag, "tests/test_calc.py")
    calls = [{"role": "test_author", "write_files": {spec_file: SPEC_TEST + "# ticket %s\\n" % tag}, "output": DONE},
             {"role": "implementer", "label": "unit-u1", "write_files": {"src/calc.py": CALC + "# ticket %s\\n" % tag}, "output": DONE},
             {"role": "implementer", "label": "unit-u2", "write_files": {"src/cli.py": CLI + "# ticket %s\\n" % tag}, "output": DONE}]
    return calls


def review_finding_calls(tag="1"):
    """Round one: Security Sage reports one high finding in src/calc.py; the fixer and its verifier follow."""
    finding = {"id": "m", "lens": "Security Sage", "file": "src/calc.py", "line": 2, "severity": "high", "title": "No docstring",
               "detail": "add has no docstring", "suggestion": "Add a docstring to add"}
    return [{"role": "reviewer", "label": "Security Sage", "output": {"findings": [finding]}},
            {"role": "fixer", "write_files": {"src/calc.py": 'def add(a, b):\n    """Add two numbers."""\n    return a + b\n# ticket %s\n' % tag},
             "output": DONE},
            {"role": "fix_verify", "output": {"results": [
                {"finding_id": "r0-1", "addressed": True, "evidence": "docstring present"}]}}]


def q(qid, kind="architecture", rec="a", **extra):
    doc = {"id": qid, "question": "Question %s?" % qid, "context": "While planning src/calc.py the step hit %s." % qid,
           "options": [{"id": "a", "label": "Option A", "consequence": "A changes the module"},
                       {"id": "b", "label": "Option B", "consequence": "B adds a file"}],
           "recommended": rec, "kind": kind, "reversible": True, "themes": ["reuse"]}
    doc.update(extra)
    return doc


def origin_file(origin, rel):
    return subprocess.run(["git", "--git-dir", str(origin), "show", "main:" + rel], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True).stdout


h = Harness("Flow v3 integration")
t0 = time.time()

# ---- Scenario A: the happy path -------------------------------------------------------------------
proj, origin, REPO = make_sandbox("proj", issues=range(1, 6))
rc, out, err = call(["init", "--repo-root", str(proj), "--force", "--set", "provider=local",
                     "--set", "check.full=python3 check.py"])
h.test_result("A: init through the CLI", rc == 0, out + err)
rc, out, err = call(["doctor", "--repo-root", str(proj)])
h.test_result("A: doctor passes", rc == 0 and "passed" in out, out + err)

script(plan_calls() + work_calls() + review_finding_calls())
rc, out, err = call(["run", "1", "--repo-root", str(proj)])
fl = flow_of(REPO, 1)
h.test_result("A: run reaches the merge gate", rc == 0 and fl is not None and status_of(REPO, 1) == "merge_gate",
              out[-1500:] + err[-800:])

rc, out, err = call(["gate", "1", "--repo", REPO])
h.test_result("A: gate screen renders", rc == 0 and "MERGE GATE" in out, out + err)
rc, out, err = call(["status", "--repo", REPO])
h.test_result("A: status shows the flow at the gate", rc == 0 and "merge_gate" in out, out)
rc, out, err = call(["inbox", "--repo", REPO])
h.test_result("A: inbox lists the merge gate", rc == 0 and "MERGE GATES (1)" in out, out)
roles = [r["role"] for r in invocations()]
h.test_result("A: the stages ran in order with a fix round between two reviews",
              roles.index("test_author") < roles.index("implementer") < roles.index("reviewer")
              < roles.index("fixer") < len(roles) - 1 - roles[::-1].index("reviewer") < roles.index("pr_text")
              and "fix_verify" in roles, str(roles))
rc, out, err = call(["merge", "1", "--repo", REPO, "--merge", "--wait"])
prov = provider.LocalProvider(origin, REPO)
fl = flow_of(REPO, 1)
pr = fl.get("pr")
h.test_result("A: merge --merge finishes the flow, status done", rc == 0 and status_of(REPO, 1) == "done", out + err)
h.test_result("A: the PR is merged in the provider", bool(pr) and prov.pr_state(int(pr["number"] if isinstance(pr, dict) else pr)) == "merged", str(pr))
h.test_result("A: origin main has the change", "return a + b" in origin_file(origin, "src/calc.py")
              and "def run" in origin_file(origin, "src/cli.py") and "Add two numbers" in origin_file(origin, "src/calc.py")
              and "def test" not in origin_file(origin, "tests/test_calc.py") and "assert" in origin_file(origin, "tests/test_calc.py"),
              origin_file(origin, "src/calc.py"))
h.test_result("A: step records and events exist",
              len(list((fl.path / "steps").glob("*.md"))) >= 6 and (fl.path / "events.jsonl").stat().st_size > 0,
              str(sorted(p.name for p in (fl.path / "steps").glob("*.md"))))

# ---- Scenario B: questions (fact, principle, assumed, human) -----------------------------------------
qs = [q("f1", "fact"), q("p1", "architecture"), q("d1", "detail"),
      q("r1", "risk", risk="b rewrites a shared module", rec="a")]
extra = [
    {"role": "fact_scout", "label": "f1", "output": {"answers": [
        {"question_id": "f1", "found": True, "option_id": "b", "answer": "B", "evidence": "README.md line 1"}]}},
    {"role": "resolver", "label": "p1", "output": {"results": [
        {"question_id": "p1", "outcome": "decides", "principle_id": "P-x", "option_id": "a", "reason": "reuse the helper"}]}},
    {"role": "resolver", "label": "d1", "output": {"results": [
        {"question_id": "d1", "outcome": "none", "principle_id": "", "option_id": "", "reason": "no principle bears on it"}]}},
    {"role": "voice", "output": {
        "answers": [{"question_id": "r1", "option_id": "b", "answer": "B", "reason_quote": "take B", "confidence": "high"}],
        "directions": [{"text": "Keep the diff small", "quote": "keep the diff small"}], "unplaced": []}},
]
script(plan_calls(questions=qs, extra=extra) + work_calls("2") + review_finding_calls("2"))
rc, out, err = call(["run", "2", "--repo-root", str(proj)])
h.test_result("B: the risk question parks the flow (exit 0)", rc == 0 and status_of(REPO, 2) == "waiting_human",
              out[-800:] + err[-800:])
rc, out, err = call(["inbox", "--repo", REPO])
h.test_result("B: inbox shows the risk card and only the risk card",
              rc == 0 and "r1" in out and "b rewrites a shared module" in out and "[f1]" not in out
              and "[p1]" not in out and "[d1]" not in out, out[-1500:])
rc, out, err = call(["answer", "2", "--repo", REPO, "--wait"], "I would take B, and keep the diff small.\n")
h.test_result("B: a free-text answer resumes the flow to the merge gate",
              rc == 0 and status_of(REPO, 2) == "merge_gate", out[-800:] + err[-800:])
dirs = [d.get("text") for d in flow_of(REPO, 2).get("directions") or []]
h.test_result("B: the direction was saved", "Keep the diff small" in dirs, str(dirs))
later = [r["prompt"] for r in invocations() if r["role"] in ("implementer", "reviewer")]
h.test_result("B: the direction reached the implement and review prompts",
              later and all("Keep the diff small" in p for p in later), "%d prompts" % len(later))
why = {}
for qid in ("r1", "p1", "d1", "f1"):
    rc, out, err = call(["why", "2", qid, "--repo", REPO])
    why[qid] = out
h.test_result("B: why explains a human decision", "Decided by: human" in why["r1"] and "take B" in why["r1"], why["r1"])
h.test_result("B: why explains a principle decision", "Decided by: principle (P-x)" in why["p1"], why["p1"])
h.test_result("B: why prints the question that was decided",
              all(len(w.split("Question:", 1)[-1].split("\n", 1)[0].strip()) > 3 for w in why.values()), why["p1"])
h.test_result("B: why explains an assumed decision", "Decided by: assumed" in why["d1"], why["d1"])
h.test_result("B: why explains a fact decision", "Decided by: fact" in why["f1"], why["f1"])
rc, out, err = call(["merge", "2", "--repo", REPO, "--merge", "--wait"])
h.test_result("B: the flow merges", rc == 0 and status_of(REPO, 2) == "done", out + err)

# ---- Scenario C: healing and stops -------------------------------------------------------------------
BAD_CLI = "from src.calc import add\n\n\ndef run(a, b):\n    return add(a, b) + 1\n"
calls = plan_calls() + work_calls("3")
calls[-2 - 0:] = calls[-2:]  # keep the plan calls as they are
for c in calls:
    if c.get("label") == "unit-u2":
        c["write_files"] = {"src/cli.py": BAD_CLI + "# ticket 3\n"}
calls.append({"role": "fixer", "label": "fix-r1", "write_files": {"src/cli.py": CLI + "# ticket 3\n"}, "output": DONE})
script(calls + review_finding_calls("3")[1:])
rc, out, err = call(["run", "3", "--repo-root", str(proj)])
fl = flow_of(REPO, 3)
ev = [e for e in fl.events() if e.get("kind") == "heal"]
h.test_result("C: a red fast check after the last unit is healed by the fixer and the flow reaches the gate",
              rc == 0 and status_of(REPO, 3) == "merge_gate"
              and any(r["label"] == "fix-r1" for r in invocations("fixer")) and any("fixer round 1" in str(e) for e in ev),
              out[-600:] + err[-600:] + str(ev)[:600])
rc, out, err = call(["merge", "3", "--repo", REPO, "--stop", "done with this one", "--wait"])
h.test_result("C: a human stop at the gate exits 2", rc == 2 and status_of(REPO, 3) == "stopped", out + err)

red, red_origin, RED_REPO = make_sandbox("redproj", yaml_extra="  base: true\n")
rc, out, err = call(["doctor", "--repo-root", str(red)])
h.test_result("C: doctor passes on a green base", rc == 0, out + err)
# the base goes red after doctor passed (someone merged a broken change)
(red / "tests" / "test_old.py").write_text("assert 1 == 2\n")
for g in (["add", "-A"], ["commit", "-qm", "break the base"], ["push", "-q", "origin", "HEAD:main"]):
    subprocess.run(["git", "-C", str(red)] + g, check=True, capture_output=True)
script(plan_calls() + work_calls("1"))
rc, out, err = call(["run", "1", "--repo-root", str(red)])
rec = flow_of(RED_REPO, 1)
stop = json.loads((rec.path / "stop.json").read_text()) if (rec and (rec.path / "stop.json").exists()) else {}
h.test_result("C: a red base stops with class base-red and exit 2",
              rc == 2 and stop.get("cls") == "base-red" and status_of(RED_REPO, 1) == "stopped", out[-800:] + err + str(stop))
rc, out, err = call(["stop-screen", "1", "--repo", RED_REPO])
h.test_result("C: stop-screen explains the base-red stop", rc == 0 and "base-red" in out and "Resume:" in out, out)
h.test_result("C: no model call ran for a red base", invocations() == [], str(invocations())[:300])

# ---- Scenario D: send back from the merge gate -------------------------------------------------------
script(plan_calls() + work_calls("4") + review_finding_calls("4"))
rc, out, err = call(["run", "4", "--repo-root", str(proj)])
h.test_result("D: run reaches the gate", rc == 0 and status_of(REPO, 4) == "merge_gate", out[-600:] + err[-600:])
before = len(invocations())
SB_CALC = 'def add(a, b):\n    """Add two numbers."""\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n# ticket 4\n'
script([{"role": "fixer", "write_files": {"src/calc.py": SB_CALC}, "output": DONE}],
       default={})
rc, out, err = call(["merge", "4", "--repo", REPO, "--send-back", "also add a subtract function", "--wait"])
roles_after = [r["role"] for r in invocations()]
fl = flow_of(REPO, 4)
h.test_result("D: send-back runs fix, review and ship again and returns to the gate",
              rc == 0 and status_of(REPO, 4) == "merge_gate" and "fixer" in roles_after and "reviewer" in roles_after
              and "pr_text" not in roles_after[:0] and (fl.data("sendback") or {}).get("handled") is True,
              out[-800:] + err[-800:] + str(roles_after))
rc, out, err = call(["merge", "4", "--repo", REPO, "--merge", "--wait"])
h.test_result("D: merging after the send-back lands the fix on origin",
              rc == 0 and status_of(REPO, 4) == "done" and "def sub" in origin_file(origin, "src/calc.py"), out + err)

# ---- Scenario E: merge.method command ------------------------------------------------------------------
MERGE_CMD = ("merge:\n  method: command\n  command: 'git push -q origin {head}:{base} && "
             "sed -i.bak \"s/\\\"state\\\": \\\"open\\\"/\\\"state\\\": \\\"merged\\\"/\" "
             "\"$(git remote get-url origin)/flow3/prs/{pr}.json\"'\n")
cproj, corigin, CREPO = make_sandbox("cmdproj", issues=(7,), yaml_extra=MERGE_CMD)
rc, out, err = call(["doctor", "--repo-root", str(cproj)])
h.test_result("E: doctor passes with a merge command", rc == 0, out + err)
script(plan_calls() + work_calls("5") + review_finding_calls("5"))
rc, out, err = call(["run", "7", "--repo-root", str(cproj)])
h.test_result("E: run reaches the gate", rc == 0 and status_of(CREPO, 7) == "merge_gate", out[-600:] + err[-600:])
rc, out, err = call(["merge", "7", "--repo", CREPO, "--merge", "--wait"])
cfl = flow_of(CREPO, 7)
cprov = provider.LocalProvider(corigin, CREPO)
cpr = cfl.get("pr")
h.test_result("E: the repo's own merge command lands the change and the provider reports merged",
              rc == 0 and status_of(CREPO, 7) == "done" and "return a + b" in origin_file(corigin, "src/calc.py")
              and bool(cpr) and cprov.pr_state(int(cpr["number"] if isinstance(cpr, dict) else cpr)) == "merged",
              out[-800:] + err[-800:])

# ---- Scenario F: a fleet of two tickets, the second depending on the first -----------------------------
fproj, forigin, FREPO = make_sandbox("fleetproj", issues=(8, 9), dep_of={9: 8})
rc, out, err = call(["doctor", "--repo-root", str(fproj)])
h.test_result("F: doctor passes", rc == 0, out + err)


def wait_for(cond, secs=60.0, give_up=None):
    """Poll cond until it holds. give_up, if given, ends the wait early (a flow that has stopped will never get there)."""
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        if give_up is not None and give_up():
            return False
        time.sleep(0.3)
    return False


def has_stopped(repo, issue):
    return status_of(repo, issue) in ("stopped", "failed")


# A whole ticket run (worktree, three model calls, checks) shares the machine with whatever else is running,
# so the gate waits get a generous budget; a flow that really stopped still fails at once, naming its cause.
GATE_WAIT_SECS = 240.0


script(plan_calls() + work_calls("8") + review_finding_calls("8") + plan_calls() + work_calls("9") + review_finding_calls("9"))
rc, out, err = call(["fleet", "start", "8>9", "--max", "2", "--repo-root", str(fproj)])
h.test_result("F: fleet start launches a detached supervisor", rc == 0 and "fleet" in out.lower(), out + err)
fid = [w for w in out.replace(",", " ").split() if w[:2] == "20" and "-" in w]
fid = fid[0] if fid else ""
FLEET_IDS.append(fid)
h.test_result("F: ticket 8 reaches its merge gate while 9 waits",
              wait_for(lambda: status_of(FREPO, 8) == "merge_gate", GATE_WAIT_SECS,
                       give_up=lambda: has_stopped(FREPO, 8)) and flow_of(FREPO, 9) is None,
              describe(FREPO, 8) + " | " + out)
rc, out, err = call(["fleet", "status", fid] if fid else ["fleet", "status"])
h.test_result("F: fleet status shows 9 waiting behind 8", rc == 0 and "waiting" in out and "#8" in out, out + err)
rc, out, err = call(["merge", "8", "--repo", FREPO, "--merge", "--wait"])
h.test_result("F: merging 8 completes it", rc == 0 and status_of(FREPO, 8) == "done", out + err)
h.test_result("F: the supervisor then starts 9, which reaches its gate",
              wait_for(lambda: status_of(FREPO, 9) == "merge_gate", GATE_WAIT_SECS,
                       give_up=lambda: has_stopped(FREPO, 9)), describe(FREPO, 9))
rc, out, err = call(["merge", "9", "--repo", FREPO, "--merge", "--wait"])
h.test_result("F: both tickets merged into origin", rc == 0 and status_of(FREPO, 9) == "done"
              and "# ticket 9" in origin_file(forigin, "src/calc.py"),
              " | ".join([describe(FREPO, 9), out, err, origin_file(forigin, "src/calc.py")]))
rc, out, err = call(["fleet", "status", fid] if fid else ["fleet", "status"])
h.test_result("F: fleet status shows both done", rc == 0 and out.count("done") >= 2, out)
h.test_result("F: the supervisor exits once nothing is left to move",
              wait_for(lambda: subprocess.run(["pgrep", "-f", "fleet supervise " + fid], stdout=subprocess.PIPE).stdout == b"",
                       30) if fid else False, "supervisor still running")

h.summarize_and_exit()
