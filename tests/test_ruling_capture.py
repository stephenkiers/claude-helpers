#!/usr/bin/env python3
"""
Test suite for ruling capture mechanism (Step 12 in expert-review.md and triage.md).

Verifies that "Needs you" escalations record their rulings durably in action-plan.md,
not just in the conversation transcript, when the human decides via AskUserQuestion.

The shared contract between the two files: each escalation has a placeholder line
that triage.md emits, and expert-review.md Step 12 fills in after answers come back.

Run with: python3 tests/test_ruling_capture.py
"""

import re

from _test_harness import REPO_ROOT, Harness

COMMANDS = REPO_ROOT / "commands"
PROMPTS = REPO_ROOT / "prompts"


def read(path):
    """Return a file's text, or '' if missing."""
    try:
        return path.read_text()
    except OSError:
        return ""


EXPERT_REVIEW = read(COMMANDS / "expert-review.md")
TRIAGE = read(PROMPTS / "triage.md")

h = Harness("RULING CAPTURE MECHANISM TEST SUITE")
t = h.test_result

# ============================================================================
# INVARIANT 1: The exact placeholder line exists in triage.md
# ============================================================================
print("[Invariant 1] Exact placeholder line present in triage.md")

# The exact placeholder line as specified in the plan
PLACEHOLDER_LINE = "- **Ruling**: _(pending your call — recorded here after you decide)_"

t("exact placeholder line is present in triage.md", PLACEHOLDER_LINE in TRIAGE,
  f"expected: {PLACEHOLDER_LINE!r}\nnot found in triage.md")

# Edge case: verify the em dash is present (not a regular hyphen)
t("placeholder uses em dash (—) not regular hyphen (-)",
  "— recorded here after you decide)_" in TRIAGE,
  "the em dash character was replaced with a regular hyphen")

# Edge case: verify underscores surround the pending state
t("pending state is wrapped in underscores (_pending...)",
  "_(pending your call" in TRIAGE,
  "underscores around the pending state are missing or malformed")

# ============================================================================
# INVARIANT 2: Placeholder position in the "Needs you" template
# ============================================================================
print("\n[Invariant 2] Placeholder is in the right position in the template")

# Find the "Needs you" item template section. It should be under a section like
# "## Needs you" or similar, with a template showing the structure of an item.
# The placeholder should appear AFTER a "- **Recommendation**:" line
# and BEFORE a "- **Proposed decision**:" line

# Search for the placeholder and verify ordering in the triage file overall
# (the section extraction might not work if the template is structured differently)

t("triage.md contains Recommendation before Ruling",
  TRIAGE.find("- **Recommendation**") < TRIAGE.find(PLACEHOLDER_LINE),
  "Recommendation line should come before the Ruling placeholder in triage.md")

t("triage.md contains Ruling before Proposed decision",
  TRIAGE.find(PLACEHOLDER_LINE) < TRIAGE.find("- **Proposed decision**"),
  "Ruling placeholder should come before Proposed decision in triage.md")

# Verify the ordering is in sequence (all three within a small region, suggesting
# they're part of the same template)
rec_pos = TRIAGE.find("- **Recommendation**")
ruling_pos = TRIAGE.find(PLACEHOLDER_LINE)
decision_pos = TRIAGE.find("- **Proposed decision**")

if rec_pos != -1 and ruling_pos != -1 and decision_pos != -1:
    # These should be within roughly 1000 characters of each other (same template item)
    span = decision_pos - rec_pos
    t("Recommendation, Ruling, and Proposed decision are close together (same item)",
      span < 1500,
      f"items are {span} chars apart; should be in same template item")

# ============================================================================
# INVARIANT 3: Step 12 in expert-review.md targets action-plan.md edits
# ============================================================================
print("\n[Invariant 3] Step 12 instructs in-place Edit of action-plan.md")

# Find Step 12 - look for "### Step 12:" heading
step12_match = re.search(
    r"### Step 12:.*?\n(.*?)(?=\n### Step|\Z)",
    EXPERT_REVIEW,
    re.DOTALL | re.IGNORECASE
)

t("Step 12 exists in expert-review.md", step12_match is not None,
  "no '### Step 12:' heading found")

