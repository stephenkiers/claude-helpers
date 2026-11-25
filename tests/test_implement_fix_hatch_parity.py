#!/usr/bin/env python3
"""Parity guard for the human-approved test-change hatch (#530).

The implement and fix steps share one approval-commit procedure (section 4.7 of
commands/implement-with-haiku.md). Every difference between the steps must be a row of
its "Differences between the steps" table. This test fails when the shared section is
duplicated, when the table loses a row, or when a step stops pointing at the shared
procedure.

Run with: python3 tests/test_implement_fix_hatch_parity.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

DOC = (REPO_ROOT / "commands" / "implement-with-haiku.md").read_text()
SHARED = "### 4.7 Human-approved test changes (shared by fix and implement)"
ALLOWED_ASPECTS = [
    "When it runs",
    "Apply method",
    "Items",
    "Live re-verify (step 0)",
    "Parent invariant (step 5)",
    "Answers file",
    "Ledger",
]


def table_aspects() -> list:
    start = DOC.index("**Differences between the steps (the complete list).**")
    block = DOC[start:].split("\n\n")[1]
    rows = [r for r in block.splitlines() if r.startswith("|")][2:]
    return [r.split("|")[1].strip() for r in rows]


h = Harness("IMPLEMENT/FIX HATCH PARITY")
t = h.test_result

t("exactly one shared 4.7 procedure", DOC.count(SHARED) == 1, f"found {DOC.count(SHARED)}")
t(
    "no second, fix-only copy of the procedure",
    "Approval commit: human-approved test edits (fix step only)" not in DOC,
)
try:
    aspects = table_aspects()
except (ValueError, IndexError):
    aspects = []
t("difference table rows are exactly the allowlist", aspects == ALLOWED_ASPECTS, f"got {aspects}")
t(
    "both steps run the shared 4.7 steps 0-5",
    len(re.findall(r"run 4\.7 steps 0.5 now", DOC)) >= 2,
)
stub = DOC.find("### 4.7 Approval commit (fix step)")
t(
    "fix 4.7 heading defers to the shared procedure",
    stub != -1 and "shared procedure" in DOC[stub : stub + 900],
)
t(
    "allow flags limited to the single-commit scan",
    "never to Gate step 2's range scan" in DOC,
)

h.summarize_and_exit()
