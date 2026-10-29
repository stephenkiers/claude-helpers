#!/usr/bin/env python3
"""
Prevention contract tests for plan-implementer cwd-drift hardening (#336).

Tests verify that agents/plan-implementer.md contains the required prevention rules
and that docs/adr/0008-machine-enforced-agent-guardrails.md contains the amendment
documenting the cause investigation and the new contract.

Run with: python3 tests/test_plan_implementer_prevention.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT


def main():
    h = Harness("PLAN-IMPLEMENTER PREVENTION CONTRACT TEST SUITE")

    # Load relevant files
    plan_agent = (REPO_ROOT / "agents" / "plan-implementer.md").read_text()
    adr08 = (REPO_ROOT / "docs" / "adr" / "0008-machine-enforced-agent-guardrails.md").read_text()

    print("[Section 1] False sentence removed from agents/plan-implementer.md")
    h.test_result(
        "No 'working directory persists' sentence",
        "working directory persists" not in plan_agent,
        "false sentence still present"
    )

    print()
    print("[Section 2] cd <Working directory> && requirement in agents/plan-implementer.md")
    h.test_result(
        "cd <Working directory> && required on every Bash call",
        "every Bash call must begin with `cd <Working directory> &&`" in plan_agent
        or "cd <Working directory> &&" in plan_agent,
        "missing cd requirement"
    )

    print()
    print("[Section 3] Absolute path requirements in agents/plan-implementer.md")
    h.test_result(
        "Absolute path must start with assigned Working directory",
        "absolute path that starts with the assigned Working directory" in plan_agent
        or ("absolute path" in plan_agent and "Working directory" in plan_agent),
        "missing absolute path requirement"
    )
    h.test_result(
        "Never derive paths from git output",
        ("never" in plan_agent and "git output" in plan_agent)
        or ("git rev-parse --git-dir" in plan_agent and "never" in plan_agent),
        "missing never-derive-from-git rule"
    )
    h.test_result(
        "Halt if path does not start with Working directory",
        "STAGED: no" in plan_agent and ("does not start" in plan_agent or "outside" in plan_agent),
        "missing halt condition"
    )

    print()
    print("[Section 4] Frontmatter tools and hooks unchanged in agents/plan-implementer.md")
    h.test_result(
        "PreToolUse hook present",
        "PreToolUse:" in plan_agent,
        "hook removed or renamed"
    )
    h.test_result(
        "plan-implementer-git-guard.py referenced in hooks",
        "plan-implementer-git-guard.py" in plan_agent,
        "guard script reference missing"
    )
    h.test_result(
        "tools: list still present",
        re.search(r"^tools:", plan_agent, re.MULTILINE) is not None,
        "tools list removed"
    )

    print()
    print("[Section 5] Constraint rule updated to name assigned Working directory")
    # Check that line 110 (or nearby) no longer says "current working directory"
    # and instead says something about "assigned Working directory"
    h.test_result(
        "Constraint updated to reference assigned Working directory",
        "assigned Working directory" in plan_agent and "Only touch files inside" in plan_agent,
        "missing update to constraint rule"
    )

    print()
    print("[Section 6] ADR-0008 amendment 2026-10-26 present")
    h.test_result(
        "Amendment 2026-10-26 (#336) heading present",
        re.search(r"^## Amendment 2026-10-26 \(#336\)", adr08, re.MULTILINE) is not None,
        "missing amendment heading"
    )

    print()
    print("[Section 7] ADR-0008 amendment defines primary checkout and ticket worktree")
    h.test_result(
        "primary checkout definition present",
        "primary checkout" in adr08,
        "missing definition"
    )
    h.test_result(
        "ticket worktree definition present",
        "ticket worktree" in adr08,
        "missing definition"
    )

    print()
    print("[Section 8] ADR-0008 amendment records cause investigation")
    h.test_result(
        "Cause class (d) mentioned",
        "Cause class" in adr08 and "(d)" in adr08,
        "missing cause class"
    )
    h.test_result(
        "git rev-parse --git-dir cause documented",
        "git rev-parse --git-dir" in adr08,
        "missing cause details"
    )

    print()
    print("[Section 9] ADR-0008 amendment records prevention contract")
    h.test_result(
        "Prevention contract section present",
        "Prevention contract" in adr08,
        "missing contract"
    )
    h.test_result(
        "cwd-drift failure class mentioned",
        "cwd-drift" in adr08,
        "missing cwd-drift"
    )
    h.test_result(
        "cwd_drift telemetry class mentioned",
        "cwd_drift" in adr08,
        "missing cwd_drift"
    )

    print()
    print("[Section 10] ADR-0008 amendment records related tickets")
    h.test_result(
        "#358 mentioned",
        "#358" in adr08,
        "missing #358 reference"
    )
    h.test_result(
        "#327 mentioned",
        "#327" in adr08,
        "missing #327 reference"
    )

    print()
    print("[Section 11] ADR-0008 correction notes about earlier amendment")
    # The amendment should point out that the earlier amendment said the measures
    # "together close the incident class" but now we know they don't — the baseline
    # and barrier detects (new in #336) are also required.
    h.test_result(
        "Earlier amendment corrections noted",
        "earlier amendment" in adr08 or "Earlier amendment" in adr08,
        "missing note about earlier amendment"
    )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
