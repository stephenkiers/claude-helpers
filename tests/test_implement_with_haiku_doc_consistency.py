#!/usr/bin/env python3
"""
Test suite for implement-with-haiku document consistency (Directives 1-9 of the plan).

This plan fixes three main areas:
- Directive 1: REPORT_FILE trailer handling in Step 4c (4 required fields, not 5)
- Directives 2-7: Canonical wording for Project-conventions, Working directory, Full report trailer
- Directives 3-4, 8-9: agents/plan-implementer.md Constraints and REPORT_FILE states
- Directive 9: Citation fix for ADR-0001

Run with: python3 tests/test_implement_with_haiku_doc_consistency.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

COMMANDS = REPO_ROOT / "commands"
AGENTS = REPO_ROOT / "agents"
ADRS = REPO_ROOT / "docs" / "adr"


def read(path):
    """Return a file's text, or '' if missing."""
    try:
        return path.read_text()
    except OSError:
        return ""


IMPLEMENT_WITH_HAIKU = read(COMMANDS / "implement-with-haiku.md")
PLAN_IMPLEMENTER = read(AGENTS / "plan-implementer.md")
ADR_0008 = read(ADRS / "0008-machine-enforced-agent-guardrails.md")

h = Harness("IMPLEMENT-WITH-HAIKU DOCUMENT CONSISTENCY TEST SUITE")
t = h.test_result

# ============================================================================
# DIRECTIVE 1: Step 4c requires 4 fields, not 5 (REPORT_FILE deferred elsewhere)
# ============================================================================
print("[Directive 1] REPORT_FILE handling in Step 4c")

t("implement-with-haiku.md exists", IMPLEMENT_WITH_HAIKU != "",
  "File not found or empty")

# Extract Step 4c (## Step 4c: ...)
step_4c_match = re.search(
    r"## Step 4c:.*?\n(.*?)(?=\n## |\n### Salvage|\Z)",
    IMPLEMENT_WITH_HAIKU, re.S
)
t("Step 4c section exists",
  step_4c_match is not None,
  "Could not find ## Step 4c: section")

step_4c = step_4c_match.group(1) if step_4c_match else ""

# Should NOT say "all five trailer lines" — that's the old, contradictory language
has_no_five_trailer = "all five trailer lines" not in step_4c.lower()
t("Step 4c does not claim 'all five trailer lines' (contradiction is fixed)",
  has_no_five_trailer,
  "Step 4c should not mention 'all five trailer lines'")

# Should explicitly say "four" or "All four handoff-critical trailer lines"
has_four_trailer = "four" in step_4c.lower() and "trailer" in step_4c.lower()
t("Step 4c mentions 'four' trailer lines",
  has_four_trailer,
  "Step 4c should mention four trailer lines")

# Should explicitly mention the 4 required fields
required_fields = ["ELAPSED_SECONDS", "VERIFIED", "FILES_TOUCHED", "STAGED"]
for field in required_fields:
    t(f"Step 4c mentions required field: {field}",
      field in step_4c,
      f"Required field {field} not mentioned in Step 4c")

# REPORT_FILE should be explicitly deferred
t("Step 4c defers REPORT_FILE handling separately",
  "REPORT_FILE" in step_4c and ("handled separately" in step_4c.lower() or "see" in step_4c.lower()),
  "Step 4c should explicitly defer REPORT_FILE to other sections")

# There should be a Report-file check section that handles it separately
report_file_check = "Report-file check" in IMPLEMENT_WITH_HAIKU
t("REPORT_FILE has a separate 'Report-file check' section",
  report_file_check,
  "Should have a separate section handling REPORT_FILE")

print()

# ============================================================================
# DIRECTIVE 2 & 7: Project-conventions block in Duplication sweep & Doc-drift
# ============================================================================
print("[Directives 2 & 7] Project-conventions block in sweep/drift sections")

# Find "**Duplication sweep:**" section (markdown bold)
duplication_section = re.search(
    r"\*\*Duplication sweep:\*\*(.*?)(?=\n\*\*|\n## |\Z)",
    IMPLEMENT_WITH_HAIKU, re.I | re.S
)
t("Duplication sweep section exists",
  duplication_section is not None,
  "Could not find **Duplication sweep:** section")

if duplication_section:
    duplication_text = duplication_section.group(1)
    t("Duplication sweep includes Project-conventions block",
      "Project conventions:" in duplication_text,
      "Project-conventions block missing in Duplication sweep")

