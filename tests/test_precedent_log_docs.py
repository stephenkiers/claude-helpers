#!/usr/bin/env python3
"""Test suite for precedent-log documentation and placement (#295).

Tests that:
- Call-site ordering in commands/expert-plan.md is correct
- Flow placement is in the right location
- The helper never appears in orchestrator docs
- predictions.jsonl never appears in reader prompts
- ADR-0024 amendment documents the feature
- CLAUDE.md paragraph exists
- precedent-pat.md never-read sentence is present

Run with: python3 tests/test_precedent_log_docs.py
"""

import re
from pathlib import Path

from _test_harness import REPO_ROOT, Harness

COMMANDS_DIR = REPO_ROOT / "commands"
PROMPTS_DIR = REPO_ROOT / "prompts"
AGENTS_DIR = REPO_ROOT / "agents"
DOCS_ADR_DIR = REPO_ROOT / "docs" / "adr"
REVIEWERS_DIR = REPO_ROOT / "reviewers"

if __name__ == "__main__":
    h = Harness("PRECEDENT-LOG DOCUMENTATION TEST SUITE")

    # ========================================================================
    # SECTION A: Non-flow call site ordering in commands/expert-plan.md
    # ========================================================================
    print("\n[Section A] Non-flow call site ordering in commands/expert-plan.md")

    expert_plan_file = COMMANDS_DIR / "expert-plan.md"
    expert_plan_content = expert_plan_file.read_text() if expert_plan_file.exists() else ""

    h.test_result(
        "A1: expert-plan.md exists",
        expert_plan_file.exists(),
        "File not found"
    )

    # Find non-flow content: everything before "The loop ends" marker
    loop_ends_idx = expert_plan_content.find("The loop ends")
    if loop_ends_idx < 0:
        loop_ends_idx = len(expert_plan_content)

    # Non-flow is everything before the loop
    nonflow_text = expert_plan_content[:loop_ends_idx]
    nonflow_section = True if "AskUserQuestion" in nonflow_text and "precedent-log.py" in nonflow_text else None

    if nonflow_section:

        # Check that AskUserQuestion appears
        has_ask = "AskUserQuestion" in nonflow_text
        h.test_result("A2: Non-flow has AskUserQuestion instruction", has_ask, "Not found")

        # Check that precedent: <PRECEDENT_TRACE> append appears
        has_precedent_append = "precedent: <PRECEDENT_TRACE>" in nonflow_text or "precedent: <PRECEDENT_" in nonflow_text
        h.test_result("A3: Non-flow has precedent trace append", has_precedent_append, "Not found")

        # Check that precedent-log.py appears (without flow-answers)
        has_log_py = "precedent-log.py" in nonflow_text and "--answers" in nonflow_text
        h.test_result("A4: Non-flow has precedent-log.py with --answers", has_log_py, "Not found")

        # Check ordering: AskUserQuestion before precedent-log.py
        ask_pos = nonflow_text.find("AskUserQuestion")
        log_pos = nonflow_text.find("precedent-log.py")
        precedent_append_pos = nonflow_text.find("precedent: <PRECEDENT_TRACE>")

        if ask_pos >= 0 and log_pos >= 0:
            h.test_result("A5: AskUserQuestion before precedent-log.py",
                         ask_pos < log_pos,
                         f"ask_pos={ask_pos}, log_pos={log_pos}")

        if precedent_append_pos >= 0 and log_pos >= 0:
            h.test_result("A6: precedent append before precedent-log.py call",
                         precedent_append_pos < log_pos,
                         f"precedent_pos={precedent_append_pos}, log_pos={log_pos}")

        # Check that precedent-log.py does NOT appear in the Precedent block (the numbered steps)
        # The Precedent block typically starts with "1. Validate", "2. Find", etc.
        precedent_block = re.search(
            r"## Precedent Pat[^#]*?(?=^##)",
            expert_plan_content,
            re.MULTILINE | re.DOTALL
        )
        if precedent_block:
            # Check if it contains the numbered steps for precedent generation
            block_text = precedent_block.group(0)
            # The actual Precedent steps should not call precedent-log.py
            if "precedent-log.py" in block_text:
                # Make sure it's not in the numbered steps
                steps_section = re.search(r"(### Step \d+|1\.|2\.|3\.)[^#]*precedent-log", block_text)
                h.test_result("A7: precedent-log.py not in Precedent block numbered steps",
                             not steps_section, "Found in steps")
    else:
        h.test_result("A-all: Non-flow section found", False, "Could not find section")

    # ========================================================================
    # SECTION B: Flow placement in "The loop ends" paragraph
    # ========================================================================
    print("\n[Section B] Flow placement in 'The loop ends' paragraph")

    loop_ends_section = re.search(
        r"\*\*The loop ends\*\*[^#]*?(?=^##|$)",
        expert_plan_content,
        re.MULTILINE | re.DOTALL
    )

    if loop_ends_section:
        loop_text = loop_ends_section.group(0)

        # Check that precedent-log.py appears with --flow-answers
        has_flow_log = "precedent-log.py" in loop_text and "--flow-answers" in loop_text
        h.test_result("B1: Flow has precedent-log.py with --flow-answers", has_flow_log, "Not found")

        # Check that it comes after "Append every round's reading to Checkpoint results"
        checkpoint_append_pos = loop_text.find("Append every round's reading to")
        log_pos = loop_text.find("precedent-log.py")
        if checkpoint_append_pos >= 0 and log_pos >= 0:
            h.test_result("B2: Checkpoint append before precedent-log.py",
                         checkpoint_append_pos < log_pos,
                         f"checkpoint_pos={checkpoint_append_pos}, log_pos={log_pos}")

        # Check that it comes before "proceed to Step 6"
        proceed_pos = loop_text.find("proceed to Step 6")
        if proceed_pos >= 0 and log_pos >= 0:
            h.test_result("B3: precedent-log.py before 'proceed to Step 6'",
                         log_pos < proceed_pos,
                         f"log_pos={log_pos}, proceed_pos={proceed_pos}")
    else:
        h.test_result("B-all: 'The loop ends' section found", False, "Section not found")

    # ========================================================================
    # SECTION B2: Trace source - --trace-file from SESSION_DIR, not PRECEDENT_TRACE
    # ========================================================================
    print("\n[Section B2] Trace source handling")

    if loop_ends_section:
        loop_text = loop_ends_section.group(0)
        # Check for --trace-file from SESSION_DIR in flow
        has_flow_session_trace = "--trace-file \"${SESSION_DIR}" in loop_text or "--trace-file '${SESSION_DIR}" in loop_text
        h.test_result("B2c: Flow uses --trace-file from SESSION_DIR", has_flow_session_trace,
                     "Trace file path not from SESSION_DIR")

        # Check that REPO_KEY is re-derived in the loop or at least used
        # Pattern: gh repo view ... or similar derivation, combined with REPO_KEY
        has_repo_key_derive = bool(re.search(r"(?:gh repo|REPO_KEY|repo-key)", loop_text, re.IGNORECASE))
        h.test_result("B2d: Flow uses REPO_KEY", has_repo_key_derive,
                     "REPO_KEY reference not found in loop")

    # Also check non-flow B2 requirements if nonflow_text is still valid
    if nonflow_section and isinstance(nonflow_text, str):
        # Check for --trace-file "${SESSION_DIR}/precedent-trace.txt"
        has_session_trace = "--trace-file \"${SESSION_DIR}" in nonflow_text or "--trace-file '${SESSION_DIR}" in nonflow_text
        h.test_result("B2a: Non-flow uses --trace-file from SESSION_DIR", has_session_trace,
                     "Trace file path not from SESSION_DIR")

        # Check that $PRECEDENT_TRACE is not used as a bare argument
        bad_trace_pattern = r'--trace-file\s+"\$PRECEDENT_TRACE"'
        has_bad_trace = bool(re.search(bad_trace_pattern, nonflow_text))
        h.test_result("B2b: Non-flow does not use bare $PRECEDENT_TRACE for --trace-file",
                     not has_bad_trace, "Found bad trace usage")

    # ========================================================================
    # SECTION C: Not in orchestrator
    # ========================================================================
    print("\n[Section C] Helper not in orchestrator docs")

    expert_flow_file = COMMANDS_DIR / "expert-flow.md"
    expert_flow_content = expert_flow_file.read_text() if expert_flow_file.exists() else ""

    flow_reference_file = PROMPTS_DIR / "flow-reference.md"
    flow_reference_content = flow_reference_file.read_text() if flow_reference_file.exists() else ""

    h.test_result("C1: precedent-log.py not in expert-flow.md",
                 "precedent-log.py" not in expert_flow_content,
                 "Found in expert-flow.md")

    h.test_result("C2: precedent-log.py not in flow-reference.md",
                 "precedent-log.py" not in flow_reference_content,
                 "Found in flow-reference.md")

    # ========================================================================
    # SECTION D: No reader leaks in triage/amalgamator/reviewer prompts
    # ========================================================================
    print("\n[Section D] predictions.jsonl not in reader prompts")

    triage_file = PROMPTS_DIR / "triage.md"
    amalgamator_file = PROMPTS_DIR / "amalgamator.md"
    synthesize_file = PROMPTS_DIR / "plan-synthesize-and-check.md"
    audit_file = PROMPTS_DIR / "plan-audit.md"

    triage_content = triage_file.read_text() if triage_file.exists() else ""
    amalgamator_content = amalgamator_file.read_text() if amalgamator_file.exists() else ""
    synthesize_content = synthesize_file.read_text() if synthesize_file.exists() else ""
    audit_content = audit_file.read_text() if audit_file.exists() else ""

    h.test_result("D1: predictions.jsonl not in triage.md",
                 "predictions.jsonl" not in triage_content,
                 "Found in triage.md")

    h.test_result("D2: predictions.jsonl not in amalgamator.md",
                 "predictions.jsonl" not in amalgamator_content,
                 "Found in amalgamator.md")

    h.test_result("D3: predictions.jsonl not in plan-synthesize-and-check.md",
                 "predictions.jsonl" not in synthesize_content,
                 "Found in plan-synthesize-and-check.md")

    h.test_result("D4: predictions.jsonl not in plan-audit.md",
                 "predictions.jsonl" not in audit_content,
                 "Found in plan-audit.md")

    # Check reviewer prompts
    if REVIEWERS_DIR.exists():
        reviewer_files = list(REVIEWERS_DIR.glob("*.yaml"))
        for reviewer_file in reviewer_files:
            reviewer_content = reviewer_file.read_text()
            h.test_result(f"D5: predictions.jsonl not in {reviewer_file.name}",
                         "predictions.jsonl" not in reviewer_content,
                         f"Found in {reviewer_file.name}")

    # ========================================================================
    # SECTION E: ADR-0024 amendment
    # ========================================================================
    print("\n[Section E] ADR-0024 amendment")

    adr_0024_file = DOCS_ADR_DIR / "0024-precedent-pat.md"
    adr_0024_content = adr_0024_file.read_text() if adr_0024_file.exists() else ""

    h.test_result("E1: ADR-0024 amendment section exists",
                 "## Amendment" in adr_0024_content and "prediction log" in adr_0024_content,
                 "Amendment not found")

    if "## Amendment" in adr_0024_content and "prediction log" in adr_0024_content:
        # Extract the amendment section
        amendment_section = re.search(
            r"## Amendment.*?prediction log.*?(?=## Amendment|$)",
            adr_0024_content,
            re.DOTALL
        )

        if amendment_section:
            amendment_text = amendment_section.group(0)

            # Check for key phrases
            h.test_result("E2: Amendment mentions precedent-log.py as writer",
                         "precedent-log.py" in amendment_text,
                         "Writer not mentioned")

            h.test_result("E3: Amendment mentions precedent-digest.py as reader",
                         "precedent-digest.py" in amendment_text and "--stats" in amendment_text,
                         "Reader not mentioned")

            h.test_result("E4: Amendment has never-read sentence",
                         "never read" in amendment_text.lower() or "never reads" in amendment_text.lower(),
                         "Never-read rule not stated")

            h.test_result("E5: Amendment mentions #292 anchoring gap",
                         "#292" in amendment_text or "292" in amendment_text,
                         "#292 gap reference not found")

    # ========================================================================
    # SECTION F: CLAUDE.md Precedent Pat section
    # ========================================================================
    print("\n[Section F] CLAUDE.md Precedent Pat section")

    claude_md_file = REPO_ROOT / "CLAUDE.md"
    claude_md_content = claude_md_file.read_text() if claude_md_file.exists() else ""

    h.test_result("F1: CLAUDE.md exists and has Precedent Pat section",
                 "## Precedent Pat" in claude_md_content,
                 "Section not found")

    # Extract Precedent Pat section
    pat_section = re.search(
        r"## Precedent Pat[^#]*?(?=^##|\Z)",
        claude_md_content,
        re.MULTILINE | re.DOTALL
    )

    if pat_section:
        pat_text = pat_section.group(0)

        h.test_result("F2: CLAUDE.md mentions prediction log path",
                     "predictions.jsonl" in pat_text or "prediction log" in pat_text.lower(),
                     "Log path not mentioned")

        h.test_result("F3: CLAUDE.md mentions precedent-log.py",
                     "precedent-log.py" in pat_text,
                     "Helper script not mentioned")

        h.test_result("F4: CLAUDE.md mentions precedent-digest.py --stats",
                     "precedent-digest.py" in pat_text and "--stats" in pat_text,
                     "Digest stats not mentioned")

        h.test_result("F5: CLAUDE.md states Pat never reads the log",
                     "never read" in pat_text.lower() or "never reads" in pat_text.lower(),
                     "Never-read statement missing")

    # ========================================================================
    # SECTION G: precedent-pat.md never-read sentence
    # ========================================================================
    print("\n[Section G] precedent-pat.md never-read sentence")

    precedent_pat_file = PROMPTS_DIR / "precedent-pat.md"
    precedent_pat_content = precedent_pat_file.read_text() if precedent_pat_file.exists() else ""

    # Check that the hard rules section mentions predictions.jsonl
    hard_rules_section = re.search(
        r"## Hard rules[^#]*?(?=^##|\Z)",
        precedent_pat_content,
        re.MULTILINE | re.DOTALL
    )

    if hard_rules_section:
        hard_rules_text = hard_rules_section.group(0)
        has_log_rule = "predictions.jsonl" in hard_rules_text
        h.test_result("G1: Hard rules section mentions predictions.jsonl",
                     has_log_rule, "Log never-read rule not found")
    else:
        h.test_result("G1: Hard rules section exists", False, "Section not found")

    h.summarize_and_exit()
