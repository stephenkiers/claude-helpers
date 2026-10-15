#!/usr/bin/env python3
"""
Test suite for the receipt marker prose contract.

The receipt markers <!-- awaiting-answers: ... --> and <!-- reused-answers: ... --> must
survive on final OK/FAILED receipts. This test enforces that every doc which references
`awaiting-answers` and writes a step receipt carries the canonical "keep" sentence (a
positive requirement, not a verb denylist), and that the explicit exemptions list stays
honest: each exempt doc must still reference the marker and must have a documented reason.

Run with: python3 tests/test_flow_receipt_marker_contract.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT


# Docs that reference `awaiting-answers` but do NOT author a step receipt, so they are not
# required to carry the canonical keep sentence. Each entry must still reference the marker
# (stale entries fail the test). Reasons are taken from the docs' own wording.
MARKER_EXEMPTIONS = {
    "commands/cleanup.md": (
        "writes no receipt of its own; its Ask appends the marker to the merge step's receipt "
        "(\"/cleanup writes **no receipt** of its own\")"
    ),
    "commands/stack-sync.md": (
        "writes no receipt of its own; a relayed ask's marker goes into the calling step's "
        "receipt (\"/stack-sync writes **no receipt** of its own\")"
    ),
    "commands/expert-flow.md": (
        "orchestrator, not a step author: it reads markers to re-ask pending questions and "
        "stubs the receipt on a re-run, where the old awaiting marker is stale by design"
    ),
}

# The canonical "keep" sentence: a `keep`/`keeping` instruction that names a marker and
# points at flow-verify-receipt.py. Whitespace is normalized before matching.
KEEP_PATTERN = re.compile(
    r"keep[^.]{0,200}?(?:awaiting-answers|reused-answers|awaiting marker).{0,250}?flow-verify-receipt",
    re.IGNORECASE,
)

# Every phrase by which a doc refers to a receipt marker (the same alternation the old drop check used).
MARKER_REF = re.compile(r"awaiting-answers|reused-answers|awaiting marker")

# Drop/remove language: checked in a 60-character window around each marker reference.
DROP_VERBS = re.compile(r"\b(?:drop|remove|strip|delete)\b")


def has_keep_sentence(text):
    return bool(KEEP_PATTERN.search(re.sub(r"\s+", " ", text)))


def has_drop_language_near_marker(text):
    normalized = re.sub(r"\s+", " ", text)
    for match in MARKER_REF.finditer(normalized):
        window = normalized[max(0, match.start() - 60):min(len(normalized), match.end() + 60)]
        if DROP_VERBS.search(window):
            return True
    return False


def marker_docs(commands_dir, flow_reference):
    """Every command doc and prompts/flow-reference.md that references a receipt marker."""
    docs = sorted(commands_dir.rglob("*.md"))
    if flow_reference.exists():
        docs.append(flow_reference)
    return [d for d in docs if MARKER_REF.search(d.read_text())]


def main():
    h = Harness("FLOW RECEIPT MARKER CONTRACT TEST SUITE")

    commands_dir = REPO_ROOT / "commands"
    flow_reference = REPO_ROOT / "prompts" / "flow-reference.md"

    print("[Test (a)] Every marker-referencing receipt writer carries the canonical 'keep' sentence")

    referencing = marker_docs(commands_dir, flow_reference)
    referencing_rel = {str(d.relative_to(REPO_ROOT)) for d in referencing}

    missing_keep = []
    for doc in referencing:
        rel = str(doc.relative_to(REPO_ROOT))
        if rel in MARKER_EXEMPTIONS:
            continue
        if not has_keep_sentence(doc.read_text()):
            missing_keep.append(f"{rel}: references awaiting-answers but lacks the canonical keep sentence")

    stale_exemptions = [
        f"{rel}: exemption is stale (no longer references awaiting-answers; remove it)"
        for rel in MARKER_EXEMPTIONS
        if rel not in referencing_rel
    ]

    # Specific forbidden phrase retained from the earlier contract.
    carries_no = []
    for doc in sorted(commands_dir.rglob("*.md")) + [flow_reference]:
        if doc.exists() and "carries no marker" in doc.read_text():
            carries_no.append(f"{doc.relative_to(REPO_ROOT)}: contains forbidden phrase 'carries no marker'")

    failures = missing_keep + stale_exemptions + carries_no
    h.test_result(
        "every marker-referencing receipt writer has the canonical keep sentence (or a documented exemption)",
        not failures,
        "\n      " + "\n      ".join(failures) if failures else "",
    )

    h.test_result(
        "marker exemptions each carry a documented reason",
        all(reason.strip() for reason in MARKER_EXEMPTIONS.values()),
        "",
    )

    # Second assertion beside the positive keep check: no drop/remove/strip/delete language near a marker.
    drop_offenders = [
        f"{doc.relative_to(REPO_ROOT)}: drop/remove language near markers"
        for doc in referencing
        if has_drop_language_near_marker(doc.read_text())
    ]
    h.test_result(
        "no drop/remove/strip/delete language near receipt marker references",
        not drop_offenders,
        "\n      " + "\n      ".join(drop_offenders) if drop_offenders else "",
    )

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
    print("[Test (c)] Canonical 'keep' sentence present at all required sites")

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

        normalized = re.sub(r'\s+', ' ', doc_path.read_text())
        matches = len(KEEP_PATTERN.findall(normalized))

        if matches < min_count:
            missing_sites.append(f"{path}: found {matches} canonical sentences, need {min_count}")

    h.test_result(
        "canonical 'keep' sentence present at required sites with correct counts",
        not missing_sites,
        "\n      " + "\n      ".join(missing_sites) if missing_sites else ""
    )

    print()
    print("[Test (d)] Guard the guard: detector flags a receipt writer without the keep sentence")

    # A writer that mentions the marker but never tells the agent to keep it must be flagged.
    synthetic_without = "Append `<!-- awaiting-answers: questions/X.json -->` to the receipt."
    # A writer carrying the canonical sentence must pass.
    synthetic_with = (
        "rewrite the whole file and **keep** every `<!-- awaiting-answers: … -->` line it holds "
        "— `flow-verify-receipt.py` counts relayed questions from them."
    )

    h.test_result(
        "synthetic writer without the keep sentence is flagged",
        not has_keep_sentence(synthetic_without),
        "detector should have flagged a writer lacking the keep sentence",
    )
    h.test_result(
        "synthetic writer with the canonical keep sentence passes",
        has_keep_sentence(synthetic_with),
        "detector should have accepted the canonical keep sentence",
    )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
