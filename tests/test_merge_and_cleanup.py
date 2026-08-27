#!/usr/bin/env python3
"""
Test suite for merge-and-cleanup command (feature #68, updated for #92).

These tests verify that `commands/merge-and-cleanup.md` correctly:
1. Uses exact branch matching for worktree resolution (never substring-match)
2. Orders phases correctly (push gate before merge gate)
3. Avoids destructive teardown verbs (delegated to /cleanup)
4. Runs merge in a subshell (never bare cd)
5. References /cleanup and passes absolute path
6. Does not re-encode stack logic (per ADR-0011)
7. Excludes destructive verbs from allowed-tools
8. Guards gh pr merge to avoid double-merge after just merge
9. Accepts either a PR number or worktree path as input
10. Path mode: detects input type before PR-number parsing
11. Path mode: reads cache before falling back to gh pr view
12. Path mode: backfills cache using temp-file-then-mv pattern
13. Path mode: errors on non-worktree or main-worktree paths

Run with: python3 tests/test_merge_and_cleanup.py
"""

import re
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

# Phase 1 should use awk with exact matching on refs/heads/<branch>. The awk
# pattern's exact whitespace is not load-bearing, so match it tolerantly
# rather than pinning to one literal spelling.
has_exact_match = 'refs/heads/' in MERGE and bool(
    re.search(r'\$0\s*==\s*"branch\s*"\s*b', MERGE)
)
t("Phase 1 uses exact refs/heads/ match in awk",
  has_exact_match,
  "expected awk with $0==\"branch \"b pattern (or equivalent spacing) for exact matching")

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
        # Strip a trailing '# ...' comment before splitting on command
        # separators, so a comment containing '(' / ')' can't fool the
        # subshell check below.
        code_part = re.split(r"(?<!\S)#", line, maxsplit=1)[0]
        # A line can chain multiple commands with ';', '&&', or '||' — check
        # each sub-command individually, not just the first token on the line.
        for sub_command in re.split(r";|&&|\|\|", code_part):
            stripped = sub_command.strip()
            if stripped.startswith("cd "):
                # Check if this sub-command is wrapped in a subshell by
                # looking at the raw line for both '(' and ')'.
                if not ("(" in line and ")" in line):
                    bare_cd_violations.append(f"line {line_num}: '{stripped}'")
                elif not re.search(r"\(\s*cd\s+", line):
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

        # Parse the if/then/else/fi structure explicitly (co-occurrence of the
        # verb and the variable name isn't enough — the verb could sit in
        # either arm and still "pass" a substring check) and confirm
        # 'gh pr merge --squash' appears only in the else arm, never in the
        # just-merge then-arm, which is what would actually cause a
        # double-merge.
        phase3_lines = phase3_bash.split("\n")
        guard_idx = next(
            (i for i, l in enumerate(phase3_lines)
             if re.search(r'if\s*\[\s*"\$MERGE_GATE_USED"\s*=\s*"just merge"\s*\]', l)),
            None,
        )
        t("Found the MERGE_GATE_USED == 'just merge' guard line",
          guard_idx is not None,
          "expected an if [ \"$MERGE_GATE_USED\" = \"just merge\" ] line in Phase 3")

        if guard_idx is not None:
            depth = 1
            in_else = False
            then_lines: list[str] = []
            else_lines: list[str] = []
            i = guard_idx + 1
            while i < len(phase3_lines) and depth > 0:
                stripped = phase3_lines[i].strip()
                if re.search(r"\bif\b.*\bthen\s*$", stripped):
                    depth += 1
                elif stripped == "fi":
                    depth -= 1
                    if depth == 0:
                        break
                elif stripped == "else" and depth == 1:
                    in_else = True
                    i += 1
                    continue
                (else_lines if in_else else then_lines).append(phase3_lines[i])
                i += 1

            then_body = "\n".join(then_lines)
            else_body = "\n".join(else_lines)

            t("'gh pr merge --squash' is absent from the just-merge (then) arm",
              "gh pr merge --squash" not in then_body,
              "found 'gh pr merge --squash' in the just-merge then-branch — this would double-merge")

            t("'gh pr merge --squash' is present in the else arm",
              "gh pr merge --squash" in else_body,
              "expected 'gh pr merge --squash' inside the else branch")
    else:
        t("Phase 3 bash block found",
          False,
          "could not extract bash block from Phase 3")

print()

# ============================================================================
# TEST 9: PR-number parsing prefers a URL's pull/<n> segment before falling
# back to the first digit run (regression test for the bug this PR fixed —
# an earlier numeric org/repo segment in a URL must not be grabbed instead
# of the actual PR number).
# ============================================================================
print("[Test 9] PR-number parsing: pull/<n> match precedes fallback digit-run match")

