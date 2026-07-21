#!/usr/bin/env python3
"""
Test suite for removing decisions.yaml / ledger machinery.

Plan: Remove decisions.yaml / ledger machinery from expert-review
- Removes LEDGER_FILE and DECISIONS_FILE variables from commands/expert-review.md
- Removes ledger-lines.jsonl row from files table
- Removes Step 13 entirely (Record the rulings)
- Removes decisions/ledger references from prompts/expert-framework.md
- Removes "Already settled" bucket from prompts/triage.md
- Removes decisions/ledger mentions from prompts/amalgamator.md
- Stubs out commands/review-stats.md
- Updates CLAUDE.md Triage section

Run with: python3 tests/test_remove_decisions_ledger.py
"""

import os
import re

from _test_harness import REPO_ROOT, Harness

COMMANDS_DIR = REPO_ROOT / "commands"
PROMPTS_DIR = REPO_ROOT / "prompts"

h = Harness("DECISIONS/LEDGER REMOVAL TEST SUITE")
test_result = h.test_result

# ============================================================================
# SECTION 1: commands/expert-review.md
# ============================================================================
print("[Section 1] commands/expert-review.md")

expert_review_file = COMMANDS_DIR / "expert-review.md"
expert_review_content = expert_review_file.read_text() if expert_review_file.exists() else ""

test_result(
    "expert-review.md exists",
    expert_review_file.exists(),
    "File not found"
)

# 1.1: LEDGER_FILE variable should not be present
test_result(
    "LEDGER_FILE variable removed",
    "LEDGER_FILE=" not in expert_review_content,
    "Found LEDGER_FILE= in expert-review.md (should be removed)"
)

# 1.2: DECISIONS_FILE variable should not be present
test_result(
    "DECISIONS_FILE variable removed",
    "DECISIONS_FILE=" not in expert_review_content,
    "Found DECISIONS_FILE= in expert-review.md (should be removed)"
)

# 1.3: ledger-lines.jsonl row should not be present
test_result(
    "ledger-lines.jsonl table row removed",
    "ledger-lines.jsonl" not in expert_review_content,
    "Found ledger-lines.jsonl in expert-review.md (should be removed)"
)

# 1.4: The step about recording rulings (Step 13: Record the rulings) should be gone
# This step was described as having three writes: decisions.yaml, ADR, and ledger append
test_result(
    "Step 13 (Record the rulings) removed",
    "Record the rulings" not in expert_review_content,
    "Found 'Record the rulings' section in expert-review.md (should be removed)"
)

# 1.5: Prose block explaining why decisions.yaml lives outside repo should be removed
test_result(
    "Decisions.yaml external location prose removed",
    "outside the repo" not in expert_review_content or "decisions.yaml" not in expert_review_content,
    "Found prose about decisions.yaml being outside repo (should be removed)"
)

# 1.6: Cross-run memory explanation should be shortened (mention REVIEW_DIR but not ledger/decisions)
test_result(
    "Ledger/decisions mention removed from cross-run memory explanation",
    "ledger" not in expert_review_content.lower() or "decisions" not in expert_review_content.lower(),
    "Found ledger or decisions reference in cross-run memory section"
)

# 1.7: Triage Chief invocation should NOT contain $DECISIONS_FILE
test_result(
    "Triage Chief invocation does not pass DECISIONS_FILE",
    "$DECISIONS_FILE" not in expert_review_content,
    "Found $DECISIONS_FILE variable reference in expert-review.md (should be removed)"
)

# 1.8: REVIEW_DIR should still be present (it's still used for per-invocation reviews)
test_result(
    "REVIEW_DIR variable still present",
    "REVIEW_DIR=" in expert_review_content,
    "REVIEW_DIR should still be present for per-invocation review caching"
)

# 1.9: Step 13 (old Cache Review Metadata) should be renumbered (new Step 13 from old Step 14)
# The old Step 14 becomes Step 13, so there should be no old Step 14 label, but Step 13 should exist
test_result(
    "Cache Review Metadata step still present (renumbered)",
    "Cache Review Metadata" in expert_review_content or "Step 13" in expert_review_content,
    "Cache Review Metadata step should still exist (renumbered to Step 13)"
)

