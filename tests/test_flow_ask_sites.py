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
does not call it. An occurrence of the token is exempt only when a negation phrase sits
immediately before it (e.g. "do not call `AskUserQuestion`", "never put to AskUserQuestion"):
those mention the tool to say it is not called at that site. A negation elsewhere on the line
does not exempt a real ask-site that appears later on the same line.

Run with: python3 tests/test_flow_ask_sites.py
"""

import re
import sys
import tempfile
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

# Explicit, narrow allowlist of negation lines whose negation follows the token rather than
# precedes it. Keyed by (repo-relative path, stripped line text) so an edit to the line
# re-activates the lint. Add an entry only for a line that is genuinely not an ask-site.
EXPLICIT_NEGATION_ALLOWLIST = {
    ("commands/expert-review.md", "`AskUserQuestion` and is never relayed in a flow."),
}

# A negation phrase immediately before the tool token (optionally separated by
# whitespace, backticks, emphasis markers, quotes, or an opening paren). Only these
# occurrences are "the tool is not called here" mentions; anything else is a site.
NEGATED_TOKEN_RE = re.compile(
    r"(?:do not call|don't call|never (?:call|put to|goes? through|relayed|use)|never put to|"
    r"is not used|not (?:an? )?|instead of|cannot call|is stripped from|stripped from|no)"
    r"[\s`*'\"(]*" + re.escape(ASK_TOKEN),
    re.IGNORECASE,
)

# An explicit exemption marker: a comment containing `flow-ask-exempt:` signals that any
# AskUserQuestion on this line should not be flagged, even if it has no negation.
EXPLICIT_EXEMPT_MARKER = "flow-ask-exempt:"


def is_exempt(line: str) -> bool:
    """The frontmatter allowed-tools grant is not a call site."""
    return line.strip().startswith("allowed-tools:")


def has_live_ask_token(line: str) -> bool:
    """True when the line has an AskUserQuestion mention that is not immediately negated."""
    return ASK_TOKEN in NEGATED_TOKEN_RE.sub("", line)


def has_explicit_exempt_marker(line: str) -> bool:
    """True when the line contains an explicit flow-ask-exempt: marker."""
    return EXPLICIT_EXEMPT_MARKER in line


def references_ask_block(line: str) -> bool:
    return any(ref in line for ref in ASK_BLOCK_REFS)


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def is_allowlisted(path: Path, line: str) -> bool:
    try:
        rel = path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return False
    return (rel, line.strip()) in EXPLICIT_NEGATION_ALLOWLIST


def scan(path: Path):
    """Return (ask_site_count, offenders) for one file."""
    offenders = []
    sites = 0
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        if ASK_TOKEN not in line:
            continue
        if is_exempt(line):
            continue
        if not has_live_ask_token(line) or is_allowlisted(path, line):
            continue
        if has_explicit_exempt_marker(line):
            continue
        sites += 1
        if not references_ask_block(line):
            offenders.append(f"{_display(path)}:{lineno}: {line.strip()[:120]}")
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
    h.test_result("roster scan found ask-sites (guard is not vacuous)", total_sites > 0,
                  f"total_sites={total_sites}")

    # Guard the guard: run the real scan() over temp-file fixtures so a detector that
    # never fires (or always fires) cannot pass vacuously.
    print()
    print("[Section 3] Detector fixtures (scan() on temp files)")
    bad_line = "Then call `AskUserQuestion` with the two options."
    good_line = "Then call `AskUserQuestion` via the Ask block (`prompts/flow-reference.md`) with the two options."
    negation_line = "Then stop and wait — do not call `AskUserQuestion`."
    mixed_line = ("Do not call `AskUserQuestion` for the sweep, but then call `AskUserQuestion` "
                  "with the two options.")
    exempt_marker_line = "Then call `AskUserQuestion`. <!-- flow-ask-exempt: this is a test utility -->"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        bad_file = tmp_dir / "bad.md"
        bad_file.write_text(bad_line + "\n" + negation_line + "\n")
        good_file = tmp_dir / "good.md"
        good_file.write_text(good_line + "\n" + negation_line + "\n")
        mixed_file = tmp_dir / "mixed.md"
        mixed_file.write_text(mixed_line + "\n")
        negation_file = tmp_dir / "negation.md"
        negation_file.write_text(negation_line + "\n")
        exempt_file = tmp_dir / "exempt.md"
        exempt_file.write_text(exempt_marker_line + "\n")

        bad_sites, bad_off = scan(bad_file)
        h.test_result("scan() flags a bare AskUserQuestion site and ignores the negation line",
                      bad_sites == 1 and len(bad_off) == 1,
                      f"sites={bad_sites} offenders={len(bad_off)}")
        good_sites, good_off = scan(good_file)
        h.test_result("scan() accepts a site that references the Ask block",
                      good_sites == 1 and not good_off,
                      f"sites={good_sites} offenders={len(good_off)}")
        h.test_result("scan() exempts a line whose only mention is negated",
                      scan(negation_file) == (0, []))
        mixed_sites, mixed_off = scan(mixed_file)
        h.test_result("negation elsewhere on the line does not hide a real site (flagged)",
                      mixed_sites == 1 and len(mixed_off) == 1,
                      f"sites={mixed_sites} offenders={len(mixed_off)}")
        exempt_sites, exempt_off = scan(exempt_file)
        h.test_result("scan() exempts a site marked with flow-ask-exempt:",
                      exempt_sites == 0 and not exempt_off,
                      f"sites={exempt_sites} offenders={len(exempt_off)}")
        h.test_result("detector fixtures produce ask-sites (guard is not vacuous)",
                      bad_sites + good_sites + mixed_sites > 0)
    h.test_result("roster is non-empty once conversions have landed", len(ROSTER) > 0)

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
