#!/usr/bin/env python3
"""Test suite for precedent-log documentation and placement (#295).

Tests that:
- Call-site ordering in commands/expert-plan.md is correct (non-flow and flow branches)
- The trace source is SESSION_DIR, not a bare PRECEDENT_TRACE variable
- The helper never appears in orchestrator docs
- predictions.jsonl never appears in reader prompts or reviewer personas
- ADR-0024 prediction-log amendment documents the feature
- CLAUDE.md Precedent Pat section documents the log and its never-read rule
- precedent-pat.md hard rules forbid reading the log
- The ordering and section helpers themselves behave correctly (self-tests)

Run with: python3 tests/test_precedent_log_docs.py
"""

import re

from _test_harness import REPO_ROOT, Harness

COMMANDS_DIR = REPO_ROOT / "commands"
PROMPTS_DIR = REPO_ROOT / "prompts"
DOCS_ADR_DIR = REPO_ROOT / "docs" / "adr"
REVIEWERS_DIR = REPO_ROOT / "reviewers"

NONFLOW_START = "**Non-flow branch:**"
NONFLOW_END = "**Flow branch"
LOOP_MARKER = "**The loop ends**"


def _read(path):
    """File text, or empty string when the file is absent (the presence test reports that)."""
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _between(text, start_marker, end_marker):
    """Text from start_marker up to (not including) the next end_marker, or to end of text."""
    start = text.find(start_marker)
    if start < 0:
        return ""
    end = text.find(end_marker, start + len(start_marker))
    return text[start:end] if end >= 0 else text[start:]


def _until_heading(text, marker):
    """Text from marker up to the next level-2 or level-3 markdown heading, or to end of text."""
    start = text.find(marker)
    if start < 0:
        return ""
    heading = re.compile(r"^#{2,3} ", re.MULTILINE).search(text, start + len(marker))
    return text[start:heading.start()] if heading else text[start:]


