#!/usr/bin/env python3
"""
ROUND 1 section-header contract for implement/fix receipts (#341, item 5, #322).

The fixtures in tests/fixtures/round1/ are trimmed, scrubbed copies of real receipts under
~/.claude/flows/ (positive/real-*.md, negative/real-*.md) and synthetic paraphrases of the same
shapes (positive/synth-*.md, negative/synth-*.md). This test reads only those fixtures, never
~/.claude.

Each test runs the verifier's module-level pattern (ROUND1_HEADER_RE) against the fixtures, and
each revert test runs a mutant pattern to show the fixtures would catch that revert:

- strict `^ROUND 1\\b` (the pre-#341 check): the paraphrase positives must fail under it.
- separator requirement removed: the status-line negative must wrongly pass under it.
- line anchor removed: the prose-only negative must wrongly pass under it.

Run with: python3 tests/test_flow_verify_receipt_round1.py
"""

import importlib.util
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness

script_path = Path(__file__).parent.parent / "scripts" / "flow-verify-receipt.py"
spec = importlib.util.spec_from_file_location("flow_verify_receipt", script_path)
flow_verify_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flow_verify_module)
ROUND1_HEADER_RE = flow_verify_module.ROUND1_HEADER_RE
ROUND1_HEADER_PATTERN = flow_verify_module.ROUND1_HEADER_PATTERN

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "round1"
POSITIVE_DIR = FIXTURE_DIR / "positive"
NEGATIVE_DIR = FIXTURE_DIR / "negative"

# The #341 Step 6(b) spec pattern, extended with one branch: "(" is a separator only
# when a digit follows, so real "ROUND 1 (3 units: ...)" lines match but
# "Round 1 (implement):" status lines do not.
SPEC_PATTERN = r"(?im)^[ \t]*(?:[#>*-][ \t]*)*\**ROUND[ \t]*1\**[ \t]*(?:[—–:+-]|\((?=\d)|$)"

# Revert mutants. Each is one edit away from ROUND1_HEADER_PATTERN.
STRICT_PATTERN = r"(?m)^ROUND 1\b"  # pre-#341 check
NO_SEPARATOR_PATTERN = r"(?im)^[ \t]*(?:[#>*-]+[ \t]*)*\**ROUND[ \t]*1\**[ \t]*"
NO_ANCHOR_PATTERN = r"(?im)ROUND[ \t]*1\**[ \t]*(?:[—–:+-]|$)"

POSITIVE_REAL = [
    "real-parallel-em-dash.md",
    "real-parallel-hyphen.md",
    "real-single-unit.md",
    "real-bare.md",
    "real-plus-join.md",
    "real-not-run.md",
    "real-colon-units.md",
]
POSITIVE_SYNTH = ["synth-bold.md", "synth-h2.md", "synth-indented.md"]
NEGATIVE_FILES = ["real-status-lines-no-header.md", "synth-prose-only.md", "synth-no-round.md"]


def read_fixture(directory: Path, name: str) -> str:
    return (directory / name).read_text(encoding="utf-8")


def failing_positives(pattern: re.Pattern) -> list:
    """Positive fixtures the given pattern does NOT match."""
    misses = []
    for name in POSITIVE_REAL + POSITIVE_SYNTH:
        if not pattern.search(read_fixture(POSITIVE_DIR, name)):
            misses.append(name)
    return misses


def passing_negatives(pattern: re.Pattern) -> list:
    """Negative fixtures the given pattern wrongly matches."""
    return [name for name in NEGATIVE_FILES if pattern.search(read_fixture(NEGATIVE_DIR, name))]


def main() -> None:
    h = Harness("ROUND 1 SECTION-HEADER CONTRACT (#341 item 5)")

    h.test_result("Module pattern is the spec pattern (with the digit-guarded paren branch)",
                  ROUND1_HEADER_PATTERN == SPEC_PATTERN,
                  f"pattern={ROUND1_HEADER_PATTERN!r}")

    misses = failing_positives(ROUND1_HEADER_RE)
    h.test_result("Every real and synthetic positive fixture matches the ROUND 1 header pattern",
                  misses == [], f"misses={misses}")

    wrong = passing_negatives(ROUND1_HEADER_RE)
    h.test_result("No negative fixture matches the ROUND 1 header pattern",
                  wrong == [], f"wrongly passing={wrong}")

    # Revert to strict ^ROUND 1\b: the paraphrase positives must fail.
    strict_misses = failing_positives(re.compile(STRICT_PATTERN))
    missing_synth = [n for n in POSITIVE_SYNTH if n not in strict_misses]
    h.test_result("Reverting to strict ^ROUND 1\\b makes the paraphrase positives fail",
                  missing_synth == [] and len(strict_misses) > 0,
                  f"strict did not reject={missing_synth}")

    # Separator requirement removed: the status-line negative must wrongly pass.
    no_sep_wrong = passing_negatives(re.compile(NO_SEPARATOR_PATTERN))
    h.test_result("Removing the separator-or-end-of-line requirement lets status lines pass",
                  "real-status-lines-no-header.md" in no_sep_wrong,
                  f"no-separator mutant passed={no_sep_wrong}")

    # Line anchor removed: the prose-only negative must wrongly pass.
    no_anchor_wrong = passing_negatives(re.compile(NO_ANCHOR_PATTERN))
    h.test_result("Removing the line anchor lets the prose-only 'round 1' line pass",
                  "synth-prose-only.md" in no_anchor_wrong,
                  f"no-anchor mutant passed={no_anchor_wrong}")

    # Status-line negative must be a real receipt shape: it carries the status lines and no header.
    status_text = read_fixture(NEGATIVE_DIR, "real-status-lines-no-header.md")
    h.test_result("Status-line negative keeps 'Round 1 join:' and 'Round 1 (implement):' lines",
                  "  Round 1 join:" in status_text and "  Round 1 (implement):" in status_text)

    # Scrub check: fixtures carry no absolute home paths or other-repo references.
    leaks = []
    for directory in (POSITIVE_DIR, NEGATIVE_DIR):
        for path in sorted(directory.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            if "/Users/" in text or "/home/" in text or "~/" in text:
                leaks.append(path.name)
    h.test_result("Fixtures carry no absolute paths", leaks == [], f"leaks={leaks}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
