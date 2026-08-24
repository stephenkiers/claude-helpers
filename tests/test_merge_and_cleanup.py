#!/usr/bin/env python3
"""
Test suite for merge-and-cleanup command (feature #68).

These tests verify that `commands/merge-and-cleanup.md` correctly:
1. Uses exact branch matching for worktree resolution (never substring-match)
2. Orders phases correctly (push gate before merge gate)
3. Avoids destructive teardown verbs (delegated to /cleanup)
4. Runs merge in a subshell (never bare cd)
5. References /cleanup and passes absolute path
6. Does not re-encode stack logic (per ADR-0011)
7. Excludes destructive verbs from allowed-tools
8. Guards gh pr merge to avoid double-merge after just merge

Run with: python3 tests/test_merge_and_cleanup.py
"""

import re
from _test_harness import REPO_ROOT, Harness

COMMANDS_DIR = REPO_ROOT / "commands"
MERGE_FILE = COMMANDS_DIR / "merge-and-cleanup.md"


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


def get_line_index(text: str, heading: str) -> int:
    """
    Return the line number where a heading appears, or -1 if not found.

    Matches top-level (###) section headings exactly.
    """
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.lstrip().startswith("###") and heading in line:
            return i
    return -1


def get_byte_index(text, section_name):
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
        # An echo/printf of a verb is emit-only (the runbook is paste-manual),
        # not an execution — skip it.
        if re.match(r"(echo|printf)\b", stripped):
            continue
        active.append(line)
    return active


MERGE = read(MERGE_FILE)

h = Harness("MERGE-AND-CLEANUP TEST SUITE (FEATURE #68)")
t = h.test_result

# ============================================================================
# TEST 1: Branch resolution never substring-matches
# ============================================================================
print("[Test 1] Branch resolution uses exact match (refs/heads/<branch>)")

# Phase 1 should use awk with exact matching on refs/heads/<branch>
has_exact_match = 'refs/heads/' in MERGE and '$0=="branch "b' in MERGE
t("Phase 1 uses exact refs/heads/ match in awk",
  has_exact_match,
  "expected awk with $0==\"branch \"b pattern for exact matching")

# Make sure there's no naive substring grep on branch name
has_naive_grep = bool(re.search(r"grep.*\$HEAD_REF", MERGE))
t("Phase 1 does NOT use naive substring grep on branch name",
  not has_naive_grep,
  "found grep -pattern matching branch name directly; use exact refs/heads/ match instead")

print()

# ============================================================================
# TEST 2: Push gate appears before merge gate (byte-index ordering)
# ============================================================================
print("[Test 2] Push gate (Phase 2) comes before merge gate (Phase 3)")

push_gate_idx = get_byte_index(MERGE, "Phase 2")
merge_gate_idx = get_byte_index(MERGE, "Phase 3")

t("Phase 2 (Push gate) heading exists",
  push_gate_idx >= 0,
  "expected '### Phase 2' heading not found")

t("Phase 3 (Merge gate) heading exists",
  merge_gate_idx >= 0,
  "expected '### Phase 3' heading not found")

if push_gate_idx >= 0 and merge_gate_idx >= 0:
    t("Push gate (Phase 2) byte-offset < merge gate (Phase 3)",
      push_gate_idx < merge_gate_idx,
      "push gate must come before merge gate")

print()

# ============================================================================
# TEST 3: No destructive teardown verbs in active command lines
# ============================================================================
print("[Test 3] No destructive teardown verbs (worktree remove, branch -D, rm -rf)")

DESTRUCTIVE_VERBS = ["worktree remove", "branch -D", "rm -rf"]

bash_blocks = extract_bash_blocks(MERGE)
violations = []

for line_num, block_text in bash_blocks:
    for line in active_command_lines(block_text):
        for verb in DESTRUCTIVE_VERBS:
            if verb in line:
                violations.append(f"line {line_num}: '{verb}'")

t("No destructive verbs as active commands",
  len(violations) == 0,
  f"found {len(violations)} violations: {', '.join(violations)}" if violations else "")

print()

# ============================================================================
# TEST 4: Merge runs in subshell, never bare cd
# ============================================================================
print("[Test 4] Merge runs in subshell (( cd ... )), never bare cd")

# Should have at least one subshell cd
has_subshell_cd = "( cd " in MERGE
t("At least one merge invocation uses subshell ( cd ... )",
  has_subshell_cd,
  "expected '( cd' pattern for running merge in subshell")

# Check no bare cd as active command line (outside subshell)
bare_cd_violations = []
for line_num, block_text in bash_blocks:
    for line in active_command_lines(block_text):
        # Bare cd is a line that starts with 'cd ' but is not part of a subshell
        # Look for lines that are exactly 'cd <path>' or start with 'cd '
        stripped = line.strip()
        if stripped.startswith("cd "):
            # Check if this line is wrapped in a subshell by looking at the raw block
            # If the line contains '( ' or is preceded by '(' on same line, it's safe
            if not ("(" in line and ")" in line):
                # Look at context: is it inside parentheses?
                # For safety, check if the full line has both ( and )
                if not re.search(r"\(\s*cd\s+", line):
                    bare_cd_violations.append(f"line {line_num}: '{stripped}'")