def _section(text, heading_pattern):
    """Level-2 section whose heading line matches heading_pattern (a regex on the text after '## ').

    The section runs to the next level-2 heading or to \\Z (end of text).
    """
    match = re.search(
        rf"^## {heading_pattern}[^\n]*\n.*?(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    return match.group(0) if match else ""


def _ordered(text, markers):
    """True iff every marker occurs in text and their first occurrences are strictly increasing."""
    positions = []
    for marker in markers:
        pos = text.find(marker)
        if pos < 0:
            return False
        positions.append(pos)
    return all(a < b for a, b in zip(positions, positions[1:]))


CALL_SITE_ORDER = ["AskUserQuestion", "precedent: <PRECEDENT_TRACE>", "precedent-log.py"]


if __name__ == "__main__":
    h = Harness("PRECEDENT-LOG DOCUMENTATION TEST SUITE")

    # ========================================================================
    # SECTION A: Non-flow call site ordering in commands/expert-plan.md
    # ========================================================================
    print("\n[Section A] Non-flow call site ordering in commands/expert-plan.md")

    expert_plan_file = COMMANDS_DIR / "expert-plan.md"
    expert_plan_content = _read(expert_plan_file)
    h.test_result("A1: expert-plan.md exists", expert_plan_file.exists(), "File not found")

    nonflow = _between(expert_plan_content, NONFLOW_START, NONFLOW_END)
    h.test_result("A0: non-flow branch marker present", bool(nonflow),
                  f"'{NONFLOW_START}' ... '{NONFLOW_END}' not found")

    h.test_result("A2: non-flow has AskUserQuestion instruction",
                  "AskUserQuestion" in nonflow, "Not found")
    h.test_result("A3: non-flow has precedent trace append",
                  "precedent: <PRECEDENT_TRACE>" in nonflow, "Not found")
    h.test_result("A4: non-flow has precedent-log.py with --answers",
                  "precedent-log.py" in nonflow and "--answers" in nonflow, "Not found")
    h.test_result("A5: non-flow AskUserQuestion before precedent-log.py",
                  _ordered(nonflow, ["AskUserQuestion", "precedent-log.py"]),
                  "AskUserQuestion is missing or does not precede precedent-log.py")
    h.test_result("A6: non-flow precedent append before precedent-log.py",
                  _ordered(nonflow, ["precedent: <PRECEDENT_TRACE>", "precedent-log.py"]),
                  "trace append is missing or does not precede precedent-log.py")
    numbered_log_step = re.search(r"^\s*\d+\.[^\n]*precedent-log", nonflow, re.MULTILINE)
    h.test_result("A7: precedent-log.py is not a numbered step in the non-flow branch",
                  bool(nonflow) and numbered_log_step is None,
                  f"numbered step found: {numbered_log_step.group(0) if numbered_log_step else None}")

    # ========================================================================
    # SECTION B: Flow placement in "The loop ends" paragraph
    # ========================================================================
    print("\n[Section B] Flow placement in 'The loop ends' paragraph")

    loop_text = _until_heading(expert_plan_content, LOOP_MARKER)
    h.test_result("B0: 'The loop ends' section present", bool(loop_text),
                  f"'{LOOP_MARKER}' not found")

    h.test_result("B1: flow has precedent-log.py with --flow-answers",
                  "precedent-log.py" in loop_text and "--flow-answers" in loop_text, "Not found")
    h.test_result("B2: checkpoint append before precedent-log.py",
                  _ordered(loop_text, ["Append every round's reading to", "precedent-log.py"]),
                  "checkpoint append is missing or does not precede precedent-log.py")
    h.test_result("B3: precedent-log.py before 'proceed to Step 6'",
                  _ordered(loop_text, ["precedent-log.py", "proceed to Step 6"]),
                  "precedent-log.py is missing or does not precede 'proceed to Step 6'")

    # ========================================================================
    # SECTION B2: Trace source - --trace-file from SESSION_DIR, not a bare PRECEDENT_TRACE
    # ========================================================================
    print("\n[Section B2] Trace source handling")

    h.test_result("B2a: non-flow uses --trace-file from SESSION_DIR",
                  '--trace-file "${SESSION_DIR}' in nonflow or "--trace-file '${SESSION_DIR}" in nonflow,
                  "Trace file path not from SESSION_DIR")
    h.test_result("B2b: non-flow does not use bare $PRECEDENT_TRACE for --trace-file",
                  bool(nonflow) and not re.search(r'--trace-file\s+"\$PRECEDENT_TRACE"', nonflow),
                  "Found bad trace usage")
    h.test_result("B2c: flow uses --trace-file from SESSION_DIR",
                  '--trace-file "${SESSION_DIR}' in loop_text or "--trace-file '${SESSION_DIR}" in loop_text,
                  "Trace file path not from SESSION_DIR")
    h.test_result("B2d: flow derives or uses REPO_KEY",
                  bool(re.search(r"(?:gh repo|REPO_KEY|repo-key)", loop_text, re.IGNORECASE)),
                  "REPO_KEY reference not found in loop")

    # ========================================================================
    # SECTION C: Not in orchestrator
    # ========================================================================
    print("\n[Section C] Helper not in orchestrator docs")

    expert_flow_content = _read(COMMANDS_DIR / "expert-flow.md")
    flow_reference_content = _read(PROMPTS_DIR / "flow-reference.md")

    h.test_result("C0: expert-flow.md present", (COMMANDS_DIR / "expert-flow.md").exists(), "File not found")
    h.test_result("C1: precedent-log.py not in expert-flow.md",
                  "precedent-log.py" not in expert_flow_content, "Found in expert-flow.md")
    h.test_result("C2: precedent-log.py not in flow-reference.md",
                  "precedent-log.py" not in flow_reference_content, "Found in flow-reference.md")

    # ========================================================================
    # SECTION D: No reader leaks in triage/amalgamator/reviewer prompts
    # ========================================================================
    print("\n[Section D] predictions.jsonl not in reader prompts")

    for label, filename in [
        ("D1", "triage.md"),
        ("D2", "amalgamator.md"),
        ("D3", "plan-synthesize-and-check.md"),
        ("D4", "plan-audit.md"),
    ]:
        content = _read(PROMPTS_DIR / filename)
        h.test_result(f"{label}: predictions.jsonl not in {filename}",
                      (PROMPTS_DIR / filename).exists() and "predictions.jsonl" not in content,
                      f"Found in {filename} or file missing")

    reviewer_files = sorted(REVIEWERS_DIR.glob("*.yaml")) if REVIEWERS_DIR.exists() else []
    h.test_result("D5: reviewer yaml files found", len(reviewer_files) > 0,
                  f"no reviewers/*.yaml under {REVIEWERS_DIR}")
    for reviewer_file in reviewer_files:
        h.test_result(f"D5: predictions.jsonl not in {reviewer_file.name}",
                      "predictions.jsonl" not in _read(reviewer_file),
                      f"Found in {reviewer_file.name}")

    # ========================================================================
    # SECTION E: ADR-0024 prediction-log amendment
    # ========================================================================
    print("\n[Section E] ADR-0024 prediction-log amendment")

    adr_file = DOCS_ADR_DIR / "0024-precedent-pat.md"
    adr_content = _read(adr_file)
    amendment = ""
    amendment_match = re.search(
        r"^## Amendment[^\n]*prediction log[^\n]*\n.*?(?=^## |\Z)",
        adr_content,
        re.MULTILINE | re.DOTALL,
    )
    if amendment_match:
        amendment = amendment_match.group(0)

    h.test_result("E1: ADR-0024 prediction-log amendment section exists", bool(amendment),
                  "No '## Amendment ... prediction log' section found")
    h.test_result("E2: amendment names precedent-log.py as writer",
                  "precedent-log.py" in amendment, "Writer not mentioned")
    h.test_result("E3: amendment names precedent-digest.py --stats as reader",
                  "precedent-digest.py" in amendment and "--stats" in amendment, "Reader not mentioned")
    h.test_result("E4: amendment states the never-read rule",
                  "never read" in amendment.lower() or "never reads" in amendment.lower(),
                  "Never-read rule not stated")
    h.test_result("E5: amendment references the #292 anchoring gap",
                  "#292" in amendment or "292" in amendment, "#292 gap reference not found")

    # ========================================================================
    # SECTION F: CLAUDE.md Precedent Pat section
    # ========================================================================
    print("\n[Section F] CLAUDE.md Precedent Pat section")

    claude_md_content = _read(REPO_ROOT / "CLAUDE.md")
    pat_text = _section(claude_md_content, r"Precedent Pat\b")
    h.test_result("F1: CLAUDE.md has a '## Precedent Pat' section", bool(pat_text),
                  "Section not found")
    h.test_result("F2: CLAUDE.md mentions the prediction log",
                  "predictions.jsonl" in pat_text or "prediction log" in pat_text.lower(),
                  "Log path not mentioned")
    h.test_result("F3: CLAUDE.md mentions precedent-log.py",
                  "precedent-log.py" in pat_text, "Helper script not mentioned")
    h.test_result("F4: CLAUDE.md mentions precedent-digest.py --stats",
                  "precedent-digest.py" in pat_text and "--stats" in pat_text,
                  "Digest stats not mentioned")
    h.test_result("F5: CLAUDE.md states Pat never reads the log",
                  "never read" in pat_text.lower() or "never reads" in pat_text.lower(),
                  "Never-read statement missing")

    # ========================================================================
    # SECTION G: precedent-pat.md hard rules
    # ========================================================================
    print("\n[Section G] precedent-pat.md never-read rule")

    hard_rules = _section(_read(PROMPTS_DIR / "precedent-pat.md"), r"Hard rules")
    h.test_result("G0: '## Hard rules' section present", bool(hard_rules), "Section not found")
    h.test_result("G1: hard rules mention predictions.jsonl",
                  "predictions.jsonl" in hard_rules, "Log never-read rule not found")

    # ========================================================================
    # SECTION H: Self-tests for the helpers this file relies on
    # ========================================================================
    print("\n[Section H] Helper self-tests")

    documented_order = (
        "Ask the user with AskUserQuestion.\n"
        "Record precedent: <PRECEDENT_TRACE> in the trace.\n"
        "Then run precedent-log.py --answers answers.json.\n"
    )
    swapped_order = (
        "Run precedent-log.py --answers answers.json first.\n"
        "Then ask with AskUserQuestion.\n"
        "Finally record precedent: <PRECEDENT_TRACE>.\n"
    )
    h.test_result("H1: ordering checker accepts documented order",
                  _ordered(documented_order, CALL_SITE_ORDER), "documented order rejected")
    h.test_result("H2: ordering checker rejects swapped order",
                  not _ordered(swapped_order, CALL_SITE_ORDER), "swapped order accepted")
    h.test_result("H3: ordering checker rejects a missing marker",
                  not _ordered("AskUserQuestion then precedent-log.py", CALL_SITE_ORDER),
                  "missing marker accepted")

    section_doc = (
        "## Intro\nnothing here\n"
        "## Hard rules\n- never read predictions.jsonl\n"
    )
    h.test_result("H4: section slice runs to end of text (\\Z) when last",
                  "predictions.jsonl" in _section(section_doc, r"Hard rules"),
                  "last-section body not captured")
    h.test_result("H5: section slice stops at the next level-2 heading",
                  "nothing here" not in _section(section_doc, r"Hard rules")
                  and "nothing here" in _section(section_doc, r"Intro"),
                  "section slice bled across headings")

    h.summarize_and_exit()
