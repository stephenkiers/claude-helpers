#!/usr/bin/env python3
"""
Test suite for the /stack-sync execution model (round 2, spec-blind).

Written from the expert-review action plan for the /stack-sync feature, NOT from
the implementation. Covers plan behaviors that tests/test_stack_awareness.py does
not already assert:

1.  Detect layout parameterized on the pivot (STACK_LAYOUT_SUBJECT) in both
    commands/stack-sync.md and prompts/worktree-reference.md
2.  Level-2+ children use the parent's captured pre-sync tip as <OLD_BASE>;
    worktree-reference prose says when merge-base IS correct (single-child ongoing)
3.  Topological verification skips refs that fail `git rev-parse --verify -q` and
    accepts ancestry from local $_par OR origin/$_par
4.  Per-child execution model: skipped-parent-failed subtrees, `git rebase --abort`
    on conflict, per-child outcome accumulation reported in Step 6, backup deletion
    gated on that child's own outcome
5.  Mode detection: ls-remote run for CLOSED PRs, mergedAt routing, lookup-failed
    distinguished from lookup-absent (exit codes, fail closed)
6.  Step 4a single-driver arm is ungated BY DESIGN with an explicit disclaimer and
    no confirmation prompt there
7.  Child detection unions cache + `gh pr list` (deduped, provenance printed);
    the canonical Find-children block in worktree-reference.md matches
8.  DESCENDANTS records are path-safe: `while IFS= read -r` + `cut -f5-` tail
9.  Notes state Find-children is re-encoded inline and must be kept in sync manually
10. stack-sync.md references ADR-0012, not "PR #64"
11. Arg parser: `set -f`, unknown --flags error out, dash-leading pivot rejected,
    `-y` documented or removed
12. Step 6 paste-me cleanup command quotes both fields
13. Frontmatter allowed-tools: no `Bash(bash:*)`, grants match tools used in the
    body, `gh stack` pattern present
14. `gh pr list` failure is captured and warned on (not a silent "Nothing to sync")
16. Post-merge detection prefers `.mergeCommit.oid` with a local-ref fallback
17. `resolve_worktree` hard-errors on duplicate matches (n>1)
18. `gh stack sync` appears as an active command in a fenced bash block inside
    Step 4a, after the dry-run guard
19. ASSUME_YES is referenced in Step 5 prose, or dropped entirely
20. cleanup.md: restack abort = deferral; "run /cleanup again" only inside the
    auto-execute arm; Step 2.6 retitled to match behavior; post-merge cache write
    uses an atomic mktemp + mv jq pattern
21. shipit.md: stack-sync gate keyed on having descendants; prints
    "Syncing N stacked descendant(s)" before invoking
22. expert-rebase.md: post-force-push note points at the ongoing /stack-sync
    recipe and records why there is no auto-invoke
23. ADR-0012 scoping statements (two-gate per-branch, ungated single-driver,
    repo-cache as build/test gate, level-2+ pre-sync tip, cleanup
    harness-conditional bullet), README index says "verified (not ordered)",
    CLAUDE.md has a /stack-sync Lifecycle bullet

Run with: python3 tests/test_stack_sync_execution_model.py
"""

import re
from _test_harness import REPO_ROOT, Harness

COMMANDS_DIR = REPO_ROOT / "commands"
PROMPTS_DIR = REPO_ROOT / "prompts"
ADR_DIR = REPO_ROOT / "docs" / "adr"

STACK_SYNC_FILE = COMMANDS_DIR / "stack-sync.md"
WORKTREE_REF_FILE = PROMPTS_DIR / "worktree-reference.md"
CLEANUP_FILE = COMMANDS_DIR / "cleanup.md"
SHIPIT_FILE = COMMANDS_DIR / "shipit.md"
EXPERT_REBASE_FILE = COMMANDS_DIR / "expert-rebase.md"
ADR_0012_FILE = ADR_DIR / "0012-stack-sync.md"
ADR_README_FILE = ADR_DIR / "README.md"
CLAUDE_MD_FILE = REPO_ROOT / "CLAUDE.md"


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
    pattern = r"```bash\n(.*?)\n```"
    for match in re.finditer(pattern, markdown_text, re.DOTALL):
        start_pos = match.start()
        line_num = markdown_text[:start_pos].count("\n") + 1
        blocks.append((line_num, match.group(1)))
    return blocks


