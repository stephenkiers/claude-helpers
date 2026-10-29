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

import importlib.util
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import telemetry_schema

COMMANDS = REPO_ROOT / "commands"
AGENTS = REPO_ROOT / "agents"
ADRS = REPO_ROOT / "docs" / "adr"


def read(path):
    """Return a file's text, or '' if missing."""
    try:
        return path.read_text()
    except OSError:
        return ""


def extract_bash_blocks(text):
    """
    Extract all fenced bash code blocks from markdown text.
    Returns a list of (block_content, line_number) tuples.
    """
    blocks = []
    lines = text.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i]
        # Match opening fence: ```bash
        if re.match(r'^```bash\s*$', line):
            # Calculate approximate line number (1-indexed)
            start_line = i + 1
            # Collect content until closing fence
            block_lines = []
            i += 1
            while i < len(lines):
                if re.match(r'^```\s*$', lines[i]):
                    # Found closing fence
                    blocks.append(('\n'.join(block_lines), start_line))
                    break
                block_lines.append(lines[i])
                i += 1
        i += 1
    return blocks


def replace_angle_bracket_placeholders(text):
    """
    Replace angle-bracket placeholders like <owned-files>, <file>, <path>, etc.
    with quoted stand-ins so bash -n can parse the block without treating < as redirection.

    Pattern: <[a-zA-Z][a-zA-Z0-9_-]*> matches placeholders like:
    - <owned-files>
    - <file>
    - <path>
    - <symbol_name>
    - <placeholder-name>

    Replaces each with "PLACEHOLDER" to make the block syntactically parseable.
    """
    return re.sub(r'<[a-zA-Z][a-zA-Z0-9_-]*>', '"PLACEHOLDER"', text)


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
  "REPORT_FILE" in step_4c and "handled separately" in step_4c.lower(),
  "Step 4c should explicitly say REPORT_FILE is 'handled separately' from other sections")

# There should be a Report-file check section (bolded heading) that handles it separately
report_file_check = re.search(r"\*\*Report-file check", IMPLEMENT_WITH_HAIKU) is not None
t("REPORT_FILE has a separate 'Report-file check' section (bolded heading)",
  report_file_check,
  "Should have a bolded '**Report-file check' heading handling REPORT_FILE")

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
      "**Project conventions:**" in duplication_text and "CLAUDE.md" in duplication_text,
      "Project-conventions block (bolded label + CLAUDE.md reference) missing in Duplication sweep")

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
      "**Project conventions:**" in docrift_text and "CLAUDE.md" in docrift_text,
      "Project-conventions block (bolded label + CLAUDE.md reference) missing in Doc-drift check")

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

# Find all "### "/"## " heading positions first (single-line match only, no DOTALL) so a
# heading whose title doesn't mention "read-only" can never be skipped over into later text
# that happens to contain the phrase — that bug previously made this check match spurious
# unrelated sections (verified via mutation: it silently matched "### Parse claude-action-plan.md
# into directives" and "### Apply Round 2's diff" instead of the two real read-only headings).
heading_matches = list(re.finditer(r"^(#{2,3}) .*$", IMPLEMENT_WITH_HAIKU, re.MULTILINE))
read_only_headings = []
for idx, heading_match in enumerate(heading_matches):
    title_line = heading_match.group(0)
    if "read-only" not in title_line.lower():
        continue
    body_start = heading_match.end() + 1
    body_end = heading_matches[idx + 1].start() if idx + 1 < len(heading_matches) else len(IMPLEMENT_WITH_HAIKU)
    read_only_headings.append(IMPLEMENT_WITH_HAIKU[body_start:body_end])

t("Found read-only pass sections (by heading)",
  len(read_only_headings) > 0,
  "No read-only pass sections found by heading")

# For each read-only section, check consistency
for i, section_text in enumerate(read_only_headings):
    has_full_report_ref = (
        "Full report trailer per plan-implementer instructions" in section_text
        and "STAGED: no (read-only pass)" in section_text
    )

    t(f"Read-only section {i+1} has report trailer reference",
      has_full_report_ref,
      f"Read-only section {i+1} should reference 'Full report trailer per plan-implementer "
      f"instructions' with the 'STAGED: no (read-only pass)' clause")

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

# ============================================================================
# ITEM 8: New Bash block ordering in Step 4d (resume mechanism)
# ============================================================================
print("[Item 8] Step 4d: New Bash block ordering for resume mechanism")

# Extract Step 4d
step_4d_match = re.search(
    r"## Step 4d:.*?\n(.*?)(?=\n## |\n### Salvage|\Z)",
    IMPLEMENT_WITH_HAIKU, re.S
)
t("Step 4d section exists",
  step_4d_match is not None,
  "Could not find ## Step 4d: section")

