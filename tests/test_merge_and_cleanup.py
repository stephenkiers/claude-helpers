#!/usr/bin/env python3
"""
Test suite for merge-and-cleanup command (feature #68, updated for #92, rewritten for #88).

Issue #88 (ADR-0013 Phase 2) ported PR resolution, the push gate, the merge gate, and the
double-merge guard out of inline bash in `commands/merge-and-cleanup.md` and into
`scripts/workflow/merge.py` / `scripts/workflow/cli.py`, which have their own test coverage
(`test_workflow_merge.py`, `test_workflow_merge_integration.py`). This suite now only checks the
doc-content properties that still belong to the `.md` wrapper itself:
1. No worktree/branch teardown verbs; `rm` only against the command's own /tmp state dir
2. References /cleanup and passes absolute path ($WT)
3. Does not re-encode stack logic (per ADR-0011)
4. Frontmatter allowed-tools excludes git push; any Bash(rm:*) grant is documented as /tmp-scoped
5. Delegates PR/worktree resolution and the push gate to `cli.py merge plan`
6. Delegates the merge gate to `cli.py merge apply`, checking its exit code before proceeding
7. Merge gate (Phase 3) comes after PR/worktree resolution (Phase 0 & 1)

Run with: python3 tests/test_merge_and_cleanup.py
"""

import re
import shlex
from _test_harness import REPO_ROOT, Harness

COMMANDS_DIR = REPO_ROOT / "commands"
MERGE_FILE = COMMANDS_DIR / "merge-and-cleanup.md"


def read(path) -> str:
    """Return a file's text, or empty string if missing."""
    try:
        return path.read_text()
    except OSError:
        return ""


def extract_bash_blocks(markdown_text: str) -> list[tuple[int, str]]:
    """
    Extract all bash code block contents from markdown.
    Returns a list of (start_line_num, block_text) tuples.
    """
    blocks = []
    pattern = r"```bash\n(.*?)\n```"
    for match in re.finditer(pattern, markdown_text, re.DOTALL):
        start_pos = match.start()
        line_num = markdown_text[:start_pos].count("\n") + 1
        block_text = match.group(1)
        blocks.append((line_num, block_text))
    return blocks


def get_byte_index(text: str, section_name: str) -> int:
    """Return the byte offset of a top-level heading, or -1 if not found."""
    pattern = rf"^### {re.escape(section_name)}"
    match = re.search(pattern, text, re.MULTILINE)
    return match.start() if match else -1


def active_command_lines(block_text: str) -> list[str]:
    """
    Return lines that execute a command — not comments, blank lines, or
    echo/printf string arguments (those are emit-only text the user pastes,
    not invocations the block runs).
    """
    active = []
    for line in block_text.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.match(r"(echo|printf)\b", stripped):
            continue
        active.append(line)
    return active


MERGE = read(MERGE_FILE)

h = Harness("MERGE-AND-CLEANUP TEST SUITE (FEATURE #68, updated for #88)")
t = h.test_result

# ============================================================================
# TEST 1: No destructive teardown verbs in active command lines
# ============================================================================
print("[Test 1] No worktree/branch teardown; rm confined to the /tmp state dir")

# Worktree/branch teardown is owned by /cleanup and must never appear here (ADR-0011).
DESTRUCTIVE_VERBS = ["worktree remove", "branch -D"]

# `rm` is permitted, but ONLY against this command's own PR-scoped /tmp state directory.
# Anything else -- a worktree path, $WT, a repo path, a bare glob -- is a teardown this
# command must delegate, so the target is checked rather than the verb being banned outright.
STATE_DIR_TARGET = re.compile(r'^"?\$MC_STATE_DIR(?:/[^"]*)?"?$|^"?/tmp/merge-and-cleanup\.pr-')


def rm_targets(line: str) -> list[str]:
    """
    Return the non-flag operands of an `rm` invocation on this line.

    Checked per-operand, not per-line: `rm -rf "$MC_STATE_DIR" "$WT"` must be a violation,
    and a line-level "does the state dir appear anywhere" substring test would pass it.
    """
    tokens = shlex.split(line.split("rm", 1)[1], posix=False)
    return [tok for tok in tokens if not tok.startswith("-")]

