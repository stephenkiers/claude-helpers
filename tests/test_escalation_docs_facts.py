#!/usr/bin/env python3
"""
Machine-testable facts about escalation from docs and reference files.

Verifies doc-pinned facts from feature #315 action plan:
- DISCARDED_ADDED_FILES template shows shape `  - <path>` (F2)
- Fourth menu option is named "Retry with sonnet" (F6, from CLAUDE.md)
- Post-escalation menu is "Take over / Abort" (F7, from ADR-0023)
- Opus is not accepted as implementer model (F7, from docs)
- Issue #325 is referenced (from action plan)

Run with: python3 tests/test_escalation_docs_facts.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness


def read_file(path):
    """Read file, return text. Path is relative to REPO_ROOT."""
    full_path = REPO_ROOT / path
    if not full_path.exists():
        return None
    return full_path.read_text(encoding="utf-8")


def section_between(text, start_marker, end_marker):
    """Return text from start_marker up to (not including) end_marker.

    Returns "" when start_marker is absent; runs to end of text when end_marker is absent.
    """
    start = text.find(start_marker)
    if start == -1:
        return ""
    end = text.find(end_marker, start + 1)
    return text[start:end] if end != -1 else text[start:]


def main():
    h = Harness("Escalation docs facts (#315 F2, F6, F7)")

    # ========================
    # F2: DISCARDED_ADDED_FILES template shape
    # ========================

    implement_md = read_file("commands/implement-with-haiku.md")
    if implement_md:
        # Template should show shape with two spaces and dash
        # Look for the actual template in context
        # The template should be in a code block showing example format
        template_pattern = r"DISCARDED_ADDED_FILES:\s*\n\s+- "
        has_proper_template = bool(re.search(template_pattern, implement_md))
        h.test_result(
            "F2: DISCARDED_ADDED_FILES template shows shape '  - <path>'",
            has_proper_template,
            "template format not found or incorrect indentation",
        )
    else:
        h.test_result("F2: commands/implement-with-haiku.md exists", False, "file not found")

    # ========================
    # F6: Fourth menu option named "Retry with sonnet"
    # ========================

    claude_md = read_file("CLAUDE.md")
    if claude_md:
        # Should mention "Retry with sonnet" as the fourth option for escalation
        has_retry_sonnet = "Retry with sonnet" in claude_md
        h.test_result(
            'F6: CLAUDE.md mentions "Retry with sonnet" as fourth menu option',
            has_retry_sonnet,
            "phrase not found",
        )
    else:
        h.test_result("F6: CLAUDE.md exists", False, "file not found")

    # ========================
    # F7: Post-escalation menu is "Take over / Abort"
    # ========================

    adr_0023 = read_file("docs/adr/0023-expert-flow.md")
    if adr_0023:
        # The post-escalation menu sentence added by #334 says the menu after a
        # failed escalated attempt is "Take over / Abort" with no Retry. Pre-#334
        # text also contains "Take over" and "Abort" (the first-hard-stop menu), so
        # the anchor is the "no Retry" qualifier on the same line.
        has_post_esc_menu = bool(
            re.search(r"Take over / Abort[^\n]*no Retry", adr_0023)
        )
        h.test_result(
            'F7a: ADR-0023 mentions post-escalation menu "Take over / Abort"',
            has_post_esc_menu,
            'no line pairs "Take over / Abort" with "no Retry"',
        )
    else:
        h.test_result("F7: docs/adr/0023-expert-flow.md exists", False, "file not found")

    # ========================
    # F7: Opus excluded from implementer escalation
    # ========================

    if adr_0023:
        # #334's amendment states implementer escalation is Sonnet only and
        # never Opus. Pre-#334 ADR-0023 mentions Opus nowhere, so a bare absence
        # check was vacuous; assert the explicit exclusion instead.
        has_opus_exclusion = bool(re.search(r"Sonnet only,?\s+never Opus", adr_0023))
        h.test_result(
            "F7b: ADR-0023 excludes Opus from implementer escalation (Sonnet only)",
            has_opus_exclusion,
            'no "Sonnet only, never Opus" statement in ADR-0023',
        )
    else:
        h.test_result(
            "F7b: Check docs for opus mention", False, "adr-0023 not readable"
        )

    # ========================
    # ADR-0004: "never Opus" principle
    # ========================

    adr_0004 = read_file("docs/adr/0004-model-cost-routing.md")
    if adr_0004:
        # Should state that Opus is never used for implementation
        has_never_opus = bool(
            re.search(r"never.*opus|opus.*never", adr_0004, re.IGNORECASE)
        )
        h.test_result(
            "ADR-0004: 'never opus' principle mentioned",
            has_never_opus,
            "principle not clearly stated",
        )
    else:
        h.test_result("ADR-0004: docs/adr/0004-model-cost-routing.md exists", False, "file not found")

    # ========================
    # Automatic escalation dormant state
    # ========================

    if claude_md:
        # Should mention automatic escalation is dormant
        has_dormant = "dormant" in claude_md and "escalation" in claude_md
        h.test_result(
            "CLAUDE.md: Automatic escalation documented as dormant",
            has_dormant,
            "dormant state not mentioned",
        )
    else:
        h.test_result(
            "CLAUDE.md: Automatic escalation docs", False, "file not readable"
        )

    # ========================
    # Manual escalation documented
    # ========================

    if claude_md:
        # #334 added the manual Retry with sonnet option to the /expert-flow command
        # bullet. Pre-#334 CLAUDE.md has "manual" only in an unrelated bullet
        # (style guide), so check that the /expert-flow bullet itself carries both.
        expert_flow_bullet = section_between(
            claude_md, "- `/expert-flow` —", "- `/merge-and-cleanup` —"
        )
        has_manual = (
            "Retry with sonnet" in expert_flow_bullet
            and re.search(r"manual", expert_flow_bullet, re.IGNORECASE) is not None
        )
        h.test_result(
            "CLAUDE.md: Manual escalation documented",
            has_manual,
            "/expert-flow bullet does not document manual Retry with sonnet escalation",
        )
    else:
        h.test_result(
            "CLAUDE.md: Manual escalation docs", False, "file not readable"
        )

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