if step_4d_match:
    step_4d = step_4d_match.group(1)

    # Verify ordering: stage-end < command-id < --outcome interrupted < RESUME-AFTER-CLEAR:
    stage_end_pos = IMPLEMENT_WITH_HAIKU.find("stage-end --stage round1-join")
    command_id_pos = IMPLEMENT_WITH_HAIKU.find("command-id --command implement-with-haiku")
    outcome_interrupted_pos = IMPLEMENT_WITH_HAIKU.find("--outcome interrupted")
    resume_after_clear_pos = IMPLEMENT_WITH_HAIKU.find("RESUME-AFTER-CLEAR:")

    has_all_markers = (
        stage_end_pos >= 0 and command_id_pos >= 0 and
        outcome_interrupted_pos >= 0 and resume_after_clear_pos >= 0
    )
    t("Step 4d has all required markers",
      has_all_markers,
      "Missing one or more of: stage-end, command-id, --outcome interrupted, RESUME-AFTER-CLEAR:")

    if has_all_markers:
        has_correct_order = (
            stage_end_pos < command_id_pos < outcome_interrupted_pos < resume_after_clear_pos
        )
        t("Step 4d ordering correct (stage-end < command-id < --outcome interrupted < RESUME-AFTER-CLEAR:)",
          has_correct_order,
          f"Ordering violation: stage-end@{stage_end_pos}, command-id@{command_id_pos}, "
          f"--outcome interrupted@{outcome_interrupted_pos}, RESUME-AFTER-CLEAR:@{resume_after_clear_pos}")

    # Check that RESUME-AFTER-CLEAR: is followed by --resume-after-round1 and --resumed-from
    if resume_after_clear_pos >= 0:
        # Search within a reasonable window after RESUME-AFTER-CLEAR:
        search_window_end = min(resume_after_clear_pos + 500, len(IMPLEMENT_WITH_HAIKU))
        window_text = IMPLEMENT_WITH_HAIKU[resume_after_clear_pos:search_window_end]

        has_resume_flag = "--resume-after-round1" in window_text
        has_resumed_from_flag = "--resumed-from" in window_text

        t("RESUME-AFTER-CLEAR: followed by --resume-after-round1 flag",
          has_resume_flag,
          "Expected --resume-after-round1 within 500 chars of RESUME-AFTER-CLEAR:")

        t("RESUME-AFTER-CLEAR: followed by --resumed-from flag",
          has_resumed_from_flag,
          "Expected --resumed-from within 500 chars of RESUME-AFTER-CLEAR:")

print()

# ============================================================================
# ITEM 9: Deleted duplicate decline-path close
# ============================================================================
print("[Item 9] Deleted duplicate decline-path close")

# Count occurrences of "--outcome interrupted" within Step 4d only — the doc has a second,
# unrelated occurrence in "Incomplete report handling" (a generic abort path for every round)
# that predates this plan and is out of scope here.
if step_4d_match:
    outcome_count = step_4d.count("--outcome interrupted")
    t("Only one 'command-end ... --outcome interrupted' in Step 4d",
      outcome_count == 1,
      f"Expected exactly 1 occurrence of '--outcome interrupted' in Step 4d, found {outcome_count}")
else:
    t("Only one 'command-end ... --outcome interrupted' in Step 4d",
      False,
      "Could not find ## Step 4d: section to check")

print()

# ============================================================================
# ITEM 10: Literal-not-variable id-paste instruction
# ============================================================================
print("[Item 10] Literal-not-variable id-paste instruction")

# Find nearby text around RESUME-AFTER-CLEAR: and check for "literal" phrasing
if resume_after_clear_pos >= 0:
    search_start = max(0, resume_after_clear_pos - 400)
    search_end = min(resume_after_clear_pos + 800, len(IMPLEMENT_WITH_HAIKU))
    nearby_text = IMPLEMENT_WITH_HAIKU[search_start:search_end]

    has_literal_mention = "literal" in nearby_text.lower()
    has_resumed_from_mention = "--resumed-from" in nearby_text or "resumed-from" in nearby_text

    t("Resume/continue prose mentions 'literal'",
      has_literal_mention,
      "Expected 'literal' mention in text around RESUME-AFTER-CLEAR:")

    t("'literal' appears near '--resumed-from' in continue instructions",
      has_literal_mention and has_resumed_from_mention,
      "Expected 'literal' and '--resumed-from' to appear near each other in continue path")

print()

# ============================================================================
# ITEM 11: Resume section ordering relative to orphan sweep
# ============================================================================
print("[Item 11] Resume section ordering relative to orphan sweep")

# Assert "## Step 0: Resume check" exists and comes before "## Step 2.5"
resume_check_pos = IMPLEMENT_WITH_HAIKU.find("## Step 0: Resume check")
step_2_5_pos = IMPLEMENT_WITH_HAIKU.find("## Step 2.5")

t("Step 0: Resume check section exists",
  resume_check_pos >= 0,
  "Could not find '## Step 0: Resume check' section")

t("Step 0: Resume check comes before Step 2.5",
  resume_check_pos >= 0 and step_2_5_pos >= 0 and resume_check_pos < step_2_5_pos,
  f"Step 0 should come before Step 2.5 (positions: resume@{resume_check_pos}, 2.5@{step_2_5_pos})")

# Extract Step 2.5 section and verify RESUME_MODE=yes is mentioned in it
if step_2_5_pos >= 0:
    # Find the next heading after Step 2.5
    next_heading_pos = IMPLEMENT_WITH_HAIKU.find("\n## ", step_2_5_pos + 1)
    if next_heading_pos < 0:
        next_heading_pos = len(IMPLEMENT_WITH_HAIKU)

    step_2_5_section = IMPLEMENT_WITH_HAIKU[step_2_5_pos:next_heading_pos]

    has_resume_mode = "RESUME_MODE=yes" in step_2_5_section or "RESUME_MODE = yes" in step_2_5_section
    t("Step 2.5 documents RESUME_MODE=yes skip in resume mode",
      has_resume_mode,
      "Step 2.5 (orphan sweep) should mention RESUME_MODE=yes to show section is skipped in resume mode")