def extract_frontmatter(text: str) -> str:
    """Return the YAML frontmatter block (between the first two --- fences), or ''."""
    m = re.match(r"^---\r?\n(.*?)\r?\n---", text, re.DOTALL)
    return m.group(1) if m else ""


def get_byte_index(text, section_name):
    """Return the byte offset of a top-level heading, or -1 if not found."""
    pattern = rf"^## {re.escape(section_name)}"
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


def slice_heading_section(text, heading_regex):
    """
    Slice from a markdown heading whose text matches heading_regex to the next
    heading of the same-or-higher level. Returns None when no heading matches.
    """
    m = re.search(rf"^(#+)\s[^\n]*{heading_regex}[^\n]*$", text, re.MULTILINE)
    if not m:
        return None
    level = len(m.group(1))
    nxt = re.search(rf"^#{{1,{level}}}\s", text[m.end():], re.MULTILINE)
    end = m.end() + nxt.start() if nxt else len(text)
    return text[m.start():end]


STACK_SYNC = read(STACK_SYNC_FILE)
WORKTREE_REF = read(WORKTREE_REF_FILE)
CLEANUP = read(CLEANUP_FILE)
SHIPIT = read(SHIPIT_FILE)
EXPERT_REBASE = read(EXPERT_REBASE_FILE)
ADR_0012 = read(ADR_0012_FILE)
ADR_README = read(ADR_README_FILE)
CLAUDE_MD = read(CLAUDE_MD_FILE)

# Step-section slices of stack-sync.md, same convention as test_stack_awareness.py.
step2_idx = get_byte_index(STACK_SYNC, "Step 2")
step3_idx = get_byte_index(STACK_SYNC, "Step 3")
step4a_idx = get_byte_index(STACK_SYNC, "Step 4a")
step4b_idx = get_byte_index(STACK_SYNC, "Step 4b")
step5_idx = get_byte_index(STACK_SYNC, "Step 5")
step6_idx = get_byte_index(STACK_SYNC, "Step 6")
step2_section = STACK_SYNC[step2_idx:step3_idx] if 0 <= step2_idx < step3_idx else ""
step4a_section = STACK_SYNC[step4a_idx:step4b_idx] if 0 <= step4a_idx < step4b_idx else ""
step5_section = STACK_SYNC[step5_idx:step6_idx] if 0 <= step5_idx < step6_idx else ""
step6_section = STACK_SYNC[step6_idx:] if step6_idx >= 0 else ""

h = Harness("STACK-SYNC EXECUTION MODEL TEST SUITE (ROUND 2, SPEC-BLIND)")
t = h.test_result

# ============================================================================
# INVARIANT 1: Detect layout parameterized on the pivot (STACK_LAYOUT_SUBJECT)
# ============================================================================
print("[Invariant 1] Detect layout is parameterized on STACK_LAYOUT_SUBJECT, not current branch")

t("stack-sync.md references STACK_LAYOUT_SUBJECT",
  "STACK_LAYOUT_SUBJECT" in STACK_SYNC,
  "expected the layout detection to be parameterized on STACK_LAYOUT_SUBJECT")

t("worktree-reference.md references STACK_LAYOUT_SUBJECT",
  "STACK_LAYOUT_SUBJECT" in WORKTREE_REF,
  "expected the shared Detect-layout block to take STACK_LAYOUT_SUBJECT")

t("stack-sync.md assigns STACK_LAYOUT_SUBJECT from the pivot",
  re.search(r'STACK_LAYOUT_SUBJECT=["\']?\$\{?(PIVOT_BRANCH|1)\}?', STACK_SYNC) is not None,
  "expected STACK_LAYOUT_SUBJECT to be assigned from PIVOT_BRANCH (or the pivot arg $1)")

print()

# ============================================================================
# INVARIANT 2: level-2+ OLD_BASE = parent's captured pre-sync tip
# ============================================================================
print("[Invariant 2] level-2+ children rebase onto the parent's pre-sync tip, not a fresh merge-base")

t("stack-sync.md captures the parent's pre-sync tip",
  re.search(r"pre-sync", STACK_SYNC, re.IGNORECASE) is not None,
  "expected a pre-sync tip capture (the level-2+ OLD_BASE) in stack-sync.md")

