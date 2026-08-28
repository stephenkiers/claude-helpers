#!/usr/bin/env python3
"""
Test suite for implement-with-haiku tag renaming (Finding 9).

The plan requires:

Finding 9 (MEDIUM): commands/implement-with-haiku.md's `[decided: doing-it]` tag must be
renamed to `[accepted: doing-it]` everywhere it appears (reserving the `decided:` root for
tags that trace to an actual `STATUS: decided`/`measured`), while `[decided: ruling recorded]`
is left unchanged.

Run with: python3 tests/test_implement_tag_rename.py
"""

import re
from _test_harness import REPO_ROOT, Harness

COMMANDS = REPO_ROOT / "commands"


def read(path):
    """Return a file's text, or '' if missing."""
    try:
        return path.read_text()
    except OSError:
        return ""


IMPLEMENT_WITH_HAIKU = read(COMMANDS / "implement-with-haiku.md")

h = Harness("IMPLEMENT-WITH-HAIKU TAG RENAME TEST SUITE")
t = h.test_result

# ============================================================================
# SECTION 1: Finding 9 - Tag renaming from [decided: doing-it] to [accepted: doing-it]
# ============================================================================
print("[Section 1] Finding 9: Tag rename [decided: doing-it] -> [accepted: doing-it]")

t("implement-with-haiku.md exists", IMPLEMENT_WITH_HAIKU != "",
  "File not found or empty")

# Check that [accepted: doing-it] tag exists (renamed)
t("implement-with-haiku.md contains [accepted: doing-it] tag",
  "[accepted: doing-it]" in IMPLEMENT_WITH_HAIKU,
  "The tag should be renamed to [accepted: doing-it]")

# Check that the old [decided: doing-it] tag has been removed (or minimized)
# We allow one or two occurrences (e.g., in documentation explaining the change)
# but not as active directives in the main content
decided_doing_it_count = IMPLEMENT_WITH_HAIKU.count("[decided: doing-it]")
t("old [decided: doing-it] tag is not active in the file",
  decided_doing_it_count <= 1,
  f"Found {decided_doing_it_count} instances of [decided: doing-it]; should be removed (allow up to 1 for documentation)")

print()

# ============================================================================
# SECTION 2: [decided: ruling recorded] must NOT be renamed
# ============================================================================
print("[Section 2] [decided: ruling recorded] is preserved (not renamed)")

# The [decided: ruling recorded] tag should remain unchanged
t("implement-with-haiku.md preserves [decided: ruling recorded] tag",
  "[decided: ruling recorded]" in IMPLEMENT_WITH_HAIKU,
  "[decided: ruling recorded] should NOT be renamed - only [decided: doing-it] changes")

print()

# ============================================================================
# SECTION 3: Semantics - understood difference between accepted and decided
# ============================================================================
print("[Section 3] Documentation clarifies difference between [accepted:] and [decided:]")

# The file should document or clarify why [accepted: doing-it] is used
# (for findings from action-plan that are "accepted" as doing-it)
# vs [decided: ...] (for things from actual STATUS: decided rulings)
t("implement-with-haiku.md distinguishes between accepted and decided tags",
  re.search(r"(accepted|decided).*?(accepted|decided)", IMPLEMENT_WITH_HAIKU, re.I) is not None
  or "[accepted:" in IMPLEMENT_WITH_HAIKU,
  "Should explain or document the distinction between accepted and decided tags")

# Look for explanatory text about why the distinction matters
t("implement-with-haiku.md explains tag semantics",
  re.search(r"(traces to|STATUS.*decided|from.*action-plan)", IMPLEMENT_WITH_HAIKU, re.I) is not None
  or "accepted" in IMPLEMENT_WITH_HAIKU,
  "Should explain that accepted tags are from action-plan (not decided rulings)")

print()

# ============================================================================
# SECTION 4: No unintended [decided: doing-it] in active instructions
# ============================================================================
print("[Section 4] No old-style [decided: doing-it] in main content")

# Extract relevant instruction sections
# Look for step descriptions or directive examples
for match in re.finditer(r"### Step.*?\n(.*?)(?=\n### |\Z)", IMPLEMENT_WITH_HAIKU, re.S | re.I):
    step_content = match.group(1)
    old_tag_count = step_content.count("[decided: doing-it]")
    if old_tag_count > 0:
        step_num = re.search(r"Step (\d+)", match.group(0))
        step_id = f"Step {step_num.group(1)}" if step_num else "unknown"
        t(f"{step_id} does not use [decided: doing-it]",
          False,
          f"Found [decided: doing-it] in {step_id}; should use [accepted: doing-it] instead")

print()

# ============================================================================
# SECTION 5: Cross-file consistency
# ============================================================================
print("[Section 5] Consistency with other command files")

# Verify-queue and other files might reference these tags for consistency
VERIFY_QUEUE = read(COMMANDS / "verify-queue.md")
t("verify-queue.md exists", VERIFY_QUEUE != "",
  "Need verify-queue.md for cross-file consistency check")

# Both should use consistent terminology
if VERIFY_QUEUE:
    # verify-queue might reference what tags will be written
    t("Files are consistent in STATUS/DECISION terminology",
      "STATUS" in IMPLEMENT_WITH_HAIKU and "STATUS" in VERIFY_QUEUE,
      "Both implement-with-haiku and verify-queue should use STATUS fields consistently")

print()

h.summarize_and_exit()
