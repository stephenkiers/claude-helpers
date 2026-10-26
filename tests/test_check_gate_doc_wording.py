#!/usr/bin/env python3
"""
Wording contract for the implement/fix check gate (#338).

Two shipped surfaces must tell a user how to opt out of the implement/fix gate and what the
not-configured failure means:
- prompts/project.yaml.template documents checkOptOut and its scope (implement/fix only;
  verify and ship still need a check).
- commands/expert-flow.md quotes the exact not-configured message the resolver emits, taken at
  runtime from scripts/workflow/checks.py so the doc cannot drift from the code.

Run with: python3 tests/test_check_gate_doc_wording.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from workflow.checks import NO_CHECK_CONFIGURED_MESSAGE

TEMPLATE = REPO_ROOT / "prompts" / "project.yaml.template"
EXPERT_FLOW = REPO_ROOT / "commands" / "expert-flow.md"


def read(path):
    """Return a file's text, or '' if missing."""
    try:
        return path.read_text()
    except OSError:
        return ""


if __name__ == "__main__":
    h = Harness("CHECK GATE DOC WORDING TEST SUITE")
    test_result = h.test_result

    print("[Section 1] prompts/project.yaml.template documents checkOptOut (#338)")

    template = read(TEMPLATE)
    test_result(
        "project.yaml.template exists and is readable",
        template != "",
        f"path={TEMPLATE}",
    )
    for token in ("checkOptOut", "implement/fix", "verify", "ship", "opted-out"):
        test_result(
            f"project.yaml.template mentions {token!r}",
            token in template,
            f"missing {token!r} in {TEMPLATE.name}",
        )

    print()
    print("[Section 2] commands/expert-flow.md quotes the resolver's not-configured message (#338)")

    expert_flow = read(EXPERT_FLOW)
    test_result(
        "expert-flow.md exists and is readable",
        expert_flow != "",
        f"path={EXPERT_FLOW}",
    )
    test_result(
        "expert-flow.md contains NO_CHECK_CONFIGURED_MESSAGE from scripts/workflow/checks.py",
        NO_CHECK_CONFIGURED_MESSAGE in expert_flow,
        f"constant={NO_CHECK_CONFIGURED_MESSAGE[:120]!r}",
    )

    print()
    h.summarize_and_exit()