t("stack-sync.md captures the pre-sync tip via git rev-parse",
  re.search(r"pre-sync.{0,400}rev-parse|rev-parse.{0,400}pre-sync",
            STACK_SYNC, re.DOTALL | re.IGNORECASE) is not None,
  "expected the pre-sync tip capture (git rev-parse) in proximity to the pre-sync language")

t("worktree-reference.md says when merge-base IS correct (single-child ongoing case)",
  re.search(r"single.?child", WORKTREE_REF, re.IGNORECASE) is not None,
  "expected prose limiting the merge-base OLD_BASE to the single-child ongoing case")

print()

# ============================================================================
# INVARIANT 3: topological verification tolerates unverifiable refs + origin fallback
# ============================================================================
print("[Invariant 3] topological verification skips unverifiable refs, accepts origin/$_par")

t("stack-sync.md verifies refs with 'git rev-parse --verify'",
  "--verify" in STACK_SYNC,
  "expected 'git rev-parse --verify -q' so missing refs are skipped, not hard errors")

t("stack-sync.md skips pairs whose refs fail verification",
  re.search(r"--verify.{0,500}skip|skip.{0,500}--verify", STACK_SYNC, re.DOTALL | re.IGNORECASE) is not None,
  "expected a skip path when a ref fails 'rev-parse --verify'")

t("stack-sync.md accepts ancestry from origin/$_par as well as local $_par",
  re.search(r'origin/"?\$\{?_par\}?"?', STACK_SYNC) is not None,
  "expected the ancestry check to fall back to origin/$_par")

print()

# ============================================================================
# INVARIANT 4: per-child execution model
# ============================================================================
print("[Invariant 4] per-child outcomes: skipped-parent-failed, rebase --abort, Step 6 report, gated backup deletion")

t("stack-sync.md marks subtrees of failed parents 'skipped-parent-failed'",
  "skipped-parent-failed" in STACK_SYNC,
  "expected the literal outcome 'skipped-parent-failed' for subtrees of non-synced parents")

t("stack-sync.md aborts the rebase on the conflict path",
  "rebase --abort" in STACK_SYNC,
  "expected 'git rebase --abort' in the conflict path so a failed child is left clean")

t("stack-sync.md Step 6 reports per-child outcomes",
  re.search(r"outcome", step6_section, re.IGNORECASE) is not None,
  "expected accumulated branch:outcome records reported in the Step 6 section")

t("stack-sync.md Step 6 suggests 'git branch -D backup/<child>'",
  "branch -D" in step6_section and "backup/" in step6_section,
  "expected the backup-deletion suggestion inside the Step 6 section")

# Pin the gate CONDITION, not just the co-presence of "branch -D" and "synced" somewhere in the
# section — a doc could mention both without gating one on the other.
t("stack-sync.md Step 6 gates backup deletion on the child's own outcome",
  "branch -D" in step6_section
  and re.search(r'\$OUTCOME" = "synced"', step6_section) is not None,
  "expected the backup-deletion suggestion gated on that child's own outcome being 'synced'")

print()

# ============================================================================
# INVARIANT 5: mode detection — lookup-failed vs lookup-absent, CLOSED handling
# ============================================================================
print("[Invariant 5] mode detection distinguishes lookup failure from absence; ls-remote for CLOSED")

# Scoped to the Step 2 section: whole-file presence is satisfiable by the frontmatter's
# allowed-tools grants (e.g. Bash(git ls-remote:*)) without the behavior existing at all.
t("stack-sync.md routes on the mergedAt field",
  "mergedAt" in step2_section,
  "expected mode routing on mergedAt (non-null -> post-merge) in Step 2")

t("stack-sync.md handles the CLOSED-without-merge case",
  "CLOSED" in step2_section,
  "expected a CLOSED + null mergedAt arm that stops and reports, in Step 2")

t("stack-sync.md runs ls-remote for the remote-branch check (CLOSED included)",
  "ls-remote" in step2_section,
  "expected 'git ls-remote' in the Step 2 mode detection (run for CLOSED PRs too)")

