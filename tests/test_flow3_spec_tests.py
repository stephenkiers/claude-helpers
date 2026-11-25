#!/usr/bin/env python3
"""
Tests for scripts/flow3/steps/spec_tests.py: the spec-blind test author (PLAN.md section 7.3).
No real model is called: FLOW3_CLAUDE_BIN is tests/flow3_fake_claude.py, scripted per scenario.
"""
import copy
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

from scripts.flow3 import context, gitops, heal, questions, runner, voice  # noqa: E402
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
            "recommended": "a", "kind": kind, "reversible": True,
            "risk": "Picking wrong renames a public helper." if kind == "risk" else ""}

from scripts.flow3.steps import spec_tests  # noqa: E402

h = Harness("flow3 spec tests")
DONE = {"status": "done", "summary": "wrote the tests"}
PLAN: Dict[str, Any] = {
    "goal": "GOALPROSE make doubling work", "non_goals": ["NONGOAL"],
    "acceptance_criteria": [{"id": "ac1", "text": "ACMARK doubles"}],
    "units": [{"id": "u1", "title": "UNITTITLE", "files": ["src/a.py"], "steps": ["STEPMARK write f"],
               "acceptance": ["ac1"]}],
    "interfaces": [{"name": "f", "signature": "def f(x: int) -> int", "file": "src/a.py"}],
    "test_spec": [{"id": "t1", "behaviour": "f doubles its input", "test_file": "tests/test_a.py"}],
    "untestable_reason": "", "existing_test_edits": [],
}
TEST_BODY = "from src.a import f\n\n\ndef test_one():\n    assert f(2) == 4\n"

# -- the normal path --------------------------------------------------------------------------
b = Box("s-ok", plan=PLAN)
voice.say(b.flow, "DIRMARK keep it small")
b.script([{"role": "test_author", "write_files": {"tests/test_a.py": TEST_BODY}, "output": DONE}])
before = gitops.head_sha(b.clone)
res = spec_tests.run(b.ctx)
prompt = b.launches()[0]["prompt"]
h.test_result("the stage is done", res.kind == "done", str(res))
h.test_result("exactly one author call", len(b.launches()) == 1 and b.launches()[0]["role"] == "test_author")
h.test_result("the prompt carries the behaviour, the signature, the check command and the test file",
              "f doubles its input" in prompt and "def f(x: int) -> int" in prompt
              and "check.py" in prompt and "tests/test_a.py" in prompt)
h.test_result("the prompt is spec-blind: no unit, step, goal, criterion or non-goal text",
              all(m not in prompt for m in ("STEPMARK", "UNITTITLE", "GOALPROSE", "ACMARK", "NONGOAL")), prompt[:300])
h.test_result("the human's directions reach the author", "DIRMARK" in prompt)
sha = gitops.head_sha(b.clone)
h.test_result("the tests are committed", sha != before and b.read("tests/test_a.py") == TEST_BODY
              and b.subjects()[0] == "flow3 #1: spec tests", str(b.subjects()))
h.test_result("data.spec_tests records files, commit and no skip",
              b.flow.data("spec_tests") == {"files": ["tests/test_a.py"], "commit": sha, "skipped": ""},
              str(b.flow.data("spec_tests")))
h.test_result("the spec tests are recorded as protected", b.flow.get("protected") == ["tests/test_a.py"])
h.test_result("the check is not run", b.runs() == 0)
h.test_result("a step record and a journal entry are written",
              any("spec_tests" in str(p) for p in b.flow.path.rglob("*")) and "Spec tests" in
              "".join(p.read_text() for p in b.flow.path.glob("journal*")))
res = spec_tests.run(b.ctx)
h.test_result("re-entering a finished stage is a no-op", res.kind == "done" and len(b.launches()) == 1)

# -- protected merges with what was there -----------------------------------------------------
b = Box("s-merge", plan=PLAN)
b.flow.set("protected", ["docs/keep.md"])
b.script([{"role": "test_author", "write_files": {"tests/test_a.py": TEST_BODY}, "output": DONE}])
spec_tests.run(b.ctx)
h.test_result("the protected list is merged and sorted", b.flow.get("protected") == ["docs/keep.md", "tests/test_a.py"],
              str(b.flow.get("protected")))

