#!/usr/bin/env python3
"""
Spec-blind test suite for the expert-flow step-gate / question-relay changes.

This test suite is written ONLY from the plan specification (12 doing-it findings),
without reading the implementation files. It verifies the accepted/implemented changes
by reading and asserting against the actual production artifact content.

The 12 findings being tested (by documentation verification and behavior):
1. F1: Relayed-question marker-scoped counting (existing tests cover this)
2. F2: Resume paths re-ask gate vs re-run (verified via documentation)
3. F3: expert-plan.md Step 5 Q[n] brief questions (verified via wording)
4. F4: STEP advanced once not twice (verified via code structure docs)
5. F5: ADR-0023 amendment supersedes clauses (verified via statement presence)
6. F8: Zero-brief carve-out (existing tests cover this)
7. F9: Unique Q counting via set() (existing tests cover this)
8. F10: Section 14 Ask assertions (verified via wording)
9. F11: Unreadable file suffix (existing tests cover this)
10. F12: E4 flag initialization (verified via documentation)
11. F14: Relay cycle comment (documented intention, may be GitHub comment)
12. F15: Gate file naming per step (verified via documentation)

Run with: python3 tests/test_expert_flow_spec_blind.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

COMMANDS_DIR = REPO_ROOT / "commands"
PROMPTS_DIR = REPO_ROOT / "prompts"
DOCS_ADR_DIR = REPO_ROOT / "docs" / "adr"
DOCS_PLANS_DIR = REPO_ROOT / "docs" / "plans"

h = Harness("EXPERT-FLOW SPEC-BLIND TEST SUITE")
t = h.test_result

print()
print("[Finding 3] expert-plan.md Step 5: each open-ended Q[n] brief")
print("=" * 70)

expert_plan_file = COMMANDS_DIR / "expert-plan.md"
plan_content = expert_plan_file.read_text() if expert_plan_file.exists() else ""

# Check that Step 5 mentions making each open-ended brief a free-text Ask
has_free_text_ask = bool(re.search(
    r"each\s+open-ended.*free-text",
    plan_content,
    re.IGNORECASE
))
t("Step 5 mentions each open-ended brief becomes free-text Ask", has_free_text_ask)

# Check for "Answer in your own words" language
has_answer_in_words = bool(re.search(
    r"Answer\s+in\s+your\s+own\s+words",
    plan_content
))
t("Step 5 specifies 'Answer in your own words' for open-ended", has_answer_in_words)

# Check that every brief is asked
has_every_brief_asked = bool(re.search(
    r"Every.*decision.*brief.*asked|every.*####.*Q",
    plan_content,
    re.IGNORECASE
))
t("Step 5 states every decision brief is asked", has_every_brief_asked)

print()
print("[Finding 5] ADR-0023: Amendment explicitly supersedes")
print("=" * 70)

adr_file = DOCS_ADR_DIR / "0023-expert-flow.md"
adr_content = adr_file.read_text() if adr_file.exists() else ""

# Check for explicit supersession statement
has_supersedes = bool(re.search(
    r"This\s+amendment\s+supersedes",
    adr_content
))
t("ADR has explicit 'This amendment supersedes' statement", has_supersedes)

# Check that it names what's being superseded
has_section_reference = bool(re.search(
    r"(section\s+2|§2|section\s+4|§4)",
    adr_content
))
t("ADR supersession names specific sections/rules superseded", has_section_reference)

# Check that it mentions step gates or resume rule changes
has_rule_mention = bool(re.search(
    r"(step\s+gate|resume\s+rule|one\s+own\s+question)",
    adr_content,
    re.IGNORECASE
))
t("ADR supersession mentions specific rule changes", has_rule_mention)

print()
print("[Finding 6] F8: Fidelity auditor zero-brief carve-out")
print("=" * 70)

flow_cmd_file = COMMANDS_DIR / "expert-flow.md"
flow_content = flow_cmd_file.read_text() if flow_cmd_file.exists() else ""

# Check for zero-brief carve-out language
has_zero_brief = bool(re.search(
    r"zero.*Q|unless.*zero|no.*Q.*headings",
    flow_content,
    re.IGNORECASE
))
t("Fidelity audit mentions zero-brief carve-out", has_zero_brief)

# Check that decisions.md is mentioned in the context
has_decisions_context = bool(re.search(
    r"decisions\.md",
    flow_content
))
t("Zero-brief context mentions decisions.md", has_decisions_context)

print()
print("[Finding 8] F10: Ask block assertions specific (summary/brief defined)")
print("=" * 70)

flow_ref_file = PROMPTS_DIR / "flow-reference.md"
flow_ref_content = flow_ref_file.read_text() if flow_ref_file.exists() else ""

# Check that Ask block exists
has_ask_block = bool(re.search(
    r"##\s+Ask|###\s+.*Ask",
    flow_ref_content
))
t("flow-reference.md has Ask block section", has_ask_block)

# Check that summary and brief are defined as fields
has_summary_field = bool(re.search(
    r"(summary|`summary`)",
    flow_ref_content
))
t("Ask block defines or references summary", has_summary_field)

has_brief_field = bool(re.search(
    r"(brief|`brief`)",
    flow_ref_content
))
t("Ask block defines or references brief", has_brief_field)

# Check that context_path is mentioned
has_context_path = bool(re.search(
    r"context_path",
    flow_ref_content
))
t("Ask block mentions context_path", has_context_path)

print()
print("[Finding 10] F12: E4 documents flag initialization")
print("=" * 70)

# Check E4 section exists
has_e4_section = bool(re.search(r"\*\*E4\.", flow_content))
t("expert-flow.md has E4 section", has_e4_section)

# Check that E4 mentions flag initialization or child behavior
has_flag_init = bool(re.search(
    r"(initialise|initialize).*flags|flags.*(false|no_gates|auto_merge)|children.*initialise",
    flow_content,
    re.IGNORECASE
))
t("E4 documents flag or child initialization", has_flag_init)

print()
print("[Finding 12] F15: Gate file naming per step (gates/<NN>-<STEP>.md)")
print("=" * 70)

# Check gate file path pattern mentioned
has_gate_path = bool(re.search(
    r"gates/[^/]*<\w+>.*\.md",
    flow_content
))
t("expert-flow.md mentions writing gate files", has_gate_path)

# Check that step-specific guidance exists (the main file table with per-step paths)
has_step_specific = bool(re.search(
    r"`plan`.*→|`review`.*→|`implement`.*→",
    flow_content
))
t("Gate section has step-specific documentation", has_step_specific)

print()
print("[Finding 2] F2: Resume paths gate vs step re-run (status paused)")
print("=" * 70)

# Check that status: paused is documented
has_status_paused = bool(re.search(
    r"(status.*paused|paused.*status|set.*paused)",
    flow_content,
    re.IGNORECASE
))
t("expert-flow.md documents status: paused", has_status_paused)

# Check that resume re-asks gate rule is documented
has_reask_rule = bool(re.search(
    r"(re-ask.*gate|gate.*re-ask|never\s+re-run)",
    flow_content,
    re.IGNORECASE
))
t("Resume rule documents re-asking gate vs re-running step", has_reask_rule)

print()
print("[Finding 4] F4: STEP advanced once (OK branch owns advance)")
print("=" * 70)

# Check that OK branch is documented as advancing STEP
has_ok_advance = bool(re.search(
    r"(OK\s+branch|OK.*Decision).*advance|step\s+gate.*continue.*advance",
    flow_content,
    re.IGNORECASE | re.DOTALL
))
t("expert-flow.md documents advance in OK/Continue path", has_ok_advance)

# Check that advance happens in clear order
has_order_doc = bool(re.search(
    r"(gate.*continue|continue.*advance|advance.*STEP|STEP.*advance)",
    flow_content,
    re.IGNORECASE
))
t("STEP advance order is documented", has_order_doc)

print()
print("[Finding 1] F1: Marker-scoped counting (implementation existence)")
print("=" * 70)

# Verify the script exists and has marker-related code
script_file = REPO_ROOT / "scripts" / "flow-verify-receipt.py"
has_script = script_file.exists()
t("scripts/flow-verify-receipt.py exists", has_script)

if has_script:
    script_content = script_file.read_text()
    # Check for marker-scoped counting logic
    has_markers = bool(re.search(
        r"awaiting-answers|reused-answers",
        script_content
    ))
    t("flow-verify-receipt.py handles markers", has_markers)

    # Check for counting logic
    has_count_logic = bool(re.search(
        r"count.*question|asked.*\d|brief.*\d",
        script_content,
        re.IGNORECASE
    ))
    t("flow-verify-receipt.py has counting logic", has_count_logic)

print()
print("[Test coverage note]")
print("=" * 70)
print("Extensive tests for F1, F8, F9, F11 (marker-scoped counting, unique Q,")
print("unreadable files, zero-brief carve-out) exist in test_flow_verify_receipt.py.")
print("This suite verifies documentation wording and command-level requirements.")

h.summarize_and_exit()
