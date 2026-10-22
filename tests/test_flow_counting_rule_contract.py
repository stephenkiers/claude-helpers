#!/usr/bin/env python3
"""
Test suite for the relayed-question counting rule contract.

The counting rule is stated identically in:
(a) scripts/flow-verify-receipt.py (named constants and docstring)
(b) commands/expert-flow.md rule 8
(c) prompts/flow-reference.md Receipt section and fidelity-audit lines in expert-flow.md
(d) docs/adr/0023-expert-flow.md amendment

This test enforces that all four stay in sync and that the attempt-field and disk-counting
semantics are correctly stated.

Run with: python3 tests/test_flow_counting_rule_contract.py
"""

import importlib.util
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT


def load_verifier_constants(verifier_path):
    """Load the flow-verify-receipt.py script and extract named constants."""
    spec = importlib.util.spec_from_file_location("flow_verify_receipt", verifier_path)
    module = importlib.util.module_from_spec(spec)

    # Execute the module (will set module-level constants)
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        return None, str(e)

    # Extract the constants we need
    constants = {
        'REASON_PLAN_INCOMPLETE': getattr(module, 'REASON_PLAN_INCOMPLETE', None),
        'REASON_REVIEW_INCOMPLETE': getattr(module, 'REASON_REVIEW_INCOMPLETE', None),
        'SUFFIX_NO_ANSWERED_FMT': getattr(module, 'SUFFIX_NO_ANSWERED_FMT', None),
        'SUFFIX_FLOW_DIR_UNRESOLVED': getattr(module, 'SUFFIX_FLOW_DIR_UNRESOLVED', None),
        'SUFFIX_UNANSWERED': getattr(module, 'SUFFIX_UNANSWERED', None),
        'SUFFIX_MALFORMED': getattr(module, 'SUFFIX_MALFORMED', None),
        'SUFFIX_OUT_OF_ATTEMPT': getattr(module, 'SUFFIX_OUT_OF_ATTEMPT', None),
        'REASON_PLAN_OLDER_FIRST': getattr(module, 'REASON_PLAN_OLDER_FIRST', None),
        'REASON_PLAN_OLDER_ATTEMPT': getattr(module, 'REASON_PLAN_OLDER_ATTEMPT', None),
        'REASON_REVIEW_OLDER_FIRST': getattr(module, 'REASON_REVIEW_OLDER_FIRST', None),
        'REASON_REVIEW_OLDER_ATTEMPT': getattr(module, 'REASON_REVIEW_OLDER_ATTEMPT', None),
    }

    return constants, None