t("No bare cd outside subshell",
  len(bare_cd_violations) == 0,
  f"found bare cd: {', '.join(bare_cd_violations)}" if bare_cd_violations else "")

print()

# ============================================================================
# TEST 5: References /cleanup and passes absolute path
# ============================================================================
print("[Test 5] References /cleanup skill with absolute path ($WT)")

# Should mention /cleanup and Skill
has_cleanup_mention = "/cleanup" in MERGE
t("Phase 4 mentions '/cleanup'",
  has_cleanup_mention,
  "expected '/cleanup' referenced in Phase 4")

has_skill_mention = "Skill" in MERGE
t("Frontmatter or invocation mentions Skill tool",
  has_skill_mention,
  "expected 'Skill' tool mentioned for invoking /cleanup")

# WT should be passed to cleanup (it's the absolute path from git worktree list)
has_wt_reference = "$WT" in MERGE
t("Phase 4 passes $WT (absolute path) to /cleanup",
  has_wt_reference,
  "expected $WT variable passed to /cleanup invocation")

print()

# ============================================================================
# TEST 6: No stack logic re-encoded (ADR-0011)
# ============================================================================
print("[Test 6] No stack logic re-encoded (ADR-0011)")

has_stack_sync = "gh stack sync" in MERGE
t("Does NOT contain 'gh stack sync'",
  not has_stack_sync,
  "stack sync belongs in /cleanup and prompts/worktree-reference.md only")

has_rebase_onto = "rebase --onto" in MERGE
t("Does NOT contain 'rebase --onto'",
  not has_rebase_onto,
  "rebase --onto (restacking) belongs in /cleanup and prompts/worktree-reference.md only")

print()

# ============================================================================
# TEST 7: Frontmatter allowed-tools excludes destructive verbs
# ============================================================================
print("[Test 7] Frontmatter allowed-tools excludes Bash(rm:*) and Bash(git push:*)")

# Extract frontmatter
frontmatter_match = re.search(r"^---\n(.*?)\n---", MERGE, re.DOTALL | re.MULTILINE)
frontmatter = frontmatter_match.group(1) if frontmatter_match else ""

# Check allowed-tools line
allowed_tools_match = re.search(r"allowed-tools:\s*(.*)$", frontmatter, re.MULTILINE)
allowed_tools = allowed_tools_match.group(1) if allowed_tools_match else ""

has_rm_tool = "Bash(rm:*)" in allowed_tools or "Bash(rm" in allowed_tools
t("allowed-tools does NOT contain Bash(rm:*)",
  not has_rm_tool,
  "rm operations delegated to /cleanup; should not be in allowed-tools")

has_push_tool = "Bash(git push:*)" in allowed_tools or "Bash(git push" in allowed_tools
t("allowed-tools does NOT contain Bash(git push:*)",
  not has_push_tool,
  "git push delegated to user or /cleanup; should not be in allowed-tools")

print()

# ============================================================================
# TEST 8: "just merge" path never calls gh pr merge unconditionally
# ============================================================================
print("[Test 8] Guards gh pr merge to avoid double-merge after just merge")

# Find Phase 3 section and extract its bash block
phase3_idx = get_line_index(MERGE, "Phase 3")
t("Phase 3 heading found",
  phase3_idx >= 0,
  "could not find Phase 3 section")

if phase3_idx >= 0:
    # Extract Phase 3 bash block
    phase3_start_idx = phase3_idx + 1
    phase3_section = MERGE.split("\n")[phase3_start_idx:]
    phase3_text = "\n".join(phase3_section)

    # Find the bash block after Phase 3
    bash_match = re.search(r"```bash\n(.*?)\n```", phase3_text, re.DOTALL)
    if bash_match:
        phase3_bash = bash_match.group(1)

        # Should have a conditional guarding gh pr merge
        # Look for: if [ "$MERGE_GATE_USED" = "just merge" ]
        has_guard = 'MERGE_GATE_USED' in phase3_bash and 'just merge' in phase3_bash
        t("Phase 3 bash block uses MERGE_GATE_USED variable to guard paths",
          has_guard,
          "expected conditional branching on MERGE_GATE_USED to prevent double-merge")

        # The gh pr merge should be inside an else or if-not condition
        # Check for the conditional structure
        has_conditional_merge = re.search(
            r'if\s*\[\s*"\$MERGE_GATE_USED"\s*[!=]',
            phase3_bash
        ) or re.search(
            r'if\s*\[\s*-z\s*"\$MERGE_GATE_USED"',
            phase3_bash
        )
        t("gh pr merge is guarded by conditional (not unconditional)",
          has_conditional_merge,
          "gh pr merge must be inside an if/else to avoid running after just merge")
    else:
        t("Phase 3 bash block found",
          False,
          "could not extract bash block from Phase 3")

print()
h.summarize_and_exit()