bash_blocks = extract_bash_blocks(MERGE)
violations = []

for line_num, block_text in bash_blocks:
    for line in active_command_lines(block_text):
        for verb in DESTRUCTIVE_VERBS:
            if verb in line:
                violations.append(f"line {line_num}: '{verb}'")
        if re.search(r"\brm\b", line):
            targets = rm_targets(line)
            stray = [tk for tk in targets if not STATE_DIR_TARGET.search(tk)]
            if stray or not targets:
                violations.append(
                    f"line {line_num}: 'rm' targets outside the /tmp state dir "
                    f"({stray or 'no operands'}): {line.strip()}")

t("No destructive verbs as active commands",
  len(violations) == 0,
  f"found {len(violations)} violations: {', '.join(violations)}" if violations else "")

print()

# ============================================================================
# TEST 2: References /cleanup and passes absolute path
# ============================================================================
print("[Test 2] References /cleanup skill with absolute path ($WT)")

has_cleanup_mention = "/cleanup" in MERGE
t("Phase 4 mentions '/cleanup'",
  has_cleanup_mention,
  "expected '/cleanup' referenced in Phase 4")

has_skill_mention = "Skill" in MERGE
t("Frontmatter or invocation mentions Skill tool",
  has_skill_mention,
  "expected 'Skill' tool mentioned for invoking /cleanup")

has_wt_reference = "$WT" in MERGE
t("Phase 4 passes $WT (absolute path) to /cleanup",
  has_wt_reference,
  "expected $WT variable passed to /cleanup invocation")

print()

# ============================================================================
# TEST 3: No stack logic re-encoded (ADR-0011)
# ============================================================================
print("[Test 3] No stack logic re-encoded (ADR-0011)")

has_stack_sync = "gh stack sync" in MERGE
t("Does NOT contain 'gh stack sync'",
  not has_stack_sync,
  "stack sync belongs in /cleanup and prompts/worktree-reference.md only")

has_rebase_onto = "rebase --onto" in MERGE
t("Does NOT contain 'rebase --onto'",
  not has_rebase_onto,
  "rebase --onto (restacking) belongs in /cleanup and prompts/worktree-reference.md only")

has_worktree_parent_assignment = "WORKTREE_PARENT=" in MERGE
t("Does NOT compute WORKTREE_PARENT itself",
  not has_worktree_parent_assignment,
  "WORKTREE_PARENT is owned by Project Detection elsewhere, not this command")

has_project_root_assignment = "PROJECT_ROOT=" in MERGE
t("Does NOT compute PROJECT_ROOT itself",
  not has_project_root_assignment,
  "PROJECT_ROOT is owned by Project Detection elsewhere, not this command")

print()

# ============================================================================
# TEST 4: Frontmatter allowed-tools excludes destructive verbs
# ============================================================================
print("[Test 4] Frontmatter allowed-tools excludes Bash(git push:*); Bash(rm:*) is /tmp-scoped")

frontmatter_match = re.search(r"^---\n(.*?)\n---", MERGE, re.DOTALL | re.MULTILINE)
frontmatter = frontmatter_match.group(1) if frontmatter_match else ""

allowed_tools_match = re.search(r"allowed-tools:\s*(.*)$", frontmatter, re.MULTILINE)
allowed_tools = allowed_tools_match.group(1) if allowed_tools_match else ""

# Bash(rm:*) is granted so Phase 4 can free its own /tmp state dir. The grant itself is
# unscoped (Claude Code has no directory-prefix form for it), so the guardrail is that the
# doc must state the /tmp-only scope -- it cannot be widened silently without the claim.
has_rm_tool = "Bash(rm:*)" in allowed_tools
rm_scope_documented = "/tmp/merge-and-cleanup.pr-" in MERGE and re.search(
    r"`Bash\(rm:\*\)`[^\n]*scoped", MERGE)
t("Bash(rm:*) grant is documented as scoped to the /tmp state dir",
  (not has_rm_tool) or bool(rm_scope_documented),
  "allowed-tools grants Bash(rm:*) but the doc does not state it is scoped to "
  "/tmp/merge-and-cleanup.pr-* state dirs")

