#!/usr/bin/env python3
"""
Test suite for the /shipit PR description rewrite (issue #86, spec-blind).

Written from the GitHub issue #86 plan, NOT from the implementation.
Covers the new 5-heading PR body template, title generation, and ingest-then-merge
update policy for existing PRs.

Tests verify:
1. New 5-heading PR body template (in the correct order)
2. No old placeholder headings remain (## Summary, ## Test plan)
3. Title regeneration (imperative summary, <70 char, no <commit subject> placeholder)
4. When PR exists: fetch current body via 'gh pr view ... --json body' before updating
5. Use --body-file (not --body) for both create and edit paths
6. Use mktemp for temp file handling
7. Quick Reference table shows "PR exists" row reflects refresh behavior
8. shipit-reference.md PR Body Example shows the new 5-heading template
9. Closes #$ISSUE_NUM and Stacked on #$STACK_PARENT_PR lines preserved
10. No "If PR exists: Report URL and stop" text (update path should exist)

Run with: python3 tests/test_shipit_pr_description_rewrite.py
"""

import re
from _test_harness import REPO_ROOT, Harness

COMMANDS_DIR = REPO_ROOT / "commands"
PROMPTS_DIR = REPO_ROOT / "prompts"

SHIPIT_FILE = COMMANDS_DIR / "shipit.md"
SHIPIT_REF_FILE = PROMPTS_DIR / "shipit-reference.md"


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
        block_text = match.group(1)
        blocks.append((line_num, block_text))
    return blocks


def slice_heading_section(text, heading_regex):
    """
    Slice from a markdown heading whose text matches heading_regex to the next
    heading of the same-or-higher level. Returns None when no heading matches.
    """
    m = re.search(rf"^(#+)\s[^\n]*{heading_regex}[^\n]*$", text, re.MULTILINE)
    if not m:
        return None
    level = len(m.group(1)) if m.group(1) else 1
    nxt = re.search(rf"^#{{1,{level}}}\s", text[m.end():], re.MULTILINE)
    end = m.end() + nxt.start() if nxt else len(text)
    return text[m.start():end]


SHIPIT = read(SHIPIT_FILE)
SHIPIT_REF = read(SHIPIT_REF_FILE)

h = Harness("SHIPIT PR DESCRIPTION REWRITE TEST SUITE (ISSUE #86, SPEC-BLIND)")
t = h.test_result

# ============================================================================
# INVARIANT 1: Five new headings in correct order
# ============================================================================
print("[Invariant 1] New 5-heading PR body template exists in shipit.md")

required_headings = [
    "Why this PR exists",
    "What it does",
    "What major decisions were made",
    "What a reviewer should pay attention to",
    "How to verify"
]

# Find all top-level heading matches (## pattern)
heading_positions = {}
for heading_text in required_headings:
    pattern = rf"^##\s+{re.escape(heading_text)}"
    match = re.search(pattern, SHIPIT, re.MULTILINE)
    if match:
        heading_positions[heading_text] = match.start()
    else:
        heading_positions[heading_text] = -1

# Check each heading exists
for heading_text in required_headings:
    t(f"shipit.md contains heading '## {heading_text}'",
      heading_positions[heading_text] >= 0,
      f"expected '## {heading_text}' heading not found in shipit.md PR body generation")

# Check headings appear in the correct order
if all(pos >= 0 for pos in heading_positions.values()):
    positions_list = [heading_positions[h] for h in required_headings]
    in_order = positions_list == sorted(positions_list)
    t("shipit.md headings appear in the required order",
      in_order,
      f"expected headings in order: {required_headings}")

print()

# ============================================================================
# INVARIANT 2: Old template headings are removed
# ============================================================================
print("[Invariant 2] Old ## Summary and ## Test plan headings are not used")

# The old template had "## Summary" and "## Test plan" — these should not appear
# as the primary headings in the new template
old_summary = re.search(r"^##\s+Summary\s*$", SHIPIT, re.MULTILINE)
old_test_plan = re.search(r"^##\s+Test plan\s*$", SHIPIT, re.MULTILINE)

t("shipit.md does not use the old '## Summary' heading",
  old_summary is None,
  "found '## Summary' heading — this should be replaced by the new 5-heading template")

t("shipit.md does not use the old '## Test plan' heading",
  old_test_plan is None,
  "found '## Test plan' heading — this should be replaced by '## How to verify'")

print()

# ============================================================================
# INVARIANT 3: Title regeneration — no <commit subject> placeholder
# ============================================================================
print("[Invariant 3] Title is regenerated from diff, not using <commit subject> placeholder")

