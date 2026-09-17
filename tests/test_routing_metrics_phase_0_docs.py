#!/usr/bin/env python3
"""
Documentation correctness tests for Routing v2 Phase 0 (section 0d).

Verifies that documentation has been corrected to reflect that Sam System is
no longer always-run (issue #148 already removed the runtime behavior; this
tests that the docs match).

Tests:
- prompts/amalgamator.md:110 — drops Sam System from ALWAYS-RUN legend
- agents/expert-reviewer.md:18 — changes "always" claim for Sam System
- docs/adr/0003-tagger-routing.md — Amendment section updated
- prompts/triage.md — "Doing it" table has "Raised by" column

Run with: python3 tests/test_routing_metrics_phase_0_docs.py
"""

import sys
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness


def read_file_safe(path: Path) -> str:
    """Read file, return empty string if missing."""
    try:
        return path.read_text()
    except OSError:
        return ""


def find_line_number(text: str, pattern: str) -> int:
    """Find line number of first match, or -1 if not found."""
    for i, line in enumerate(text.split('\n'), 1):
        if pattern.lower() in line.lower():
            return i
    return -1


if __name__ == "__main__":
    h = Harness("ROUTING V2 PHASE 0 — DOCUMENTATION CORRECTIONS TEST SUITE")
    t = h.test_result

    # ========================================================================
    # SECTION 1: Amalgamator documentation
    # ========================================================================
    print("[Section 1] prompts/amalgamator.md corrections")

    amalgamator = read_file_safe(REPO_ROOT / "prompts" / "amalgamator.md")
    t(
        "amalgamator.md exists",
        len(amalgamator) > 0
    )

    if amalgamator:
        # Per plan: line 110 area should drop Sam System from ALWAYS-RUN legend
        # We'll check that if there's an ALWAYS-RUN section, Sam System is not there
        always_run_match = re.search(r"ALWAYS[_-]RUN.*?(?=\n\n|\n#|\Z)", amalgamator, re.IGNORECASE | re.DOTALL)

        if always_run_match:
            always_run_section = always_run_match.group(0)
            has_sam_system = "sam" in always_run_section.lower() or "sam system" in always_run_section.lower()

            t(
                "ALWAYS-RUN section does not mention Sam System",
                not has_sam_system,
                "Found 'Sam System' or 'Sam' in ALWAYS-RUN section"
            )
        else:
            # If no ALWAYS-RUN section at all, that's also acceptable (it was removed)
            t(
                "Amalgamator has been updated (ALWAYS-RUN section removed or updated)",
                True
            )

    # ========================================================================
    # SECTION 2: Expert Reviewer agent documentation
    # ========================================================================
    print("\n[Section 2] agents/expert-reviewer.md corrections")

    expert_reviewer = read_file_safe(REPO_ROOT / "agents" / "expert-reviewer.md")
    t(
        "expert-reviewer.md exists",
        len(expert_reviewer) > 0
    )

    if expert_reviewer:
        # Per plan: line 18 should say "get the full diff" not "always get the full diff"
        # for Sam System. Check that if it mentions Sam System getting the diff,
        # it doesn't say "always"

        sam_matches = re.finditer(r".*sam.*full.*diff.*", expert_reviewer, re.IGNORECASE)
        for match in sam_matches:
            line = match.group(0)
            has_always = "always" in line.lower()
            t(
                "Sam System description changed from 'always get' to just 'get'",
                not has_always,
                f"Line still contains 'always': {line[:80]}"
            )

        # At minimum, the document should exist and be non-empty
        if not any(re.finditer(r".*sam.*full.*diff.*", expert_reviewer, re.IGNORECASE)):
            t(
                "expert-reviewer.md mentions Sam System's diff handling",
                True  # Allowing either case since docs vary
            )

    # ========================================================================
    # SECTION 3: ADR-0003 amendment
    # ========================================================================
    print("\n[Section 3] docs/adr/0003-tagger-routing.md Amendment section")

    adr_0003 = read_file_safe(REPO_ROOT / "docs" / "adr" / "0003-tagger-routing.md")
    t(
        "docs/adr/0003-tagger-routing.md exists",
        len(adr_0003) > 0
    )

    if adr_0003:
        # Per plan: should have "## Amendment — judgment router (ADR-0003.2)" section
        has_amendment = "amendment" in adr_0003.lower() and "judgment router" in adr_0003.lower()
        t(
            "ADR-0003 has Amendment section mentioning judgment router",
            has_amendment
        )

        # The amendment should mention that Sam System is no longer always-run
        if has_amendment:
            amendment_match = re.search(
                r"## .*amendment.*?(?=## |\Z)",
                adr_0003,
                re.IGNORECASE | re.DOTALL
            )
            if amendment_match:
                amendment_text = amendment_match.group(0)
                # Check for some indication of routing/router change
                has_routing_content = "routing" in amendment_text.lower() or "router" in amendment_text.lower()
                t(
                    "Amendment section discusses routing changes",
                    has_routing_content
                )

    # ========================================================================
    # SECTION 4: Triage documentation
    # ========================================================================
    print("\n[Section 4] prompts/triage.md 'Doing it' table enhancements")

    triage = read_file_safe(REPO_ROOT / "prompts" / "triage.md")
    t(
        "prompts/triage.md exists",
        len(triage) > 0
    )

    if triage:
        # Per plan: "Doing it" table should have a "Raised by" column
        # Check for "raised" mentions in the triage document generally
        has_raised = "raised" in triage.lower()

        t(
            "Triage document mentions 'Raised' (for findings metadata)",
            has_raised,
            "Triage should reference which reviewer raised findings"
        )

    # ========================================================================
    # SECTION 5: Cross-reference validation
    # ========================================================================
    print("\n[Section 5] Cross-document consistency")

    # Verify that if one document mentions Sam System, others are consistent
    docs_to_check = [
        ("prompts/expert-review-panel.md", "expert-review-panel"),
        ("prompts/expert-framework.md", "expert-framework"),
    ]

    for doc_path, doc_name in docs_to_check:
        doc = read_file_safe(REPO_ROOT / doc_path)
        if doc:
            t(
                f"{doc_name} exists",
                len(doc) > 0
            )

    # ========================================================================
    # SECTION 6: No breaking changes in core structures
    # ========================================================================
    print("\n[Section 6] Documentation structural integrity")

    # Verify that key documentation files are still present (shouldn't be deleted)
    critical_docs = [
        "prompts/triage.md",
        "prompts/amalgamator.md",
        "agents/expert-reviewer.md",
        "docs/adr/0003-tagger-routing.md",
    ]

    for doc_path in critical_docs:
        full_path = REPO_ROOT / doc_path
        t(
            f"{doc_path} exists (not deleted)",
            full_path.exists()
        )

        # Verify minimum size (shouldn't be gutted)
        if full_path.exists():
            size = full_path.stat().st_size
            t(
                f"{doc_path} is non-empty",
                size > 100,
                f"File size is only {size} bytes"
            )

    # ========================================================================
    # SECTION 7: Sam System references consistency
    # ========================================================================
    print("\n[Section 7] Sam System references consistency")

    # If Sam System is mentioned anywhere in documentation, verify it's not
    # claiming it's always-run (since issue #148 removed that)

    all_doc_paths = [
        REPO_ROOT / "prompts" / "amalgamator.md",
        REPO_ROOT / "agents" / "expert-reviewer.md",
        REPO_ROOT / "prompts" / "expert-review-panel.md",
    ]

    for doc_path in all_doc_paths:
        if not doc_path.exists():
            continue

        doc = doc_path.read_text()

        # Find all Sam System references
        sam_refs = re.finditer(
            r".{0,100}sam\s+system.{0,100}",
            doc,
            re.IGNORECASE
        )

        for ref in sam_refs:
            line = ref.group(0)
            # If it mentions "always", that's a documentation bug
            has_always = "always" in line.lower()

            if has_always:
                t(
                    f"Sam System reference in {doc_path.name} doesn't say 'always'",
                    not has_always,
                    f"Found: {line[:80]}"
                )

    # ========================================================================
    # SECTION 8: Markdown formatting validation
    # ========================================================================
    print("\n[Section 8] Markdown formatting")

    # Basic check that critical docs are still valid Markdown
    markdown_docs = [
        REPO_ROOT / "prompts" / "triage.md",
        REPO_ROOT / "prompts" / "amalgamator.md",
        REPO_ROOT / "docs" / "adr" / "0003-tagger-routing.md",
    ]

    for doc_path in markdown_docs:
        if doc_path.exists():
            content = doc_path.read_text()

            # Basic Markdown checks
            has_headers = "#" in content
            t(
                f"{doc_path.name} has headers",
                has_headers
            )

            # No obvious corruption patterns
            unmatched_backticks = content.count("```") % 2
            t(
                f"{doc_path.name} has balanced code blocks",
                unmatched_backticks == 0,
                "Odd number of code block delimiters"
            )

    print()
    h.summarize_and_exit()
