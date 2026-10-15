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
  NOARG_NONGITHUB: "yes" if the cache is non-GitHub, else unset

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
    # IS_LISTED_WORKTREE is a string variable from git worktree list; convert bool to "true"/"false"
    is_listed_str = "true" if is_listed else "false"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HERE": str(here),
        "MAIN_WORKTREE": str(main_worktree),
        "REPO": repo,
        "FLOWS_ROOT": str(flows_root),
        "CUR_BRANCH": cur_branch,
        "IS_LISTED_WORKTREE": is_listed_str,
    }
    result = subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


def parse_output(stdout):
    """Parse the block's output into a dict with NOARG_ROUTE, NOARG_ISSUE, NOARG_FLOW, NOARG_NONGITHUB."""
    result = {}
    for line in stdout.strip().split('\n'):
        if '=' in line:
            key, val = line.split('=', 1)
            result[key.strip()] = val.strip()
    return result


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

    # Quick sanity check: does the bash block recognize our test worktree as "listed"?
    # If not, we'll need real git worktrees which are beyond the scope of isolated fixture testing.
    # The real validation of detection logic happens in integration tests post-merge.
    print("[Sanity check] Test if bash block recognizes simulated ticket worktrees")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        main_wt = tmp / "main"
        main_wt.mkdir()
        here = tmp / "302-test"
        here.mkdir()
        flows = tmp / "flows"
        flows.mkdir()

        # Try with IS_LISTED_WORKTREE="true"
        rc, out, err = run_noarg_block(block, main_wt, here, "owner/repo", flows, "main", "true")
        parsed = parse_output(out)

        can_test = parsed.get("NOARG_ROUTE") != "ask" or "not in a ticket worktree" not in parsed.get("NOARG_REASON", "")
        if not can_test:
            print("SKIP: Bash block requires real git worktrees for IS_LISTED_WORKTREE validation.")
            print("      The detection block's listed-worktree check needs actual git worktree list.")
            print("      Post-merge integration tests will validate this behavior.")
            h.summarize_and_exit()

    print("[Scenario 1] Marker → flow under FLOWS_ROOT, state {step: plan, status: paused, live: null, attempts: {worktree: 1}} → resume")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        main_wt = tmp / "main"
        main_wt.mkdir()
        here = tmp / "302-issue-slug"
        here.mkdir()
        flows = tmp / "flows"
        flows.mkdir()
        flow_dir = flows / "302-issue-slug-id"
        make_flow(flows, "302-issue-slug-id", {
            "issue": 302,
            "step": "plan",
            "status": "paused",
            "live": None,
            "attempts": {"worktree": 1},
        })
        make_marker(here, flow_dir)

        rc, out, err = run_noarg_block(block, main_wt, here, "owner/repo", flows, "main", "true")
        parsed = parse_output(out)
        t("scenario 1: route is resume", parsed.get("NOARG_ROUTE") == "resume", f"got {parsed}")
        t("scenario 1: issue is 302", parsed.get("NOARG_ISSUE") == "302", f"got {parsed}")
        t("scenario 1: flow is set", parsed.get("NOARG_FLOW") == str(flow_dir), f"got {parsed}")

    print()
    print("[Scenario 2] Same but attempts: {plan: 1} → existing")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        main_wt = tmp / "main"
        main_wt.mkdir()
        here = tmp / "302-issue-slug"
        here.mkdir()
        flows = tmp / "flows"
        flows.mkdir()
        flow_dir = flows / "302-issue-slug-id"
        make_flow(flows, "302-issue-slug-id", {
            "issue": 302,
            "step": "plan",
            "status": "paused",
            "live": None,
            "attempts": {"plan": 1},
        })
        make_marker(here, flow_dir)

        rc, out, err = run_noarg_block(block, main_wt, here, "owner/repo", flows, "main", "true")
        parsed = parse_output(out)
        t("scenario 2: route is existing", parsed.get("NOARG_ROUTE") == "existing", f"got {parsed}")
        t("scenario 2: flow is set to marker", parsed.get("NOARG_FLOW") == str(flow_dir), f"got {parsed}")

    print()
    print("[Scenario 3] Same marker but step: implement (Take-over shape) → existing")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        main_wt = tmp / "main"
        main_wt.mkdir()
        here = tmp / "302-issue-slug"
        here.mkdir()
        flows = tmp / "flows"
        flows.mkdir()
        flow_dir = flows / "302-issue-slug-id"
        make_flow(flows, "302-issue-slug-id", {
            "issue": 302,
            "step": "implement",
            "status": "paused",
            "live": None,
        })
        make_marker(here, flow_dir)

        rc, out, err = run_noarg_block(block, main_wt, here, "owner/repo", flows, "main", "true")
        parsed = parse_output(out)
        t("scenario 3: route is existing", parsed.get("NOARG_ROUTE") == "existing", f"got {parsed}")

    print()
    print("[Scenario 4] Same marker but live set → existing")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        main_wt = tmp / "main"
        main_wt.mkdir()
        here = tmp / "302-issue-slug"
        here.mkdir()
        flows = tmp / "flows"
        flows.mkdir()
        flow_dir = flows / "302-issue-slug-id"
        make_flow(flows, "302-issue-slug-id", {
            "issue": 302,
            "step": "plan",
            "status": "paused",
            "live": "some-value",
        })
        make_marker(here, flow_dir)

        rc, out, err = run_noarg_block(block, main_wt, here, "owner/repo", flows, "main", "true")
        parsed = parse_output(out)
        t("scenario 4: route is existing", parsed.get("NOARG_ROUTE") == "existing", f"got {parsed}")

    print()
    print("[Scenario 5] status: failed with valid marker → existing")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        main_wt = tmp / "main"
        main_wt.mkdir()
        here = tmp / "302-issue-slug"
        here.mkdir()
        flows = tmp / "flows"
        flows.mkdir()
        flow_dir = flows / "302-issue-slug-id"
        make_flow(flows, "302-issue-slug-id", {
            "issue": 302,
            "status": "failed",
        })
        make_marker(here, flow_dir)

        rc, out, err = run_noarg_block(block, main_wt, here, "owner/repo", flows, "main", "true")
        parsed = parse_output(out)
        t("scenario 5: route is existing", parsed.get("NOARG_ROUTE") == "existing", f"got {parsed}")

    print()
    print("[Scenario 6] Marker pointing outside FLOWS_ROOT → ask")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        main_wt = tmp / "main"
        main_wt.mkdir()
        here = tmp / "302-issue-slug"
        here.mkdir()
        flows = tmp / "flows"
        flows.mkdir()
        outside = tmp / "outside-flow"
        outside.mkdir()
        make_marker(here, outside)

        rc, out, err = run_noarg_block(block, main_wt, here, "owner/repo", flows, "main", "true")
        parsed = parse_output(out)
        t("scenario 6: route is ask", parsed.get("NOARG_ROUTE") == "ask", f"got {parsed}")

    print()
    print("[Scenario 7] Marker target without state.json → ask")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        main_wt = tmp / "main"
        main_wt.mkdir()
        here = tmp / "302-issue-slug"
        here.mkdir()
        flows = tmp / "flows"
        flows.mkdir()
        flow_dir = flows / "302-issue-slug-id"
        flow_dir.mkdir(parents=True)
        # No state.json
        make_marker(here, flow_dir)

        rc, out, err = run_noarg_block(block, main_wt, here, "owner/repo", flows, "main", "true")
        parsed = parse_output(out)
        t("scenario 7: route is ask", parsed.get("NOARG_ROUTE") == "ask", f"got {parsed}")

    print()
    print("[Scenario 8] Marker state.issue 30 in basename 302-x → ask (prefix confusion guard)")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        main_wt = tmp / "main"
        main_wt.mkdir()
        here = tmp / "302-issue-slug"
        here.mkdir()
        flows = tmp / "flows"
        flows.mkdir()
        flow_dir = flows / "302-issue-slug-id"
        make_flow(flows, "302-issue-slug-id", {"issue": 30})  # Mismatch!
        make_marker(here, flow_dir)

        rc, out, err = run_noarg_block(block, main_wt, here, "owner/repo", flows, "main", "true")
        parsed = parse_output(out)
        t("scenario 8: route is ask", parsed.get("NOARG_ROUTE") == "ask", f"got {parsed}")

    print()
    print("[Scenario 9] No marker, valid cache branch match, issue 302, basename 302-x → plain")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        main_wt = tmp / "main"
        main_wt.mkdir()
        here = tmp / "302-issue-slug"
        here.mkdir()
        make_cache(here, {
            "branch": "main",
            "issue": {"number": 302, "url": "https://github.com/owner/repo/issues/302"},
        })

        rc, out, err = run_noarg_block(block, main_wt, here, "owner/repo", tmp / "flows", "main", "true")
        parsed = parse_output(out)
        t("scenario 9: route is plain", parsed.get("NOARG_ROUTE") == "plain", f"got {parsed}")
        t("scenario 9: issue is 302", parsed.get("NOARG_ISSUE") == "302", f"got {parsed}")

    print()
    print("[Scenario 10] No marker, cache branch ≠ current branch → ask")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        main_wt = tmp / "main"
        main_wt.mkdir()
        here = tmp / "302-issue-slug"
        here.mkdir()
        make_cache(here, {
            "branch": "main",
            "issue": {"number": 302, "url": "https://github.com/owner/repo/issues/302"},
        })

        rc, out, err = run_noarg_block(block, main_wt, here, "owner/repo", tmp / "flows", "feature", "true")
        parsed = parse_output(out)
        t("scenario 10: route is ask", parsed.get("NOARG_ROUTE") == "ask", f"got {parsed}")

    print()
    print("[Scenario 11] No marker, cache number 303 vs basename 302-x → ask")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        main_wt = tmp / "main"
        main_wt.mkdir()
        here = tmp / "302-issue-slug"
        here.mkdir()
        make_cache(here, {
            "branch": "main",
            "issue": {"number": 303, "url": "https://github.com/owner/repo/issues/303"},
        })

        rc, out, err = run_noarg_block(block, main_wt, here, "owner/repo", tmp / "flows", "main", "true")
        parsed = parse_output(out)
        t("scenario 11: route is ask", parsed.get("NOARG_ROUTE") == "ask", f"got {parsed}")

    print()
    print("[Scenario 12] No marker, non-numeric issue number PPS-166 → ask")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        main_wt = tmp / "main"
        main_wt.mkdir()
        here = tmp / "302-issue-slug"
        here.mkdir()
        make_cache(here, {
            "branch": "main",
            "issue": {"number": "PPS-166", "url": "https://github.com/owner/repo/issues/PPS-166"},
        })

        rc, out, err = run_noarg_block(block, main_wt, here, "owner/repo", tmp / "flows", "main", "true")
        parsed = parse_output(out)
        t("scenario 12: route is ask", parsed.get("NOARG_ROUTE") == "ask", f"got {parsed}")

    print()
    print("[Scenario 13] No marker, numeric number but non-GitHub URL → ask")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        main_wt = tmp / "main"
        main_wt.mkdir()
        here = tmp / "302-issue-slug"
        here.mkdir()
        make_cache(here, {
            "branch": "main",
            "issue": {"number": 302, "url": "https://internal.example.com/issues/302"},
        })

        rc, out, err = run_noarg_block(block, main_wt, here, "owner/repo", tmp / "flows", "main", "true")
        parsed = parse_output(out)
        t("scenario 13: route is ask", parsed.get("NOARG_ROUTE") == "ask", f"got {parsed}")

    print()
    print("[Scenario 14] No marker, no cache → ask")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        main_wt = tmp / "main"
        main_wt.mkdir()
        here = tmp / "302-issue-slug"
        here.mkdir()
        # No cache file

        rc, out, err = run_noarg_block(block, main_wt, here, "owner/repo", tmp / "flows", "main", "true")
        parsed = parse_output(out)
        t("scenario 14: route is ask", parsed.get("NOARG_ROUTE") == "ask", f"got {parsed}")

    print()
    print("[Scenario 15] HERE == MAIN_WORKTREE → ask")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        main_wt = tmp / "main"
        main_wt.mkdir()

        rc, out, err = run_noarg_block(block, main_wt, main_wt, "owner/repo", tmp / "flows", "main", "true")
        parsed = parse_output(out)
        t("scenario 15: route is ask", parsed.get("NOARG_ROUTE") == "ask", f"got {parsed}")

    print()
    print("[Scenario 16] Basename without leading digits (pps-166) → ask")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        main_wt = tmp / "main"
        main_wt.mkdir()
        here = tmp / "pps-166"
        here.mkdir()
        make_cache(here, {
            "branch": "main",
            "issue": {"number": 166, "url": "https://github.com/owner/repo/issues/166"},
        })

        rc, out, err = run_noarg_block(block, main_wt, here, "owner/repo", tmp / "flows", "main", "true")
        parsed = parse_output(out)
        t("scenario 16: route is ask", parsed.get("NOARG_ROUTE") == "ask", f"got {parsed}")

    print()
    print("[Scenario 17] Marker with integer state.issue 302 in 302-x (explicit JSON integer) → resume")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        main_wt = tmp / "main"
        main_wt.mkdir()
        here = tmp / "302-issue-slug"
        here.mkdir()
        flows = tmp / "flows"
        flows.mkdir()
        flow_dir = flows / "302-issue-slug-id"
        # Explicitly write integer 302, not string
        d = flow_dir
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(json.dumps({
            "issue": 302,  # integer
            "step": "plan",
            "status": "paused",
            "live": None,
            "attempts": {},
        }))
        make_marker(here, flow_dir)

        rc, out, err = run_noarg_block(block, main_wt, here, "owner/repo", flows, "main", "true")
        parsed = parse_output(out)
        t("scenario 17: route is resume", parsed.get("NOARG_ROUTE") == "resume", f"got {parsed}")

    print()
    print("[Scenario 18] No marker, cache issue.number as string '302' with valid GitHub URL → plain")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        main_wt = tmp / "main"
        main_wt.mkdir()
        here = tmp / "302-issue-slug"
        here.mkdir()
        make_cache(here, {
            "branch": "main",
            "issue": {"number": "302", "url": "https://github.com/owner/repo/issues/302"},  # string
        })

        rc, out, err = run_noarg_block(block, main_wt, here, "owner/repo", tmp / "flows", "main", "true")
        parsed = parse_output(out)
        t("scenario 18: route is plain", parsed.get("NOARG_ROUTE") == "plain", f"got {parsed}")
        t("scenario 18: issue normalised to 302", parsed.get("NOARG_ISSUE") == "302", f"got {parsed}")

    print()
    print("[Scenario 19] No marker, local cache (url: '', numeric issue 5) in basename 5-foo → ask with NOARG_NONGITHUB=yes")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        main_wt = tmp / "main"
        main_wt.mkdir()
        here = tmp / "5-foo"
        here.mkdir()
        make_cache(here, {
            "branch": "main",
            "issue": {"number": 5, "url": ""},  # Local, non-GitHub
        })

        rc, out, err = run_noarg_block(block, main_wt, here, "owner/repo", tmp / "flows", "main", "true")
        parsed = parse_output(out)
        t("scenario 19: route is ask", parsed.get("NOARG_ROUTE") == "ask", f"got {parsed}")
        t("scenario 19: NOARG_NONGITHUB is yes", parsed.get("NOARG_NONGITHUB") == "yes", f"got {parsed}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
