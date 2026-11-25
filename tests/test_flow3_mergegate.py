#!/usr/bin/env python3
"""
Tests for scripts/flow3/mergegate.py (PLAN.md section 7.8): the screen and the merge stage, on the
local provider with a temp bare origin. No network.
"""
import contextlib
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
TMP = Path(tempfile.mkdtemp(prefix="flow3-mgtest-")).resolve()
os.environ["FLOW3_HOME"] = str(TMP / "home")

from _test_harness import Harness  # noqa: E402

from scripts.flow3 import config, context, gitops, heal, mergegate, runner  # noqa: E402
from scripts.flow3.paths import repo_dir  # noqa: E402
from scripts.flow3.state import FlowDir  # noqa: E402
from scripts.flow3.types import FlowStop  # noqa: E402
from scripts.workflow.fsutil import nonblocking_flock  # noqa: E402

h = Harness("flow3 mergegate")
heal._sleep = lambda s: None  # type: ignore[assignment]
runner._sleep = lambda s: None  # type: ignore[assignment]
mergegate._sleep = lambda s: None  # type: ignore[assignment]


def sh(args: List[str], cwd: Path) -> str:
    return subprocess.run(args, cwd=str(cwd), check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True).stdout.strip()


CHECK_PY = '''
import sys
d, mode = sys.argv[1], sys.argv[2]
open(d + "/runs", "a").write(mode + "\\n")
'''


class Box:
    def __init__(self, name: str, merge_yaml: Optional[List[str]] = None) -> None:
        self.name = name
        self.ctl = TMP / (name + "-ctl")
        self.ctl.mkdir()
        (self.ctl / "check.py").write_text(CHECK_PY)
        yml = ["provider: local", "check:",
               "  fast: 'python3 %s/check.py %s fast'" % (self.ctl, self.ctl),
               "  full: 'python3 %s/check.py %s full'" % (self.ctl, self.ctl)]
        if merge_yaml:
            yml += ["merge:"] + ["  " + ln for ln in merge_yaml]
        self.origin = TMP / (name + "-origin.git")
        self.clone = TMP / name
        sh(["git", "init", "--bare", "-q", "-b", "main", str(self.origin)], TMP)
        sh(["git", "clone", "-q", str(self.origin), str(self.clone)], TMP)
        for k, v in (("user.name", "t"), ("user.email", "t@example.invalid")):
            sh(["git", "config", k, v], self.clone)
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
        (self.wt / "x.txt").write_text("x\n")
        gitops.commit_all(self.wt, "Issue #1: Add feature X")
        gitops.push(self.wt, self.branch)
        self.ctx = context.build(self.flow)
        pr = self.ctx.provider.open_pr(self.branch, "main", "Add feature X", "body\n\nCloses #1")
        self.flow.set("pr", pr.number)
        self.flow.set_data("ship", {"pr": pr.number, "url": pr.url, "title": "Add feature X",
                                    "head": gitops.head_sha(self.wt)})
        self.other = TMP / (name + "-other")
        self.moves = 0

    def move_base(self) -> None:
        """Someone else lands a commit on origin/main."""
        if self.moves == 0:
            sh(["git", "clone", "-q", str(self.origin), str(self.other)], TMP)
            for k, v in (("user.name", "t"), ("user.email", "t@example.invalid")):
                sh(["git", "config", k, v], self.other)
        self.moves += 1
        sh(["git", "fetch", "-q", "origin"], self.other)
        sh(["git", "checkout", "-q", "-B", "main", "origin/main"], self.other)
        (self.other / ("moved%d.txt" % self.moves)).write_text("m\n")
        sh(["git", "add", "-A"], self.other)
        sh(["git", "commit", "-q", "-m", "base %d" % self.moves], self.other)
        sh(["git", "push", "-q", "origin", "main"], self.other)

    def runs(self, mode: str) -> int:
        p = self.ctl / "runs"
        return p.read_text().split().count(mode) if p.exists() else 0

    def main_files(self) -> List[str]:
        return sh(["git", "ls-tree", "-r", "--name-only", "main"], self.origin).split()

    def journal(self) -> str:
        p = self.flow.path / "journal.md"
        return p.read_text() if p.exists() else ""


def stops(fn: Any) -> Optional[FlowStop]:
    try:
        fn()
    except FlowStop as exc:
        return exc
    return None


