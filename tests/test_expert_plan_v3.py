#!/usr/bin/env python3
"""
Test suite for structural requirements of expert-plan-v3 command and related prompts.

Covers:
1. Command existence and non-empty content
2. Frontmatter: model: sonnet (main thread only)
3. Effort flags: only --effort 2 and --effort 3 supported; error message for 1/4/5
4. Stage list: all required stages with proper pairing (gather-context, select-experts,
   expert-contributions, contrarian, checkpoint, synthesize-plan,
   audit-plan [conditional], repair-plan [conditional], present)
5. No router/digest/pod/swarm subagents (v2-specific)
6. Synthesize and Consistency-check merged into Step 6 single dispatch with Opus
7. Audit conditionally dispatched as Opus subagent (effort 3 or recorded escalation)
8. FINAL_PLAN_PATH includes ${INVOCATION_ID}, not bare ${SLUG}
9. Exit path guards: exit paths emit appropriate telemetry (interrupted or failure)
10. FINAL_PLAN_PATH includes invocation ID
11. Prompt file existence: plan-synthesize-and-check.md, plan-audit.md, others
12. No literal $0 in command doc shell snippets
13. No echo "$VAR" | pattern in command doc shell snippets

Run with: python3 tests/test_expert_plan_v3.py
"""

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT

SCRIPT_CHECK_STAGE = REPO_ROOT / "scripts" / "check_stage_pairing.py"


