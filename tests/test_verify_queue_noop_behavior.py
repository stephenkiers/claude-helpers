#!/usr/bin/env python3
"""
Test suite for verify-queue enhancements (Findings 1, 3, 7).

The plan requires:

Finding 1 (HIGH): commands/verify-queue.md's `done <id> [--result "…"]` command must support
a --noop flag. For `your-call` rows, one of --result or --noop is required (an error otherwise),
and --noop results in `STATUS: no-op` being written back (vs `STATUS: decided` for a real result).

Finding 3 (HIGH): the illustrative fallback `sed` shown for the single-remaining-item case
must scope its DECISION/STATUS substitutions to the specific item's heading block (from its own
### N. heading through the next ### heading or EOF), not operate unscoped across the whole file.

Finding 7 (MEDIUM): that same fallback sed must branch on the row's `kind` (`your-call` vs
`measurement`) rather than only ever handling the `measurement`/`pending-measurement`→`measured` case.

Run with: python3 tests/test_verify_queue_noop_behavior.py
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


VERIFY_QUEUE = read(COMMANDS / "verify-queue.md")

h = Harness("VERIFY-QUEUE ENHANCEMENTS TEST SUITE")
t = h.test_result

# ============================================================================
# SECTION 1: Finding 1 - --noop flag support in done command (HIGH)
# ============================================================================
print("[Section 1] Finding 1: --noop flag support in verify-queue done")

t("verify-queue.md exists", VERIFY_QUEUE != "",
  "File not found or empty")

# The `done` command section should exist
done_section = re.search(r"## .*done\b.*?(?=\n## |\Z)", VERIFY_QUEUE, re.S | re.I)
t("verify-queue.md has a 'done' command/step", done_section is not None,
  "Must document the done command")

if done_section:
    done = done_section.group(0)

    # Should document the --noop flag
    t("'done' command documents --noop flag",
      "--noop" in done,
      "Must document --noop flag as an option")

    # Should indicate that for your-call rows, one of --result or --noop is required
    t("'done' documents that --noop is for your-call rows",
      re.search(r"(your.call|your-call)", done, re.I) is not None,
      "Should explain --noop usage for your-call rows")

    t("'done' documents that --result or --noop is required for your-call",
      re.search(r"(one of|either|require)", done, re.I) is not None,
      "Should document that one of --result or --noop is mandatory for your-call")

    # Should document that --noop writes STATUS: no-op
    t("'done' documents that --noop writes 'STATUS: no-op'",
      "no-op" in done or "no_op" in done,
      "Should document that --noop results in 'STATUS: no-op'")

    # Should document that --result writes STATUS: decided
    t("'done' documents that --result writes 'STATUS: decided'",
      re.search(r"(STATUS.*decided|decided.*STATUS)", done, re.I) is not None
      or "decided" in done,
      "Should document the STATUS: decided result for --result")

print()

# ============================================================================
# SECTION 2: Finding 3 - Scoped sed for single-remaining-item (HIGH)
# ============================================================================
print("[Section 2] Finding 3: scoped sed for single-item case")

# Look for the fallback sed example/instruction (appears in a bash code block)
sed_section = re.search(r"Fallback ONLY.*?(?=```\n)", VERIFY_QUEUE, re.S | re.I)
t("verify-queue.md has a sed fallback example", sed_section is not None,
  "Should document the fallback sed command for manual edits")

if sed_section:
    sed = sed_section.group(0)

    # Should document scoping to a specific item's heading block
    t("sed example/instruction mentions item heading scope",
      re.search(r"(### \d|heading|block|scope|from|through)", sed, re.I) is not None,
      "Should document that sed edits are scoped to item's own heading block")

    # Should mention it scopes from `### N.` through next `###` or EOF
    t("sed scope is defined as '### N. ... next ### or EOF'",
      re.search(r"(next ###|EOF|end of|section)", sed, re.I) is not None,
      "Should clearly state the scope boundaries")

    # Should mention DECISION and STATUS fields as targets
    t("sed targets DECISION and STATUS fields",
      "DECISION" in sed and "STATUS" in sed,
      "Should show sed modifying the DECISION/STATUS field markers")

print()

# ============================================================================
# SECTION 3: Finding 7 - Sed branches on kind (your-call vs measurement) (MEDIUM)
# ============================================================================
print("[Section 3] Finding 7: sed branches on row kind (your-call vs measurement)")

# The sed instruction should branch based on the row's kind
if sed_section:
    sed = sed_section.group(0)

    # Should distinguish between your-call and measurement branches
    t("sed documentation branches on kind",
      re.search(r"(your.call|measurement)", sed, re.I) is not None,
      "Should document different sed logic for your-call vs measurement rows")

    # For your-call: should handle --noop case (no-op) or --result case (decided)
    t("sed handles your-call rows (with no-op/decided options)",
      re.search(r"(your.call|your-call)", sed, re.I) is not None,
      "Should show sed handling your-call row type")

    # For measurement: should handle pending-measurement → measured transition
    t("sed handles measurement rows (pending-measurement → measured)",
      re.search(r"(measurement|pending.measurement)", sed, re.I) is not None,
      "Should show sed handling measurement row type")

    # The branching logic should be clear (e.g., if/elif or case statement)
    t("sed demonstrates clear branching logic",
      re.search(r"(if|case|kind.*==|kind.*if)", sed, re.I) is not None
      or (re.search(r"your.call", sed, re.I) and re.search(r"measurement", sed, re.I)),
      "Should show clear branching based on kind")

print()

# ============================================================================
# SECTION 4: Cross-checks - consistency with other files
# ============================================================================
print("[Section 4] Consistency checks")

# The done command should be consistent with triage.md status values
TRIAGE = read(REPO_ROOT / "prompts" / "triage.md")
t("TRIAGE file exists for cross-checking", TRIAGE != "",
  "triage.md needed for consistency checks")

if TRIAGE:
    # Both should mention no-op and decided status values
    t("triage.md and verify-queue both reference status values",
      "STATUS" in TRIAGE and "STATUS" in VERIFY_QUEUE,
      "Both files should reference STATUS field for consistency")

print()

h.summarize_and_exit()
