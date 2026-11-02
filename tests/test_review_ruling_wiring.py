#!/usr/bin/env python3
"""
Text invariants for the review-ruling.py wiring in commands/expert-review.md (#408, D5).

These assert tokens, not sentences: the script name with decide and measure, the python3
allowed-tools entry, the absence of the old Edit-in-place procedure, the fallback exit codes,
and the Read-tool existence check (with no shell test on the script path).
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

EXPERT_REVIEW = REPO_ROOT / "commands" / "expert-review.md"
SCRIPT_TOKEN = "review-ruling.py"


def frontmatter(text: str) -> str:
    lines = text.split("\n")
    closing = [i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---"]
    return "\n".join(lines[: closing[0] + 1]) if closing else ""


def step_twelve(text: str) -> str:
    match = re.search(r"^### Step 12:.*?$(.*?)(?=^### Step 13)", text, re.DOTALL | re.MULTILINE)
    return match.group(0) if match else ""


def paragraph_with(section: str, *tokens: str) -> str:
    for paragraph in re.split(r"\n\s*\n", section):
        if all(token in paragraph for token in tokens):
            return paragraph
    return ""


def main() -> None:
    h = Harness("review-ruling.py Wiring Invariants (commands/expert-review.md)")
    text = EXPERT_REVIEW.read_text(encoding="utf-8")
    front = frontmatter(text)
    section = step_twelve(text)

    h.test_result("Step 12 section is found", section != "")

    h.test_result(
        "Step 12 names review-ruling.py with the decide subcommand",
        SCRIPT_TOKEN in section and re.search(r"review-ruling\.py\"?\s+decide\b", section) is not None,
    )
    h.test_result(
        "Step 12 names review-ruling.py with the measure subcommand",
        re.search(r"review-ruling\.py\"?\s+measure\b", section) is not None,
    )

    h.test_result(
        "frontmatter allowed-tools carries Bash(python3:*)",
        "Bash(python3:*)" in front,
    )

    h.test_result(
        "old numbered Edit-in-place procedure is gone",
        "**`Edit` `{REVIEW_DIR}/claude-action-plan.md` in place**" not in section
        and not any(re.search(r"\bEdit\b.*\bin place\b", line) for line in section.split("\n")),
    )

    fallback = paragraph_with(section, "Existence check and fallback")
    h.test_result("fallback paragraph is present", fallback != "")
    h.test_result("fallback condition names exit 126", "126" in fallback)
    h.test_result("fallback condition names exit 127", "127" in fallback)
    h.test_result(
        "fallback existence check names the Read tool with the review-ruling.py path",
        "`Read` tool" in fallback and SCRIPT_TOKEN in fallback and "limit: 1" in fallback,
    )
    h.test_result(
        "Step 12 uses no shell test (-f or test -f) on the review-ruling.py path",
        not any(
            SCRIPT_TOKEN in line and re.search(r"\[ -f|test -f", line)
            for line in section.split("\n")
        ),
    )
    h.test_result(
        "visible not-installed line is printed on fallback",
        "review-ruling.py not installed; run /setup-local" in section,
    )

    h.test_result(
        "stale `Edit` mechanism reference removed from the per-batch logging line",
        "recorded with the `Edit` (and only when" not in section,
    )

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