print()

# ============================================================================
# ITEM 12: Gate-always-re-runs statement
# ============================================================================
print("[Item 12] Integration Gate section: never trusted / always re-run")

# Find Integration Gate section
integration_gate_match = re.search(
    r"###? +Integration Gate.*?\n(.*?)(?=\n###? |\n## |\Z)",
    IMPLEMENT_WITH_HAIKU, re.I | re.S
)
t("Integration Gate section exists",
  integration_gate_match is not None,
  "Could not find Integration Gate section")

if integration_gate_match:
    gate_text = integration_gate_match.group(1)

    # Check for candidate phrases indicating gate is never trusted / always re-run
    has_gate_phrase = (
        "never trusted" in gate_text.lower() or
        "always re-run" in gate_text.lower() or
        "always re-runs" in gate_text.lower() or
        "never trust" in gate_text.lower()
    )
    t("Integration Gate mentions it is never trusted or always re-run",
      has_gate_phrase,
      "Gate section should mention phrases like 'never trusted', 'always re-run', or 'never trust'")

print()

# ============================================================================
# ITEM 13: ADR-0019 amendments and kill-criterion keywords
# ============================================================================
print("[Item 13] ADR-0019: amendments, deleted, 8, 30 days keywords")

ADR_0019 = read(ADRS / "0019-content-driven-pause-checkpoints.md")
t("ADR-0019 exists", ADR_0019 != "",
  "docs/adr/0019-content-driven-pause-checkpoints.md not found or empty")

if ADR_0019:
    # Check for the 2026-09-15 amendment heading
    has_amendment_date = "Amendment (2026-09-15)" in ADR_0019 or "amendment (2026-09-15)" in ADR_0019.lower()
    t("ADR-0019 has Amendment (2026-09-15) heading",
      has_amendment_date,
      "Could not find 'Amendment (2026-09-15)' in ADR-0019")

    # Find the amendment section and verify required keywords appear within it
    if has_amendment_date:
        amendment_pos = ADR_0019.find("Amendment (2026-09-15)")
        if amendment_pos < 0:
            amendment_pos = ADR_0019.lower().find("amendment (2026-09-15)")

        if amendment_pos >= 0:
            # This amendment is the last one in the file, so its section runs to EOF —
            # the pre-registered kill-criterion prose (required keywords live here) is long
            # enough that a fixed-size window undercounts it.
            amendment_section = ADR_0019[amendment_pos:]

            has_deleted = "deleted" in amendment_section.lower()
            has_eight = "8 real" in amendment_section
            has_thirty_days = "30 days" in amendment_section or "30-day" in amendment_section.lower()

            t("Amendment section contains 'deleted'",
              has_deleted,
              "Amendment (2026-09-15) should mention 'deleted'")

            t("Amendment section contains '8'",
              has_eight,
              "Amendment (2026-09-15) should contain the number '8'")

            t("Amendment section contains '30 days' or '30-day'",
              has_thirty_days,
              "Amendment (2026-09-15) should mention '30 days' or '30-day'")

print()

# ============================================================================
# NEW TEST: Bash syntax validation for all code blocks
# ============================================================================
print("[New] Bash syntax check: all fenced bash blocks in implement-with-haiku.md")

bash_blocks = extract_bash_blocks(IMPLEMENT_WITH_HAIKU)
t("Found bash code blocks in implement-with-haiku.md",
  len(bash_blocks) > 0,
  "No ```bash blocks found in the document")

if bash_blocks:
    syntax_errors = []
    for block_content, line_number in bash_blocks:
        # Replace angle-bracket placeholders (e.g., <owned-files>, <file>) with quoted stand-ins
        # so bash -n can parse the block without treating < as redirection
        processed_content = replace_angle_bracket_placeholders(block_content)

        # Write block to a temporary file and check syntax with bash -n
        with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False) as tmpfile:
            tmpfile.write(processed_content)
            tmpfile.flush()
            tmppath = tmpfile.name

        try:
            result = subprocess.run(
                ["bash", "-n", tmppath],
                capture_output=True,
                timeout=5
            )
            if result.returncode != 0:
                stderr_output = result.stderr.decode('utf-8', errors='replace').strip()
                syntax_errors.append(
                    f"Line ~{line_number}: {stderr_output}"
                )
        except subprocess.TimeoutExpired:
            syntax_errors.append(f"Line ~{line_number}: bash -n timed out")
        except Exception as e:
            syntax_errors.append(f"Line ~{line_number}: {str(e)}")
        finally:
            # Clean up temp file
            try:
                Path(tmppath).unlink()
            except Exception:
                pass

    if syntax_errors:
        error_message = "Bash syntax errors found:\n  " + "\n  ".join(syntax_errors)
        t(f"All {len(bash_blocks)} bash blocks pass syntax check (bash -n)",
          False,
          error_message)
    else:
        t(f"All {len(bash_blocks)} bash blocks pass syntax check (bash -n)",
          True)

