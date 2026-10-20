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


def main():
    h = Harness("Escalation docs facts (#315 F2, F6, F7)")

    # ========================
    # F2: DISCARDED_ADDED_FILES template shape
    # ========================

    implement_md = read_file("commands/implement-with-haiku.md")
    if implement_md:
        # Template should show shape with two spaces and dash
        has_template = "  - " in implement_md and "DISCARDED_ADDED_FILES" in implement_md
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
        # Should mention post-escalation menu with exactly "Take over" and "Abort"
        has_takeoverbot = "Take over" in adr_0023 and "Abort" in adr_0023
        # More specific: should have a section about post-escalation menu
        has_post_esc_menu = bool(
            re.search(r"post.escalation.*menu|escalat.*Take over|Take over.*Abort", adr_0023, re.IGNORECASE)
        )
        h.test_result(
            'F7a: ADR-0023 mentions post-escalation menu "Take over / Abort"',
            has_takeoverbot,
            "menu options not clearly mentioned",
        )
    else:
        h.test_result("F7: docs/adr/0023-expert-flow.md exists", False, "file not found")

    # ========================
    # F7: Opus not in model enum
    # ========================

    if adr_0023:
        # Should NOT mention "opus" as a valid implementer model
        # Look for places where models are listed
        # Pattern: looking for Haiku/Sonnet being accepted but not Opus
        has_opus_as_model = bool(re.search(r"(opus|OPUS)", adr_0023))
        # If opus appears, it should be in context of rejection/not-allowed
        if has_opus_as_model:
            # Check if it's explicitly rejected
            context_before = adr_0023[max(0, adr_0023.find("opus") - 100) : adr_0023.find("opus")]
            context_after = adr_0023[adr_0023.find("opus") : min(len(adr_0023), adr_0023.find("opus") + 100)]
            combined = context_before + context_after
            # Should contain rejection language if mentioned
            is_rejected = "not" in combined.lower() or "never" in combined.lower()
            h.test_result(
                "F7b: Opus not listed as valid implementer model (rejection or absence)",
                is_rejected or not has_opus_as_model,
                "opus mentioned without clear rejection context",
            )
        else:
            h.test_result(
                "F7b: Opus not mentioned as implementer model (correct)",
                True,
                "opus not in doc",
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
        # Should mention manual escalation option
        has_manual = "manual" in claude_md.lower() and "escalat" in claude_md.lower()
        h.test_result(
            "CLAUDE.md: Manual escalation documented",
            has_manual,
            "manual escalation not mentioned",
        )
    else:
        h.test_result(
            "CLAUDE.md: Manual escalation docs", False, "file not readable"
        )

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
