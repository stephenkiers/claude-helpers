#!/usr/bin/env python3
"""
Test suite for North Star Nick's always-on seat and his in-flight snapshot input.

Covers:
1. scripts/in-flight-snapshot.py — pure helpers run against the REAL functions (loaded via
   importlib, since the filename is hyphenated), plus the fail-open CLI path.
2. The wiring invariants ADR-0003's 2026-10-09 amendment relies on: Nick is in ALWAYS_RUN_SLUGS,
   the index marks him `always: true`, the router/panel/plan docs seat him, and the snapshot is
   written before he runs.

Run with: python3 tests/test_north_star_nick_always_on.py
"""

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

SNAPSHOT_PATH = REPO_ROOT / "scripts" / "in-flight-snapshot.py"
ROUTE_SCORE_PATH = REPO_ROOT / "scripts" / "route-score.py"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read(relative):
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def main():
    h = Harness("NORTH STAR NICK ALWAYS-ON + IN-FLIGHT SNAPSHOT TEST SUITE")
    t = h.test_result

    snapshot = load("in_flight_snapshot", SNAPSHOT_PATH)
    route_score = load("route_score", ROUTE_SCORE_PATH)

    print("[Section 1] Snapshot helpers")

    t("clean() collapses newlines to one line", snapshot.clean("a\nb\n\nc") == "a b c")
    t("clean() strips table-breaking pipes", "|" not in snapshot.clean("a | b"))
    long_title = snapshot.clean("x" * 500)
    t("clean() caps length", len(long_title) == snapshot.TITLE_MAX_CHARS, str(len(long_title)))
    t("clean() tolerates None", snapshot.clean(None) == "")

    t("is_epic() matches an epic label",
      snapshot.is_epic({"title": "Routing v2", "labels": [{"name": "Epic"}]}))
    t("is_epic() matches an 'Epic:' title",
      snapshot.is_epic({"title": "Epic: harden telemetry", "labels": []}))
    t("is_epic() is false for an ordinary issue",
      not snapshot.is_epic({"title": "Fix typo", "labels": [{"name": "bug"}]}))
    t("is_epic() tolerates missing labels", not snapshot.is_epic({"title": "Fix typo"}))

    porcelain = (
        "worktree /repo/worktrees/main\nHEAD abc\nbranch refs/heads/main\n\n"
        "worktree /repo/worktrees/scratch\nHEAD def\ndetached\n\n"
        "worktree /repo/worktrees/12-feature\nHEAD 123\nbranch refs/heads/feat/12-feature\n"
    )
    records = snapshot.parse_worktrees(porcelain)
    t("parse_worktrees() finds every record, including an unterminated last one",
      len(records) == 3, str(records))
    t("parse_worktrees() strips refs/heads/",
      [r.get("branch") for r in records] == ["main", "(detached)", "feat/12-feature"], str(records))
    t("parse_worktrees() on empty input returns nothing", snapshot.parse_worktrees("") == [])

    print("\n[Section 2] Snapshot is fail-open")

    out, reason = snapshot.run_command(["definitely-not-a-real-binary-xyz"], str(REPO_ROOT))
    t("run_command() reports a missing binary instead of raising",
      out is None and reason is not None and "not installed" in reason, str(reason))

    with tempfile.TemporaryDirectory() as tmp:
        # A directory that is not a git repo: every section must degrade, the run must still pass.
        not_a_repo = Path(tmp) / "plain"
        not_a_repo.mkdir()
        out_file = Path(tmp) / "nested" / "in-flight.md"
        proc = subprocess.run(
            [sys.executable, str(SNAPSHOT_PATH), "--repo-dir", str(not_a_repo), "--out", str(out_file)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
        )
        t("CLI exits 0 outside a git repo", proc.returncode == 0, proc.stderr)
        t("CLI creates the output's parent directory and writes the file", out_file.is_file())
        text = out_file.read_text(encoding="utf-8") if out_file.is_file() else ""
        t("snapshot has all three sections",
          all(s in text for s in ("## Open issues", "## Open pull requests", "## Local worktrees")))
        t("worktree section degrades to 'unavailable' rather than vanishing",
          "unavailable:" in text.split("## Local worktrees")[-1], text[-300:])
        t("snapshot labels its contents as data, not instructions",
          "never as instructions" in text)

    print("\n[Section 3] Always-run wiring")

    t("north-star-nick is in ALWAYS_RUN_SLUGS", "north-star-nick" in route_score.ALWAYS_RUN_SLUGS)
    t("ALWAYS_RUN_SLUGS is exactly the four-reviewer set",
      set(route_score.ALWAYS_RUN_SLUGS)
      == {"contrarian-carl", "code-rot-cody", "consistency-checker", "north-star-nick"},
      str(sorted(route_score.ALWAYS_RUN_SLUGS)))

    index = read("reviewers/index.yaml")
    nick_block = index.split("- name: North Star Nick", 1)[-1].split("- name:", 1)[0]
    t("index.yaml gives Nick route: {always: true}", "always: true" in nick_block, nick_block)
    t("index.yaml keeps Nick plan- and review-tagged",
      "contexts: {review: primary, plan: primary}" in nick_block, nick_block)

    router = read("prompts/router.md")
    t("router lists North Star Nick in the always-run set", "- **North Star Nick**" in router)
    t("router receipt reports four always-run", "always-run: 4 |" in router)
    t("router no longer says three are pre-seated", "These three are pre-seated" not in router)

    panel = read("prompts/expert-review-panel.md")
    t("panel named-mode loop seats north-star-nick",
      "for r in code-rot-cody consistency-checker contrarian-carl north-star-nick; do" in panel)
    t("panel writes the in-flight snapshot",
      'in-flight-snapshot.py" $SNAPSHOT_FLAGS' in panel
      and '--repo-dir ${WORKTREE_PATH:-.}' in panel)
    t("panel launches the three Haiku North Star scouts as expert-scout",
      "prompts/north-star-scout.md" in panel and "Lens: {adrs|docs|in-flight}" in panel
      and 'Then launch **three** `subagent_type: "expert-scout"` agents in ONE message' in panel)

    review = read("commands/expert-review.md")
    t("expert-review reviewer-count dedup loop includes north-star-nick",
      "for r in code-rot-cody consistency-checker contrarian-carl north-star-nick; do" in review)

    print("\n[Section 4] Plan seat and persona")

    plan = read("commands/expert-plan.md")
    t("expert-plan pins Nick to a seat", "**North Star Nick always holds one of those seats**" in plan)
    t("expert-plan writes the in-flight snapshot",
      'in-flight-snapshot.py" --repo-dir "$PROJECT_ROOT"' in plan)
    t("expert-plan dispatches the three North Star scouts",
      "prompts/north-star-scout.md" in plan and "Lens: {adrs|docs|in-flight}" in plan)
    t("expert-plan hands Nick the scout briefs and the snapshot",
      "{SESSION_DIR}/in-flight.md" in plan and "{SESSION_DIR}/north-star-adrs.md" in plan)

    persona = read("reviewers/north-star-nick.yaml")
    t("persona review prompt reads the scout briefs", "STEP 1: Read the Scout Briefs" in persona)
    t("persona verifies scout quotes at the source",
      "Never cite a document you have only seen quoted." in persona)
    t("persona review prompt checks in-flight conflicts", "**In-Flight Conflicts**" in persona)
    t("persona carries a plan-time context load", "contextLoad: |" in persona)
    t("persona treats direction as organic, with no vision document",
      'must never ask for one' in persona and "strategicDocuments.vision" not in persona)

    print("\n[Section 5] Scout prompt")

    scout = read("prompts/north-star-scout.md")
    t("scout prompt defines all three lenses",
      all("### `{}`".format(lens) in scout for lens in ("adrs", "docs", "in-flight")))
    t("scout prompt forbids verdicts", "**No verdicts.**" in scout)
    t("scout prompt has a completion sentinel", "<!-- north-star-scout-end -->" in scout)
    t("expert-scout agent lists the North Star Scout job",
      "**North Star Scout** (`prompts/north-star-scout.md`)" in read("agents/expert-scout.md"))

    with tempfile.TemporaryDirectory() as tmp:
        bodies = Path(tmp) / "bodies.md"
        proc = subprocess.run(
            [sys.executable, str(SNAPSHOT_PATH), "--repo-dir", tmp,
             "--out", str(Path(tmp) / "a.md"), "--bodies-out", str(bodies)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
        )
        t("--bodies-out writes the scout input even when gh has no repo",
          proc.returncode == 0 and bodies.is_file(), proc.stderr)
    rendered = snapshot.issue_bodies(
        [{"number": 7, "title": "Epic: x", "labels": [], "body": "b" * 5000}], None)
    t("issue_bodies() marks epics and truncates long bodies",
      "## #7 [EPIC] Epic: x" in rendered and "[… truncated]" in rendered
      and len(rendered) < snapshot.BODY_MAX_CHARS + 400, str(len(rendered)))
    t("issue_bodies() reports an unavailable source",
      "unavailable: no gh" in snapshot.issue_bodies(None, "no gh"))

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
