#!/usr/bin/env python3
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="flow3-test-")).resolve()
os.environ["FLOW3_HOME"] = str(TMP / "home")

from _test_harness import Harness  # noqa: E402


def sh(args, cwd):
    return subprocess.run(args, cwd=str(cwd), check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True).stdout.strip()


def make_repo(name, files=None):
    """A bare origin plus a clone with main pushed. Returns (clone, origin)."""
    origin = TMP / (name + "-origin.git")
    clone = TMP / name
    sh(["git", "init", "--bare", "-q", "-b", "main", str(origin)], TMP)
    sh(["git", "clone", "-q", str(origin), str(clone)], TMP)
    sh(["git", "config", "user.name", "t"], clone)
    sh(["git", "config", "user.email", "t@example.invalid"], clone)
    for rel, text in (files or {"README.md": "hi\n"}).items():
        p = clone / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    sh(["git", "add", "-A"], clone)
    sh(["git", "commit", "-q", "-m", "init"], clone)
    sh(["git", "push", "-q", "origin", "main"], clone)
    sh(["git", "remote", "set-head", "origin", "main"], clone)
    return clone, origin


def commit_push(clone, rel, text, msg, branch="main"):
    p = clone / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    sh(["git", "add", "-A"], clone)
    sh(["git", "commit", "-q", "-m", msg], clone)
    sh(["git", "push", "-q", "origin", "HEAD:" + branch], clone)
    return sh(["git", "rev-parse", "HEAD"], clone)

from scripts.flow3 import provider, repoconfig  # noqa: E402
from scripts.flow3.provider import BaseMoved, ProviderError  # noqa: E402

h = Harness("Flow v3 provider")
CFG = repoconfig.parse("provider: local\ncheck: {full: 'true'}\n")


def rev(origin, ref):
    return sh(["git", "rev-parse", ref], origin)


def count(origin, ref="main"):
    return int(sh(["git", "rev-list", "--count", ref], origin))


def feature(clone, branch, rel, text):
    sh(["git", "checkout", "-q", "-B", branch, "origin/main"], clone)
    (clone / rel).parent.mkdir(parents=True, exist_ok=True)
    (clone / rel).write_text(text)
    sh(["git", "add", "-A"], clone)
    sh(["git", "commit", "-q", "-m", "work on " + branch], clone)
    sh(["git", "commit", "-q", "--allow-empty", "-m", "second commit"], clone)
    sh(["git", "push", "-q", "origin", branch], clone)
    return sh(["git", "rev-parse", "HEAD"], clone)


clone, origin = make_repo("p1", {"base.txt": "base\n"})
prov = provider.for_repo(clone, CFG)
h.test_result("for_repo local gives LocalProvider", isinstance(prov, provider.LocalProvider) and prov.name == "local")
try:
    provider.for_repo(clone, repoconfig.parse("provider: github\ncheck: {full: x}\n"))
    ok = False
except ProviderError as exc:
    ok = "not built yet" in str(exc)
h.test_result("for_repo github raises ProviderError", ok)

# issues
n1 = prov.create_issue("First thing", "Do the first thing.\nMore.")
n2 = prov.create_issue("Second thing", "Needs the first.", depends_on=[n1])
h.test_result("issue numbers allocate in order", (n1, n2) == (1, 2))
i2 = prov.get_issue(n2)
h.test_result("issue parsed: title, deps, state", i2.title == "Second thing" and i2.depends_on == (1,)
              and i2.state == "open" and "Needs the first." in i2.body, str(i2))
(origin / "flow3" / "issues" / "9.md").write_text("# Hand written\n\nSome text\nDepends on: #4, #5\n")
i9 = prov.get_issue(9)
h.test_result("hand-written issue with Depends on", i9.title == "Hand written" and i9.depends_on == (4, 5), str(i9))
(origin / "flow3" / "issues" / "9.closed").write_text("")
h.test_result("closed marker gives state closed", prov.get_issue(9).state == "closed")
try:
    prov.get_issue(404)
    ok = False
except ProviderError:
    ok = True
h.test_result("missing issue raises ProviderError", ok)

# PRs
try:
    prov.open_pr("nope", "main", "t", "b")
    ok = False
except ProviderError:
    ok = True
h.test_result("open_pr requires the branch on the remote", ok)
head = feature(clone, "flow/1-first", "feat.txt", "feat\n")
pr = prov.open_pr("flow/1-first", "main", "First thing", "Closes #1")
h.test_result("open_pr record", pr.number == 1 and pr.url == "local://p1-origin/pr/1" and pr.state == "open"
              and pr.branch == "flow/1-first" and pr.base == "main", str(pr))