if step12_match:
    step12_text = step12_match.group(1)

    # Step 12 should mention editing action-plan.md
    t("Step 12 mentions action-plan.md", "action-plan.md" in step12_text or "action_plan" in step12_text.replace("-", "_"),
      "Step 12 does not reference action-plan.md")

    # Step 12 should mention using Edit (the in-place edit operation)
    t("Step 12 instructs using Edit operation", "Edit" in step12_text or "`Edit`" in step12_text,
      "Step 12 does not mention the Edit operation")

    # Step 12 should target the Ruling placeholder
    t("Step 12 targets the Ruling placeholder",
      "Ruling" in step12_text and ("placeholder" in step12_text.lower() or "replace" in step12_text.lower()),
      "Step 12 does not mention targeting the Ruling placeholder")

# Edge case: Step 12 should run when needs-you > 0
if step12_match:
    t("Step 12 runs when needs-you > 0",
      "needs-you" in step12_text or "needs_you" in step12_text or "escalation" in step12_text.lower(),
      "Step 12 does not condition on needs-you count")

# ============================================================================
# INVARIANT 4: Shared contract - token consistency between triage and expert-review
# ============================================================================
print("\n[Invariant 4] Shared contract: placeholder token appears in both files")

# The orchestrator (expert-review.md Step 12) must target the exact same
# placeholder token that triage.md emits. This is a silent failure point:
# if they drift, the ruling never gets recorded.

t("placeholder line is present in triage.md (contract point 1)",
  PLACEHOLDER_LINE in TRIAGE,
  "triage.md must emit the exact placeholder line")

# The shared contract element is the "- **Ruling**:" pattern
# Both files must reference this same pattern to target the right line
t("expert-review.md Step 12 searches for '- **Ruling**:' pattern",
  step12_match is not None and "- **Ruling**" in (step12_match.group(1) if step12_match else ""),
  "Step 12 must search for the same '- **Ruling**:' pattern that triage.md emits")

if step12_match:
    step12_text = step12_match.group(1)
    # The shared contract is the "- **Ruling**:" token that both files use
    ruling_pattern_in_triage = "- **Ruling**:" in TRIAGE
    ruling_pattern_in_step12 = "- **Ruling**" in step12_text

    t("both files reference the '- **Ruling**:' token",
      ruling_pattern_in_triage and ruling_pattern_in_step12,
      "the shared contract token '- **Ruling**:' is missing from one of the files")

# ============================================================================
# INVARIANT 5: Step 13 red line allows action-plan.md
# ============================================================================
print("\n[Invariant 5] Step 13 red line permits action-plan.md as Edit target")

# Find Step 13 - look for "### Step 13:" or similar
step13_match = re.search(
    r"### Step 13:.*?\n(.*?)(?=\n### Step|\n## |\Z)",
    EXPERT_REVIEW,
    re.DOTALL
)

t("Step 13 exists in expert-review.md", step13_match is not None,
  "no '### Step 13:' heading found")

if step13_match:
    step13_text = step13_match.group(1)

    # Step 13 should have a red-line section that describes what can be edited
    # Look for language like "red line", "forbid", "must not", "only", "allowed", etc.
    has_red_line = bool(re.search(r"red.?line|forbidden|must not", step13_text, re.IGNORECASE))
    t("Step 13 contains a red line constraint", has_red_line,
      "no red line section found in Step 13")

    # The red line should name action-plan.md as an allowed target
    t("Step 13 red line names action-plan.md as an allowed Edit target",
      "action-plan.md" in step13_text or "action_plan" in step13_text.replace("-", "_"),
      "action-plan.md is not listed as an allowed Edit target in the red line")

# ============================================================================
# INVARIANT 6: Step 13 red line still forbids prohibited files/directories
# ============================================================================
print("\n[Invariant 6] Step 13 red line forbids prohibited files and directories")

if step13_match:
    step13_text = step13_match.group(1)

    # Extract just the red line section (look for a subsection or paragraph about constraints)
    red_line_section = re.search(
        r"(?:red.?line|forbidden|must not|cannot|prohibited)(.*?)(?=\n\n[A-Z]|\Z)",
        step13_text,
        re.DOTALL | re.IGNORECASE
    )

    if red_line_section:
        red_line_text = red_line_section.group(1)

        # These files/directories must be forbidden
        prohibited = {
            ".claude/settings.json": ".claude/settings.json",
            "CLAUDE.md": "CLAUDE.md",
            "agents/": "agents/ directory",
            "reviewers/": "reviewers/ directory"
        }

        for pattern, label in prohibited.items():
            t(f"red line forbids edits to {label}",
              pattern in red_line_text,
              f"the prohibition of {label} is missing from the red line")

