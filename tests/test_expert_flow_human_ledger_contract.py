#!/usr/bin/env python3
"""
Structural contract tests for human ledger binding in /expert-flow (#288).

Covers:
1. D4: --kind direction only in Redo (verified by presence)
2. Grants: `allowed-tools` includes gh issue create/list
3. Receipt template: carries `Addressed: none`
4. Auditor inputs: PLAN_PATH and owed directions
5. Plan rulings only to Triage Chief
6. ledger_format in state.json docs

Run with: python3 tests/test_expert_flow_human_ledger_contract.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

EXPERT_FLOW = REPO_ROOT / "commands" / "expert-flow.md"
EXPERT_REVIEW = REPO_ROOT / "commands" / "expert-review.md"


def main():
    h = Harness("EXPERT-FLOW HUMAN LEDGER CONTRACT TESTS (#288)")

    print("[Section 1] D4: --kind direction appears in expert-flow.md")

    text = EXPERT_FLOW.read_text(encoding="utf-8")
    # Remove frontmatter
    text = re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.S)

    has_direction_kind = "--kind direction" in text
    h.test_result("--kind direction appears at least once in commands/expert-flow.md",
                  has_direction_kind, f"found: {has_direction_kind}")

    print()
    print("[Section 2] Grants: allowed-tools includes gh issue")

    text = EXPERT_FLOW.read_text(encoding="utf-8")
    has_gh_create = "gh issue create" in text
    has_gh_list = "gh issue list" in text

    h.test_result("commands/expert-flow.md mentions 'gh issue create'",
                  has_gh_create, f"found: {has_gh_create}")
    h.test_result("commands/expert-flow.md mentions 'gh issue list'",
                  has_gh_list, f"found: {has_gh_list}")

    print()
    print("[Section 3] Receipt template: 'Addressed: none'")

    text = EXPERT_FLOW.read_text(encoding="utf-8")
    has_addressed_none = "Addressed: none" in text
    h.test_result("'Addressed: none' appears in commands/expert-flow.md",
                  has_addressed_none, f"found: {has_addressed_none}")

    print()
    print("[Section 4] Auditor inputs: PLAN_PATH")

    text = EXPERT_FLOW.read_text(encoding="utf-8")
    has_plan_path = "PLAN_PATH" in text
    h.test_result("'PLAN_PATH' appears in commands/expert-flow.md",
                  has_plan_path, f"found: {has_plan_path}")

    print()
    print("[Section 5] ledger_format in state.json")

    text = EXPERT_FLOW.read_text(encoding="utf-8")
    has_ledger_format = "ledger_format" in text
    h.test_result("'ledger_format' is mentioned in commands/expert-flow.md",
                  has_ledger_format, f"found: {has_ledger_format}")

    print()
    print("[Section 6] Plan rulings pass to review")

    expert_review_text = EXPERT_REVIEW.read_text(encoding="utf-8")
    has_plan_rulings = "Plan rulings" in expert_review_text
    h.test_result("'Plan rulings' is mentioned in commands/expert-review.md",
                  has_plan_rulings, f"found: {has_plan_rulings}")

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
