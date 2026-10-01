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
import json
import subprocess
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

# ============================================================================
# TEST 8: Q1 Predicate presence and literal jq evaluation
# ============================================================================
print("[Test 8] Q1 predicate text is present and evaluates correctly")

# Extract the predicate text from Phase 1
predicate_text = '.queue.decision=="refuse" and .queue.detection.state=="configured" and .queue.pr_base!=null and .queue.pr_base==.queue.queue_base'
has_predicate = predicate_text in MERGE
t("Q1 predicate text is present in Phase 0 & 1",
  has_predicate,
  f"expected predicate text: {predicate_text}")

# Try to evaluate the predicate with jq against sample payloads

def test_predicate_with_payload(description, payload, expected_result):
    """Test the predicate against a payload; expected_result is True/False."""
    try:
        # Use jq to evaluate: true = route, false = refuse without route
        result = subprocess.run(
            ["jq", "-e", predicate_text],
            input=json.dumps(payload).encode(),
            capture_output=True,
            timeout=5
        )
        actual = result.returncode == 0  # jq -e exits 0 if expr is truthy
        passed = actual == expected_result
        t(f"Predicate evaluation: {description}",
          passed,
          f"expected {expected_result}, jq returned {actual}")
        return passed
    except Exception as e:
        t(f"Predicate evaluation: {description}",
          False,
          f"jq evaluation failed: {e}")
        return False

# Test case (a): configured + matching bases → route (predicate true)
test_predicate_with_payload(
    "configured + pr_base==queue_base → route",
    {
        "queue": {
            "decision": "refuse",
            "detection": {"state": "configured"},
            "pr_base": "main",
            "queue_base": "main"
        }
    },
    True  # Should route
)

# Test case (b): unknown state + matching bases → refuse (predicate false)
test_predicate_with_payload(
    "unknown state (predicate false)",
    {
        "queue": {
            "decision": "refuse",
            "detection": {"state": "unknown"},
            "pr_base": "main",
            "queue_base": "main"
        }
    },
    False  # Should refuse
)

# Test case (c): configured + pr_base=None → refuse (predicate false)
test_predicate_with_payload(
    "configured but pr_base=null (predicate false)",
    {
        "queue": {
            "decision": "refuse",
            "detection": {"state": "configured"},
            "pr_base": None,
            "queue_base": "main"
        }
    },
    False  # Should refuse
)

# Test case (d): configured + queue_base=None → refuse (predicate false)
test_predicate_with_payload(
    "configured but queue_base=null (predicate false)",
    {
        "queue": {
            "decision": "refuse",
            "detection": {"state": "configured"},
            "pr_base": "main",
            "queue_base": None
        }
    },
    False  # Should refuse
)

print()

# ============================================================================
# TEST 9: Predicate-false path prints message and exits 3
# ============================================================================
print("[Test 9] Predicate-false path prints .queue.message and exits 3")

# Look for the pattern where predicate is false, we print the message, and exit 3
pattern = r'printf.*\.queue\.message.*exit 3'
has_refuse_path = bool(re.search(pattern, MERGE, re.MULTILINE | re.DOTALL))
t("Predicate-false path prints .queue.message and exits 3",
  has_refuse_path,
  "expected pattern that prints .queue.message and exits 3 on predicate false")

print()

# ============================================================================
# TEST 10: Route marker and start-time writes before Phase 2Q
# ============================================================================
print("[Test 10] Route marker + start-time writes appear before Phase 2Q instruction")

# Find byte indices for critical sections
phase1_route_write_idx = MERGE.find('printf \'queue\\n\' > "$MC_STATE_DIR/route"')
phase2b_route_write_idx = MERGE.find('printf \'queue\\n\' > "$MC_STATE_DIR/route"', phase1_route_write_idx + 1 if phase1_route_write_idx >= 0 else 0)
phase2q_idx = get_byte_index(MERGE, "Phase 2Q")
phase3_idx = get_byte_index(MERGE, "Phase 3")

t("Phase 1 or 2b writes route marker",
  phase1_route_write_idx >= 0 or phase2b_route_write_idx >= 0,
  "expected route marker write in Phase 1 or Phase 2b")

if phase2q_idx >= 0:
    earliest_write = min(p for p in [phase1_route_write_idx, phase2b_route_write_idx] if p >= 0)
    t("Route marker write appears before Phase 2Q",
      earliest_write < phase2q_idx if earliest_write >= 0 else False,
      f"write at {earliest_write}, Phase 2Q at {phase2q_idx}")

print()

# ============================================================================
# TEST 11: Phase 2b does NOT have pre-skill rm -rf (only on Stop branch)
# ============================================================================
print("[Test 11] Phase 2b 'Set up' branch does not rm -rf state dir before Skill invocation")

