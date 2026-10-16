#!/usr/bin/env python3
"""
Fixture test for the bare /expert-flow detection block (### No-argument run).

The first ```bash block under "### No-argument run" in commands/expert-flow.md is
extracted at test time and runs under bash with real jq and git against temporary
fixture directories. Skips cleanly when jq is missing.

The block computes its own inputs. HERE, CUR_BRANCH and IS_LISTED_WORKTREE default
from the current git worktree when unset or empty. REPO, FLOWS_ROOT and MAIN_WORKTREE
come from the doc's placeholder literals, which the orchestrator substitutes before
running the block; the test passes them through the environment instead. The block
computes:
  NOARG_ROUTE: one of resume, existing, plain, ask
  NOARG_ISSUE: the issue number, or empty
  NOARG_FLOW: the validated (realpath) flow directory, or empty
  NOARG_NONGITHUB: "yes" if the cache is non-GitHub, else empty
  NOARG_REASON: a fixed reason string, or empty (never a cache-derived value)

The argument-parsing block under "### Argument parsing" is extracted and run too, to
pin the bare-run rule and the --plan-file character allowlist. A few doc-text checks
pin the prose that the detection block relies on.

Testing Strategy: 25 numbered block scenarios (scenario 25 covers several
missing-input variants), 9 parser checks, and the doc-text checks in main().

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
NOARG_HEADING = "### No-argument run"
PARSE_HEADING = "### Argument parsing"
NOARG_KEYS = ["NOARG_ROUTE", "NOARG_ISSUE", "NOARG_FLOW", "NOARG_NONGITHUB", "NOARG_REASON"]

REASON_NOT_WORKTREE = "not in a ticket worktree"
REASON_BASENAME = "basename does not start with digits"
REASON_MARKER = "flow marker does not match this worktree"
REASON_NO_INPUTS = "no flow marker or .claude/github-cache.json"
REASON_BRANCH = "cache branch differs from current branch"
REASON_NO_BRANCH = "cache has no branch"
REASON_NUMBER = "cache issue.number does not match basename"
REASON_NO_NUMBER = "cache has no issue.number"
REASON_URL = "cache url is not for this issue"
REASON_NONGITHUB = "cache is not a GitHub issue"
REASON_MISSING = "detection inputs missing"


def extract_block(text, heading):
    """Return the first ```bash block under heading, or ''."""
    start = text.find(heading)
    if start < 0:
        return ""
    m = re.search(r"```bash\n(.*?)```", text[start:], re.DOTALL)
    if not m:
        return ""
    return m.group(1)


def section_text(text, heading):
    """Return the text of a ### section, up to the next ## or ### heading."""
    start = text.find(heading)
    if start < 0:
        return ""
    body_start = start + len(heading)
    m = re.search(r"\n##+ ", text[body_start:])
    end = body_start + m.start() if m else len(text)
    return text[start:end]


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


def block_env(fx, here, repo, cur_branch, is_listed):
    """Environment for the detection block: inputs pre-set by the test, as the orchestrator would substitute."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "HERE": str(here),
        "MAIN_WORKTREE": str(fx.main),
        "REPO": repo,
        "FLOWS_ROOT": str(fx.flows),
        "CUR_BRANCH": cur_branch,
        "IS_LISTED_WORKTREE": "yes" if is_listed else "no",
    }


def run_block(snippet, env, cwd):
    """Run a snippet under bash with the given env and cwd. Returns (returncode, stdout, stderr)."""
    result = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True,
                            env=env, cwd=str(cwd))
    return result.returncode, result.stdout, result.stderr


def parse_output(stdout):
    """Parse one-KEY=value-per-line output into a dict."""
    result = {}
    for line in stdout.split("\n"):
        if "=" in line:
            key, val = line.split("=", 1)
            result[key] = val
    return result


def git(cwd, *args):
    """Run git in a fixture repo with a throwaway identity."""
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "-c", "commit.gpgsign=false", *args],
                   cwd=str(cwd), check=True, capture_output=True, text=True)


class Fixture:
    """A temp tree: main/ (or main_name), a ticket worktree, and flows/."""

    def __init__(self, tmp, wt_name="302-issue-slug", main_name="main"):
        self.tmp = Path(tmp)
        self.main = self.tmp / main_name
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
    block = extract_block(cmd_text, NOARG_HEADING)
    parse_block = extract_block(cmd_text, PARSE_HEADING)
    noarg_sec = section_text(cmd_text, NOARG_HEADING)
    t("the no-arg detection block is present in expert-flow.md", bool(block))
    if not block:
        h.summarize_and_exit()

    # The block may call git only inside its ': "${VAR:=$(git ...)}"' defaults.
    git_lines = [ln for ln in block.splitlines() if re.search(r"\bgit\b", ln)]
    t("git runs only inside the := defaults of the detection block",
      bool(git_lines) and all(re.match(r'\s*: "\$\{\w+:=\$\(git ', ln) for ln in git_lines))
    t("block never exits the shell", re.search(r"^\s*exit\b", block, re.MULTILINE) is None)
    t("block has no trailing whitespace", re.search(r"[ \t]+$", block, re.MULTILINE) is None)
    t("block has the orchestrator placeholder literals for REPO, FLOWS_ROOT, MAIN_WORKTREE",
      all(s in block for s in (': "${REPO:=<REPO>}"', ': "${FLOWS_ROOT:=<FLOWS_ROOT>}"',
                               ': "${MAIN_WORKTREE:=<MAIN_WORKTREE>}"')))
    t("prose no longer asks the reader to run a separate input block",
      "HERE=$(git rev-parse --show-toplevel)" not in noarg_sec)

    def check(label, route, issue="", flow="", nongithub="", cur_branch="main",
              is_listed=True, repo="owner/repo", setup=None, wt_name="302-issue-slug",
              use_main_as_here=False, main_name="main", reason=None):
        """Build a fixture, run the block, assert the exact outputs (and the fixed reason if given)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = Fixture(tmpdir, wt_name, main_name)
            expected_flow = setup(fx) if setup else ""
            here = fx.main if use_main_as_here else fx.here
            env = block_env(fx, here, repo, cur_branch, is_listed)
            rc, out, err = run_block(block, env, fx.tmp)
            p = parse_output(out)
            want_flow = os.path.realpath(str(expected_flow)) if flow == "<flow>" else flow
            got = (p.get("NOARG_ROUTE"), p.get("NOARG_ISSUE"), p.get("NOARG_FLOW"), p.get("NOARG_NONGITHUB"))
            want = (route, issue, want_flow, nongithub)
            t(f"{label}: exact outputs", rc == 0 and got == want,
              f"rc={rc} got={got} want={want} err={err!r}")
            if reason is not None:
                t(f"{label}: fixed reason", p.get("NOARG_REASON") == reason,
                  f"got={p.get('NOARG_REASON')!r} want={reason!r}")

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
    check("scenario 6", "ask", setup=outside, reason=REASON_MARKER)

    print("[Scenario 6b] marker in sibling dir sharing the FLOWS_ROOT prefix (flows vs flowsx) -> ask")

    def sibling(fx):
        make_flow(fx.tmp / "flowsx", "302-issue-slug-id", handoff)
        make_marker(fx.here, fx.tmp / "flowsx" / "302-issue-slug-id")
    check("scenario 6b", "ask", setup=sibling, reason=REASON_MARKER)

    print("[Scenario 6c] marker is FLOWS_ROOT itself -> ask")

    def is_root(fx):
        (fx.flows / "state.json").write_text(json.dumps(handoff))
        make_marker(fx.here, fx.flows)
    check("scenario 6c", "ask", setup=is_root, reason=REASON_MARKER)

    print("[Scenario 7] marker target without state.json, otherwise-valid cache -> ask (cache must not rescue it)")

    def marker_no_state_valid_cache(fx):
        flow_dir = fx.flows / "302-issue-slug-id"
        flow_dir.mkdir(parents=True)
        make_marker(fx.here, flow_dir)
        make_cache(fx.here, gh302)
        return ""
    check("scenario 7", "ask", setup=marker_no_state_valid_cache, reason=REASON_MARKER)
    print("[Scenario 8] state.issue 30 vs basename 302 -> ask")
    check("scenario 8", "ask", setup=marker_setup({"issue": 30}), reason=REASON_MARKER)

    print("[Scenario 9] valid cache -> plain")
    check("scenario 9", "plain", "302", setup=cache_setup(gh302), reason="")
    print("[Scenario 10] cache branch differs -> ask")
    check("scenario 10", "ask", cur_branch="feature", setup=cache_setup(gh302), reason=REASON_BRANCH)
    print("[Scenario 11] cache number 303 vs basename 302 -> ask")
    check("scenario 11", "ask", setup=cache_setup(
        {"branch": "main", "issue": {"number": 303, "url": "https://github.com/owner/repo/issues/303"}}),
        reason=REASON_NUMBER)
    print("[Scenario 12] non-numeric number PPS-166 -> ask")
    check("scenario 12", "ask", setup=cache_setup(
        {"branch": "main", "issue": {"number": "PPS-166", "url": "https://github.com/owner/repo/issues/PPS-166"}}),
        reason=REASON_NUMBER)
    print("[Scenario 13] non-GitHub URL -> ask + NOARG_NONGITHUB=yes")
    check("scenario 13", "ask", nongithub="yes", reason=REASON_NONGITHUB, setup=cache_setup(
        {"branch": "main", "issue": {"number": 302, "url": "https://internal.example.com/issues/302"}}))
    print("[Scenario 13b] GitHub URL for a different issue -> ask, not non-GitHub")
    check("scenario 13b", "ask", reason=REASON_URL, setup=cache_setup(
        {"branch": "main", "issue": {"number": 302, "url": "https://github.com/owner/repo/issues/999"}}))
    print("[Scenario 14] no marker, no cache -> ask")
    check("scenario 14", "ask", reason=REASON_NO_INPUTS)

    print("[Scenario 15] HERE == MAIN_WORKTREE, and MAIN is 302-main with a valid cache -> ask only because of the main guard")

    def main_cache(fx):
        make_cache(fx.main, gh302)
        return ""
    check("scenario 15", "ask", use_main_as_here=True, main_name="302-main",
          setup=main_cache, reason=REASON_NOT_WORKTREE)
    print("[Scenario 15b] HERE != MAIN, IS_LISTED_WORKTREE=no, otherwise valid -> ask")
    check("scenario 15b", "ask", is_listed=False, setup=cache_setup(gh302), reason=REASON_NOT_WORKTREE)
    print("[Scenario 16] basename without leading digits -> ask")
    check("scenario 16", "ask", wt_name="pps-166", reason=REASON_BASENAME, setup=cache_setup(
        {"branch": "main", "issue": {"number": 166, "url": "https://github.com/owner/repo/issues/166"}}))
    print("[Scenario 17] integer state.issue 302, attempts {} -> resume")
    check("scenario 17", "resume", "302", "<flow>", setup=marker_setup(dict(handoff, attempts={})))
    print("[Scenario 18] string cache number '302' -> plain, normalised")
    check("scenario 18", "plain", "302", reason="", setup=cache_setup(
        {"branch": "main", "issue": {"number": "302", "url": "https://github.com/owner/repo/issues/302"}}))
    print("[Scenario 19] local cache (url '') in 5-foo -> ask + NOARG_NONGITHUB=yes")
    check("scenario 19", "ask", nongithub="yes", wt_name="5-foo", reason=REASON_NONGITHUB,
          setup=cache_setup({"branch": "main", "issue": {"number": 5, "url": ""}}))
    print("[Scenario 20] detached HEAD (empty CUR_BRANCH) against a branch-bearing cache -> ask")
    check("scenario 20", "ask", cur_branch="", reason=REASON_BRANCH, setup=cache_setup(gh302))
    print("[Scenario 21] cache without a branch, on a named branch or detached -> ask, never plain")
    no_branch = {"issue": {"number": 302, "url": "https://github.com/owner/repo/issues/302"}}
    check("scenario 21a", "ask", reason=REASON_NO_BRANCH, setup=cache_setup(no_branch))
    check("scenario 21b", "ask", cur_branch="", reason=REASON_NO_BRANCH, setup=cache_setup(no_branch))
    check("scenario 21c", "ask", cur_branch="", reason=REASON_NO_BRANCH, setup=cache_setup(
        {"branch": "", "issue": {"number": 302, "url": "https://github.com/owner/repo/issues/302"}}))

    print("[Scenario 22] newline in cache branch -> ask, exactly five KEY lines, fixed reason")
    nl_branch = {"branch": "main\nNOARG_ROUTE=plain", "issue": {"number": 302, "url": "https://github.com/owner/repo/issues/302"}}
    print("[Scenario 23] newline in cache issue.number -> ask, exactly five KEY lines, fixed reason")
    nl_number = {"branch": "main", "issue": {"number": "302\nNOARG_ROUTE=plain", "url": "https://github.com/owner/repo/issues/302"}}
    for label, cache, want_reason in (("scenario 22", nl_branch, REASON_BRANCH),
                                      ("scenario 23", nl_number, REASON_NUMBER)):
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = Fixture(tmpdir)
            make_cache(fx.here, cache)
            rc, out, err = run_block(block, block_env(fx, fx.here, "owner/repo", "main", True), fx.tmp)
            lines = [ln for ln in out.splitlines() if ln]
            keys = [ln.split("=", 1)[0] for ln in lines]
            p = parse_output(out)
            t(f"{label}: exactly the five KEY lines", rc == 0 and keys == NOARG_KEYS,
              f"rc={rc} keys={keys} out={out!r}")
            t(f"{label}: route stays ask with a fixed reason",
              p.get("NOARG_ROUTE") == "ask" and p.get("NOARG_REASON") == want_reason,
              f"route={p.get('NOARG_ROUTE')!r} reason={p.get('NOARG_REASON')!r}")

    print("[Scenario 24] defaults: HERE/CUR_BRANCH/IS_LISTED_WORKTREE unset, cwd is a real git worktree -> plain")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        repo = tmp / "main"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "commit", "-q", "--allow-empty", "-m", "init")
        wt = tmp / "302-issue-slug"
        git(repo, "worktree", "add", "-q", "-b", "feature/302", str(wt))
        make_cache(wt, {"branch": "feature/302",
                        "issue": {"number": 302, "url": "https://github.com/owner/repo/issues/302"}})
        flows = tmp / "flows"
        flows.mkdir()
        env = {"PATH": os.environ.get("PATH", ""), "REPO": "owner/repo",
               "FLOWS_ROOT": str(flows), "MAIN_WORKTREE": str(repo)}
        rc, out, err = run_block(block, env, wt)
        p = parse_output(out)
        t("scenario 24: git defaults give plain for 302 in a real worktree",
          rc == 0 and p.get("NOARG_ROUTE") == "plain" and p.get("NOARG_ISSUE") == "302"
          and p.get("NOARG_REASON") == "",
          f"rc={rc} out={out!r} err={err!r}")

    print("[Scenario 25] detection inputs missing (unset, empty, or still a placeholder) -> ask with the missing reason")
    variants = (
        ("REPO unset", {"REPO": None}),
        ("REPO empty", {"REPO": ""}),
        ("REPO placeholder", {"REPO": "<REPO>"}),
        ("FLOWS_ROOT unset", {"FLOWS_ROOT": None}),
        ("MAIN_WORKTREE unset", {"MAIN_WORKTREE": None}),
        ("HERE empty", {"HERE": ""}),
    )
    for label, overrides in variants:
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = Fixture(tmpdir)
            make_cache(fx.here, gh302)
            env = block_env(fx, fx.here, "owner/repo", "main", True)
            for key, val in overrides.items():
                if val is None:
                    env.pop(key, None)
                else:
                    env[key] = val
            rc, out, err = run_block(block, env, fx.tmp)
            p = parse_output(out)
            t(f"scenario 25 ({label}): ask with the missing-inputs reason",
              rc == 0 and p.get("NOARG_ROUTE") == "ask" and p.get("NOARG_REASON") == REASON_MISSING,
              f"rc={rc} out={out!r} err={err!r}")

    # Argument parsing: run the first bash block under "### Argument parsing" with argv.
    print("[Parser] bare-run rule and --plan-file allowlist")
    parse_script = parse_block + '\nprintf "BAD=%s\\nNO_ARG=%s\\n" "$BAD" "$NO_ARG"\n'

    def run_parse(args):
        with tempfile.TemporaryDirectory() as tmpdir:
            r = subprocess.run(["bash", "-c", parse_script, "expert-flow", *args], capture_output=True,
                               text=True, env={"PATH": os.environ.get("PATH", "")}, cwd=tmpdir)
            return r.returncode, parse_output(r.stdout)

    rc, p = run_parse([])
    t("parser: no arguments is a bare run", rc == 0 and p.get("NO_ARG") == "yes" and p.get("BAD") == "",
      f"rc={rc} got={p}")
    rc, p = run_parse([""])
    t("parser: an empty argument string that parses to nothing is refused, not a bare run",
      rc == 0 and p.get("NO_ARG") == "no" and p.get("BAD") not in ("", None), f"rc={rc} got={p}")
    rc, p = run_parse(["302"])
    t("parser: an issue number is not a bare run and is not refused",
      rc == 0 and p.get("NO_ARG") == "no" and p.get("BAD") == "", f"rc={rc} got={p}")
    rc, p = run_parse(["302", "--from=implement", "--plan-file", "/tmp/plans/plan-1.md"])
    t("parser: a plain plan path is accepted", rc == 0 and p.get("BAD") == "", f"rc={rc} got={p}")
    plan_refusal = "--plan-file path may contain only letters, digits, and . _ / ~ + : = @ , -"
    for label, path in (("a space in the path", "/tmp/my plans/plan.md"),
                        ("a double quote in the path", '/tmp/a"b/plan.md'),
                        ("a single quote in the path", "/tmp/a'b/plan.md"),
                        ("a dollar sign in the path", "/tmp/$HOME/plan.md"),
                        ("a backtick in the path", "/tmp/a`b/plan.md")):
        rc, p = run_parse(["302", "--from=implement", "--plan-file", path])
        t(f"parser: {label} is refused before any worktree", rc == 0 and p.get("BAD") == plan_refusal,
          f"rc={rc} got={p}")

    print("[Doc] pinned prose and the no-argument run section")
    locators = re.findall(r":\d{3,4}\b", noarg_sec)
    t("no-arg section has no line-number locators (:NNN)", locators == [], f"found={locators}")
    t("resume trail line is unchanged",
      "resuming flow <basename of NOARG_FLOW> at plan (hand-off).  Not this one? /expert-flow --abandon <N>, "
      "or /expert-flow <N> from the main worktree to choose Start fresh" in noarg_sec)
    ask_start = noarg_sec.find("- **`ask`**")
    ask_bullet = noarg_sec[ask_start:] if ask_start >= 0 else ""
    ask_call = ask_bullet.find("AskUserQuestion")
    nongh = ask_bullet.find("NOARG_NONGITHUB=yes")
    t("ask route guard_blocks before AskUserQuestion when NOARG_NONGITHUB=yes",
      ask_call > 0 and 0 <= nongh < ask_call and "do not ask" in ask_bullet[:ask_call]
      and "guard_block" in ask_bullet[:ask_call],
      f"nongh={nongh} ask_call={ask_call}")
    t("ask route still offers the one-line question for other worktrees",
      "Which issue should this flow run?" in ask_bullet)
    t("Hand-off template sentence is unchanged",
      "When the orchestrator prints hand-off lines (both \"go into the worktree\" sites: Main's fresh-run "
      "worktree-ready, and the post-worktree Hand-off), compute `QUOTED_WORKTREE=$(printf '%q' \"$WORKTREE\")` "
      "and print:" in cmd_text)
    t("Plain /expert-flow <N> paragraph is unchanged",
      "**Plain `/expert-flow <N>` with an existing flow.** An explicit `/expert-flow <N>` always asks the "
      "Existing-flow question when a flow exists" in cmd_text)
    t("--from hand-off states the plan-path fallback rule (refused by the parser, never escaped)",
      "refuses any other path with a `guard_block` before a worktree is created" in cmd_text
      and "`QUOTED_PLAN_FILE` is the `--plan-file` path as given" in cmd_text)
    marker_para = ""
    mp = cmd_text.find("**Flow marker validity.**")
    if mp >= 0:
        marker_para = cmd_text[mp:cmd_text.find("\n\n", mp)]
    t("Flow marker validity names the no-argument detection exception",
      "no-argument detection block" in marker_para and "never writes or deletes it" in marker_para,
      f"paragraph={marker_para[:80]!r}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
