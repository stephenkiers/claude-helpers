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
    """Return a file's text, or '' if it is missing — so a moved/renamed file turns into a
    failing assertion (see the non-empty guard below), never a suite-crashing exception that would
    skip every later invariant."""
    try:
        return path.read_text()
    except OSError:
        return ""


EXPERT_REVIEW = read(COMMANDS / "expert-review.md")
TRIAGE = read(PROMPTS / "triage.md")

h = Harness("COLLAPSE PASS TEST SUITE (Issue #42)")
t = h.test_result

# ============================================================================
# INVARIANT 0: The files under test exist and are non-empty. read() swallows a
# missing file into '', which would otherwise make every "section missing" failure
# below ambiguous — a moved file and a deleted section look identical. Fail loudly here.
# ============================================================================
print("[Invariant 0] Files under test are present and non-empty")
t("prompts/triage.md is present and non-empty", bool(TRIAGE),
  "file is missing or empty — every downstream invariant would be meaningless")
t("commands/expert-review.md is present and non-empty", bool(EXPERT_REVIEW),
  "file is missing or empty — the receipt-parity invariant would be meaningless")


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

    # Core question: one policy/design decision resolving >=2 findings. This is the ONE place the
    # >=2 threshold is asserted structurally (a second copy in the guardrail block was a duplicate).
    t("Collapse pass asks whether one policy/design decision resolves >=2 findings",
      bool(re.search(r"policy|design decision", section_body, re.I))
      and bool(re.search(r"≥\s*2|>=\s*2|at least 2|two", section_body, re.I)),
      "core question about a policy decision resolving 2+ findings is missing")

    # Consolidation-only (Ruling #1, #42): the pass merges escalations already in Needs you; it does
    # NOT promote accepted Doing-it fixes into new escalations, and writes no `Resolved by:` marking.
    t("Collapse pass acts on escalations already in Needs you (consolidation-only)",
      bool(re.search(r"Needs you|needs.you", section_body, re.I))
      and bool(re.search(r"consolidat", section_body, re.I)),
      "the consolidation instruction (merge existing Needs-you escalations) is missing")
    t("Collapse pass does NOT reintroduce the dropped 'Resolved by:' Doing-it marking",
      "Resolved by:" not in section_body,
      "the Branch-A `Resolved by:` marking was dropped by Ruling #1 — it must not return")

    # Guardrail: one clause / nameability
    t("Collapse pass contains guardrail about one-clause nameability",
      bool(re.search(r"clause|word|name", section_body, re.I)),
      "guardrail about nameability is missing")

    # Cap: anchored on the '**Cap:**' phrasing so nearby prose can't false-positive it.
    cap_match = re.search(r"\*\*Cap:\*\*(.*?)(?=\n\*\*|\Z)", section_body, re.S)
    t("Collapse pass declares a cap of at most 2 consolidations",
      cap_match is not None and "2" in cap_match.group(1),
      "cap on collapse consolidations is missing, unanchored, or not set to 2")

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

        # Adjacency must be checked against the REAL receipt string, not the Python list above
        # (comparing indices into RECEIPT_FIELD_ORDER is tautological — it never reads the file).
        # No other field may appear between collapsed: and wrote-plan: in the actual line.
        between = tri_prompt[tri_prompt.find("collapsed:"):tri_prompt.find("wrote-plan:")]
        intervening = [f for f in RECEIPT_FIELD_ORDER
                       if f not in ("collapsed:", "wrote-plan:") and f in between]
        t("collapsed: is immediately before wrote-plan: in the actual receipt line",
          tri_prompt.find("collapsed:") < tri_prompt.find("wrote-plan:") and not intervening,
          f"fields between collapsed: and wrote-plan: — {intervening}; line: {tri_prompt!r}")

# ============================================================================
# INVARIANT 6: Receipt parity: the receipt line is byte-identical in both files
# ============================================================================
print("\n[Invariant 6] Receipt parity across command and prompt")

def receipt_raw(text, head):
    """Extract the receipt line WITHOUT stripping — so a trailing-whitespace divergence between
    the two files is caught, not masked. The 'identical' claim is only true if it is byte-for-byte."""
    m = re.search(r"^(" + re.escape(head) + r".*)$", text, re.M)
    return m.group(1) if m else None

tri_cmd_raw = receipt_raw(EXPERT_REVIEW, "triage | doing:")
tri_prompt_raw = receipt_raw(TRIAGE, "triage | doing:")
t("triage receipt line exists in expert-review.md", tri_cmd_raw is not None,
  "command file does not contain the triage receipt")

if tri_cmd_raw is not None and tri_prompt_raw is not None:
    t("triage receipt is byte-identical in both command and prompt (surrounding whitespace included)",
      tri_cmd_raw == tri_prompt_raw,
      f"command: {tri_cmd_raw!r}\n      prompt: {tri_prompt_raw!r}")

h.summarize_and_exit()
