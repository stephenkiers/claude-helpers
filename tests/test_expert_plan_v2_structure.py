#!/usr/bin/env python3
"""
Test suite for structural requirements of expert-plan-v2 command and plan-contribution-contract prompt.

Covers:
1. File existence: commands/expert-plan-v2.md and prompts/plan-contribution-contract.md exist
   and are non-empty.
2. Stage list match: commands/expert-plan-v2.md contains all 8 required stage-begin and
   stage-end calls (gather-context, route-experts, expert-contributions, contrarian,
   digest-questions, checkpoint, synthesize-plan, alignment-pass).
3. Contract file fields: prompts/plan-contribution-contract.md contains all required
   open-question sub-fields and top-level format keys.
4. Directory-boundary constraint: commands/expert-plan-v2.md uses ~/.claude/plan-sessions/
   for checkpoints, not repo-relative paths.

Run with: python3 tests/test_expert_plan_v2_structure.py
"""

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT

SCRIPT_CHECK_STAGE = REPO_ROOT / "scripts" / "check_stage_pairing.py"


def main():
    h = Harness("EXPERT-PLAN-V2 STRUCTURAL TEST SUITE")

    # Test 1: File existence for commands/expert-plan-v2.md
    command_file = REPO_ROOT / "commands" / "expert-plan-v2.md"
    command_exists = command_file.is_file()
    h.test_result(
        "commands/expert-plan-v2.md exists as a regular file",
        command_exists,
        str(command_file) if not command_exists else "",
    )

    command_nonempty = False
    command_content = ""
    if command_exists:
        command_content = command_file.read_text()
        command_nonempty = len(command_content.strip()) > 0
    h.test_result(
        "commands/expert-plan-v2.md is non-empty",
        command_nonempty,
        "file is empty" if not command_nonempty else "",
    )

    # Test 2: File existence for prompts/plan-contribution-contract.md
    contract_file = REPO_ROOT / "prompts" / "plan-contribution-contract.md"
    contract_exists = contract_file.is_file()
    h.test_result(
        "prompts/plan-contribution-contract.md exists as a regular file",
        contract_exists,
        str(contract_file) if not contract_exists else "",
    )

    contract_nonempty = False
    contract_content = ""
    if contract_exists:
        contract_content = contract_file.read_text()
        contract_nonempty = len(contract_content.strip()) > 0
    h.test_result(
        "prompts/plan-contribution-contract.md is non-empty",
        contract_nonempty,
        "file is empty" if not contract_nonempty else "",
    )

    # Test 3: Stage list match using subprocess call to check_stage_pairing.py
    if command_exists:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_CHECK_STAGE), str(command_file)],
            capture_output=True,
            text=True,
        )
        stage_pairing_clean = result.returncode == 0
        stage_findings = result.stdout.strip()
        h.test_result(
            "commands/expert-plan-v2.md has no orphaned or unmatched stages",
            stage_pairing_clean,
            stage_findings if not stage_pairing_clean else "",
        )

        # Extract and verify exactly 8 expected stages using regex pattern
        # Pattern mirrors check_stage_pairing.py's extract_stage_names
        stage_begin_pattern = r'run-metrics\.py.*stage-begin.*--stage\s+(\S+)'
        stage_begins = set(re.findall(stage_begin_pattern, command_content))

        expected_stages = {
            "gather-context",
            "route-experts",
            "expert-contributions",
            "contrarian",
            "digest-questions",
            "checkpoint",
            "synthesize-plan",
            "alignment-pass",
            "reconcile-alignment",
            "post-alignment-checkpoint",
        }

        stages_match = stage_begins == expected_stages
        missing = expected_stages - stage_begins
        extra = stage_begins - expected_stages
        detail = ""
        if missing:
            detail += f"missing: {', '.join(sorted(missing))}; "
        if extra:
            detail += f"extra: {', '.join(sorted(extra))}"
        h.test_result(
            "commands/expert-plan-v2.md contains all 10 required stage names",
            stages_match,
            detail.rstrip("; ") if detail else "",
        )
    else:
        h.test_result(
            "commands/expert-plan-v2.md has no orphaned or unmatched stages",
            False,
            "file does not exist",
        )
        h.test_result(
            "commands/expert-plan-v2.md contains all 10 required stage names",
            False,
            "file does not exist",
        )

    # Test 4: Literal $0 check is already covered by test_command_doc_shell_conventions.py
    # which globs all commands/*.md files. No need to duplicate.
    print("Note: literal $0 check is already covered by test_command_doc_shell_conventions.py")

    # Test 5: Contract file has all four open-question sub-fields
    # Anchor check to the schema-definition section (### Open Question Examples) to avoid
    # false positives from examples in intro text.
    if contract_exists:
        schema_section_start = contract_content.find("#### Open Question Examples")
        if schema_section_start == -1:
            # Fallback to looking for the format examples section
            schema_section_start = contract_content.find("#### Format")

        # Extract just the schema definition section for this check
        check_text = contract_content[schema_section_start:] if schema_section_start != -1 else contract_content

        required_subfields = ["_Why it matters_", "_Recommendation_", "_Confounders_", "_Source_"]
        subfields_found = [sf in check_text for sf in required_subfields]
        all_subfields_present = all(subfields_found)
        missing_subfields = [
            sf for sf, found in zip(required_subfields, subfields_found) if not found
        ]
        h.test_result(
            "prompts/plan-contribution-contract.md contains all four open-question sub-fields",
            all_subfields_present,
            f"missing: {', '.join(missing_subfields)}" if missing_subfields else "",
        )
    else:
        h.test_result(
            "prompts/plan-contribution-contract.md contains all four open-question sub-fields",
            False,
            "file does not exist",
        )

    # Test 6: Contract file names the five top-level format keys
    if contract_exists:
        format_keys = [
            "**Domain**",
            "**Requirements**",
            "**Risks**",
            "**Recommended Approach**",
            "**Open Questions**",
        ]
        keys_found = [key in contract_content for key in format_keys]
        all_keys_present = all(keys_found)
        missing_keys = [key for key, found in zip(format_keys, keys_found) if not found]
        h.test_result(
            "prompts/plan-contribution-contract.md contains all five top-level format keys",
            all_keys_present,
            f"missing: {', '.join(missing_keys)}" if missing_keys else "",
        )
    else:
        h.test_result(
            "prompts/plan-contribution-contract.md contains all five top-level format keys",
            False,
            "file does not exist",
        )

    # Test 7: Directory-boundary constraint (plan-sessions in checkpoints, not repo-relative paths)
    if command_exists:
        # Look for evidence of plan-sessions directory usage
        has_plan_sessions = "plan-sessions" in command_content
        h.test_result(
            "commands/expert-plan-v2.md references plan-sessions directory",
            has_plan_sessions,
            "plan-sessions not found in command doc" if not has_plan_sessions else "",
        )

        # Check that there's no obvious repo-relative checkpoint paths like ./checkpoints or similar
        # A soft check: look for patterns like Write/mkdir with paths that look like they'd be in the repo
        # Specifically, check that checkpoint writes don't use relative paths
        repo_relative_patterns = [
            r'Write\s+["\'](?!~)[^"\']*checkpoint',
            r'mkdir\s+["\'](?!~)[^"\']*checkpoint',
            r'Write\s+["\']\./',
            r'mkdir\s+["\']\./',
        ]
        found_repo_paths = []
        for pattern in repo_relative_patterns:
            if re.search(pattern, command_content):
                found_repo_paths.append(pattern)

        no_repo_relative = len(found_repo_paths) == 0
        detail = (
            f"found patterns: {', '.join(found_repo_paths)}"
            if found_repo_paths
            else ""
        )
        h.test_result(
            "commands/expert-plan-v2.md does not use repo-relative paths for checkpoints",
            no_repo_relative,
            detail,
        )
    else:
        h.test_result(
            "commands/expert-plan-v2.md references plan-sessions directory",
            False,
            "file does not exist",
        )
        h.test_result(
            "commands/expert-plan-v2.md does not use repo-relative paths for checkpoints",
            False,
            "file does not exist",
        )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
