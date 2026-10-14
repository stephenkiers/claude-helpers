#!/usr/bin/env python3
"""
Structural tests for commands/track-and-start.md: the --worktree-only flag and
--issue existing-worktree reuse.

Run with: python3 tests/test_track_and_start_worktree_only.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

DOC = REPO_ROOT / "commands" / "track-and-start.md"
POSITIONAL_ZERO = "$" + "0"


def section(text, start_marker, end_marker):
    """Return text between two markers ('' if either is missing)."""
    s = text.find(start_marker)
    if s < 0:
        return ""
    e = text.find(end_marker, s + len(start_marker))
    return text[s:e] if e >= 0 else text[s:]


def main():
    h = Harness("TRACK-AND-START --worktree-only / WORKTREE REUSE TEST SUITE")
    t = h.test_result
    text = DOC.read_text()

    frontmatter = text.split("---", 2)[1]
    t("frontmatter argument-hint documents --worktree-only",
      re.search(r"^argument-hint:.*--worktree-only", frontmatter, re.M) is not None)
    t("frontmatter argument-hint still documents --issue and --plan-file",
      "--issue" in frontmatter and "--plan-file" in frontmatter)

    flags = section(text, "## Flags", "## Behavior")
    t("Flags list documents --worktree-only", "- `--worktree-only`" in flags)
    t("Flags list says it requires --issue and rejects plan/positional args",
      "--issue" in flags and "no plan file" in flags and "positional" in flags)
    t("Flags list says it never mutates the issue", "never mutates the issue" in flags)

    parsing = section(text, "## Argument Parsing", "**Known limitation:**")
    t("parsing block strips --worktree-only into WORKTREE_ONLY_FLAG",
      "WORKTREE_ONLY_FLAG=1" in parsing and "--worktree-only" in parsing
      and re.search(r"sed -E '[^']*--worktree-only", parsing) is not None)
    t("parsing block echoes WORKTREE_ONLY_FLAG", "echo \"WORKTREE_ONLY_FLAG=" in parsing)

    dispatch = section(text, "**Entry mode dispatch:**", "### `--plan-file` dispatch")
    t("dispatch table has a worktree-only row before branch 0",
      "`worktree-only`" in dispatch
      and dispatch.find("`worktree-only`") < dispatch.find("| 0 |"))
    chain = section(text, "Then assign `ENTRY_MODE`", "```bash\n# After you've made")
    t("ordered chain handles W before 0 (single chain, no independent ifs)",
      chain.find("W. If `$WORKTREE_ONLY_FLAG`") >= 0
      and chain.find("W. If `$WORKTREE_ONLY_FLAG`") < chain.find("0. Else if `$PLAN_FILE_FLAG`"))
    t("validation requires --issue and rejects ticket ids, --plan-file, positionals",
      "--worktree-only requires --issue" in text
      and "not a tracker ticket ID" in text
      and "cannot be combined with --plan-file" in text
      and "takes no positional arguments" in text)

    flow = section(text, "## `--worktree-only` Flow", "## Local Plan Mode")
    t("--worktree-only Flow section exists", bool(flow))
    t("flow states it never mutates the issue",
      "Never mutates the issue" in flow and "must not call `gh issue create`" in flow
      and "`gh issue comment`" in flow and "`gh issue edit`" in flow)
    t("flow never actually runs a mutating gh command",
      not re.search(r"^\s*gh issue (create|comment|edit)", flow, re.M))
    t("flow validates issue is open via gh issue view",
      "gh issue view" in flow and '!= "OPEN"' in flow)
    t("flow derives branch via track.py helpers",
      "infer_type" in flow and "slugify" in flow and "build_branch_name" in flow)
    t("flow has nested-path and main-worktree fail-closed checks",
      'dirname "$WORKTREE_PATH"' in flow and '"$MAIN_WORKTREE"/*' in flow)
    t("flow fetches then creates worktree from default branch",
      flow.find("git fetch origin") >= 0
      and flow.find("git fetch origin") < flow.find("git worktree add")
      and 'origin/${DEFAULT_BASE}' in flow)
    t("flow writes per-worktree github-cache.json",
      ".claude/github-cache.json" in flow)
    t("flow refuses when a worktree for <N>- exists with the standard message",
      "Worktree already exists at" in flow and '"${ISSUE_FLAG}-"*' in flow)
    t("flow handoff prints **Branch:** and **Worktree:** lines",
      "**Branch:** `<branch-name>`" in flow and "**Worktree:** `<worktree-path>`" in flow)
    t("flow does not touch the project issue cache body",
      "$CACHE_FILE" in flow and "jq" in flow
      and not re.search(r">\s*\"?\$CACHE_FILE", flow))

    pre = section(text, "### Step 4-pre", "### Step 4a")
    t("Step 4-pre has the existing-worktree reuse rule",
      "Existing worktree reuse" in pre and "Exactly one" in pre and "reuse it" in pre)
    t("reuse reads the branch from git, never guesses",
      "git -C \"$REUSE_WORKTREE_PATH\" branch --show-current" in pre and "never guess" in pre)
    t("reuse skips creation but keeps archive-then-replace and cache refresh",
      "Skip branch and worktree creation" in pre
      and "archive-then-replace" in pre and "github-cache.json" in pre)
    t("Pivot-to-existing flow points at the reuse rule",
      "Existing worktree reuse" in section(text, '### "Pivot to existing" Flow', "## Branch Naming"))

    row = next((ln for ln in text.splitlines() if ln.startswith("| Worktree already exists")), "")
    t("error table 'Worktree already exists' row says when it applies",
      "no `--issue`" in row and "different issue" in row and "reused" in row)

    t("no literal positional-zero token in the doc", POSITIONAL_ZERO not in text)
    t("no echo \"$VAR\" | jq in the doc",
      re.search(r"echo\s+\"\$[A-Za-z_{][^\"]*\"\s*\|\s*jq", text) is None)
    t("no JSON interpolated into --argjson string literal",
      re.search(r"--argjson\s+\w+\s+\"\{", text) is None)

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