print()

# ============================================================================
# REGRESSION TEST: PLAN_REF == "none" suppression check in Step 4d
# ============================================================================
print("[Regression] PLAN_REF == \"none\" check in SUPPRESS_RESUME logic")

# Extract Step 4d's SUPPRESS_RESUME bash block
if step_4d_match:
    step_4d_section = step_4d_match.group(1)

    # Look for the PLAN_REF == "none" check pattern
    has_plan_ref_none_check = (
        'PLAN_REF' in step_4d_section and
        '"none"' in step_4d_section and
        'SUPPRESS_RESUME' in step_4d_section and
        re.search(r'if.*PLAN_REF.*==.*["\']none["\'].*SUPPRESS_RESUME', step_4d_section, re.S) is not None
    )

    t("SUPPRESS_RESUME logic includes PLAN_REF == \"none\" check",
      has_plan_ref_none_check,
      "Step 4d should check for PLAN_REF == \"none\" in the SUPPRESS_RESUME logic before the whitespace check")

    # Also verify the diagnostic message includes the reason
    has_none_in_diagnostic = "PLAN_REF is 'none'" in step_4d_section
    t("Diagnostic message includes PLAN_REF 'none' as a suppression reason",
      has_none_in_diagnostic,
      "Resume suppression message should list PLAN_REF being 'none' as a reason")
else:
    t("SUPPRESS_RESUME logic includes PLAN_REF == \"none\" check",
      False,
      "Could not find ## Step 4d: section")

    t("Diagnostic message includes PLAN_REF 'none' as a suppression reason",
      False,
      "Could not find ## Step 4d: section to verify diagnostic message")

print()

# ============================================================================
# PLAN STEP 6: run-ledger / fail-closed finish contract (issue #259)
# ============================================================================
print("[Step 6] Run-ledger and fail-closed finish doc pins")


def logical_lines(text):
    """Join backslash-continued shell lines into single logical lines."""
    out = []
    buf = ""
    for line in text.split("\n"):
        if line.rstrip().endswith("\\"):
            buf += line.rstrip()[:-1] + " "
            continue
        out.append(buf + line)
        buf = ""
    if buf:
        out.append(buf)
    return out


def is_placeholder(token):
    return token == "" or token[0] in "<$%.[" or token in ("...", "*")


_ledger_spec = importlib.util.spec_from_file_location(
    "implement_ledger_doc_pins", REPO_ROOT / "scripts" / "implement-ledger.py")
_ledger = importlib.util.module_from_spec(_ledger_spec)
_ledger_spec.loader.exec_module(_ledger)

# (a) every literal --failure-class token is a FAILURE_CLASSES member
classes_cited = []
for m in re.finditer(r"--failure-class\s+(\S+)", IMPLEMENT_WITH_HAIKU):
    tok = m.group(1).strip("\"'`;,)")
    if not is_placeholder(tok):
        classes_cited.append(tok)
bad_classes = sorted({c for c in classes_cited if c not in telemetry_schema.FAILURE_CLASSES})
t("(a) every literal --failure-class in implement-with-haiku.md is in FAILURE_CLASSES",
  bool(classes_cited) and not bad_classes,
  f"not in FAILURE_CLASSES: {bad_classes}" if bad_classes else "no literal --failure-class sites found")

# (b) no command-end line carries --outcome success
success_ends = [
    line for line in logical_lines(IMPLEMENT_WITH_HAIKU)
    if "command-end" in line and re.search(r"--outcome\s+success\b", line)
]
t("(b) no command-end line in implement-with-haiku.md carries --outcome success",
  not success_ends,
  f"offending lines: {success_ends[:2]}")

# (c) exactly one finish invocation, with no error suppression on that line
finish_lines = [
    line for line in logical_lines(IMPLEMENT_WITH_HAIKU)
    if re.search(r'implement-ledger\.py"?\s+finish\b', line)
]
t("(c) exactly one implement-ledger.py finish invocation",
  len(finish_lines) == 1,
  f"found {len(finish_lines)}")
if len(finish_lines) == 1:
    t("(c) the finish invocation has no '|| true' or '2>/dev/null'",
      "|| true" not in finish_lines[0] and "2>/dev/null" not in finish_lines[0],
      f"line: {finish_lines[0][:200]}")

# (d) no-early-exit and post-compaction (locate) rules appear before ## Step 0
step0_pos = IMPLEMENT_WITH_HAIKU.find("## Step 0")
early_match = re.search(r"early[- ]exit", IMPLEMENT_WITH_HAIKU, re.I)
locate_match = re.search(r'implement-ledger\.py"?\s+locate\b', IMPLEMENT_WITH_HAIKU)
t("(d) no-early-exit rule appears before ## Step 0",
  step0_pos != -1 and early_match is not None and early_match.start() < step0_pos,
  f"Step 0 at {step0_pos}, early-exit at {early_match.start() if early_match else None}")
t("(d) post-compaction implement-ledger.py locate rule appears before ## Step 0",
  step0_pos != -1 and locate_match is not None and locate_match.start() < step0_pos,
  f"Step 0 at {step0_pos}, locate at {locate_match.start() if locate_match else None}")