# -- 1. screen: every section from seeded records, and rendering with nothing recorded
b = Box("g1")
b.flow.set_data("plan", {"plan": {"goal": "g", "units": [{"id": "u1", "title": "Write the X module"}]}})
b.flow.set_data("review", {"rounds": [{"findings": [{"id": "F1", "title": "Null deref"},
                                                    {"id": "F2", "title": "Slow loop"}],
                                       "triage": {"doing_it": [{"finding_id": "F1"}],
                                                  "deferred": [{"finding_id": "F2", "reason": "not this ticket"}]},
                                       "measurements": [{"finding_id": "F2", "command": "bench.sh", "ran": False}]}]})
b.flow.set_data("fix", {"rounds": [{"fixed": ["F1"], "open": []}]})
b.flow.set_data("implement", {"test_edits": [{"file": "tests/a.py", "verdict": "tightened",
                                              "reason": "stricter assert", "applied": True}]})
b.flow.event("check", mode="full", ok=True, tree="abcdef0123456789")
(b.flow.path / "decisions").mkdir(parents=True, exist_ok=True)
(b.flow.path / "decisions" / "q1.json").write_text(json.dumps({
    "question_id": "q1", "card": {"decision": "Use retries?"},
    "answer": {"source": "principle", "principle_id": "P3", "text": "yes", "reason": "cheap"}}))
text = mergegate.screen(b.ctx)
for needle, label in [("local://g1-origin/pr/1", "PR url"), ("Write the X module", "unit title"),
                      ("x.txt", "diff stat"), ("full check passed", "check line"),
                      ("F1 (Null deref)", "fixed finding with title"), ("not this ticket", "deferred reason"),
                      ("bench.sh", "measurement not run"), ("tests/a.py", "test edit"),
                      ("merge  |  send back with notes  |  stop", "the three choices"),
                      ("Cost:", "cost line")]:
    h.test_result("screen shows " + label, needle in text, needle)
h.test_result("screen shows the principle decision and how to overrule it",
              "P3" in text and "Use retries?" in text and "overrule" in text)

b = Box("g1b")
b.flow.set_data("review", {"rounds": [
    {"findings": [{"id": "F1", "title": "Null deref", "bucket": "doing_it"}]},
    {"last_round": True, "answers": {"L2": {"option_id": "fix", "source": "human", "text": "fix it"}},
     "findings": [{"id": "L1", "title": "Late one", "bucket": "doing_it"},
                  {"id": "L2", "title": "Asked one", "bucket": "doing_it"},
                  {"id": "L3", "title": "Put off", "bucket": "deferred"}]}]})
b.flow.set_data("fix", {"rounds": [{"fixed": ["F1"], "open": []}]})
late = mergegate.screen(b.ctx)
h.test_result("gate: findings of the last review round are listed open, a human `fix` answer is marked",
              "Open (2), not fixed" in late and "Fixed: F1" in late and "L1 (Late one)" in late and "L3 (Put off)" not in late.split("Deferred")[0]
              and "L2 (Asked one): YOU ANSWERED `fix`" in late and "Send back with notes" in late
              and "- F1" not in late, late)

bare = mergegate.screen(b.ctx)
h.test_result("screen renders with no records", "MERGE GATE" in bare and "(nothing recorded)" in bare)
b.ctx.flow.set_data("ship", {})
try:
    again = mergegate.screen(b.ctx)
    h.test_result("screen renders with no ship record", "(none recorded)" in again)
except Exception as exc:  # noqa: BLE001
    h.test_result("screen renders with no ship record", False, repr(exc))

# -- 2. the happy path
b = Box("g2")
res = mergegate.run(b.ctx)
h.test_result("merge returns done", res.kind == "done")
h.test_result("squash landed x.txt on origin main", "x.txt" in b.main_files(), str(b.main_files()))
h.test_result("worktree removed", not b.wt.exists())
m = b.flow.data("merge")
h.test_result("data.merge records attempts and merged", m.get("merged") is True and m.get("attempts") == 1
              and m.get("warnings") == [], str(m))
h.test_result("merged_sha is origin main", m.get("merged_sha") == sh(["git", "rev-parse", "main"], b.origin))
h.test_result("local branch deleted", subprocess.run(
    ["git", "rev-parse", "--verify", "--quiet", "refs/heads/" + b.branch], cwd=str(b.clone),
    stdout=subprocess.PIPE).returncode != 0)
h.test_result("the PR is merged and the issue closed", b.ctx.provider.pr_state(1) == "merged")
h.test_result("the lock info file is gone", not (repo_dir(b.ctx.repo) / "merge.lock.json").exists())
h.test_result("one full check ran", b.runs("full") == 1, str(b.runs("full")))

# -- 3. the exact tree already passed the full check: it is reused
b = Box("g3")
heal.check(b.ctx, "full")
before = b.runs("full")
mergegate.run(b.ctx)
h.test_result("full check reused for the same tree", b.runs("full") == before == 1, str(b.runs("full")))
h.test_result("a check_reused event was recorded", any(e.get("kind") == "check_reused" for e in b.flow.events()))