# Anchored on the Step 2 section, not a bare find("gh pr view") — the first "gh pr view" in the
# file is the frontmatter's Bash(gh pr view:*) grant, so a naive window never reaches the code.
t("stack-sync.md captures the lookup exit code (fails closed on error)",
  "$?" in step2_section
  and re.search(r'\*\)\s*echo\s+"ERROR', step2_section) is not None,
  "expected an exit-code check after 'gh pr view' with a catch-all ERROR arm, so lookup failure"
  " is not treated as absence")

print()

# ============================================================================
# INVARIANT 6: Step 4a single-driver arm is ungated BY DESIGN (with disclaimer)
# ============================================================================
print("[Invariant 6] Step 4a is ungated by design and says so; confirmation lives in Step 5 only")

t("stack-sync.md Step 4a carries an explicit ungated-by-design disclaimer",
  re.search(r"ungated|no confirmation|without (a )?confirmation|by design",
            step4a_section, re.IGNORECASE) is not None,
  "expected a disclaimer in the Step 4a section that the single-driver arm is ungated by design")

t("stack-sync.md Step 4a has no AskUserQuestion confirmation prompt",
  "AskUserQuestion" not in step4a_section,
  "the Step 4a arm must not gate on AskUserQuestion; confirmation is Step 5's job")

print()

# ============================================================================
# INVARIANT 7: child detection unions both detectors, deduped, with provenance
# ============================================================================
print("[Invariant 7] child detection = cache UNION 'gh pr list', deduped, provenance printed")

t("stack-sync.md runs the cache-based child detector",
  re.search(r"github-cache\.json|cache", STACK_SYNC) is not None,
  "expected the repo-cache detector in stack-sync.md's Find-children")

# "gh pr list" alone is satisfied by the frontmatter's Bash(gh pr list:*) grant even if the body
# never runs the detector — pin the actual invocation shape instead.
t("stack-sync.md always runs 'gh pr list' as the second detector",
  "gh pr list --base" in STACK_SYNC,
  "expected a 'gh pr list --base <parent>' detector call alongside the cache detector")

t("stack-sync.md dedupes the union of detectors by branch",
  re.search(r"sort -u|dedup|unique", STACK_SYNC, re.IGNORECASE) is not None,
  "expected deduplication (e.g. 'sort -u') of the two detectors' output")

# The old regex's only match was an incidental "via cache" in a dedupe comment — not provenance
# output. Pin the actual per-child echo shape for BOTH detector sources.
t("stack-sync.md prints per-child detection provenance",
  re.search(r"detected child .{0,80}\(worktree cache\)", STACK_SYNC) is not None
  and re.search(r"detected child .{0,80}\(gh pr list\)", STACK_SYNC) is not None,
  "expected each child's detection source echoed per child: 'detected child <b> of <p> (worktree"
  " cache)' and '(gh pr list)'")

find_children_section = slice_heading_section(WORKTREE_REF, r"[Ff]ind.{0,20}[Cc]hildren") or ""
t("worktree-reference.md has a canonical Find-children block",
  len(find_children_section) > 0,
  "expected a 'Find children' heading in worktree-reference.md")

t("worktree-reference.md Find-children block also unions both detectors",
  "gh pr list" in find_children_section and "cache" in find_children_section.lower(),
  "expected the canonical Find-children block to combine the cache and 'gh pr list'")

print()

# ============================================================================
# INVARIANT 8: DESCENDANTS records are path-safe
# ============================================================================
print("[Invariant 8] DESCENDANTS format: newline-separated records, IFS-safe read, cut -f5- tail")

t("stack-sync.md parses DESCENDANTS with 'while IFS= read -r'",
  "while IFS= read -r" in STACK_SYNC,
  "expected 'while IFS= read -r' so paths with spaces survive the parse")

t("stack-sync.md extracts the path-typed tail with 'cut -f5-'",
  re.search(r"cut -d: -f5-", STACK_SYNC) is not None,
  "expected a 'cut -d: -f5-'-style tail extraction (no two-delimiter ambiguity)")

print()

# ============================================================================
# INVARIANT 9: notes admit Find-children is re-encoded inline
# ============================================================================
print("[Invariant 9] stack-sync.md notes the inline Find-children copy must be kept in sync manually")

