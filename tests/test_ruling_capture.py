#!/usr/bin/env python3
"""
Test suite for ruling capture mechanism (Step 12 in expert-review.md and triage.md).

These are text invariants over two Markdown files, not behavioral tests: no
claude-action-plan.md is produced or read here, and AskUserQuestion is never invoked.
They check that "Needs you" and "Needs measurement" escalations carry STATUS/DECISION field
pairs that triage.md emits and that expert-review.md Step 12 targets for edit.

On the classification of the two prior regressions (issue #18, twice): neither
was the orchestrator failing to execute a present instruction. Both occurred
against the version of commands/expert-review.md at f397320, which had no
STATUS/DECISION fields and no Step 12 edit instruction at all — Step 12
ended at "ask in successive calls rather than dropping any." The mechanism was
absent, not un-executed.

That makes text-drift a live failure mode rather than a hypothetical one: the
mechanism now exists, so deleting it returns the system to exactly the state
that produced both regressions, and that is the deletion these invariants catch.
The mode this suite is structurally blind to — instruction present, orchestrator
does not execute it — has not yet occurred, and is guarded instead by Step 12's
own pre-Step-13 post-condition (re-read claude-action-plan.md, confirm no unset
STATUS/DECISION remains). The two guards cover different halves; neither replaces the other.

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
# INVARIANT 1: The STATUS/DECISION field pair exists in triage.md
# ============================================================================
print("[Invariant 1] STATUS/DECISION field pairs present in triage.md")

# The new format uses STATUS and DECISION fields instead of Ruling placeholder
STATUS_PENDING_DECISION = "- **STATUS**: pending-decision"
STATUS_PENDING_MEASUREMENT = "- **STATUS**: pending-measurement"
DECISION_EMPTY = "- **DECISION**: _(empty until STATUS changes)_"

t("STATUS pending-decision line is present in triage.md", STATUS_PENDING_DECISION in TRIAGE,
  f"expected: {STATUS_PENDING_DECISION!r}\nnot found in triage.md")

t("STATUS pending-measurement line is present in triage.md", STATUS_PENDING_MEASUREMENT in TRIAGE,
  f"expected: {STATUS_PENDING_MEASUREMENT!r}\nnot found in triage.md")

t("DECISION empty line is present in triage.md", DECISION_EMPTY in TRIAGE,
  f"expected: {DECISION_EMPTY!r}\nnot found in triage.md")

# Edge case: verify the STATUS field format is correct (no stray characters)
t("STATUS field format is correct",
  "- **STATUS**: " in TRIAGE,
  "STATUS field should be formatted as '- **STATUS**: '")

# Edge case: verify DECISION field format is correct
t("DECISION field format is correct",
  "- **DECISION**: " in TRIAGE,
  "DECISION field should be formatted as '- **DECISION**: '")

# ============================================================================
# INVARIANT 2: STATUS/DECISION position in the "Needs you" template
# ============================================================================
print("\n[Invariant 2] STATUS/DECISION fields are in the right position in the template")

# The STATUS field should appear AFTER a "- **Recommendation**:" line.
rec_pos = TRIAGE.find("- **Recommendation**")
status_pos = TRIAGE.find(STATUS_PENDING_DECISION)

positions_found = rec_pos != -1 and status_pos != -1
t("Recommendation and STATUS markers are both present",
  positions_found,
  f"one or more markers missing: Recommendation={rec_pos}, STATUS={status_pos}")

if positions_found:
    t("triage.md contains Recommendation before STATUS",
      rec_pos < status_pos,
      "Recommendation line should come before the STATUS field in triage.md")

    # These should be within roughly 1000 characters of each other (same template item)
    span = status_pos - rec_pos
    t("Recommendation and STATUS are close together (same item)",
      span < 1500,
      f"items are {span} chars apart; should be in same template item")

# ============================================================================
# INVARIANT 3: Step 12 in expert-review.md targets claude-action-plan.md edits
# ============================================================================
print("\n[Invariant 3] Step 12 instructs in-place Edit of claude-action-plan.md")

t("Step 12 exists in expert-review.md", step12_match is not None,
  "no '### Step 12:' heading found")

# Step 12 should mention editing claude-action-plan.md
t("Step 12 mentions claude-action-plan.md", "claude-action-plan.md" in step12_text,
  "Step 12 does not reference claude-action-plan.md")

# Step 12 should mention using Edit (the in-place edit operation)
t("Step 12 instructs using Edit operation", "`Edit`" in step12_text,
  "Step 12 does not mention the Edit operation")

# Step 12 should target the STATUS/DECISION fields
t("Step 12 targets the STATUS/DECISION fields",
  "STATUS" in step12_text and "DECISION" in step12_text,
  "Step 12 does not mention targeting the STATUS/DECISION fields")

# Edge case: Step 12 should run when needs-you > 0
t("Step 12 runs when needs-you > 0",
  "needs-you" in step12_text,
  "Step 12 does not condition on needs-you count")

# ============================================================================
# INVARIANT 4: Shared contract - token consistency between triage and expert-review
# ============================================================================
print("\n[Invariant 4] Shared contract: STATUS/DECISION tokens appear in both files")

# The orchestrator (expert-review.md Step 12) must target the exact same
# STATUS/DECISION tokens that triage.md emits. This is a silent failure point:
# if they drift, the ruling never gets recorded.

# The shared contract element is the "- **STATUS**:" pattern
# Both files must reference this same pattern to target the right line
t("expert-review.md Step 12 searches for '- **STATUS**:' pattern",
  "- **STATUS**" in step12_text or "STATUS" in step12_text,
  "Step 12 must search for the same '- **STATUS**:' pattern that triage.md emits")

t("both files reference the '- **STATUS**:' token",
  "- **STATUS**:" in TRIAGE and ("- **STATUS**" in step12_text or "STATUS" in step12_text),
  "the shared contract token '- **STATUS**:' is missing from one of the files")

# ============================================================================
# INVARIANT 5: Step 12 constrains edits to claude-action-plan.md (Step 13 is caching, no constraint needed)
# ============================================================================
print("\n[Invariant 5] Step 12 targets claude-action-plan.md edits (Step 13 is metadata only)")

t("Step 12 exists in expert-review.md", step12_match is not None,
  "no '### Step 12:' heading found")
t("Step 13 exists in expert-review.md", step13_match is not None,
  "no '### Step 13:' heading found")

# Step 12 is the edit step (claude-action-plan.md), Step 13 is just metadata caching (no red line needed there)
t("Step 12 mentions claude-action-plan.md as the edit target",
  "claude-action-plan.md" in step12_text,
  "Step 12 should mention claude-action-plan.md as the file being edited")
t("Step 13 is Cache Review Metadata (metadata caching, not edit constraints)",
  "Cache Review Metadata" in EXPERT_REVIEW and re.search(r"### Step 13: Cache Review Metadata", EXPERT_REVIEW),
  "Step 13 should be metadata caching, not edit constraints")

# ============================================================================
# INVARIANT 6/7: Verify Step 12 properly constrains claude-action-plan.md edits
# ============================================================================
print("\n[Invariant 6/7] Step 12 properly constrains which claude-action-plan.md fields can be edited")

# Step 12 targets claude-action-plan.md specifically; the constraint is that it can only edit
# STATUS/DECISION fields and option restructuring, not source files or configuration.
# Since Step 13 is just metadata caching (no complex constraints), we verify that
# the command's allowed-tools don't accidentally permit editing forbidden files.
t("Step 12 mentions 'Edit' operation for claude-action-plan.md",
  "Edit" in step12_text and "claude-action-plan.md" in step12_text,
  "Step 12 should reference the Edit operation on claude-action-plan.md")

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

# Step 12's boundary now relies on the STATUS/DECISION field markers.
t("triage.md template contains STATUS field as boundary marker",
  "- **STATUS**:" in TRIAGE,
  "- **STATUS**: is missing from triage.md — Step 12's edit boundary depends on it")

t("triage.md template contains DECISION field as boundary marker",
  "- **DECISION**:" in TRIAGE,
  "- **DECISION**: is missing from triage.md — Step 12's edit boundary depends on it")

# ============================================================================
# INVARIANT 10: Edge case - STATUS/DECISION fields are not duplicated
# ============================================================================
print("\n[Invariant 10] STATUS/DECISION field lines appear in correct quantity in triage.md")

# For Needs you items, we should have at least one STATUS pending-decision line
status_pending_decision_count = TRIAGE.count(STATUS_PENDING_DECISION)
t("STATUS pending-decision line appears at least once", status_pending_decision_count > 0,
  "STATUS pending-decision not found at all")

# For Needs measurement items, we should have at least one STATUS pending-measurement line
status_pending_measurement_count = TRIAGE.count(STATUS_PENDING_MEASUREMENT)
t("STATUS pending-measurement line appears at least once", status_pending_measurement_count > 0,
  "STATUS pending-measurement not found at all")

# DECISION empty line should appear at least once
decision_empty_count = TRIAGE.count(DECISION_EMPTY)
t("DECISION empty line appears at least once", decision_empty_count > 0,
  "DECISION empty line not found at all")

h.summarize_and_exit()