# (e) every step id and skip reason cited in a ledger append is in the helper's vocabulary
cited_steps = []
cited_reasons = []
for line in logical_lines(IMPLEMENT_WITH_HAIKU):
    if "printf" not in line or ">>" not in line or "LEDGER" not in line:
        continue
    fmt_and_args = line.split(">>")[0]
    done = re.search(r"\bDONE\s+(\S+)\s+%s", fmt_and_args)
    skipped = re.search(r"\bSKIPPED\s+(\S+)\s+%s", fmt_and_args)
    if done and not is_placeholder(done.group(1)):
        cited_steps.append(done.group(1))
    if skipped:
        if not is_placeholder(skipped.group(1)):
            cited_steps.append(skipped.group(1))
        quoted = re.findall(r'"([^"]*)"', fmt_and_args)
        if quoted and not is_placeholder(quoted[-1]):
            cited_reasons.append(quoted[-1])
bad_steps = sorted({s for s in cited_steps if s not in _ledger.STEP_IDS})
bad_reasons = sorted({r for r in cited_reasons if r not in _ledger.SKIP_REASONS})
t("(e) every step id cited in a ledger append is in implement-ledger.py STEP_IDS",
  len(cited_steps) > 0 and not bad_steps,
  f"unknown step ids: {bad_steps} (cited {len(cited_steps)})")
t("(e) every skip reason cited in a ledger append is in implement-ledger.py SKIP_REASONS",
  len(cited_reasons) > 0 and not bad_reasons,
  f"unknown reasons: {bad_reasons} (cited {len(cited_reasons)})")

# (f) frontmatter grants the helper, and the python3 grant is not a bare wildcard
frontmatter = re.match(r"---\n(.*?)\n---", IMPLEMENT_WITH_HAIKU, re.S)
allowed_tools = ""
if frontmatter:
    am = re.search(r"^allowed-tools:(.*)$", frontmatter.group(1), re.M)
    allowed_tools = am.group(1) if am else ""
t("(f) allowed-tools frontmatter grants implement-ledger.py",
  "implement-ledger.py" in allowed_tools,
  f"allowed-tools: {allowed_tools[:200]}")
t("(f) the python3 grant is narrow (no bare Bash(python3:*))",
  "Bash(python3:*)" not in allowed_tools and "Bash(python3 *)" not in allowed_tools,
  f"allowed-tools: {allowed_tools[:200]}")

# (#338) the Final check: resolve, run in the worktree, record outcome, and never trust a bare pass
DOC = IMPLEMENT_WITH_HAIKU
t("(#338) frontmatter allowed-tools grants scripts.workflow.cli checks",
  "scripts.workflow.cli checks" in allowed_tools,
  f"allowed-tools: {allowed_tools[:200]}")


def table_row(marker):
    """The first markdown table row containing marker, or '' when absent."""
    for line in DOC.splitlines():
        if line.startswith("|") and marker in line:
            return line
    return ""


for tok in ("checks resolve-check", "checks run -", "run_in_background", "cd <WORKTREE>", "rev-parse HEAD",
            "timeout_secs"):
    t(f"(#338) doc names {tok!r}", tok in DOC)

t("(#338) doc says a wrapped `timeout` is never used for a gate or check",
  "Never wrap" in DOC and "`timeout`" in DOC)

for tok in ("CHECK:", "CHECK_EXIT:", "CHECK_SOURCE:", "CHECK_SHA:", "opted-out",
            "check-not-run", "check-not-configured"):
    t(f"(#338) the check receipt vocabulary names {tok!r}", tok in DOC)

t("(#338) doc states \"not run\" is never a pass",
  '"not run" is never a pass' in DOC)

t("(#338) doc says the exit status is not branched on (use source/command from resolver JSON)",
  "do not branch on the exit status" in DOC and "`source`" in DOC and "`command`" in DOC)

t("(#338) no `resolve-check ... ||` shell fallback (the exit code is never the gate)",
  not any("resolve-check" in line and "||" in line for line in DOC.splitlines()))

# Heading order: 4.6 Verify green, then commit -> ## Final check -> ## Final summary
pos_46 = DOC.find("### 4.6 Verify green, then commit")
pos_final = DOC.find("## Final check")
pos_summary = DOC.find("## Final summary")
t("(#338) the Final check section sits after 4.6 Verify green, then commit",
  pos_46 != -1 and pos_final != -1 and pos_46 < pos_final,
  f"4.6={pos_46} final={pos_final}")
t("(#338) the Final check section sits before ## Final summary",
  pos_final != -1 and pos_summary != -1 and pos_final < pos_summary,
  f"final={pos_final} summary={pos_summary}")

t("(#338) the Round-4 skip sentence says neither the gate nor the Final check is skippable",
  "neither is the Final check" in DOC)

mech_lines = [line for line in DOC.splitlines() if "Mechanical" in line and "Final check" in line]
t("(#338) a mechanical run routes to the Final check",
  len(mech_lines) > 0, f"mechanical lines naming Final check: {len(mech_lines)}")

# One assertion per mapping-table row: the row holds both the outcome and the next action
row_exit0 = table_row("exit `0`")
t("(#338) mapping row exit 0 holds the pass outcome and the OK receipt next action",
  "pass" in row_exit0 and "OK receipt" in row_exit0, row_exit0[:160])

