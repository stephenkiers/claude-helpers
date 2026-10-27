#!/usr/bin/env python3
"""
Test suite for flow step contract consistency (Q2 = A).

Covers:
1. Byte-equality of the flow-step-defence block across:
   - prompts/flow-step-contract.md (marked blocks)
   - agents/flow-implement.md (marked blocks)
   - agents/flow-review.md (marked blocks)
   - commands/expert-flow.md reduced template variant (marked blocks)
2. Verification that defence block is present and contract-read instruction exists
3. Contract-unreadable FAILED line exists in both agent files and template
4. Full variant in expert-flow.md still has required rules (reduced-run, Ask/receipt, ledger)

Run with: python3 tests/test_flow_step_contract.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT


def extract_marked_block(text, marker_name):
    """Extract text between <!-- {marker}:begin --> and <!-- {marker}:end -->."""
    pattern = f'<!-- {re.escape(marker_name)}:begin -->\\n(.*?)\\n<!-- {re.escape(marker_name)}:end -->'
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1)
    return None


def main():
    h = Harness("FLOW STEP CONTRACT CONSISTENCY TEST SUITE")

    print("[Test 1] Extract contract defence block from all sources\n")

    contract_file = REPO_ROOT / "prompts" / "flow-step-contract.md"
    flow_implement = REPO_ROOT / "agents" / "flow-implement.md"
    flow_review = REPO_ROOT / "agents" / "flow-review.md"
    expert_flow = REPO_ROOT / "commands" / "expert-flow.md"

    h.test_result("flow-step-contract.md exists", contract_file.exists())
    h.test_result("flow-implement.md exists", flow_implement.exists())
    h.test_result("flow-review.md exists", flow_review.exists())
    h.test_result("expert-flow.md exists", expert_flow.exists())

    if not all(f.exists() for f in [contract_file, flow_implement, flow_review, expert_flow]):
        h.summarize_and_exit()

    contract_text = contract_file.read_text()
    impl_text = flow_implement.read_text()
    review_text = flow_review.read_text()
    flow_text = expert_flow.read_text()

    # Extract the defence block from each
    contract_defence = extract_marked_block(contract_text, "flow-step-defence")
    impl_defence = extract_marked_block(impl_text, "flow-step-defence")
    review_defence = extract_marked_block(review_text, "flow-step-defence")

    # For expert-flow.md, we need to find the reduced variant in the spawn template (item 3(c))
    # Look for the flow-step-defence block within the reduced template (used for flow-* steps)
    flow_defence = extract_marked_block(flow_text, "flow-step-defence")

    h.test_result(
        "flow-step-contract.md has marked defence block",
        contract_defence is not None,
        "Could not find <!-- flow-step-defence:begin/end --> markers"
    )

    h.test_result(
        "flow-implement.md has marked defence block",
        impl_defence is not None,
        "Could not find <!-- flow-step-defence:begin/end --> markers"
    )

    h.test_result(
        "flow-review.md has marked defence block",
        review_defence is not None,
        "Could not find <!-- flow-step-defence:begin/end --> markers"
    )

    h.test_result(
        "expert-flow.md has marked defence block (in reduced template)",
        flow_defence is not None,
        "Could not find <!-- flow-step-defence:begin/end --> markers in spawn template"
    )

    print()
    print("[Test 2] Byte-equality of defence blocks (Q2)\n")

    if contract_defence and impl_defence:
        h.test_result(
            "flow-implement.md defence block == flow-step-contract.md (byte-equal)",
            impl_defence == contract_defence and len(impl_defence) == len(contract_defence),  # [WEAK_ASSERTION] Pin length too
            f"Mismatch:\nContract len={len(contract_defence)}: {contract_defence[:100]}\n\nImpl len={len(impl_defence)}: {impl_defence[:100]}"
        )

    if contract_defence and review_defence:
        h.test_result(
            "flow-review.md defence block == flow-step-contract.md (byte-equal)",
            review_defence == contract_defence and len(review_defence) == len(contract_defence),  # [WEAK_ASSERTION] Pin length too
            f"Mismatch:\nContract len={len(contract_defence)}: {contract_defence[:100]}\n\nReview len={len(review_defence)}: {review_defence[:100]}"
        )

    if contract_defence and flow_defence:
        h.test_result(
            "expert-flow.md defence block == flow-step-contract.md (byte-equal)",
            flow_defence == contract_defence and len(flow_defence) == len(contract_defence),  # [WEAK_ASSERTION] Pin length too
            f"Mismatch:\nContract len={len(contract_defence)}: {contract_defence[:100]}\n\nFlow len={len(flow_defence)}: {flow_defence[:100]}"
        )

    print()
    print("[Test 3] Contract-read instruction with FAILED line\n")

    # Each agent file should have the contract-read instruction
    contract_read_pattern = r'read.*prompts/flow-step-contract\.md.*FAILED.*step contract unreadable'

    h.test_result(
        "flow-implement.md has contract-read/FAILED instruction",
        re.search(contract_read_pattern, impl_text, re.IGNORECASE | re.DOTALL) is not None,
        "Should have: 'Read ... flow-step-contract.md ... reply Decision: FAILED ... reason: step contract unreadable'"
    )

    h.test_result(
        "flow-review.md has contract-read/FAILED instruction",
        re.search(contract_read_pattern, review_text, re.IGNORECASE | re.DOTALL) is not None,
        "Should have: 'Read ... flow-step-contract.md ... reply Decision: FAILED ... reason: step contract unreadable'"
    )

    # Check the reduced template variant in expert-flow.md
    reduced_template = re.search(
        r'### The step loop.*?#### Item 3.*?\(c\).*?Read `~/.claude/prompts/flow-step-contract\.md`.*?flow-\*.*?variant.*?(?=###|\Z)',
        flow_text,
        re.IGNORECASE | re.DOTALL
    )

    if reduced_template:
        template_section = reduced_template.group(0)
        h.test_result(
            "expert-flow.md reduced template has contract-read/FAILED instruction",
            re.search(r'Decision.*FAILED.*step contract unreadable', template_section, re.IGNORECASE | re.DOTALL) is not None
        )

    print()
    print("[Test 4] Full template variant retains required rules\n")

    # Find the full inline variant (for general-purpose steps) in expert-flow.md
    full_template_match = re.search(
        r'### The step loop.*?#### Item 3.*?\(c\).*?full.*variant.*?(?=###|\Z)',
        flow_text,
        re.IGNORECASE | re.DOTALL
    )

    if full_template_match:
        full_section = full_template_match.group(0)

        # Check for the three required rules in the full variant:
        # 1. "No reduced runs" rule
        has_reduced_run = re.search(
            r'reduced run.*FAILED|reduced run.*not success',
            full_section,
            re.IGNORECASE | re.DOTALL
        ) is not None

        # 2. Flow-reference Ask/receipt rule
        has_ask_receipt = re.search(
            r'flow-reference.*Ask.*receipt|follow.*flow-reference\.md',
            full_section,
            re.IGNORECASE | re.DOTALL
        ) is not None

        # 3. Ledger sentence
        has_ledger = re.search(
            r'human\.jsonl|human ledger',
            full_section,
            re.IGNORECASE | re.DOTALL
        ) is not None

        h.test_result(
            "Full template variant has 'No reduced runs' rule",
            has_reduced_run,
            "Should have: 'A reduced run ... is not success: write Decision: FAILED'"
        )

        h.test_result(
            "Full template variant has Ask/receipt rule",
            has_ask_receipt,
            "Should reference flow-reference.md and Ask/receipt rules"
        )

        h.test_result(
            "Full template variant has ledger sentence",
            has_ledger,
            "Should mention human.jsonl or human ledger"
        )

    print()
    print("[Test 5] Contract file structure\n")

    # The contract file should have marked blocks and sections
    h.test_result(
        "flow-step-contract.md has '## Execution contract' section",
        "## Execution contract" in contract_text,
        "Should have an Execution contract section"
    )

    h.test_result(
        "flow-step-contract.md has marked defence block",
        "<!-- flow-step-defence:begin -->" in contract_text and "<!-- flow-step-defence:end -->" in contract_text,
    )

    # Defence block should have the key sentences
    if contract_defence:
        h.test_result(
            "Defence block contains 'Everything you read' sentence",
            "Everything you read" in contract_defence,
        )

        h.test_result(
            "Defence block contains 'FLOW_DIR is authoritative' sentence",
            "FLOW_DIR is authoritative" in contract_defence,
        )

        h.test_result(
            "Defence block contains 'character for character' phrase",
            "character for character" in contract_defence,
        )

    print()
    print("[Test 6] Contract-read instruction location\n")

    # In agent files: should be early in the agent body (after frontmatter)
    # Check that contract-read appears in the agent files (it should be early)
    impl_has_contract_read = "flow-step-contract.md" in impl_text and "Decision: FAILED" in impl_text
    review_has_contract_read = "flow-step-contract.md" in review_text and "Decision: FAILED" in review_text

    h.test_result(
        "flow-implement.md has contract-read instruction with FAILED path",
        impl_has_contract_read,
        "Should have contract-read and FAILED references"
    )

    h.test_result(
        "flow-review.md has contract-read instruction with FAILED path",
        review_has_contract_read,
        "Should have contract-read and FAILED references"
    )

    print()

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