# Extract the Phase 2b section (look for "### Phase 2b" through next "### Phase")
phase2b_start = get_byte_index(MERGE, "Phase 2b")
if phase2b_start >= 0:
    # Find the next phase heading
    next_phase_match = re.search(r"^###", MERGE[phase2b_start + 10:], re.MULTILINE)
    phase2b_end = phase2b_start + 10 + next_phase_match.start() if next_phase_match else len(MERGE)
    phase2b_text = MERGE[phase2b_start:phase2b_end]

    # Check for "Set up" and "Stop" branches
    has_set_up_branch = "Set up" in phase2b_text or "Set up," in phase2b_text
    has_stop_branch = "Stop" in phase2b_text

    # Look for rm -rf in a code block that's marked "Set up" but before a Skill invocation
    # This is a simplified check: if we find "rm -rf" and "Skill" in Phase 2b with rm before Skill, that's bad
    rm_idx_in_2b = phase2b_text.find('rm -rf "$MC_STATE_DIR"')
    skill_idx_in_2b = phase2b_text.find('queued-merge')  # Skill invocation indicator

    if rm_idx_in_2b >= 0 and skill_idx_in_2b >= 0:
        # If rm comes before Skill in the Set-Up branch, that's a violation
        # But if it's in the Stop branch (after "Stop" keyword), it's fine
        stop_idx = phase2b_text.find("**Stop**")
        if stop_idx >= 0 and rm_idx_in_2b < stop_idx:
            t("Phase 2b 'Set up' does not rm -rf before Skill invocation",
              False,
              "found rm -rf before Skill instruction in Set-Up branch")
        else:
            t("Phase 2b 'Set up' does not rm -rf before Skill invocation",
              True,
              "")
    else:
        t("Phase 2b 'Set up' does not rm -rf before Skill invocation",
          True,
          "no rm -rf found before Skill in Phase 2b")
else:
    t("Phase 2b section exists",
      False,
      "Phase 2b not found")

print()

# ============================================================================
# TEST 12: Phase 2Q contains explicit "return here and run Phase 4-Q" instruction
# ============================================================================
print("[Test 12] Phase 2Q contains explicit return/Phase 4-Q instruction")

has_phase2q = "Phase 2Q" in MERGE
t("Phase 2Q section exists",
  has_phase2q,
  "expected '### Phase 2Q' heading")

if has_phase2q:
    phase2q_idx = get_byte_index(MERGE, "Phase 2Q")
    phase3_idx = get_byte_index(MERGE, "Phase 3")
    phase2q_text = MERGE[phase2q_idx:phase3_idx] if phase3_idx > phase2q_idx else MERGE[phase2q_idx:]

    has_return_instruction = "return here" in phase2q_text.lower() and "phase 4-q" in phase2q_text.lower()
    t("Phase 2Q contains 'return here and run Phase 4-Q' instruction",
      has_return_instruction,
      "expected explicit instruction to return and run Phase 4-Q")

print()

# ============================================================================
# TEST 13: Phase 4-Q: MERGED check precedes cleanup Skill invocation
# ============================================================================
print("[Test 13] Phase 4-Q: MERGED check precedes cleanup Skill invocation")

has_phase4q = "Phase 4-Q" in MERGE
t("Phase 4-Q section exists",
  has_phase4q,
  "expected '### Phase 4-Q' heading")

if has_phase4q:
    phase4q_idx = get_byte_index(MERGE, "Phase 4-Q")
    phase5_idx = get_byte_index(MERGE, "Phase 5")
    phase4q_text = MERGE[phase4q_idx:phase5_idx] if phase5_idx > phase4q_idx else MERGE[phase4q_idx:]

    # Look for FINAL_STATE check and cleanup Skill within Phase 4-Q
    has_final_state = 'FINAL_STATE=$(gh pr view' in phase4q_text
    has_cleanup_skill = 'cleanup` skill' in phase4q_text

    t("Phase 4-Q has FINAL_STATE MERGED check",
      has_final_state,
      "expected FINAL_STATE=$(gh pr view ...) check in Phase 4-Q")

    if has_final_state and has_cleanup_skill:
        merged_idx = phase4q_text.find('FINAL_STATE=$(gh pr view')
        cleanup_idx = phase4q_text.find('cleanup` skill')
        t("Phase 4-Q MERGED check precedes cleanup Skill",
          merged_idx < cleanup_idx,
          f"MERGED at {merged_idx}, cleanup at {cleanup_idx}")

print()

# ============================================================================
# TEST 14: Phase 4-Q requires route=queue; original Phase 4 has apply_exit_code check
# ============================================================================
print("[Test 14] Phase 4-Q requires route=queue; Phase 4 still has apply_exit_code check")

phase4q_idx = get_byte_index(MERGE, "Phase 4-Q")
phase4_idx = get_byte_index(MERGE, "Phase 4 —")
phase5_idx = get_byte_index(MERGE, "Phase 5")

if phase4q_idx >= 0:
    phase4q_text = MERGE[phase4q_idx:phase5_idx] if phase5_idx > phase4q_idx else MERGE[phase4q_idx:]
    has_route_check = "route" in phase4q_text and "queue" in phase4q_text
    t("Phase 4-Q requires route file to contain 'queue'",
      has_route_check,
      "expected route file check in Phase 4-Q")

