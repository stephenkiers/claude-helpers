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

import time  # noqa: E402

from scripts.flow3 import checks, gitops  # noqa: E402

h = Harness("Flow v3 checks")
clone, _ = make_repo("c1")
out = TMP / "logs" / "nested" / "ok.log"

res = checks.run("echo hello; echo oops 1>&2", clone, "fast", 30, out)
h.test_result("pass: ok, exit 0", res.ok and res.exit_code == 0 and not res.timed_out and not res.reused)
h.test_result("output file written (stdout and stderr, parent dirs created)",
              out.exists() and "hello" in out.read_text() and "oops" in out.read_text())
h.test_result("CheckRun fields", res.mode == "fast" and res.command.startswith("echo")
              and res.output_path == str(out) and res.duration_secs >= 0 and "hello" in res.trimmed)
h.test_result("tree taken from the repo", res.tree == gitops.tree_id(clone), res.tree)

res = checks.run("echo broken; exit 3", clone, "full", 30, TMP / "logs" / "fail.log")
h.test_result("fail: not ok, exit code kept", (not res.ok) and res.exit_code == 3 and not res.timed_out)

nogit = TMP / "plain"
nogit.mkdir()
res = checks.run("true", nogit, "install", 30, TMP / "logs" / "plain.log")
h.test_result("tree is empty outside a git repo", res.ok and res.tree == "")

res = checks.run("echo $FLOW3_X", clone, "fast", 30, TMP / "logs" / "env.log", env={"FLOW3_X": "seen"})
h.test_result("env is passed through", "seen" in res.trimmed)

marker = TMP / "child.pid"
t0 = time.monotonic()
res = checks.run("sleep 30 & echo $! > %s; wait" % marker, clone, "fast", 1, TMP / "logs" / "to.log")
h.test_result("timeout: timed_out, not ok, quick", res.timed_out and not res.ok and time.monotonic() - t0 < 10,
              "%.1fs" % (time.monotonic() - t0))
time.sleep(0.3)
pid = int(marker.read_text().strip())
try:
    os.kill(pid, 0)
    alive = True
except ProcessLookupError:
    alive = False
h.test_result("timeout: the child process group is gone", not alive)

# trim
big = "\n".join("line %d ok" % i for i in range(2000))
lines = big.split("\n")
lines[700] = "FAIL tests/test_x.py::test_important - AssertionError"
lines[1200] = "Traceback (most recent call last):"
text = "\n".join(lines)
trimmed = checks.trim(text, 3000)
h.test_result("trim keeps the earlier failure line", "FAIL tests/test_x.py::test_important" in trimmed)
h.test_result("trim keeps the traceback line", "Traceback (most recent call last):" in trimmed)
h.test_result("trim keeps the last line", "line 1999 ok" in trimmed)
h.test_result("trim stays within budget and says it cut", len(trimmed) < 3600 and "cut" in trimmed, str(len(trimmed)))
h.test_result("trim drops boring middle lines", "line 300 ok" not in trimmed)
h.test_result("trim leaves short output alone", checks.trim("short\n") == "short\n")
h.test_result("trim caps very long lines", len(checks.trim("x" * 20000 + "\nFAIL\n", 1000)) < 1500)

# trim puts the failed tests first: a long run with noisy passing tests and four real failures
noisy: list = []
for i in range(3000):
    noisy.append("\x1b[32m(pass)\x1b[0m suite > case %d [0.1ms]" % i)
    if i % 10 == 0:
        noisy.append("error: logged on purpose by passing case %d" % i)
for n in (400, 900, 1500, 2100):
    noisy[n:n] = ["src/hooks/__tests__/thing.test.ts:", "error: expect(received).toBe(expected)", "",
                  "Expected: true", "Received: false", "      at <anonymous> (thing.test.ts:%d:5)" % n,
                  "\x1b[31m(fail)\x1b[0m thing > T%d: does the thing \x1b[2m[12.34ms]\x1b[0m" % n]
long_run = "\n".join(noisy)
cut = checks.trim(long_run, 6000)
h.test_result("trim strips colour codes", "\x1b[" not in cut)
h.test_result("trim keeps every failed test's name",
              all("T%d: does the thing" % n in cut for n in (400, 900, 1500, 2100)), cut[:400])
h.test_result("trim keeps what each failed test expected and received",
              cut.count("Expected: true") == 4 and cut.count("Received: false") == 4, str(cut.count("Expected: true")))
h.test_result("trim keeps the file a failed test is in", "src/hooks/__tests__/thing.test.ts" in cut)
h.test_result("trim stays near its budget on a noisy run", len(cut) < 7000, str(len(cut)))
h.test_result("failures names the failed tests, without timing or colour",
              checks.failures(long_run) == ["thing > T%d: does the thing" % n for n in (400, 900, 1500, 2100)],
              str(checks.failures(long_run)))
h.test_result("failures stops at its limit", len(checks.failures(long_run, limit=2)) == 2)
h.test_result("failures reads other runners' lines",
              checks.failures("FAILED tests/test_a.py::test_one - assert 1 == 2\nnot ok 3 - parses\n--- FAIL: TestX (0.00s)\n")
              == ["tests/test_a.py::test_one - assert 1 == 2", "parses", "TestX (0.00s)"],
              str(checks.failures("FAILED tests/test_a.py::test_one - assert 1 == 2\nnot ok 3 - parses\n--- FAIL: TestX (0.00s)\n")))
h.test_result("failures is empty for a run that names none", checks.failures("exit 1\n") == [])

# record_pass / passed_before
h.test_result("nothing recorded yet", not checks.passed_before("r", "tree1", "just check"))
checks.record_pass("r", "tree1", "just check")
h.test_result("recorded pass is found", checks.passed_before("r", "tree1", "just check"))
h.test_result("keyed by tree", not checks.passed_before("r", "tree2", "just check"))
h.test_result("keyed by command", not checks.passed_before("r", "tree1", "just check-minimum"))
h.test_result("keyed by repo", not checks.passed_before("other", "tree1", "just check"))
checks.record_pass("r", "tree2", "just check")
h.test_result("records accumulate", checks.passed_before("r", "tree1", "just check")
              and checks.passed_before("r", "tree2", "just check"))
checks.record_pass("r", "", "x")
h.test_result("an empty tree is never recorded", not checks.passed_before("r", "", "x"))

h.summarize_and_exit()
