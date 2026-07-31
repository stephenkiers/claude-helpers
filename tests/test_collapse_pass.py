#!/usr/bin/env python3
"""
Test suite for collapse pass feature (issue #42).

The collapse pass is a cross-cutting scan that runs after bucket assignment and
before output writing in the Triage Chief prompt. When it finds a policy decision
that resolves ≥2 findings, it promotes the policy call to Needs You as the primary
escalation and marks subsumed findings as `Resolved by:` in Doing It.

This suite tests the wiring invariants: section presence, positioning, receipt
field presence, and receipt parity across command and prompt files.

Cosmetic wording is not tested; wiring is.

Run with: python3 tests/test_collapse_pass.py
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

h = Harness("COLLAPSE PASS TEST SUITE (Issue #42)")
t = h.test_result

# ============================================================================
# INVARIANT 1: "Collapse pass" section exists in triage.md
# ============================================================================
print("[Invariant 1] Collapse pass section exists in triage.md")

collapse_section = re.search(r"^## Collapse pass\b", TRIAGE, re.M)
t("triage.md contains a '## Collapse pass' section", collapse_section is not None,
  "the new pass is missing or mis-named")

# ============================================================================
# INVARIANT 2: Collapse pass section is positioned after gut check, before Output
# ============================================================================
print("\n[Invariant 2] Collapse pass positioning in triage.md")

gut_check_pos = TRIAGE.find("## The gut check")
collapse_pos = TRIAGE.find("## Collapse pass")
output_pos = TRIAGE.find("## Output")

t("gut check section exists in triage.md", gut_check_pos != -1,
  "cannot verify positioning without the gut check section")
t("Output section exists in triage.md", output_pos != -1,
  "cannot verify positioning without the Output section")

if gut_check_pos != -1 and collapse_pos != -1 and output_pos != -1:
    t("Collapse pass appears after The gut check section",
      collapse_pos > gut_check_pos,
      f"gut check at {gut_check_pos}, collapse at {collapse_pos}")
    t("Collapse pass appears before Output section",
      collapse_pos < output_pos,
      f"collapse at {collapse_pos}, Output at {output_pos}")

# ============================================================================
# INVARIANT 3: Collapse pass section contains core guardrail keywords
# ============================================================================
print("\n[Invariant 3] Collapse pass section contains required guardrails")

if collapse_section is not None:
    # Extract the section body (from "## Collapse pass" until next ## or EOF)
    section_match = re.search(
        r"^## Collapse pass\b(.*?)(?=\n## |\Z)",
        TRIAGE,
        re.M | re.S
    )
    section_body = section_match.group(1) if section_match else ""

    # Core question: "Does one policy or design decision resolve ≥2 findings?"
    t("Collapse pass asks about policy decisions resolving findings",
      bool(re.search(r"policy|design decision", section_body, re.I))
      and bool(re.search(r"≥\s*2|>=\s*2|at least 2", section_body, re.I)),
      "core question about policy decisions and 2+ findings is missing")

    # "If yes" action: promote to Needs You
    t("Collapse pass instructs to promote to Needs You",
      bool(re.search(r"Needs You|needs.you|escalate", section_body, re.I)),
      "the 'If yes' action to promote to Needs You is missing")

    # "Resolved by:" marking in Doing It
    t("Collapse pass instructs to mark subsumed findings 'Resolved by:'",
      "Resolved by:" in section_body,
      "the instruction to mark subsumed findings is missing")

    # Guardrail: one clause / ~15 words
    t("Collapse pass contains guardrail about one-clause nameability",
      bool(re.search(r"clause|word|name", section_body, re.I)),
      "guardrail about nameability is missing")

    # Guardrail: ≥2 findings
    t("Collapse pass guardrail: ≥2 findings",
      bool(re.search(r"≥\s*2|>=\s*2|at least 2", section_body, re.I)),
      "guardrail about minimum 2 findings is missing")

    # Cap: at most 2 collapse promotions
    t("Collapse pass contains cap of at most 2 promotions",
      bool(re.search(r"(?:at most|no more than|maximum|cap).*?2|2.*?(?:promotions|collapses|collapse)",
                      section_body, re.I)),
      "cap on collapse promotions is missing or incorrect")

    # Skip silently instruction
    t("Collapse pass contains instruction to skip silently when nothing collapses",
      bool(re.search(r"skip.*silent|silently", section_body, re.I)),
      "skip-silently instruction is missing")

# ============================================================================
# INVARIANT 4: Triage receipt line contains 'collapsed:' field
# ============================================================================
print("\n[Invariant 4] Triage receipt field includes collapsed counter")

def receipt(text, head):
    """Extract the full receipt line starting with head."""
    m = re.search(r"^(" + re.escape(head) + r".*)$", text, re.M)
    return m.group(1).strip() if m else None

tri_prompt = receipt(TRIAGE, "triage | doing:")
t("triage receipt line exists in triage.md", tri_prompt is not None,
  "cannot verify collapsed field without a receipt line")

if tri_prompt is not None:
    t("triage receipt contains 'collapsed:' field",
      "collapsed:" in tri_prompt,
      f"receipt line: {tri_prompt!r}")

# ============================================================================
# INVARIANT 5: Receipt field order includes collapsed in correct position
# ============================================================================
print("\n[Invariant 5] Triage receipt field order (with collapsed)")

# Canonical field order: doing → needs-you → measure → deferred → declined → clusters → collapsed → wrote-plan.
RECEIPT_FIELD_ORDER = ["doing:", "needs-you:", "measure:", "deferred:", "declined:", "clusters:", "collapsed:", "wrote-plan:"]

if tri_prompt is not None:
    positions = [tri_prompt.find(f) for f in RECEIPT_FIELD_ORDER]
    all_present = all(p != -1 for p in positions)
    t("all receipt fields are present (including collapsed)",
      all_present,
      f"missing fields: {[RECEIPT_FIELD_ORDER[i] for i, p in enumerate(positions) if p == -1]}")

    if all_present:
        t("receipt fields appear in canonical order (collapsed immediately before wrote-plan)",
          positions == sorted(positions),
          f"field positions: {list(zip(RECEIPT_FIELD_ORDER, positions))}")

        collapsed_idx = RECEIPT_FIELD_ORDER.index("collapsed:")
        wrote_plan_idx = RECEIPT_FIELD_ORDER.index("wrote-plan:")
        t("collapsed field is immediately before wrote-plan field",
          collapsed_idx == wrote_plan_idx - 1,
          "collapsed field must be the last numeric counter before wrote-plan")

# ============================================================================
# INVARIANT 6: Receipt parity: collapsed field is identical in both files
# ============================================================================
print("\n[Invariant 6] Receipt parity across command and prompt")

tri_cmd = receipt(EXPERT_REVIEW, "triage | doing:")
t("triage receipt line exists in expert-review.md", tri_cmd is not None,
  "command file does not contain the triage receipt")

if tri_cmd is not None and tri_prompt is not None:
    t("triage receipt is identical in both command and prompt",
      tri_cmd == tri_prompt,
      f"command: {tri_cmd!r}\n      prompt: {tri_prompt!r}")

h.summarize_and_exit()