row_timeout = table_row("timeout prefix")
t("(#338) mapping row for a timed-out step holds 'not run' and a hard stop",
  "not run" in row_timeout and "hard stop" in row_timeout, row_timeout[:160])

row_127 = table_row("126`/`127")
t("(#338) mapping row for exit 126/127 holds 'not run' and a hard stop",
  "not run" in row_127 and "hard stop" in row_127, row_127[:160])

row_real = table_row("exit `1` otherwise")
t("(#338) mapping row for a real failure holds 'real failure' and the Final check fix loop",
  "real failure" in row_real and "Final check fix loop" in row_real, row_real[:160])

row_other = table_row("exit `2`")
t("(#338) mapping row for exit 2 and any other exit holds 'not run'",
  "not run" in row_other, row_other[:160])

row_optout = table_row('source: "opt-out"')
t("(#338) mapping row for resolver opt-out records CHECK: opted-out with no run",
  "opted-out" in row_optout and "no run" in row_optout, row_optout[:160])

row_noconf = table_row("no opt-out")
t("(#338) mapping row for resolver failure with no opt-out is a check-not-configured hard stop",
  "check-not-configured" in row_noconf and "hard stop" in row_noconf, row_noconf[:160])

print()

# ============================================================================
# STEP 5-8, 10, 12: Primary-checkout drift detection (new for #336)
# ============================================================================
print("[Step 5-8] Primary-checkout drift detection")

# Step 5: Rename MAIN_WT* -> PRIMARY_CHECKOUT/TICKET_WT_PATH
t("No MAIN_WT= or MAIN_WT_PATH references remain (except in this test)",
  "MAIN_WT=" not in IMPLEMENT_WITH_HAIKU and "<MAIN_WT_PATH>" not in IMPLEMENT_WITH_HAIKU,
  "Found MAIN_WT= or <MAIN_WT_PATH> in doc")

# Step 5: Block A should say "ticket worktree" not "main worktree"
block_a_mentions = IMPLEMENT_WITH_HAIKU.count("the ticket worktree") + \
                  IMPLEMENT_WITH_HAIKU.count("<TICKET_WT_PATH>")
has_block_a_updates = block_a_mentions > 0
t("Block A updated with ticket worktree references",
  has_block_a_updates,
  "Block A should reference 'the ticket worktree' and <TICKET_WT_PATH>")

# Step 6: Snapshot command should exist
has_snapshot = "primary-checkout-drift.py" in IMPLEMENT_WITH_HAIKU and "snapshot" in IMPLEMENT_WITH_HAIKU
t("Snapshot command added at Step 2",
  has_snapshot,
  "Should call primary-checkout-drift.py snapshot in Step 2")

# Step 6-7: Compare commands should exist (at least once for Round 1 barrier)
has_compare = "compare" in IMPLEMENT_WITH_HAIKU and \
              "primary-checkout-drift.py" in IMPLEMENT_WITH_HAIKU and \
              "--baseline" in IMPLEMENT_WITH_HAIKU
t("Compare command added at Round 1 barrier (Step 4d)",
  has_compare,
  "Should call primary-checkout-drift.py compare in Step 4d")

# Step 6: planned-paths.txt should be written in Step 3
has_planned_paths = "planned-paths.txt" in IMPLEMENT_WITH_HAIKU
t("planned-paths.txt written in Step 3",
  has_planned_paths,
  "Should write planned-paths.txt file after decomposition")

# Step 7: Check that compare appears before RESUME-AFTER-CLEAR in Step 4d
step_4d_match = re.search(r"## Step 4d:.*?(?=\n## |\Z)", IMPLEMENT_WITH_HAIKU, re.S)
if step_4d_match:
    step_4d_text = step_4d_match.group(0)
    compare_pos = step_4d_text.find("primary-checkout-drift.py")
    resume_pos = step_4d_text.find("RESUME-AFTER-CLEAR:")
    has_correct_order = compare_pos > 0 and resume_pos > 0 and compare_pos < resume_pos
    t("Round 1 compare appears before RESUME-AFTER-CLEAR in Step 4d",
      has_correct_order,
      f"Compare should come before resume line (compare@{compare_pos}, resume@{resume_pos})")
else:
    t("Round 1 compare appears before RESUME-AFTER-CLEAR in Step 4d",
      False,
      "Could not find ## Step 4d: section")

# Step 8: Check that drift block uses git -C
drift_check_match = re.search(
    r"\*\*Drift check.*?\*\*Failure procedure|### Failure procedure.*?(?=\n## |\n### |\Z)",
    IMPLEMENT_WITH_HAIKU, re.I | re.S
)
if drift_check_match:
    drift_text = drift_check_match.group(0)
    git_c_count = drift_text.count("git -C")
    has_explicit_c = git_c_count >= 2  # At least status and rev-parse
    t("Drift block uses explicit git -C paths",
      has_explicit_c,
      f"Drift block should use 'git -C <path>' for all git commands (found {git_c_count})")

# Step 8: Check that salvage procedure doesn't commit
has_no_commit_in_salvage = "merged (salvaged-from-main)" not in IMPLEMENT_WITH_HAIKU
t("No 'merged (salvaged-from-main)' label in doc",
  has_no_commit_in_salvage,
  "Salvage label should be removed")

