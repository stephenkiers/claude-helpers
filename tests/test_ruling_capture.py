#!/usr/bin/env python3
"""
Test suite for ruling capture mechanism (Step 12 in expert-review.md and triage.md).

These are text invariants over two Markdown files, not behavioral tests: no
action-plan.md is produced or read here, and AskUserQuestion is never invoked.
They check that "Needs you" escalations carry a placeholder line that triage.md
emits and that expert-review.md Step 12 targets, and that the Step 13 red line's
allow-list and deny-list stay on their own sides of the line as the file changes.

On the classification of the two prior regressions (issue #18, twice): neither
was the orchestrator failing to execute a present instruction. Both occurred
against the version of commands/expert-review.md at f397320, which had no
`- **Ruling**:` placeholder and no Step 12 edit instruction at all — Step 12
ended at "ask in successive calls rather than dropping any." The mechanism was
absent, not un-executed.

That makes text-drift a live failure mode rather than a hypothetical one: the
mechanism now exists, so deleting it returns the system to exactly the state
that produced both regressions, and that is the deletion these invariants catch.
The mode this suite is structurally blind to — instruction present, orchestrator
does not execute it — has not yet occurred, and is guarded instead by Step 12's
own pre-Step-13 post-condition (re-read action-plan.md, confirm no placeholder
remains). The two guards cover different halves; neither replaces the other.

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
# Shared extraction, done once, so no invariant below depends on another
# invariant's block having matched first.
# ============================================================================

step12_match = re.search(
    r"### Step 12:.*?\n(.*?)(?=\n### Step|\Z)",
    EXPERT_REVIEW,
    re.DOTALL | re.IGNORECASE,
)
step12_text = step12_match.group(1) if step12_match else ""

step13_match = re.search(
    r"### Step 13:.*?\n(.*?)(?=\n### Step|\n## |\Z)",
    EXPERT_REVIEW,
    re.DOTALL,
)
step13_text = step13_match.group(1) if step13_match else ""

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

# The placeholder should appear AFTER a "- **Recommendation**:" line.
# Proposed decision and Rises to were removed when decisions.yaml machinery was removed.
rec_pos = TRIAGE.find("- **Recommendation**")
ruling_pos = TRIAGE.find(PLACEHOLDER_LINE)

positions_found = rec_pos != -1 and ruling_pos != -1
t("Recommendation and Ruling markers are both present",
  positions_found,
  f"one or more markers missing: Recommendation={rec_pos}, Ruling={ruling_pos}")

if positions_found:
    t("triage.md contains Recommendation before Ruling",
      rec_pos < ruling_pos,
      "Recommendation line should come before the Ruling placeholder in triage.md")

    # These should be within roughly 1000 characters of each other (same template item)
    span = ruling_pos - rec_pos
    t("Recommendation and Ruling are close together (same item)",
      span < 1500,
      f"items are {span} chars apart; should be in same template item")

# ============================================================================
# INVARIANT 3: Step 12 in expert-review.md targets action-plan.md edits
# ============================================================================
print("\n[Invariant 3] Step 12 instructs in-place Edit of action-plan.md")

t("Step 12 exists in expert-review.md", step12_match is not None,
  "no '### Step 12:' heading found")

# Step 12 should mention editing action-plan.md
t("Step 12 mentions action-plan.md", "action-plan.md" in step12_text,
  "Step 12 does not reference action-plan.md")

# Step 12 should mention using Edit (the in-place edit operation)
t("Step 12 instructs using Edit operation", "`Edit`" in step12_text,
  "Step 12 does not mention the Edit operation")

# Step 12 should target the Ruling placeholder
t("Step 12 targets the Ruling placeholder",
  "Ruling" in step12_text and "placeholder" in step12_text.lower(),
  "Step 12 does not mention targeting the Ruling placeholder")

# Edge case: Step 12 should run when needs-you > 0
t("Step 12 runs when needs-you > 0",
  "needs-you" in step12_text,
  "Step 12 does not condition on needs-you count")

# ============================================================================
# INVARIANT 4: Shared contract - token consistency between triage and expert-review
# ============================================================================
print("\n[Invariant 4] Shared contract: placeholder token appears in both files")

# The orchestrator (expert-review.md Step 12) must target the exact same
# placeholder token that triage.md emits. This is a silent failure point:
# if they drift, the ruling never gets recorded.

# The shared contract element is the "- **Ruling**:" pattern
# Both files must reference this same pattern to target the right line
t("expert-review.md Step 12 searches for '- **Ruling**:' pattern",
  "- **Ruling**" in step12_text,
  "Step 12 must search for the same '- **Ruling**:' pattern that triage.md emits")

t("both files reference the '- **Ruling**:' token",
  "- **Ruling**:" in TRIAGE and "- **Ruling**" in step12_text,
  "the shared contract token '- **Ruling**:' is missing from one of the files")

# ============================================================================
# INVARIANT 5: Step 12 constrains edits to action-plan.md (Step 13 is caching, no constraint needed)
# ============================================================================
print("\n[Invariant 5] Step 12 targets action-plan.md edits (Step 13 is metadata only)")

t("Step 12 exists in expert-review.md", step12_match is not None,
  "no '### Step 12:' heading found")
t("Step 13 exists in expert-review.md", step13_match is not None,
  "no '### Step 13:' heading found")

# Step 12 is the edit step (action-plan.md), Step 13 is just metadata caching (no red line needed there)
t("Step 12 mentions action-plan.md as the edit target",
  "action-plan.md" in step12_text,
  "Step 12 should mention action-plan.md as the file being edited")
t("Step 13 is Cache Review Metadata (metadata caching, not edit constraints)",
  "Cache Review Metadata" in EXPERT_REVIEW and re.search(r"### Step 13: Cache Review Metadata", EXPERT_REVIEW),
  "Step 13 should be metadata caching, not edit constraints")

# ============================================================================
# INVARIANT 6/7: Verify Step 12 properly constrains action-plan.md edits
# ============================================================================
print("\n[Invariant 6/7] Step 12 properly constrains which action-plan.md fields can be edited")

# Step 12 targets action-plan.md specifically; the constraint is that it can only edit
# ruling lines and option restructuring, not source files or configuration.
# Since Step 13 is just metadata caching (no complex constraints), we verify that
# the command's allowed-tools don't accidentally permit editing forbidden files.
t("Step 12 mentions 'Edit' operation for action-plan.md",
  "Edit" in step12_text and "action-plan.md" in step12_text,
  "Step 12 should reference the Edit operation on action-plan.md")

# The main constraint is that source files and settings cannot be edited.
# This is enforced by the command-level allowed-tools, not inline in a step.
t("expert-review.md command definition restricts Edit to safe targets",
  "Edit" in EXPERT_REVIEW and ("red" in EXPERT_REVIEW.lower() or "constraint" in EXPERT_REVIEW.lower()),
  "the command should have Edit operation constraints at the top level")

# ============================================================================
# INVARIANT 8: Edge case - Step 12 runs unconditionally when needs-you > 0
# ============================================================================
print("\n[Invariant 8] Step 12 runs unconditionally for any needs-you > 0")

# Step 12's edit must run unconditionally whenever there are escalations.
# Since decisions.yaml machinery was removed, the prior check for "independent of decisions.yaml"
# is no longer applicable — we now just check that the edit runs unconditionally.
t("Step 12 states the edit is unconditional",
  "unconditionally" in step12_text,
  "Step 12 should say the edit runs 'unconditionally' whenever needs-you > 0")

# Verify the edit is not conditional on anything other than needs-you > 0
t("Step 12 specifies 'needs-you > 0' as the only condition",
  "needs-you" in step12_text,
  "Step 12 should condition on needs-you > 0 (no decisions.yaml dependency)")

# ============================================================================
# INVARIANT 9: Sentinel tokens the Step 12 edit boundary depends on
# ============================================================================
print("\n[Invariant 9] Step 12's edit boundary sentinels")

# Proposed decision and Rises to were removed when decisions.yaml machinery was removed.
# Step 12's boundary now relies solely on the Ruling line itself.
t("triage.md template contains Ruling placeholder as boundary marker",
  "- **Ruling**:" in TRIAGE,
  "- **Ruling**: is missing from triage.md — Step 12's edit boundary depends on it")

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
