#!/usr/bin/env python3
"""
Structural tests for the flow-mode cache refresh in commands/track-and-start.md
(#341 item 7). When /expert-flow runs its track step against an issue whose
worktree already exists, the reuse path (Pivot to existing, Step 4-pre reuse)
must write the ticket worktree's .claude/github-cache.json and the project
issue cache ($CACHE_FILE) as concrete commands, not as prose.

Run with: python3 tests/test_track_and_start_cache_flow_mode.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

DOC = REPO_ROOT / "commands" / "track-and-start.md"


def section(text, start_marker, end_marker):
    """Return text between two markers ('' if either is missing)."""
    s = text.find(start_marker)
    if s < 0:
        return ""
    e = text.find(end_marker, s + len(start_marker))
    return text[s:e] if e >= 0 else text[s:]


def main():
    h = Harness("TRACK-AND-START FLOW-MODE CACHE REFRESH TEST SUITE")
    t = h.test_result
    text = DOC.read_text()

    pivot = section(text, '### "Pivot to existing" Flow', "## Branch Naming")
    t("Pivot-to-existing flow section is present", pivot != "")

    t("reuse path writes the ticket worktree github-cache.json to an explicit path",
      'WT_CACHE="${REUSE_WORKTREE_PATH}/.claude/github-cache.json"' in pivot)
    t("reuse path merges the branch from git into the worktree github-cache.json",
      '--arg branch "$REUSE_BRANCH"' in pivot)
    t("reuse path writes the worktree github-cache.json via temp file and mv",
      'printf \'%s\' "$EXISTING" | jq' in pivot
      and 'mv "$TMP" "$WT_CACHE"' in pivot)
    t("reuse path writes the new plan as the worktree cache issue body",
      '--arg body "$NEW_PLAN"' in pivot and "body: $body" in pivot)
    t("reuse path preserves the pr section via the . + merge",
      "'. + {branch: $branch, issue:" in pivot)

    t("project issue cache write is a concrete jq command keyed by issue number",
      'jq --arg num "$EXISTING_ISSUE_NUM" --arg body "$NEW_PLAN"' in pivot
      and ".issues[$num].body = $body" in pivot)
    t("project issue cache write goes through a temp file and mv",
      'mv "$TMP" "$CACHE_FILE"' in pivot)

    t("reuse-only cache write is gated on the reuse case",
      pivot.find("Reuse case only") >= 0
      and pivot.find("Reuse case only") < pivot.find('WT_CACHE="${REUSE_WORKTREE_PATH}'))

    t("project issue cache read uses [ -s ] guard for non-zero files",
      '[ -s "$CACHE_FILE" ]' in pivot)
    t("worktree github-cache.json read uses [ -s ] guard for non-zero files",
      '[ -s "$WT_CACHE" ]' in pivot)
    t("temp file write is guarded with [ -s ] before both mv operations",
      pivot.count('[ -s "$TMP" ]') >= 2)

    t("no truncating redirect onto a cache path in the pivot section",
      re.search(r">\s*\"?\$\{?(CACHE_FILE|WT_CACHE)", pivot) is None)
    t("no bare positional token in the pivot section",
      re.search(r"\$[0-9]", pivot) is None)
    t("no echo of a variable piped into jq in the pivot section",
      re.search(r"echo\s+\"\$[A-Za-z_{][^\"]*\"\s*\|\s*jq", pivot) is None)
    t("no JSON interpolated into an --argjson string literal in the pivot section",
      re.search(r"--argjson\s+\w+\s+\"\{", pivot) is None)

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