# Find "**Doc-drift check:**" section
docrift_section = re.search(
    r"\*\*Doc-drift check:\*\*(.*?)(?=\n\*\*|\n## |\Z)",
    IMPLEMENT_WITH_HAIKU, re.I | re.S
)
t("Doc-drift check section exists",
  docrift_section is not None,
  "Could not find **Doc-drift check:** section")

if docrift_section:
    docrift_text = docrift_section.group(1)
    t("Doc-drift check includes Project-conventions block",
      "Project conventions:" in docrift_text,
      "Project-conventions block missing in Doc-drift check")

print()

# ============================================================================
# DIRECTIVE 5: Working directory: phrasing consistency
# ============================================================================
print("[Directive 5] Working directory: phrasing consistency")

# Find all "Working directory:" lines (in backticks)
# Pattern: > `Working directory: ...` (with quote prefix for blockquote)
working_dir_pattern = r"^>\s*`Working directory:\s*<([^>]+)>`\s*\(([^)]*)\)"
working_dir_matches = list(re.finditer(working_dir_pattern, IMPLEMENT_WITH_HAIKU, re.MULTILINE))

t("Found at least one Working directory: line",
  len(working_dir_matches) > 0,
  "No 'Working directory:' lines found in blockquote format")

# Check that all parenthetical explanations are identical
if working_dir_matches:
    explanations = [match.group(2).strip() for match in working_dir_matches]
    non_empty = [e for e in explanations if e]

    if non_empty:
        first = non_empty[0]
        all_match = all(e == first for e in non_empty)
        t("All Working directory: parenthetical explanations are identical",
          all_match,
          f"Found {len(set(explanations))} distinct explanations")

print()

# ============================================================================
# DIRECTIVE 6 & 7: Full report trailer reference consistency
# ============================================================================
print("[Directives 6 & 7] Full report trailer reference consistency")

# Count subsection headings that have "read-only" in them (actual read-only pass sections)
# Pattern: ### ... read-only ...
read_only_headings = list(re.finditer(
    r"### .+?read-only.+?\n(.*?)(?=\n### |\n## |\Z)",
    IMPLEMENT_WITH_HAIKU, re.I | re.S
))

t("Found read-only pass sections (by heading)",
  len(read_only_headings) > 0,
  "No read-only pass sections found by heading")

# For each read-only section, check consistency
for i, section in enumerate(read_only_headings):
    section_text = section.group(1)
    has_full_report_ref = "Full report trailer" in section_text

    t(f"Read-only section {i+1} has report trailer reference",
      has_full_report_ref,
      f"Read-only section {i+1} should reference full report trailer")

print()

# ============================================================================
# DIRECTIVE 3: agents/plan-implementer.md REPORT_FILE trailer states
# ============================================================================
print("[Directive 3] plan-implementer.md REPORT_FILE trailer documentation")

t("plan-implementer.md exists", PLAN_IMPLEMENTER != "",
  "File not found or empty")

# Should document three states:
# 1. Success form (REPORT_FILE: <value>)
# 2. "none" form (no report was generated)
# 3. "failed —" form (report generation failed)

has_success_form = re.search(r"REPORT_FILE:\s*\w+|REPORT_FILE:\s*<", PLAN_IMPLEMENTER)
has_none_form = re.search(r"REPORT_FILE:\s*none", PLAN_IMPLEMENTER, re.I)
has_failed_form = re.search(r"REPORT_FILE:\s*failed\s*[—-]", PLAN_IMPLEMENTER, re.I)

t("plan-implementer.md documents success form (REPORT_FILE: <value>)",
  has_success_form is not None,
  "Missing documentation of normal REPORT_FILE: <value> form")

t("plan-implementer.md documents 'none' form (REPORT_FILE: none)",
  has_none_form is not None,
  "Missing documentation of REPORT_FILE: none form")

t("plan-implementer.md documents 'failed' form (REPORT_FILE: failed —)",
  has_failed_form is not None,
  "Missing documentation of REPORT_FILE: failed — <reason> form")

print()

# ============================================================================
# DIRECTIVE 4: Round 3 pass-1 "no report (unit failed)" producer instruction
# ============================================================================
print("[Directive 4] Round 3 pass-1 'no report (unit failed)' producer instruction")

# Look for the explicit instruction in Step 4c
t("Step 4c instructs to record 'no report (unit failed)' label",
  "no report (unit failed)" in step_4c,
  "Step 4c should explicitly instruct recording of 'no report (unit failed)' label")

# Also check the Round 3 pass 1 section
round3_section = re.search(
    r"### Round 3 pass 1:.*?\n(.*?)(?=\n### |\n## |\Z)",
    IMPLEMENT_WITH_HAIKU, re.I | re.S
)
t("Round 3 pass 1 section exists",
  round3_section is not None,
  "Could not find ### Round 3 pass 1: section")

