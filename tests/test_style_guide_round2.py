#!/usr/bin/env python3
"""
Test suite for style-guide.json cascade integration (findings 1–8 from PR #131 round 2).

Findings under test:
1. Backup step checks cp exit status and uses timestamped backup filename
2. Pre-flight mutation-keyword check before gh api graphql calls
3. Step 3 (gh search prs) failure-handling: skip on error, note skipped repo count
4. Step 6 escape hatch: "or all surviving examples if fewer than 6 remain"
5. Step 4 GraphQL login binding via jq --arg (not interpolation)
6. Step 5 shortfall message includes PR/repo counts alongside comment count
7. pr-comment-guide.md Tone section unified cascade-consumption with both examples and toneNotes
8. ADR-0017 cross-references folded into Consequences, no standalone section

Run with: python3 tests/test_style_guide_round2.py
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


GENERATE_GUIDE = read(COMMANDS / "generate-style-guide.md")
PR_COMMENT_GUIDE = read(PROMPTS / "pr-comment-guide.md")
ADR17 = read(ADRS / "0017-style-guide-cascade.md")

h = Harness("STYLE GUIDE CASCADE INTEGRATION TEST SUITE (Round 2)")
t = h.test_result

# ============================================================================
# FINDING 1: Backup step checks cp exit status and uses timestamped filename
# ============================================================================
print("[Finding 1] Backup step with exit-status check and timestamp")

t("Step 8 references backup with timestamped filename pattern",
  "style-guide.json.bak.$timestamp" in GENERATE_GUIDE,
  "Expected backup filename pattern 'style-guide.json.bak.$timestamp' not found")

t("Step 8 creates timestamp variable via date +%s",
  re.search(r"timestamp=\$\(date \+%s\)", GENERATE_GUIDE) is not None,
  "Expected 'timestamp=$(date +%s)' not found")

t("Step 8 checks cp exit status with || error handling",
  re.search(r"cp.*style-guide\.json\.bak\.\$timestamp\s+\|\|\s*\{\s*echo.*exit 1", GENERATE_GUIDE, re.DOTALL) is not None,
  "Expected 'cp ... || { echo ...; exit 1; }' error-handling pattern not found")

t("Step 8 backup comment explains clobber prevention",
  re.search(r"repeated.*interrupted.*don't clobber|clobber.*prior backup", GENERATE_GUIDE, re.IGNORECASE | re.DOTALL) is not None,
  "Expected explanation about preventing clobber with timestamp not found")

# ============================================================================
# FINDING 2: Pre-flight mutation-keyword check before gh api graphql
# ============================================================================
print("\n[Finding 2] Pre-flight mutation-keyword check")

t("Step 4 includes mutation pre-flight check instruction",
  re.search(r"Before executing.*gh api graphql.*verify.*query.*mutation.*case-insensitive", GENERATE_GUIDE, re.IGNORECASE | re.DOTALL) is not None,
  "Expected pre-flight check instruction for 'mutation' keyword not found")

t("Pre-flight check says to stop and report on mutation",
  re.search(r"mutation.*stop.*report|report.*error.*mutation", GENERATE_GUIDE, re.IGNORECASE | re.DOTALL) is not None,
  "Expected instruction to 'stop and report error' for mutation found")

t("Step 4 includes read-only constraint about mutations",
  re.search(r"never.*mutation.*only.*query|query.*operations.*never.*mutation", GENERATE_GUIDE, re.IGNORECASE | re.DOTALL) is not None,
  "Expected 'never mutation' read-only constraint not found")

# ============================================================================
# FINDING 3: Step 3 failure-handling matches Step 4 (skip + count)
# ============================================================================
print("\n[Finding 3] Step 3 failure-handling: skip on error, note skipped count")

t("Step 3 mentions skipping repos on error",
  re.search(r"Step 3.*error.*skip|skip.*repo.*error", GENERATE_GUIDE, re.IGNORECASE | re.DOTALL) is not None,
  "Expected Step 3 error-handling 'skip repo' pattern not found")

t("Step 3 says to note count of skipped repos in report",
  re.search(r"Step 3.*skipped.*count|count.*skipped.*repos|note.*skipped", GENERATE_GUIDE, re.IGNORECASE | re.DOTALL) is not None,
  "Expected 'note skipped repo count' in Step 3 not found")

# ============================================================================
# FINDING 4: Step 6 escape hatch for fewer than 6 examples
# ============================================================================
print("\n[Finding 4] Step 6 escape hatch for 6-12 examples")

t("Step 6 mentions picking 6-12 examples",
  "6-12" in GENERATE_GUIDE or re.search(r"6.*12.*examples|pick.*6.*12", GENERATE_GUIDE, re.IGNORECASE) is not None,
  "Expected '6-12 examples' guidance in Step 6 not found")

t("Step 6 includes escape hatch for fewer than 6",
  re.search(r"or all.*examples.*fewer.*6|fewer.*6.*all.*surviving", GENERATE_GUIDE, re.IGNORECASE | re.DOTALL) is not None,
  "Expected escape hatch 'or all surviving examples if fewer than 6 remain' not found")

# ============================================================================
# FINDING 5: Step 4 login binding via jq --arg (not interpolation)
# ============================================================================
print("\n[Finding 5] Step 4 GraphQL login binding via jq --arg")

t("Step 4 GraphQL uses jq --arg flag for login",
  re.search(r"jq\s+--arg\s+login", GENERATE_GUIDE) is not None,
  "Expected 'jq --arg login' binding pattern not found")

t("Step 4 jq filter references $login variable",
  re.search(r"select\([^)]*\$login[^)]*\)|jq.*\$login", GENERATE_GUIDE) is not None,
  "Expected jq filter to use '$login' variable reference not found")

t("Step 4 comment explains jq --arg convention",
  re.search(r"login.*bound.*jq\s*--arg.*CLAUDE\.md|jq.*--arg.*shell.*boundary", GENERATE_GUIDE, re.IGNORECASE | re.DOTALL) is not None,
  "Expected explanation of jq --arg convention and CLAUDE.md reference not found")

# ============================================================================
# FINDING 6: Step 5 shortfall message includes PR/repo counts
# ============================================================================
print("\n[Finding 6] Step 5 shortfall message with counts")

t("Step 5 shortfall message example includes comment count",
  re.search(r"substantive comments.*across|comments.*found", GENERATE_GUIDE, re.IGNORECASE) is not None,
  "Expected example shortfall message with comment count not found")

t("Step 5 shortfall message example includes repo count",
  re.search(r"repos.*PRs|repos.*searched", GENERATE_GUIDE, re.IGNORECASE | re.DOTALL) is not None,
  "Expected repo/PR count in Step 5 shortfall message not found")

t("Step 5 shortfall example uses format like 'X comments across Y repos and Z PRs'",
  re.search(r"(\d+|only)\s+(substantive\s+)?comments.*\d+\s+repos.*\d+\s+PRs|comments.*across.*repos.*PRs", GENERATE_GUIDE, re.IGNORECASE | re.DOTALL) is not None,
  "Expected full shortfall message format with comment, repo, and PR counts")

# ============================================================================
# FINDING 7: pr-comment-guide.md Tone section unified cascade
# ============================================================================
print("\n[Finding 7] pr-comment-guide.md Tone section unified cascade")

t("Tone section states 'examples' and 'toneNotes' loaded together",
  re.search(r"Load.*apply.*examples.*toneNotes|both.*examples.*toneNotes", PR_COMMENT_GUIDE, re.IGNORECASE | re.DOTALL) is not None,
  "Expected 'Load and apply both examples and toneNotes together' not found")

t("Tone section mentions REPLACE-ME placeholder filtering",
  re.search(r"REPLACE ME|replace me|placeholder", PR_COMMENT_GUIDE, re.IGNORECASE) is not None,
  "Expected REPLACE-ME placeholder skip instruction not found")

t("Tone section says REPLACE-ME/empty check applies to both arrays",
  re.search(r"either\s+array.*empty.*REPLACE|empty.*filtering.*both|minimum-count.*toneNotes.*examples", PR_COMMENT_GUIDE, re.IGNORECASE | re.DOTALL) is not None,
  "Expected minimum-count/empty-check instruction for both arrays not found")

t("Tone section requires fallback-disclosure for 'neither file present' case",
  re.search(r"no.*style guide|no.*file.*present", PR_COMMENT_GUIDE, re.IGNORECASE) is not None and
  re.search(r"Note.*Summary|Summary.*layer.*used", PR_COMMENT_GUIDE, re.IGNORECASE | re.DOTALL) is not None,
  "Expected fallback-disclosure for 'no file present' case not found")

t("Tone section requires fallback-disclosure for 'empty after filtering' case",
  re.search(r"personal.*empty.*placeholder.*shipped default|fell back.*shipped default", PR_COMMENT_GUIDE, re.IGNORECASE | re.DOTALL) is not None,
  "Expected fallback-disclosure for 'empty after filtering' case not found")

t("Tone section describes three-layer cascade: personal → shipped default → inline",
  re.search(r"three.*layer.*cascade|personal.*shipped.*inline|last-resort.*inline", PR_COMMENT_GUIDE, re.IGNORECASE | re.DOTALL) is not None,
  "Expected three-layer cascade description not found")

t("Tone section says cascade chains regardless of failure mode",
  re.search(r"missing.*unreadable.*unparse|try.*next.*layer|chain.*regardless", PR_COMMENT_GUIDE, re.IGNORECASE | re.DOTALL) is not None,
  "Expected 'chains regardless of failure mode' instruction not found")

# ============================================================================
# FINDING 8: ADR-0017 cross-references folded into Consequences
# ============================================================================
print("\n[Finding 8] ADR-0017 format: no standalone Cross-references section")

t("ADR-0017 cross-references (ADR-0005, ADR-0007) are in Consequences",
  re.search(r"## Consequences.*ADR-0005.*ADR-0007", ADR17, re.DOTALL) is not None,
  "Expected ADR-0005 and ADR-0007 cross-references in Consequences section")

t("ADR-0017 does not have a standalone '## Cross-references' section",
  re.search(r"^## Cross-references", ADR17, re.MULTILINE) is None,
  "Found standalone '## Cross-references' section (should be folded into Consequences)")

t("ADR-0017 cross-references use 'See also:' format (matching other ADRs)",
  re.search(r"\*\*See also:\*\*.*ADR", ADR17) is not None,
  "Expected 'See also:' format for cross-references not found")

# ============================================================================
# SUMMARY
# ============================================================================
print()
h.summarize_and_exit()