t("stack-sync.md carries a keep-in-sync note for the inline Find-children",
  re.search(r"re-?encoded inline|kept? in sync|keep .{0,20}in sync", STACK_SYNC, re.IGNORECASE) is not None,
  "expected an honest note that Find-children is re-encoded inline and must be kept in sync manually")

print()

# ============================================================================
# INVARIANT 10: stack-sync.md references ADR-0012, not "PR #64"
# ============================================================================
print("[Invariant 10] stack-sync.md cites ADR-0012")

t("stack-sync.md references ADR-0012",
  re.search(r"ADR-0012|adr/0012", STACK_SYNC) is not None,
  "expected a reference to ADR-0012 (docs/adr/0012-stack-sync.md)")

t("stack-sync.md does not cite 'PR #64' as its rationale",
  "PR #64" not in STACK_SYNC,
  "expected the ADR-0012 reference instead of the stale 'PR #64' citation")

print()

# ============================================================================
# INVARIANT 11: argument-parser hardening
# ============================================================================
print("[Invariant 11] arg parser: set -f, unknown flags error, dash-leading pivot rejected, -y handled")

t("stack-sync.md disables globbing with 'set -f'",
  "set -f" in STACK_SYNC,
  "expected 'set -f' in the arg parser so unquoted patterns cannot glob")

t("stack-sync.md errors out on unknown --flags",
  re.search(r"[Uu]nknown (flag|option|argument)", STACK_SYNC) is not None,
  "expected an 'Unknown flag/option' error arm in the arg parser")

t("stack-sync.md rejects a dash-leading pivot",
  re.search(r'-\*\)|leading.{0,15}dash|dash-leading|begins? with (a )?["\']?-',
            STACK_SYNC, re.IGNORECASE) is not None,
  "expected the pivot to be rejected when it starts with a dash (option-injection guard)")

y_short = re.search(r"\b-y\b", STACK_SYNC)
t("stack-sync.md documents -y alongside --yes, or omits -y",
  y_short is None or re.search(r"-y.{0,30}--yes|--yes.{0,30}-y", STACK_SYNC) is not None,
  "found a bare '-y' with no nearby '--yes' documentation")

print()

# ============================================================================
# INVARIANT 12: Step 6 paste-me cleanup command quotes both fields
# ============================================================================
print("[Invariant 12] Step 6 paste-me cleanup command is quoted")

cleanup_cmd_lines = [line for line in step6_section.split("\n") if "branch -D" in line]
t("stack-sync.md Step 6 emits a paste-me cleanup command",
  len(cleanup_cmd_lines) > 0,
  "expected a 'git branch -D backup/...' suggestion line in the Step 6 section")

# Both fields must be quote-wrapped so the pasted command survives paths with spaces. Either quote
# style counts — the doc emits echo "... git -C '$CHILD_WT' branch -D 'backup/$CHILD_BRANCH'"
# (single-quoted fields inside a double-quoted echo, so the pasted command carries literal quotes
# around the expanded values). Check: inside the echo payload, no $-variable may sit OUTSIDE a
# quoted span (the outer echo wrapper itself is excluded first so it can't launder bare fields).
def cleanup_fields_quoted(line):
    m = re.search(r'echo\s+"(.*)"', line)
    payload = m.group(1) if m else line
    if "$" not in payload:
        return False
    stripped = re.sub(r"'[^']*'", "", payload)          # drop 'single-quoted' spans
    stripped = re.sub(r'\\?"[^"\\]*\\?"', "", stripped)  # drop "double" or \"escaped\" spans
    return "$" not in stripped

t("stack-sync.md Step 6 cleanup command quotes its fields",
  any(cleanup_fields_quoted(line) for line in cleanup_cmd_lines),
  "expected every variable field in the Step 6 cleanup command to sit inside quotes")

print()

# ============================================================================
# INVARIANT 13: frontmatter allowed-tools match the tools actually used
# ============================================================================
print("[Invariant 13] frontmatter allowed-tools: no Bash(bash:*), grants match body usage")

stack_sync_fm = extract_frontmatter(STACK_SYNC)

t("stack-sync.md frontmatter has no 'Bash(bash:*)' grant",
  "Bash(bash" not in stack_sync_fm,
  "expected no shell-out 'Bash(bash:*)' grant in the frontmatter")

