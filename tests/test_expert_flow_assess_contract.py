#!/usr/bin/env python3
"""
Spec-blind contract tests for the #383 slice 1 assess stage: the expert-flow command doc, the
ADR-0023 amendment, the CLAUDE.md bullets, the flow-reference roster and answer/question shapes,
and the restricted flow-assessor agent.

Assertions are literal checks of the plan's required wording and structure. The range-overlap
boundary is read from the verifier's RANGE_OVERLAP_SLACK constant, not hardcoded here.

Run with: python3 tests/test_expert_flow_assess_contract.py
"""

import importlib.util
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
VERIFIER = REPO_ROOT / "scripts" / "flow-verify-receipt.py"


def load_verifier():
    """Import scripts/flow-verify-receipt.py as a module, the way sibling tests load script modules."""
    spec = importlib.util.spec_from_file_location("flow_verify_receipt_contract", VERIFIER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RANGE_OVERLAP_SLACK = load_verifier().RANGE_OVERLAP_SLACK


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
    h.test_result("expert-flow.md names the verified-done status", "verified-done" in ef)
    h.test_result("expert-flow.md states it does not finish outstanding items automatically",
                  "does not finish outstanding items automatically" in ef)

    print()
    print("[commands/expert-flow.md: #435 assess call shapes and wording]")

    h.test_result("assess item 4a calls --assess-anchors with --flow-dir only",
                  '--assess-anchors --flow-dir "${FLOW_DIR}"' in assess_sec and "--assess-anchors --step" not in ef)
    h.test_result("assess item 4a parses the '<id> | overlap|no-overlap|unavailable | <detail>' lines",
                  "<id> | overlap|no-overlap|unavailable | <detail>" in assess_sec)
    h.test_result("--match-outstanding-issue truncation is exit 3 (not exit 2)",
                  "exits 3 (search truncated)" in assess_sec and "exits 2 (search truncated)" not in ef)
    h.test_result("--match-outstanding-issue invalid input (exit 2) stops and asks",
                  "On exit 2 (invalid input), create nothing" in assess_sec)
    h.test_result("assess item 10.d names the Fix now sequence with FIXNOW_IDS (no circular reference)",
                  "run the Fix now sequence with `FIXNOW_IDS`" in assess_sec
                  and "continue to the Fix now sequence (sub-step `c` of item 10.b" not in ef)
    h.test_result("Fix now carry rule is stated as step 3a of the assess stage",
                  "**Fix now carry rule.**" in assess_sec)
    h.test_result("Fix now carry rule names the carried-seq file and the step 6 ledger mark",
                  "answers/assess-<SEQ>.fixnow-seq" in assess_sec and "addressed" in assess_sec
                  and "step 6" in assess_sec)
    h.test_result("Fix now carry rule leaves a Fix now record unmarked and out of the marker",
                  "do not mark it" in assess_sec and "is not written to the marker" in assess_sec)

    print()
    print("[commands/expert-flow.md: one coverage writer and ordered evidence stubs (#435 F9)]")

    cov_stub_attempt = "printf 'STUB: superseded before attempt\\n' > \"$FLOW_DIR/steps/<NN>-<STEP>.coverage.txt\""
    cov_stub_audit = "printf 'STUB: superseded before audit\\n' > \"$FLOW_DIR/steps/<NN>-<STEP>.coverage.txt\""
    ev_stub = ("[ \"<STEP>\" = fix ] && printf '%s\\n' '{\"format\":1,\"available\":false,\"reason\":\"superseded\"}'"
               " > \"$FLOW_DIR/steps/<NN>-fix.evidence.json\"")
    h.test_result("both STUB sites stub the coverage file",
                  ef.count(cov_stub_attempt) == 1 and ef.count(cov_stub_audit) == 1,
                  f"attempt={ef.count(cov_stub_attempt)} audit={ef.count(cov_stub_audit)}")
    ev_sites = [m.start() for m in re.finditer(re.escape(ev_stub), ef)]
    h.test_result("both STUB sites stub the fix evidence file too", len(ev_sites) == 2, f"sites={len(ev_sites)}")
    # Each evidence stub comes after the coverage stub of its own site: the nearest preceding coverage
    # stub is at most one site-block (a few lines) away, and the coverage stub is written first.
    cov_sites = sorted([m.start() for m in re.finditer(re.escape(cov_stub_attempt), ef)]
                       + [m.start() for m in re.finditer(re.escape(cov_stub_audit), ef)])
    ordered = len(cov_sites) == 2 and all(
        any(0 < ev - cov <= 400 for cov in cov_sites) for ev in ev_sites
    )
    h.test_result("each evidence stub follows its coverage stub at the same site (ordering)", ordered)
    h.test_result("the fix coverage step is the single writer of the fix coverage and evidence files",
                  "is the single writer of `steps/<NN>-fix.coverage.txt`" in ef)
    h.test_result("the audit writes the implement coverage file only, not the fix one",
                  "For `implement`, write the output to" in ef and "the orchestrator writes no coverage file" in ef)

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
    flow_bullet = section(claude, "- `/expert-flow` — lifecycle", "- `/expert-fleet`")
    h.test_result("CLAUDE.md /expert-flow summary points at the 2026-11-02 human-free closure amendment",
                  "2026-11-02 amendment, #435" in flow_bullet, f"bullet length={len(flow_bullet)}")

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
    h.test_result("ADR amendment documents RANGE_OVERLAP_SLACK at the verifier's value",
                  f"RANGE_OVERLAP_SLACK = {RANGE_OVERLAP_SLACK}" in adr)
    h.test_result("flow-assessor documents RANGE_OVERLAP_SLACK at the verifier's value",
                  f"RANGE_OVERLAP_SLACK = {RANGE_OVERLAP_SLACK}" in assessor)
    h.test_result("ADR amendment mentions both B and A conditions",
                  "(B)" in adr and "(A)" in adr and "both of these pass" in adr)
    h.test_result("ADR amendment mentions Q1, Q2, Q3, D1, D2 decisions",
                  all(d in adr for d in ("Q1 = BOTH", "Q2 = C", "Q3 = A", "D1 (accepted", "D2 (accepted")))
    h.test_result("ADR amendment states both resume rules (no re-asks, replay of unapplied files)",
                  "neither re-asks an answered form" in adr and "is replayed" in adr)
    h.test_result("ADR amendment hand-offs cite sections, not plan-internal step numbers",
                  "Steps 6, 7, 8" not in adr and "Step 1 (ADR amendment)" not in adr)
    h.test_result("flow-assessor.md does not contain 'Unknown Coverage: Always Decision'",
                  "Unknown Coverage: Always Decision" not in assessor)
    h.test_result("flow-assessor.md documents range_overlap field",
                  "range_overlap:" in assessor and "overlap|no-overlap|unavailable" in assessor)
    h.test_result("flow-assessor.md describes anchor grammar for already-done fix items",
                  "path:line" in assessor and "path:start-end" in assessor and "7-40 hex" in assessor)
    h.test_result("flow-assessor.md requires the anchor path exactly as git diff --name-only prints it",
                  "exactly as `git diff --name-only" in assessor)
    h.test_result("flow-assessor.md requires the SHA anchor to touch the row's path",
                  "touched the row's path" in assessor)
    h.test_result("flow-assessor.md downgrades a non-conforming anchor to unfinished and decision",
                  "not an anchor" in assessor and "decided by the rule below" in assessor)
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
                  "steps/05-fix.evidence.json" in ef or "steps/<NN>-fix.evidence.json" in ef)
    h.test_result("expert-flow.md assess item 9 mentions unknown with range_overlap",
                  "coverage `unknown` and `range_overlap: overlap`" in assess_sec)
    h.test_result("expert-flow.md uses printf '%s' with issue list JSON piping",
                  'printf \'%s\' "$VAR"' in assess_sec)

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
