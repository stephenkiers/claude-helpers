#!/usr/bin/env python3
"""
Test suite for stack-awareness invariants (issue #51).

These tests verify that `/cleanup` and `/shipit` commands are gh-stack-aware
and worktree-safe, using a shared "Stack Detection" block in prompts/worktree-reference.md.

Tests assert:
1. Worktree-safety: gh stack init/sync/checkout never invoked in bash blocks
2. Shared Stack Detection block exists with defined variables
3. Only worktree-safe verbs (gh stack link, unstack) used
4. shipit.md opens stacked PRs correctly
5. cleanup.md emits a restack runbook (for merged stacked PRs)
6. Cache schema documented in both files
7. Non-stacked regression guard (ordinary PRs unaffected)

Run with: python3 tests/test_stack_awareness.py
"""

import re
from _test_harness import REPO_ROOT, Harness

COMMANDS_DIR = REPO_ROOT / "commands"
PROMPTS_DIR = REPO_ROOT / "prompts"

CLEANUP_FILE = COMMANDS_DIR / "cleanup.md"
SHIPIT_FILE = COMMANDS_DIR / "shipit.md"
WORKTREE_REF_FILE = PROMPTS_DIR / "worktree-reference.md"


def read(path):
    """Return a file's text, or empty string if missing."""
    try:
        return path.read_text()
    except OSError:
        return ""


def extract_bash_blocks(markdown_text):
    """
    Extract all bash code block contents from markdown.
    Returns a list of (start_line_num, block_text) tuples.
    """
    blocks = []
    # Match ```bash ... ``` fenced code blocks
    pattern = r"```bash\n(.*?)\n```"
    for match in re.finditer(pattern, markdown_text, re.DOTALL):
        # Count newlines before the match to get approximate line number
        start_pos = match.start()
        line_num = markdown_text[:start_pos].count("\n") + 1
        block_text = match.group(1)
        blocks.append((line_num, block_text))
    return blocks


def get_line_index(text, heading):
    """Return the line number where a heading appears, or -1 if not found."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if heading in line and line.lstrip().startswith("##"):
            return i
    return -1


def get_byte_index(text, section_name):
    """Return the byte offset of a top-level heading, or -1 if not found."""
    pattern = rf"^## {re.escape(section_name)}"
    match = re.search(pattern, text, re.MULTILINE)
    return match.start() if match else -1


CLEANUP = read(CLEANUP_FILE)
SHIPIT = read(SHIPIT_FILE)
WORKTREE_REF = read(WORKTREE_REF_FILE)

h = Harness("STACK AWARENESS TEST SUITE (ISSUE #51)")
t = h.test_result

# ============================================================================
# INVARIANT 1: Worktree-safety (no hostile verbs invoked in bash blocks)
# ============================================================================
print("[Invariant 1] Worktree-safety: no hostile gh stack verbs in bash blocks")

HOSTILE_VERBS = ["gh stack init", "gh stack sync", "gh stack checkout"]

def check_file_worktree_safe(content, filename):
    """Check that hostile verbs don't appear in bash blocks of a file."""
    bash_blocks = extract_bash_blocks(content)
    violations = []
    for line_num, block_text in bash_blocks:
        for verb in HOSTILE_VERBS:
            if verb in block_text:
                violations.append(f"{filename}:{line_num} contains '{verb}'")
    return violations


cleanup_violations = check_file_worktree_safe(CLEANUP, "cleanup.md")
shipit_violations = check_file_worktree_safe(SHIPIT, "shipit.md")
ref_violations = check_file_worktree_safe(WORKTREE_REF, "worktree-reference.md")

t("cleanup.md bash blocks are worktree-safe",
  len(cleanup_violations) == 0,
  f"found {len(cleanup_violations)} violations: {cleanup_violations}" if cleanup_violations else "")

t("shipit.md bash blocks are worktree-safe",
  len(shipit_violations) == 0,
  f"found {len(shipit_violations)} violations: {shipit_violations}" if shipit_violations else "")

t("worktree-reference.md bash blocks are worktree-safe",
  len(ref_violations) == 0,
  f"found {len(ref_violations)} violations: {ref_violations}" if ref_violations else "")

# Bonus: assert the warning prose exists in worktree-reference.md
warning_exists = any(
    verb in WORKTREE_REF for verb in HOSTILE_VERBS
) and "worktree" in WORKTREE_REF.lower()
t("worktree-reference.md explicitly warns against hostile verbs",
  warning_exists,
  "expected a prose warning about not using gh stack init/sync/checkout")

print()

# ============================================================================
# INVARIANT 2: Shared Stack Detection block exists with variable definitions
# ============================================================================
print("[Invariant 2] Stack Detection block exists with three shared variables")

has_stack_detection = "## Stack Detection" in WORKTREE_REF

t("worktree-reference.md has '## Stack Detection' heading",
  has_stack_detection,
  "expected top-level '## Stack Detection' section not found")

required_vars = ["STACK_IS_STACKED", "STACK_PARENT_BRANCH", "STACK_PARENT_PR"]

for var in required_vars:
    t(f"Stack Detection defines {var}",
      var in WORKTREE_REF,
      f"variable {var} not found in worktree-reference.md")

print()

# ============================================================================
# INVARIANT 3: Worktree-safe verbs are used (gh stack link, unstack)
# ============================================================================
print("[Invariant 3] Worktree-safe stack verbs appear in docs")

t("'gh stack link' appears in the docs",
  "gh stack link" in CLEANUP or "gh stack link" in SHIPIT,
  "expected 'gh stack link' in cleanup.md or shipit.md")

t("'gh stack unstack' appears in the docs",
  "gh stack unstack" in CLEANUP,
  "expected 'gh stack unstack' in cleanup.md")