t("stack-sync.md frontmatter grants the 'gh stack' pattern it invokes",
  re.search(r"Bash\(gh stack", stack_sync_fm) is not None,
  "expected a 'Bash(gh stack ...)' grant matching the Step 4a 'gh stack sync' invocation")

body_bash_text = "\n".join(block for _, block in extract_bash_blocks(STACK_SYNC))
TOOL_CANDIDATES = ["sed", "awk", "cut", "jq", "sort", "tr", "comm", "xargs"]
for tool in TOOL_CANDIDATES:
    if re.search(rf"(^|[^A-Za-z0-9_]){tool}\s", body_bash_text, re.MULTILINE):
        t(f"stack-sync.md frontmatter grants Bash({tool}:*) used in the body",
          f"Bash({tool}" in stack_sync_fm,
          f"body bash blocks use '{tool}' but the frontmatter has no 'Bash({tool}...' grant")

print()

# ============================================================================
# INVARIANT 14: 'gh pr list' failure is not a silent "Nothing to sync"
# ============================================================================
print("[Invariant 14] gh pr list failure is captured and warned on")

# Anchored on the actual invocation "gh pr list --base" in the body — a bare find("gh pr list")
# hits the frontmatter's Bash(gh pr list:*) grant and windows the wrong text. Exit-status capture
# may take either idiom: `if VAR=$(gh pr list ...)` (status tested by the if) or an explicit $?
# check after the call; both distinguish API failure from an empty result.
gh_pr_list_idx = STACK_SYNC.find("gh pr list --base")
detection_window = STACK_SYNC[gh_pr_list_idx:gh_pr_list_idx + 1500] if gh_pr_list_idx >= 0 else ""

t("stack-sync.md captures the 'gh pr list' exit status",
  re.search(r"if\s+\w+=\$\(gh pr list", STACK_SYNC) is not None
  or "$?" in detection_window
  or re.search(r"exit (code|status)", detection_window) is not None,
  "expected an exit-status capture on 'gh pr list' (if-capture or $?) so API failure is not"
  " mistaken for no children")

t("stack-sync.md warns when child detection fails",
  re.search(r"WARNING[^\n]*failed", detection_window, re.IGNORECASE) is not None,
  "expected a WARNING near the 'gh pr list' call when detection fails")

print()

# ============================================================================
# INVARIANT 16: post-merge detection prefers .mergeCommit.oid with local fallback
# ============================================================================
print("[Invariant 16] post-merge tip comes from .mergeCommit.oid, falling back to the local ref")

t("stack-sync.md prefers the .mergeCommit.oid field",
  ".mergeCommit.oid" in STACK_SYNC,
  "expected '.mergeCommit.oid' as the preferred merged-tip source")

# Sliced to the Step 2 (mode detection) section: the fallback lives ~50 lines below the first
# mergeCommit mention, past any fixed char window. Pin both halves — the fallback wording and the
# actual local-ref expression it falls back to.
t("stack-sync.md documents a local-ref fallback for the merged tip",
  re.search(r"fall\s?back|fallback|local (ref|branch)", step2_section, re.IGNORECASE) is not None
  and re.search(r'git rev-parse "\$PIVOT_BRANCH"', step2_section) is not None,
  "expected a local-ref fallback (git rev-parse \"$PIVOT_BRANCH\") for the merged tip in Step 2")

print()

# ============================================================================
# INVARIANT 17: resolve_worktree hard-errors on duplicate matches
# ============================================================================
print("[Invariant 17] resolve_worktree fails loudly on ambiguous (n>1) matches")

t("stack-sync.md defines a resolve_worktree helper",
  "resolve_worktree" in STACK_SYNC,
  "expected a resolve_worktree function mapping each child branch to its worktree")

resolve_idx = STACK_SYNC.find("resolve_worktree")
resolve_window = STACK_SYNC[resolve_idx:resolve_idx + 2000] if resolve_idx >= 0 else ""
t("resolve_worktree hard-errors on duplicate matches",
  re.search(r"duplicate|more than one|multiple|ambig", resolve_window, re.IGNORECASE) is not None,
  "expected resolve_worktree to error (listing both paths) when more than one worktree matches")

print()

