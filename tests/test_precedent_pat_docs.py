#!/usr/bin/env python3
"""
Test suite for Precedent Pat role prompt, ADR-0024, and ADR-0007 amendment.

Plan: Precedent Pat — role prompt, ADR-0024, and ADR-0007 escape-hatch amendment (#293)
- Adds prompts/precedent-pat.md: the role mandate
- Adds docs/adr/0024-precedent-pat.md: status Proposed
- Amends docs/adr/0007-triage-and-decision-memory.md with escape-hatch amendment
- Updates docs/adr/README.md with 0024 entry
- Updates CLAUDE.md with Precedent Pat section
- Updates agents/expert-reviewer.md to list precedent-pat.md
- Writes tests/test_precedent_pat_docs.py

Run with: python3 tests/test_precedent_pat_docs.py
"""

import re
from _test_harness import REPO_ROOT, Harness

PROMPTS_DIR = REPO_ROOT / "prompts"
DOCS_ADR_DIR = REPO_ROOT / "docs" / "adr"
AGENTS_DIR = REPO_ROOT / "agents"

if __name__ == "__main__":
    h = Harness("PRECEDENT PAT DOCUMENTATION TEST SUITE")
    test_result = h.test_result

    # ============================================================================
    # SECTION 1: prompts/precedent-pat.md
    # ============================================================================
    print("[Section 1] prompts/precedent-pat.md")

    precedent_pat_file = PROMPTS_DIR / "precedent-pat.md"
    precedent_pat_content = precedent_pat_file.read_text() if precedent_pat_file.exists() else ""

    test_result(
        "precedent-pat.md exists",
        precedent_pat_file.exists(),
        "File not found"
    )

    # 1.1: Receipt line format should be present in the output grammar
    # Should contain the format: precedent-pat | questions: {n} | spoke-on: {k} | citations: {m} | confidence: high={a} medium={b}
    receipt_format = "precedent-pat |" in precedent_pat_content and "questions:" in precedent_pat_content
    test_result(
        "Receipt line format present in output grammar",
        receipt_format,
        "Found no 'precedent-pat |' format line with 'questions:' in precedent-pat.md"
    )

    # 1.2: Pinned receipt regex pattern - check for the pattern in the prompt
    # Pattern: ^precedent-pat \| questions: \d+ \| spoke-on: \d+ \| citations: \d+ \| confidence: high=\d+ medium=\d+$
    receipt_regex_pattern = r"^precedent-pat \| questions: \d+ \| spoke-on: \d+ \| citations: \d+ \| confidence: high=\d+ medium=\d+$"

    # Find any receipt lines that could be worked examples (could have placeholders or real numbers)
    receipt_lines = [line for line in precedent_pat_content.split('\n') if 'precedent-pat |' in line]

    # Check if at least one matches the real regex (with actual numbers)
    real_receipt_matches = [line for line in receipt_lines if re.match(receipt_regex_pattern, line.strip())]

    test_result(
        "Pinned receipt regex matches a worked example in the prompt",
        len(real_receipt_matches) > 0,
        f"No receipt line in the prompt matches the real-data pattern: {receipt_regex_pattern}"
    )

    # 1.3: Distinguish mandatory - should contain "Distinguish" with mandatory context
    distinguish_present = "Distinguish" in precedent_pat_content
    test_result(
        "Distinguish mentioned in prompt",
        distinguish_present,
        "Found no 'Distinguish' in precedent-pat.md"
    )

    # 1.4: Mandatory Distinguish requirement - check for phrases like "mandatory" or "always" with Distinguish
    distinguish_mandatory_variants = [
        "Distinguish is mandatory",
        "mandatory Distinguish",
        "Distinguish line is mandatory",
        "Distinguish: mandatory",
        "Distinguish when Pat speaks",
        "spoken question has a Distinguish",
        "Distinguish line, and then"
    ]

    distinguish_mandatory = any(variant in precedent_pat_content for variant in distinguish_mandatory_variants)
    test_result(
        "Distinguish mandatory wording present",
        distinguish_mandatory,
        "Found 'Distinguish' but no mandatory wording like 'mandatory Distinguish' or similar"
    )

    # 1.5: Confidence rule - medium or above, two consistent priors
    medium_rule_present = "medium" in precedent_pat_content.lower()
    test_result(
        "Confidence rule mentions medium",
        medium_rule_present,
        "Found no mention of 'medium' confidence level"
    )

    # Check for "two consistent priors" or similar
    priors_mentioned = "prior" in precedent_pat_content.lower() or "priors" in precedent_pat_content.lower()
    test_result(
        "Priors mentioned in confidence rule",
        priors_mentioned,
        "Found no mention of 'prior' or 'priors' for high confidence"
    )

    # 1.6: Curated rule reserved until #297
    curated_rule_reserved = "#297" in precedent_pat_content or "reserved until" in precedent_pat_content
    test_result(
        "Curated rule marked reserved until #297",
        curated_rule_reserved,
        "Found no mention of curated rule being reserved until #297"
    )

    # 1.7: Five-citation ceiling
    citation_ceiling = "5" in precedent_pat_content or "five" in precedent_pat_content.lower()
    test_result(
        "Five-citation ceiling mentioned",
        citation_ceiling,
        "Found no mention of 5-citation ceiling"
    )

    # 1.8: (silent — nothing related) literal
    silent_literal = "(silent — nothing related)" in precedent_pat_content
    test_result(
        "Silent literal '(silent — nothing related)' present",
        silent_literal,
        "Found no '(silent — nothing related)' literal in prompt"
    )

    # 1.9: Data-not-instructions sentence covering both digest and pending questions
    data_not_instructions_present = "data" in precedent_pat_content.lower() and "instruction" in precedent_pat_content.lower()
    test_result(
        "Data-not-instructions sentence present",
        data_not_instructions_present,
        "Found no 'data' and 'instruction' keywords in prompt"
    )

    # Check for coverage of both inputs in the rule
    both_inputs_covered = (
        ("digest" in precedent_pat_content.lower() and "question" in precedent_pat_content.lower()) or
        ("digest" in precedent_pat_content.lower() and "input" in precedent_pat_content.lower())
    )
    test_result(
        "Data-not-instructions covers both digest and questions",
        both_inputs_covered,
        "Found data-not-instructions rule but doesn't mention both digest and questions"
    )

    # 1.10: Single-output-path Write sentence
    single_write_present = "exactly one file" in precedent_pat_content or "single" in precedent_pat_content.lower() and "output path" in precedent_pat_content.lower()
    test_result(
        "Single-output-path Write sentence present",
        single_write_present,
        "Found no mention of writing exactly one file"
    )

    # 1.11: Never-open-path rule
    never_open_path = "path" in precedent_pat_content.lower() and ("never open" in precedent_pat_content.lower() or "do not open" in precedent_pat_content.lower())
    test_result(
        "Never-open-path rule present",
        never_open_path,
        "Found no rule about not opening the path field"
    )

    print()

    # ============================================================================
    # SECTION 2: docs/adr/0024-precedent-pat.md
    # ============================================================================
    print("[Section 2] docs/adr/0024-precedent-pat.md")

    adr_0024_file = DOCS_ADR_DIR / "0024-precedent-pat.md"
    adr_0024_content = adr_0024_file.read_text() if adr_0024_file.exists() else ""

    test_result(
        "0024-precedent-pat.md exists",
        adr_0024_file.exists(),
        "File not found"
    )

    # 2.1: Status should be Proposed
    status_proposed = "**Status:** Proposed" in adr_0024_content or "Status: Proposed" in adr_0024_content
    test_result(
        "ADR-0024 status is Proposed",
        status_proposed,
        "Found no 'Status: Proposed' in ADR-0024"
    )

    print()

    # ============================================================================
    # SECTION 3: docs/adr/0007-triage-and-decision-memory.md amendments
    # ============================================================================
    print("[Section 3] docs/adr/0007 amendments")

    adr_0007_file = DOCS_ADR_DIR / "0007-triage-and-decision-memory.md"
    adr_0007_content = adr_0007_file.read_text() if adr_0007_file.exists() else ""

    test_result(
        "0007-triage-and-decision-memory.md exists",
        adr_0007_file.exists(),
        "File not found"
    )

    # 3.1: Escape hatch amendment heading
    escape_hatch_heading = "## Amendment — Escape hatch exercised by ADR-0024" in adr_0007_content
    test_result(
        "ADR-0007 has escape-hatch amendment heading",
        escape_hatch_heading,
        "Found no '## Amendment — Escape hatch exercised by ADR-0024' heading in ADR-0007"
    )

    # 3.2: Forward pointer in the escape hatch paragraph
    # Should be near line 238 in the chore/29 Escape hatch paragraph
    forward_pointer = "Exercised by ADR-0024" in adr_0007_content and "see the amendment" in adr_0007_content.lower()
    test_result(
        "ADR-0007 has forward-pointer to ADR-0024",
        forward_pointer,
        "Found no forward pointer mentioning 'Exercised by ADR-0024' and amendment reference"
    )

    print()

    # ============================================================================
    # SECTION 4: docs/adr/README.md
    # ============================================================================
    print("[Section 4] docs/adr/README.md")

    adr_readme_file = DOCS_ADR_DIR / "README.md"
    adr_readme_content = adr_readme_file.read_text() if adr_readme_file.exists() else ""

    test_result(
        "README.md exists",
        adr_readme_file.exists(),
        "File not found"
    )

    # 4.1: ADR-0024 listed in the index
    adr_0024_listed = "ADR-0024" in adr_readme_content and "precedent" in adr_readme_content.lower()
    test_result(
        "ADR-0024 listed in README index",
        adr_0024_listed,
        "Found no ADR-0024 entry in docs/adr/README.md"
    )

    print()

    # ============================================================================
    # SECTION 5: CLAUDE.md
    # ============================================================================
    print("[Section 5] CLAUDE.md")

    claude_md_file = REPO_ROOT / "CLAUDE.md"
    claude_md_content = claude_md_file.read_text() if claude_md_file.exists() else ""

    test_result(
        "CLAUDE.md exists",
        claude_md_file.exists(),
        "File not found"
    )

    # 5.1: Precedent Pat mentioned
    precedent_pat_mentioned = "Precedent Pat" in claude_md_content
    test_result(
        "CLAUDE.md mentions Precedent Pat",
        precedent_pat_mentioned,
        "Found no 'Precedent Pat' mention in CLAUDE.md"
    )

    # 5.2: Reference to ADR-0024 in CLAUDE.md (should be in How the review system works section)
    adr_0024_in_claude = "ADR-0024" in claude_md_content or "0024" in claude_md_content
    test_result(
        "CLAUDE.md references ADR-0024",
        adr_0024_in_claude,
        "Found no ADR-0024 reference in CLAUDE.md"
    )

    print()

    # ============================================================================
    # SECTION 6: agents/expert-reviewer.md
    # ============================================================================
    print("[Section 6] agents/expert-reviewer.md")

    expert_reviewer_file = AGENTS_DIR / "expert-reviewer.md"
    expert_reviewer_content = expert_reviewer_file.read_text() if expert_reviewer_file.exists() else ""

    test_result(
        "expert-reviewer.md exists",
        expert_reviewer_file.exists(),
        "File not found"
    )

    # 6.1: precedent-pat.md listed in role prompts
    precedent_pat_listed = "precedent-pat.md" in expert_reviewer_content
    test_result(
        "expert-reviewer.md lists precedent-pat.md",
        precedent_pat_listed,
        "Found no 'precedent-pat.md' reference in agents/expert-reviewer.md"
    )

    print()

    # ============================================================================
    # SECTION 7: Cross-checks and integrity
    # ============================================================================
    print("[Section 7] Cross-file integrity checks")

    # 7.1: If a receipt line with real numbers exists, verify it matches the regex
    # (The worked example in the prompt may use placeholders like {n}, {k}, etc.)
    if real_receipt_matches:
        receipt_line = real_receipt_matches[0]
        test_result(
            "Receipt line with real numbers matches pinned regex",
            re.match(receipt_regex_pattern, receipt_line.strip()),
            f"Receipt line does not match pattern: {receipt_line}"
        )

        # 7.2: Verify the example's counts are consistent (questions, spoke-on, citations, confidence fields)
        # Extract counts from receipt line using the regex
        import re as regex_mod
        counts_match = regex_mod.search(
            r'questions: (\d+) \| spoke-on: (\d+) \| citations: (\d+) \| confidence: high=(\d+) medium=(\d+)',
            receipt_line
        )
        if counts_match:
            n_questions = int(counts_match.group(1))
            n_spoke_on = int(counts_match.group(2))
            n_citations = int(counts_match.group(3))
            n_high = int(counts_match.group(4))
            n_medium = int(counts_match.group(5))

            # Sanity checks: high + medium should equal spoke-on
            confidence_sum_ok = (n_high + n_medium) == n_spoke_on
            test_result(
                "Receipt confidence counts sum to spoke-on count",
                confidence_sum_ok,
                f"high({n_high}) + medium({n_medium}) != spoke-on({n_spoke_on})"
            )

            # Spoke-on should not exceed questions
            spoke_on_ok = n_spoke_on <= n_questions
            test_result(
                "Receipt spoke-on does not exceed questions",
                spoke_on_ok,
                f"spoke-on({n_spoke_on}) > questions({n_questions})"
            )

            # Citations should not exceed 5
            citations_ok = n_citations <= 5
            test_result(
                "Receipt citations does not exceed ceiling of 5",
                citations_ok,
                f"citations({n_citations}) > 5"
            )

    print()

    # ============================================================================
    # SECTION 7.3: [WEAK_ASSERTION] Validate receipt counts against example bodies
    # ============================================================================
    print("[Section 7.3] [WEAK_ASSERTION] Receipt count validation against example bodies")

    # Find all fenced code blocks with receipt lines (worked examples)
    # Pattern: look for ```markdown blocks containing "precedent-pat |" with numbers
    fenced_example_pattern = r'```markdown\n(#.*?\n.*?precedent-pat \| questions: \d+.*?```)'
    fenced_examples = re.findall(fenced_example_pattern, precedent_pat_content, re.DOTALL)

    if fenced_examples:
        for idx, example in enumerate(fenced_examples, 1):
            # Extract receipt line from example
            receipt_match = re.search(
                r'precedent-pat \| questions: (\d+) \| spoke-on: (\d+) \| citations: (\d+) \| confidence: high=(\d+) medium=(\d+)',
                example
            )
            if not receipt_match:
                continue

            receipt_n_questions = int(receipt_match.group(1))
            receipt_n_spoke_on = int(receipt_match.group(2))
            receipt_n_citations = int(receipt_match.group(3))
            receipt_n_high = int(receipt_match.group(4))
            receipt_n_medium = int(receipt_match.group(5))

            # Count Q headings (## Q{n})
            q_headings = re.findall(r'^## Q\d+', example, re.MULTILINE)
            body_n_questions = len(q_headings)

            # Count **Precedent** lines
            precedent_lines = re.findall(r'^\*\*Precedent\*\*:', example, re.MULTILINE)
            body_n_citations = len(precedent_lines)

            # Count non-silent blocks (blocks with at least one **Precedent** line)
            # A non-silent block is a section starting with ## Q that contains **Precedent**
            q_blocks = re.findall(r'^## Q\d+.*?(?=^## Q|\Z)', example, re.MULTILINE | re.DOTALL)
            body_n_spoke_on = sum(1 for block in q_blocks if '**Precedent**' in block)

            # Count **Confidence**: high and **Confidence**: medium lines
            high_lines = re.findall(r'^\*\*Confidence\*\*: high', example, re.MULTILINE)
            medium_lines = re.findall(r'^\*\*Confidence\*\*: medium', example, re.MULTILINE)
            body_n_high = len(high_lines)
            body_n_medium = len(medium_lines)

            # Test: questions count matches
            example_label = f"Example {idx}"
            test_result(
                f"[WEAK_ASSERTION] {example_label}: questions count matches body (receipt={receipt_n_questions}, body={body_n_questions})",
                receipt_n_questions == body_n_questions,
                f"Receipt says {receipt_n_questions} questions, but found {body_n_questions} ## Q headings"
            )

            # Test: citations count matches
            test_result(
                f"[WEAK_ASSERTION] {example_label}: citations count matches body (receipt={receipt_n_citations}, body={body_n_citations})",
                receipt_n_citations == body_n_citations,
                f"Receipt says {receipt_n_citations} citations, but found {body_n_citations} **Precedent** lines"
            )

            # Test: spoke-on count matches
            test_result(
                f"[WEAK_ASSERTION] {example_label}: spoke-on count matches body (receipt={receipt_n_spoke_on}, body={body_n_spoke_on})",
                receipt_n_spoke_on == body_n_spoke_on,
                f"Receipt says {receipt_n_spoke_on} spoke-on, but found {body_n_spoke_on} non-silent blocks"
            )

            # Test: high count matches
            test_result(
                f"[WEAK_ASSERTION] {example_label}: high confidence count matches body (receipt={receipt_n_high}, body={body_n_high})",
                receipt_n_high == body_n_high,
                f"Receipt says {receipt_n_high} high, but found {body_n_high} **Confidence**: high lines"
            )

            # Test: medium count matches
            test_result(
                f"[WEAK_ASSERTION] {example_label}: medium confidence count matches body (receipt={receipt_n_medium}, body={body_n_medium})",
                receipt_n_medium == body_n_medium,
                f"Receipt says {receipt_n_medium} medium, but found {body_n_medium} **Confidence**: medium lines"
            )

            # Test: high + medium == spoke-on
            test_result(
                f"[WEAK_ASSERTION] {example_label}: high + medium == spoke-on ({body_n_high} + {body_n_medium} == {body_n_spoke_on})",
                (body_n_high + body_n_medium) == body_n_spoke_on,
                f"high({body_n_high}) + medium({body_n_medium}) != spoke-on({body_n_spoke_on})"
            )
    else:
        test_result(
            "[WEAK_ASSERTION] Fenced examples with receipt lines found",
            False,
            "Could not find any fenced code blocks with receipt lines in prompts/precedent-pat.md"
        )

    print()

    h.summarize_and_exit()
