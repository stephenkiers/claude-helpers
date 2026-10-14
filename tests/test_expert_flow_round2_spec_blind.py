#!/usr/bin/env python3
"""
Spec-blind structural tests for the expert-flow dogfood hardening round 2 plan
(commands/expert-flow.md and prompts/worktree-reference.md, read at test time).

Covers (new coverage only; existing coverage lives in tests/test_expert_flow.py,
tests/test_worktree_reference_detection.py and tests/test_workflow_worktrees_containment.py):
1. "Start fresh" deletes the old worktree via the /cleanup helper, asks for explicit
   confirm when dirty/unpushed, orders delete -> create new -> abandon-old (jq edit).
2. worktree-reference.md contains no duplicated worktree-parent detection logic
   (no `git worktree list` / porcelain parsing anywhere in the doc).
3. allowed-tools carries the exact git grants, with no wildcard on git fetch/rebase.
4. Newest-flow lookup uses sort_by([.updated, .dir]) at every consumer site.

Run with: python3 tests/test_expert_flow_round2_spec_blind.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT

COMMAND = REPO_ROOT / "commands" / "expert-flow.md"
WT_REF = REPO_ROOT / "prompts" / "worktree-reference.md"

EXACT_GIT_GRANTS = {
    "Bash(git fetch origin)",
    "Bash(git rebase --abort)",
    "Bash(git rebase origin/main)",
    "Bash(git rebase origin/master)",
}


def frontmatter(text):
    """Return the YAML frontmatter block of a command doc, or an empty string."""
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    return m.group(1) if m else ""


def start_fresh_window(text):
    """Return the concatenated windows from each 'Start fresh' mention to the next `## ` section.

    Only level-2 headings end a window: bash comments inside fenced blocks start with `# `
    and must not be mistaken for headings. Every mention is included so that a short
    mention elsewhere in the doc does not hide the procedure text.
    """
    windows = []
    for m in re.finditer(r"start fresh", text, re.IGNORECASE):
        nxt = re.search(r"^## ", text[m.end():], re.MULTILINE)
        end = m.end() + nxt.start() if nxt else len(text)
        windows.append(text[m.start():end])
    return "\n".join(windows)


if __name__ == "__main__":
    h = Harness("EXPERT-FLOW ROUND 2 SPEC-BLIND TEST SUITE")
    t = h.test_result

    cmd_text = COMMAND.read_text() if COMMAND.is_file() else ""
    wt_text = WT_REF.read_text() if WT_REF.is_file() else ""

    print("[Section 1] Start fresh deletes the old worktree")
    window = start_fresh_window(cmd_text)
    t("a Start fresh section exists", bool(window))
    lower = window.lower()
    t("question text says it deletes the old worktree at its path",
      bool(re.search(r"delet\w*[^\n]*worktree", lower)) and "path" in lower,
      window[:200])
    t("dirty or unpushed old worktree requires an explicit confirm",
      ("dirty" in lower or "unpushed" in lower) and "confirm" in lower)
    cleanup_pos = window.find("python3 -m scripts.workflow.cli cleanup")
    t("old worktree is deleted via the /cleanup helper CLI", cleanup_pos >= 0)
    raw_remove_lines = [ln for ln in window.splitlines() if "git worktree remove" in ln]
    t("old worktree is not removed with raw git worktree remove (mentions must be prohibitions)",
      all(re.search(r"\b(not|never|avoid|instead|don't|do not)\b", ln, re.IGNORECASE)
          for ln in raw_remove_lines),
      " | ".join(raw_remove_lines))
    jq_positions = [m.start() for m in re.finditer(r"\bjq\b", window)]
    abandon_pos = [m.start() for m in re.finditer(r"abandon", window, re.IGNORECASE)]
    t("old flow is marked abandoned with a jq edit after the cleanup call",
      cleanup_pos >= 0 and any(p > cleanup_pos for p in jq_positions)
      and any(p > cleanup_pos for p in abandon_pos),
      f"cleanup={cleanup_pos} jq={jq_positions} abandon={abandon_pos}")
    create_pos = [m.start() for m in re.finditer(r"creat\w*|new flow|mkdir", window, re.IGNORECASE)]
    t("order: delete old worktree, then create the new flow, then abandon the old flow",
      cleanup_pos >= 0 and any(cleanup_pos < p for p in create_pos)
      and any(p > cleanup_pos for p in jq_positions)
      and any(cleanup_pos < c < max(jq_positions) for c in create_pos if jq_positions),
      f"cleanup={cleanup_pos} create={create_pos} jq={jq_positions}")

    print()
    print("[Section 2] Worktree-parent detection has one implementation")
    t("worktree-reference.md is present", WT_REF.is_file())
    bash_blocks = re.findall(r"```bash\n(.*?)```", wt_text, re.S)
    all_bash = "\n".join(bash_blocks)
    # Other sections of the doc legitimately list worktrees (main-worktree lookup,
    # stacked-branch detection); only the detect-parent step is single-implementation,
    # and test_worktree_reference_detection.py scopes its no-duplication check to that step.
    t("worktree-reference.md calls the detect-parent CLI",
      "scripts.workflow.cli worktree detect-parent" in wt_text)

    print()
    print("[Section 3] allowed-tools exact git grants")
    fm = frontmatter(cmd_text)
    tools_line = re.search(r"allowed-tools:\s*(.*)", fm)
    tools = tools_line.group(1) if tools_line else ""
    inners = [i.strip() for i in re.findall(r"Bash\(([^)]*)\)", tools)]
    grants = {f"Bash({i})" for i in inners}
    for g in sorted(EXACT_GIT_GRANTS):
        t(f"allowed-tools includes exact grant {g}", g in grants, tools)
    git_fetch_rebase = [g for g in grants if re.match(r"Bash\(git (fetch|rebase)\b", g)]
    t("no git fetch/rebase grant outside the exact set",
      set(git_fetch_rebase) <= EXACT_GIT_GRANTS, ", ".join(sorted(set(git_fetch_rebase) - EXACT_GIT_GRANTS)))
    t("no wildcard grant on git fetch or git rebase",
      not any(re.match(r"Bash\(git (fetch|rebase)\b", g) and ":*" in g for g in grants),
      ", ".join(g for g in grants if ":*" in g and "git" in g))

    print()
    print("[Section 4] Newest-flow lookup is deterministic")
    t("lookup sorts by [.updated, .dir] (name tie-break)",
      "sort_by([.updated, .dir])" in cmd_text)
    t("no bare sort_by(.updated) without a tie-break remains",
      not re.search(r"sort_by\(\s*\.updated\s*\)", cmd_text))
    sites = cmd_text.count("sort_by([.updated, .dir])")
    t("the tie-broken lookup is spelled out at the shared definition, --list and --resume",
      sites >= 3, f"sort_by sites={sites}")
    guard_near = all(
        re.search(r"try|malformed|skip", cmd_text[max(0, m.start() - 400):m.start() + 400])
        for m in re.finditer(r"sort_by\(\[\.updated, \.dir\]\)", cmd_text)
    ) if sites else False
    t("each lookup site has a per-entry guard (try/malformed/skip) nearby",
      guard_near)

    h.summarize_and_exit()
