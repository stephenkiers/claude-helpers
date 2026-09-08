#!/usr/bin/env python3
"""
Test suite for Effort 1-2 swarm/pod modes of expert-plan-v2 command.

Covers the new role prompts (plan-swarm-scout.md, plan-swarm-merge.md, plan-pod.md),
their references in commands/expert-plan-v2.md, sentinel strings, reviewer availability,
ADR link fix, and join-barrier-pattern.md pod support.

This test is spec-blind: written from the plan (GitHub issue #151) alone, without
reading round 1's implementation files (commands/expert-plan-v2.md, agents/expert-reviewer.md,
prompts/join-barrier-pattern.md, prompts/plan-swarm-scout.md, prompts/plan-swarm-merge.md,
prompts/plan-pod.md).

Run with: python3 tests/test_expert_plan_v2_swarm_pod.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT


def main():
    h = Harness("EFFORT 1-2 SWARM/POD MODES TEST SUITE")

    # ========== EFFORT 1: SWARM PATH TESTS ==========

    # Test 1: plan-swarm-scout.md exists and is non-empty
    swarm_scout_file = REPO_ROOT / "prompts" / "plan-swarm-scout.md"
    swarm_scout_exists = swarm_scout_file.is_file()
    h.test_result(
        "prompts/plan-swarm-scout.md exists",
        swarm_scout_exists,
        "" if swarm_scout_exists else "file not found",
    )

    swarm_scout_content = ""
    if swarm_scout_exists:
        swarm_scout_content = swarm_scout_file.read_text()
        swarm_scout_nonempty = len(swarm_scout_content.strip()) > 0
        h.test_result(
            "prompts/plan-swarm-scout.md is non-empty",
            swarm_scout_nonempty,
            "" if swarm_scout_nonempty else "file is empty",
        )
    else:
        h.test_result(
            "prompts/plan-swarm-scout.md is non-empty",
            False,
            "file does not exist",
        )

    # Test 2: plan-swarm-merge.md exists and is non-empty
    swarm_merge_file = REPO_ROOT / "prompts" / "plan-swarm-merge.md"
    swarm_merge_exists = swarm_merge_file.is_file()
    h.test_result(
        "prompts/plan-swarm-merge.md exists",
        swarm_merge_exists,
        "" if swarm_merge_exists else "file not found",
    )

    swarm_merge_content = ""
    if swarm_merge_exists:
        swarm_merge_content = swarm_merge_file.read_text()
        swarm_merge_nonempty = len(swarm_merge_content.strip()) > 0
        h.test_result(
            "prompts/plan-swarm-merge.md is non-empty",
            swarm_merge_nonempty,
            "" if swarm_merge_nonempty else "file is empty",
        )
    else:
        h.test_result(
            "prompts/plan-swarm-merge.md is non-empty",
            False,
            "file does not exist",
        )

    # Test 3: plan-swarm-merge.md contains swarm-contribution.md reference
    if swarm_merge_exists:
        has_swarm_contrib_ref = "swarm-contribution.md" in swarm_merge_content
        h.test_result(
            "prompts/plan-swarm-merge.md mentions swarm-contribution.md",
            has_swarm_contrib_ref,
            "" if has_swarm_contrib_ref else "reference not found in file",
        )
    else:
        h.test_result(
            "prompts/plan-swarm-merge.md mentions swarm-contribution.md",
            False,
            "file does not exist",
        )

    # Test 4: plan-swarm-merge.md contains contribution-end sentinel
    if swarm_merge_exists:
        has_sentinel = "<!-- contribution-end -->" in swarm_merge_content
        h.test_result(
            "prompts/plan-swarm-merge.md contains <!-- contribution-end --> sentinel",
            has_sentinel,
            "" if has_sentinel else "sentinel not found in file",
        )
    else:
        h.test_result(
            "prompts/plan-swarm-merge.md contains <!-- contribution-end --> sentinel",
            False,
            "file does not exist",
        )

    # ========== EFFORT 2: POD PATH TESTS ==========

    # Test 5: plan-pod.md exists and is non-empty
    pod_file = REPO_ROOT / "prompts" / "plan-pod.md"
    pod_exists = pod_file.is_file()
    h.test_result(
        "prompts/plan-pod.md exists",
        pod_exists,
        "" if pod_exists else "file not found",
    )

    pod_content = ""
    if pod_exists:
        pod_content = pod_file.read_text()
        pod_nonempty = len(pod_content.strip()) > 0
        h.test_result(
            "prompts/plan-pod.md is non-empty",
            pod_nonempty,
            "" if pod_nonempty else "file is empty",
        )
    else:
        h.test_result(
            "prompts/plan-pod.md is non-empty",
            False,
            "file does not exist",
        )

    # Test 6: plan-pod.md contains pod file references
    if pod_exists:
        # Check for either the pattern {pod-id}-pod.md or literal pod names
        has_pod_file_refs = (
            "{pod-id}-pod.md" in pod_content
            or "domain-requirements-pod.md" in pod_content
            or "contracts-risk-pod.md" in pod_content
        )
        h.test_result(
            "prompts/plan-pod.md mentions pod file pattern ({pod-id}-pod.md or literal names)",
            has_pod_file_refs,
            "" if has_pod_file_refs else "pod file pattern not found",
        )
    else:
        h.test_result(
            "prompts/plan-pod.md mentions pod file pattern ({pod-id}-pod.md or literal names)",
            False,
            "file does not exist",
        )

    # Test 7: plan-pod.md contains pod-end sentinel
    if pod_exists:
        has_pod_sentinel = "<!-- pod-end -->" in pod_content
        h.test_result(
            "prompts/plan-pod.md contains <!-- pod-end --> sentinel",
            has_pod_sentinel,
            "" if has_pod_sentinel else "sentinel not found in file",
        )
    else:
        h.test_result(
            "prompts/plan-pod.md contains <!-- pod-end --> sentinel",
            False,
            "file does not exist",
        )

    # ========== COMMAND DOC REFERENCES TESTS ==========

    # Test 8: commands/expert-plan-v2.md exists
    command_file = REPO_ROOT / "commands" / "expert-plan-v2.md"
    command_exists = command_file.is_file()
    h.test_result(
        "commands/expert-plan-v2.md exists",
        command_exists,
        "" if command_exists else "file not found",
    )

    command_content = ""
    if command_exists:
        command_content = command_file.read_text()

    # Test 9: commands/expert-plan-v2.md mentions plan-swarm-scout.md
    if command_exists:
        has_swarm_scout = "plan-swarm-scout.md" in command_content
        h.test_result(
            "commands/expert-plan-v2.md references plan-swarm-scout.md",
            has_swarm_scout,
            "" if has_swarm_scout else "reference not found",
        )
    else:
        h.test_result(
            "commands/expert-plan-v2.md references plan-swarm-scout.md",
            False,
            "file does not exist",
        )

    # Test 10: commands/expert-plan-v2.md mentions plan-swarm-merge.md
    if command_exists:
        has_swarm_merge = "plan-swarm-merge.md" in command_content
        h.test_result(
            "commands/expert-plan-v2.md references plan-swarm-merge.md",
            has_swarm_merge,
            "" if has_swarm_merge else "reference not found",
        )
    else:
        h.test_result(
            "commands/expert-plan-v2.md references plan-swarm-merge.md",
            False,
            "file does not exist",
        )

    # Test 11: commands/expert-plan-v2.md mentions plan-pod.md
    if command_exists:
        has_pod_ref = "plan-pod.md" in command_content
        h.test_result(
            "commands/expert-plan-v2.md references plan-pod.md",
            has_pod_ref,
            "" if has_pod_ref else "reference not found",
        )
    else:
        h.test_result(
            "commands/expert-plan-v2.md references plan-pod.md",
            False,
            "file does not exist",
        )

    # Test 12: commands/expert-plan-v2.md mentions swarm-contribution.md
    if command_exists:
        has_swarm_contrib = "swarm-contribution.md" in command_content
        h.test_result(
            "commands/expert-plan-v2.md mentions swarm-contribution.md",
            has_swarm_contrib,
            "" if has_swarm_contrib else "reference not found",
        )
    else:
        h.test_result(
            "commands/expert-plan-v2.md mentions swarm-contribution.md",
            False,
            "file does not exist",
        )

    # Test 13: commands/expert-plan-v2.md mentions pod file pattern
    if command_exists:
        has_pod_files = (
            "domain-requirements-pod.md" in command_content
            or "contracts-risk-pod.md" in command_content
            or "{pod-id}-pod.md" in command_content
        )
        h.test_result(
            "commands/expert-plan-v2.md mentions pod file names/pattern",
            has_pod_files,
            "" if has_pod_files else "pod file references not found",
        )
    else:
        h.test_result(
            "commands/expert-plan-v2.md mentions pod file names/pattern",
            False,
            "file does not exist",
        )

    # Test 14: commands/expert-plan-v2.md mentions pod-end sentinel
    if command_exists:
        has_pod_end_sentinel = "<!-- pod-end -->" in command_content
        h.test_result(
            "commands/expert-plan-v2.md mentions <!-- pod-end --> sentinel",
            has_pod_end_sentinel,
            "" if has_pod_end_sentinel else "sentinel not found",
        )
    else:
        h.test_result(
            "commands/expert-plan-v2.md mentions <!-- pod-end --> sentinel",
            False,
            "file does not exist",
        )

    # Test 15: commands/expert-plan-v2.md mentions contribution-end sentinel (for swarm)
    if command_exists:
        has_contrib_end_sentinel = "<!-- contribution-end -->" in command_content
        h.test_result(
            "commands/expert-plan-v2.md mentions <!-- contribution-end --> sentinel (for swarm)",
            has_contrib_end_sentinel,
            "" if has_contrib_end_sentinel else "sentinel not found",
        )
    else:
        h.test_result(
            "commands/expert-plan-v2.md mentions <!-- contribution-end --> sentinel (for swarm)",
            False,
            "file does not exist",
        )

    # ========== REVIEWER AVAILABILITY TESTS ==========

    # Test 16: Effort 1 swarm scouts exist - north-star-nick.yaml
    nick_file = REPO_ROOT / "reviewers" / "north-star-nick.yaml"
    h.test_result(
        "reviewers/north-star-nick.yaml exists (Effort 1 swarm scout)",
        nick_file.is_file(),
        "" if nick_file.is_file() else "file not found",
    )

    # Test 17: Effort 1 swarm scouts exist - tara-typesafe.yaml
    tara_file = REPO_ROOT / "reviewers" / "tara-typesafe.yaml"
    h.test_result(
        "reviewers/tara-typesafe.yaml exists (Effort 1 swarm scout)",
        tara_file.is_file(),
        "" if tara_file.is_file() else "file not found",
    )

    # Test 18: Effort 1 swarm scouts exist - security-sage.yaml
    sage_file = REPO_ROOT / "reviewers" / "security-sage.yaml"
    h.test_result(
        "reviewers/security-sage.yaml exists (Effort 1 swarm scout)",
        sage_file.is_file(),
        "" if sage_file.is_file() else "file not found",
    )

    # Test 19: Effort 2 pod 1 (domain-requirements) - north-star-nick.yaml (already tested above)
    # Test 20: Effort 2 pod 1 (domain-requirements) - business-beth.yaml
    beth_file = REPO_ROOT / "reviewers" / "business-beth.yaml"
    h.test_result(
        "reviewers/business-beth.yaml exists (Effort 2 pod domain-requirements)",
        beth_file.is_file(),
        "" if beth_file.is_file() else "file not found",
    )

    # Test 21: Effort 2 pod 1 (domain-requirements) - eric-evans.yaml
    eric_file = REPO_ROOT / "reviewers" / "eric-evans.yaml"
    h.test_result(
        "reviewers/eric-evans.yaml exists (Effort 2 pod domain-requirements)",
        eric_file.is_file(),
        "" if eric_file.is_file() else "file not found",
    )

    # Test 22: Effort 2 pod 1 (domain-requirements) - data-scientist-dana.yaml
    dana_file = REPO_ROOT / "reviewers" / "data-scientist-dana.yaml"
    h.test_result(
        "reviewers/data-scientist-dana.yaml exists (Effort 2 pod domain-requirements)",
        dana_file.is_file(),
        "" if dana_file.is_file() else "file not found",
    )

    # Test 23: Effort 2 pod 2 (contracts-risk) - tara-typesafe.yaml (already tested above)
    # Test 24: Effort 2 pod 2 (contracts-risk) - security-sage.yaml (already tested above)
    # Test 25: Effort 2 pod 2 (contracts-risk) - sam-system.yaml
    sam_file = REPO_ROOT / "reviewers" / "sam-system.yaml"
    h.test_result(
        "reviewers/sam-system.yaml exists (Effort 2 pod contracts-risk)",
        sam_file.is_file(),
        "" if sam_file.is_file() else "file not found",
    )

    # Test 26: Effort 2 pod 2 (contracts-risk) - fragile-feynman.yaml
    fragile_file = REPO_ROOT / "reviewers" / "fragile-feynman.yaml"
    h.test_result(
        "reviewers/fragile-feynman.yaml exists (Effort 2 pod contracts-risk)",
        fragile_file.is_file(),
        "" if fragile_file.is_file() else "file not found",
    )

    # ========== ADR LINK FIX TESTS ==========

    # Test 27: Old broken ADR link does not exist anywhere
    broken_adr_link = "0012-effort-ladder-and-model-cost-routing.md"
    broken_found = False
    try:
        result = (
            str(REPO_ROOT)
            .split("\n")
        )  # dummy to avoid subprocess for now, will use grep
        # Use grep to search for broken link in all markdown files
        import subprocess

        grep_result = subprocess.run(
            ["grep", "-r", broken_adr_link, str(REPO_ROOT), "--include=*.md"],
            capture_output=True,
            text=True,
        )
        broken_found = grep_result.returncode == 0
    except Exception:
        pass

    h.test_result(
        "Old broken ADR link (0012-effort-ladder-and-model-cost-routing.md) not found anywhere",
        not broken_found,
        "" if not broken_found else "broken link still exists in repo",
    )

    # Test 28: New correct ADR link file exists
    new_adr_file = REPO_ROOT / "docs" / "adr" / "0012-effort-ladder-and-pr-mode.md"
    h.test_result(
        "docs/adr/0012-effort-ladder-and-pr-mode.md exists (new correct ADR)",
        new_adr_file.is_file(),
        "" if new_adr_file.is_file() else "file not found",
    )

    # ========== JOIN BARRIER PATTERN TESTS ==========

    # Test 29: prompts/join-barrier-pattern.md mentions pod atomicity
    join_barrier_file = REPO_ROOT / "prompts" / "join-barrier-pattern.md"
    if join_barrier_file.is_file():
        join_barrier_content = join_barrier_file.read_text()
        has_pod_section = "pod" in join_barrier_content.lower() and (
            "Pod Barriers" in join_barrier_content
            or "pod atomic" in join_barrier_content.lower()
        )
        h.test_result(
            "prompts/join-barrier-pattern.md has Pod Barriers section or pod atomicity mention",
            has_pod_section,
            "" if has_pod_section else "pod section/mention not found",
        )

        # Test 30: join-barrier-pattern.md mentions swarm-contribution.md sentinel
        has_swarm_sentinel_ref = (
            "swarm-contribution.md" in join_barrier_content
            and "<!-- contribution-end -->" in join_barrier_content
        )
        h.test_result(
            "prompts/join-barrier-pattern.md mentions swarm-contribution.md with <!-- contribution-end --> sentinel",
            has_swarm_sentinel_ref,
            ""
            if has_swarm_sentinel_ref
            else "swarm file/sentinel pattern not found together",
        )

        # Test 31: join-barrier-pattern.md mentions pod file pattern with pod-end sentinel
        has_pod_sentinel_ref = (
            ("{pod-id}-pod.md" in join_barrier_content or "pod.md" in join_barrier_content)
            and "<!-- pod-end -->" in join_barrier_content
        )
        h.test_result(
            "prompts/join-barrier-pattern.md mentions pod file pattern with <!-- pod-end --> sentinel",
            has_pod_sentinel_ref,
            ""
            if has_pod_sentinel_ref
            else "pod file/sentinel pattern not found together",
        )
    else:
        h.test_result(
            "prompts/join-barrier-pattern.md has Pod Barriers section or pod atomicity mention",
            False,
            "file does not exist",
        )
        h.test_result(
            "prompts/join-barrier-pattern.md mentions swarm-contribution.md with <!-- contribution-end --> sentinel",
            False,
            "file does not exist",
        )
        h.test_result(
            "prompts/join-barrier-pattern.md mentions pod file pattern with <!-- pod-end --> sentinel",
            False,
            "file does not exist",
        )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
