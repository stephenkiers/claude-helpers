#!/usr/bin/env python3
"""
Test suite for the issues.json cache lookup in the command docs that read it.

The documented cache is dict-keyed (docs/workflow-cli-invariants.md; ADR-0013
validators reject a non-dict `issues`). The array-shaped
`{"issues": [{"number": N}]}` form has no producer, so the lookup in
commands/cleanup.md and commands/track.md must be map-only: no array iteration over
`issues`, no array literal written to `issues`, and no `.number` match against an
issues array.

These are text assertions on the real command docs, read at test time.

Run with: python3 tests/test_cleanup_issues_cache_lookup.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

DOCS = [
    REPO_ROOT / "commands" / "cleanup.md",
    REPO_ROOT / "commands" / "track.md",
]

# Each pattern is one way an array-shaped `issues` cache gets read or written.
ARRAY_SHAPE_PATTERNS = {
    "iterates an issues array (.issues[] or .issues[ ]|)": r"\.issues\s*\[\s*\]",
    "array literal written under the issues key": r"[\"']issues[\"']\s*:\s*\[",
    "matches .number against an issues array on one line": r"\.issues[^\n]*\.number",
    "select on .number inside a list": r"select\(\s*\.number",
}


def main():
    h = Harness("issues.json cache lookup is map-only (cleanup.md, track.md)")

    for doc in DOCS:
        rel = doc.relative_to(REPO_ROOT)
        if not doc.exists():
            h.test_result(f"{rel} exists", False, "doc missing")
            continue
        text = doc.read_text()
        for label, pattern in ARRAY_SHAPE_PATTERNS.items():
            hit = re.search(pattern, text)
            line_no = text[: hit.start()].count("\n") + 1 if hit else None
            h.test_result(
                f"{rel}: does not {label}",
                hit is None,
                f"matched at line {line_no}: {hit.group(0)!r}" if hit else "",
            )

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