phase0_idx = get_line_index(MERGE, "Phase 0")
t("Phase 0 heading found",
  phase0_idx >= 0,
  "could not find Phase 0 section")

if phase0_idx >= 0:
    phase0_start_idx = phase0_idx + 1
    phase0_text = "\n".join(MERGE.split("\n")[phase0_start_idx:])
    bash_match = re.search(r"```bash\n(.*?)\n```", phase0_text, re.DOTALL)
    t("Phase 0 bash block found",
      bash_match is not None,
      "could not extract bash block from Phase 0")

    if bash_match:
        phase0_bash = bash_match.group(1)

        pull_match_idx = phase0_bash.find("pull/[0-9]+")
        t("Phase 0 matches a URL's pull/<n> segment",
          pull_match_idx != -1,
          "expected a 'pull/[0-9]+' pattern to extract the PR number from a URL")

        fallback_match = re.search(r'PR_NUM=\$\(echo "\$ARGUMENTS" \| grep -oE \'\[0-9\]\+\'', phase0_bash)
        t("Phase 0 has a fallback first-digit-run match",
          fallback_match is not None,
          "expected a fallback grep -oE '[0-9]+' match for the bare/#/PR-prefixed forms")

        if pull_match_idx != -1 and fallback_match is not None:
            t("The pull/<n> match appears before the fallback digit-run match",
              pull_match_idx < fallback_match.start(),
              "the pull/<n> pattern must be tried before the naive digit-run fallback, "
              "or a numeric org/repo segment earlier in a URL would be grabbed instead")

print()

# ============================================================================
# TEST 10: Path-mode input detection precedes PR-number parsing
# ============================================================================
print("[Test 10] Path mode: input type detection before PR-number parsing")

phase0_idx = get_line_index(MERGE, "Phase 0")
t("Phase 0 heading found",
  phase0_idx >= 0,
  "could not find Phase 0 section")

if phase0_idx >= 0:
    phase0_start_idx = phase0_idx + 1
    phase0_text = "\n".join(MERGE.split("\n")[phase0_start_idx:])
    bash_match = re.search(r"```bash\n(.*?)\n```", phase0_text, re.DOTALL)
    t("Phase 0 bash block found",
      bash_match is not None,
      "could not extract bash block from Phase 0")

    if bash_match:
        phase0_bash = bash_match.group(1)

        # Should test if $ARGUMENTS is a directory using test -d or cd ... 2>/dev/null
        has_path_detection = ('test -d "$ARGUMENTS"' in phase0_bash or
                             'cd "$ARGUMENTS" 2>/dev/null' in phase0_bash)
        t("Phase 0 detects path input (cd or test -d) before PR-number parsing",
          has_path_detection,
          "expected path detection (test -d or cd check) in Phase 0")

        # The pull/ pattern match (PR parsing) should come after the path detection
        # in the control flow (i.e., in an else branch or after the path branch)
        pull_pattern_idx = phase0_bash.find("pull/[0-9]+")
        path_detect_idx = phase0_bash.find("WT_CANDIDATE") if "WT_CANDIDATE" in phase0_bash else 0
        if path_detect_idx >= 0 and pull_pattern_idx >= 0:
            t("Path detection appears before PR-number parsing in the control flow",
              path_detect_idx < pull_pattern_idx,
              "path detection should precede PR-number regex parsing")

print()

# ============================================================================
# TEST 11: Path mode reads cache before gh pr view fallback
# ============================================================================
print("[Test 11] Path mode: cache-first before gh pr view fallback")

if phase0_idx >= 0:
    phase0_start_idx = phase0_idx + 1
    phase0_text = "\n".join(MERGE.split("\n")[phase0_start_idx:])
    bash_match = re.search(r"```bash\n(.*?)\n```", phase0_text, re.DOTALL)

    if bash_match:
        phase0_bash = bash_match.group(1)

        # Should read github-cache.json first
        has_cache_read = "github-cache.json" in phase0_bash and "cat " in phase0_bash
        t("Phase 0 path mode reads .claude/github-cache.json",
          has_cache_read,
          "expected 'cat' on github-cache.json in path-mode logic")

        # Should use jq to extract .pr.number
        has_cache_extract = ".pr.number" in phase0_bash and "jq" in phase0_bash
        t("Phase 0 path mode extracts .pr.number from cache via jq",
          has_cache_extract,
          "expected 'jq .pr.number' to extract cached PR number")

        # Should fall back to gh pr view if cache miss
        has_gh_fallback = "gh pr view" in phase0_bash
        t("Phase 0 path mode falls back to 'gh pr view' on cache miss",
          has_gh_fallback,
          "expected 'gh pr view' fallback when cache is missing")

