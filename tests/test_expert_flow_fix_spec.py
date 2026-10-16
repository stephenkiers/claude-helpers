#!/usr/bin/env python3
"""
Spec-blind tests for the expert-flow review-fix plan (decided item 1 and the testable
Doing-it items of the claude-action-plan for PR #302).

Written from the plan alone. Every check loads the real production file at test time:
- commands/expert-flow.md: bare-run detection block, marker-validity rule, --from hand-off, NO_ARG wording
- docs/adr/0023-expert-flow.md: trail-line ruling, Start fresh wording, Epic #290 exception, #307 note
- CLAUDE.md: bare-form sentence

Runtime checks extract the first fenced bash block containing IS_LISTED_WORKTREE from
commands/expert-flow.md, fill its <PLACEHOLDER> literals (FLOWS_ROOT, MAIN_WORKTREE, REPO),
and run it under bash inside a real git worktree fixture with a minimal environment.

Deliberately not covered here:
- Plan items 3, 4, 12, 13, 14 concern the round-1 test files themselves (assertion anchoring,
  dead helper, test names). Authoring tests about test files is out of scope for this pass.
- Plan item 11's parser behaviour (empty vs unparseable argument string): the plan names no
  stable anchor for the argument-parsing block, so only the NO_ARG wording is checked statically.
- Plan item 6's runtime behaviour (plan path with whitespace): the --from hand-off block has no
  named anchor either; only the undefined-variable part is checked statically.

Run with: python3 tests/test_expert_flow_fix_spec.py
"""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _git_fixture import GitFixture
from _test_harness import Harness, REPO_ROOT

DOC = REPO_ROOT / "commands" / "expert-flow.md"
ADR = REPO_ROOT / "docs" / "adr" / "0023-expert-flow.md"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"


def read(path):
    """Return the file text, or '' when the file is absent."""
    return path.read_text() if path.is_file() else ""


def norm(text):
    """Collapse all whitespace runs so wrapped prose compares as one line."""
    return re.sub(r"\s+", " ", text)


def paragraphs(text):
    """Blank-line separated blocks of text."""
    return [p for p in re.split(r"\n\s*\n", text) if p.strip()]


def section_from(text, needle, max_chars=1500):
    """Text from `needle` up to the next heading or bold/bullet lead-in, capped at max_chars."""
    i = text.find(needle)
    if i < 0:
        return ""
    rest = text[i + len(needle):]
    end = len(rest)
    for stop in ("\n**", "\n- **", "\n## ", "\n### ", "\n#### "):
        j = rest.find(stop)
        if j != -1:
            end = min(end, j)
    return needle + rest[:min(end, max_chars)]