def main():
    h = Harness("FLOW COUNTING RULE CONTRACT TEST SUITE")

    verifier_path = REPO_ROOT / "scripts" / "flow-verify-receipt.py"
    expert_flow_doc = REPO_ROOT / "commands" / "expert-flow.md"
    flow_reference_doc = REPO_ROOT / "prompts" / "flow-reference.md"
    adr_path = REPO_ROOT / "docs" / "adr" / "0023-expert-flow.md"

    print("[Test (a)] Verifier module constants are defined")
    constants, error = load_verifier_constants(verifier_path)
    if error:
        h.test_result("Verifier module loads without errors", False, error)
    else:
        # Check that all constants are defined
        missing = [k for k, v in constants.items() if v is None]
        if missing:
            h.test_result("All named constants defined", False, f"missing: {', '.join(missing)}")
        else:
            h.test_result("All named constants defined", True)

    if not constants:
        print()
        h.summarize_and_exit()

    print()
    print("[Test (b)] commands/expert-flow.md rule 8 lists all required constants verbatim")

    if not expert_flow_doc.exists():
        h.test_result("commands/expert-flow.md exists", False, f"{expert_flow_doc} not found")
    else:
        expert_flow_text = expert_flow_doc.read_text()

        # Every constant's VALUE (the fixed prefix, for format strings) must appear in the doc.
        # The needles are derived from the loaded constants, so renaming a value in the script
        # without updating the doc fails here (no hard-coded literal to drift alongside it).
        rule8_checks = []
        for name, value in constants.items():
            if value is None:
                continue
            needle = value.split("{", 1)[0]
            rule8_checks.append((value, needle))

        missing_in_rule8 = []
        for const_val, search_text in rule8_checks:
            if not search_text:
                missing_in_rule8.append(f"empty needle for constant {const_val!r}")
            elif search_text not in expert_flow_text:
                missing_in_rule8.append(f"'{search_text}' (from constant value)")

        if missing_in_rule8:
            h.test_result(
                "rule 8 contains all checkpoint/suffix/floor constants",
                False,
                "\n      " + "\n      ".join(missing_in_rule8)
            )
        else:
            h.test_result(
                "rule 8 contains all checkpoint/suffix/floor constants",
                True
            )

        # Check that forbidden phrase is gone
        if "receipt carries no" in expert_flow_text:
            h.test_result(
                "rule 8 no longer contains 'receipt carries no' phrase",
                False,
                "found 'receipt carries no' in expert-flow.md"
            )
        else:
            h.test_result(
                "rule 8 no longer contains 'receipt carries no' phrase",
                True
            )

    print()
    print("[Test (c)] Counting rule stated in fidelity audit lines and flow-reference")

    # The canonical counting-rule sentence must appear in:
    # - expert-flow.md plan fidelity audit (~L966)
    # - expert-flow.md review fidelity audit (~L968)
    # - flow-reference.md Receipt section
    # Each must mention attempt, answers/, and reused-answers within one sentence

    counting_rule_regex = re.compile(
        r"(attempt[^.]*answers/[^.]*reused-answers|"
        r"attempt[^.]*reused-answers[^.]*answers/|"
        r"answers/[^.]*attempt[^.]*reused-answers|"
        r"answers/[^.]*reused-answers[^.]*attempt|"
        r"reused-answers[^.]*attempt[^.]*answers/|"
        r"reused-answers[^.]*answers/[^.]*attempt)",
        re.IGNORECASE | re.DOTALL
    )

    def audit_line(content, step_prefix):
        """The fidelity-audit checklist line for a step: from `<prefix>` to the next checklist line."""
        m = re.search(rf"^{re.escape(step_prefix)}.*?(?=^[a-z, ]+: |^Write |\Z)", content, re.MULTILINE | re.DOTALL)
        return m.group(0) if m else None

    def receipt_section(content):
        """The flow-reference `## Receipt` section, up to the next `## ` heading."""
        m = re.search(r"^## Receipt\b.*?(?=^## |\Z)", content, re.MULTILINE | re.DOTALL)
        return m.group(0) if m else None

    missing_counting_rule = []
    flow_text = expert_flow_doc.read_text() if expert_flow_doc.exists() else ""
    ref_text = flow_reference_doc.read_text() if flow_reference_doc.exists() else ""
    # A missing anchor is a failure, never a silent skip.
    for location, section in (
        ("expert-flow.md plan audit", audit_line(flow_text, "plan: Rule 8")),
        ("expert-flow.md review audit", audit_line(flow_text, "review: Rule 8")),
    ):
        if section is None:
            missing_counting_rule.append(f"{location}: anchor not found")
        elif not counting_rule_regex.search(section):
            missing_counting_rule.append(f"{location}: counting rule not found")
    # flow-reference: the Ask section's attempt text and the counting rule must both be present
    # in the Receipt (Artifact verification / Ask) text, not merely somewhere in the file.
    ask_m = re.search(r"`attempt` is the step prompt's `ATTEMPT=<k>`[^\n]*", ref_text)
    if ask_m is None:
        missing_counting_rule.append("flow-reference.md: attempt definition anchor not found")
    elif "malformed" not in ask_m.group(0):
        missing_counting_rule.append("flow-reference.md: attempt-less file is not described as malformed")
    rec = receipt_section(ref_text)
    if rec is None:
        missing_counting_rule.append("flow-reference.md Receipt section: anchor not found")
    elif not counting_rule_regex.search(rec):
        missing_counting_rule.append("flow-reference.md Receipt section: counting rule not found")

    if missing_counting_rule:
        h.test_result(
            "counting rule (attempt + answers/ + reused-answers) in audit/Receipt sections",
            False,
            "\n      " + "\n      ".join(missing_counting_rule)
        )
    else:
        h.test_result(
            "counting rule (attempt + answers/ + reused-answers) in audit/Receipt sections",
            True
        )

    print()
    print("[Test (d)] flow-reference.md Ask schema contains 'attempt' field")

    if flow_reference_doc.exists():
        flow_ref_text = flow_reference_doc.read_text()
        # Look for the JSON schema block with "attempt"
        if '"attempt"' in flow_ref_text:
            h.test_result("Ask schema JSON block contains 'attempt' field", True)
        else:
            h.test_result("Ask schema JSON block contains 'attempt' field", False)
    else:
        h.test_result("flow-reference.md exists", False)

    print()
    print("[Test (e)] ADR-0023 has the new amendment and marks Decision 4 as superseded")

    if adr_path.exists():
        adr_text = adr_path.read_text()

        # Check for the new amendment heading
        amendment_heading = "## Amendment (2026-10-21):"
        has_heading = amendment_heading in adr_text
        h.test_result(
            "ADR-0023 amendment 2026-10-21 heading present",
            has_heading,
            f"expected '{amendment_heading}' not found" if not has_heading else ""
        )

        # Check that Decision 4 of the 2026-10-14 amendment is marked superseded
        amend_m = re.search(r"^## Amendment \(2026-10-21\):.*?(?=^## Amendment \(|\Z)", adr_text, re.MULTILINE | re.DOTALL)
        amend = amend_m.group(0) if amend_m else ""
        superseded_check = ("supersedes Decision point 4" in amend and "2026-10-14" in amend
                            and "narrows the 2026-10-19" in amend)
        h.test_result(
            "2026-10-14 amendment Decision 4 marked as superseded",
            superseded_check,
            "2026-10-21 amendment must supersede Decision point 4 of 2026-10-14 and narrow the 2026-10-19 wording" if not superseded_check else ""
        )
    else:
        h.test_result("docs/adr/0023-expert-flow.md exists", False)

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
