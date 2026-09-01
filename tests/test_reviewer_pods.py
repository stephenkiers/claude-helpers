#!/usr/bin/env python3
"""Contract tests for the effort-2 reviewer-pod path (GH #79)."""

import re
from pathlib import Path

from _test_harness import Harness, REPO_ROOT

h = Harness("Reviewer pods")
panel = (REPO_ROOT / "prompts/expert-review-panel.md").read_text()
pod_prompt = (REPO_ROOT / "prompts/reviewer-pod.md").read_text()
verifier = (REPO_ROOT / "prompts/pod-verifier.md").read_text()
cards_path = REPO_ROOT / "prompts/reviewer-lens-cards.yaml"
cards_text = cards_path.read_text()
routed_lenses = [
    "tara-typesafe", "contract-chris", "know-it-all-nigel", "sam-system", "mozart",
    "eric-evans", "rachel", "fragile-feynman", "vera-verifier",
]

h.test_result(
    "effort 2 diverts before ordinary reviewer pipeline",
    "If `EFFORT=2`, skip Steps 4–10 entirely" in panel
    and "Do not run the ordinary Router" in panel,
)
h.test_result(
    "exactly two pods launch in one capped batch",
    "launch exactly two" in panel.lower()
    and "maxConcurrentPodAgents: 2" in panel
    and "concurrency cap is two" in panel,
)

ordered = re.search(
    r"Pod 1.*?`tara-typesafe`, `contract-chris`, `know-it-all-nigel`.*?"
    r"`sam-system`, `mozart`, `eric-evans`, `rachel`,\s*"
    r"`fragile-feynman`, `vera-verifier`",
    panel,
    re.DOTALL,
)
h.test_result("nine named lenses are routed in fixed order", bool(ordered))
h.test_result(
    "compact cards exist for every routed lens",
    all(re.search(rf"^  {re.escape(lens)}:\s*$", cards_text, re.MULTILINE) for lens in routed_lenses),
)

required_card_keys = {"attribution", "look_for", "do_not_flag", "questions", "evidence"}
h.test_result(
    "every compact lens card has the detection contract",
    all(
        all(
            re.search(rf"^    {key}:", cards_text[cards_text.index(f"  {lens}:"):], re.MULTILINE)
            for key in required_card_keys
        )
        for lens in routed_lenses
    ),
)
h.test_result(
    "every lens requires attribution and explicit NO_FINDING",
    bool(re.search(r"every\s+assigned lens key exactly once", panel))
    and "explicit `NO_FINDING`" in panel
    and "attribution and either a non-empty `findings` list or the scalar `NO_FINDING`" in pod_prompt,
)
h.test_result(
    "deduplication happens after independent lens passes",
    "Only after every lens pass is written may you deduplicate" in pod_prompt,
)

packet_files = {
    "manifest.json",
    "changed-symbols.json",
    "selected-hunks.patch",
    "project-constraints.md",
    "relevant-adrs.md",
    "deterministic-findings.json",
}
h.test_result(
    "neutral shared packet contains all required artifacts",
    all(name in panel for name in packet_files)
    and "sole shared framework/diff/" in panel,
)
h.test_result(
    "one scout batches all reviewer questions",
    bool(re.search(r"launch exactly\s+one `expert-scout`", panel, re.IGNORECASE))
    and "There is never one Q&A scout per lens or pod" in panel,
)
h.test_result(
    "neutral verifier batches Pass 2",
    "Batched neutral Pass 2" in panel
    and bool(re.search(r"do not\s+launch per-lens Pass 2 agents", panel)),
)
h.test_result(
    "specialist promotion is restricted",
    "grounded uncertain/disputed High/Critical" in panel
    and "Do not promote Medium/Low" in panel
    and "explicit high-risk tag" in verifier,
)
h.test_result(
    "verifier classifies into all four labels and stays neutral",
    "Classify each item `CONFIRMED`, `RESOLVED`, `DOWNGRADED`, or `DISPUTED`" in verifier
    and "Do not introduce new findings and do not favor a" in verifier,
)
h.test_result(
    "pod and verifier artifacts retain injection and truncation guards",
    "untrusted data" in pod_prompt
    and "<!-- pod-end -->" in pod_prompt
    and "untrusted data" in verifier
    and "<!-- pod-verification-end -->" in verifier,
)

metrics = {
    "modelCalls",
    "inputTokens",
    "outputTokens",
    "maximumContextTokens",
    "wallTimeMs",
    "findingsAccepted",
    "findingsRejected",
    "uniqueHigherLevelFindings",
}
h.test_result("evaluation metrics cover the issue contract", all(key in panel for key in metrics))
h.test_result(
    "effort levels 4 and 5 retain the independent path",
    "Effort levels 3–5 continue through Steps 4–10" in panel,
)

h.summarize_and_exit()