def main():
    h = Harness("EXPERT-PLAN-V3 STRUCTURAL TEST SUITE")

    # Test 1: File existence for commands/expert-plan-v3.md
    command_file = REPO_ROOT / "commands" / "expert-plan-v3.md"
    command_exists = command_file.is_file()
    h.test_result(
        "commands/expert-plan-v3.md exists as a regular file",
        command_exists,
        str(command_file) if not command_exists else "",
    )

    command_nonempty = False
    command_content = ""
    if command_exists:
        command_content = command_file.read_text()
        command_nonempty = len(command_content.strip()) > 0
    h.test_result(
        "commands/expert-plan-v3.md is non-empty",
        command_nonempty,
        "file is empty" if not command_nonempty else "",
    )

    # Test 2: Frontmatter model is sonnet (not opus)
    if command_exists:
        # Extract frontmatter
        frontmatter_match = re.match(r'^---\n(.*?)\n---', command_content, re.DOTALL)
        frontmatter = frontmatter_match.group(1) if frontmatter_match else ""

        has_model_sonnet = re.search(r'model:\s*sonnet', frontmatter)
        h.test_result(
            "commands/expert-plan-v3.md frontmatter specifies model: sonnet",
            bool(has_model_sonnet),
            "frontmatter does not specify model: sonnet" if not has_model_sonnet else "",
        )

        # Verify it's not set to opus at command level
        has_model_opus = re.search(r'model:\s*opus', frontmatter)
        h.test_result(
            "commands/expert-plan-v3.md frontmatter is NOT model: opus",
            not has_model_opus,
            "frontmatter incorrectly specifies model: opus" if has_model_opus else "",
        )
    else:
        h.test_result(
            "commands/expert-plan-v3.md frontmatter specifies model: sonnet",
            False,
            "file does not exist",
        )
        h.test_result(
            "commands/expert-plan-v3.md frontmatter is NOT model: opus",
            False,
            "file does not exist",
        )

    # Test 3: Effort flag validation: only --effort 2 and --effort 3 documented
    if command_exists:
        has_effort_2 = re.search(r'--effort\s+2|--effort\s*=\s*2|--effort\s*<[^>]*2', command_content)
        has_effort_3 = re.search(r'--effort\s+3|--effort\s*=\s*3|--effort\s*<[^>]*3', command_content)
        h.test_result(
            "commands/expert-plan-v3.md documents --effort 2",
            bool(has_effort_2),
            "" if has_effort_2 else "no mention of --effort 2",
        )
        h.test_result(
            "commands/expert-plan-v3.md documents --effort 3",
            bool(has_effort_3),
            "" if has_effort_3 else "no mention of --effort 3",
        )

        # Should mention rejecting 1, 4, 5
        rejects_bad_efforts = re.search(
            r'--effort\s+[145]|--effort\s*=\s*[145]',
            command_content
        ) or re.search(
            r'Reject.*--effort|reject.*[145]|only\s+[2,3]|support.*2.*3',
            command_content,
            re.IGNORECASE
        )
        h.test_result(
            "commands/expert-plan-v3.md indicates --effort 1/4/5 are not supported",
            bool(rejects_bad_efforts),
            "" if rejects_bad_efforts else "no mention of rejecting invalid effort levels",
        )
    else:
        h.test_result(
            "commands/expert-plan-v3.md documents --effort 2",
            False,
            "file does not exist",
        )
        h.test_result(
            "commands/expert-plan-v3.md documents --effort 3",
            False,
            "file does not exist",
        )
        h.test_result(
            "commands/expert-plan-v3.md indicates --effort 1/4/5 are not supported",
            False,
            "file does not exist",
        )

    # Test 4: Stage pairing check using subprocess
    if command_exists:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_CHECK_STAGE), str(command_file)],
            capture_output=True,
            text=True,
        )
        stage_pairing_clean = result.returncode == 0
        stage_findings = result.stdout.strip()
        h.test_result(
            "commands/expert-plan-v3.md has no orphaned or unmatched stages",
            stage_pairing_clean,
            stage_findings if not stage_pairing_clean else "",
        )

        # Extract stage names
        stage_begin_pattern = r'run-metrics\.py.*stage-begin.*--stage\s+(\S+)'
        stage_begins = set(re.findall(stage_begin_pattern, command_content))

        # Required stages (gather-context through checkpoint are mandatory)
        # Note: synthesize-plan now includes both synthesis and consistency-check (merged in Step 6)
        required_stages = {
            "gather-context",
            "select-experts",
            "expert-contributions",
            "contrarian",
            "checkpoint",
            "synthesize-plan",
            "present",
        }

        # Conditional stages (audit-plan, repair-plan)
        conditional_stages = {"audit-plan", "repair-plan"}

        # All stages should be either required or conditional
        all_expected = required_stages | conditional_stages

        # Check that all required stages are present
        required_present = required_stages.issubset(stage_begins)
        missing = required_stages - stage_begins
        h.test_result(
            "commands/expert-plan-v3.md contains all required stage names",
            required_present,
            f"missing: {', '.join(sorted(missing))}" if missing else "",
        )

        # Check that any extra stages (besides conditional) are noted
        extra = stage_begins - all_expected
        h.test_result(
            "commands/expert-plan-v3.md contains only expected stages",
            len(extra) == 0,
            f"unexpected: {', '.join(sorted(extra))}" if extra else "",
        )

        # Check that audit-plan and repair-plan are documented as conditional if present
        if "audit-plan" in stage_begins or "repair-plan" in stage_begins:
            conditional_documented = re.search(
                r'conditional|effort\s*3',
                command_content,
                re.IGNORECASE
            )
            h.test_result(
                "commands/expert-plan-v3.md documents conditional stages (audit-plan, repair-plan)",
                bool(conditional_documented),
                "" if conditional_documented else "no documentation of conditional nature",
            )
        else:
            h.test_result(
                "commands/expert-plan-v3.md documents conditional stages (audit-plan, repair-plan)",
                True,
                "conditional stages not present (may be acceptable if effort 3 logic is conditional)",
            )
    else:
        h.test_result(
            "commands/expert-plan-v3.md has no orphaned or unmatched stages",
            False,
            "file does not exist",
        )
        h.test_result(
            "commands/expert-plan-v3.md contains all required stage names",
            False,
            "file does not exist",
        )
        h.test_result(
            "commands/expert-plan-v3.md contains only expected stages",
            False,
            "file does not exist",
        )
        h.test_result(
            "commands/expert-plan-v3.md documents conditional stages (audit-plan, repair-plan)",
            False,
            "file does not exist",
        )

    # Test 5: No router, digest, pod, or swarm subagents (v2-specific, should not appear in v3)
    if command_exists:
        v2_subagents = [
            "plan-router",
            "plan-digest",
            "plan-pod",
            "plan-swarm-scout",
            "plan-swarm-merge",
        ]

        has_v2_patterns = False
        found_v2 = []
        for agent in v2_subagents:
            if agent in command_content:
                has_v2_patterns = True
                found_v2.append(agent)

        h.test_result(
            "commands/expert-plan-v3.md does NOT mention v2 subagents (router/digest/pod/swarm)",
            not has_v2_patterns,
            f"found: {', '.join(found_v2)}" if found_v2 else "",
        )
    else:
        h.test_result(
            "commands/expert-plan-v3.md does NOT mention v2 subagents (router/digest/pod/swarm)",
            False,
            "file does not exist",
        )

    # Test 6: Synthesize and Consistency-check merged into single Step 6 dispatch with model: opus
    if command_exists:
        # Check for the merged prompt file reference
        has_synthesize_and_check_prompt = "plan-synthesize-and-check" in command_content

        # Look for model: opus mention with Step 6 (within bounded context around the prompt reference)
        # Find the location of the merged prompt reference and check nearby for model: opus
        synthesize_pattern = r'Step\s+6.*model:\s*opus|model:\s*opus.*Step\s+6'
        has_opus_for_dispatch = re.search(
            synthesize_pattern,
            command_content,
            re.DOTALL | re.IGNORECASE
        )

        h.test_result(
            "commands/expert-plan-v3.md references plan-synthesize-and-check.md",
            has_synthesize_and_check_prompt,
            "" if has_synthesize_and_check_prompt else "no reference to plan-synthesize-and-check.md",
        )

        h.test_result(
            "commands/expert-plan-v3.md documents Step 6 (merged Synthesize+Check) dispatched with model: opus",
            bool(has_opus_for_dispatch),
            "" if has_opus_for_dispatch else "no explicit model: opus for Step 6 dispatch",
        )
    else:
        h.test_result(
            "commands/expert-plan-v3.md references plan-synthesize-and-check.md",
            False,
            "file does not exist",
        )
        h.test_result(
            "commands/expert-plan-v3.md documents Step 6 (merged Synthesize+Check) dispatched with model: opus",
            False,
            "file does not exist",
        )

    # Test 8: Audit step conditionally dispatched for effort 3
    if command_exists:
        has_audit_prompt = "plan-audit" in command_content
        h.test_result(
            "commands/expert-plan-v3.md references plan-audit.md",
            has_audit_prompt,
            "" if has_audit_prompt else "no reference to plan-audit.md",
        )

        # Check effort 3 or conditional language
        has_audit_conditional = re.search(
            r'effort\s+3|conditional.*audit|audit.*conditional',
            command_content,
            re.IGNORECASE
        )
        h.test_result(
            "commands/expert-plan-v3.md documents audit as conditional (effort 3)",
            bool(has_audit_conditional),
            "" if has_audit_conditional else "no documentation of audit being conditional on effort 3",
        )

        # Check that the audit-plan stage-begin is properly gated (either on EFFORT -eq 3
        # or on audit-escalation or other conditions that indicate audit should run)
        # Find stage-begin --stage audit-plan and verify it's within an if block
        has_audit_conditional_gate = re.search(
            r'if\s+\[.*\].*stage-begin\s+--stage\s+audit-plan',
            command_content,
            re.DOTALL
        ) or re.search(
            r'if\s+\[\s*"\$EFFORT"\s+-eq\s+3',
            command_content
        )
        h.test_result(
            "commands/expert-plan-v3.md's audit-plan stage is conditionally gated",
            bool(has_audit_conditional_gate),
            "" if has_audit_conditional_gate else "audit-plan stage-begin is not properly gated",
        )
    else:
        h.test_result(
            "commands/expert-plan-v3.md references plan-audit.md",
            False,
            "file does not exist",
        )
        h.test_result(
            "commands/expert-plan-v3.md documents audit as conditional (effort 3)",
            False,
            "file does not exist",
        )
        h.test_result(
            "commands/expert-plan-v3.md's audit-plan stage gate uses -eq 3",
            False,
            "file does not exist",
        )

    # Test 9: FINAL_PLAN_PATH includes ${INVOCATION_ID}
    if command_exists:
        has_invocation_id = (
            "INVOCATION_ID" in command_content and
            "FINAL_PLAN_PATH" in command_content
        )

        # Check that the final path includes invocation ID
        invocation_in_path = re.search(
            r'FINAL_PLAN_PATH.*\$\{?INVOCATION_ID\}?.*\.md|'
            r'cp.*\$\{?INVOCATION_ID\}?\}?.*FINAL_PLAN_PATH',
            command_content
        )

        h.test_result(
            "commands/expert-plan-v3.md defines FINAL_PLAN_PATH with INVOCATION_ID",
            bool(has_invocation_id and invocation_in_path),
            "" if (has_invocation_id and invocation_in_path) else "FINAL_PLAN_PATH does not include INVOCATION_ID",
        )

        # Ensure final output is not just ${SLUG}.md
        has_bare_slug_path = re.search(
            r'FINAL_PLAN_PATH.*\$\{?SLUG\}?\.md(?!\w)',
            command_content
        )
        h.test_result(
            "commands/expert-plan-v3.md does NOT use bare ${SLUG}.md for FINAL_PLAN_PATH",
            not bool(has_bare_slug_path),
            "FINAL_PLAN_PATH uses bare ${SLUG}.md" if has_bare_slug_path else "",
        )
    else:
        h.test_result(
            "commands/expert-plan-v3.md defines FINAL_PLAN_PATH with INVOCATION_ID",
            False,
            "file does not exist",
        )
        h.test_result(
            "commands/expert-plan-v3.md does NOT use bare ${SLUG}.md for FINAL_PLAN_PATH",
            False,
            "file does not exist",
        )

    # Test 10: Exit paths document --outcome interrupted double-emit
    if command_exists:
        # Look for stage-end --outcome interrupted patterns
        has_interrupted_exit = re.search(
            r'stage-end.*--outcome\s+interrupted|--outcome\s+interrupted.*stage-end',
            command_content
        )

        # Look for command-end --outcome interrupted
        has_interrupted_command_end = re.search(
            r'command-end.*--outcome\s+interrupted|--outcome\s+interrupted.*command-end',
            command_content
        )

        h.test_result(
            "commands/expert-plan-v3.md documents exit path with stage-end --outcome interrupted",
            bool(has_interrupted_exit),
            "" if has_interrupted_exit else "no stage-end --outcome interrupted documented",
        )

        h.test_result(
            "commands/expert-plan-v3.md documents exit path with command-end --outcome interrupted",
            bool(has_interrupted_command_end),
            "" if has_interrupted_command_end else "no command-end --outcome interrupted documented",
        )
    else:
        h.test_result(
            "commands/expert-plan-v3.md documents exit path with stage-end --outcome interrupted",
            False,
            "file does not exist",
        )
        h.test_result(
            "commands/expert-plan-v3.md documents exit path with command-end --outcome interrupted",
            False,
            "file does not exist",
        )

    # Test 11: Prompt files exist and are non-empty
    prompt_files = {
        "plan-synthesize-and-check.md": REPO_ROOT / "prompts" / "plan-synthesize-and-check.md",
        "plan-audit.md": REPO_ROOT / "prompts" / "plan-audit.md",
        "plan-contribution-contract.md": REPO_ROOT / "prompts" / "plan-contribution-contract.md",
    }

    for name, path in prompt_files.items():
        exists = path.is_file()
        is_nonempty = exists and len(path.read_text().strip()) > 0
        h.test_result(
            f"prompts/{name} exists and is non-empty",
            is_nonempty,
            "" if is_nonempty else ("file does not exist" if not exists else "file is empty"),
        )

    # Test 12: No literal $0 in command doc (covered by other test, but document it)
    if command_exists:
        has_literal_zero = re.search(r'[^$]\$0|^\$0', command_content, re.MULTILINE)
        h.test_result(
            "commands/expert-plan-v3.md contains no literal $0 (checked by test_command_doc_shell_conventions.py)",
            not bool(has_literal_zero),
            "" if not has_literal_zero else "found literal $0",
        )
    else:
        h.test_result(
            "commands/expert-plan-v3.md contains no literal $0 (checked by test_command_doc_shell_conventions.py)",
            False,
            "file does not exist",
        )

    # Test 13: No echo "$VAR" | pattern (dangerous in zsh)
    if command_exists:
        # Look for echo "$VAR" | pattern (dangerous in zsh)
        has_dangerous_echo = re.search(
            r'echo\s+"[^"]*\$\w+[^"]*"\s*\|',
            command_content
        )
        h.test_result(
            "commands/expert-plan-v3.md does not use echo \"$VAR\" | pattern",
            not bool(has_dangerous_echo),
            "" if not has_dangerous_echo else "found dangerous echo \"$VAR\" | pattern",
        )
    else:
        h.test_result(
            "commands/expert-plan-v3.md does not use echo \"$VAR\" | pattern",
            False,
            "file does not exist",
        )

    # ============================================================================
    # SPEC-BLIND TESTS: Round 2 (derived from triage action plan spec items)
    # ============================================================================

    # SPEC ITEM 1: Exit-path telemetry
    # Every exit path must emit appropriate telemetry before returning (no silent
    # early-return that skips the telemetry pattern). The interrupted-path checks are
    # already covered by Test 10 above; only the distinct multi-exit-path count check
    # is added here.
    if command_exists:
        # Count command-end occurrences — each exit path should have one
        command_end_count = len(re.findall(r'command-end.*--command\s+expert-plan-v3', command_content))

        h.test_result(
            "Spec 1a: Exit-path telemetry — command-end appears multiple times (all exit paths)",
            command_end_count >= 2,
            f"found {command_end_count} command-end calls (expected >= 2)" if command_end_count < 2 else "",
        )
    else:
        h.test_result(
            "Spec 1a: Exit-path telemetry — command-end appears multiple times (all exit paths)",
            False,
            "file does not exist",
        )

    # SPEC ITEM 2: Effort-2 audit gate must not be hardcoded
    # The decision of whether Step 7 runs must be driven by EFFORT flag value or escalation state,
    # not hardcoded to always-skip or always-run
    if command_exists:
        # Check that audit-plan stage-begin is inside an if statement checking EFFORT or escalation
        has_conditional_audit_begin = re.search(
            r'if\s+\[\s*"\$EFFORT"\s+-eq\s+3\s*\]|'
            r'if\s+\[\s*-f\s+".*audit-escalation',
            command_content
        )

        # Verify both branches are present: effort 3 runs audit, effort 2 may escalate
        has_effort_3_branch = re.search(
            r'\[\s*"\$EFFORT"\s+-eq\s+3\s*\]',
            command_content
        )
        has_escalation_branch = re.search(
            r'elif\s+\[\s*-f\s+".*audit-escalation',
            command_content
        )

        h.test_result(
            "Spec 2a: Effort-2 audit gate — audit-plan stage-begin is conditional on EFFORT or escalation",
            bool(has_conditional_audit_begin),
            "" if has_conditional_audit_begin else "audit-plan not gated by EFFORT or escalation check",
        )

        h.test_result(
            "Spec 2b: Effort-2 audit gate — branch for EFFORT -eq 3 exists",
            bool(has_effort_3_branch),
            "" if has_effort_3_branch else "no EFFORT -eq 3 branch",
        )

        h.test_result(
            "Spec 2c: Effort-2 audit gate — elif branch for escalation exists",
            bool(has_escalation_branch),
            "" if has_escalation_branch else "no elif escalation branch",
        )
    else:
        h.test_result(
            "Spec 2a: Effort-2 audit gate — audit-plan stage-begin is conditional on EFFORT or escalation",
            False,
            "file does not exist",
        )
        h.test_result(
            "Spec 2b: Effort-2 audit gate — branch for EFFORT -eq 3 exists",
            False,
            "file does not exist",
        )
        h.test_result(
            "Spec 2c: Effort-2 audit gate — elif branch for escalation exists",
            False,
            "file does not exist",
        )

    # SPEC ITEM 3: Slug and FINAL_PLAN_PATH ordering
    # SLUG must be computed BEFORE FINAL_PLAN_PATH is constructed from it
    if command_exists:
        # Find line numbers where SLUG is set and where FINAL_PLAN_PATH is set
        slug_assignments = [(i, line) for i, line in enumerate(command_content.split('\n'), 1)
                           if re.search(r'SLUG\s*=', line) and 'FINAL_PLAN_PATH' not in line]
        final_path_assignments = [(i, line) for i, line in enumerate(command_content.split('\n'), 1)
                                 if re.search(r'FINAL_PLAN_PATH\s*=', line)]

        # Check that SLUG is assigned before FINAL_PLAN_PATH (using line numbers)
        slug_before_final = False
        if slug_assignments and final_path_assignments:
            last_slug_line = max([i for i, _ in slug_assignments])
            first_final_line = min([i for i, _ in final_path_assignments])
            slug_before_final = last_slug_line < first_final_line

        # Also verify FINAL_PLAN_PATH uses ${SLUG} in its definition
        uses_slug_in_path = re.search(
            r'FINAL_PLAN_PATH\s*=.*\$\{?SLUG\}?',
            command_content
        )

        h.test_result(
            "Spec 3a: Slug ordering — SLUG is assigned before FINAL_PLAN_PATH is constructed",
            slug_before_final or (not slug_assignments or not final_path_assignments),
            "" if (slug_before_final or not slug_assignments or not final_path_assignments) else "SLUG assigned after FINAL_PLAN_PATH",
        )

        h.test_result(
            "Spec 3b: Slug ordering — FINAL_PLAN_PATH uses ${SLUG} in its definition",
            bool(uses_slug_in_path),
            "" if uses_slug_in_path else "FINAL_PLAN_PATH does not reference SLUG",
        )
    else:
        h.test_result(
            "Spec 3a: Slug ordering — SLUG is assigned before FINAL_PLAN_PATH is constructed",
            False,
            "file does not exist",
        )
        h.test_result(
            "Spec 3b: Slug ordering — FINAL_PLAN_PATH uses ${SLUG} in its definition",
            False,
            "file does not exist",
        )

    # SPEC ITEM 4: Flag parsing correctness
    # --effort and --models must use single while-loop with explicit error branches for no-value case
    if command_exists:
        # Check for single while loop for flag parsing
        has_single_while_loop = len(re.findall(r'while\s+\[\s*\$#\s*-gt\s+0\s*\]', command_content)) == 1

        # Check for explicit error handling for --effort with no value
        has_effort_no_value_error = re.search(
            r'--effort\)\s*shift.*if\s+\[\s*\$#.*\].*ERROR.*--effort.*value',
            command_content,
            re.DOTALL
        )

        # Check for explicit error handling for --models with no value
        has_models_no_value_error = re.search(
            r'--models\)\s*shift.*if\s+\[\s*\$#.*\].*ERROR.*--models.*value',
            command_content,
            re.DOTALL
        )

        # Verify case-style (using case statement, not if-chains)
        has_case_statement = re.search(r'case\s+"\$1"\s+in', command_content)

        h.test_result(
            "Spec 4a: Flag parsing — single while loop for argument processing",
            has_single_while_loop,
            "incorrect number of while loops" if not has_single_while_loop else "",
        )

        h.test_result(
            "Spec 4b: Flag parsing — explicit error for --effort with no value",
            bool(has_effort_no_value_error),
            "" if has_effort_no_value_error else "no explicit error check for --effort without value",
        )

        h.test_result(
            "Spec 4c: Flag parsing — explicit error for --models with no value",
            bool(has_models_no_value_error),
            "" if has_models_no_value_error else "no explicit error check for --models without value",
        )

        h.test_result(
            "Spec 4d: Flag parsing — uses case statement (not if-chain)",
            bool(has_case_statement),
            "" if has_case_statement else "flag parsing uses if-chain instead of case",
        )
    else:
        h.test_result(
            "Spec 4a: Flag parsing — single while loop for argument processing",
            False,
            "file does not exist",
        )
        h.test_result(
            "Spec 4b: Flag parsing — explicit error for --effort with no value",
            False,
            "file does not exist",
        )
        h.test_result(
            "Spec 4c: Flag parsing — explicit error for --models with no value",
            False,
            "file does not exist",
        )
        h.test_result(
            "Spec 4d: Flag parsing — uses case statement (not if-chain)",
            False,
            "file does not exist",
        )

    # SPEC ITEM 5: Steps 6&7 merged structure
    # Step 6 = Synthesize + self-check (single dispatch), Step 7 = Audit (separate)
    # Verify: plan-synthesize-and-check.md exists, old files don't exist, not referenced
    if command_exists:
        # Check old files do NOT exist
        plan_synthesize_path = REPO_ROOT / "prompts" / "plan-synthesize.md"
        plan_consistency_check_path = REPO_ROOT / "prompts" / "plan-consistency-check.md"

        plan_synthesize_exists = plan_synthesize_path.is_file()
        plan_consistency_check_exists = plan_consistency_check_path.is_file()

        # Check old files are NOT referenced in command, ADR, or agents
        adr_0020_path = REPO_ROOT / "docs" / "adr" / "0020-expert-plan-v3-focused-panel.md"
        expert_reviewer_agent_path = REPO_ROOT / "agents" / "expert-reviewer.md"
        adr_0020_content = adr_0020_path.read_text() if adr_0020_path.is_file() else ""
        expert_reviewer_agent_content = expert_reviewer_agent_path.read_text() if expert_reviewer_agent_path.is_file() else ""
        has_old_reference = any(
            "plan-synthesize.md" in text or "plan-consistency-check.md" in text
            for text in (command_content, adr_0020_content, expert_reviewer_agent_content)
        )

        # Check that Step 6 dispatch mentions both Synthesize and Consistency Check
        step6_section = re.search(
            r'###\s+Step\s+6.*?###\s+Step',
            command_content,
            re.DOTALL
        )
        step6_merged = False
        if step6_section:
            step6_text = step6_section.group(0)
            step6_merged = (
                re.search(r'Synthesize.*Consistency|Consistency.*Synthesize', step6_text, re.IGNORECASE) and
                'single' in step6_text.lower()
            )

        # Check references to plan-audit.md
        has_plan_audit = "plan-audit" in command_content

        h.test_result(
            "Spec 5a: Step 6&7 structure — plan-synthesize.md does NOT exist",
            not plan_synthesize_exists,
            "file exists (should be removed)" if plan_synthesize_exists else "",
        )

        h.test_result(
            "Spec 5b: Step 6&7 structure — plan-consistency-check.md does NOT exist",
            not plan_consistency_check_exists,
            "file exists (should be removed)" if plan_consistency_check_exists else "",
        )

        h.test_result(
            "Spec 5c: Step 6&7 structure — no references to old plan-*.md files in command/ADR/agents",
            not has_old_reference,
            "old files referenced" if has_old_reference else "",
        )

        h.test_result(
            "Spec 5d: Step 6&7 structure — Step 6 section describes merged Synthesize+Consistency dispatch",
            step6_merged,
            "" if step6_merged else "Step 6 not described as merged single dispatch",
        )

        h.test_result(
            "Spec 5e: Step 6&7 structure — plan-audit.md is referenced for Step 7",
            has_plan_audit,
            "" if has_plan_audit else "no reference to plan-audit.md",
        )
    else:
        h.test_result(
            "Spec 5a: Step 6&7 structure — plan-synthesize.md does NOT exist",
            False,
            "file does not exist",
        )
        h.test_result(
            "Spec 5b: Step 6&7 structure — plan-consistency-check.md does NOT exist",
            False,
            "file does not exist",
        )
        h.test_result(
            "Spec 5c: Step 6&7 structure — no references to old plan-*.md files in command/ADR/agents",
            False,
            "file does not exist",
        )
        h.test_result(
            "Spec 5d: Step 6&7 structure — Step 6 section describes merged Synthesize+Consistency dispatch",
            False,
            "file does not exist",
        )
        h.test_result(
            "Spec 5e: Step 6&7 structure — plan-audit.md is referenced for Step 7",
            False,
            "file does not exist",
        )

    # SPEC ITEM 6: Overview/model-policy/checkpoint-files table matching merged step numbering
    # Step 6 should be "Synthesize & Consistency Check", Step 7 should be "Audit"
    if command_exists:
        # Check model-policy section
        model_policy_section = re.search(
            r'##\s+Model Policy.*?(?=##\s+|\Z)',
            command_content,
            re.DOTALL
        )
        model_policy_text = model_policy_section.group(0) if model_policy_section else ""

        has_step6_in_model_policy = re.search(
            r'Step\s+6.*Synthesize.*Consistency|Step\s+6.*Consistency.*Synthesis',
            model_policy_text,
            re.IGNORECASE
        )
        has_step7_in_model_policy = re.search(
            r'Step\s+7.*Audit|Step\s+7.*independent',
            model_policy_text,
            re.IGNORECASE
        )

        # Check checkpoint-files table
        checkpoint_section = re.search(
            r'##\s+Checkpoint Files.*?\n\|',
            command_content,
            re.DOTALL
        )
        checkpoint_text = checkpoint_section.group(0) if checkpoint_section else ""

        # Extract table lines about Step 6 and Step 7
        has_step6_in_checkpoint = re.search(
            r'\|\s*`plan\.md`.*?Step 6|Step 6.*`plan\.md`',
            command_content,
            re.IGNORECASE
        )
        has_step7_in_checkpoint = re.search(
            r'\|\s*`audit\.md`.*?Step 7|Step 7.*`audit\.md`',
            command_content,
            re.IGNORECASE
        )

        # Check "All Stage Names" section mentions correct numbering
        stages_section = re.search(
            r'##\s+All Stage Names.*?(?=---|\Z)',
            command_content,
            re.DOTALL
        )
        stages_text = stages_section.group(0) if stages_section else ""

        has_synthesize_plan_stage = re.search(
            r'synthesize-plan.*Step 6|Step 6.*synthesize-plan',
            stages_text,
            re.IGNORECASE
        )
        has_audit_plan_stage = re.search(
            r'audit-plan.*Step 7|Step 7.*audit-plan',
            stages_text,
            re.IGNORECASE
        )

        h.test_result(
            "Spec 6a: Overview/numbering — Model Policy section documents Step 6 as Synthesize & Consistency",
            bool(has_step6_in_model_policy),
            "" if has_step6_in_model_policy else "Step 6 not documented in Model Policy",
        )

        h.test_result(
            "Spec 6b: Overview/numbering — Model Policy section documents Step 7 as Audit",
            bool(has_step7_in_model_policy),
            "" if has_step7_in_model_policy else "Step 7 not documented in Model Policy",
        )

        h.test_result(
            "Spec 6c: Overview/numbering — Checkpoint Files table documents plan.md in Step 6",
            bool(has_step6_in_checkpoint),
            "" if has_step6_in_checkpoint else "Step 6 plan.md not in checkpoint table",
        )

        h.test_result(
            "Spec 6d: Overview/numbering — Checkpoint Files table documents audit.md in Step 7",
            bool(has_step7_in_checkpoint),
            "" if has_step7_in_checkpoint else "Step 7 audit.md not in checkpoint table",
        )

        h.test_result(
            "Spec 6e: Overview/numbering — All Stage Names section documents synthesize-plan as Step 6",
            bool(has_synthesize_plan_stage),
            "" if has_synthesize_plan_stage else "synthesize-plan not in stage names for Step 6",
        )

        h.test_result(
            "Spec 6f: Overview/numbering — All Stage Names section documents audit-plan as Step 7",
            bool(has_audit_plan_stage),
            "" if has_audit_plan_stage else "audit-plan not in stage names for Step 7",
        )
    else:
        for i in range(6):
            h.test_result(
                f"Spec 6{chr(97+i)}: Overview/numbering — (checked)",
                False,
                "file does not exist",
            )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
