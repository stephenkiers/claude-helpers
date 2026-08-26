#!/usr/bin/env python3
"""
Test suite for stack-awareness invariants (issue #51).

These tests verify that `/cleanup` and `/shipit` commands are gh-stack-aware
and worktree-safe, using a shared "Stack Detection" block in prompts/worktree-reference.md.

Tests assert (updated for ADR-0011, which reversed PR #55's blanket ban on
`gh stack sync` and scoped the worktree-safety ban to per-branch layout):
1. Worktree-safety: gh stack init/sync/checkout never *executed* as active
   commands in bash blocks (comments/prose mentions are permitted; the
   single-driver `gh stack sync` action is described in prose, not inlined)
2. Shared Stack Detection block exists with defined variables
3. Worktree-safe verbs (gh stack unstack) used; `gh stack link` is documented but
   NOT auto-executed by /shipit (it is worktree-safe but destructive — repoints the
   bottom PR's base to trunk; /shipit establishes the chain via `gh pr create --base`)
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
    pattern = rf"^## {re.escape(section_name)}"
    match = re.search(pattern, text, re.MULTILINE)
    return match.start() if match else -1


CLEANUP = read(CLEANUP_FILE)
SHIPIT = read(SHIPIT_FILE)
WORKTREE_REF = read(WORKTREE_REF_FILE)
MERGE = read(MERGE_FILE)

h = Harness("STACK AWARENESS TEST SUITE (ISSUE #51)")
t = h.test_result

# ============================================================================
# INVARIANT 1: Worktree-safety (no hostile verbs *executed* in bash blocks)
# ============================================================================
# ADR-0011 reversed PR #55's blanket ban: `gh stack sync` is the intended
# single-driver push command, and `gh stack init`/`checkout` are fatal under
# per-branch layout. This repo's convention (updated per accepted review
# finding #30 / ADR-0011): the single-driver Step 4a arm of stack-sync.md may
# inline `gh stack sync` as an active bash command after a dry-run guard;
# everywhere else the action is described in *prose* ("Run `gh stack sync`")
# and never inlined, so bash blocks stay worktree-safe (manual git primitives +
# API-only `gh stack link`/`unstack`). The guard below flags hostile verbs only
# when they appear as *active command lines* — not inside comments
# (explanatory) or echo/printf strings (emit-only runbooks the user pastes
# manually).
print("[Invariant 1] Worktree-safety: no hostile gh stack verbs as active commands in bash blocks")

HOSTILE_VERBS: list[str] = ["gh stack init", "gh stack sync", "gh stack checkout"]

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

def check_file_worktree_safe(content: str, filename: str, hostile_verbs=None) -> list[str]:
    """
    Check that hostile verbs don't appear as active (non-comment, non-echo)
    command lines in bash blocks of a file. Comments and prose mentions are
    permitted. `hostile_verbs` overrides HOSTILE_VERBS (used to scope the
    `gh stack sync` ban outside stack-sync.md's Step 4a section).

    Returns: list of violation strings (empty if no violations found)
    """
    verbs = hostile_verbs if hostile_verbs is not None else HOSTILE_VERBS
    bash_blocks = extract_bash_blocks(content)
    violations = []
    for line_num, block_text in bash_blocks:
        for line in active_command_lines(block_text):
            for verb in verbs:
                if verb in line:
                    violations.append(f"{filename}:{line_num} executes '{verb}'")
    return violations


cleanup_violations = check_file_worktree_safe(CLEANUP, "cleanup.md")
shipit_violations = check_file_worktree_safe(SHIPIT, "shipit.md")
ref_violations = check_file_worktree_safe(WORKTREE_REF, "worktree-reference.md")
merge_violations = check_file_worktree_safe(MERGE, "merge-and-cleanup.md")

t("cleanup.md bash blocks are worktree-safe",
  len(cleanup_violations) == 0,
  f"found {len(cleanup_violations)} violations: {cleanup_violations}" if cleanup_violations else "")

t("shipit.md bash blocks are worktree-safe",
  len(shipit_violations) == 0,
  f"found {len(shipit_violations)} violations: {shipit_violations}" if shipit_violations else "")

t("worktree-reference.md bash blocks are worktree-safe",
  len(ref_violations) == 0,
  f"found {len(ref_violations)} violations: {ref_violations}" if ref_violations else "")

t("merge-and-cleanup.md bash blocks are worktree-safe",
  len(merge_violations) == 0,
  f"found {len(merge_violations)} violations: {merge_violations}" if merge_violations else "")

# ADR-0011: the warning is now *scoped* — gh stack init/sync/checkout are fatal
# under per-branch layout specifically, not a blanket "never use" ban. The docs
# must carry that scoped warning and name `gh stack sync` as the safe
# single-driver path (the deliberate reversal of PR #55's blanket ban).
scoped_warning = re.search(
    r"gh stack.{0,150}fatal under.{0,30}per-branch",
    WORKTREE_REF, re.DOTALL)
t("worktree-reference.md warns gh stack verbs are fatal under per-branch layout",
  scoped_warning is not None,
  "expected a scoped warning that gh stack init/sync/checkout are fatal under per-branch layout (per ADR-0011)")

sync_single_driver = re.search(
    r"gh stack sync.{0,80}(intended|single-driver)",
    WORKTREE_REF, re.DOTALL)
t("worktree-reference.md names 'gh stack sync' as the single-driver path (ADR-0011)",
  sync_single_driver is not None,
  "expected 'gh stack sync' identified as the intended single-driver command (ADR-0011 reversal of #55)")

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

# Regression (2026-08-25, PPS-228 → PR #77): `gh stack link <parent> <child>` run as
# an ACTIVE command by /shipit destructively repointed the parent's base to trunk and
# detached it from its real parent. /shipit must establish the chain via
# `gh pr create --base` only; `gh stack link` may appear in comments/prose, never as
# an executed bash command line.
shipit_bash_blocks = extract_bash_blocks(SHIPIT)
shipit_link_active = []
for line_num, block_text in shipit_bash_blocks:
    for line in active_command_lines(block_text):
        if "gh stack link" in line:
            shipit_link_active.append(f"shipit.md:{line_num} executes 'gh stack link'")
t("shipit.md does NOT auto-execute 'gh stack link' (destructive — use --base only)",
  len(shipit_link_active) == 0,
  f"found {len(shipit_link_active)} active 'gh stack link' calls: {shipit_link_active}" if shipit_link_active else "")

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
t("cleanup.md mentions 'push --force-with-lease' for safe force-push",
  "force-with-lease" in CLEANUP,
  "expected 'force-with-lease' in restack runbook")

# --- Corrected primitive: detect-then-branch (reset/prove before rebase/replay) ---

# Confine high-stakes Invariant-5 checks to bash blocks (not prose)
cleanup_bash_blocks = extract_bash_blocks(CLEANUP)
cleanup_bash_text = "\n".join(block for _, block in cleanup_bash_blocks)

# Should reset --hard to take the already-rebased remote
t("cleanup.md restack bash block mentions 'reset --hard'",
  "reset --hard" in cleanup_bash_text,
  "expected 'reset --hard' in the detect-then-branch restack bash block")

# Should detect the force-push signature via merge-base --is-ancestor
t("cleanup.md restack bash block uses 'merge-base --is-ancestor'",
  "merge-base --is-ancestor" in cleanup_bash_text,
  "expected 'merge-base --is-ancestor' in the restack bash block")

# Should snapshot a backup branch before rewriting
t("cleanup.md restack bash block snapshots 'backup/' branch",
  "backup/" in cleanup_bash_text,
  "expected 'backup/<branch>' snapshot in the restack bash block")

# Should prove the reset lost no work via an empty diff
t("cleanup.md restack bash block proves no data loss with 'diff --stat'",
  "diff --stat" in cleanup_bash_text,
  "expected 'git diff --stat backup HEAD' empty-diff proof in bash block")

# Should re-link upstream tracking (rebased branches can lose it)
t("cleanup.md restack bash block re-links upstream with '--set-upstream-to'",
  "--set-upstream-to" in cleanup_bash_text,
  "expected '--set-upstream-to' to re-link tracking in bash block")

# The shared primitive lives in worktree-reference.md
restack_heading_idx = get_line_index(WORKTREE_REF, "Restack a child")
worktree_ref_bash_blocks = extract_bash_blocks(WORKTREE_REF)
worktree_ref_bash_text = "\n".join(block for _, block in worktree_ref_bash_blocks)

t("worktree-reference.md contains the shared '### Restack a child' sub-block",
  restack_heading_idx >= 0,
  "expected a '### Restack a child' sub-block in worktree-reference.md")

t("worktree-reference.md restack block contains 'reset --hard'",
  "reset --hard" in worktree_ref_bash_text,
  "expected 'reset --hard' in the worktree-reference.md restack bash block")

# Runbook should be emit-only (manual, user runs it)
# Key off specific emit-only signal like "emit-only" or "you run" phrasing
is_emit_only = re.search(r"(emit-only|you.*run|user.*run)", CLEANUP.lower())
t("cleanup.md runbook is emit-only (manual, not auto-executed)",
  is_emit_only is not None,
  "expected language indicating user manually runs the runbook (e.g., 'emit-only', 'you run')")

# Runbook step should come BEFORE "Remove Worktree" step
# Find the section headers using get_line_index (matches ### headings)
runbook_idx = get_line_index(CLEANUP, "Detect Stacked Children")
remove_worktree_idx = get_line_index(CLEANUP, "Remove Worktree")

t("cleanup.md has '### Detect Stacked Children' section",
  runbook_idx >= 0,
  "expected '### Detect Stacked Children' section")

t("cleanup.md has '### Remove Worktree' section",
  remove_worktree_idx >= 0,
  "expected '### Remove Worktree' section")

if runbook_idx >= 0 and remove_worktree_idx >= 0:
    t("cleanup.md restack section comes before 'Remove Worktree' step",
      runbook_idx < remove_worktree_idx,
      "restack must capture tip before worktree is deleted")

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
# Assert co-occurrence in one expression (avoid independent substrings)
has_false_stacked = re.search(r"isStacked.*false", SHIPIT, re.DOTALL)
t("shipit.md mentions 'isStacked: false' value for non-stacked PRs",
  has_false_stacked is not None,
  "expected support for non-stacked (isStacked: false) case")

print()

# ============================================================================
# INVARIANT 8: /stack-sync command exists with frontmatter (issue #57)
# ============================================================================
# /stack-sync is the layout-routed *sync* mirror of #64's layout-routed *push*.
# It must exist as a command doc with YAML frontmatter declaring its tools and
# argument hint, and must document the --dry-run / --yes flags.
print("[Invariant 8] /stack-sync command file exists with frontmatter")

STACK_SYNC_FILE = COMMANDS_DIR / "stack-sync.md"
STACK_SYNC = read(STACK_SYNC_FILE)

t("commands/stack-sync.md exists and is non-empty",
  len(STACK_SYNC.strip()) > 0,
  "expected commands/stack-sync.md to exist with content")


def extract_frontmatter(text: str) -> str:
    """Return the YAML frontmatter block (between the first two --- fences), or ''."""
    # Tolerate CRLF line endings so the check doesn't false-fail on Windows-authored docs.
    m = re.match(r"^---\r?\n(.*?)\r?\n---", text, re.DOTALL)
    return m.group(1) if m else ""


stack_sync_fm = extract_frontmatter(STACK_SYNC)
t("stack-sync.md has YAML frontmatter",
  len(stack_sync_fm) > 0,
  "expected frontmatter delimited by --- fences at the top of stack-sync.md")

t("stack-sync.md frontmatter declares 'name: stack-sync'",
  re.search(r"^name:\s*stack-sync\s*$", stack_sync_fm, re.MULTILINE) is not None,
  "expected a 'name: stack-sync' field in stack-sync.md frontmatter")

t("stack-sync.md frontmatter declares allowed-tools",
  "allowed-tools" in stack_sync_fm,
  "expected an 'allowed-tools' field in stack-sync.md frontmatter")

t("stack-sync.md frontmatter declares argument-hint",
  "argument-hint" in stack_sync_fm,
  "expected an 'argument-hint' field in stack-sync.md frontmatter")

t("stack-sync.md documents --dry-run flag",
  "--dry-run" in STACK_SYNC,
  "expected --dry-run flag documented in stack-sync.md")

t("stack-sync.md documents --yes flag",
  "--yes" in STACK_SYNC,
  "expected --yes flag documented in stack-sync.md")

print()

# ============================================================================
# INVARIANT 9: /stack-sync routes on STACK_LAYOUT (three arms)
# ============================================================================
# single-driver -> delegate to `gh stack sync`; per-branch -> manual git -C
# bottom-up rebase walk; unknown -> STOP and ask (fail closed).
print("[Invariant 9] /stack-sync routes on STACK_LAYOUT (single-driver / per-branch / unknown)")

t("stack-sync.md references STACK_LAYOUT variable",
  "STACK_LAYOUT" in STACK_SYNC,
  "expected STACK_LAYOUT variable referenced in stack-sync.md")

# Step-section slices, computed once and reused by later invariants (9, 11).
step4a_idx = get_byte_index(STACK_SYNC, "Step 4a")
step4b_idx = get_byte_index(STACK_SYNC, "Step 4b")
step5_idx = get_byte_index(STACK_SYNC, "Step 5")
step6_idx = get_byte_index(STACK_SYNC, "Step 6")
step4a_section = STACK_SYNC[step4a_idx:step4b_idx] if 0 <= step4a_idx < step4b_idx else ""
step4b_section = STACK_SYNC[step4b_idx:step5_idx] if 0 <= step4b_idx < step5_idx else ""
step5_section = STACK_SYNC[step5_idx:step6_idx] if 0 <= step5_idx < step6_idx else ""

t("stack-sync.md has a '## Step 4a' single-driver arm heading",
  step4a_idx >= 0,
  "expected a '## Step 4a' heading for the single-driver arm")

t("stack-sync.md Step 4a delegates to 'gh stack sync' near the heading",
  re.search(r"^## Step 4a\b.{0,600}?gh stack sync", STACK_SYNC, re.MULTILINE | re.DOTALL) is not None,
  "expected 'gh stack sync' in proximity to the '## Step 4a' heading (ungated by design per ADR-0012)")

t("stack-sync.md has a '## Step 4b' per-branch arm heading",
  step4b_idx >= 0,
  "expected a '## Step 4b' heading for the per-branch arm")

t("stack-sync.md per-branch arm uses a manual rebase walk",
  "rebase" in STACK_SYNC and "git -C" in STACK_SYNC,
  "expected per-branch arm to use 'git -C' + 'rebase' manual walk")

t("stack-sync.md unknown arm fails closed near STACK_LAYOUT=\"unknown\"",
  re.search(r'STACK_LAYOUT="unknown".{0,160}(stop and ask|fail.?closed)',
            STACK_SYNC, re.DOTALL | re.IGNORECASE) is not None,
  "expected a fail-closed stop-and-ask line in proximity to STACK_LAYOUT=\"unknown\"")

# --- Behavior branches: mode detection, pivot guard, dry-run, install guard ---

t("stack-sync.md defines a post-merge routing mode",
  'SYNC_MODE="post-merge"' in STACK_SYNC,
  "expected a SYNC_MODE=\"post-merge\" assignment in mode detection")

t("stack-sync.md defines an ongoing routing mode",
  'SYNC_MODE="ongoing"' in STACK_SYNC,
  "expected a SYNC_MODE=\"ongoing\" assignment in mode detection")

t("stack-sync.md post-merge detection fetches the mergeCommit field",
  "mergeCommit" in STACK_SYNC,
  "expected 'mergeCommit' in the post-merge 'gh pr view --json' field list")

t("stack-sync.md guards the pivot against the default branch",
  '[ "$PIVOT_BRANCH" = "$DEFAULT_BRANCH" ]' in STACK_SYNC,
  "expected a pivot guard comparing PIVOT_BRANCH to DEFAULT_BRANCH")

t("stack-sync.md gates 'git fetch' behind dry-run",
  re.search(r'DRY_RUN" = "true" \] \|\| git fetch', STACK_SYNC) is not None,
  "expected '[ \"$DRY_RUN\" = \"true\" ] || git fetch ...' so --dry-run performs no fetch")

t("stack-sync.md single-driver arm has a gh-stack install guard",
  "command -v gh-stack" in STACK_SYNC,
  "expected a 'command -v gh-stack' install guard in the single-driver arm")

t("stack-sync.md enforces the dry-run gate in the single-driver arm (Step 4a)",
  "DRY_RUN" in step4a_section,
  "expected a DRY_RUN check inside the Step 4a section")

t("stack-sync.md enforces the dry-run gate in the per-branch arm (Step 4b)",
  "DRY_RUN" in step4b_section,
  "expected a DRY_RUN check inside the Step 4b section")

print()

# ============================================================================
# INVARIANT 10: bottom-up ordering via merge-base --is-ancestor + cycle guard
# ============================================================================
print("[Invariant 10] bottom-up ordering via merge-base --is-ancestor + cycle detection")

t("stack-sync.md uses 'git merge-base --is-ancestor' for ordering",
  "merge-base --is-ancestor" in STACK_SYNC,
  "expected 'git merge-base --is-ancestor' for bottom-up ancestor-before-descendant ordering")

t("stack-sync.md detects cycles (hard error)",
  "cycle" in STACK_SYNC.lower(),
  "expected cycle detection with a hard error in stack-sync.md")

# --- Structural ordering assertions: pin the walk's shape, not just its keywords ---
# (A keyword grep was green on the buggy walk PR #57 had to fix — the append must
# precede the recursion, the seed call must be the pivot at level 0, and each child
# must be recorded at $((level + 1)) so level-1 substitutions apply to direct children.)

desc_append_idx = STACK_SYNC.find('DESCENDANTS="${DESCENDANTS}')
walk_recurse_idx = STACK_SYNC.find('walk "$child_branch"')

t("stack-sync.md walk() appends to DESCENDANTS before the recursive walk call (pre-order)",
  desc_append_idx >= 0 and walk_recurse_idx > desc_append_idx,
  "expected the DESCENDANTS append to precede the recursive walk call so ancestors precede descendants")

t("stack-sync.md seeds the descendant walk at the pivot with level 0",
  'walk "$PIVOT_BRANCH" 0' in STACK_SYNC,
  "expected the initial call 'walk \"$PIVOT_BRANCH\" 0' (pivot itself is never synced)")

t("stack-sync.md walk() records each child's level as $((level + 1))",
  re.search(r'DESCENDANTS="\$\{DESCENDANTS\}[^\n]*\$\(\(level \+ 1\)\)', STACK_SYNC) is not None,
  "expected the DESCENDANTS record to emit the child level via $((level + 1))")

print()

# ============================================================================
# INVARIANT 11: push confirmation gate (force-with-lease + repo-cache check)
# ============================================================================
print("[Invariant 11] push gates: force-with-lease, repo-cache CHECK_CMD gate, Step 5 confirmation")

t("stack-sync.md gates push with 'force-with-lease'",
  "force-with-lease" in STACK_SYNC,
  "expected 'push --force-with-lease' in stack-sync.md")

# The runnable gate lives in the canonical Restack block, not in stack-sync.md:
# CHECK_CMD is read from .claude/repo-cache.json and must be non-empty before any
# force-push. Assert the gate's shape (mechanism), not just the filename keyword.
t("worktree-reference.md gates force-push on the repo-cache.json CHECK_CMD",
  re.search(r'CHECK_CMD=\$\(jq[^\n]*repo-cache\.json.{0,220}\[ -n "\$CHECK_CMD" \]',
            WORKTREE_REF, re.DOTALL) is not None,
  "expected the canonical Restack block to read CHECK_CMD from repo-cache.json and require it non-empty before force-push")

# The confirmation gate is Step 5's job specifically — scope the assertions there.
t("stack-sync.md Step 5 confirmation requires AskUserQuestion",
  "AskUserQuestion" in step5_section,
  "expected the Step 5 push-confirmation gate to pause via AskUserQuestion")

t("stack-sync.md Step 5 confirmation honors --yes",
  "--yes" in step5_section,
  "expected the Step 5 confirmation to be skippable via --yes")

print()

# ============================================================================
# INVARIANT 12: stack-sync.md bash blocks are worktree-safe
# ============================================================================
print("[Invariant 12] stack-sync.md bash blocks are worktree-safe")

# Finding 30 / ADR-0011: the single-driver Step 4a arm may inline `gh stack sync`
# as an active command after a dry-run guard. Scope the ban: `gh stack init` and
# `gh stack checkout` are banned everywhere in the file; `gh stack sync` is banned
# everywhere EXCEPT the Step 4a section. step4a_idx/step4b_idx are computed in
# Invariant 9 above.
if 0 <= step4a_idx < step4b_idx:
    stack_sync_4a_slice = STACK_SYNC[step4a_idx:step4b_idx]
    # Blank the Step 4a slice (preserving its line count so violation line
    # numbers stay accurate) and apply the full ban to the remainder.
    stack_sync_outside_4a = (
        STACK_SYNC[:step4a_idx]
        + "\n" * stack_sync_4a_slice.count("\n")
        + STACK_SYNC[step4b_idx:]
    )
    stack_sync_violations = (
        check_file_worktree_safe(stack_sync_outside_4a, "stack-sync.md")
        + check_file_worktree_safe(stack_sync_4a_slice, "stack-sync.md (Step 4a)",
                                   hostile_verbs=["gh stack init", "gh stack checkout"])
    )
else:
    # Step headings missing (Invariant 9 already fails) — apply the full ban.
    stack_sync_violations = check_file_worktree_safe(STACK_SYNC, "stack-sync.md")
t("stack-sync.md bash blocks are worktree-safe",
  len(stack_sync_violations) == 0,
  f"found {len(stack_sync_violations)} violations: {stack_sync_violations}" if stack_sync_violations else "")

print()

# ============================================================================
# INVARIANT 13: generalized Restack block params + ongoing recipe (worktree-ref)
# ============================================================================
# The Restack-a-child block is generalized: <MERGED_TIP>/<DEFAULT_BRANCH> are
# replaced by <NEW_BASE>/<OLD_BASE>, and a new ongoing-sync recipe is added.
print("[Invariant 13] Restack block generalized to <NEW_BASE>/<OLD_BASE> + ongoing recipe")

t("worktree-reference.md restack block parameterizes <NEW_BASE>",
  "<NEW_BASE>" in WORKTREE_REF,
  "expected <NEW_BASE> parameter in the generalized restack block")

t("worktree-reference.md restack block parameterizes <OLD_BASE>",
  "<OLD_BASE>" in WORKTREE_REF,
  "expected <OLD_BASE> parameter in the generalized restack block")

ongoing_heading_idx = get_line_index(WORKTREE_REF, "Sync a child (ongoing")
t("worktree-reference.md has '### Sync a child (ongoing' recipe heading",
  ongoing_heading_idx >= 0,
  "expected a '### Sync a child (ongoing ...)' heading for the ongoing-sync recipe")

# Post-merge mapping: NEW_BASE=origin/<default>, OLD_BASE=<merged tip>
t("worktree-reference.md documents post-merge NEW_BASE=origin/<default>",
  re.search(r"NEW_BASE.{0,80}origin/", WORKTREE_REF, re.DOTALL) is not None,
  "expected NEW_BASE mapped to origin/<default> (post-merge) in worktree-reference.md")

# Ongoing mapping: OLD_BASE=$(git merge-base HEAD origin/<parent>)
t("worktree-reference.md documents ongoing OLD_BASE via git merge-base",
  re.search(r"OLD_BASE.{0,80}merge-base", WORKTREE_REF, re.DOTALL) is not None,
  "expected OLD_BASE mapped to $(git merge-base ...) for the ongoing recipe")

print()

# ============================================================================
# INVARIANT 14: shipit.md + cleanup.md wired to /stack-sync
# ============================================================================
print("[Invariant 14] shipit.md and cleanup.md wired to /stack-sync")

t("shipit.md references /stack-sync",
  "stack-sync" in SHIPIT,
  "expected shipit.md to invoke /stack-sync after a per-branch stacked push")

# Gating must be verifiable, not two keywords anywhere in the file: require the
# stack-sync invocation and the STACK_LAYOUT="per-branch" test in proximity.
t("shipit.md gates /stack-sync on per-branch layout",
  re.search(r'STACK_LAYOUT="per-branch".{0,400}stack-sync|stack-sync.{0,400}STACK_LAYOUT="per-branch"',
            SHIPIT, re.DOTALL) is not None,
  "expected shipit.md to tie the /stack-sync invocation to a STACK_LAYOUT=\"per-branch\" check")

t("cleanup.md references /stack-sync",
  "stack-sync" in CLEANUP,
  "expected cleanup.md to execute restack via /stack-sync (post-merge)")

t("cleanup.md gates /stack-sync on per-branch layout",
  re.search(r'STACK_LAYOUT="per-branch".{0,500}stack-sync|stack-sync.{0,500}STACK_LAYOUT="per-branch"',
            CLEANUP, re.DOTALL) is not None,
  "expected cleanup.md to tie the /stack-sync invocation to a STACK_LAYOUT=\"per-branch\" check")

t("cleanup.md keeps a manual-runbook fallback when the Skill harness is absent",
  "command -v claude" in CLEANUP and re.search(r"not on PATH", CLEANUP, re.IGNORECASE) is not None,
  "expected a 'command -v claude' harness check with a not-on-PATH manual-runbook fallback")

t("cleanup.md supports a STACK_SYNC_MANUAL=1 opt-out of auto stack-sync",
  "STACK_SYNC_MANUAL" in CLEANUP,
  "expected a STACK_SYNC_MANUAL=1 opt-out for the auto-execute arm")

print()

# ============================================================================
# INVARIANT 15: ADR-0012 exists with required sections + ADR-0011 cross-ref
# ============================================================================
print("[Invariant 15] ADR-0012 exists with Status/Context/Decision/Consequences + ADR-0011 cross-ref")

ADR_DIR = REPO_ROOT / "docs" / "adr"
ADR_0012_FILE = ADR_DIR / "0012-stack-sync.md"
ADR_0012 = read(ADR_0012_FILE)

t("docs/adr/0012-stack-sync.md exists and is non-empty",
  len(ADR_0012.strip()) > 0,
  "expected docs/adr/0012-stack-sync.md to exist with content")

t("ADR-0012 has a Status field",
  re.search(r"Status", ADR_0012) is not None,
  "expected a 'Status' field in ADR-0012")

t("ADR-0012 has a Context section",
  re.search(r"^##\s+Context", ADR_0012, re.MULTILINE) is not None,
  "expected a '## Context' section in ADR-0012")

t("ADR-0012 has a Decision section",
  re.search(r"^##\s+Decision", ADR_0012, re.MULTILINE) is not None,
  "expected a '## Decision' section in ADR-0012")

t("ADR-0012 has a Consequences section",
  re.search(r"^##\s+Consequences", ADR_0012, re.MULTILINE) is not None,
  "expected a '## Consequences' section in ADR-0012")

t("ADR-0012 cross-references ADR-0011",
  "0011" in ADR_0012,
  "expected ADR-0012 to cross-reference ADR-0011")

print()

# ============================================================================
# INVARIANT 16: ADR README index has an ADR-0012 entry
# ============================================================================
print("[Invariant 16] ADR README index contains an ADR-0012 entry")

ADR_README = read(ADR_DIR / "README.md")

t("docs/adr/README.md exists and is non-empty",
  len(ADR_README.strip()) > 0,
  "expected docs/adr/README.md to exist with content")

t("README.md index references ADR-0012",
  "0012" in ADR_README,
  "expected an ADR-0012 entry in the ADR README index")

t("README.md index links to 0012-stack-sync.md",
  "0012-stack-sync.md" in ADR_README,
  "expected a link to 0012-stack-sync.md in the ADR README index")

print()
h.summarize_and_exit()
