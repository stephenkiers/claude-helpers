#!/usr/bin/env python3
"""
Spec-blind doc-fact suite for the #341 follow-ups (ROUND 1 contract and maxTurns look-back).

Written only from the plan's explicit statements about the docs. Each check reads a real repo
doc at test time and asserts a fact the plan names. Facts that depend on the outcome of the
maxTurns probe (plan Step 2) or on wording the plan does not pin are deliberately not asserted.

Facts used:
- commands/expert-flow.md: the strict `^ROUND 1\\b` regex is no longer restated (plan Step 6c,
  the step table and receipt rule cite the verifier's pattern instead).
- commands/expert-flow.md: the sentence "The required `ROUND 1` line comes from the command's own
  final summary" is reworded (plan Step 6a).
- commands/implement-with-haiku.md: the producer contract "summary exactly as emitted" stays
  (plan Step 6a, :1984).
- docs/adr/0023-expert-flow.md: a dated 2026-10-2x amendment exists; it records
  "insufficient data", keeps maxTurns at 120, names escalations.jsonl and the event: "attempt"
  stream as the re-check trigger, and keeps the "Raising or overriding maxTurns" out-of-scope line
  (plan Steps 8 and Q4).
- docs/adr/0004-model-cost-routing.md: the "reserved for when a turns source becomes available"
  statement points at the new amendment (plan Step 8).
- CLAUDE.md: the /expert-flow bullet "automatic escalation is dormant until a turns source exists"
  points at a 2026-10-2x amendment instead of only the 2026-10-16 one (plan Step 8).

Run with: python3 tests/test_spec_blind_expert_flow_docs.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

EXPERT_FLOW = REPO_ROOT / "commands" / "expert-flow.md"
IMPLEMENT_WITH_HAIKU = REPO_ROOT / "commands" / "implement-with-haiku.md"
ADR_0023 = REPO_ROOT / "docs" / "adr" / "0023-expert-flow.md"
ADR_0004 = REPO_ROOT / "docs" / "adr" / "0004-model-cost-routing.md"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"

h = Harness("SPEC-BLIND: EXPERT-FLOW ROUND 1 AND MAXTURNS DOC FACTS")
t = h.test_result


def read(path):
    return path.read_text(encoding="utf-8") if path.exists() else ""


def paragraph_around(text, needle):
    """Return the blank-line-delimited paragraph containing needle (case-insensitive), or ''."""
    idx = text.lower().find(needle.lower())
    if idx < 0:
        return ""
    start = text.rfind("\n\n", 0, idx)
    end = text.find("\n\n", idx)
    start = 0 if start < 0 else start
    end = len(text) if end < 0 else end
    return text[start:end]


def line_containing(text, needle):
    for line in text.splitlines():
        if needle.lower() in line.lower():
            return line
    return ""


expert_flow = read(EXPERT_FLOW)
implement = read(IMPLEMENT_WITH_HAIKU)
adr23 = read(ADR_0023)
adr04 = read(ADR_0004)
claude_md = read(CLAUDE_MD)

# ---------------------------------------------------------------------------
print("[Step 6(c)] expert-flow.md does not restate the strict ^ROUND 1\\b pattern")
print("=" * 70)

t("commands/expert-flow.md exists", EXPERT_FLOW.exists(), "missing file")
t("expert-flow.md no longer contains the strict pattern ^ROUND 1\\b",
  r"^ROUND 1\b" not in expert_flow,
  "the literal ^ROUND 1\\b still appears; it should cite the verifier's pattern instead")

# ---------------------------------------------------------------------------
print()
print("[Step 6(a)] implement step wording no longer says the line comes from the command's summary")
print("=" * 70)

old_sentence = "The required `ROUND 1` line comes from the command's own final summary"
t("expert-flow.md drops the old 'comes from the command's own final summary' sentence",
  old_sentence not in expert_flow,
  "the superseded sentence is still present")

t("implement-with-haiku.md keeps the producer contract 'summary exactly as emitted'",
  "summary exactly as emitted" in implement,
  "the producer contract phrase is missing from commands/implement-with-haiku.md")

# ---------------------------------------------------------------------------
print()
print("[Items 1 and 2] ADR-0023 carries a dated 2026-10-2x amendment for the maxTurns look-back")
print("=" * 70)

amend_heading = re.search(r"^#+\s.*Amendment \(2026-10-2\d\)", adr23, re.MULTILINE)
t("ADR-0023 has a heading 'Amendment (2026-10-2x)'", amend_heading is not None,
  "no dated 2026-10-2x amendment heading found")

# Scope the rest of the checks to the text from the 2026-10-2x amendment onward.
amend_text = adr23[amend_heading.start():] if amend_heading else ""

t("amendment records 'insufficient data' for the maxTurns look-back",
  "insufficient data" in amend_text.lower(),
  "the amendment does not say 'insufficient data'")
t("amendment keeps maxTurns at 120",
  "maxTurns" in amend_text and "120" in amend_text,
  "the amendment does not state that maxTurns stays 120")
t("amendment names escalations.jsonl as the re-check stream",
  "escalations.jsonl" in amend_text,
  "escalations.jsonl is not named in the amendment")
t("amendment names the event: \"attempt\" lines as the trigger stream",
  'event: "attempt"' in amend_text,
  'the event: "attempt" stream is not named in the amendment')
t("ADR-0023 still carries the 'Raising or overriding maxTurns' out-of-scope line",
  "Raising or overriding" in adr23 and "maxTurns" in adr23,
  "the out-of-scope line for raising maxTurns was removed")

# ---------------------------------------------------------------------------
print()
print("[Step 8] ADR-0004 turns-source statement points at the ADR-0023 amendment")
print("=" * 70)

adr04_phrase = "reserved for when a turns source becomes available"
para = paragraph_around(adr04, adr04_phrase)
t("ADR-0004 turns-source statement is either removed or points at ADR-0023",
  (not para) or ("0023" in para),
  "the 'reserved for when a turns source becomes available' paragraph does not cite ADR-0023")

# ---------------------------------------------------------------------------
print()
print("[Step 8] CLAUDE.md /expert-flow bullet points at a 2026-10-2x amendment")
print("=" * 70)

claude_line = line_containing(claude_md, "automatic escalation is dormant until a turns source exists")
t("CLAUDE.md dormant-escalation bullet cites a 2026-10-2x amendment",
  bool(re.search(r"2026-10-2\d", claude_line)),
  f"bullet does not cite a 2026-10-2x amendment; line: {claude_line[:200]!r}")

h.summarize_and_exit()