def heading_section(text, heading_regex):
    """Text of the markdown section whose heading matches, up to the next heading line."""
    m = re.search(heading_regex, text, re.M)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = re.search(r"^#", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


def windows_around(text, needle, radius):
    """Substrings of +-radius characters around every occurrence of needle."""
    out = []
    for m in re.finditer(re.escape(needle), text):
        out.append(text[max(0, m.start() - radius): m.end() + radius])
    return out


PLACEHOLDER = re.compile(r"<([A-Z][A-Z0-9_]*)>")


def detection_block():
    """First fenced bash block in the command doc that mentions IS_LISTED_WORKTREE, or ''."""
    for block in re.findall(r"```bash\n(.*?)```", read(DOC), re.S):
        if "IS_LISTED_WORKTREE" in block:
            return block
    return ""


def fill_placeholders(block, flows_root, main_wt):
    """Replace <FLOWS...>, <MAIN...> and <REPO...> literals with fixture values."""
    def repl(m):
        name = m.group(1)
        if "FLOW" in name:
            return str(flows_root)
        if "MAIN" in name:
            return str(main_wt)
        if "REPO" in name:
            return main_wt.name
        return m.group(0)
    return PLACEHOLDER.sub(repl, block)


def run_block(block, cwd, home, env_extra=None):
    """Run the block under bash with a minimal environment (PATH and a temp HOME only)."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home)}
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", "-c", block],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def key_values(stdout):
    """KEY=value lines of stdout as a dict (last write wins)."""
    return {k: v for k, v in re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", stdout, re.M)}


def make_repo(tmp_root):
    """Fresh git fixture with one commit. Returns (fixture, resolved main worktree path)."""
    fx = GitFixture(tmp_root / "fx")
    fx.create_initial_commit()
    return fx, fx.repo_root.resolve()


def add_worktree(fx, dirname, branch):
    """Create a linked worktree named `dirname` on a new `branch`. Returns its resolved path."""
    path = fx.worktree_parent / dirname
    path.parent.mkdir(parents=True, exist_ok=True)
    fx._run_git(["worktree", "add", "-b", branch, str(path)], cwd=fx.repo_root)
    return path.resolve()


def home_for(tmp_root):
    """Temp HOME; FLOWS_ROOT for the fixture repo lives under it."""
    home = tmp_root / "home"
    home.mkdir(parents=True, exist_ok=True)
    return home


if __name__ == "__main__":
    h = Harness("EXPERT-FLOW FIX-PLAN SPEC-BLIND TEST SUITE")
    t = h.test_result

    doc_text = read(DOC)
    adr_text = read(ADR)
    claude_text = read(CLAUDE_MD)

    # ------------------------------------------------------------------
    print("[Decided item 1] ADR-0023 trail-line ruling matches the command doc")
    print("=" * 70)

    t("ADR states the trail line prints on silent resume of the hand-off flow",
      "silent resume of the hand-off flow" in norm(adr_text),
      "phrase 'silent resume of the hand-off flow' not found in ADR-0023")

    trail_paras = [p for p in paragraphs(adr_text) if "trail" in p.lower()]
    t("ADR no longer describes the trail line as firing on a 'non-hand-off' flow",
      not any("non-hand-off" in norm(p).lower() for p in trail_paras),
      "a trail-line paragraph still says 'non-hand-off'")

    t("ADR no longer claims the amendment 'preserves the Start fresh / Abort recovery' paths",
      "preserves the Start fresh / Abort recovery" not in norm(adr_text),
      "the unsupported 'preserves the Start fresh / Abort recovery' claim is still present")

    start_fresh_paras = [p for p in paragraphs(adr_text)
                         if "start fresh" in p.lower() and "main worktree" in p.lower()]
    t("ADR says Start fresh must be run from the main worktree (same paragraph)",
      bool(start_fresh_paras),
      "no paragraph mentions both 'Start fresh' and 'main worktree'")

    # ------------------------------------------------------------------
    print()
    print("[Decided item 1 + Doing-it 15] Epic #290 exception and CLAUDE.md wording")
    print("=" * 70)

    t("ADR records the bare-form silent resume as an exception to Epic #290 principle 2",
      any("#290" in p and "principle 2" in p and "exception" in p.lower() and "bare" in p.lower()
          for p in paragraphs(adr_text)),
      "no single paragraph of ADR-0023 mentions #290, 'principle 2', 'exception' and 'bare' together")

    t("CLAUDE.md bare-form sentence says: in the exact hand-off state it resumes silently",
      "in the exact hand-off state it resumes silently" in norm(claude_text),
      "CLAUDE.md does not contain 'in the exact hand-off state it resumes silently'")

    t("CLAUDE.md bare-form sentence says: otherwise it asks as usual",
      "otherwise it asks as usual" in norm(claude_text),
      "CLAUDE.md does not contain 'otherwise it asks as usual'")

    # ------------------------------------------------------------------
    print()
    print("[Doing-it 2] No hard-coded file:line locators in the command doc or ADR")
    print("=" * 70)

    for label, text in (("commands/expert-flow.md", doc_text), ("docs/adr/0023-expert-flow.md", adr_text)):
        hits = re.findall(r"\.md:\d+", text)
        t(f"{label} has no '.md:NNN' line locators",
          not hits, f"found locators: {hits[:5]}")

    # ------------------------------------------------------------------
    print()
    print("[Doing-it 2] ADR Consequences cites #307 (empty-parse masking)")
    print("=" * 70)

    cons_at = adr_text.find("Consequences")
    t("ADR has a Consequences section that cites #307",
      cons_at >= 0 and "#307" in adr_text[cons_at:],
      "no '#307' appears at or after the ADR's Consequences heading")

    # ------------------------------------------------------------------
    print()
    print("[Doing-it 5] Non-GitHub worktree routing: guard without asking")
    print("=" * 70)

    nongh_windows = windows_around(doc_text, "NOARG_NONGITHUB", 400)
    t("command doc mentions NOARG_NONGITHUB in route prose",
      bool(nongh_windows), "NOARG_NONGITHUB is not mentioned anywhere in expert-flow.md")
    t("NOARG_NONGITHUB route says guard_block without asking",
      any("guard_block" in w and re.search(r"without asking|not ask|do not ask|no ask", w, re.I)
          for w in nongh_windows),
      "no NOARG_NONGITHUB passage pairs guard_block with a no-ask statement")

    # ------------------------------------------------------------------
    print()
    print("[Doing-it 6] --from hand-off does not reference an undefined QUOTED_PLAN_FILE")
    print("=" * 70)

    if "QUOTED_PLAN_FILE" in doc_text:
        t("QUOTED_PLAN_FILE, where used, is assigned in the doc",
          re.search(r"QUOTED_PLAN_FILE=", doc_text) is not None,
          "QUOTED_PLAN_FILE is used but never assigned")
    else:
        t("QUOTED_PLAN_FILE removed from the doc (no undefined reference)", True)

    # ------------------------------------------------------------------
    print()
    print("[Doing-it 8] 'Flow marker validity' rule names the bare-run exception")
    print("=" * 70)

    validity = section_from(doc_text, "Flow marker validity")
    t("'Flow marker validity' rule exists in the command doc",
      bool(validity), "no 'Flow marker validity' passage found")
    t("'Flow marker validity' rule names the bare-run detection exception",
      re.search(r"bare|no-arg|no arg|argument-less", validity, re.I) is not None,
      "the validity rule still says stale markers are always ignored, with no bare-run exception")

    # ------------------------------------------------------------------
    print()
    print("[Doing-it 9] NOARG_FLOW carries the validated real path")
    print("=" * 70)

    t('NOARG_FLOW is assigned from MARKER_REAL (NOARG_FLOW="$MARKER_REAL")',
      re.search(r"NOARG_FLOW=[\"']?\$\{?MARKER_REAL\}?[\"']?", doc_text) is not None,
      "no NOARG_FLOW=\"$MARKER_REAL\" assignment found in expert-flow.md")

    # ------------------------------------------------------------------
    print()
    print("[Doing-it 11] Empty-argument routing is gated on the raw argument string")
    print("=" * 70)

    no_arg_windows = windows_around(doc_text, "NO_ARG=yes", 300)
    t("command doc sets NO_ARG=yes",
      bool(no_arg_windows), "NO_ARG=yes not found in expert-flow.md")
    t("NO_ARG=yes is tied to an empty raw argument string",
      any("empty" in w.lower() for w in no_arg_windows),
      "no NO_ARG=yes passage mentions an empty argument string")

    # ------------------------------------------------------------------
    print()
    print("[Doing-it 1] Bare-run detection block runs from a real worktree with inputs unset")
    print("=" * 70)

    block = detection_block()
    t("the bare-run detection bash block is present in expert-flow.md",
      bool(block), "no fenced bash block mentions IS_LISTED_WORKTREE")

    if block:
        with tempfile.TemporaryDirectory() as d:
            tmp_root = Path(d)
            fx, main_wt = make_repo(tmp_root)
            wt = add_worktree(fx, "5-widget", "feature/5-widget")
            home = home_for(tmp_root)
            flows_root = home / ".claude" / "flows" / main_wt.name
            filled = fill_placeholders(block, flows_root, main_wt)

            unset = run_block(filled, wt, home)
            explicit = run_block(filled, wt, home, {
                "HERE": str(wt),
                "CUR_BRANCH": "feature/5-widget",
                "IS_LISTED_WORKTREE": "yes",
            })

            t("block exits 0 with its inputs unset, from a real worktree cwd",
              unset.returncode == 0, f"rc={unset.returncode} stderr={unset.stderr[-300:]!r}")
            t("block emits at least one KEY=value line with inputs unset",
              bool(key_values(unset.stdout)), f"stdout={unset.stdout[-300:]!r}")
            t("block does not report missing detection inputs when run from a worktree",
              re.search(r"input[s]?\s+(are\s+)?missing|missing\s+(detection\s+)?input",
                        unset.stdout + unset.stderr, re.I) is None,
              f"stdout={unset.stdout!r} stderr={unset.stderr!r}")
            t("unset-input run matches the explicit-input run (defaults are self-contained)",
              unset.stdout == explicit.stdout and unset.returncode == explicit.returncode,
              f"unset={unset.stdout!r} explicit={explicit.stdout!r}")

    # ------------------------------------------------------------------
    print()
    print("[Doing-it 5] Non-GitHub worktree output channel carries NOARG_NONGITHUB")
    print("=" * 70)

    if block:
        with tempfile.TemporaryDirectory() as d:
            tmp_root = Path(d)
            fx, main_wt = make_repo(tmp_root)
            wt = add_worktree(fx, "scratch", "scratch-notes")
            home = home_for(tmp_root)
            flows_root = home / ".claude" / "flows" / main_wt.name
            filled = fill_placeholders(block, flows_root, main_wt)
            res = run_block(filled, wt, home)
            kv = key_values(res.stdout)
            t("scratch worktree: detection output carries the NOARG_NONGITHUB key",
              "NOARG_NONGITHUB" in kv,
              f"rc={res.returncode} stdout={res.stdout!r} stderr={res.stderr[-300:]!r}")

    # ------------------------------------------------------------------
    print()
    print("[Doing-it 10] Detached HEAD with an empty cached branch is not trusted")
    print("=" * 70)

    if block:
        with tempfile.TemporaryDirectory() as d:
            tmp_root = Path(d)
            fx, main_wt = make_repo(tmp_root)
            wt = add_worktree(fx, "5-widget", "feature/5-widget")
            fx._run_git(["checkout", "--detach"], cwd=wt)
            fx.write_cache_file(wt, {"issue": 5, "branch": ""})
            home = home_for(tmp_root)
            flows_root = home / ".claude" / "flows" / main_wt.name
            filled = fill_placeholders(block, flows_root, main_wt)
            res = run_block(filled, wt, home)
            t("detached HEAD with empty cached branch: block exits 0",
              res.returncode == 0, f"rc={res.returncode} stderr={res.stderr[-300:]!r}")
            t("detached HEAD with empty cached branch: no NOARG_FLOW is trusted",
              key_values(res.stdout).get("NOARG_FLOW", "") == "",
              f"stdout={res.stdout!r}")

    # ------------------------------------------------------------------
    print()
    print("[Doing-it 7] Cache values with embedded newlines cannot inject KEY=value lines")
    print("=" * 70)

    if block:
        payload = "x\nNOARG_FLOW=INJECTED_PAYLOAD\nNOARG_REASON=INJECTED_PAYLOAD"
        with tempfile.TemporaryDirectory() as d:
            tmp_root = Path(d)
            fx, main_wt = make_repo(tmp_root)
            wt = add_worktree(fx, "5-widget", "feature/5-widget")
            fx.write_cache_file(wt, {
                "issue": 5,
                "branch": "feature/5-widget",
                "title": payload,
                "url": payload,
                "state": payload,
                "tracker": payload,
                "repo": payload,
            })
            home = home_for(tmp_root)
            flows_root = home / ".claude" / "flows" / main_wt.name
            filled = fill_placeholders(block, flows_root, main_wt)
            res = run_block(filled, wt, home)
            t("injected newline payload from the cache does not reach stdout",
              "INJECTED_PAYLOAD" not in res.stdout, f"stdout={res.stdout!r}")
            t("injected newline payload from the cache does not reach stderr",
              "INJECTED_PAYLOAD" not in res.stderr, f"stderr={res.stderr!r}")

    h.summarize_and_exit()