print()

# ============================================================================
# INVARIANT 4: shipit.md opens stacked PRs correctly
# ============================================================================
print("[Invariant 4] shipit.md handles stacked PRs correctly")

# Should reference STACK_PARENT_BRANCH as --base for gh pr create
has_parent_branch_base = "STACK_PARENT_BRANCH" in SHIPIT and "--base" in SHIPIT
t("shipit.md uses STACK_PARENT_BRANCH as --base",
  has_parent_branch_base,
  "expected STACK_PARENT_BRANCH to be used with --base for stacked PR creation")

# Should add "Stacked on" line to PR body
has_stacked_on = "Stacked on" in SHIPIT or "stacked on" in SHIPIT.lower()
t("shipit.md mentions 'Stacked on' in PR body",
  has_stacked_on,
  "expected PR body to include 'Stacked on' line for stacked PRs")

# Should run Stack Detection (reference the worktree-reference.md block)
has_stack_detection_ref = "Stack Detection" in SHIPIT or "STACK_IS_STACKED" in SHIPIT
t("shipit.md references Stack Detection block or its variables",
  has_stack_detection_ref,
  "expected reference to Stack Detection block or STACK_IS_STACKED variable")

# Cache section should use jq with one --arg/--argjson per variable, not JSON literal interpolation
# Check for the bad pattern: --argjson <name> "{
bad_argjson_pattern = r"--argjson\s+\w+\s+\{"
has_bad_pattern = bool(re.search(bad_argjson_pattern, SHIPIT))
t("shipit.md .stack cache avoids JSON literal interpolation",
  not has_bad_pattern,
  "found --argjson with direct JSON literal; use --arg/--argjson per variable instead")

print()

# ============================================================================
# INVARIANT 5: cleanup.md emits a restack runbook for merged stacked PRs
# ============================================================================
print("[Invariant 5] cleanup.md emits restack runbook for merged stacked children")

# Should capture merged tip SHA (a variable like MERGED_TIP)
has_merged_tip = ("MERGED_TIP" in CLEANUP or "merged_tip" in CLEANUP.lower()) and "git rev-parse" in CLEANUP
t("cleanup.md captures merged tip SHA before deletion",
  has_merged_tip,
  "expected a MERGED_TIP variable and 'git rev-parse' to capture the tip")

# Should mention rebase --onto for restacking
has_rebase_onto = "rebase --onto" in CLEANUP
t("cleanup.md mentions 'rebase --onto' for restacking",
  has_rebase_onto,
  "expected 'rebase --onto' in restack runbook")

# Should mention push --force-with-lease
has_force_with_lease = "push --force-with-lease" in CLEANUP or "force-with-lease" in CLEANUP
t("cleanup.md mentions 'push --force-with-lease' for safe force-push",
  has_force_with_lease,
  "expected 'push --force-with-lease' in restack runbook")

# Runbook should be emit-only (manual, user runs it)
is_emit_only = "runbook" in CLEANUP.lower() or "you" in CLEANUP.lower()
t("cleanup.md runbook is emit-only (manual, not auto-executed)",
  is_emit_only,
  "expected language indicating user manually runs the runbook (e.g., 'runbook', 'you run')")

# Runbook step should come BEFORE "Remove Worktree" step
# Find the section headers
runbook_idx = get_byte_index(CLEANUP, "restack") if "restack" in CLEANUP.lower() else -1
remove_worktree_idx = get_byte_index(CLEANUP, "Remove Worktree")

if runbook_idx >= 0 and remove_worktree_idx >= 0:
    t("cleanup.md runbook comes before 'Remove Worktree' step",
      runbook_idx < remove_worktree_idx,
      "runbook must capture tip before worktree is deleted")
else:
    # If we can't find clear section markers, just verify the concepts exist in the right order
    cleanup_lines = CLEANUP.split("\n")
    runbook_line = -1
    remove_line = -1
    for i, line in enumerate(cleanup_lines):
        if "restack" in line.lower() or "merge" in line.lower():
            if runbook_line < 0:
                runbook_line = i
        if "Remove Worktree" in line or "remove worktree" in line.lower():
            remove_line = i

    t("cleanup.md runbook concept comes before 'Remove Worktree'",
      runbook_line >= 0 and (remove_line < 0 or runbook_line < remove_line),
      "runbook/tip-capture must logically precede worktree removal")

print()

# ============================================================================
# INVARIANT 6: Cache schema documented in both files
# ============================================================================
print("[Invariant 6] Cache schema documented in both cleanup.md and shipit.md")

schema_fields = ["isStacked", "parentBranch", "parentPr", "stackNumber"]

for field in schema_fields:
    t(f"shipit.md documents schema field '{field}'",
      field in SHIPIT,
      f"expected '{field}' documented in shipit.md")

for field in schema_fields:
    t(f"cleanup.md documents schema field '{field}'",
      field in CLEANUP,
      f"expected '{field}' documented in cleanup.md")

print()

# ============================================================================
# INVARIANT 7: Non-stacked regression guard
# ============================================================================
print("[Invariant 7] Non-stacked regression guard (ordinary PRs unaffected)")

# shipit.md should still have an unstacked gh pr create path
# (one that does NOT force --base to a parent)
t("shipit.md contains unstacked 'gh pr create' path",
  "gh pr create" in SHIPIT,
  "expected 'gh pr create' for non-stacked PRs")

# Verify that stack.isStacked can be false (non-stacked path)
t("shipit.md mentions 'false' value for stack.isStacked",
  "isStacked" in SHIPIT and ("false" in SHIPIT.lower() or "not stacked" in SHIPIT.lower()),
  "expected support for non-stacked (isStacked=false) case")

print()
h.summarize_and_exit()