# -- untestable --------------------------------------------------------------------------------
untestable = copy.deepcopy(PLAN)
untestable["test_spec"] = []
untestable["untestable_reason"] = "It only changes wording in a doc."
b = Box("s-skip", plan=untestable)
b.script([])
res = spec_tests.run(b.ctx)
h.test_result("an untestable plan is skipped with its reason and no call",
              res.kind == "done" and b.launches() == []
              and b.flow.data("spec_tests") == {"files": [], "commit": None, "skipped": "It only changes wording in a doc."},
              str(b.flow.data("spec_tests")))
h.test_result("a skipped stage protects nothing", not b.flow.get("protected"))

# -- the author strays -------------------------------------------------------------------------
b = Box("s-stray", plan=PLAN, protected=["docs/"])
b.script([{"role": "test_author", "write_files": {"tests/test_a.py": TEST_BODY, "src/a.py": "def f(x):\n    return 2 * x\n",
                                                   "docs/x.md": "x\n"}, "output": DONE},
          {"role": "test_author", "write_files": {"tests/test_a.py": TEST_BODY}, "output": DONE}])
res = spec_tests.run(b.ctx)
h.test_result("writing outside the spec's test files is reverted and retried",
              res.kind == "done" and b.read("src/a.py") is None and b.read("docs/x.md") is None
              and b.read("tests/test_a.py") == TEST_BODY and len(b.launches()) == 2)

b = Box("s-stray2", plan=PLAN)
stray = {"tests/test_a.py": TEST_BODY, "src/a.py": "x\n"}
b.script([{"role": "test_author", "write_files": stray, "output": DONE},
          {"role": "test_author", "write_files": stray, "output": DONE}])
stop = expect_stop(lambda: spec_tests.run(b.ctx), "protected")
h.test_result("a second stray write stops protected", stop is not None)

# -- the author writes nothing -----------------------------------------------------------------
b = Box("s-none", plan=PLAN)
b.script([{"role": "test_author", "output": DONE}])
stop = expect_stop(lambda: spec_tests.run(b.ctx), "implement")
h.test_result("an author who writes no test file stops implement", stop is not None and "tests/test_a.py" in stop.reason)

# -- the author asks ---------------------------------------------------------------------------
b = Box("s-ask", plan=PLAN)
ask = {"status": "needs_answers", "summary": "unclear", "questions": [question("t-q1", "risk")]}
b.script([{"role": "test_author", "output": ask, "session_id": "author-1"},
          {"role": "test_author", "write_files": {"tests/test_a.py": TEST_BODY}, "output": DONE}])
res = spec_tests.run(b.ctx)
h.test_result("a question for the human parks the stage", res.kind == "needs_human"
              and questions.open_batch(b.flow) is not None and b.flow.data("spec_tests").get("phase") == "waiting")
questions.apply_reply(b.ctx, "t-q1: a")
res = spec_tests.run(b.ctx)
second = b.launches()[1]
h.test_result("on re-entry the author resumes with the answer and finishes",
              res.kind == "done" and second["resume"] == "author-1" and "Name A" in second["prompt"]
              and b.flow.data("spec_tests")["files"] == ["tests/test_a.py"], str(b.flow.data("spec_tests")))

# -- the plan names a test file the base already has ---------------------------------------------
OLD_TEST = "def test_old():\n    assert True\n"
b = Box("s-exists", files={"tests/test_a.py": OLD_TEST}, plan=PLAN)
b.script([{"role": "test_author", "write_files": {"tests/test_a_spec.py": TEST_BODY}, "output": DONE}])
res = spec_tests.run(b.ctx)
prompt = b.launches()[0]["prompt"]
h.test_result("spec tests for an existing test file go in a new sibling file",
              res.kind == "done" and b.flow.data("spec_tests")["files"] == ["tests/test_a_spec.py"]
              and "tests/test_a_spec.py" in prompt, str(b.flow.data("spec_tests")))
h.test_result("the existing test file is untouched and not protected",
              b.read("tests/test_a.py") == OLD_TEST and b.flow.get("protected") == ["tests/test_a_spec.py"])

b = Box("s-exists2", files={"tests/test_a.py": OLD_TEST}, plan=PLAN)
b.script([{"role": "test_author", "write_files": {"tests/test_a.py": TEST_BODY}, "output": DONE}],
         default={"output": DONE})
stop = expect_stop(lambda: spec_tests.run(b.ctx), "protected")
h.test_result("an author who edits the existing test file instead is reverted and stopped",
              b.read("tests/test_a.py") == OLD_TEST, str(stop))

if __name__ == "__main__":
    h.summarize_and_exit()