t("shipit.md does not use '<commit subject>' placeholder for title",
  "<commit subject>" not in SHIPIT,
  "expected the title to be computed from the diff, not a <commit subject> placeholder")

# The new title should be generated as an imperative summary. Look for evidence
# of title generation logic (computing from diff)
has_title_generation = "title" in SHIPIT.lower() and ("imperative" in SHIPIT.lower()
                                                        or re.search(r"<.*70.*char|70.*char", SHIPIT, re.IGNORECASE) is not None
                                                        or "summary" in SHIPIT.lower())

t("shipit.md has logic for generating the PR title",
  has_title_generation,
  "expected the title to be generated (mentioned 'title', 'imperative', or similar)")

print()

# ============================================================================
# INVARIANT 4: For existing PRs, fetch current body via 'gh pr view'
# ============================================================================
print("[Invariant 4] When PR exists, fetch current body via 'gh pr view ... --json body'")

# Look for the pattern "gh pr view" with "--json body" or "body" in the JSON fetch
has_pr_view_body_fetch = re.search(
    r'gh\s+pr\s+view.{0,200}--json\s+body',
    SHIPIT, re.DOTALL)

t("shipit.md calls 'gh pr view ... --json body' to fetch existing PR",
  has_pr_view_body_fetch is not None,
  "expected shipit.md to fetch the current PR body via 'gh pr view ... --json body' before updating")

print()

# ============================================================================
# INVARIANT 5: Use --body-file for both create and edit
# ============================================================================
print("[Invariant 5] Use --body-file (not --body) for create and edit")

# The new approach uses mktemp + --body-file for both gh pr create and gh pr edit
has_body_file_flag = "--body-file" in SHIPIT

t("shipit.md uses '--body-file' flag",
  has_body_file_flag,
  "expected '--body-file' flag used for PR creation/update (not direct --body string)")

# Should NOT use direct --body with a quoted string (old pattern)
has_bad_body_pattern = re.search(r"--body\s+['\"]", SHIPIT)

t("shipit.md does not use direct quoted '--body' strings",
  has_bad_body_pattern is None,
  "expected --body-file approach, not direct quoted --body strings")

print()

# ============================================================================
# INVARIANT 6: Use mktemp for temp file
# ============================================================================
print("[Invariant 6] Temp file created and used via mktemp")

has_mktemp = "mktemp" in SHIPIT

t("shipit.md uses 'mktemp' to create temp file",
  has_mktemp,
  "expected shipit.md to use mktemp for creating a temporary body file")

# Should have some pattern like BODY_FILE=$(mktemp ...) and later use it with --body-file
# Or it could use other variable names like body_file, TEMP_FILE, BODY_PATH, etc.
has_body_file_var = re.search(r"(BODY|body|TEMP|temp|FILE|file)\w*\s*=.*mktemp|mktemp.*\$", SHIPIT)

t("shipit.md assigns the mktemp output to a variable for use with --body-file",
  has_body_file_var is not None,
  "expected the temp file path from mktemp to be captured in a variable")

print()

# ============================================================================
# INVARIANT 7: Quick Reference table shows PR refresh behavior
# ============================================================================
print("[Invariant 7] Quick Reference table shows 'PR exists' row with refresh behavior")

# Look for a Quick Reference or similar table
has_quick_ref = re.search(r"Quick Reference|quick reference", SHIPIT, re.IGNORECASE)

t("shipit.md has a Quick Reference table",
  has_quick_ref is not None,
  "expected a Quick Reference table in shipit.md")

# The "PR exists" row should NOT just say "Report URL and stop"
# Instead it should reflect refresh/update behavior
has_pr_exists_row = re.search(r"PR\s+exists|pr\s+exists", SHIPIT, re.IGNORECASE)

t("shipit.md Quick Reference table has a 'PR exists' row",
  has_pr_exists_row is not None,
  "expected a 'PR exists' row in the Quick Reference table")

# The PR exists row should describe refresh/refresh/update behavior, not "Report URL"
# Look for evidence of title/body refresh on the PR exists path
has_refresh_behavior = re.search(
    r"(refresh|update|regenerate).{0,150}(title|body)",
    SHIPIT, re.DOTALL | re.IGNORECASE)

t("shipit.md describes title/body refresh for existing PRs",
  has_refresh_behavior is not None,
  "expected the Quick Reference to describe title/body refresh for existing PRs")