# Step 8: Check for diff --binary HEAD usage (non-mutating patch creation)
has_diff_binary_head = "diff --binary HEAD" in IMPLEMENT_WITH_HAIKU
t("Drift block uses 'diff --binary HEAD' for patch creation",
  has_diff_binary_head,
  "Should use 'git diff --binary HEAD' not 'git add -A' to capture drift")

# Step 8: Check for untracked file listing
has_untracked_listing = "ls-files --others --exclude-standard" in IMPLEMENT_WITH_HAIKU
t("Drift block lists untracked owned files separately",
  has_untracked_listing,
  "Should use 'git ls-files --others --exclude-standard' for untracked files")

# Step D: Failure classification mentions cwd-drift as exception
classification_match = re.search(
    r"\*\*Failure classification\.\*\*(.*?)(?=\n\n## |\Z)",
    IMPLEMENT_WITH_HAIKU, re.S
)
if classification_match:
    classification_text = classification_match.group(1)
    mentions_exception = "cwd-drift" in classification_text and "exception" in classification_text.lower()
    t("Failure classification names cwd-drift as exception to multi-unit rule",
      mentions_exception,
      "Classification paragraph should mention cwd-drift as exception to 'Multi-unit failures are always other'")

# Step D: Telemetry vocabulary includes cwd_drift
has_cwd_drift_telemetry = "cwd_drift" in IMPLEMENT_WITH_HAIKU
t("Telemetry vocabulary includes cwd_drift class",
  has_cwd_drift_telemetry,
  "Should list 'cwd_drift' in Telemetry vocabulary section")

# Step D: Reason strings for drift
has_baseline_unavailable = "primary-checkout-drift: baseline-unavailable" in IMPLEMENT_WITH_HAIKU
has_cwd_drift_reason = "cwd-drift:" in IMPLEMENT_WITH_HAIKU or "primary-checkout-drift:" in IMPLEMENT_WITH_HAIKU
t("FAILED receipt includes 'primary-checkout-drift: baseline-unavailable' reason",
  has_baseline_unavailable,
  "Should mention baseline-unavailable reason")
t("FAILED receipt includes cwd-drift reason string",
  has_cwd_drift_reason,
  "Should mention cwd-drift or primary-checkout-drift in FAILED receipt reasons")

# Step 6: Block B (Working directory) should appear in single-unit prompt
single_unit_section = re.search(
    r"\*\*Single-unit fallback:\*\*(.*?)(?=\n## |\n### |\Z)",
    IMPLEMENT_WITH_HAIKU, re.S
)
if single_unit_section:
    su_text = single_unit_section.group(1)
    has_wd_mentioned = "Working directory:" in su_text
    t("Single-unit fallback prompt mentions Working directory block",
      has_wd_mentioned,
      "Single-unit section should reference Working directory block")

# Check Round 2 and Round 3 sections have Working directory references
# Extract Round 2 and Round 3 pass 1 sections
round2_match = re.search(r"### Round 2:.*?(?=\n### |\n## |\Z)", IMPLEMENT_WITH_HAIKU, re.S)
round3_match = re.search(r"### Round 3 pass 1:.*?(?=\n### |\n## |\Z)", IMPLEMENT_WITH_HAIKU, re.S)

has_round2_wd = round2_match and "Working directory:" in round2_match.group(0)
has_round3_wd = round3_match and "Working directory:" in round3_match.group(0)

t("Round 2 section references Working directory",
  has_round2_wd,
  "Round 2 section should mention Working directory block")
t("Round 3 pass 1 section references Working directory",
  has_round3_wd,
  "Round 3 pass 1 section should mention Working directory block")

print()

# ============================================================================
# STEP 7: Primary-checkout drift compare calls at all serial barriers
# ============================================================================
print("[Step 7] Primary-checkout drift compare calls at serial barriers")

# Find each location where compare should appear:
# 1. Single-unit fallback (after ledger row for round1)
single_unit_fallback = re.search(
    r"\*\*Single-unit fallback:\*\*(.*?)(?=\n## |\n### |\Z)",
    IMPLEMENT_WITH_HAIKU, re.S
)
if single_unit_fallback:
    su_text = single_unit_fallback.group(1)
    has_compare_in_su = "primary-checkout-drift.py" in su_text and "compare" in su_text
    t("Single-unit fallback includes compare call after ledger row",
      has_compare_in_su,
      "Single-unit fallback should call primary-checkout-drift.py compare after agent completes")
else:
    t("Single-unit fallback includes compare call after ledger row",
      False,
      "Could not find single-unit fallback section")

# 2. Round 2 apply (after the ledger row, before Part C)
round2_apply_match = re.search(
    r"### Apply Round 2's diff(.*?)(?=\n### Machine-check|\Z)",
    IMPLEMENT_WITH_HAIKU, re.S
)
if round2_apply_match:
    r2_apply_text = round2_apply_match.group(1)
    has_compare_after_r2 = "primary-checkout-drift.py" in r2_apply_text and "compare" in r2_apply_text
    t("Round 2 apply includes compare call after ledger row",
      has_compare_after_r2,
      "Round 2 apply should call primary-checkout-drift.py compare before Part C")
