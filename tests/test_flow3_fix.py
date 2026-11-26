#!/usr/bin/env python3
"""
Tests for scripts/flow3/steps/fix.py: grouping, the verify and second-round loop, send-back notes,
re-entry, healing a red check, and the protected-path rule (PLAN.md section 7.6). Fake claude binary only.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="flow3-test-")).resolve()
HOME = TMP / "home"
os.environ["FLOW3_HOME"] = str(HOME)
os.environ["HOME"] = str(TMP / "userhome")
os.environ["FLOW3_CLAUDE_BIN"] = str(ROOT / "tests" / "flow3_fake_claude.py")
os.environ.pop("FLOW3_LIMIT_CEILING_SECS", None)

from _test_harness import Harness  # noqa: E402

from scripts.flow3 import context, gitops, heal, runner  # noqa: E402
from scripts.flow3.state import FlowDir  # noqa: E402
from scripts.flow3.steps import fix, review  # noqa: E402
from scripts.flow3.types import FlowStop  # noqa: E402

heal._sleep = lambda s: None  # type: ignore[assignment]
runner._sleep = lambda s: None  # type: ignore[assignment]
_counter = {"n": 0}

CHECK_PY = '''
import sys
d = sys.argv[1]
with open(d + "/runs", "a") as fh:
    fh.write("x\\n")
lines = []
try:
    lines = [x for x in open(d + "/results").read().split("\\n") if x]
except OSError:
    pass
cur = lines.pop(0) if lines else "pass"
open(d + "/results", "w").write("\\n".join(lines) + "\\n")
if cur == "pass":
    sys.exit(0)
print("FAILED tests/test_a.py::test_one - assert 1 == 2")
sys.exit(1)
'''


def sh(args: List[str], cwd: Path) -> str:
    return subprocess.run(args, cwd=str(cwd), check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True).stdout.strip()


def finding(fid: str, file: str, bucket: str = "doing_it", rnd: int = 0) -> Dict[str, Any]:
    return {"id": fid, "lens": "Uncle Bob", "file": file, "line": 3, "severity": "high", "title": "Title " + fid,
            "detail": "Detail " + fid, "suggestion": "Suggest " + fid, "status": "confirmed", "bucket": bucket}


class Box:
    def __init__(self, name: str, findings: Optional[List[Dict[str, Any]]] = None, notes: str = "") -> None:
        self.ctl = TMP / (name + "-ctl")
        self.ctl.mkdir()
        (self.ctl / "check.py").write_text(CHECK_PY)
        check = "python3 %s/check.py %s" % (self.ctl, self.ctl)
        yml = ["provider: local", "check:", "  fast: '%s'" % check, "  full: '%s'" % check, "  timeout_secs: 60",
               "budget:", "  soft_usd: 15.0", "  hard_usd: 40.0", "protected_paths: [docs/]"]
        origin = TMP / (name + "-origin.git")
        self.clone = TMP / name
        sh(["git", "init", "--bare", "-q", "-b", "main", str(origin)], TMP)
        sh(["git", "clone", "-q", str(origin), str(self.clone)], TMP)
        sh(["git", "config", "user.name", "t"], self.clone)
        sh(["git", "config", "user.email", "t@example.invalid"], self.clone)
        files = {".claude/flow.yaml": "\n".join(yml) + "\n", "README.md": "hi\n", "docs/spec.md": "spec\n"}
        for rel, text in files.items():
            p = self.clone / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        sh(["git", "add", "-A"], self.clone)
        sh(["git", "commit", "-q", "-m", "init"], self.clone)
        sh(["git", "push", "-q", "origin", "main"], self.clone)
        sh(["git", "remote", "set-head", "origin", "main"], self.clone)
        self.flow = FlowDir.create(name, 1, "Ticket " + name, self.clone)
        self.flow.set("worktree", str(self.clone))
        self.flow.set("base", "main")
        base = gitops.head_sha(self.clone)
        self.flow.set_data("worktree", {"base_sha": base})
        fl = findings or []
        triage = {"doing_it": [{"finding_id": f["id"], "fix": "Do " + f["id"], "files": [f["file"]] if f["file"] else []}
                               for f in fl if f["bucket"] == "doing_it"],
                  "needs_you": [], "needs_measurement": [], "deferred": [], "gut_check": ""}
        self.flow.set_data("review", {"rounds": [{"round": 0, "from": base, "to": base, "findings": fl,
                                                  "triage": triage, "measurements": []}], "phase": "done"})
        if notes:
            self.flow.set_data("sendback", {"notes": notes, "handled": False})
        self.ctx = context.build(self.flow)
        self._log = self.ctl / "log-none.jsonl"

    def results(self, *lines: str) -> None:
        (self.ctl / "results").write_text("\n".join(lines) + "\n")

    def runs(self) -> int:
        p = self.ctl / "runs"
        return len(p.read_text().split()) if p.exists() else 0

    def script(self, calls: List[Dict[str, Any]]) -> None:
        _counter["n"] += 1
        path = self.ctl / ("script%d.json" % _counter["n"])
        path.write_text(json.dumps({"calls": calls}))
        os.environ["FLOW3_FAKE_SCRIPT"] = str(path)
        self._log = self.ctl / ("log%d.jsonl" % _counter["n"])
        os.environ["FLOW3_FAKE_LOG"] = str(self._log)

    def launches(self) -> List[Dict[str, Any]]:
        if not self._log.exists():
            return []
        return [json.loads(x) for x in self._log.read_text().splitlines() if x.strip()]

    def labels(self) -> List[str]:
        return [str(x.get("label")) for x in self.launches()]

    def subjects(self) -> List[str]:
        return sh(["git", "log", "--format=%s"], self.clone).splitlines()

    def rounds(self) -> List[Dict[str, Any]]:
        return list(self.flow.data("fix").get("rounds") or [])


DONE = {"status": "done", "summary": "did it"}


def w(label: str, rel: str, text: str = "x = 1\n", role: str = "fixer") -> Dict[str, Any]:
    return {"role": role, "label": label, "write_files": {rel: text}, "output": DONE}


def verdicts(*pairs: Any) -> Dict[str, Any]:
    return {"role": "fix_verify", "output": {"results": [
        {"finding_id": fid, "addressed": ok, "evidence": "ev-" + fid} for fid, ok in pairs]}}


h = Harness("flow3 fix tests")

# -- grouping ---------------------------------------------------------------------------------
items = [{"id": "f1", "files": ["a.py"]}, {"id": "f2", "files": ["b.py"]}, {"id": "f3", "files": ["a.py", "c.py"]},
         {"id": "f4", "files": []}, {"id": "f5", "files": ["c.py"]}, {"id": "f6", "files": []}]
units = fix.group_units(items, "u")
h.test_result("findings sharing a file (transitively) form one unit; each file-less finding is its own",
              [(u["ids"], u["files"]) for u in units] == [
                  (["f1", "f3", "f5"], ["a.py", "c.py"]), (["f2"], ["b.py"]), (["f4"], []), (["f6"], [])],
              str(units))
bridge = fix.group_units([{"id": "x", "files": ["a"]}, {"id": "y", "files": ["b"]}, {"id": "z", "files": ["a", "b"]}], "u")
h.test_result("a finding that bridges two groups merges them", len(bridge) == 1 and sorted(bridge[0]["ids"]) == ["x", "y", "z"],
              str(bridge))

# -- a happy round ----------------------------------------------------------------------------
b = Box("happy", [finding("r0-1", "src/a.py"), finding("r0-2", "src/b.py"), finding("r0-3", "src/c.py", "deferred"),
                  finding("r0-4", "src/d.py", "needs_you")])
b.script([w("unit-a1-1", "src/a.py"), w("unit-a1-2", "src/b.py"), verdicts(("r0-1", True), ("r0-2", True))])
res = fix.run(b.ctx)
h.test_result("a round fixes each doing_it finding in its own unit, then verifies once",
              res.kind == "done" and b.labels() == ["unit-a1-1", "unit-a1-2", "fix-verify-1"], str(b.labels()))
r = b.rounds()
h.test_result("data.fix records review_round, fixed ids, no open ids, and the head",
              len(r) == 1 and r[0]["review_round"] == 0 and r[0]["fixed"] == ["r0-1", "r0-2"] and r[0]["open"] == []
              and r[0]["head"] == gitops.head_sha(b.clone), str(r))
h.test_result("each unit made a plain commit", len([s for s in b.subjects() if s.startswith("flow3 #1 fix a1-")]) == 2,
              str(b.subjects()))
h.test_result("review.open_findings no longer lists the fixed findings", review.open_findings(b.flow) == [])
h.test_result("working state is cleared and a record exists", b.flow.data("fix_progress") == {}
              and any("fix" in p.name for p in b.flow.path.rglob("*")))
vp = [x for x in b.launches() if x["label"] == "fix-verify-1"][0]["prompt"]
h.test_result("the verify prompt carries both findings and the diff of the fix",
              "r0-1" in vp and "r0-2" in vp and "src/a.py" in vp and "r0-3" not in vp)
up = b.launches()[0]["prompt"]
h.test_result("a unit prompt holds its finding text and the protected path",
              "Detail r0-1" in up and "Do r0-1" in up and "docs/" in up and "Detail r0-2" not in up)

# -- a second round ---------------------------------------------------------------------------
b = Box("second", [finding("r0-1", "src/a.py"), finding("r0-2", "src/b.py")])
b.script([w("unit-a1-1", "src/a.py"), w("unit-a1-2", "src/b.py"), verdicts(("r0-1", True), ("r0-2", False)),
          w("unit-a2-1", "src/b.py", "x = 2\n"), verdicts(("r0-2", True))])
res = fix.run(b.ctx)
h.test_result("an unaddressed finding gets one more round and a second verify",
              res.kind == "done" and b.labels() == ["unit-a1-1", "unit-a1-2", "fix-verify-1", "unit-a2-1", "fix-verify-2"],
              str(b.labels()))
r = b.rounds()
h.test_result("both end up fixed and nothing is open", r[0]["fixed"] == ["r0-1", "r0-2"] and r[0]["open"] == []
              and review.open_findings(b.flow) == [], str(r))
p2 = [x for x in b.launches() if x["label"] == "unit-a2-1"][0]["prompt"]
h.test_result("the second-round prompt covers only the missed finding and says why it was missed",
              "Detail r0-2" in p2 and "Detail r0-1" not in p2 and "ev-r0-2" in p2)

# -- still unaddressed ------------------------------------------------------------------------
b = Box("open", [finding("r0-1", "src/a.py"), finding("r0-2", "src/b.py")])
b.script([w("unit-a1-1", "src/a.py"), w("unit-a1-2", "src/b.py"), verdicts(("r0-1", True), ("r0-2", False)),
          w("unit-a2-1", "src/b.py", "x = 2\n"), verdicts(("r0-2", False))])
res = fix.run(b.ctx)
r = b.rounds()
h.test_result("after two rounds a still-unaddressed finding is recorded open, with no third round",
              res.kind == "done" and r[0]["fixed"] == ["r0-1"] and r[0]["open"] == ["r0-2"]
              and b.labels().count("fix-verify-2") == 1 and len(b.launches()) == 5, str(r))
op = review.open_findings(b.flow)
h.test_result("review.open_findings reports it as not fixed",
              [(f["id"], f["reason"]) for f in op] == [("r0-2", "not fixed")], str(op))

# -- a fixer that cannot finish leaves its findings open; it does not stop the flow ------------
PART = {"status": "partial", "summary": "did the part I may", "remaining": ["the test file is protected"]}
b = Box("unfinished", [finding("r0-1", "src/a.py"), finding("r0-2", "src/a.py")])
part = {"role": "fixer", "write_files": {"src/a.py": "x = 1\n"}, "output": PART}
b.script([part, part, verdicts(("r0-1", True), ("r0-2", False)), part, part, verdicts(("r0-2", False))])
try:
    res = fix.run(b.ctx)
    stopped: Optional[FlowStop] = None
except FlowStop as exc:
    stopped = exc
r = b.rounds()
h.test_result("an unfinished fixer is not a stop: the verifier rules and the rest stays open",
              stopped is None and res.kind == "done" and r[0]["fixed"] == ["r0-1"] and r[0]["open"] == ["r0-2"],
              "%s %s" % (stopped, r))
h.test_result("its partial work is kept", (b.clone / "src" / "a.py").exists()
              and any("(partial)" in x for x in b.subjects()), str(b.subjects()))

# -- send-back notes --------------------------------------------------------------------------
b = Box("sendback", [], notes="Please rename the helper in src/a.py to something clearer.")
b.script([w("unit-sendback", "src/a.py")])
res = fix.run(b.ctx)
h.test_result("send-back notes become one unit, with no verify call (there is no finding to rule on)",
              res.kind == "done" and b.labels() == ["unit-sendback"], str(b.labels()))
h.test_result("the notes are marked handled once the unit is committed",
              b.flow.data("sendback") == {"notes": "Please rename the helper in src/a.py to something clearer.",
                                          "handled": True} and any("fix sendback" in s for s in b.subjects()))
sp = b.launches()[0]["prompt"]
h.test_result("the prompt shows the notes as data and allows any file but protected ones",
              "rename the helper" in sp and "Any file except the protected paths" in sp)
h.test_result("the round entry is empty of finding ids", b.rounds()[0]["fixed"] == [] and b.rounds()[0]["open"] == [])

b = Box("sendback-mixed", [finding("r0-1", "src/a.py")], notes="Also tidy the README.")
b.script([w("unit-a1-1", "src/a.py"), w("unit-sendback", "README.md"), verdicts(("r0-1", True))])
fix.run(b.ctx)
h.test_result("notes and findings run together: finding units, then the notes unit, then one verify",
              b.labels() == ["unit-a1-1", "unit-sendback", "fix-verify-1"] and b.flow.data("sendback")["handled"] is True,
              str(b.labels()))

# -- nothing to fix ---------------------------------------------------------------------------
b = Box("nothing", [finding("r0-1", "src/c.py", "deferred")])
b.script([])
res = fix.run(b.ctx)
h.test_result("nothing to fix returns done at once and records an empty round",
              res.kind == "done" and b.launches() == [] and b.rounds()[0]["fixed"] == [] and b.rounds()[0]["open"] == []
              and b.rounds()[0]["review_round"] == 0, str(b.rounds()))

b = Box("already", [finding("r0-1", "src/a.py")])
b.flow.set_data("fix", {"rounds": [{"review_round": 0, "fixed": ["r0-1"], "open": [], "head": "x"}]})
b.script([])
fix.run(b.ctx)
h.test_result("a finding an earlier fix round fixed is not fixed again", b.launches() == [] and len(b.rounds()) == 2)

# -- protected path, then re-entry ------------------------------------------------------------
b = Box("protected", [finding("r0-1", "src/a.py"), finding("r0-2", "src/b.py")])
b.script([w("unit-a1-1", "src/a.py"),
          {"role": "fixer", "label": "unit-a1-2", "write_files": {"docs/spec.md": "changed\n"}, "output": DONE},
          {"role": "fixer", "label": "unit-a1-2", "write_files": {"docs/spec.md": "changed again\n"}, "output": DONE}])
stop = None
try:
    fix.run(b.ctx)
except FlowStop as exc:
    stop = exc
h.test_result("writing a protected path twice is refused as a protected stop with a reason",
              stop is not None and stop.cls == "protected" and stop.reason.strip() != ""
              and (b.clone / "docs/spec.md").read_text() == "spec\n", str(stop and (stop.cls, stop.reason)))
h.test_result("the first unit is remembered as done", b.flow.data("fix_progress")["units"]["a1-1"].get("done") is True)
b.script([w("unit-a1-2", "src/b.py"), verdicts(("r0-1", True), ("r0-2", True))])
res = fix.run(b.ctx)
h.test_result("re-entry does not redo the finished unit",
              res.kind == "done" and b.labels() == ["unit-a1-2", "fix-verify-1"] and b.rounds()[0]["fixed"] == ["r0-1", "r0-2"],
              str(b.labels()))

# -- a red check the fixer heals --------------------------------------------------------------
b = Box("heal", [finding("r0-1", "src/a.py")])
b.results("fail", "fail", "pass")
b.script([w("unit-a1-1", "src/a.py"), w("fix-r1", "src/a.py", "x = 3\n"), verdicts(("r0-1", True))])
res = fix.run(b.ctx)
h.test_result("a red check after a unit is healed by a fixer round, then the verify runs",
              res.kind == "done" and b.labels() == ["unit-a1-1", "fix-r1", "fix-verify-1"]
              and b.rounds()[0]["fixed"] == ["r0-1"], str(b.labels()))

# -- a fixer's question parks, then resumes ---------------------------------------------------
b = Box("ask", [finding("r0-1", "src/a.py")])
q = {"id": "q1", "question": "Which name?", "context": "Naming a helper.", "kind": "risk", "reversible": True,
     "risk": "Wrong name renames a public helper.", "recommended": "a",
     "options": [{"id": "a", "label": "A", "consequence": "Uses A."}, {"id": "b", "label": "B", "consequence": "Uses B."}]}
b.script([{"role": "fixer", "label": "unit-a1-1", "output": {"status": "needs_answers", "summary": "asked",
                                                              "questions": [q]}}])
res = fix.run(b.ctx)
h.test_result("a fixer's question parks the stage", res.kind == "needs_human" and b.rounds() == [], str(res))
from scripts.flow3 import questions  # noqa: E402
questions.apply_reply(b.ctx, "q1: a")
b.script([w("unit-a1-1-answers", "src/a.py"), verdicts(("r0-1", True))])
res = fix.run(b.ctx)
h.test_result("after the answer the worker resumes and the round completes",
              res.kind == "done" and b.rounds()[0]["fixed"] == ["r0-1"], str(b.labels()))

# -- a finding in a protected file is left open, never sent to a fixer ------------------------
def allowed_of(prompt: str) -> str:
    return prompt.split("## Files you may change (and no others)")[1].split("## Protected paths")[0]


b = Box("prot-only", [finding("r0-1", "tests/spec_test.py")])
b.flow.set("protected", ["tests/spec_test.py"])
b.script([])
res = fix.run(b.ctx)
h.test_result("a finding only in a protected file gets no fixer call and is listed open",
              res.kind == "done" and b.launches() == [] and b.rounds()[0]["fixed"] == []
              and b.rounds()[0]["open"] == ["r0-1"], str(b.rounds()))
h.test_result("review.open_findings still reports it", [f["id"] for f in review.open_findings(b.flow)] == ["r0-1"])
h.test_result("the step record says it was left open because the file is protected",
              any("protected" in x.read_text() for x in b.flow.path.rglob("*") if x.is_file() and "fix" in x.name),
              str([x.name for x in b.flow.path.rglob("*")]))

b = Box("prot-config", [finding("r0-1", "docs/spec.md")])
b.script([])
fix.run(b.ctx)
h.test_result("a path from protected_paths in flow.yaml blocks the same way",
              b.launches() == [] and b.rounds()[0]["open"] == ["r0-1"], str(b.rounds()))

b = Box("prot-plus-normal", [finding("r0-1", "tests/spec_test.py"), finding("r0-2", "src/b.py")])
b.flow.set("protected", ["tests/spec_test.py"])
b.script([w("unit-a1-1", "src/b.py"), verdicts(("r0-2", True))])
res = fix.run(b.ctx)
h.test_result("a protected-only finding beside a normal one: the normal one is fixed, the protected one is open",
              res.kind == "done" and b.labels() == ["unit-a1-1", "fix-verify-1"] and b.rounds()[0]["fixed"] == ["r0-2"]
              and b.rounds()[0]["open"] == ["r0-1"], str((b.labels(), b.rounds())))
vp = [x for x in b.launches() if x["label"] == "fix-verify-1"][0]["prompt"]
h.test_result("the verify call does not see the blocked finding", "r0-1" not in vp)

# After the last review round only the findings the human answered `fix` are fixed; a send-back takes all.
b = Box("capped", [finding("r0-1", "src/a.py"), finding("r0-2", "src/b.py")])
rd = b.flow.data("review")
rd["rounds"][0]["last_round"] = True
rd["rounds"][0]["answers"] = {"r0-2": {"option_id": "fix", "source": "human", "text": "fix it"},
                              "r0-1": {"option_id": "fix", "source": "assumed", "text": "fix"}}
b.flow.set_data("review", rd)
h.test_result("last round: only the human-answered fix is a candidate",
              [i["id"] for i in fix.candidates(b.ctx)[1]] == ["r0-2"], str(fix.candidates(b.ctx)))
b.flow.set_data("sendback", {"notes": "do them all", "handled": False})
h.test_result("last round with a send-back: every doing_it finding is a candidate",
              [i["id"] for i in fix.candidates(b.ctx)[1]] == ["r0-1", "r0-2"], str(fix.candidates(b.ctx)))

mixed = finding("r0-1", "src/a.py")
b = Box("prot-mixed", [mixed])
b.flow.set("protected", ["tests/spec_test.py"])
rd = b.flow.data("review")
rd["rounds"][0]["triage"]["doing_it"][0]["files"] = ["src/a.py", "tests/spec_test.py"]
b.flow.set_data("review", rd)
b.script([w("unit-a1-1", "src/a.py"), verdicts(("r0-1", True))])
res = fix.run(b.ctx)
al = allowed_of(b.launches()[0]["prompt"]) if b.launches() else ""
h.test_result("a mixed finding keeps only its unprotected file in the fixer's allowed list",
              res.kind == "done" and "src/a.py" in al and "tests/spec_test.py" not in al and b.rounds()[0]["open"] == [],
              al)
h.test_result("the protected file still appears under protected paths",
              "tests/spec_test.py" in b.launches()[0]["prompt"].split("## Protected paths")[1])

# -- one check per attempt, not one per fix unit (#571) ------------------------------------------
b = Box("onecheck", [finding("r0-1", "src/a.py"), finding("r0-2", "src/b.py"), finding("r0-3", "src/c.py")])
b.script([w("unit-a1-1", "src/a.py"), w("unit-a1-2", "src/b.py"), w("unit-a1-3", "src/c.py"),
          verdicts(("r0-1", True), ("r0-2", True), ("r0-3", True))])
res = fix.run(b.ctx)
h.test_result("three fix units, one check, run after the last of them",
              res.kind == "done" and b.runs() == 1 and b.rounds()[0]["open"] == [], "runs=%d" % b.runs())

b = Box("onecheck-red", [finding("r0-1", "src/a.py"), finding("r0-2", "src/b.py")])
b.results("fail", "fail", "pass")
b.script([w("unit-a1-1", "src/a.py"), w("unit-a1-2", "src/b.py"), w("fix-r1", "src/a.py", "x = 7\n"),
          verdicts(("r0-1", True), ("r0-2", True))])
res = fix.run(b.ctx)
fr = [x for x in b.launches() if x["label"] == "fix-r1"]
h.test_result("a red check after the fixes gets a fixer that may edit any of the fixed files",
              res.kind == "done" and b.runs() == 3 and len(fr) == 1
              and "src/a.py" in allowed_of(fr[0]["prompt"]) and "src/b.py" in allowed_of(fr[0]["prompt"]),
              "runs=%d %s" % (b.runs(), b.labels()))

# -- a fixer that cannot fix from here (#572) ----------------------------------------------------
b = Box("blocked", [finding("r0-1", "src/a.py"), finding("r0-2", "src/b.py")])
b.script([{"role": "fixer", "label": "unit-a1-1",
           "output": {"status": "blocked", "summary": "could not",
                      "blocked": {"reason": "The schema is generated; I cannot run the generator.",
                                  "needs": "shell", "what": "make schema"}}},
          w("unit-a1-2", "src/b.py"), verdicts(("r0-2", True))])
res = fix.run(b.ctx)
launched = [x for x in b.launches() if x["label"] == "unit-a1-1"]
log = (b.flow.path / "journal.md").read_text()
h.test_result("a blocked fixer is launched once: no retry on a stronger model",
              len(launched) == 1 and "unit-a1-2" in b.labels(), str(b.labels()))
h.test_result("the other fix still lands and the blocked finding stays open, with the fixer's reason kept",
              res.kind == "done" and "r0-1" in b.rounds()[-1]["open"] and "r0-2" not in b.rounds()[-1]["open"]
              and "make schema" in log and "I cannot run the generator." in log, str(b.rounds()))

# -- F9: check_attempt branches ------------------------------------------------------------------
# Test check_attempt branch 1: fixer succeeds, check passes
b1 = Box("check_attempt_pass", [finding("r0-1", "src/a.py")])
b1.results("fail", "pass")  # First run fails, fixer succeeds, check passes
b1.script([w("unit-a1-1", "src/a.py"), w("fix-r1", "src/a.py", "x = 1\n"),
           verdicts(("r0-1", True))])
res1 = fix.run(b1.ctx)
h.test_result("F9: check_attempt branch 1 - fixer fixes check and it passes",
              res1.kind == "done" and b1.runs() == 2 and b1.rounds()[0]["open"] == [],
              "runs=%d, rounds=%s" % (b1.runs(), b1.rounds()))

# Test check_attempt branch 2: fixer blocked (raises Blocked exception)
# When a fixer is blocked, the check is still attempted by the fix step
b2 = Box("check_attempt_blocked", [finding("r0-1", "src/a.py")])
b2.results("fail", "fail")  # Fixer blocked, check runs but fails
b2.script([w("unit-a1-1", "src/a.py"),
           {"role": "fixer", "label": "fix-r1",
            "output": {"status": "blocked", "summary": "cannot fix",
                       "blocked": {"reason": "Need a tool", "needs": "environment", "what": "npm"}}},
           verdicts(("r0-1", False)),  # Finding stays open after blocked fixer
           w("unit-a2-1", "src/a.py"),
           {"role": "fixer", "label": "fix-r1",
            "output": {"status": "blocked", "summary": "cannot fix",
                       "blocked": {"reason": "Need a tool", "needs": "environment", "what": "npm"}}},
           verdicts(("r0-1", False))])
res2 = fix.run(b2.ctx)
# When fixer is blocked, the check still runs but stays red, finding remains open
h.test_result("F9: check_attempt branch 2 - fixer blocked, check still attempted",
              res2.kind == "done" and "r0-1" in b2.rounds()[-1]["open"] and b2.runs() == 2,
              "runs=%d, open=%s" % (b2.runs(), b2.rounds()[-1].get("open", [])))

if __name__ == "__main__":
    h.summarize_and_exit()
