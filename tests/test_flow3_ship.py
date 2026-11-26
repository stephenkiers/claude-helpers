#!/usr/bin/env python3
"""
Tests for scripts/flow3/steps/ship.py (PLAN.md section 7.7). The local provider and a scripted fake
`claude` are used; nothing touches a network.
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
TMP = Path(tempfile.mkdtemp(prefix="flow3-shiptest-")).resolve()
HOME = TMP / "home"
os.environ["FLOW3_HOME"] = str(HOME)
os.environ["FLOW3_CLAUDE_BIN"] = str(ROOT / "tests" / "flow3_fake_claude.py")

from _test_harness import Harness  # noqa: E402

from scripts.flow3 import context, gitops, heal, runner  # noqa: E402
from scripts.flow3.state import FlowDir  # noqa: E402
from scripts.flow3.steps import ship  # noqa: E402
from scripts.flow3.types import FlowStop  # noqa: E402

h = Harness("flow3 ship")
heal._sleep = lambda s: None  # type: ignore[assignment]
runner._sleep = lambda s: None  # type: ignore[assignment]
_n = {"n": 0}


def sh(args: List[str], cwd: Path) -> str:
    return subprocess.run(args, cwd=str(cwd), check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True).stdout.strip()


CHECK_PY = '''
import sys, os
d, mode = sys.argv[1], sys.argv[2]
open(d + "/runs", "a").write(mode + "\\n")
lines = []
if os.path.exists(d + "/results"):
    lines = [x for x in open(d + "/results").read().split("\\n") if x]
cur = lines.pop(0) if lines else "pass"
open(d + "/results", "w").write("\\n".join(lines) + "\\n")
if cur != "pass":
    print("FAILED tests/test_a.py::test_one - assert 1 == 2")
    sys.exit(1)
'''


class Box:
    def __init__(self, name: str) -> None:
        self.name = name
        self.ctl = TMP / (name + "-ctl")
        self.ctl.mkdir()
        (self.ctl / "check.py").write_text(CHECK_PY)
        yml = ["provider: local", "check:", "  fast: 'python3 %s/check.py %s fast'" % (self.ctl, self.ctl),
               "  full: 'python3 %s/check.py %s full'" % (self.ctl, self.ctl)]
        self.origin = TMP / (name + "-origin.git")
        self.clone = TMP / name
        sh(["git", "init", "--bare", "-q", "-b", "main", str(self.origin)], TMP)
        sh(["git", "clone", "-q", str(self.origin), str(self.clone)], TMP)
        sh(["git", "config", "user.name", "t"], self.clone)
        sh(["git", "config", "user.email", "t@example.invalid"], self.clone)
        (self.clone / ".claude").mkdir()
        (self.clone / ".claude" / "flow.yaml").write_text("\n".join(yml) + "\n")
        (self.clone / "README.md").write_text("hi\n")
        sh(["git", "add", "-A"], self.clone)
        sh(["git", "commit", "-q", "-m", "init"], self.clone)
        sh(["git", "push", "-q", "origin", "main"], self.clone)
        sh(["git", "remote", "set-head", "origin", "main"], self.clone)
        self.flow = FlowDir.create(name, 1, "Add feature X", self.clone)
        self.branch = "flow3/1-add-feature-x"
        self.wt = TMP / (name + "-wt")
        gitops.create_worktree(self.clone, self.wt, self.branch, "origin/main")
        self.flow.set("worktree", str(self.wt))
        self.flow.set("branch", self.branch)
        self.flow.set("base", "main")
        self.flow.set_data("plan", {"phase": "done", "plan": {
            "goal": "Make X possible.", "units": [{"id": "u1", "title": "Write the X module"}]}})
        self.ctx = context.build(self.flow)

    def write(self, rel: str, text: str) -> None:
        (self.wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.wt / rel).write_text(text)

    def script(self, default: Dict[str, Any]) -> None:
        _n["n"] += 1
        path = self.ctl / ("script%d.json" % _n["n"])
        path.write_text(json.dumps({"calls": [], "default": default}))
        os.environ["FLOW3_FAKE_SCRIPT"] = str(path)
        self._log = self.ctl / ("log%d.jsonl" % _n["n"])
        os.environ["FLOW3_FAKE_LOG"] = str(self._log)

    def launches(self) -> int:
        return len(self._log.read_text().splitlines()) if self._log.exists() else 0

    def check_runs(self) -> int:
        p = self.ctl / "runs"
        return len(p.read_text().split()) if p.exists() else 0

    def pr(self, n: int) -> Optional[Dict[str, Any]]:
        p = self.origin / "flow3" / "prs" / ("%d.json" % n)
        return json.loads(p.read_text()) if p.exists() else None

    def origin_has_branch(self) -> bool:
        proc = subprocess.run(["git", "rev-parse", "--verify", "--quiet", "refs/heads/" + self.branch],
                              cwd=str(self.origin), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return proc.returncode == 0


# -- 1. first ship: commit, check, push, open the PR with the pr_text title and body
b = Box("s1")
b.script({"mode": "ok", "output": {"title": "Add feature X", "body": "This adds X.\n\n- the X module"}})
b.write("x.txt", "x\n")
res = ship.run(b.ctx)
h.test_result("ship returns done", res.kind == "done")
h.test_result("origin has the branch", b.origin_has_branch())
pr = b.pr(1)
h.test_result("PR opened with the pr_text title", pr is not None and pr["title"] == "Add feature X", str(pr))
h.test_result("PR body is the pr_text body plus a Closes line",
              pr is not None and "This adds X." in pr["body"] and "Closes #1" in pr["body"])
ship_data = b.flow.data("ship")
head = gitops.head_sha(b.wt)
h.test_result("data.ship has pr, url, title, head", ship_data == {
    "pr": 1, "url": "local://s1-origin/pr/1", "title": "Add feature X", "head": head}, str(ship_data))
h.test_result("top-level pr is set", b.flow.get("pr") == 1)
h.test_result("branch head on origin equals local head",
              sh(["git", "rev-parse", "refs/heads/" + b.branch], b.origin) == head)
h.test_result("leftover file was committed", sh(["git", "status", "--porcelain"], b.wt) == "")
h.test_result("exactly one pr_text call", b.launches() == 1)
h.test_result("fast check ran once", b.check_runs() == 1)
h.test_result("step record and journal written", any((b.flow.path / "steps").glob("*ship.md"))
              and "Shipped" in (b.flow.path / "journal.md").read_text())

# -- 2. re-ship after a send-back reuses the PR and makes no pr_text call
b.write("y.txt", "y\n")
res = ship.run(b.ctx)
h.test_result("re-ship returns done", res.kind == "done")
h.test_result("no second PR", b.pr(2) is None and b.flow.get("pr") == 1)
h.test_result("no second pr_text call", b.launches() == 1)
h.test_result("re-ship pushed the new commit",
              sh(["git", "rev-parse", "refs/heads/" + b.branch], b.origin) == gitops.head_sha(b.wt))
h.test_result("re-ship keeps the title and updates head", b.flow.data("ship")["title"] == "Add feature X"
              and b.flow.data("ship")["head"] == gitops.head_sha(b.wt))
before = b.check_runs()
ship.run(b.ctx)
h.test_result("a third ship on the same tree does not re-run the check", b.check_runs() == before,
              "%d vs %d" % (b.check_runs(), before))

# -- 3. behind base: base is merged in before the push
b = Box("s3")
b.script({"mode": "ok", "output": {"title": "T", "body": "B"}})
other = TMP / "s3-other"
sh(["git", "clone", "-q", str(b.origin), str(other)], TMP)
sh(["git", "config", "user.name", "t"], other)
sh(["git", "config", "user.email", "t@example.invalid"], other)
(other / "from_base.txt").write_text("base moved\n")
sh(["git", "add", "-A"], other)
sh(["git", "commit", "-q", "-m", "base work"], other)
sh(["git", "push", "-q", "origin", "main"], other)
b.write("mine.txt", "mine\n")
ship.run(b.ctx)
tree_files = sh(["git", "ls-tree", "-r", "--name-only", "refs/heads/" + b.branch], b.origin).split()
h.test_result("pushed branch contains the base's new file and ours", "from_base.txt" in tree_files
              and "mine.txt" in tree_files, str(tree_files))
h.test_result("the base tip is an ancestor of the pushed branch",
              subprocess.run(["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"], cwd=str(b.wt)).returncode == 0)
h.test_result("a base-stale heal was recorded",
              any(e.get("kind") == "heal" and e.get("cls") == "base-stale" for e in b.flow.events()))

# -- 4. pr_text failure falls back to the issue title and a body rendered from the plan
b = Box("s4")
b.script({"mode": "crash"})
b.write("z.txt", "z\n")
res = ship.run(b.ctx)
pr = b.pr(1)
h.test_result("ship still succeeds when pr_text fails", res.kind == "done" and pr is not None)
h.test_result("fallback title is the issue title", pr is not None and pr["title"] == "Add feature X",
              str(pr and pr["title"]))
h.test_result("fallback body renders the plan", pr is not None and "Make X possible." in pr["body"]
              and "Write the X module" in pr["body"] and "Closes #1" in pr["body"])
h.test_result("the fallback is recorded", any(e.get("kind") == "pr_text_fallback" for e in b.flow.events()))

# an empty title from the model is also a fallback
b = Box("s4b")
b.script({"mode": "ok", "output": {"title": "  ", "body": "b"}})
b.write("z.txt", "z\n")
ship.run(b.ctx)
h.test_result("blank pr_text title falls back", (b.pr(1) or {}).get("title") == "Add feature X")

# -- 5. a red check stops before any push
b = Box("s5")
b.script({"mode": "ok", "output": {"title": "T", "body": "B"}})
(b.ctl / "results").write_text("fail\nfail\nfail\nfail\n")
b.write("w.txt", "w\n")
stop: Optional[FlowStop] = None
try:
    ship.run(b.ctx)
except FlowStop as exc:
    stop = exc
h.test_result("red check stops with class check", stop is not None and stop.cls == "check", str(stop))
h.test_result("stop carries a reason", stop is not None and bool(stop.reason))
h.test_result("origin has no branch after a red check", not b.origin_has_branch())
h.test_result("no PR and no pr_text call after a red check", b.pr(1) is None and b.launches() == 0)

h.summarize_and_exit()
