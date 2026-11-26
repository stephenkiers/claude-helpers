#!/usr/bin/env python3
"""
Test suite for the ADR-0013 amendments that govern the cleanup teardown scope (#567).

Checks, on the real ADR text read at test time:
- Amendment 15 points forward to Amendment 15.1.
- Amendment 15.1 cites #567's scope boundary.
- Amendment 15.1 records the rejected narrower-scoping option.

These are structural text assertions; they do not judge the wording beyond the
plan-named content.

Run with: python3 tests/test_deterministic_workflow_cli_adr.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

ADR_0013 = REPO_ROOT / "docs" / "adr" / "0013-deterministic-workflow-cli.md"

AMENDMENT_15_RE = re.compile(r"Amendment 15(?![.\d])")
AMENDMENT_15_1_RE = re.compile(r"Amendment 15\.1(?!\d)")
HEADER_LIKE_RE = re.compile(r"^[ \t]*#+[^\n]*$", re.MULTILINE)


def header_lines_with(text, pattern):
    """Return (char_offset, line) for header-like lines matching pattern."""
    hits = []
    for m in HEADER_LIKE_RE.finditer(text):
        if pattern.search(m.group(0)):
            hits.append((m.start(), m.group(0)))
    return hits


def section_after(text, start):
    """Text from start up to the next amendment heading or top-level heading."""
    first_nl = text.find("\n", start)
    body_start = first_nl + 1 if first_nl != -1 else len(text)
    rest = text[body_start:]
    nxt = re.search(r"^#{2,3} ", rest, re.MULTILINE)
    end = body_start + (nxt.start() if nxt else len(rest))
    return text[start:end]


def main():
    h = Harness("ADR-0013 amendments 15 / 15.1 (cleanup scope, #567)")

    if not ADR_0013.exists():
        h.test_result("ADR-0013 exists", False, "docs/adr/0013-deterministic-workflow-cli.md missing")
        h.summarize_and_exit()

    text = ADR_0013.read_text()

    a15 = header_lines_with(text, AMENDMENT_15_RE)
    a151 = header_lines_with(text, AMENDMENT_15_1_RE)

    h.test_result(
        "ADR-0013 has an Amendment 15 header",
        len(a15) > 0,
        "no header line for Amendment 15",
    )
    h.test_result(
        "ADR-0013 has an Amendment 15.1 header",
        len(a151) > 0,
        "no header line for Amendment 15.1",
    )

    if a15:
        offset15 = a15[0][0]
        window = section_after(text, offset15)
        # Amendment 15 must point forward to 15.1, not stand alone.
        h.test_result(
            "Amendment 15 points to Amendment 15.1",
            "15.1" in window,
            "no reference to 15.1 in the Amendment 15 section",
        )

    if a151:
        offset151 = a151[0][0]
        section = section_after(text, offset151)
        h.test_result(
            "Amendment 15.1 cites #567's scope boundary",
            "#567" in section,
            "no #567 reference in the Amendment 15.1 section",
        )
        h.test_result(
            "Amendment 15.1 records the rejected narrower-scoping option",
            re.search(r"narrow", section, re.IGNORECASE) is not None,
            "no 'narrow' wording in the Amendment 15.1 section",
        )

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