h.test_result("find_pr finds it", prov.find_pr("flow/1-first") == pr and prov.find_pr("other") is None)
h.test_result("pr_state open", prov.pr_state(1) == "open")
head2 = feature(clone, "flow/2-second", "two.txt", "two\n")
pr2 = prov.open_pr("flow/2-second", "main", "Second", "no close line")
h.test_result("PR numbers allocate in order", pr2.number == 2)

# stale head sha
before = rev(origin, "main")
try:
    prov.merge_pr(1, "0" * 40, "First thing")
    ok = False
except BaseMoved:
    ok = False
except ProviderError as exc:
    ok = "head moved" in str(exc)
h.test_result("stale head sha raises ProviderError (not BaseMoved)", ok)
h.test_result("stale head leaves base untouched", rev(origin, "main") == before)

# squash merge lands exactly one commit
prov.merge_pr(1, head, "First thing (#1)")
h.test_result("squash adds exactly one commit", count(origin) == 2, str(count(origin)))
h.test_result("squash subject is the title", sh(["git", "log", "-1", "--format=%s", "main"], origin) == "First thing (#1)")
h.test_result("squash carries the change", "feat.txt" in sh(["git", "ls-tree", "--name-only", "main"], origin))
h.test_result("PR marked merged", prov.pr_state(1) == "merged" and prov.find_pr("flow/1-first").state == "merged")
h.test_result("Closes #1 closes the issue", prov.get_issue(1).state == "closed" and prov.get_issue(2).state == "open")
h.test_result("remote branch deleted", sh(["git", "branch", "--list", "flow/1-first"], origin) == "")
try:
    prov.merge_pr(1, head, "again")
    ok = False
except ProviderError:
    ok = True
h.test_result("merging a merged PR raises ProviderError", ok)

# base moved with a conflict
sh(["git", "checkout", "-q", "main"], clone)
sh(["git", "pull", "-q", "origin", "main"], clone)
c_head = feature(clone, "flow/3-conflict", "shared.txt", "from branch\n")
pr3 = prov.open_pr("flow/3-conflict", "main", "Conflicting", "")
sh(["git", "checkout", "-q", "main"], clone)
sh(["git", "pull", "-q", "origin", "main"], clone)
base_after = commit_push(clone, "shared.txt", "from main\n", "main also edits shared")
try:
    prov.merge_pr(3, c_head, "Conflicting")
    ok = False
except BaseMoved:
    ok = True
h.test_result("conflict with moved base raises BaseMoved", ok)
h.test_result("BaseMoved leaves base untouched", rev(origin, "main") == base_after)
h.test_result("PR stays open after BaseMoved", prov.pr_state(3) == "open")

# non-conflicting moved base still merges (squash happens on current base)
h2 = feature(clone, "flow/4-ok", "four.txt", "four\n")
pr4 = prov.open_pr("flow/4-ok", "main", "Four", "Closes #2")
sh(["git", "checkout", "-q", "main"], clone)
sh(["git", "pull", "-q", "origin", "main"], clone)
commit_push(clone, "unrelated.txt", "u\n", "unrelated")
n_before = count(origin)
prov.merge_pr(4, h2, "Four")
h.test_result("moved base without conflict merges one commit", count(origin) == n_before + 1)
h.test_result("Closes #2 closed issue 2", prov.get_issue(2).state == "closed")

# CLI
def cli(*args):
    return subprocess.run([sys.executable, "-m", "scripts.flow3.provider"] + list(args), cwd=str(ROOT),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)


h5 = feature(clone, "flow/5-cli", "five.txt", "five\n")
pr5 = prov.open_pr("flow/5-cli", "main", "Five", "Closes #3")
r = cli("new-issue", "--repo-root", str(clone), "--title", "CLI issue", "--body", "text", "--depends", "1,2")
num = int(r.stdout.strip()) if r.returncode == 0 else -1
h.test_result("CLI new-issue prints the number", r.returncode == 0 and num == prov.get_issue(num).number
              and prov.get_issue(num).depends_on == (1, 2), r.stderr)
r = cli("merge-local", "--repo-root", str(clone), "--pr", "5", "--head", "1" * 40, "--title", "Five")
h.test_result("CLI merge-local exit 1 on a moved head", r.returncode == 1, "%d %s" % (r.returncode, r.stderr))
r = cli("merge-local", "--repo-root", str(clone), "--pr", "5", "--head", h5, "--title", "Five")
h.test_result("CLI merge-local exit 0 on success", r.returncode == 0 and prov.pr_state(5) == "merged", r.stderr)
r = cli("merge-local", "--repo-root", str(clone), "--pr", "3", "--head", c_head, "--title", "Conflicting")
h.test_result("CLI merge-local exit 3 on BaseMoved", r.returncode == 3, "%d %s" % (r.returncode, r.stderr))

h.summarize_and_exit()