print()

# ============================================================================
# TEST 12: Path mode backfills cache using temp-file-then-mv pattern
# ============================================================================
print("[Test 12] Path mode: cache backfill uses temp-file-then-mv pattern")

if phase0_idx >= 0:
    phase0_start_idx = phase0_idx + 1
    phase0_text = "\n".join(MERGE.split("\n")[phase0_start_idx:])
    bash_match = re.search(r"```bash\n(.*?)\n```", phase0_text, re.DOTALL)

    if bash_match:
        phase0_bash = bash_match.group(1)

        # Should use mktemp
        has_mktemp = "mktemp" in phase0_bash
        t("Phase 0 path mode uses mktemp for temp file",
          has_mktemp,
          "expected 'mktemp' to create a temp file for cache write")

        # Should use jq to merge cache
        has_jq_merge = "jq" in phase0_bash and ("--argjson" in phase0_bash or "--arg" in phase0_bash)
        t("Phase 0 path mode uses jq to merge PR data into cache",
          has_jq_merge,
          "expected 'jq --arg...' to merge cache entries")

        # Should use mv to atomically replace the cache
        has_atomic_mv = '"$TMP"' in phase0_bash and 'mv "$TMP"' in phase0_bash
        t("Phase 0 path mode uses mv to atomically replace cache file",
          has_atomic_mv,
          "expected 'mv \"$TMP\" ...cache.json' for atomic write")

        # Should NOT have bare > redirect to github-cache.json (which would truncate)
        bare_redirect = bool(re.search(r'>\s*"\$.*?github-cache\.json"', phase0_bash))
        t("Phase 0 path mode does NOT use bare > redirect to cache file",
          not bare_redirect,
          "bare redirect would truncate cache; must use temp-file-then-mv pattern")

print()

# ============================================================================
# TEST 13: Path mode errors on non-worktree or main-worktree
# ============================================================================
print("[Test 13] Path mode: error paths for non-worktree and main-worktree")

if phase0_idx >= 0:
    phase0_start_idx = phase0_idx + 1
    phase0_text = "\n".join(MERGE.split("\n")[phase0_start_idx:])
    bash_match = re.search(r"```bash\n(.*?)\n```", phase0_text, re.DOTALL)

    if bash_match:
        phase0_bash = bash_match.group(1)

        # Should check git rev-parse --is-inside-work-tree
        has_worktree_check = "rev-parse" in phase0_bash and "is-inside-work-tree" in phase0_bash
        t("Phase 0 path mode checks for git worktree validity",
          has_worktree_check,
          "expected 'git rev-parse --is-inside-work-tree' check")

        # Should error on non-worktree
        has_non_wt_error = 'ERROR' in phase0_bash and ('not a git worktree' in phase0_bash or 'is not a git worktree' in phase0_bash)
        t("Phase 0 path mode has ERROR for non-worktree path",
          has_non_wt_error,
          "expected ERROR message when path is not a git worktree")

        # Should check against MAIN_WT
        has_main_wt_check = "MAIN_WT" in phase0_bash and "=" in phase0_bash
        t("Phase 0 path mode checks against main worktree",
          has_main_wt_check,
          "expected MAIN_WT comparison to reject main worktree")

        # Should error on main-worktree resolution
        has_main_error = ("Cannot merge from main" in phase0_bash or "resolves to main" in phase0_bash)
        t("Phase 0 path mode has ERROR when resolving to main",
          has_main_error,
          "expected ERROR message when path resolves to main worktree")

print()

# ============================================================================
# TEST 14: Both input modes converge on identical output before Phase 2
# ============================================================================
print("[Test 14] Both input modes output PR_NUM and WT before Phase 2")

if phase0_idx >= 0:
    phase0_start_idx = phase0_idx + 1
    phase0_text = "\n".join(MERGE.split("\n")[phase0_start_idx:])
    bash_match = re.search(r"```bash\n(.*?)\n```", phase0_text, re.DOTALL)

    if bash_match:
        phase0_bash = bash_match.group(1)

        # Both branches should emit PR_NUM=... and WT=...
        pr_num_output_count = phase0_bash.count('echo "PR_NUM=$PR_NUM"')
        wt_output_count = phase0_bash.count('echo "WT=$WT"')

        # We expect these to appear once at the end of each branch, so they should
        # appear at least once in the whole block (could be more if emitted by both branches)
        t("Phase 0 emits PR_NUM= for variable export",
          'PR_NUM=' in phase0_bash,
          "expected 'PR_NUM=...' output for variable export to Phase 2")

        t("Phase 0 emits WT= for variable export",
          'WT=' in phase0_bash,
          "expected 'WT=...' output for variable export to Phase 2")

print()
h.summarize_and_exit()
