#!/usr/bin/env python3
"""
Spec-blind contract tests for the #383 slice 1 assess stage: the expert-flow command doc, the
ADR-0023 amendment, the CLAUDE.md bullets, the flow-reference roster and answer/question shapes,
and the restricted flow-assessor agent.

Assertions are literal checks of the plan's required wording and structure.

Run with: python3 tests/test_expert_flow_assess_contract.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _test_harness import REPO_ROOT, Harness  # noqa: E402

EXPERT_FLOW = REPO_ROOT / "commands" / "expert-flow.md"
ADR = REPO_ROOT / "docs" / "adr" / "0023-expert-flow.md"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
FLOW_REF = REPO_ROOT / "prompts" / "flow-reference.md"
ASSESSOR = REPO_ROOT / "agents" / "flow-assessor.md"


def section(text, start, end):
    """Text from the first occurrence of `start` up to the next occurrence of `end` after it."""
    i = text.find(start)
    if i < 0:
        return ""
    j = text.find(end, i + len(start))
    return text[i:j] if j >= 0 else text[i:]


def frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return m.group(1) if m else ""


def main():
    h = Harness("EXPERT-FLOW ASSESS STAGE CONTRACT TESTS (#383 slice 1)")

    ef = EXPERT_FLOW.read_text()
    adr = ADR.read_text()
    claude = CLAUDE_MD.read_text()
    flow_ref = FLOW_REF.read_text()
    assessor = ASSESSOR.read_text()

    print("[commands/expert-flow.md: assess stage]")

    # Section boundaries are the bold stage headings: the bare strings "assess." / "ship." / "merge gate"
    # also occur in prose and in file names such as 08-ship.outstanding.md, which cut sections short.
    assess_sec = section(ef, "**assess.**", "**ship.**")
    ship_sec = section(ef, "**ship.**", "**merge gate.**")
    merge_sec = section(ef, "**merge gate.**", "\n## ")

    h.test_result("expert-flow.md has an assess. section before ship.", len(assess_sec) > 0)
    h.test_result("roster numbers assess 07, ship 08, merge 09",
                  all(r in ef for r in ("| 07 | assess |", "| 08 | ship |", "| 09 | merge |")))
    h.test_result("roster documents the outstanding_format legacy numbering rule",
                  "Numbering is keyed on `outstanding_format`" in ef and "keeps ship at `07` and merge at `08`" in ef)
    h.test_result("marker path is defined once as MARKER and used by the gate calls",
                  "MARKER=${FLOW_DIR}/steps/08-ship.outstanding.md" in ef
                  and ef.count('--receipt "${MARKER}"') == 4
                  and '--receipt "${FLOW_DIR}/steps/07-ship.outstanding.md"' not in ef)
    h.test_result("ship. section contains --outstanding-gate", "--outstanding-gate" in ship_sec,
                  f"ship section length={len(ship_sec)}")
    h.test_result("ship. section has no 'Open an issue ... or ... Drop' relay",
                  re.search(r"Open an issue[^\n]*\bor\b[^\n]*\bDrop\b", ship_sec) is None)
    h.test_result("merge gate section says regardless of ... auto_merge",
                  re.search(r"regardless of[\s\S]{0,200}auto_merge", merge_sec) is not None)

    ask_count = assess_sec.count("AskUserQuestion")
    ask_block_count = assess_sec.count("Ask block")
    h.test_result("AskUserQuestion mentions in assess. section do not exceed Ask block mentions",
                  ask_count <= ask_block_count, f"AskUserQuestion={ask_count} Ask block={ask_block_count}")

    h.test_result("assess stop reason 'assess: working tree changed during assessment' is documented",
                  "assess: working tree changed during assessment" in ef)
    h.test_result("assess section bounds the batch at most 8", "at most 8" in assess_sec,
                  f"assess section length={len(assess_sec)}")
    h.test_result("assess section uses mv for atomic writes", "temp file then `mv`" in assess_sec)
    h.test_result("assess section uses printf '%s' for JSON piping", "printf '%s'" in assess_sec)
    h.test_result("assess section mentions follow-up", "follow-up" in assess_sec)
    h.test_result("assess section says persist each answer as it is made",
                  "Persist each answer as soon as it is applied" in assess_sec)
    h.test_result("assess section has the 'Already done (assessor-verified):' label",
                  "Already done (assessor-verified):" in assess_sec)
    h.test_result("assess section offers Take over", "Take over" in assess_sec)
    h.test_result("resume rules name step: \"assess\" and re-enter",
                  'step: "assess"' in ef and "re-enter" in ef)
    h.test_result("roster row '| 07 | assess |' is present", "| 07 | assess |" in ef)
    h.test_result("expert-flow.md names the verified-done status", "verified-done" in ef)
    h.test_result("expert-flow.md states it does not finish outstanding items automatically",
                  "does not finish outstanding items automatically" in ef)

    print()
    print("[docs/adr/0023-expert-flow.md: amendment]")

    h.test_result("ADR has an assess amendment heading",
                  "## Amendment" in adr and "assess outstanding items before ship" in adr and "#383" in adr)
    h.test_result("ADR has at least three 'superseded ... 2026-10-27' mentions",
                  len(re.findall(r"superseded[\s\S]{0,120}2026-10-27", adr)) >= 3
                  or len(re.findall(r"2026-10-27[\s\S]{0,120}superseded", adr)) >= 3)
    for term in ("orchestrator stage", "attempts.assess", "failures.assess", "stop_source.assess",
                 "outstanding_format", "legacy", "boundary with #380", "slice 2"):
        h.test_result(f"ADR mentions {term!r}", term in adr)

    print()
    print("[CLAUDE.md]")

    h.test_result("CLAUDE.md drops the 'a choice to open an issue or drop' relay wording",
                  "a choice to open an issue or drop" not in claude)
    agents_idx = claude.find("## Agents")
    agents_part = claude[agents_idx:] if agents_idx >= 0 else ""
    h.test_result("CLAUDE.md Agents section lists flow-assessor", "flow-assessor" in agents_part)

    print()
    print("[agents/flow-assessor.md]")

    fm = frontmatter(assessor)
    h.test_result("flow-assessor frontmatter sets model: sonnet", re.search(r"^model:\s*sonnet\s*$", fm, re.M) is not None,
                  f"frontmatter:\n{fm}")
    tools_line = next((ln for ln in fm.splitlines() if ln.startswith("tools:")), "")
    h.test_result("flow-assessor tools list has no Write, Edit, MultiEdit or NotebookEdit",
                  not any(t in tools_line for t in ("Write", "Edit", "MultiEdit", "NotebookEdit")),
                  f"tools line: {tools_line!r}")
    bash_entries = re.findall(r"Bash\(([^)]*)\)", tools_line)
    h.test_result("flow-assessor Bash grants are read-only git and ls only",
                  bash_entries and all(
                      re.match(r"^(git (diff|log|show|rev-parse|status)|ls)(\b|:|\s|\*)", e.strip()) for e in bash_entries),
                  f"bash entries: {bash_entries}")

    print()
    print("[prompts/flow-reference.md]")

    h.test_result("flow-reference mentions the 07-assess receipt", "07-assess" in flow_ref)
    h.test_result("flow-reference mentions the item field for questions and answers",
                  re.search(r"\bitem\b", flow_ref) is not None)

    print()
    print("[#435 amendment: resolved File-cell basenames and script-anchored already-done]")

    h.test_result("ADR amendment 2026-11-02 exists with correct heading",
                  "Amendment (2026-11-02): resolved File-cell basenames" in adr)
    h.test_result("ADR amendment mentions frozen artifact evidence.json",
                  "steps/<NN>-fix.evidence.json" in adr)
    h.test_result("ADR amendment documents RANGE_OVERLAP_SLACK = 3",
                  "RANGE_OVERLAP_SLACK = 3" in adr)
    h.test_result("ADR amendment mentions both B and A conditions",
                  "(B)" in adr and "(A)" in adr and "both of these pass" in adr)
    h.test_result("ADR amendment mentions Q1, Q2, Q3, D1, D2 decisions",
                  all(d in adr for d in ("Q1 = BOTH", "Q2 = C", "Q3 = A", "D1 (accepted", "D2 (accepted")))
    h.test_result("flow-assessor.md does not contain 'Unknown Coverage: Always Decision'",
                  "Unknown Coverage: Always Decision" not in assessor)
    h.test_result("flow-assessor.md documents range_overlap field",
                  "range_overlap:" in assessor and "overlap|no-overlap|unavailable" in assessor)
    h.test_result("flow-assessor.md describes anchor grammar for already-done fix items",
                  "path:line" in assessor and "path:start-end" in assessor and "7-40 hex" in assessor)
    h.test_result("expert-flow.md assess section calls --assess-anchors",
                  "--assess-anchors" in assess_sec)
    h.test_result("expert-flow.md assess section mentions range_overlap in ITEM block",
                  "range_overlap:" in assess_sec)
    h.test_result("expert-flow.md assess item 10 mentions forms of at most items",
                  "forms of at most" in assess_sec)
    h.test_result("expert-flow.md assess item 10 uses --match-outstanding-issue",
                  "--match-outstanding-issue" in assess_sec)
    h.test_result("expert-flow.md assess item 10 describes per-form persistence",
                  "answers/assess-<SEQ>" in assess_sec and "Write the answers file" in assess_sec)
    h.test_result("expert-flow.md assess item 10 describes Pause-once logic",
                  "after the last form" in assess_sec and "log `assess paused`" in assess_sec)
    h.test_result("expert-flow.md fix section mentions fix-audit coverage call",
                  "--coverage-out" in ef and "--evidence-out" in ef)
    h.test_result("expert-flow.md fix section mentions evidence.json artifact",
                  "steps/06-fix.evidence.json" in ef or "steps/<NN>-fix.evidence.json" in ef)
    h.test_result("expert-flow.md assess item 9 mentions unknown with range_overlap",
                  "coverage `unknown` and `range_overlap: overlap`" in assess_sec)
    h.test_result("expert-flow.md uses printf '%s' with issue list JSON piping",
                  'printf \'%s\' "$VAR"' in assess_sec)

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