# -- 4. lock: wait for a live holder, then proceed
b = Box("g4")
lock_path = repo_dir(b.ctx.repo) / "merge.lock"
lock_path.parent.mkdir(parents=True, exist_ok=True)
stack = contextlib.ExitStack()
stack.enter_context(nonblocking_flock(lock_path))
waits: List[float] = []


def release_after_wait(secs: float) -> None:
    waits.append(secs)
    stack.close()


mergegate._sleep = release_after_wait  # type: ignore[assignment]
mergegate.run(b.ctx)
mergegate._sleep = lambda s: None  # type: ignore[assignment]
h.test_result("waited once for the holder, then merged", waits == [mergegate.LOCK_POLL_SECS]
              and "x.txt" in b.main_files(), str(waits))
h.test_result("the wait was journaled", "Waiting for the merge lock" in b.journal())

# -- 5. lock: bounded wait ends in merge-contended
b = Box("g5")
lock_path = repo_dir(b.ctx.repo) / "merge.lock"
holder = contextlib.ExitStack()
holder.enter_context(nonblocking_flock(lock_path))
old_wait = mergegate.LOCK_WAIT_SECS
mergegate.LOCK_WAIT_SECS = 3 * mergegate.LOCK_POLL_SECS
stop = stops(lambda: mergegate.run(b.ctx))
mergegate.LOCK_WAIT_SECS = old_wait
holder.close()
h.test_result("contended lock stops merge-contended", stop is not None and stop.cls == "merge-contended", str(stop))
h.test_result("nothing merged while contended", "x.txt" not in b.main_files())

# -- 6. lock: a dead holder's info is taken over, with a journal line
b = Box("g6")
dead = subprocess.Popen([sys.executable, "-c", "pass"])
dead.wait()
info = repo_dir(b.ctx.repo) / "merge.lock.json"
info.parent.mkdir(parents=True, exist_ok=True)
info.write_text(json.dumps({"pid": dead.pid, "flow": "old-flow", "since": "2026-01-01T00:00:00+00:00"}))
mergegate.run(b.ctx)
h.test_result("takeover is journaled", "Took over the merge lock from a dead process" in b.journal()
              and "old-flow" in b.journal())
h.test_result("merge proceeded after takeover", "x.txt" in b.main_files())

# -- 7. base moved during the check: merged in, re-checked, then merged
b = Box("g7")
real_moved = mergegate._base_moved
state = {"n": 0}


def moved_once(ctx: Any) -> bool:
    state["n"] += 1
    if state["n"] == 1:
        b.move_base()
    return real_moved(ctx)


mergegate._base_moved = moved_once  # type: ignore[assignment]
mergegate.run(b.ctx)
mergegate._base_moved = real_moved  # type: ignore[assignment]
h.test_result("two attempts", b.flow.data("merge").get("attempts") == 2, str(b.flow.data("merge")))
h.test_result("full check ran again on the new tree", b.runs("full") == 2, str(b.runs("full")))
h.test_result("main has both the moved file and ours", {"x.txt", "moved1.txt"} <= set(b.main_files()),
              str(b.main_files()))

# -- 8. base keeps moving: stop merge-contended after MAX_MERGE_ATTEMPTS, nothing merged
b = Box("g8")


def always_moved(ctx: Any) -> bool:
    b.move_base()
    return real_moved(ctx)


mergegate._base_moved = always_moved  # type: ignore[assignment]
stop = stops(lambda: mergegate.run(b.ctx))
mergegate._base_moved = real_moved  # type: ignore[assignment]
h.test_result("keeps moving stops merge-contended", stop is not None and stop.cls == "merge-contended", str(stop))
h.test_result("tried MAX_MERGE_ATTEMPTS times", b.runs("full") == config.MAX_MERGE_ATTEMPTS, str(b.runs("full")))
h.test_result("our file never landed", "x.txt" not in b.main_files())
h.test_result("the lock was released after the stop", not (repo_dir(b.ctx.repo) / "merge.lock.json").exists())
try:
    with nonblocking_flock(repo_dir(b.ctx.repo) / "merge.lock"):
        again = True
except Exception:  # noqa: BLE001
    again = False
h.test_result("the lock can be taken again", again)

# -- 9. pre_merge failure stops and nothing merges
b = Box("g9", ["pre_merge: [\"echo checking-deploy-gate; exit 3\"]"])
stop = stops(lambda: mergegate.run(b.ctx))
h.test_result("pre_merge failure stops with class check", stop is not None and stop.cls == "check", str(stop))
h.test_result("nothing merged after a failed pre_merge", "x.txt" not in b.main_files()
              and b.ctx.provider.pr_state(1) == "open")

