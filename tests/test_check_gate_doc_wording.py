#!/usr/bin/env python3
"""
Wording contract for the implement/fix check gate (#338).

Three shipped surfaces must tell a user or a step agent how the gate behaves:
- prompts/project.yaml.template documents checkOptOut and its scope (implement/fix only;
  verify and ship still need a check).
- commands/expert-flow.md quotes the exact not-configured message the resolver emits, taken at
  runtime from scripts/workflow/checks.py so the doc cannot drift from the code.
- commands/implement-with-haiku.md's Final check names exactly one wait primitive for the
  check run (run_in_background under the join-barrier rule) and never tells the agent to poll.

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
IMPLEMENT_WITH_HAIKU = REPO_ROOT / "commands" / "implement-with-haiku.md"

FINAL_CHECK_HEADING = "## Final check ("
FINAL_CHECK_END = "## Report-file handoff check"


def read(path):
    """Return a file's text, or '' if missing."""
    try:
        return path.read_text()
    except OSError:
        return ""


def final_check_section(text):
    """Return the Final check section of implement-with-haiku.md, or '' if not found."""
    start = text.find(FINAL_CHECK_HEADING)
    if start == -1:
        return ""
    end = text.find(FINAL_CHECK_END, start)
    if end == -1:
        return text[start:]
    return text[start:end]


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
    for token in ("checkOptOut", "implement/fix", "verify and ship", "opted-out"):
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
    print("[Section 3] commands/implement-with-haiku.md Final check names one wait primitive (#338)")

    implement = read(IMPLEMENT_WITH_HAIKU)
    test_result(
        "implement-with-haiku.md exists and is readable",
        implement != "",
        f"path={IMPLEMENT_WITH_HAIKU}",
    )
    final_check = final_check_section(implement)
    test_result(
        "implement-with-haiku.md has a Final check section",
        final_check != "",
        f"heading={FINAL_CHECK_HEADING!r}",
    )
    test_result(
        "Final check launches the check run with run_in_background",
        "run_in_background" in final_check,
        "missing 'run_in_background' in Final check section",
    )
    test_result(
        "Final check cites the join-barrier wait rule",
        "join-barrier-pattern.md" in final_check,
        "missing 'join-barrier-pattern.md' in Final check section",
    )
    test_result(
        "Final check states the never-poll rule",
        "never poll" in final_check,
        "missing 'never poll' in Final check section",
    )
    for stale in ("Poll the background task", "never polled", "is polled"):
        test_result(
            f"Final check no longer tells the agent to poll ({stale!r} absent)",
            stale not in final_check,
            f"found stale poll wording {stale!r} in Final check section",
        )

    print()
    print("[Section 4] Other docs that tell an agent to wait for a background check (#338)")

    expert_flow = read(EXPERT_FLOW) if "EXPERT_FLOW" in globals() else ""
    test_result(
        "expert-flow.md generic check step points at the barrier wait, not polling",
        "join-barrier-pattern.md" in expert_flow and "run_in_background` and poll" not in expert_flow,
        "expert-flow.md still tells the agent to poll, or lacks the barrier pointer",
    )
    test_result(
        "implement-with-haiku.md receipt instructions require one contiguous header block",
        "outside the header block" in implement,
        "missing the header-block rule for the receipt",
    )

    print()
    h.summarize_and_exit()
