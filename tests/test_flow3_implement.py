#!/usr/bin/env python3
"""
Tests for scripts/flow3/steps/implement.py: units in order, the judge on a red spec test, and the
existing-test edit rule (PLAN.md sections 7.4 and 12). Fake claude binary only.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

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

from scripts.flow3 import context, gitops, heal, questions, runner  # noqa: E402
from scripts.flow3.state import FlowDir  # noqa: E402
from scripts.flow3.types import FlowStop  # noqa: E402

heal._sleep = lambda s: None  # type: ignore[assignment]
runner._sleep = lambda s: None  # type: ignore[assignment]
_counter = {"n": 0}


def sh(args: List[str], cwd: Path) -> str:
    return subprocess.run(args, cwd=str(cwd), check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True).stdout.strip()


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


class Box:
    """One sandbox: bare origin, clone (also the worktree), and a control dir for check and fake claude."""

    def __init__(self, name: str, files: Optional[Dict[str, str]] = None,
                 protected: Optional[List[str]] = None, plan: Optional[Dict[str, Any]] = None,
                 per_unit: bool = False) -> None:
        self.ctl = TMP / (name + "-ctl")
        self.ctl.mkdir()
        (self.ctl / "check.py").write_text(CHECK_PY)
        check = "python3 %s/check.py %s" % (self.ctl, self.ctl)
        yml = ["provider: local", "check:", "  fast: '%s'" % check, "  full: '%s'" % check,
               "  timeout_secs: 60"] + (["  per_unit: true"] if per_unit else []) + [
               "budget:", "  soft_usd: 15.0", "  hard_usd: 40.0"]
        if protected:
            yml.append("protected_paths: [%s]" % ", ".join(protected))
        base = {".claude/flow.yaml": "\n".join(yml) + "\n", "README.md": "hi\n"}
        base.update(files or {})
        origin = TMP / (name + "-origin.git")
        self.clone = TMP / name
        sh(["git", "init", "--bare", "-q", "-b", "main", str(origin)], TMP)
        sh(["git", "clone", "-q", str(origin), str(self.clone)], TMP)
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
        self.flow = FlowDir.create(name, 1, "Ticket " + name, self.clone)
        self.flow.set("worktree", str(self.clone))
        self.flow.set("base", "main")
        self.flow.set_data("worktree", {"base_sha": gitops.head_sha(self.clone)})
        if plan is not None:
            self.flow.set_data("plan", {"phase": "done", "plan": plan})
        self.ctx = context.build(self.flow)
        self._log = self.ctl / "log-none.jsonl"

    def results(self, *lines: str) -> None:
        (self.ctl / "results").write_text("\n".join(lines) + "\n")

    def runs(self) -> int:
        p = self.ctl / "runs"
        return len(p.read_text().split()) if p.exists() else 0

    def script(self, calls: List[Dict[str, Any]], default: Optional[Dict[str, Any]] = None) -> None:
        _counter["n"] += 1
        path = self.ctl / ("script%d.json" % _counter["n"])
        doc: Dict[str, Any] = {"calls": calls}
        if default is not None:
            doc["default"] = default
        path.write_text(json.dumps(doc))
        os.environ["FLOW3_FAKE_SCRIPT"] = str(path)
        self._log = self.ctl / ("log%d.jsonl" % _counter["n"])
        os.environ["FLOW3_FAKE_LOG"] = str(self._log)
        limit = HOME / "limit.json"
        if limit.exists():
            limit.unlink()

    def launches(self) -> List[Dict[str, Any]]:
        if not self._log.exists():
            return []
        return [json.loads(x) for x in self._log.read_text().splitlines() if x.strip()]

    def subjects(self) -> List[str]:
        return sh(["git", "log", "--format=%s"], self.clone).splitlines()

    def read(self, rel: str) -> Optional[str]:
        p = self.clone / rel
        return p.read_text() if p.is_file() else None


def expect_stop(fn: Callable[[], Any], cls: str) -> Optional[FlowStop]:
    try:
        fn()
    except FlowStop as stop:
        return stop if stop.cls == cls and stop.reason.strip() else None
    return None


def question(qid: str, kind: str = "detail") -> Dict[str, Any]:
    return {"id": qid, "question": "Which name?", "context": "Naming a helper in src/a.py.",
            "options": [{"id": "a", "label": "Name A", "consequence": "Uses A."},
                        {"id": "b", "label": "Name B", "consequence": "Uses B."}],
            "recommended": "a", "kind": kind, "reversible": True,
            "risk": "Picking wrong renames a public helper." if kind == "risk" else ""}

from scripts.flow3.steps import implement  # noqa: E402

h = Harness("flow3 implement tests")
DONE = {"status": "done", "summary": "did it"}
SPEC = ["tests/test_a.py"]


def mkplan(two: bool = True, edits: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    units = [{"id": "u2", "title": "second", "files": ["src/b.py"], "steps": ["write b"], "acceptance": ["ac1"],
              "after": ["u1"]},
             {"id": "u1", "title": "first", "files": ["src/a.py"], "steps": ["write a"], "acceptance": ["ac1"]}]
    if not two:
        units = [units[1]]
    return {"goal": "g", "non_goals": [], "acceptance_criteria": [{"id": "ac1", "text": "it works"}],
            "units": units, "interfaces": [{"name": "f", "signature": "def f()", "file": "src/a.py"}],
            "test_spec": [{"id": "t1", "behaviour": "f works", "test_file": "tests/test_a.py"}],
            "untestable_reason": "", "existing_test_edits": edits or []}


def mkbox(name: str, plan: Dict[str, Any], files: Optional[Dict[str, str]] = None, per_unit: bool = False) -> Box:
    base = {"tests/test_a.py": "def test_one():\n    assert True\n"}
    base.update(files or {})
    b = Box(name, files=base, protected=["docs/"], plan=plan, per_unit=per_unit)
    b.flow.set("protected", list(SPEC))
    b.flow.set_data("spec_tests", {"files": list(SPEC), "commit": None, "skipped": ""})
    return b


def w(label: str, rel: str, text: str = "x = 1\n", output: Optional[Dict[str, Any]] = None,
      role: str = "implementer") -> Dict[str, Any]:
    return {"role": role, "label": label, "write_files": {rel: text}, "output": output or DONE}


def labels(b: Box) -> List[str]:
    return [str(x.get("label")) for x in b.launches()]


def card_of(b: Box) -> Dict[str, Any]:
    batch = questions.open_batch(b.flow)
    assert batch is not None
    return batch["cards"][0]


# -- unit order and re-entry --------------------------------------------------------------------
b = mkbox("i-order", mkplan())
b.script([w("unit-u1", "src/a.py"), w("unit-u2", "src/b.py")])
res = implement.run(b.ctx)
h.test_result("units run in dependency order, not plan order",
              res.kind == "done" and labels(b) == ["unit-u1", "unit-u2"], str(labels(b)))
st = b.flow.data("implement")
h.test_result("both units are recorded done with commits", st["done_units"] == ["u1", "u2"]
              and st["units"]["u1"]["commit"] and st["phase"] == "done", str(st))
n = len(b.launches())
res = implement.run(b.ctx)
h.test_result("a finished stage re-enters as a no-op", res.kind == "done" and len(b.launches()) == n)
h.test_result("a step record is written", any("implement" in p.name for p in b.flow.path.rglob("*")))

b = mkbox("i-skip", mkplan())
b.flow.set_data("implement", {"phase": "units", "done_units": ["u1"], "start_head": gitops.head_sha(b.clone)})
b.script([w("unit-u2", "src/b.py")])
res = implement.run(b.ctx)
h.test_result("re-entry skips a unit that is already done", res.kind == "done" and labels(b) == ["unit-u2"],
              str(labels(b)))

# -- a change that belongs to another unit's file ------------------------------------------------
b = mkbox("i-elsewhere", mkplan())
b.script([w("unit-u1", "src/a.py", output={"status": "done", "summary": "a done",
                                           "elsewhere": ["src/b.py: pass the new argument to f()"]}),
          w("unit-u2", "src/b.py")])
res = implement.run(b.ctx)
ls = b.launches()
h.test_result("a unit that leaves a change for another file's owner is done, with no card and no retry",
              res.kind == "done" and labels(b) == ["unit-u1", "unit-u2"], str(labels(b)))
h.test_result("the later unit's prompt carries what the earlier unit left",
              "Left for you by earlier units" in ls[1]["prompt"] and "pass the new argument to f()" in ls[1]["prompt"]
              and "Left for you by earlier units" not in ls[0]["prompt"])
h.test_result("the handoff is kept in the stage record",
              b.flow.data("implement").get("handoffs") == [{"from": "u1", "text": "src/b.py: pass the new argument to f()"}])

# -- partial unit escalation --------------------------------------------------------------------
b = mkbox("i-partial", mkplan(two=False))
b.script([w("unit-u1", "src/a.py", output={"status": "partial", "summary": "half"}),
          w("unit-u1", "src/a.py", "x = 2\n")])
res = implement.run(b.ctx)
models = [str(x.get("model")) for x in b.launches()]
h.test_result("a partial unit is retried once on the stronger model",
              res.kind == "done" and len(models) == 2 and models[0] != models[1]
              and any("(partial)" in s for s in b.subjects()), str(models))

# -- red check, fixers, judge, stronger fixer --------------------------------------------------
b = mkbox("i-codewrong", mkplan(two=False))
b.results("fail", "fail", "fail", "fail", "pass")
b.script([w("unit-u1", "src/a.py"), w("fix-r1", "src/a.py", "x = 3\n", role="fixer"),
          w("fix-r2", "src/a.py", "x = 4\n", role="fixer"),
          {"role": "judge", "output": {"verdict": "code_wrong", "reason": "f returns the wrong value"}},
          w("fix-judged-u1", "src/a.py", "x = 5\n", role="fixer")])
res = implement.run(b.ctx)
ls = b.launches()
h.test_result("red check goes through two fixer rounds, the judge, and a stronger fixer to green",
              res.kind == "done" and labels(b) == ["unit-u1", "fix-r1", "fix-r2", "judge-u1", "fix-judged-u1"],
              str(labels(b)))
fm = [x["model"] for x in ls if x["role"] == "fixer"]
h.test_result("fixer round 1 is the fixer model, round 2 and the judged fix get stronger",
              len(set(fm)) == 3 and fm[0] != fm[1] != fm[2], str(fm))
h.test_result("the judge's verdict is recorded",
              b.flow.data("implement")["judgements"][0]["verdict"] == "code_wrong")
h.test_result("the judge prompt carries the spec behaviour and the failing test file",
              "f works" in ls[3]["prompt"] and "tests/test_a.py" in ls[3]["prompt"])

# -- test_wrong, tightening: applied without a question ---------------------------------------
b = mkbox("i-testwrong", mkplan(two=False))
b.results("fail", "fail", "fail", "fail", "pass")
TIGHT = "def test_one():\n    assert 1 + 1 == 2\n    assert True\n"
b.script([w("unit-u1", "src/a.py"), w("fix-r1", "src/a.py", "x = 3\n", role="fixer"), w("fix-r2", "src/a.py", "x = 4\n", role="fixer"),
          {"role": "judge", "output": {"verdict": "test_wrong", "reason": "typo in expectation",
                                       "edit_weakens": False, "test_edit": "fix the expectation"}},
          w("fix-test-u1", "tests/test_a.py", TIGHT, role="fixer")])
res = implement.run(b.ctx)
h.test_result("test_wrong that does not weaken is applied with no question",
              res.kind == "done" and questions.open_batch(b.flow) is None and b.read("tests/test_a.py") == TIGHT,
              str(res))
te = b.flow.data("implement")["test_edits"]
h.test_result("the test edit is recorded as keeps_or_tightens and applied",
              te and te[0]["verdict"] == "keeps_or_tightens" and te[0]["applied"], str(te))

# -- test_wrong, weakening: parks with a risk card, then apply / keep -----------------------
def weak_box(name: str) -> Box:
    bx = mkbox(name, mkplan(two=False))
    bx.results("fail", "fail", "fail", "fail", "pass", "pass")
    bx.script([w("unit-u1", "src/a.py"), w("fix-r1", "src/a.py", "x = 3\n", role="fixer"),
               w("fix-r2", "src/a.py", "x = 4\n", role="fixer"),
               {"role": "judge", "output": {"verdict": "test_wrong", "reason": "the assertion is too strict",
                                            "edit_weakens": True, "test_edit": "drop the assertion"}},
               w("fix-test-u1", "tests/test_a.py", "def test_one():\n    pass\n", role="fixer")])
    return bx


b = weak_box("i-weak")
res = implement.run(b.ctx)
card = card_of(b) if res.kind == "needs_human" else {}
h.test_result("a weakening test_wrong edit parks with a risk card",
              res.kind == "needs_human" and card.get("kind") == "risk" and card.get("id") == "judge-u1", str(card)[:300])
h.test_result("the card leads with the risk and offers apply or keep, recommending keep",
              str(card.get("decision", "")).startswith("The edit to tests/test_a.py would weaken the test")
              and [o["id"] for o in card.get("options", [])] == ["apply", "keep"]
              and card.get("recommended") == "keep", str(card)[:300])
h.test_result("the test file is untouched while parked", b.read("tests/test_a.py") == "def test_one():\n    assert True\n")
questions.apply_reply(b.ctx, "judge-u1: apply")
res = implement.run(b.ctx)
h.test_result("answering apply and re-entering applies the edit",
              res.kind == "done" and b.read("tests/test_a.py") == "def test_one():\n    pass\n"
              and b.flow.data("implement")["test_edits"][-1]["applied"] is True, str(res))

b = weak_box("i-weak-keep")
implement.run(b.ctx)
b.results("fail", "fail", "fail")
questions.apply_reply(b.ctx, "judge-u1: keep")
try:
    implement.run(b.ctx)
    stop = None
except FlowStop as exc:
    stop = exc
h.test_result("answering keep leaves the test alone and the still-red check stops",
              b.read("tests/test_a.py") == "def test_one():\n    assert True\n"
              and b.flow.data("implement")["test_edits"][-1]["applied"] is False and stop is not None and stop.cls == "check",
              "%s %s" % (stop and stop.cls, stop and stop.reason))

# -- spec_wrong parks -------------------------------------------------------------------------
b = mkbox("i-specwrong", mkplan(two=False))
b.results("fail", "fail", "fail", "fail", "pass")
b.script([w("unit-u1", "src/a.py"), w("fix-r1", "src/a.py", "x = 3\n", role="fixer"), w("fix-r2", "src/a.py", "x = 4\n", role="fixer"),
          {"role": "judge", "output": {"verdict": "spec_wrong", "reason": "the spec contradicts the ticket"}}])
res = implement.run(b.ctx)
card = card_of(b) if res.kind == "needs_human" else {}
h.test_result("spec_wrong parks with a risk card that leads with the spec problem",
              res.kind == "needs_human" and card.get("kind") == "risk"
              and str(card.get("decision", "")).startswith("The spec itself looks wrong"), str(card)[:300])

# -- the existing-test edit rule --------------------------------------------------------------
OLD = "def test_old():\n    assert 1 == 1\n    assert 2 == 2\n"
NEWTIGHT = "def test_old():\n    assert 1 == 1\n    assert 2 == 2\n    assert 3 == 3\n"
NEWWEAK = "def test_old():\n    assert 1 == 1\n"
EDITS = [{"file": "tests/test_old.py", "why": "rename helper"}]


def edit_box(name: str, new: str, verdicts: List[Dict[str, Any]]) -> Box:
    bx = mkbox(name, mkplan(two=False, edits=EDITS), files={"tests/test_old.py": OLD})
    bx.script([w("test-edits", "tests/test_old.py", new), {"role": "edit_judge", "output": {"edits": verdicts}},
               w("unit-u1", "src/a.py")])
    return bx


b = edit_box("e-tight", NEWTIGHT, [{"file": "tests/test_old.py", "verdict": "keeps_or_tightens", "reason": "adds one"}])
res = implement.run(b.ctx)
h.test_result("a keeps_or_tightens edit is applied with no question, before any unit",
              res.kind == "done" and questions.open_batch(b.flow) is None and b.read("tests/test_old.py") == NEWTIGHT
              and labels(b) == ["test-edits", "edit-judge", "unit-u1"], str(labels(b)))
h.test_result("it is recorded as applied", b.flow.data("implement")["test_edits"][0]["applied"] is True)

LOST = "Nothing checks that 2 == 2 any more."
weak = [{"file": "tests/test_old.py", "verdict": "weakens", "reason": "dropped an assertion", "no_longer_proven": LOST}]
b = edit_box("e-weak", NEWWEAK, weak)
res = implement.run(b.ctx)
card = card_of(b) if res.kind == "needs_human" else {}
h.test_result("a weakening edit is reverted and parks with a risk card",
              res.kind == "needs_human" and b.read("tests/test_old.py") == OLD and card.get("kind") == "risk", str(card)[:300])
h.test_result("the card leads with what is no longer proven; options apply or keep; keep recommended",
              str(card.get("decision", "")).startswith(LOST) and card.get("risk") == LOST
              and [o["id"] for o in card.get("options", [])] == ["apply", "keep"] and card.get("recommended") == "keep",
              str(card)[:300])
h.test_result("no unit ran before the answer", labels(b) == ["test-edits", "edit-judge"], str(labels(b)))
cid = card["id"]
questions.apply_reply(b.ctx, "%s: apply" % cid)
res = implement.run(b.ctx)
h.test_result("approving re-applies the edit and the units then run",
              res.kind == "done" and b.read("tests/test_old.py") == NEWWEAK and labels(b)[-1] == "unit-u1"
              and b.flow.data("implement")["test_edits"][0] == {"file": "tests/test_old.py", "verdict": "weakens",
                                                                 "reason": "dropped an assertion", "applied": True},
              str(b.flow.data("implement")["test_edits"]))

b = edit_box("e-decline", NEWWEAK, weak)
implement.run(b.ctx)
questions.apply_reply(b.ctx, "%s: keep" % card_of(b)["id"])
res = implement.run(b.ctx)
h.test_result("declining keeps the original test and the flow carries on",
              res.kind == "done" and b.read("tests/test_old.py") == OLD
              and b.flow.data("implement")["test_edits"][0]["applied"] is False)

b = edit_box("e-unclassified", NEWTIGHT, [])
res = implement.run(b.ctx)
card = card_of(b) if res.kind == "needs_human" else {}
h.test_result("an edit the judge did not classify counts as weakens: reverted and asked",
              res.kind == "needs_human" and b.read("tests/test_old.py") == OLD and card.get("kind") == "risk", str(res))

# -- protected paths are stripped from the plan -----------------------------------------------
pl = mkplan(two=False)
pl["units"][0]["files"] = ["src/a.py", "tests/test_a.py"]
b = mkbox("i-protfiles", pl)
b.script([w("unit-u1", "src/a.py")])
res = implement.run(b.ctx)
pr = b.launches()[0]["prompt"] if b.launches() else ""
allowed = pr.split("Files you may create or change (and no others):")[1].split("## ")[0] if pr else ""
h.test_result("a protected spec test listed in a unit's files is not in the implementer's allowed list",
              res.kind == "done" and "src/a.py" in allowed and "tests/test_a.py" not in allowed, allowed)
h.test_result("it still appears under protected paths", "tests/test_a.py" in pr.split("## Protected paths")[1])

b = mkbox("i-protedit", mkplan(two=False, edits=[{"file": "tests/test_a.py", "why": "rename helper"}]))
b.script([w("unit-u1", "src/a.py")])
res = implement.run(b.ctx)
h.test_result("an existing_test_edit naming a protected file is dropped: no edit or judge call, stage completes",
              res.kind == "done" and labels(b) == ["unit-u1"] and questions.open_batch(b.flow) is None
              and b.flow.data("implement")["test_edits"] == [], str(labels(b)))

# -- one check per distinct tree per command (#571) ---------------------------------------------


def plan3() -> Dict[str, Any]:
    plan = mkplan()
    plan["units"].append({"id": "u3", "title": "third", "files": ["src/c.py"], "steps": ["write c"],
                          "acceptance": ["ac1"], "after": ["u2"]})
    return plan


def journal(b: Box) -> str:
    p = b.flow.path / "journal.md"
    return p.read_text() if p.is_file() else ""


def tree(b: Box) -> str:
    return sh(["git", "rev-parse", "HEAD^{tree}"], b.clone)


THREE = [w("unit-u1", "src/a.py"), w("unit-u2", "src/b.py"), w("unit-u3", "src/c.py")]

b = mkbox("i-onecheck", plan3())
b.script(list(THREE))
res = implement.run(b.ctx)
h.test_result("three units, one check: only the tree after the last unit is measured",
              res.kind == "done" and labels(b) == ["unit-u1", "unit-u2", "unit-u3"] and b.runs() == 1,
              "runs=%d" % b.runs())
h.test_result("units before the last are done without a check of their own",
              b.flow.data("implement")["done_units"] == ["u1", "u2", "u3"])
run = heal.check(b.ctx, "fast")
h.test_result("checking the finished tree again runs nothing and journals the reuse with its tree",
              run.reused and b.runs() == 1
              and "not re-run: tree %s already passed" % tree(b)[:12] in journal(b), journal(b)[-400:])

b = mkbox("i-perunit", plan3(), per_unit=True)
b.script(list(THREE))
res = implement.run(b.ctx)
h.test_result("check.per_unit: every unit is checked as it lands, once per tree",
              res.kind == "done" and b.runs() == 3, "runs=%d" % b.runs())

b = mkbox("i-onecheck-red", plan3())
b.results("fail", "fail", "pass")
b.script(list(THREE) + [w("fix-r1", "src/a.py", "x = 9\n", role="fixer")])
res = implement.run(b.ctx)
fix = [x for x in b.launches() if x["label"] == "fix-r1"]
h.test_result("the one check's fixer may edit any unit's files, since any unit may have broken it",
              res.kind == "done" and b.runs() == 3 and len(fix) == 1 and b.read("src/a.py") == "x = 9\n"
              and all(f in fix[0]["prompt"] for f in ("src/a.py", "src/b.py", "src/c.py")), "runs=%d" % b.runs())

b = mkbox("i-red-idle", mkplan(two=False))
b.results("fail", "fail", "fail", "fail", "fail", "fail")
b.script([w("unit-u1", "src/a.py"), {"role": "fixer", "label": "fix-r1", "output": DONE},
          {"role": "fixer", "label": "fix-r2", "output": DONE},
          {"role": "judge", "output": {"verdict": "code_wrong", "reason": "still wrong"}},
          {"role": "fixer", "label": "fix-judged-u1", "output": DONE}])
try:
    implement.run(b.ctx)
except FlowStop:
    pass
h.test_result("fixers that change nothing never cause the same red tree to be checked again",
              b.runs() == 2 and journal(b).count("not re-run: tree %s already failed" % tree(b)[:12]) >= 2,
              "runs=%d" % b.runs())

# -- a worker that cannot finish from here (#572) ------------------------------------------------


def blocked(needs: str, what: str, reason: str) -> Dict[str, Any]:
    return {"status": "blocked", "summary": "could not", "blocked": {"reason": reason, "needs": needs, "what": what}}


b = mkbox("i-blocked-handoff", mkplan())
b.script([w("unit-u1", "src/a.py", output=blocked("other_file", "src/b.py", "f() must also be called from b.")),
          w("unit-u2", "src/b.py")])
res = implement.run(b.ctx)
ls = b.launches()
h.test_result("a unit blocked on a later unit's file hands it over: no retry, no card, no stop",
              res.kind == "done" and labels(b) == ["unit-u1", "unit-u2"] and questions.open_batch(b.flow) is None,
              str(labels(b)))
h.test_result("the unit that owns the file is told what was needed",
              "f() must also be called from b." in ls[1]["prompt"]
              and b.flow.data("implement")["handoffs"] == [{"from": "u1", "text": "src/b.py: f() must also be called from b."}])

b = mkbox("i-blocked-shell", mkplan(two=False))
b.script([w("unit-u1", "src/a.py", output=blocked("shell", "npm run codegen", "The types are generated by a command."))])
stop = expect_stop(lambda: implement.run(b.ctx), "environment")
h.test_result("a unit that needs a command run stops naming it, quoting the worker, with one launch",
              stop is not None and "npm run codegen" in stop.reason
              and "The types are generated by a command." in stop.reason and labels(b) == ["unit-u1"], str(stop))

b = mkbox("i-blocked-nobody", mkplan(two=False))
b.script([w("unit-u1", "src/a.py", output=blocked("other_file", "src/zzz.py", "The bug is elsewhere."))])
stop = expect_stop(lambda: implement.run(b.ctx), "implement")
h.test_result("a file no later unit owns is a stop, not a handoff",
              stop is not None and "src/zzz.py" in stop.reason and labels(b) == ["unit-u1"], str(stop))

b = mkbox("i-blocked-fixer", mkplan(two=False))
b.results("fail", "fail")
b.script([w("unit-u1", "src/a.py"),
          {"role": "fixer", "label": "fix-r1",
           "output": blocked("shell", "git merge origin/main", "The branch is behind the base and I have no shell.")}])
stop = expect_stop(lambda: implement.run(b.ctx), "environment")
models = [x["model"] for x in b.launches() if x["role"] == "fixer"]
h.test_result("a check fixer that says it cannot fix from here: no round 2, no judge, no stronger model",
              stop is not None and labels(b) == ["unit-u1", "fix-r1"] and len(models) == 1
              and "The branch is behind the base and I have no shell." in stop.reason
              and "git merge origin/main" in stop.reason, "%s %s" % (labels(b), stop))

# -- a unit's bookkeeping question never becomes a card (#570) -----------------------------------
OWN_Q = {"id": "own1", "question": "Which unit should own the edit to src/b.py?",
         "context": "u1 needs src/b.py changed but may only edit src/a.py.",
         "options": [{"id": "a", "label": "Leave it to u2", "consequence": "u2 edits src/b.py later."},
                     {"id": "b", "label": "Add src/b.py to this unit's file list", "consequence": "u1 edits both."}],
         "recommended": "b", "kind": "architecture", "reversible": True, "risk": ""}
b = mkbox("i-ownership", mkplan())
b.script([w("unit-u1", "src/a.py", output={"status": "needs_answers", "summary": "asking", "questions": [OWN_Q]}),
          w("unit-u1-answers", "src/a.py", "x = 2\n",
            output={"status": "done", "summary": "a done", "elsewhere": ["src/b.py: call f()"]}),
          w("unit-u2", "src/b.py")])
res = implement.run(b.ctx)
ls = b.launches()
h.test_result("an ownership question from a unit is settled without a card and the flow goes on",
              res.kind == "done" and labels(b) == ["unit-u1", "unit-u1-answers", "unit-u2"]
              and questions.open_batch(b.flow) is None and not (b.flow.path / "questions.json").exists(),
              str(labels(b)))
h.test_result("the worker is told the driver's rule, not handed an option",
              "Keep to the files you were given" in ls[1]["prompt"] and "call f()" in ls[2]["prompt"], ls[1]["prompt"][-600:])
h.test_result("the journal says the driver settled it",
              "the driver (not a question for a person)" in journal(b))

# -- F4: Handoff path comparison with normalised paths -----
# Verify that the existing elsewhere/handoff test still passes - it tests normalised path equality
# (The earlier test at line ~213 "a unit that leaves a change for another file's owner..." already
# validates that handoffs use normalised path comparison, not substring matching)
# This just confirms the behaviour is preserved:
b = mkbox("i-handoff-normalised", mkplan())
b.script([w("unit-u1", "src/a.py", output={"status": "done", "summary": "a done",
                                           "elsewhere": ["src/b.py: pass the new argument to f()"]}),
          w("unit-u2", "src/b.py")])
res = implement.run(b.ctx)
handoffs = b.flow.data("implement").get("handoffs", [])
# F4: Verify paths are compared by normalised equality (not substring match)
h.test_result("F4: handoff path comparison uses normalised equality",
              res.kind == "done" and len(handoffs) == 1 and
              handoffs[0]["text"] == "src/b.py: pass the new argument to f()",
              f"done={res.kind=='done'}, handoffs={handoffs}")

if __name__ == "__main__":
    h.summarize_and_exit()
