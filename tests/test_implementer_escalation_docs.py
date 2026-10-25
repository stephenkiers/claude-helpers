#!/usr/bin/env python3
"""
Doc-contract tests for implementer escalation feature (#315).

Tests verify that command docs, ADRs, and other documentation maintain
contracts for the --implementer-model flag, receipt fields, and escalation logic.

Run with: python3 tests/test_implementer_escalation_docs.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT


def main():
    h = Harness("IMPLEMENTER ESCALATION DOCS TEST SUITE")

    # Load relevant files
    impl_cmd = (REPO_ROOT / "commands" / "implement-with-haiku.md").read_text()
    flow_cmd = (REPO_ROOT / "commands" / "expert-flow.md").read_text()
    adr23 = (REPO_ROOT / "docs" / "adr" / "0023-expert-flow.md").read_text()
    adr04 = (REPO_ROOT / "docs" / "adr" / "0004-model-cost-routing.md").read_text()
    claude_md = (REPO_ROOT / "CLAUDE.md").read_text()
    plan_agent = (REPO_ROOT / "agents" / "plan-implementer.md").read_text()

    print("[Section 1] --implementer-model flag in implement-with-haiku.md")
    h.test_result(
        "flags section parses --implementer-model",
        "--implementer-model" in impl_cmd and "haiku|sonnet|opus" in impl_cmd,
        "missing flag or values"
    )

    print()
    print("[Section 2] Receipt template includes new fields")
    h.test_result(
        "FAILURE_CLASS: field in receipt template",
        "FAILURE_CLASS:" in impl_cmd,
        "missing"
    )
    h.test_result(
        "IMPLEMENTER_MODEL: field in receipt template",
        "IMPLEMENTER_MODEL:" in impl_cmd,
        "missing"
    )
    h.test_result(
        "UNITS: field in receipt template",
        "UNITS:" in impl_cmd,
        "missing"
    )
    h.test_result(
        "TURNS_USED: field in receipt template",
        "TURNS_USED:" in impl_cmd,
        "missing"
    )
    h.test_result(
        "MAX_TURNS: field in receipt template",
        "MAX_TURNS:" in impl_cmd,
        "missing"
    )
    h.test_result(
        "TURNS_EXHAUSTED: field in receipt template",
        "TURNS_EXHAUSTED:" in impl_cmd,
        "missing"
    )
    h.test_result(
        "INCOMPLETE_STEPS: field in receipt template",
        # The template line is bare "  INCOMPLETE_STEPS:" (indented, end of line). Pre-#334
        # prose has "incomplete" but never this field line.
        re.search(r"^\s*INCOMPLETE_STEPS:\s*$", impl_cmd, re.MULTILINE) is not None,
        "missing receipt template line"
    )
    h.test_result(
        "PARTIAL_WORK: field in receipt template (escalated attempts)",
        "PARTIAL_WORK:" in impl_cmd,
        "missing"
    )
    h.test_result(
        "DISCARDED_ADDED_FILES: field in receipt template",
        "DISCARDED_ADDED_FILES:" in impl_cmd,
        "missing"
    )

    print()
    print("[Section 3] implementer_shortfall failure class")
    h.test_result(
        "failure class rule: UNITS: 1 required",
        "UNITS: 1" in impl_cmd and "implementer-shortfall" in impl_cmd,
        "missing class rule"
    )
    h.test_result(
        "failure class rule: all-units-failed writes other",
        "all-units-failed" in impl_cmd and "FAILURE_CLASS: other" in impl_cmd,
        "missing"
    )

    print()
    print("[Section 4] Model applied at exactly three launch sites")
    # Check for the three launch sites mentioned in Step 5:
    # - the single-unit fallback (line 483)
    # - Step 4b round-1 units (line 533)
    # - Gate step 4 fix-loop units (line 942)

    impl_with_lines = impl_cmd.split('\n')

    # Count --implementer-model mentions
    model_mentions = [i for i, line in enumerate(impl_with_lines, 1) if '--implementer-model' in line]

    h.test_result(
        "--implementer-model mentioned in the doc",
        len(model_mentions) > 0,
        f"found {len(model_mentions)} mentions"
    )

    # Check that some specific context mentions launch sites
    h.test_result(
        "single-unit fallback uses --implementer-model flag",
        "single-unit" in impl_cmd and "--implementer-model" in impl_cmd,
        "missing single-unit or flag reference"
    )

    print()
    print("[Section 5] Step 6 discard validation block")
    h.test_result(
        "git ls-tree validation",
        # The exact Step 6 "did not exist at base" check added by #334. Pre-#334 the doc
        # has an unrelated "git ls-tree -r --name-only" listing, so match the full command.
        r'ls-tree --literal-pathspecs "$START_SHA" -- "$DISCARD_PATH"' in impl_cmd,
        "missing Step 6 ls-tree validation at START_SHA"
    )
    h.test_result(
        "--diff-filter=A validation",
        "--diff-filter=A" in impl_cmd,
        "missing"
    )
    h.test_result(
        "git rm -f validation",
        "git rm -f" in impl_cmd,
        "missing"
    )

    print()
    print("[Section 6] Turns source handling")
    h.test_result(
        "TURNS_USED: unknown default when no source",
        "TURNS_USED: unknown" in impl_cmd,
        "missing TURNS_USED: unknown field"
    )
    h.test_result(
        "MAX_TURNS from frontmatter",
        "MAX_TURNS" in impl_cmd and "frontmatter" in impl_cmd.lower(),
        "missing"
    )

    print()
    print("[Section 7] Escalated attempt handling in plan-implementer.md")
    h.test_result(
        "INCOMPLETE_STEPS: optional block in agent",
        "INCOMPLETE_STEPS:" in plan_agent,
        "missing from agent contract"
    )
    h.test_result(
        "PARTIAL_WORK: optional block for escalated attempts",
        "PARTIAL_WORK:" in plan_agent,
        "missing from agent contract"
    )
    h.test_result(
        "DISCARDED_ADDED_FILES: optional block in agent",
        "DISCARDED_ADDED_FILES:" in plan_agent,
        "missing from agent contract"
    )
    h.test_result(
        "Escalated attempt section exists",
        "Escalated" in plan_agent or "escalated" in plan_agent,
        "missing section"
    )

    print()
    print("[Section 8] ADR amendments")
    h.test_result(
        "ADR-0023 has fourth amendment heading (2026-10-16, #315 escalation)",
        # #334 added the "fourth" heading. The pre-existing "third" heading and the
        # bare "Amendment (2026-10-16" prefix both survive a revert, so match the
        # fourth heading as a whole line.
        re.search(r"^## Amendment \(2026-10-16, fourth\)", adr23, re.MULTILINE) is not None,
        "missing fourth amendment heading"
    )
    h.test_result(
        "ADR-0004 has implementer escalation amendment",
        "implementer escalate-once" in adr04 or "Amendment: implementer escalate-once" in adr04,
        "missing amendment"
    )

    print()
    print("[Section 9] CLAUDE.md mentions escalation")
    h.test_result(
        "CLAUDE.md mentions dormant escalation and Retry with sonnet",
        "dormant" in claude_md and "Retry with sonnet" in claude_md,
        "missing dormant or Retry with sonnet"
    )

    print()
    print("[Section 10] expert-flow.md Hard stop includes escalation")
    # Find the Hard stop section using "### Hard stop" which appears in the command doc
    hard_stop_start = flow_cmd.find("### Hard stop")
    if hard_stop_start != -1:
        next_heading = flow_cmd.find("\n### ", hard_stop_start + 1)
        hard_stop_end = next_heading if next_heading != -1 else len(flow_cmd)
        hard_stop_section = flow_cmd[hard_stop_start:hard_stop_end]
    else:
        hard_stop_section = ""
    h.test_result(
        "Hard stop section mentions implementer-shortfall",
        "implementer-shortfall" in hard_stop_section,
        "missing implementer-shortfall mention"
    )
    h.test_result(
        "Hard stop section mentions Retry with sonnet option",
        "Retry with sonnet" in hard_stop_section,
        "missing Retry with sonnet fourth option"
    )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
