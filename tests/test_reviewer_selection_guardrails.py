#!/usr/bin/env python3
"""
Test suite for reviewer selection guardrails (structural pre-Router exclusion rules).

Covers:
  (a) Every slug in structural_pre_gate_ineligible exists as a reviewer in index.yaml
  (b) No denylisted slug appears in pre-Router structural-gate blocks
  (c) reviewer-selection-audit.py is not referenced in core reviewer infrastructure
  (d) Sam System gate block has explicit readable-input guard on diff-index.md

Run with: python3 tests/test_reviewer_selection_guardrails.py
"""

import sys
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT


def main():
    h = Harness("REVIEWER SELECTION GUARDRAILS TEST SUITE")

    # ========================================================================
    # Test (a): Every slug in structural_pre_gate_ineligible exists as a
    #           reviewer in index.yaml
    # ========================================================================
    print("[Test (a)] structural_pre_gate_ineligible slugs are defined reviewers")

    index_file = REPO_ROOT / "reviewers" / "index.yaml"
    index_content = index_file.read_text() if index_file.exists() else ""

    # Extract the structural_pre_gate_ineligible list
    denylist = []
    ineligible_match = re.search(
        r"^structural_pre_gate_ineligible\s*:\s*\[([^\]]+)\]",
        index_content,
        re.MULTILINE
    )
    if ineligible_match:
        # Parse comma-separated values, strip whitespace and quotes
        values_str = ineligible_match.group(1)
        denylist = [
            v.strip().strip("'\"")
            for v in values_str.split(",")
        ]

    h.test_result(
        "structural_pre_gate_ineligible is defined in index.yaml",
        len(denylist) > 0,
        f"Found {len(denylist)} entries: {denylist}" if denylist else "No entries found"
    )

    # Extract all reviewer slugs from the reviewers: list
    reviewer_slugs = set()
    for match in re.finditer(
        r"^\s*file:\s+([a-z\-]+)\.yaml",
        index_content,
        re.MULTILINE
    ):
        reviewer_slugs.add(match.group(1))

    # Check each denylist entry
    missing_reviewers = [slug for slug in denylist if slug not in reviewer_slugs]
    h.test_result(
        "All denylisted slugs exist as reviewers",
        len(missing_reviewers) == 0,
        f"Missing: {missing_reviewers}" if missing_reviewers else ""
    )

    # ========================================================================
    # Test (b): No denylisted slug appears in pre-Router structural-gate
    #           blocks (bash code blocks containing gate logic)
    # ========================================================================
    print()
    print("[Test (b)] No denylisted slug in pre-Router structural-gate blocks")

    panel_file = REPO_ROOT / "prompts" / "expert-review-panel.md"
    panel_content = panel_file.read_text() if panel_file.exists() else ""

    # Find bash code blocks that contain Sam System gate logic (```bash ... SAM_SYSTEM_GATE_REASON ... ```)
    # Match between ```bash and the closing ``` to get only the code, not prose that follows
    gate_pattern = r"```bash\n.*?SAM_SYSTEM_GATE_REASON.*?\n```"
    gate_blocks = re.findall(
        gate_pattern,
        panel_content,
        re.DOTALL
    )

    gated_reviewers_in_block = []
    for gate_block in gate_blocks:
        for slug in denylist:
            if slug in gate_block:
                gated_reviewers_in_block.append(slug)

    h.test_result(
        "No denylisted reviewer in pre-Router structural gate code blocks",
        len(gated_reviewers_in_block) == 0,
        f"Found {len(gated_reviewers_in_block)} denylisted in gate code: {gated_reviewers_in_block}"
        if gated_reviewers_in_block else ""
    )

    # ========================================================================
    # Test (c): reviewer-selection-audit.py is not referenced in core
    #           reviewer infrastructure
    # ========================================================================
    print()
    print("[Test (c)] reviewer-selection-audit.py not referenced")

    check_files = [
        REPO_ROOT / "prompts" / "expert-review-panel.md",
        REPO_ROOT / "prompts" / "router.md",
        REPO_ROOT / "prompts" / "triage.md",
    ]
    # Also check all reviewer YAML files
    check_files.extend((REPO_ROOT / "reviewers").glob("*.yaml"))

    audit_script_references = []
    for fpath in check_files:
        if fpath.exists():
            content = fpath.read_text()
            if "reviewer-selection-audit" in content:
                audit_script_references.append(fpath.name)

    h.test_result(
        "reviewer-selection-audit.py not referenced in core files",
        len(audit_script_references) == 0,
        f"Found in: {', '.join(audit_script_references)}"
        if audit_script_references else ""
    )

    # ========================================================================
    # Test (d): Sam System gate block has explicit readable-input guard on
    #           diff-index.md
    # ========================================================================
    print()
    print("[Test (d)] Sam System gate has readable-input guard on diff-index.md")

    # Look for the fail-open guard pattern
    fail_open_pattern = r"\[\s*!\s*-r\s+.*diff-index\.md"
    has_fail_open = bool(re.search(fail_open_pattern, panel_content))

    h.test_result(
        "Sam System gate has explicit readable-input guard",
        has_fail_open,
        "Pattern '[ ! -r $REVIEW_DIR/diff-index.md ]' not found in Sam System gate"
        if not has_fail_open else ""
    )

    # Also check that when guard fires, it appends the skipped marker
    skipped_marker = "structural-gate: sam-system | skipped | diff-index.md unreadable"
    has_skipped_marker = skipped_marker in panel_content

    h.test_result(
        "Sam System gate appends skipped marker on unreadable input",
        has_skipped_marker,
        "Skipped marker comment not found in Sam System gate"
        if not has_skipped_marker else ""
    )

    # Also verify the audit marker is present for excluded case
    audit_marker_pattern = "structural-gate: sam-system \\| excluded \\|"
    has_audit_marker = bool(re.search(audit_marker_pattern, panel_content))

    h.test_result(
        "Sam System gate appends audit marker on exclusion",
        has_audit_marker,
        "Audit marker comment not found in Sam System gate"
        if not has_audit_marker else ""
    )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