print()

# ============================================================================
# SECTION 2: prompts/expert-framework.md
# ============================================================================
print("[Section 2] prompts/expert-framework.md")

expert_framework_file = PROMPTS_DIR / "expert-framework.md"
expert_framework_content = expert_framework_file.read_text() if expert_framework_file.exists() else ""

test_result(
    "expert-framework.md exists",
    expert_framework_file.exists(),
    "File not found"
)

# 2.1: "Recorded decisions are settled law" section should be removed
test_result(
    "Recorded decisions are settled law section removed",
    "Recorded decisions are settled law" not in expert_framework_content,
    "Found 'Recorded decisions are settled law' section (should be removed)"
)

# 2.2: "Report what a decision suppressed" section should be removed
test_result(
    "Report what a decision suppressed section removed",
    "Report what a decision suppressed" not in expert_framework_content,
    "Found 'Report what a decision suppressed' section (should be removed)"
)

# 2.3: "Suppressed by decision" output block should be removed
test_result(
    "Suppressed by decision output block removed",
    "Suppressed by decision" not in expert_framework_content,
    "Found 'Suppressed by decision' output block (should be removed)"
)

# 2.4: Load Project Context section should still exist
test_result(
    "Load Project Context section still present",
    "Load Project Context" in expert_framework_content,
    "Load Project Context section should still be present"
)

# 2.5: recorded-decisions file bullet should be removed from Load Project Context
test_result(
    "Recorded-decisions file bullet removed from Load Project Context",
    "recorded-decisions" not in expert_framework_content or "Load Project Context" not in expert_framework_content,
    "Found recorded-decisions reference in Load Project Context (should be removed)"
)

print()

# ============================================================================
# SECTION 3: prompts/triage.md
# ============================================================================
print("[Section 3] prompts/triage.md")

triage_file = PROMPTS_DIR / "triage.md"
triage_content = triage_file.read_text() if triage_file.exists() else ""

test_result(
    "triage.md exists",
    triage_file.exists(),
    "File not found"
)

# 3.1: "Already settled" bucket should be removed (bucket 4)
test_result(
    "Already settled bucket removed",
    "Already settled" not in triage_content or "4." not in triage_content,
    "Found 'Already settled' bucket in triage.md (should be removed)"
)

# 3.2: Ledger.jsonl references should be removed
test_result(
    "ledger.jsonl references removed",
    "ledger.jsonl" not in triage_content and "ledger-lines" not in triage_content,
    "Found ledger.jsonl or ledger-lines references in triage.md (should be removed)"
)

# 3.3: decisions.yaml.template reference should be removed
test_result(
    "decisions.yaml.template reference removed from Read section",
    "decisions.yaml.template" not in triage_content,
    "Found decisions.yaml.template reference in triage.md (should be removed)"
)

# 3.4: recorded-decisions file bullet should be removed
test_result(
    "recorded-decisions file bullet removed",
    "recorded-decisions file" not in triage_content,
    "Found recorded-decisions file reference (should be removed)"
)

# 3.5: "Proposing decisions" section should be removed
test_result(
    "Proposing decisions section removed",
    "Proposing decisions" not in triage_content,
    "Found 'Proposing decisions' section (should be removed)"
)

# 3.6: "Proposed decision" field should not appear in template
test_result(
    "Proposed decision field removed from Needs-you template",
    "**Proposed decision**" not in triage_content,
    "Found 'Proposed decision' field in template (should be removed)"
)

# 3.7: action-plan.md template should not have "Already settled" section
test_result(
    "Already settled section removed from action-plan template",
    "## Already settled" not in triage_content,
    "Found '## Already settled' section in template (should be removed)"
)

# 3.8: "Recurring?" bullet in gut check should reference ledger (should be removed)
test_result(
    "Recurring bullet ledger reference removed",
    "ledger.jsonl" not in triage_content,
    "Found ledger.jsonl in gut check Recurring bullet (should be removed)"
)

