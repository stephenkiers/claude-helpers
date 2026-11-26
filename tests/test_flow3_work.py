#!/usr/bin/env python3
"""
Tests for scripts/flow3/steps/work.py: protected paths, run_unit, and the check fixer.
No real model is called: FLOW3_CLAUDE_BIN is tests/flow3_fake_claude.py, scripted per scenario.
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

from scripts.flow3 import context, gitops, heal, runner  # noqa: E402
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
                 protected: Optional[List[str]] = None, plan: Optional[Dict[str, Any]] = None) -> None:
        self.ctl = TMP / (name + "-ctl")
        self.ctl.mkdir()
        (self.ctl / "check.py").write_text(CHECK_PY)
        check = "python3 %s/check.py %s" % (self.ctl, self.ctl)
        yml = ["provider: local", "check:", "  fast: '%s'" % check, "  full: '%s'" % check,
               "  timeout_secs: 60", "budget:", "  soft_usd: 15.0", "  hard_usd: 40.0"]
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
            "recommended": "a", "kind": kind, "reversible": True}

from scripts.flow3.steps import work  # noqa: E402

h = Harness("flow3 work")
DONE = {"status": "done", "summary": "did it"}
UNIT = {"id": "u1", "title": "Add a", "files": ["src/a.py"], "steps": ["write a"], "acceptance": []}

# -- protected_paths --------------------------------------------------------------------------
b = Box("w-prot", protected=["docs/"])
b.flow.set("protected", ["tests/test_a.py", "docs/"])
h.test_result("protected_paths merges config and state without repeats",
              work.protected_paths(b.ctx) == ["docs/", "tests/test_a.py"], str(work.protected_paths(b.ctx)))
b2 = Box("w-prot2")
h.test_result("protected_paths is empty with neither", work.protected_paths(b2.ctx) == [])

# -- run_unit: commits in-unit edits ----------------------------------------------------------
b = Box("w-ok")
b.script([{"role": "implementer", "write_files": {"src/a.py": "A = 1\n"}, "output": DONE}])
res = work.run_unit(b.ctx, UNIT, "implementer", "do the unit", "flow3 #1 unit u1: Add a")
h.test_result("run_unit returns the worker's result", res.ok and (res.output or {}).get("status") == "done")
h.test_result("run_unit commits the unit's edit", b.read("src/a.py") == "A = 1\n"
              and b.subjects()[0] == "flow3 #1 unit u1: Add a", str(b.subjects()))
h.test_result("run_unit runs the check once", b.runs() == 1, str(b.runs()))
h.test_result("run_unit gives the worker write tools",
              "Edit" in b.launches()[0]["argv"][b.launches()[0]["argv"].index("--tools") + 1])

# -- an edit outside the unit's files ---------------------------------------------------------
b = Box("w-out")
b.script([{"role": "implementer", "write_files": {"src/a.py": "A = 1\n", "src/other.py": "X\n"}, "output": DONE},
          {"role": "implementer", "write_files": {"src/a.py": "A = 2\n"}, "output": DONE}])
res = work.run_unit(b.ctx, UNIT, "implementer", "do the unit", "msg")
h.test_result("out-of-unit edit is reverted, the retry succeeds",
              b.read("src/other.py") is None and b.read("src/a.py") == "A = 2\n" and len(b.launches()) == 2,
              str(b.launches()))
h.test_result("the retry restates the rule", "src/other.py" in b.launches()[1]["prompt"])

b = Box("w-out2")
both = {"src/a.py": "A = 1\n", "src/other.py": "X\n"}
b.script([{"role": "implementer", "write_files": both, "output": DONE},
          {"role": "implementer", "write_files": both, "output": DONE}])
stop = expect_stop(lambda: work.run_unit(b.ctx, UNIT, "implementer", "p", "msg"), "protected")
h.test_result("a second out-of-unit edit stops protected", stop is not None and "src/other.py" in stop.reason,
              str(stop))
h.test_result("the stopped worktree has no stray file", b.read("src/other.py") is None)

# -- a protected spec test ---------------------------------------------------------------------
b = Box("w-spec", files={"tests/test_a.py": "def test_one():\n    assert True\n"})
b.flow.set("protected", ["tests/test_a.py"])
unit = {"id": "u1", "title": "t", "files": ["src/a.py", "tests/test_a.py"], "steps": ["s"], "acceptance": []}
b.script([{"role": "implementer", "write_files": {"src/a.py": "A\n", "tests/test_a.py": "def test_one():\n    pass\n"},
           "output": DONE},
          {"role": "implementer", "write_files": {"src/a.py": "A\n"}, "output": DONE}])
work.run_unit(b.ctx, unit, "implementer", "p", "msg")
h.test_result("editing a protected spec test is reverted",
              b.read("tests/test_a.py") == "def test_one():\n    assert True\n" and b.read("src/a.py") == "A\n")

# -- check_fixer: cheap model first, then the stronger ----------------------------------------
b = Box("w-fix")
b.results("fail", "fail", "fail", "pass")
b.script([{"role": "implementer", "write_files": {"src/a.py": "A = 1\n"}, "output": DONE},
          {"role": "fixer", "label": "fix-r1", "write_files": {"src/a.py": "A = 2\n"}, "output": DONE},
          {"role": "fixer", "label": "fix-r2", "write_files": {"src/a.py": "A = 3\n"}, "output": DONE}])
res = work.run_unit(b.ctx, UNIT, "implementer", "p", "msg")
fixers = [x for x in b.launches() if x["role"] == "fixer"]
h.test_result("check_fixer: round 1 on the cheap model, round 2 on the stronger",
              [x["model"] for x in fixers] == ["haiku", "sonnet"], str([x["model"] for x in fixers]))
h.test_result("check_fixer: the fixer gets the trimmed failure", "test_one" in fixers[0]["prompt"])
h.test_result("check_fixer: run_unit ends green and committed",
              b.read("src/a.py") == "A = 3\n" and b.runs() == 4 and any("fix the fast check (round 2)" in s for s in b.subjects()),
              str(b.subjects()))

b = Box("w-fix2")
b.results("fail", "fail", "pass")
b.script([{"role": "implementer", "write_files": {"src/a.py": "A = 1\n"}, "output": DONE},
          {"role": "fixer", "write_files": {"src/b.py": "B = 1\n"}, "output": DONE}])
work.run_unit(b.ctx, UNIT, "implementer", "p", "msg", fixer_allowed=["src/b.py"])
h.test_result("fixer_allowed widens what the fixer may touch", b.read("src/b.py") == "B = 1\n")

b = Box("w-fix3", protected=["docs/"])
b.results("fail", "fail", "pass")
b.script([{"role": "implementer", "write_files": {"src/a.py": "A = 1\n"}, "output": DONE},
          {"role": "fixer", "write_files": {"src/a.py": "A = 2\n"}, "output": DONE}])
work.run_unit(b.ctx, UNIT, "implementer", "p", "msg")
fx = [x for x in b.launches() if x["role"] == "fixer"][0]["prompt"]
h.test_result("the fixer prompt names its files and the protected paths", "src/a.py" in fx and "docs/" in fx)

# -- a red check ------------------------------------------------------------------------------
b = Box("w-red")
b.results("fail", "fail", "fail", "fail")
b.script([{"role": "implementer", "write_files": {"src/a.py": "A = 1\n"}, "output": DONE}], default={"output": DONE})
stop = expect_stop(lambda: work.run_unit(b.ctx, UNIT, "implementer", "p", "msg"), "check")
h.test_result("run_unit stops check when both fixer rounds fail", stop is not None and bool(stop.tried))

b = Box("w-red2")
b.results("fail", "fail", "fail", "fail")
b.script([{"role": "implementer", "write_files": {"src/a.py": "A = 1\n"}, "output": DONE}], default={"output": DONE})
res, run = work.run_unit_checked(b.ctx, UNIT, "implementer", "p", "msg", raise_on_fail=False)
h.test_result("raise_on_fail=False returns the red CheckRun", run is not None and not run.ok and res.ok)
b.results("fail", "fail", "fail", "fail")
res2 = work.run_unit(b.ctx, UNIT, "implementer", "p", "msg", raise_on_fail=False)
h.test_result("run_unit(raise_on_fail=False) does not stop", res2.ok)

# -- needs_answers is not swallowed -----------------------------------------------------------
b = Box("w-ask")
ask = {"status": "needs_answers", "summary": "need a decision", "questions": [question("q1")]}
b.script([{"role": "implementer", "output": ask, "session_id": "sess-1"}])
res = work.run_unit(b.ctx, UNIT, "implementer", "p", "msg")
h.test_result("needs_answers is returned as is", work.needs_answers(res) and res.session_id == "sess-1")
h.test_result("no check runs while the worker is waiting", b.runs() == 0)

# -- resume_worker ----------------------------------------------------------------------------
b = Box("w-resume")
b.script([{"role": "implementer", "write_files": {"src/a.py": "A = 9\n"}, "output": DONE}])
res = work.resume_worker(b.ctx, "implementer", "sess-1", "orig", "Question q1\n  Answer: A", ["src/a.py"], "msg", "lbl")
ln = b.launches()[0]
h.test_result("resume_worker resumes the session with the answers",
              ln["resume"] == "sess-1" and "Question q1" in ln["prompt"] and b.read("src/a.py") == "A = 9\n")
b.script([{"role": "implementer", "output": DONE}])
work.resume_worker(b.ctx, "implementer", None, "ORIGINAL PROMPT", "Question q1", ["src/a.py"], "msg", "lbl")
h.test_result("without a session the original prompt is restated",
              b.launches()[0]["resume"] is None and "ORIGINAL PROMPT" in b.launches()[0]["prompt"])

# -- helpers ----------------------------------------------------------------------------------
h.test_result("interfaces_text lists signatures",
              "f(x) -> int" in work.interfaces_text({"interfaces": [{"name": "f", "signature": "f(x) -> int", "file": "src/a.py"}]}))
h.test_result("cap cuts and says so", "cut" in work.cap("x" * 50, 10) and work.cap("x", 10) == "x")

if __name__ == "__main__":
    h.summarize_and_exit()
