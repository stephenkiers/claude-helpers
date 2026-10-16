#!/usr/bin/env python3
"""
Test suite asserting that the install.sh linked-worktree guard is recorded as a
decision: CLAUDE.md's Setup section says installs come from the main worktree, and
ADR-0010 carries an amendment saying the same and why.

Doc assertions read the real files at test time; they check the requirement, not
the wording, so they tolerate rephrasing.

Run with: python3 tests/test_install_worktree_guard_docs.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
ADR_0010_GLOB = "docs/adr/0010-*.md"


def section_under(text, heading):
    """Body of the `## <heading>` section (up to the next level-2 heading), or ""."""
    m = re.search(rf"(?m)^##\s+{re.escape(heading)}\b[^\n]*$", text)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = re.search(r"(?m)^##\s", rest)
    return rest[: nxt.start()] if nxt else rest


def adr_0010_text():
    matches = sorted((REPO_ROOT).glob(ADR_0010_GLOB))
    if not matches:
        return ""
    return matches[0].read_text()


def amendment_tail(adr_text):
    """Text from the first 'amend' mention onward, or '' when there is none."""
    m = re.search(r"(?i)amend", adr_text)
    return adr_text[m.start():] if m else ""


def main():
    h = Harness("install.sh main-worktree guard is documented (CLAUDE.md + ADR-0010)")

    claude = CLAUDE_MD.read_text() if CLAUDE_MD.exists() else ""
    setup = section_under(claude, "Setup")
    h.test_result(
        "CLAUDE.md has a Setup section",
        bool(setup),
        "no level-2 'Setup' heading found",
    )
    h.test_result(
        "CLAUDE.md Setup says installs run from the main worktree",
        bool(re.search(r"(?i)main[\s-]+worktree", setup)),
        "Setup section does not mention the main worktree",
    )
    h.test_result(
        "CLAUDE.md Setup documents the linked-worktree escape hatch (--from-worktree)",
        "--from-worktree" in setup,
        "Setup section does not mention --from-worktree",
    )

    adr = adr_0010_text()
    h.test_result(
        "ADR-0010 file exists",
        bool(adr),
        "no docs/adr/0010-*.md found",
    )
    tail = amendment_tail(adr)
    h.test_result(
        "ADR-0010 carries an amendment",
        bool(tail),
        "no 'amend' mention in ADR-0010",
    )
    h.test_result(
        "ADR-0010 amendment records that links must point at the main worktree",
        bool(re.search(r"(?i)main[\s-]+worktree", tail)) and bool(re.search(r"(?i)install", tail)),
        "amendment does not mention both the install and the main worktree",
    )

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