# 3.9: Receipt line should not mention settled: or wrote-ledger:
test_result(
    "Receipt line does not mention settled or ledger",
    "| settled:" not in triage_content and "| wrote-ledger:" not in triage_content,
    "Found settled: or wrote-ledger: in Receipt line (should be removed)"
)

# 3.10: The sanity-check line should be updated (confirmed = doing + needs-you + deferred)
test_result(
    "Sanity check updated (no Already settled in calculation)",
    "doing + needs-you + deferred" in triage_content or "doing + needs_you + deferred" in triage_content,
    "Sanity check line should show confirmed = doing + needs-you + deferred (without Already settled)"
)

# 3.11: Core buckets 1, 2, 3 should still be present
test_result(
    "Core buckets still present (Doing it, Needs you, Deferred)",
    "Doing it" in triage_content and "Needs you" in triage_content and "Deferred" in triage_content,
    "Core buckets (Doing it, Needs you, Deferred) should still be present"
)

# 3.12: DECISIONS_FILE template variable should not be present
test_result(
    "DECISIONS_FILE template variable removed",
    "{DECISIONS_FILE}" not in triage_content,
    "Found {DECISIONS_FILE} template variable in triage.md (should be removed)"
)

print()

# ============================================================================
# SECTION 4: prompts/amalgamator.md
# ============================================================================
print("[Section 4] prompts/amalgamator.md")

amalgamator_file = PROMPTS_DIR / "amalgamator.md"
amalgamator_content = amalgamator_file.read_text() if amalgamator_file.exists() else ""

test_result(
    "amalgamator.md exists",
    amalgamator_file.exists(),
    "File not found"
)

# 4.1: "Suppressed Findings" section should be removed
test_result(
    "Suppressed Findings section removed",
    "Suppressed Findings" not in amalgamator_content,
    "Found 'Suppressed Findings' section in amalgamator.md (should be removed)"
)

# 4.2: Table collecting "Suppressed by decision" output should be removed
test_result(
    "Suppressed by decision table removed",
    "Suppressed by decision" not in amalgamator_content,
    "Found 'Suppressed by decision' table in amalgamator.md (should be removed)"
)

print()

# ============================================================================
# SECTION 5: commands/review-stats.md
# ============================================================================
print("[Section 5] commands/review-stats.md")

review_stats_file = COMMANDS_DIR / "review-stats.md"
review_stats_content = review_stats_file.read_text() if review_stats_file.exists() else ""

test_result(
    "review-stats.md exists",
    review_stats_file.exists(),
    "File not found"
)

# 5.1: Command body should be a stub paragraph
test_result(
    "review-stats.md is now a stub",
    "ledger machinery was removed" in review_stats_content or "non-functional" in review_stats_content,
    "review-stats.md should be stubbed with note that ledger machinery was removed"
)

# 5.2: The command should NOT have old ledger processing logic
test_result(
    "review-stats.md does not contain ledger processing logic",
    "ledger.jsonl" not in review_stats_content,
    "Found ledger.jsonl references in review-stats.md (should be removed)"
)

print()

# ============================================================================
# SECTION 6: CLAUDE.md
# ============================================================================
print("[Section 6] CLAUDE.md")

claude_md_file = REPO_ROOT / "CLAUDE.md"
claude_md_content = claude_md_file.read_text() if claude_md_file.exists() else ""

test_result(
    "CLAUDE.md exists",
    claude_md_file.exists(),
    "File not found"
)

# 6.1: "Already settled" bucket description should be removed
test_result(
    "Already settled bucket removed from CLAUDE.md",
    "Already settled" not in claude_md_content or "CLAUDE" not in claude_md_content,
    "Found 'Already settled' bucket in CLAUDE.md Triage section (should be removed)"
)

# 6.2: "Decisions are recorded and reviewers obey them" paragraph should be removed
test_result(
    "Decisions are recorded paragraph removed",
    "Decisions are recorded and reviewers obey them" not in claude_md_content,
    "Found 'Decisions are recorded and reviewers obey them' paragraph (should be removed)"
)