# Edge case: source files should also be forbidden
if step13_match:
    step13_text = step13_match.group(1)
    red_line_section = re.search(
        r"(?:red.?line|forbidden|must not|cannot|prohibited)(.*?)(?=\n\n[A-Z]|\Z)",
        step13_text,
        re.DOTALL | re.IGNORECASE
    )
    if red_line_section:
        red_line_text = red_line_section.group(1)
        t("red line forbids edits to source files",
          bool(re.search(r"source\s+file|\.py|\.ts|\.go|\.rs|code\s+file", red_line_text, re.IGNORECASE)),
          "prohibition of source files is missing or unclear in the red line")

# ============================================================================
# INVARIANT 7: The prohibition half survived (critical security constraint)
# ============================================================================
print("\n[Invariant 7] Prohibition half of red line survived intact")

# This invariant verifies that adding action-plan.md to the allowed list
# did NOT accidentally weaken the constraints on prohibited files.

if step13_match:
    step13_text = step13_match.group(1)

    # The red line should use language that makes it clear these are FORBIDDEN
    # Patterns like "must not edit", "forbidden from", "cannot write", "do not touch", etc.
    has_prohibition_language = bool(re.search(
        r"(?:must not|cannot|forbidden|never|prohibited|do not)\s+(?:edit|write|touch|modify)",
        step13_text,
        re.IGNORECASE
    ))

    t("red line uses prohibition language (must not, forbidden, cannot, etc.)",
      has_prohibition_language,
      "the red line lacks clear prohibition language")

# Edge case: verify that .claude/settings.json is explicitly named as forbidden
# (not just "no ~/.claude files" which could accidentally allow action-plan.md)
if step13_match:
    step13_text = step13_match.group(1)
    t("red line explicitly names .claude/settings.json as forbidden",
      ".claude/settings.json" in step13_text,
      "settings.json should be explicitly forbidden, not just vaguely")

# ============================================================================
# INVARIANT 8: Edge case - Step 12 runs unconditionally when needs-you > 0
# ============================================================================
print("\n[Invariant 8] Step 12 runs unconditionally for any needs-you > 0")

if step12_match:
    step12_text = step12_match.group(1)

    # Step 12 must NOT be conditional on recording a decision to decisions.yaml
    # i.e., the edit must happen regardless of whether the user wants to record
    # a general policy decision
    t("Step 12 edit is independent of decisions.yaml recording",
      "unconditional" in step12_text.lower() or "whenever" in step12_text.lower() or "any" in step12_text.lower(),
      "Step 12 should run unconditionally when needs-you > 0, not depend on decisions.yaml")

# ============================================================================
# INVARIANT 9: Edge case - the ruling line syntax is markdown-valid
# ============================================================================
print("\n[Invariant 9] Placeholder line is valid markdown list syntax")

# The placeholder must be a valid markdown list item
# Format: "- **Ruling**: _(pending your call — recorded here after you decide)_"
#
# This is:
# - A bullet point (starts with "- ")
# - A bold label (**Ruling**:)
# - Italic content (_(text)_)

t("placeholder uses valid markdown bold for label", "**Ruling**:" in PLACEHOLDER_LINE,
  "bold syntax **Ruling**: is malformed")

t("placeholder uses valid markdown italic for content",
  "_(pending your call" in PLACEHOLDER_LINE and "decide)_" in PLACEHOLDER_LINE,
  "italic syntax _(...)_ is malformed")

# ============================================================================
# INVARIANT 10: Edge case - placeholder is not a duplicate
# ============================================================================
print("\n[Invariant 10] Placeholder line appears exactly once in triage.md")

placeholder_count = TRIAGE.count(PLACEHOLDER_LINE)
t("placeholder line appears in triage.md", placeholder_count > 0,
  "placeholder not found at all")

t("placeholder line appears exactly once (not duplicated)",
  placeholder_count == 1,
  f"found {placeholder_count} copies; only 1 expected (template templates should not be duplicated)")

h.summarize_and_exit()
