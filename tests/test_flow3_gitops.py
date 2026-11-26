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

from scripts.flow3 import gitops  # noqa: E402

h = Harness("Flow v3 gitops")
clone, origin = make_repo("g1", {"a.txt": "one\n", "dir/b.txt": "b\n"})

# worktree create / reuse / remove
wt = gitops.worktree_path_for(clone, 7, "thing")
h.test_result("worktree_path_for is a sibling", wt == clone.parent / "7-thing", str(wt))
gitops.create_worktree(clone, wt, "flow/7-thing", "origin/main")
h.test_result("create_worktree makes the dir", (wt / "a.txt").exists())
h.test_result("worktree is on the branch", gitops.git(["branch", "--show-current"], wt) == "flow/7-thing")
gitops.create_worktree(clone, wt, "flow/7-thing", "origin/main")
h.test_result("create_worktree is a no-op the second time", True)
gitops.remove_worktree(clone, wt)
h.test_result("remove_worktree deletes the dir", not wt.exists())
gitops.create_worktree(clone, wt, "flow/7-thing", "origin/main")
h.test_result("branch is reused after removal", (wt / "a.txt").exists())
gitops.remove_worktree(clone, wt)

# changed_files: untracked nested, rename
(clone / "new" / "deep").mkdir(parents=True)
(clone / "new" / "deep" / "x.txt").write_text("x")
sh(["git", "mv", "a.txt", "renamed.txt"], clone)
changed = gitops.changed_files(clone)
h.test_result("changed_files sees untracked nested file", "new/deep/x.txt" in changed, str(changed))
h.test_result("changed_files sees the rename (new and old)",
              "renamed.txt" in changed and "a.txt" in changed, str(changed))

# revert_paths
(clone / "dir" / "b.txt").write_text("changed\n")
gitops.revert_paths(clone, ["dir/b.txt", "new"])
h.test_result("revert_paths restores a tracked file", (clone / "dir" / "b.txt").read_text() == "b\n")
h.test_result("revert_paths deletes untracked files", not (clone / "new").exists())
sh(["git", "mv", "renamed.txt", "a.txt"], clone)

# commit_all
h.test_result("commit_all returns None when clean", gitops.commit_all(clone, "nothing") is None)
(clone / "c.txt").write_text("c")
(clone / "d.txt").write_text("d")
sha = gitops.commit_all(clone, "only c", paths=["c.txt"])
h.test_result("commit_all with paths commits only those",
              sha == gitops.head_sha(clone) and gitops.changed_files(clone) == ["d.txt"])
sha2 = gitops.commit_all(clone, "rest")
h.test_result("commit_all stages everything", sha2 is not None and gitops.changed_files(clone) == [])
h.test_result("tree_id is a tree", len(gitops.tree_id(clone)) == 40)

# commit_all supplies an identity when the repo has none
bare_id = TMP / "noid"
sh(["git", "init", "-q", "-b", "main", str(bare_id)], TMP)
(bare_id / "f").write_text("f")
env_home = TMP / "emptyhome"
env_home.mkdir()
os.environ["HOME"] = str(env_home)
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
os.environ["XDG_CONFIG_HOME"] = str(env_home)
try:
    h.test_result("commit_all works without identity", gitops.commit_all(bare_id, "m") is not None)
except gitops.GitError as exc:
    h.test_result("commit_all works without identity", False, str(exc))

# is_behind / merge_in clean
other, _ = make_repo("g2", {"f.txt": "1\n"})
sh(["git", "checkout", "-q", "-b", "feat"], other)
commit_push(other, "feat.txt", "feat\n", "feat work", branch="feat")
h.test_result("not behind at first", not gitops.is_behind(other, "origin/main"))
sh(["git", "checkout", "-q", "main"], other)
commit_push(other, "main2.txt", "m\n", "main moves")
sh(["git", "checkout", "-q", "feat"], other)
sh(["git", "fetch", "-q", "origin"], other)
h.test_result("is_behind after base moved", gitops.is_behind(other, "origin/main"))
clean, files = gitops.merge_in(other, "origin/main")
h.test_result("merge_in clean", clean and files == [] and not gitops.is_behind(other, "origin/main"))

# merge_in conflict then abort
sh(["git", "checkout", "-q", "main"], other)
commit_push(other, "f.txt", "main side\n", "main edits f")
sh(["git", "checkout", "-q", "-b", "side", "HEAD~1"], other)
(other / "f.txt").write_text("side edit\n")
gitops.commit_all(other, "side edits f")
sh(["git", "fetch", "-q", "origin"], other)
clean, files = gitops.merge_in(other, "origin/main")
h.test_result("merge_in reports conflict", (not clean) and files == ["f.txt"], str((clean, files)))
gitops.abort_merge(other)
h.test_result("abort_merge restores a clean tree", gitops.changed_files(other) == [])

# diff helpers
h.test_result("diff_names lists the branch's change", gitops.diff_names(other, "origin/main") == ["f.txt"])
h.test_result("diff has the edit", "side edit" in gitops.diff(other, "origin/main"))

# push never forces, and a diverged push is refused
sh(["git", "checkout", "-q", "feat"], other)
(other / "p.txt").write_text("p")
gitops.commit_all(other, "p")
gitops.push(other, "feat")
h.test_result("push works", gitops.git(["rev-parse", "refs/heads/feat"], TMP / "g2-origin.git") == gitops.head_sha(other))
sh(["git", "reset", "-q", "--hard", "HEAD~1"], other)
(other / "q.txt").write_text("q")
gitops.commit_all(other, "q")
try:
    gitops.push(other, "feat")
    refused = False
except gitops.GitError:
    refused = True
h.test_result("diverged push is refused (no force)", refused)
seen = []
real_git = gitops.git
gitops.git = lambda args, cwd, check=True: seen.append(list(args)) or ""
try:
    gitops.push(other, "feat")
finally:
    gitops.git = real_git
h.test_result("push is a plain refspec push",
              seen == [["push", "origin", "feat:feat"]]
              and not any(a.startswith("--force") or a.startswith("+") for a in seen[0]), str(seen))

# GitError carries stderr
try:
    gitops.git(["rev-parse", "--verify", "nope"], other)
    msg = ""
except gitops.GitError as exc:
    msg = str(exc)
h.test_result("GitError raised on failure", "nope" in msg or "fatal" in msg or "exit" in msg, msg)
h.test_result("check=False returns output", gitops.git(["rev-parse", "--verify", "nope"], other, check=False) is not None)

# match_any
TABLE = [
    ("docs/a/b.md", ["docs/"], True), ("docs/a/b.md", ["docs"], True), ("docs", ["docs"], True),
    ("docsx/a", ["docs"], False), ("a.lock", ["a.lock"], True), ("src/a/b.py", ["src/**/*.py"], True),
    ("src/a.py", ["**/*.py"], True), ("src/a.txt", ["*.py"], False), ("x", [], False),
    ("db/migrations/001.sql", ["db/migrations/**"], True), ("other", ["db/", "x"], False),
]
for path, pats, want in TABLE:
    h.test_result("match_any %r %r" % (path, pats), gitops.match_any(path, pats) == want)

h.summarize_and_exit()
