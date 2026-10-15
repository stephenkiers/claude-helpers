#!/usr/bin/env python3
"""
Fixture test for the bare /expert-flow detection block (### No-argument run).

The first ```bash block under "### No-argument run" in commands/expert-flow.md is
extracted at test time, and it runs under bash with real jq and realpath against
temporary fixture directories. Skips cleanly when jq is missing.

The block takes inputs as pre-set variables (HERE, MAIN_WORKTREE, REPO, FLOWS_ROOT,
CUR_BRANCH, IS_LISTED_WORKTREE) and computes:
  NOARG_ROUTE: one of resume, existing, plain, ask
  NOARG_ISSUE: the issue number, or empty
  NOARG_FLOW: the flow directory path, or empty
  NOARG_NONGITHUB: "yes" if the cache is non-GitHub, else empty

Testing Strategy: 19 scenarios as defined in the plan.

Run with: python3 tests/test_expert_flow_noarg.py
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT

COMMAND = REPO_ROOT / "commands" / "expert-flow.md"
SECTION_HEADING = "### No-argument run"


def extract_noarg_block(text):
    """Return the first ```bash block under ### No-argument run, or ''."""
    start = text.find(SECTION_HEADING)
    if start < 0:
        return ""
    # Find the first bash block after the heading
    m = re.search(r"```bash\n(.*?)```", text[start:], re.DOTALL)
    if not m:
        return ""
    return m.group(1)


def make_flow(root, dirname, state):
    """Create FLOWS_ROOT/<dirname>/state.json from a dict."""
    d = root / dirname
    d.mkdir(parents=True, exist_ok=True)
    body = state if isinstance(state, str) else json.dumps(state)
    (d / "state.json").write_text(body)


def make_cache(worktree, cache_data):
    """Create <worktree>/.claude/github-cache.json from a dict."""
    cache_dir = worktree / ".claude"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "github-cache.json").write_text(json.dumps(cache_data))


def make_marker(worktree, flow_path):
    """Create <worktree>/.claude/flow-run marker pointing to flow_path."""
    marker_dir = worktree / ".claude"
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / "flow-run").write_text(str(flow_path))


def run_noarg_block(snippet, main_worktree, here, repo, flows_root, cur_branch, is_listed):
    """Run the detection block with the given variables. Returns (returncode, stdout, stderr)."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HERE": str(here),
        "MAIN_WORKTREE": str(main_worktree),
        "REPO": repo,
        "FLOWS_ROOT": str(flows_root),
        "CUR_BRANCH": cur_branch,
        "IS_LISTED_WORKTREE": "yes" if is_listed else "no",
    }
    result = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, env=env)
    return result.returncode, result.stdout, result.stderr


def parse_output(stdout):
    """Parse one-KEY=value-per-line output into a dict."""
    result = {}
    for line in stdout.split("\n"):
        if "=" in line:
            key, val = line.split("=", 1)
            result[key] = val
    return result


class Fixture:
    """A temp tree: main/, a ticket worktree, and flows/."""

    def __init__(self, tmp, wt_name="302-issue-slug"):
        self.tmp = Path(tmp)
        self.main = self.tmp / "main"
        self.main.mkdir()
        self.here = self.tmp / wt_name
        self.here.mkdir()
        self.flows = self.tmp / "flows"
        self.flows.mkdir()


def main():
    h = Harness("EXPERT-FLOW NO-ARGUMENT RUN DETECTION FIXTURE TEST SUITE")
    t = h.test_result

    if shutil.which("jq") is None:
        print("SKIP: jq is not installed; no-arg detection fixture test not run")
        sys.exit(0)

    cmd_text = COMMAND.read_text() if COMMAND.is_file() else ""
    block = extract_noarg_block(cmd_text)
    t("the no-arg detection block is present in expert-flow.md", bool(block))
    if not block:
        h.summarize_and_exit()

    t("block calls no git (inputs are pre-set)", re.search(r"\bgit\b", block) is None)
    t("block never exits the shell", re.search(r"^\s*exit\b", block, re.MULTILINE) is None)
    t("block has no trailing whitespace", re.search(r"[ \t]+$", block, re.MULTILINE) is None)

    def check(label, route, issue="", flow="", nongithub="", cur_branch="main",
              is_listed=True, repo="owner/repo", setup=None, wt_name="302-issue-slug",
              use_main_as_here=False):
        """Build a fixture, run the block, assert the exact outputs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = Fixture(tmpdir, wt_name)
            expected_flow = setup(fx) if setup else ""
            here = fx.main if use_main_as_here else fx.here
            rc, out, err = run_noarg_block(block, fx.main, here, repo, fx.flows, cur_branch, is_listed)
            p = parse_output(out)
            want_flow = str(expected_flow) if flow == "<flow>" else flow
            got = (p.get("NOARG_ROUTE"), p.get("NOARG_ISSUE"), p.get("NOARG_FLOW"), p.get("NOARG_NONGITHUB"))
            want = (route, issue, want_flow, nongithub)
            t(f"{label}: exact outputs", rc == 0 and got == want,
              f"rc={rc} got={got} want={want} err={err!r}")

    def marker_setup(state, dirname="302-issue-slug-id"):
        def setup(fx):
            flow_dir = fx.flows / dirname
            if state is not None:
                make_flow(fx.flows, dirname, state)
            else:
                flow_dir.mkdir(parents=True)
            make_marker(fx.here, flow_dir)
            return flow_dir
        return setup

    def cache_setup(cache):
        def setup(fx):
            make_cache(fx.here, cache)
            return ""
        return setup

    gh302 = {"branch": "main", "issue": {"number": 302, "url": "https://github.com/owner/repo/issues/302"}}
    handoff = {"issue": 302, "step": "plan", "status": "paused", "live": None, "attempts": {"worktree": 1}}

    print("[Scenario 1] hand-off state -> resume")
    check("scenario 1", "resume", "302", "<flow>", setup=marker_setup(handoff))
    print("[Scenario 2] attempts.plan=1 -> existing")
    check("scenario 2", "existing", "302", "<flow>",
          setup=marker_setup(dict(handoff, attempts={"plan": 1})))
    print("[Scenario 3] step implement -> existing")
    check("scenario 3", "existing", "302", "<flow>",
          setup=marker_setup({"issue": 302, "step": "implement", "status": "paused", "live": None}))
    print("[Scenario 4] live set -> existing")
    check("scenario 4", "existing", "302", "<flow>",
          setup=marker_setup(dict(handoff, live="some-value")))
    print("[Scenario 5] status failed -> existing")
    check("scenario 5", "existing", "302", "<flow>",
          setup=marker_setup({"issue": 302, "status": "failed"}))

    print("[Scenario 6] marker outside FLOWS_ROOT -> ask")

    def outside(fx):
        o = fx.tmp / "outside-flow"
        make_flow(fx.tmp, "outside-flow", handoff)
        make_marker(fx.here, o)
    check("scenario 6", "ask", setup=outside)

    print("[Scenario 6b] marker in sibling dir sharing the FLOWS_ROOT prefix (flows vs flowsx) -> ask")

    def sibling(fx):
        make_flow(fx.tmp / "flowsx", "302-issue-slug-id", handoff)
        make_marker(fx.here, fx.tmp / "flowsx" / "302-issue-slug-id")
    check("scenario 6b", "ask", setup=sibling)

    print("[Scenario 6c] marker is FLOWS_ROOT itself -> ask")

    def is_root(fx):
        (fx.flows / "state.json").write_text(json.dumps(handoff))
        make_marker(fx.here, fx.flows)
    check("scenario 6c", "ask", setup=is_root)

    print("[Scenario 7] marker target without state.json -> ask")
    check("scenario 7", "ask", setup=marker_setup(None))
    print("[Scenario 8] state.issue 30 vs basename 302 -> ask")
    check("scenario 8", "ask", setup=marker_setup({"issue": 30}))

    print("[Scenario 9] valid cache -> plain")
    check("scenario 9", "plain", "302", setup=cache_setup(gh302))
    print("[Scenario 10] cache branch differs -> ask")
    check("scenario 10", "ask", cur_branch="feature", setup=cache_setup(gh302))
    print("[Scenario 11] cache number 303 vs basename 302 -> ask")
    check("scenario 11", "ask", setup=cache_setup(
        {"branch": "main", "issue": {"number": 303, "url": "https://github.com/owner/repo/issues/303"}}))
    print("[Scenario 12] non-numeric number PPS-166 -> ask")
    check("scenario 12", "ask", setup=cache_setup(
        {"branch": "main", "issue": {"number": "PPS-166", "url": "https://github.com/owner/repo/issues/PPS-166"}}))
    print("[Scenario 13] non-GitHub URL -> ask + NOARG_NONGITHUB=yes")
    check("scenario 13", "ask", nongithub="yes", setup=cache_setup(
        {"branch": "main", "issue": {"number": 302, "url": "https://internal.example.com/issues/302"}}))
    print("[Scenario 13b] GitHub URL for a different issue -> ask, not non-GitHub")
    check("scenario 13b", "ask", setup=cache_setup(
        {"branch": "main", "issue": {"number": 302, "url": "https://github.com/owner/repo/issues/999"}}))
    print("[Scenario 14] no marker, no cache -> ask")
    check("scenario 14", "ask")
    print("[Scenario 15] HERE == MAIN_WORKTREE (listed) -> ask")
    check("scenario 15", "ask", use_main_as_here=True)
    print("[Scenario 15b] HERE != MAIN, IS_LISTED_WORKTREE=no, otherwise valid -> ask")
    check("scenario 15b", "ask", is_listed=False, setup=cache_setup(gh302))
    print("[Scenario 16] basename without leading digits -> ask")
    check("scenario 16", "ask", wt_name="pps-166", setup=cache_setup(
        {"branch": "main", "issue": {"number": 166, "url": "https://github.com/owner/repo/issues/166"}}))
    print("[Scenario 17] integer state.issue 302, attempts {} -> resume")
    check("scenario 17", "resume", "302", "<flow>", setup=marker_setup(dict(handoff, attempts={})))
    print("[Scenario 18] string cache number '302' -> plain, normalised")
    check("scenario 18", "plain", "302", setup=cache_setup(
        {"branch": "main", "issue": {"number": "302", "url": "https://github.com/owner/repo/issues/302"}}))
    print("[Scenario 19] local cache (url '') in 5-foo -> ask + NOARG_NONGITHUB=yes")
    check("scenario 19", "ask", nongithub="yes", wt_name="5-foo", setup=cache_setup(
        {"branch": "main", "issue": {"number": 5, "url": ""}}))

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