# ============================================================================
# INVARIANT 18: 'gh stack sync' is an active command in Step 4a, after the guard
# ============================================================================
print("[Invariant 18] 'gh stack sync' appears in a fenced bash block in Step 4a, after the dry-run guard")

# Positions come from the finditer match offsets, not str.find(block_text): the bare string
# "gh stack sync" also appears in Step 4a's backtick-quoted prose BEFORE the bash blocks, so
# find() mis-anchors the invocation there and inverts the ordering check.
sync_lines = []
for match in re.finditer(r"```bash\n(.*?)\n```", step4a_section, re.DOTALL):
    block_text = match.group(1)
    for line in active_command_lines(block_text):
        if "gh stack sync" in line:
            sync_lines.append(match.start())

t("stack-sync.md Step 4a inlines 'gh stack sync' as an active command",
  len(sync_lines) > 0,
  "expected an active (non-comment, non-echo) 'gh stack sync' line in a Step 4a bash block")

# Anchor the guard WITH its polarity (`= "true"`): a flipped guard (`!= "true"`) must fail here,
# not silently relocate the anchor. (Part C weak-assertion fix: presence alone pinned nothing.)
dry_run_guard_idx = step4a_section.find('if [ "$DRY_RUN" = "true" ]')
t("stack-sync.md Step 4a dry-run guard fires when DRY_RUN is true",
  dry_run_guard_idx >= 0,
  "expected the guard 'if [ \"$DRY_RUN\" = \"true\" ]' — polarity matters, not just presence")

t("stack-sync.md Step 4a places the dry-run guard before 'gh stack sync'",
  dry_run_guard_idx >= 0 and len(sync_lines) > 0 and dry_run_guard_idx < min(sync_lines),
  "expected the DRY_RUN guard to precede the 'gh stack sync' invocation inside Step 4a")

print()

# ============================================================================
# INVARIANT 19: ASSUME_YES is wired into Step 5, or dropped
# ============================================================================
print("[Invariant 19] ASSUME_YES is referenced in Step 5 prose, or absent entirely")

t("ASSUME_YES appears in the Step 5 section if it exists at all",
  "ASSUME_YES" not in STACK_SYNC or "ASSUME_YES" in step5_section,
  "found ASSUME_YES outside Step 5 — the --yes plumbing must be visible where the confirmation gate lives")

print()

# ============================================================================
# INVARIANT 20: cleanup.md — deferral, auto-arm-only re-run echo, Step 2.6 title, atomic cache write
# ============================================================================
print("[Invariant 20] cleanup.md restack semantics: abort = deferral, scoped re-run echo, retitled Step 2.6, atomic cache write")

t("cleanup.md frames an aborted restack as a deferral",
  re.search(r"defer", CLEANUP, re.IGNORECASE) is not None,
  "expected the runbook to be ALWAYS emitted, with abort treated as deferral")

claude_check_idx = CLEANUP.find("command -v claude")
again_indices = [m.start() for m in re.finditer(r"cleanup again", CLEANUP, re.IGNORECASE)]
t("cleanup.md suggests re-running /cleanup",
  len(again_indices) > 0,
  "expected a 'run /cleanup again' hint for the auto-executed restack outcome")

t("cleanup.md echoes 'cleanup again' only inside the auto-execute arm",
  claude_check_idx >= 0 and len(again_indices) > 0
  and all(i > claude_check_idx for i in again_indices),
  "expected every 'cleanup again' echo to appear after the 'command -v claude' harness check")

step26_heading = re.search(r"^#+[^\n]*2\.6[^\n]*$", CLEANUP, re.MULTILINE)
t("cleanup.md Step 2.6 heading matches the restack/sync behavior",
  step26_heading is not None and re.search(r"sync|restack", step26_heading.group(0), re.IGNORECASE) is not None,
  "expected the Step 2.6 heading retitled to describe the stack-sync/restack behavior")

t("cleanup.md writes the post-merge cache update atomically (mktemp + mv)",
  "mktemp" in CLEANUP and re.search(r"\bmv\b", CLEANUP) is not None,
  "expected a jq-safe mktemp + mv pattern for writing the child's .claude/github-cache.json")

t("cleanup.md post-merge cache write sets isStacked (false or new parent)",
  re.search(r"isStacked", CLEANUP) is not None and re.search(r"parentBranch", CLEANUP) is not None,
  "expected the post-merge write to update stack.isStacked/parentBranch in the child's cache")