# 6.3: decisions.yaml references in CLAUDE.md should be removed
test_result(
    "decisions.yaml references removed from CLAUDE.md",
    "decisions.yaml" not in claude_md_content,
    "Found decisions.yaml references in CLAUDE.md (should be removed)"
)

# 6.4: ledger.jsonl references should be removed
test_result(
    "ledger.jsonl references removed from CLAUDE.md",
    "ledger.jsonl" not in claude_md_content,
    "Found ledger.jsonl references in CLAUDE.md (should be removed)"
)

# 6.5: /review-stats should be noted as non-functional
test_result(
    "/review-stats marked as non-functional",
    "/review-stats" in claude_md_content and ("non-functional" in claude_md_content or "currently" in claude_md_content),
    "/review-stats should be noted as non-functional in Commands section"
)

# 6.6: decisions.yaml.template should NOT be deleted (it's still on disk for reference)
test_result(
    "decisions.yaml.template still exists on disk",
    (PROMPTS_DIR / "decisions.yaml.template").exists(),
    "decisions.yaml.template should still exist as a reference for future re-addition"
)

# 6.7: Triage section should still exist and be intact
test_result(
    "Triage section still present in CLAUDE.md",
    "## Triage" in claude_md_content or "### Triage" in claude_md_content,
    "Triage section should still be present"
)

print()

# ============================================================================
# SECTION 7: Cross-file integrity checks
# ============================================================================
print("[Section 7] Cross-file integrity")

# 7.1: No file should reference the old mechanisms
forbidden_phrases = [
    ("ledger", ["expert-review.md", "triage.md", "amalgamator.md"]),
    ("decisions.yaml", ["expert-framework.md", "expert-review.md", "triage.md", "amalgamator.md"]),
    ("ledger.jsonl", ["expert-review.md", "triage.md", "amalgamator.md"]),
    ("Suppressed by decision", ["expert-framework.md", "amalgamator.md"])
]

for phrase, should_not_be_in in forbidden_phrases:
    violations = []
    for filename in should_not_be_in:
        if filename == "expert-review.md":
            content = expert_review_content
        elif filename == "expert-framework.md":
            content = expert_framework_content
        elif filename == "triage.md":
            content = triage_content
        elif filename == "amalgamator.md":
            content = amalgamator_content
        else:
            content = ""

        # Use case-insensitive search for more robust matching
        if phrase.lower() in content.lower():
            violations.append(filename)

    if violations:
        test_result(
            f"Phrase '{phrase}' removed from {', '.join(should_not_be_in)}",
            len(violations) == 0,
            f"Still found in: {', '.join(violations)}"
        )

print()

# ============================================================================
# SECTION 8: Preservation of core functionality
# ============================================================================
print("[Section 8] Core functionality preserved")

# 8.1: expert-review.md should still have Triage Chief invocation (just without DECISIONS_FILE)
test_result(
    "Triage Chief invocation still present",
    "Triage Chief" in expert_review_content or "triage" in expert_review_content.lower(),
    "Triage Chief should still be invoked in Step 11"
)

# 8.2: expert-framework.md should still have Load Project Context with project.yaml
test_result(
    "Project context loading still mentions project.yaml",
    "project.yaml" in expert_framework_content,
    "project.yaml should still be referenced in Load Project Context"
)

# 8.3: triage.md should still have the three core buckets
test_result(
    "All three core buckets preserved",
    "1." in triage_content and "2." in triage_content and "3." in triage_content,
    "Core buckets (1. Doing it, 2. Needs you, 3. Deferred) should all be present"
)

# 8.4: triage.md action-plan template should still exist
test_result(
    "action-plan.md template still present",
    "action-plan.md" in triage_content or "## Output" in triage_content,
    "action-plan.md template should still be present"
)

# 8.5: amalgamator.md should still exist and have core structure
test_result(
    "amalgamator.md still has core functionality",
    "final-report.md" in amalgamator_content or "finding" in amalgamator_content.lower(),
    "amalgamator.md should still synthesize findings (just without suppressed findings table)"
)

print()
h.summarize_and_exit()