else:
    t("Round 2 apply includes compare call after ledger row",
      False,
      "Could not find '### Apply Round 2's diff' section")

# 3. Round 3 follow-up apply (after ledger row, before Round 4)
round3_followup_match = re.search(
    r"\*\*Applying and verifying Round 3's fixes:\*\*(.*?)(?=\n## Round 4|\Z)",
    IMPLEMENT_WITH_HAIKU, re.S
)
if round3_followup_match:
    r3_followup_text = round3_followup_match.group(1)
    has_compare_after_r3_followup = "primary-checkout-drift.py" in r3_followup_text and "compare" in r3_followup_text
    t("Round 3 follow-up apply includes compare call after ledger row",
      has_compare_after_r3_followup,
      "Round 3 follow-up apply should call primary-checkout-drift.py compare before Round 4")
else:
    t("Round 3 follow-up apply includes compare call after ledger row",
      False,
      "Could not find 'Applying and verifying Round 3's fixes' section")

# 4. End-of-run compare (before final summary heading)
final_summary_match = re.search(
    r"## Final summary(.*?)(?=\n## |\Z)",
    IMPLEMENT_WITH_HAIKU, re.S
)
if final_summary_match:
    final_text = final_summary_match.group(1)
    # The end-of-run compare should be BEFORE the "Collect each round's" line
    collect_pos = final_text.find("Collect each round's")
    if collect_pos > 0:
        before_collect = final_text[:collect_pos]
        has_compare_before_final = "primary-checkout-drift.py" in before_collect and "compare" in before_collect
        t("End-of-run compare appears before 'Collect each round's' in Final summary",
          has_compare_before_final,
          "Should call primary-checkout-drift.py compare as backstop before final summary begins")
    else:
        t("End-of-run compare appears before 'Collect each round's' in Final summary",
          False,
          "Could not find 'Collect each round's' line in final summary")
else:
    t("End-of-run compare appears before 'Collect each round's' in Final summary",
      False,
      "Could not find ## Final summary section")

print()

# ============================================================================
# STEP F5: No "salvage" wording in cwd-drift recovery contexts
# ============================================================================
print("[Step F5] No 'salvage' wording in cwd-drift recovery contexts")

# Search for cwd-drift sections and check for stale "salvage" terminology
drift_check_match = re.search(
    r"##+ .*[Dd]rift.*?\n(.*?)(?=\n## |\n### |\Z)",
    IMPLEMENT_WITH_HAIKU, re.I | re.S
)
if drift_check_match:
    drift_text = drift_check_match.group(1)
    # Check that "salvage" does not appear in cwd-drift recovery context
    # (legitimate non-cwd uses like "salvage procedure" should use different phrasing)
    has_stale_salvage = re.search(r"salvage.*cwd|cwd.*salvage|salvage.*drift|drift.*salvage", drift_text, re.I)
    t("Cwd-drift sections do not use stale 'salvage' terminology",
      not has_stale_salvage,
      "Should use 'failure procedure' or 'recovery' instead of 'salvage' in cwd-drift contexts")
else:
    t("Cwd-drift sections do not use stale 'salvage' terminology",
      True,
      "")

# Also check the Failure procedure section explicitly
failure_proc_match = re.search(
    r"### Failure procedure.*?\n(.*?)(?=\n## |\n### |\Z)",
    IMPLEMENT_WITH_HAIKU, re.I | re.S
)
if failure_proc_match:
    proc_text = failure_proc_match.group(1)
    has_salvage_in_proc = "salvage" in proc_text.lower()
    t("Failure procedure section does not contain 'salvage' wording",
      not has_salvage_in_proc,
      "Failure procedure should not mention 'salvage'; use 'failure procedure' or 'recovery' instead")

print()

# ============================================================================
# STEP F5/F8: Reason prefix pinning — single "primary-checkout-drift:" prefix
# ============================================================================
print("[Step F5/F8] Reason prefix consistency: only 'primary-checkout-drift:' allowed")

# Find all Reason: lines in cwd-drift receipts
reason_lines = re.findall(
    r"Reason:\s*([^\n]+)",
    IMPLEMENT_WITH_HAIKU
)

# Check that all drift-related reasons use "primary-checkout-drift:" prefix
bad_prefixes = []
for reason_line in reason_lines:
    if "drift" in reason_line.lower() or "cwd" in reason_line.lower():
        # This looks like a drift-related reason
        if not reason_line.startswith("primary-checkout-drift:"):
            bad_prefixes.append(reason_line)

t("All drift-related Reason: lines use 'primary-checkout-drift:' prefix",
  not bad_prefixes,
  f"Found non-compliant reasons: {bad_prefixes[:2]}" if bad_prefixes else "")

# Also check that there are no remaining "cwd-drift:" or "cwd-drift-overlap:" prefixes
has_old_cwd_drift = "cwd-drift:" in IMPLEMENT_WITH_HAIKU or "cwd-drift-overlap:" in IMPLEMENT_WITH_HAIKU
t("No old 'cwd-drift:' or 'cwd-drift-overlap:' prefixes remain in doc",
  not has_old_cwd_drift,
  "Found old cwd-drift prefixes; should be 'primary-checkout-drift:'")

print()

h.summarize_and_exit()