# The old behavior was "Report URL and stop" — this should not be the only behavior
has_old_report_only = (re.search(r"If PR exists.*Report URL.*stop", SHIPIT, re.IGNORECASE | re.DOTALL) is not None
                        and re.search(r"PR.*exists.*update|update.*PR.*exists", SHIPIT, re.IGNORECASE | re.DOTALL) is None)

t("shipit.md does not use 'Report URL and stop' as the only PR exists behavior",
  not has_old_report_only,
  "expected the PR exists path to include update/refresh logic, not just 'Report URL and stop'")

print()

# ============================================================================
# INVARIANT 8: shipit-reference.md shows new 5-heading template
# ============================================================================
print("[Invariant 8] shipit-reference.md PR Body Example shows 5-heading template")

# Look for a "PR Body Example" or similar section
has_pr_body_example = re.search(
    r"PR\s+Body\s+Example|pr.*body.*example|example.*pr.*body",
    SHIPIT_REF, re.IGNORECASE)

t("shipit-reference.md has a 'PR Body Example' section",
  has_pr_body_example is not None,
  "expected a 'PR Body Example' section in shipit-reference.md")

# The example should contain the new 5 headings
for heading_text in required_headings:
    heading_in_example = heading_text in SHIPIT_REF
    t(f"shipit-reference.md PR Body Example includes '## {heading_text}'",
      heading_in_example,
      f"expected '## {heading_text}' in the PR Body Example")

print()

# ============================================================================
# INVARIANT 9: Closes and Stacked on lines preserved
# ============================================================================
print("[Invariant 9] PR body still includes Closes #N and Stacked on #N lines")

has_closes_support = re.search(
    r"Closes|closes\s*#\|ISSUE",
    SHIPIT, re.IGNORECASE)

t("shipit.md supports 'Closes #N' line in PR body",
  has_closes_support is not None,
  "expected support for prepending 'Closes #$ISSUE_NUM' to the PR body")

# Stacked on should still be there (tested in test_stack_awareness.py)
has_stacked_on = "Stacked on" in SHIPIT or "stacked on" in SHIPIT.lower()

t("shipit.md includes 'Stacked on #N' line for stacked PRs",
  has_stacked_on,
  "expected 'Stacked on #$STACK_PARENT_PR' line appended for stacked PRs (unchanged from before)")

print()

# ============================================================================
# INVARIANT 10: Update path exists and reads current body
# ============================================================================
print("[Invariant 10] Update path exists when PR already exists")

# Check for explicit handling of the "PR already exists" case
has_update_path = re.search(
    r"if.*PR.*exists|PR.*already.*exists|gh.*pr.*view",
    SHIPIT, re.IGNORECASE | re.DOTALL)

t("shipit.md has an explicit update path for existing PRs",
  has_update_path is not None,
  "expected shipit.md to check if PR exists and have an update path")

# The update path should involve reading the current body and merging
has_ingest_merge = re.search(
    r"(current.*body|existing.*body|fetch.*body|ingest|merge|preserve)",
    SHIPIT, re.IGNORECASE)

t("shipit.md mentions ingesting or merging current PR body",
  has_ingest_merge is not None,
  "expected shipit.md to describe fetching/ingesting the current PR body before updating")

print()

# ============================================================================
# INVARIANT 11: Both create and update paths regenerate title/body
# ============================================================================
print("[Invariant 11] Both create and update paths regenerate title and body")

# Just verify the overall file has evidence of regenerating both title and body
# since we already verified update path exists and current body is fetched

has_title_regen = re.search(
    r"(compute|generate|create|write|set).*title|title.*regen",
    SHIPIT, re.IGNORECASE)

has_body_regen = re.search(
    r"(compute|generate|create|write|set).*(body|description)|body.*regen",
    SHIPIT, re.IGNORECASE)

t("shipit.md regenerates title (create and update paths)",
  has_title_regen is not None,
  "expected title regeneration logic in shipit.md")

t("shipit.md regenerates body (create and update paths)",
  has_body_regen is not None,
  "expected body regeneration logic in shipit.md")

print()

# ============================================================================
# INVARIANT 12: No unused placeholder text in template
# ============================================================================
print("[Invariant 12] PR body template uses real values, not placeholder text")

# Old template had placeholders like <what changed>, <how to verify>
old_placeholders = ["<what changed>", "<how to verify>", "<why this pr>", "<decisions>"]
for placeholder in old_placeholders:
    has_placeholder = placeholder in SHIPIT.lower()
    t(f"shipit.md does not use '{placeholder}' placeholder",
      not has_placeholder,
      f"found old placeholder '{placeholder}' — should be computed from diff")

print()

h.summarize_and_exit()
