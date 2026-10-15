#!/usr/bin/env python3
"""
Test suite for the receipt marker prose contract.

The receipt markers <!-- awaiting-answers: ... --> and <!-- reused-answers: ... --> must
survive on final OK/FAILED receipts. This test enforces that command docs and flow-reference.md
never tell agents to drop or remove markers, and that the canonical "keep" sentence appears at
all required sites.

Run with: python3 tests/test_flow_receipt_marker_contract.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT


def main():
    h = Harness("FLOW RECEIPT MARKER CONTRACT TEST SUITE")

    commands_dir = REPO_ROOT / "commands"
    flow_reference = REPO_ROOT / "prompts" / "flow-reference.md"

    print("[Test (a)] No drop language: commands and flow-reference")
    # Scan for drop/remove/strip language near marker references
    # Normalize whitespace across lines for wrapped sentences
    drop_verbs = r'\b(?:drop|remove|strip|delete)\b'
    marker_refs = r'(?:awaiting-answers|reused-answers|awaiting marker)'

    offenders = []

    # Check all command docs
    for doc in sorted(commands_dir.rglob("*.md")):
        content = doc.read_text()
        # Normalize whitespace (replace newlines + spaces with single space)
        normalized = re.sub(r'\s+', ' ', content)

        # Check for drop language near marker references
        for match in re.finditer(marker_refs, normalized):
            start = max(0, match.start() - 60)
            end = min(len(normalized), match.end() + 60)
            window = normalized[start:end]

            if re.search(drop_verbs, window):
                rel = doc.relative_to(REPO_ROOT)
                offenders.append(f"{rel}: drop/remove language near markers")
                break  # One offender per file

    # Check flow-reference.md
    if flow_reference.exists():
        content = flow_reference.read_text()
        normalized = re.sub(r'\s+', ' ', content)

        for match in re.finditer(marker_refs, normalized):
            start = max(0, match.start() - 60)
            end = min(len(normalized), match.end() + 60)
            window = normalized[start:end]

            if re.search(drop_verbs, window):
                offenders.append(f"prompts/flow-reference.md: drop/remove language near markers")
                break

    # Also check for "carries no marker" which is a specific forbidden phrase
    carries_no = []
    for doc in sorted(commands_dir.rglob("*.md")) + [flow_reference]:
        if not doc.exists():
            continue
        content = doc.read_text()
        if "carries no marker" in content:
            rel = doc.relative_to(REPO_ROOT)
            carries_no.append(str(rel))

    h.test_result(
        "no drop/remove/strip language near marker references",
        not offenders and not carries_no,
        "\n      " + "\n      ".join(offenders + carries_no) if offenders or carries_no else ""
    )

    # List exceptions that should NOT trigger (these are allowed because they don't refer to marker dropping)
    exceptions = {
        "cleanup.md": "worktree removal",
        "expert-rebase.md": "conflict markers",
        "merge-and-cleanup.md": ".claude/flow-run marker",
        "generate-style-guide.md": "bot markers",
    }

    print()
    print("[Test (b)] No forbidden-shape entry: 'Decision: together with awaiting marker'")

    if flow_reference.exists():
        content = flow_reference.read_text()
        # Check for the forbidden-shape entry that must be deleted
        if "together with an awaiting marker" in content:
            h.test_result(
                "forbidden-shape entry 'Decision: together with an awaiting marker' is deleted",
                False,
                "found: 'together with an awaiting marker' in Forbidden trailer shapes"
            )
        else:
            h.test_result(
                "forbidden-shape entry 'Decision: together with an awaiting marker' is deleted",
                True
            )
    else:
        h.test_result(
            "flow-reference.md exists",
            False,
            f"{flow_reference} not found"
        )

    print()
    print("[Test (c)] Canonical 'keep' sentence present at all seven sites")

    # The canonical sentence mentions "keep" and "flow-verify-receipt.py"
    keep_pattern = r'keep[^.]{0,200}?(?:awaiting-answers|reused-answers|awaiting marker).{0,250}?flow-verify-receipt'

    # Sites with their minimum count (shipit and implement-with-haiku need 2 each, others 1)
    sites = {
        "commands/expert-plan.md": 1,
        "commands/expert-review.md": 1,
        "commands/shipit.md": 2,
        "commands/implement-with-haiku.md": 2,
        "commands/merge-and-cleanup.md": 1,
    }

    missing_sites = []
    for path, min_count in sites.items():
        doc_path = REPO_ROOT / path
        if not doc_path.exists():
            missing_sites.append(f"{path}: file not found")
            continue

        content = doc_path.read_text()
        normalized = re.sub(r'\s+', ' ', content)
        matches = len(re.findall(keep_pattern, normalized, re.IGNORECASE))

        if matches < min_count:
            missing_sites.append(f"{path}: found {matches} canonical sentences, need {min_count}")

    h.test_result(
        "canonical 'keep' sentence present at all seven sites with correct counts",
        not missing_sites,
        "\n      " + "\n      ".join(missing_sites) if missing_sites else ""
    )

    print()
    print("[Test (d)] Guard the guard: synthetic old-wording string trips detector")

    # Synthetic string with drop language
    synthetic_old = "rewrite the whole file and drop its awaiting-answers marker lines"

    # Check if our detector would catch this
    drop_verbs = r'\b(?:drop|remove|strip|delete)\b'
    marker_refs = r'(?:awaiting-answers|reused-answers|awaiting marker)'
    normalized = re.sub(r'\s+', ' ', synthetic_old)

    # Check for drop language near marker references
    would_catch = False
    for match in re.finditer(marker_refs, normalized):
        start = max(0, match.start() - 60)
        end = min(len(normalized), match.end() + 60)
        window = normalized[start:end]

        if re.search(drop_verbs, window):
            would_catch = True
            break

    h.test_result(
        "synthetic old-wording string is detected as violation",
        would_catch,
        "detector should have caught the old wording"
    )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
