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
import subprocess
import sys
import tempfile
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

h.summarize_and_exit()