print()

# ============================================================================
# INVARIANT 21: shipit.md — descendant-keyed gate + progress message
# ============================================================================
print("[Invariant 21] shipit.md gates /stack-sync on having descendants and announces the count")

t("shipit.md keys the /stack-sync gate on descendants (not STACK_IS_STACKED alone)",
  re.search(r"descendant.{0,500}stack-sync|stack-sync.{0,500}descendant",
            SHIPIT, re.DOTALL | re.IGNORECASE) is not None,
  "expected the stack-sync gate to be keyed on having stacked descendants")

t("shipit.md prints 'Syncing N stacked descendant(s)' before invoking /stack-sync",
  re.search(r"Syncing[^\n]*stacked descendant", SHIPIT) is not None,
  "expected a 'Syncing N stacked descendant(s) via /stack-sync' progress line")

print()

# ============================================================================
# INVARIANT 22: expert-rebase.md points at the ongoing /stack-sync recipe
# ============================================================================
print("[Invariant 22] expert-rebase.md post-force-push note -> ongoing /stack-sync recipe, no auto-invoke")

t("expert-rebase.md exists and is non-empty",
  len(EXPERT_REBASE.strip()) > 0,
  "expected commands/expert-rebase.md to exist with content")

t("expert-rebase.md references /stack-sync after a force-push",
  "stack-sync" in EXPERT_REBASE,
  "expected the post-force-push note to point at /stack-sync")

t("expert-rebase.md points at the ongoing sync recipe",
  re.search(r"ongoing", EXPERT_REBASE, re.IGNORECASE) is not None,
  "expected the note to reference the ongoing (not post-merge) /stack-sync recipe")

t("expert-rebase.md records why there is no auto-invoke",
  re.search(r"not auto|no auto|does not (run|invoke)|not invoked|manual", EXPERT_REBASE, re.IGNORECASE) is not None,
  "expected an explicit reason why /stack-sync is not auto-invoked after a rebase force-push")

print()

# ============================================================================
# INVARIANT 23: ADR-0012 scoping statements + README index line + CLAUDE.md bullet
# ============================================================================
print("[Invariant 23] ADR-0012 scoping, README 'verified (not ordered)', CLAUDE.md Lifecycle bullet")

t("ADR-0012 scopes the two-gate force-push guarantee to the per-branch arm",
  re.search(r"two.{0,20}gate|two-gate|both gates", ADR_0012, re.IGNORECASE) is not None
  and "per-branch" in ADR_0012,
  "expected the two-gate guarantee (force-with-lease + repo-cache) scoped to the per-branch arm")

t("ADR-0012 states the single-driver arm is ungated by design",
  re.search(r"ungated", ADR_0012, re.IGNORECASE) is not None,
  "expected the single-driver arm declared ungated by design (ADR-0011 blessing)")

t("ADR-0012 describes the repo-cache gate as a build/test gate",
  re.search(r"repo-cache.{0,400}(build|test)", ADR_0012, re.DOTALL | re.IGNORECASE) is not None,
  "expected the repo-cache gate framed as a build/test gate, not a remote-freshness check")

t("ADR-0012 records level-2+ OLD_BASE = captured pre-sync tip",
  re.search(r"pre-sync", ADR_0012, re.IGNORECASE) is not None,
  "expected the level-2+ OLD_BASE decision (parent's captured pre-sync tip) in ADR-0012")

t("ADR-0012 records cleanup's harness-conditional auto-execution",
  "command -v claude" in ADR_0012 or re.search(r"emit-only", ADR_0012, re.IGNORECASE) is not None,
  "expected a Decision bullet on cleanup's 'command -v claude' gating with emit-only fallback")

t("ADR README index describes ordering as 'verified (not ordered)'",
  re.search(r"verified \(not ordered\)", ADR_README) is not None,
  "expected the README index entry to say 'verified (not ordered)'")

t("CLAUDE.md has a /stack-sync Lifecycle bullet",
  "stack-sync" in CLAUDE_MD,
  "expected a '/stack-sync' bullet in CLAUDE.md's Lifecycle command list")

print()
h.summarize_and_exit()
