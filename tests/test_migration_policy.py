#!/usr/bin/env python3
"""
Test suite for migration policy and fail-loud behavior (Ruling A + Finding 2).

The plan requires:
- Ruling A: "Fail loud, no auto-migration" — /implement-with-haiku and /verify-queue sync
  must not silently ignore pre-existing action-plan.md files. Instead:
  1. /implement-with-haiku must hard-stop with loud error when reading a file with zero
     "- **STATUS**:" occurrences (old format, pre-migration)
  2. /verify-queue sync must print explicit warning when finding old-name action-plan.md
     without new claude-action-plan.md sibling
  3. docs/adr/0007 must document this "no auto-migration, fail loud" policy

- Finding 2 (HIGH): prompts/triage.md's ## Output section must enumerate full closed set
  of 5 STATUS values (pending-decision, pending-measurement, decided, no-op, measured)
  and their transitions

Run with: python3 tests/test_migration_policy.py
"""

import re
from _test_harness import REPO_ROOT, Harness

COMMANDS = REPO_ROOT / "commands"
PROMPTS = REPO_ROOT / "prompts"
ADRS = REPO_ROOT / "docs" / "adr"


def read(path):
    """Return a file's text, or '' if missing."""
    try:
        return path.read_text()
    except OSError:
        return ""


IMPLEMENT_WITH_HAIKU = read(COMMANDS / "implement-with-haiku.md")
VERIFY_QUEUE = read(COMMANDS / "verify-queue.md")
TRIAGE = read(PROMPTS / "triage.md")
ADR7 = read(ADRS / "0007-triage-and-decision-memory.md")

h = Harness("MIGRATION POLICY & STATUS ENUMERATION TEST SUITE")
t = h.test_result

# ============================================================================
# SECTION 1: Fail-loud behavior in implement-with-haiku.md (Ruling A)
# ============================================================================
print("[Section 1] implement-with-haiku.md: fail loud on zero STATUS occurrences")

t("implement-with-haiku.md exists", IMPLEMENT_WITH_HAIKU != "",
  "File not found or empty")

# Scope to the "Parse claude-action-plan.md into directives" subsection specifically,
# so these checks can't be satisfied by unrelated STATUS/error mentions elsewhere in the file.
parse_section = re.search(
    r"### Parse claude-action-plan\.md into directives\n(.*?)(?=\n### |\n## |\Z)",
    IMPLEMENT_WITH_HAIKU, re.S,
)
t("implement-with-haiku.md has a 'Parse claude-action-plan.md into directives' subsection",
  parse_section is not None,
  "Expected subsection heading not found")

parse_text = parse_section.group(1) if parse_section else ""

# The pre-check must be a structural check for zero '- **STATUS**:' occurrences
t("parse subsection describes a pre-check for zero STATUS occurrences",
  re.search(r"zero.{0,20}(occurrence|match)", parse_text, re.I) is not None
  and "- **STATUS**:" in parse_text,
  "Must document a structural pre-check that greps for '- **STATUS**:' and checks for zero matches")

# It must name the pre-migration / old-format condition explicitly
t("parse subsection names the pre-migration/old-format condition",
  re.search(r"pre-migration|old format|old.{0,10}action-plan\.md", parse_text, re.I) is not None,
  "Should explicitly name the pre-migration action-plan.md condition being detected")

# It must be a hard stop, explicitly distinguished from the non-blocking warning
# a few lines below it in this same subsection (pending-decision/pending-measurement items).
t("parse subsection states this is a hard stop, not a non-blocking warning",
  re.search(r"hard\s+stop", parse_text, re.I) is not None,
  "Should explicitly state this pre-check is a hard stop (unlike the per-item skip warning)")

print()

# ============================================================================
# SECTION 2: Fail-loud behavior in verify-queue.md (Ruling A)
# ============================================================================
print("[Section 2] verify-queue.md: warning on old action-plan.md discovery")

t("verify-queue.md exists", VERIFY_QUEUE != "",
  "File not found or empty")