if round3_section:
    round3_text = round3_section.group(1)
    # Should reference the input list that includes these labels
    t("Round 3 pass 1 references the unit-failed input list",
      "no report" in round3_text or "unit failed" in round3_text,
      "Round 3 pass 1 should reference the no-report handling from Step 4c")

print()

# ============================================================================
# DIRECTIVE 8: agents/plan-implementer.md Constraints out-of-cwd exceptions
# ============================================================================
print("[Directive 8] plan-implementer.md Constraints out-of-cwd exceptions")

# Find Constraints section
constraints_section = re.search(
    r"## Constraints(.*?)(?=\n## |\Z)",
    PLAN_IMPLEMENTER, re.S
)
t("plan-implementer.md has Constraints section",
  constraints_section is not None,
  "Constraints section not found")

if constraints_section:
    constraints_text = constraints_section.group(1)

    # Should mention "only" out-of-cwd exceptions (limiting language)
    has_only_clause = re.search(
        r"only\s+(?:out-of-cwd|out-of-directory|exception)",
        constraints_text, re.I
    )
    t("Constraints section contains limiting language for out-of-cwd exceptions",
      has_only_clause is not None,
      "Should explicitly state what out-of-cwd exceptions are permitted")

    # Should name the two specific exceptions: report-file write and prior-report read
    t("Constraints mentions report-file write as exception",
      re.search(r"report.*file.*write|write.*report.*file", constraints_text, re.I) is not None,
      "Should name report-file write as a permitted out-of-cwd exception")

    t("Constraints mentions prior-report read as exception",
      re.search(r"prior.*report.*read|read.*prior.*report", constraints_text, re.I) is not None,
      "Should name prior-report read as a permitted out-of-cwd exception")

print()

# ============================================================================
# DIRECTIVE 8 (continued): ADR-0008 documents both exceptions
# ============================================================================
print("[Directive 8 continued] ADR-0008 documents out-of-cwd exceptions")

t("ADR-0008 exists", ADR_0008 != "",
  "docs/adr/0008-machine-enforced-agent-guardrails.md not found or empty")

if ADR_0008:
    t("ADR-0008 documents report-file write exception",
      re.search(r"report.*file.*write|write.*report.*file|exception.*report", ADR_0008, re.I) is not None,
      "ADR-0008 should document the report-file write exception")

    t("ADR-0008 documents prior-report read exception",
      re.search(r"prior.*report.*read|read.*prior.*report|exception.*read", ADR_0008, re.I) is not None,
      "ADR-0008 should document the prior-report read exception")

print()

# ============================================================================
# DIRECTIVE 9: ADR-0001 citation fix (not expert-review.md)
# ============================================================================
print("[Directive 9] ADR-0001 citation fix")

# Look for context around CLAUDE.md / project.yaml context-discipline note
context_note = re.search(
    r"Do not read `CLAUDE\.md`.*?\n(.*?)(?=\n\n|\n\*\*[A-Z]|\Z)",
    IMPLEMENT_WITH_HAIKU, re.S
)
t("Context-discipline note about CLAUDE.md exists",
  context_note is not None,
  "Could not find note about not reading CLAUDE.md/project.yaml")

if context_note:
    note_text = context_note.group(1)

    # Should reference ADR-0001
    has_adr_0001 = "ADR-0001" in note_text or "ADR 0001" in note_text
    has_progressive_disclosure = "progressive" in note_text.lower() and "disclosure" in note_text.lower()

    t("Citation references ADR-0001 or progressive-disclosure principle",
      has_adr_0001 or has_progressive_disclosure,
      "Should cite ADR-0001 for progressive-disclosure principle")

    # Should NOT incorrectly claim the rule was applied to reviewer personas in expert-review.md
    t("Citation does not incorrectly attribute rule to expert-review.md reviewers",
      not re.search(r"expert-review\.md.*reviewer|reviewer.*expert-review\.md", note_text, re.I),
      "Should not incorrectly cite expert-review.md as applying the same rule to reviewer personas")

    # If it mentions expert-review.md, it should accurately describe the complementary pattern
    if "expert-review" in note_text.lower() and "expert-review.md" in note_text:
        t("expert-review.md description mentions complementary pattern or central distribution",
          re.search(r"complementary|central|distribute", note_text, re.I) is not None,
          "If mentioning expert-review.md, should describe its different/complementary approach")

print()

h.summarize_and_exit()