has_push_tool = "Bash(git push:*)" in allowed_tools or "Bash(git push" in allowed_tools
t("allowed-tools does NOT contain Bash(git push:*)",
  not has_push_tool,
  "git push delegated to user or /cleanup; should not be in allowed-tools")

has_cli_tool = "scripts.workflow.cli" in allowed_tools
t("allowed-tools grants the workflow CLI invocation",
  has_cli_tool,
  "expected 'Bash(python3 -m scripts.workflow.cli:*)' in allowed-tools")

print()

# ============================================================================
# TEST 5: Phase 0 & 1 delegates PR/worktree resolution and the push gate to
# `cli.py merge plan` — the mechanical parsing (awk exact-match, pull/<n> URL
# regex, cache-first path mode, mktemp+jq+mv cache backfill, worktree
# validity checks) now lives in scripts/workflow/merge.py, covered by
# test_workflow_merge.py and test_workflow_merge_integration.py.
# ============================================================================
print("[Test 5] Phase 0 & 1 delegates resolution + push gate to `cli.py merge plan`")

has_plan_call = "scripts.workflow.cli merge plan" in MERGE
t("Calls `python3 -m scripts.workflow.cli merge plan`",
  has_plan_call,
  "expected a call to 'scripts.workflow.cli merge plan' for PR/worktree resolution + push gate")

has_plan_error_check = bool(re.search(r"PLAN_RESULT\s*=\s*\$\?", MERGE)) or bool(
    re.search(r"if\s*\[\s*\$PLAN_RESULT\s*-ne\s*0\s*\]", MERGE)
)
t("Checks the plan call's exit code before proceeding",
  has_plan_error_check,
  "expected an exit-code check on the merge-plan call before extracting PR_NUM/WT")

has_blocking_check = "blocking_failures" in MERGE
t("Checks plan JSON for push-gate blocking_failures",
  has_blocking_check,
  "expected the plan JSON's 'blocking_failures' field to be checked before continuing")

print()

# ============================================================================
# TEST 6: Phase 3 delegates the merge gate (and its double-merge guard) to
# `cli.py merge apply`, and checks its result before reporting success.
# ============================================================================
print("[Test 6] Phase 3 delegates the merge gate to `cli.py merge apply`")

phase3_idx = get_byte_index(MERGE, "Phase 3")
t("Phase 3 heading exists",
  phase3_idx >= 0,
  "expected '### Phase 3' heading not found")

has_apply_call = "scripts.workflow.cli merge apply" in MERGE
t("Calls `python3 -m scripts.workflow.cli merge apply`",
  has_apply_call,
  "expected a call to 'scripts.workflow.cli merge apply' for the merge gate")

has_pr_merged_check = "pr_merged" in MERGE
t("Checks apply JSON's pr_merged field",
  has_pr_merged_check,
  "expected the apply JSON's 'pr_merged' field to be checked before reporting success")

# The double-merge guard itself (never call `gh pr merge --squash` after
# `just merge` succeeded) lives in merge.py's apply_merge(), not in this
# markdown file — assert the .md file does NOT reimplement it inline.
has_inline_gh_merge = bool(re.search(r"^\s*gh pr merge\b", MERGE, re.MULTILINE))
t("Does NOT reimplement `gh pr merge` inline (owned by merge.py's apply_merge)",
  not has_inline_gh_merge,
  "found an inline 'gh pr merge' call — the merge gate and its double-merge guard belong in "
  "scripts/workflow/merge.py, not re-encoded here")

print()

# ============================================================================
# TEST 7: Resolution (Phase 0 & 1) comes before the merge gate (Phase 3)
# ============================================================================
print("[Test 7] Resolution (Phase 0 & 1) comes before the merge gate (Phase 3)")

resolve_idx = get_byte_index(MERGE, "Phase 0")
t("Phase 0 & 1 heading exists",
  resolve_idx >= 0,
  "expected a '### Phase 0' heading not found")

if resolve_idx >= 0 and phase3_idx >= 0:
    t("Phase 0 & 1 byte-offset < Phase 3 byte-offset",
      resolve_idx < phase3_idx,
      "PR/worktree resolution and the push gate must come before the merge gate")

print()

h.summarize_and_exit()