if phase4_idx >= 0 and phase4q_idx >= 0:
    phase4_text = MERGE[phase4_idx:phase4q_idx]
    has_apply_check = "apply_exit_code" in phase4_text
    t("Original Phase 4 still contains apply_exit_code check",
      has_apply_check,
      "expected apply_exit_code guard in Phase 4 (not removed)")

print()

# ============================================================================
# TEST 15: No result file deletion
# ============================================================================
print("[Test 15] No deletion of result.json or result-*.json")

rm_lines = [ln for line_num, blk in extract_bash_blocks(MERGE)
            for ln in blk.split("\n")
            if re.search(r"\brm\b", ln) and not ln.strip().startswith("#")]

# Check specifically for queue result files (not apply_result.json which is a merge result)
# Queue result files are: result.json or result-<PR>.json from the merge-queue state dir
deletes_queue_result = [ln for ln in rm_lines if re.search(r'(merge-queue.*result|result-\d+\.json|/result\.json)', ln)]
t("No queue result.json or result-*.json files are deleted",
  len(deletes_queue_result) == 0,
  f"found rm lines targeting queue result files: {deletes_queue_result}")

print()

# ============================================================================
# TEST 16: No `date` command in bash blocks
# ============================================================================
print("[Test 16] No `date` command used in any bash block")

has_date = any(re.search(r'\bdate\b', blk) for line_num, blk in extract_bash_blocks(MERGE))
t("No 'date' command in bash blocks (using jq -n 'now' instead)",
  not has_date,
  "found 'date' command in bash blocks")

print()

# ============================================================================
# TEST 17: allowed-tools includes Step 4 additions, still includes scripts.workflow.cli
# ============================================================================
print("[Test 17] allowed-tools includes queue-route tools and workflow CLI")

required_tools = [
    "Bash(cat:*)",
    "Bash(python3:*)",
    "Bash(command:*)",
    "Bash(gh repo view:*)",
    "Bash(*merge-queue*)",
    "scripts.workflow.cli"
]

for tool in required_tools:
    has_tool = tool in allowed_tools
    t(f"allowed-tools includes '{tool}'",
      has_tool,
      f"missing from allowed-tools: {tool}")

has_no_git_push = "Bash(git push" not in allowed_tools
t("allowed-tools does NOT include Bash(git push:*)",
  has_no_git_push,
  "should not have git push in allowed-tools")

print()

# ============================================================================
# TEST 18: Summary examples contain QUEUE row
# ============================================================================
print("[Test 18] Summary examples show QUEUE row for routed PRs")

phase5_idx = get_byte_index(MERGE, "Phase 5")
files_idx = MERGE.find("## Files")
phase5_text = MERGE[phase5_idx:files_idx] if files_idx > phase5_idx else MERGE[phase5_idx:]

has_queue_row = "QUEUE" in phase5_text and "✓" in phase5_text
t("Summary examples include QUEUE row",
  has_queue_row,
  "expected QUEUE row in Phase 5 examples")

print()

# ============================================================================
# TEST 19: Old refuse note text is gone
# ============================================================================
print("[Test 19] Old 'refuses ... and prints /queued-merge' text is gone")

old_text_patterns = [
    r'refuses.*exit code 3.*prints.*\/queued-merge',
    r'exit code 3.*prints.*\/queued-merge.*PR',
    r'print.*\/queued-merge <PR>',
]

found_old = any(re.search(pattern, MERGE, re.IGNORECASE) for pattern in old_text_patterns)
t("Old refuse note text is removed",
  not found_old,
  "old note about printing /queued-merge <PR> should be gone")

print()

# ============================================================================
# TEST 20: 'Skip if route=queue' line appears at top of BOTH Phase 3 AND Phase 4
# ============================================================================
print("[Test 20] 'Skip if route is queue' guard appears in both Phase 3 and Phase 4")

phase3_idx = get_byte_index(MERGE, "Phase 3 —")
phase4_idx = get_byte_index(MERGE, "Phase 4 —")
phase4q_idx = get_byte_index(MERGE, "Phase 4-Q")
phase5_idx = get_byte_index(MERGE, "Phase 5")

skip_guard_text = re.compile(r'skip.*route.*queue', re.IGNORECASE)

if phase3_idx >= 0 and phase4_idx >= 0:
    phase3_text = MERGE[phase3_idx:phase4_idx]
    phase4_text = MERGE[phase4_idx:phase4q_idx] if phase4q_idx > phase4_idx else MERGE[phase4_idx:phase5_idx]

    has_skip_3 = skip_guard_text.search(phase3_text) is not None
    has_skip_4 = skip_guard_text.search(phase4_text) is not None

    t("Phase 3 has 'skip if route=queue' guard",
      has_skip_3,
      "expected skip guard at top of Phase 3")

    t("Phase 4 has 'skip if route=queue' guard",
      has_skip_4,
      "expected skip guard at top of Phase 4")

print()

h.summarize_and_exit()