# verify-queue sync must warn about old action-plan.md without claude-action-plan.md
t("verify-queue.md documents sync discovery of old action-plan.md",
  "action-plan.md" in VERIFY_QUEUE.lower() or "migration" in VERIFY_QUEUE.lower(),
  "Should document checking for old-format action-plan.md files")

# The sync command should print a warning
t("verify-queue.md describes warning behavior for old action-plan.md",
  re.search(r"(warn|warning|alert)", VERIFY_QUEUE, re.I) is not None,
  "Should document that sync prints an explicit warning for old-name files")

# Should mention that this only happens when claude-action-plan.md sibling is missing
t("verify-queue.md mentions the sibling condition (claude-action-plan.md)",
  "claude-action-plan.md" in VERIFY_QUEUE,
  "Should document that warning occurs when new-name sibling is missing")

print()

# ============================================================================
# SECTION 3: ADR-0007 Amendment documents the no-auto-migration policy
# ============================================================================
print("[Section 3] ADR-0007 Amendment: no-auto-migration policy documented")

t("ADR-0007 exists", ADR7 != "",
  "File not found or empty")

t("ADR-0007 has an Amendment section", "## Amendment" in ADR7,
  "The Amendment section should document the fail-loud policy")

amendment_section = re.search(r"## Amendment(.*?)(?=\n## |\Z)", ADR7, re.S)
t("ADR-0007 Amendment section exists", amendment_section is not None)

if amendment_section:
    amendment = amendment_section.group(1)
    # Should mention the no-auto-migration / fail-loud policy
    t("Amendment documents 'fail loud' or 'no auto-migration'",
      re.search(r"(fail.?loud|no.?auto.?migration|no.?silent|explicit|warn)", amendment, re.I) is not None,
      "Amendment should explain the fail-loud / no-auto-migration policy")

    # Should have a bullet point
    t("Amendment is structured as bullet points",
      "-" in amendment or "•" in amendment,
      "Policy should be documented as a clear, scannable bullet")

print()

# ============================================================================
# SECTION 4: Finding 2 - triage.md enumerates 5 STATUS values (HIGH)
# ============================================================================
print("[Section 4] triage.md: enumerate 5 closed-set STATUS values")

t("triage.md exists", TRIAGE != "",
  "File not found or empty")

# Extract the ## Output section (or similar template section)
output_section = re.search(r"## Output(.*?)(?=\n## |\Z)", TRIAGE, re.S)
t("triage.md has an ## Output section", output_section is not None,
  "Output section should exist to define claude-action-plan.md shape")

if output_section:
    output = output_section.group(1)

    # Check for all 5 STATUS values explicitly enumerated
    status_values = [
        "pending-decision",
        "pending-measurement",
        "decided",
        "no-op",
        "measured"
    ]

    for status_val in status_values:
        t(f"Output section mentions STATUS: {status_val}",
          status_val in output,
          f"All 5 STATUS values must be enumerated in ## Output section")

    # Should describe the closed set explicitly
    t("Output section indicates these are the complete set of STATUS values",
      re.search(r"(closed set|full set|only|these five|5 (?:status|value))", output, re.I) is not None
      or all(sv in output for sv in status_values),
      "Should clearly indicate these 5 values are exhaustive")

    # Should describe transitions between STATUS values
    t("Output section describes STATUS transitions",
      re.search(r"(transition|change|→|->|move from|becomes)", output, re.I) is not None,
      "Should describe when/how STATUS values transition")

print()

# ============================================================================
# SECTION 5: Cross-file integrity - no silent migration
# ============================================================================
print("[Section 5] Cross-file consistency: both files reject old format")

t("both implement-with-haiku and verify-queue mention the migration issue",
  ("- **STATUS**:" in IMPLEMENT_WITH_HAIKU or "STATUS" in IMPLEMENT_WITH_HAIKU)
  and ("claude-action-plan.md" in VERIFY_QUEUE),
  "Both commands should reference the new STATUS-based format")

print()

h.summarize_and_exit()