# -- 10. method: command runs under the lock with the env, and the provider must still confirm
envfile = TMP / "g10-env.json"
script = TMP / "g10-merge.py"
script.write_text("import json, os, sys\n"
                  "keys = ['FLOW3_PR', 'FLOW3_BRANCH', 'FLOW3_BASE', 'FLOW3_HEAD']\n"
                  "open(sys.argv[1], 'w').write(json.dumps({k: os.environ.get(k) for k in keys}))\n"
                  "lock = os.path.join(sys.argv[2], 'merge.lock.json')\n"
                  "open(sys.argv[1] + '.lockseen', 'w').write(str(os.path.exists(lock)))\n")
b = Box("g10", ["method: command", "command: \"python3 %s %s %s {pr}\"" % (script, envfile, repo_dir("g10"))])
stop = stops(lambda: mergegate.run(b.ctx))
seen = json.loads(envfile.read_text()) if envfile.exists() else {}
h.test_result("command got the FLOW3_ env", seen.get("FLOW3_PR") == "1" and seen.get("FLOW3_BRANCH") == b.branch
              and seen.get("FLOW3_BASE") == "main" and len(seen.get("FLOW3_HEAD") or "") == 40, str(seen))
h.test_result("command ran while the lock info existed",
              Path(str(envfile) + ".lockseen").read_text() == "True" if Path(str(envfile) + ".lockseen").exists()
              else False)
h.test_result("exit 0 from the command is not enough: provider says open, so stop merge",
              stop is not None and stop.cls == "merge", str(stop))

# the same command, but this time it really merges (it marks the PR merged through the provider's record)
pr_file = b.origin / "flow3" / "prs" / "1.json"
b = Box("g10b", ["method: command", "command: \"true\""])
orig = b.ctx.provider.merge_pr


def _command_merges() -> None:
    b.ctx.provider.merge_pr(1, gitops.head_sha(b.wt), "t")


run_cmd = mergegate._run_command


def merging_command(ctx: Any, command: str, cwd: Path, mode: str, env: Optional[Dict[str, str]] = None) -> Any:
    res = run_cmd(ctx, command, cwd, mode, env=env)
    if mode == "merge":
        _command_merges()
    return res


mergegate._run_command = merging_command  # type: ignore[assignment]
res = mergegate.run(b.ctx)
mergegate._run_command = run_cmd  # type: ignore[assignment]
h.test_result("command method merges once the provider confirms", res.kind == "done"
              and b.flow.data("merge").get("merged") is True)

# a failing merge command is a merge stop
b = Box("g10c", ["method: command", "command: \"exit 4\""])
stop = stops(lambda: mergegate.run(b.ctx))
h.test_result("non-zero merge command stops merge", stop is not None and stop.cls == "merge", str(stop))

# -- 11. the provider never reports merged: stop (after retrying)
b = Box("g11")
b.ctx.provider.pr_state = lambda n: "open"  # type: ignore[assignment]
polls = {"n": 0}
mergegate._sleep = lambda s: polls.__setitem__("n", polls["n"] + 1)  # type: ignore[assignment]
stop = stops(lambda: mergegate.run(b.ctx))
mergegate._sleep = lambda s: None  # type: ignore[assignment]
h.test_result("unconfirmed merge stops with class merge", stop is not None and stop.cls == "merge", str(stop))
h.test_result("it polled before giving up", polls["n"] == mergegate.CONFIRM_TRIES - 1, str(polls))

# -- 12. post-merge problems are warnings
b = Box("g12", ["post_merge: [\"exit 1\"]"])
real_remove = gitops.remove_worktree


def broken_remove(repo_root: Path, path: Path) -> None:
    raise RuntimeError("worktree is busy")


gitops.remove_worktree = broken_remove  # type: ignore[assignment]
res = mergegate.run(b.ctx)
gitops.remove_worktree = real_remove  # type: ignore[assignment]
warns = res.info.get("warnings") if isinstance(res.info, dict) else None
h.test_result("stage still returns done", res.kind == "done")
h.test_result("two warnings returned", isinstance(warns, list) and len(warns) >= 2, str(warns))
h.test_result("warnings name the failures", isinstance(warns, list)
              and any("post-merge" in w for w in warns) and any("worktree" in w for w in warns), str(warns))
h.test_result("data.merge keeps the warnings and merged is true",
              b.flow.data("merge").get("merged") is True and b.flow.data("merge").get("warnings") == warns)
h.test_result("the merge itself landed", "x.txt" in b.main_files())

h.summarize_and_exit()
