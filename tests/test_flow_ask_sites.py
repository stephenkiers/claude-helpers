#!/usr/bin/env python3
"""Ask-site lint for commands converted to the /expert-flow question relay (issue #247).

Every command in ROSTER below has been converted so that each place it would call
`AskUserQuestion` instead applies the **Ask** block from `prompts/flow-reference.md`
(which calls `AskUserQuestion` as written when not in a flow, and writes a question file
when it is). This lint fails on any line in a rostered command that mentions
`AskUserQuestion` without also referencing the Ask block — the signal that a new or
reworded ask-site was added without being wired to the relay.

Roster growth is one line per command: add the path when its conversion lands
(#248: expert-plan, expert-review; #249: implement-with-haiku, shipit; #250:
merge-and-cleanup, cleanup, stack-sync). Commands not on the roster are not scanned;
`prompts/expert-review-panel.md` is included explicitly because `/expert-review`'s
escalation ask lives there, and `prompts/shipit-reference.md` because `/shipit` lazy-loads it.

A line "references the Ask block" when it contains the token `Ask block` or the path
`flow-reference.md`. The frontmatter `allowed-tools:` line is exempt — it grants the tool, it
does not call it. Lines that say the tool is *not* used at a site (e.g. "do not call
AskUserQuestion", "never put to AskUserQuestion") are also exempt, since they are not ask-sites.

Run with: python3 tests/test_flow_ask_sites.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

# One line per converted command. Paths are repo-relative.
ROSTER = [
    "commands/expert-plan.md",
    "commands/expert-review.md",
    "prompts/expert-review-panel.md",
    "commands/implement-with-haiku.md",
    "commands/shipit.md",
    "prompts/shipit-reference.md",
    "commands/merge-and-cleanup.md",
    "commands/cleanup.md",
    "commands/stack-sync.md",
]

ASK_TOKEN = "AskUserQuestion"
ASK_BLOCK_REFS = ("Ask block", "flow-reference.md")

# Lines that mention the tool only to say it is not called at this site.
NEGATION_RE = re.compile(
    r"(do not call|don't call|never (?:call|put to|goes? through|relayed|use)|not (?:an? )?AskUserQuestion|"
    r"never put to|is not used|not AskUserQuestion|instead of AskUserQuestion|cannot call|"
    r"is stripped|stripped from|no AskUserQuestion)",
    re.IGNORECASE,
)


def is_exempt(line: str) -> bool:
    stripped = line.strip()
    if stripped.startswith("allowed-tools:"):
        return True
    if NEGATION_RE.search(stripped):
        return True
    return False


def references_ask_block(line: str) -> bool:
    return any(ref in line for ref in ASK_BLOCK_REFS)


def scan(path: Path):
    """Return (ask_site_count, offenders) for one file."""
    offenders = []
    sites = 0
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        if ASK_TOKEN not in line:
            continue
        if is_exempt(line):
            continue
        sites += 1
        if not references_ask_block(line):
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()[:120]}")
    return sites, offenders


def main():
    h = Harness("FLOW ASK-SITE LINT (prompts/flow-reference.md)")

    ref = REPO_ROOT / "prompts" / "flow-reference.md"
    h.test_result("prompts/flow-reference.md exists", ref.exists(), str(ref))
    if ref.exists():
        text = ref.read_text()
        for heading in ("## Resolve FLOW_DIR", "## Ask", "## Receipt"):
            h.test_result(f"flow-reference.md defines '{heading}'", heading in text)
        h.test_result(
            "flow-reference.md ends with its sentinel",
            text.rstrip().endswith("<!-- flow-reference-end -->"),
        )
        h.test_result(
            "flow-reference.md names the questions/answers file shape",
            "questions/<STEP_NAME>-<SEQ>.json" in text and "answers/<STEP_NAME>-<SEQ>.json" in text,
        )
        h.test_result(
            "flow-reference.md names the awaiting-answers receipt marker",
            "<!-- awaiting-answers:" in text,
        )
        h.test_result(
            "flow-reference.md tells the step to end its turn (no polling)",
            "END THE TURN" in text,
        )
        h.test_result(
            "flow-reference.md is a no-op when FLOW_DIR is empty",
            "FLOW_DIR` is empty" in text or "FLOW_DIR is empty" in text,
        )
        h.test_result(
            "flow-reference.md is not argument-substituted (lives in prompts/, not commands/)",
            not (REPO_ROOT / "commands" / "flow-reference.md").exists(),
        )

    print()
    print("[Section 2] Rostered commands reference the Ask block at every ask-site")

    total_sites = 0
    for rel in ROSTER:
        path = REPO_ROOT / rel
        if not path.exists():
            h.test_result(f"{rel} exists (rostered)", False, "missing")
            continue
        sites, offenders = scan(path)
        total_sites += sites
        h.test_result(
            f"{rel}: every AskUserQuestion mention references the Ask block ({sites} site(s))",
            not offenders,
            "\n      " + "\n      ".join(offenders) if offenders else "",
        )
        h.test_result(
            f"{rel}: references prompts/flow-reference.md at least once",
            "flow-reference.md" in path.read_text(),
        )

    # Guard the guard: a rostered command with zero ask-sites and zero reference would pass
    # vacuously; prove the detector fires on a synthetic bad line and passes a synthetic good one.
    bad = "Then call `AskUserQuestion` with the two options."
    good = "Then call `AskUserQuestion` via the Ask block (`prompts/flow-reference.md`) with the two options."
    h.test_result("detector flags a bare AskUserQuestion line", ASK_TOKEN in bad and not references_ask_block(bad))
    h.test_result("detector accepts a line that references the Ask block", references_ask_block(good))
    h.test_result(
        "detector exempts a negation line",
        is_exempt("Then stop and wait — do not call `AskUserQuestion`."),
    )
    h.test_result("roster is non-empty once conversions have landed", len(ROSTER) > 0)

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
