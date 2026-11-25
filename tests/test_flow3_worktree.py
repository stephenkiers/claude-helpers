#!/usr/bin/env python3
"""Tests for scripts/flow3/steps/worktree.py. No real model is called: the fake claude binary is the only one."""

import json
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
os.environ["HOME"] = str(TMP / "userhome")
os.environ["FLOW3_CLAUDE_BIN"] = str(ROOT / "tests" / "flow3_fake_claude.py")
LOG = TMP / "fake-log.jsonl"
os.environ["FLOW3_FAKE_LOG"] = str(LOG)

from _test_harness import Harness  # noqa: E402

from scripts.flow3 import context, heal, repoconfig, runner  # noqa: E402
from scripts.flow3.state import FlowDir  # noqa: E402
from scripts.flow3.steps import worktree  # noqa: E402
from scripts.flow3.types import FlowStop  # noqa: E402

heal._sleep = lambda s: None  # type: ignore[assignment]
runner._sleep = lambda s: None  # type: ignore[assignment]


def sh(args, cwd):
    return subprocess.run(args, cwd=str(cwd), check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True).stdout.strip()


_COUNTER = [100]


def make_ctx(check="true", install=None, body="The body.\n", extra_issues=None):
    """A sandbox repo (bare origin + clone) with a local-provider issue, a flow and its Ctx."""
    _COUNTER[0] += 1
    issue = _COUNTER[0]
    origin = TMP / ("o%d.git" % issue)
    clone = TMP / ("c%d" % issue)
    sh(["git", "init", "--bare", "-q", "-b", "main", str(origin)], TMP)
    sh(["git", "clone", "-q", str(origin), str(clone)], TMP)
    sh(["git", "config", "user.name", "t"], clone)
    sh(["git", "config", "user.email", "t@example.invalid"], clone)
    (clone / ".claude").mkdir()
    lines = ["provider: local"]
    if install:
        lines.append("install: '%s'" % install)
    lines.append("check: {full: '%s'}" % check)
    (clone / ".claude" / "flow.yaml").write_text("\n".join(lines) + "\n")
    (clone / "README.md").write_text("hi\n")
    sh(["git", "add", "-A"], clone)
    sh(["git", "commit", "-q", "-m", "init"], clone)
    sh(["git", "push", "-q", "origin", "main"], clone)
    issues = origin / "flow3" / "issues"
    issues.mkdir(parents=True)
    (issues / ("%d.md" % issue)).write_text("# Worktree ticket\n\n\n%s" % body)
    for number, closed in (extra_issues or {}).items():
        (issues / ("%d.md" % number)).write_text("# Dependency\n\n\nBody.\n")
        if closed:
            (issues / ("%d.closed" % number)).write_text("")
    flow = FlowDir.create(repoconfig.repo_name(clone), issue, "Worktree ticket", clone)
    return context.build(flow), origin


def invocations():
    if not LOG.exists():
        return []
    return [json.loads(line) for line in LOG.read_text().splitlines() if line.strip()]


def stop_of(ctx):
    try:
        worktree.run(ctx)
    except FlowStop as exc:
        return exc
    return None


h = Harness("Flow v3 worktree stage")

# ---- green base: worktree, branch, state ----------------------------------------------------
if LOG.exists():
    LOG.unlink()
ctx, origin = make_ctx(install="touch installed.marker")
res = worktree.run(ctx)
wt = Path(str(ctx.flow.get("worktree")))
data = ctx.flow.data("worktree")
origin_sha = sh(["git", "rev-parse", "main"], origin)
h.test_result("a green base returns done", res.kind == "done")
h.test_result("the worktree exists on a new branch cut from the origin base",
              wt.is_dir() and sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], wt) == ctx.flow.get("branch")
              and sh(["git", "rev-parse", "HEAD"], wt) == origin_sha)
h.test_result("top-level worktree, branch and base are recorded",
              ctx.flow.get("base") == "main" and str(ctx.flow.get("branch")).startswith("flow3/%d-" % ctx.issue))
h.test_result("data.worktree records base_sha, installed and base_check",
              data == {"base_sha": origin_sha, "installed": True, "base_check": "passed"}, repr(data))
h.test_result("the install command ran in the worktree", (wt / "installed.marker").exists())
h.test_result("the step record and a journal line exist",
              len(list((ctx.flow.path / "steps").glob("*-worktree.md"))) == 1
              and "Worktree ready" in (ctx.flow.path / "journal.md").read_text())
h.test_result("nothing here called a model", invocations() == [])

# ---- re-entry --------------------------------------------------------------------------------
again = worktree.run(ctx)
h.test_result("re-entry after a finished stage does not fail", again.kind == "done"
              and ctx.flow.data("worktree") == data)

# Re-entry after a crash between creating the worktree and finishing the stage.
ctx.flow.set_data("worktree", {})
again = worktree.run(ctx)
h.test_result("re-entry on an existing worktree without a recorded pass does not fail",
              again.kind == "done" and ctx.flow.data("worktree").get("base_check") == "passed"
              and Path(str(ctx.flow.get("worktree"))) == wt)

# ---- no install configured -------------------------------------------------------------------
ctx, _ = make_ctx()
worktree.run(ctx)
h.test_result("no install configured: installed is false", ctx.flow.data("worktree")["installed"] is False)

# ---- failed install ----------------------------------------------------------------------------
ctx, _ = make_ctx(install="exit 3")
exc = stop_of(ctx)
h.test_result("a failed install is an environment stop with the log as evidence",
              exc is not None and exc.cls == "environment" and bool(exc.evidence), repr(exc))
h.test_result("a failed install does not record a pass", ctx.flow.data("worktree").get("base_check") != "passed")

# ---- unmet dependency ----------------------------------------------------------------------------
if LOG.exists():
    LOG.unlink()
ctx, _ = make_ctx(body="Depends on: #9001\n", extra_issues={9001: False})
exc = stop_of(ctx)
h.test_result("an open dependency stops with class dependency", exc is not None and exc.cls == "dependency"
              and "#9001" in exc.reason, repr(exc))
h.test_result("the dependency stop made no worktree and no model call",
              not ctx.flow.get("worktree") and invocations() == [])

ctx, _ = make_ctx(body="Depends on: #9002\n", extra_issues={9002: True})
h.test_result("a closed dependency passes", stop_of(ctx) is None and ctx.flow.data("worktree")["base_check"] == "passed")

ctx, _ = make_ctx(body="Depends on: #9003\n")
exc = stop_of(ctx)
h.test_result("a dependency that cannot be read is a dependency stop", exc is not None and exc.cls == "dependency", repr(exc))

# ---- red base --------------------------------------------------------------------------------
if LOG.exists():
    LOG.unlink()
ctx, _ = make_ctx(check="exit 1")
exc = stop_of(ctx)
h.test_result("a red base stops with base-red", exc is not None and exc.cls == "base-red", repr(exc))
h.test_result("the red-base stop carries the check output and what was tried",
              exc is not None and bool(exc.evidence) and bool(exc.tried))
h.test_result("a red base made no model call", invocations() == [])
h.test_result("a red base records no pass", ctx.flow.data("worktree").get("base_check") != "passed")

h.summarize_and_exit()
