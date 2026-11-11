#!/usr/bin/env python3
"""
Drift check: every ADR-0023 amendment heading is referenced under docs/v2-inventory/.

An amendment heading is a level-2 or level-3 heading whose text contains the word "amendment".
Its date (YYYY-MM-DD) and each of its issue numbers (#N) must appear somewhere in the inventory
files (any regular file under docs/v2-inventory/, recursively). An amendment heading with no date
is keyed by its title text instead. Headings inside fenced code blocks are ignored.

Covered here:
1. docs/v2-inventory/README.md and rules.md exist.
2. The parser finds the real ADR-0023 amendments, and every one is referenced by the real inventory.
3. Synthetic fixtures (a temp ADR text and a temp inventory dir) prove the check fails when a
   heading is missing and passes when it is present, including an appended synthetic heading
   against the real files.

Run with: python3 tests/test_v2_inventory_coverage.py
"""

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT


ADR_0023 = REPO_ROOT / "docs" / "adr" / "0023-expert-flow.md"
INVENTORY_DIR = REPO_ROOT / "docs" / "v2-inventory"
REQUIRED_INVENTORY_FILES = ("README.md", "rules.md")

HEADING_RE = re.compile(r"^(#{2,3})[ \t]+(.+?)[ \t]*$")
AMENDMENT_WORD_RE = re.compile(r"\bamend\w*\b", re.IGNORECASE)
AMENDMENT_PREFIX_RE = re.compile(r"^\s*amend\w*\b\s*(?:\([^)]*\))?\s*[:\-—–]?\s*", re.IGNORECASE)
DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
ISSUE_RE = re.compile(r"#(\d+)\b")

# Minimum amendment count in ADR-0023. Set as a floor to detect parser regressions.
# This baseline (30) was established by parsing the real ADR-0023 and is the count as of the
# most recent amendment (2026-11-05, #462). Update this if the document structure changes
# or if amendments are added/removed.
MIN_AMENDMENT_COUNT = 30


def parse_amendments(adr_text):
    """Return one dict per amendment heading in adr_text.

    Keys: heading (text after the #s), date (first YYYY-MM-DD or None), issues (digit strings,
    without '#'), title (heading text with the leading 'Amendment' word and punctuation removed).
    """
    amendments = []
    in_fence = False
    for line in adr_text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = HEADING_RE.match(line)
        if match is None:
            continue
        text = match.group(2)
        if AMENDMENT_WORD_RE.search(text) is None:
            continue
        date_match = DATE_RE.search(text)
        amendments.append({
            "heading": text,
            "date": date_match.group(1) if date_match else None,
            "issues": ISSUE_RE.findall(text),
            "title": AMENDMENT_PREFIX_RE.sub("", text, count=1),
        })
    # Ensure fences are closed at the end of the file
    if in_fence:
        raise ValueError("Unclosed fence detected in ADR text: ``` fence was not closed before end of file")
    return amendments


def _normalise(text):
    return " ".join(text.lower().split())


def read_inventory(inventory_dir):
    """Concatenate every regular file under inventory_dir (recursive). Missing dir gives ''."""
    if not inventory_dir.is_dir():
        return ""
    parts = []
    for path in sorted(inventory_dir.rglob("*")):
        if path.is_file():
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def uncovered_amendments(adr_text, inventory_dir):
    """Return the heading text of every amendment in adr_text that the inventory does not reference."""
    corpus = read_inventory(inventory_dir)
    normalised_corpus = _normalise(corpus)
    missing = []
    for amendment in parse_amendments(adr_text):
        if amendment["date"] is None or not amendment["issues"]:
            # No date or no issues: key by normalised title instead
            key = _normalise(amendment["title"])
            covered = bool(key) and key in normalised_corpus
        else:
            # Has both date and issues: require co-located date AND all issues
            covered = amendment["date"] in corpus and all(
                re.search(r"#%s(?!\d)" % number, corpus) for number in amendment["issues"]
            )
        if not covered:
            missing.append(amendment["heading"])
    return missing


def _adr_text():
    return ADR_0023.read_text(encoding="utf-8")


def _uncovered_with_files(adr_text, files):
    """Build a temp inventory dir from {relative path: text} and run the check against it."""
    with tempfile.TemporaryDirectory() as td:
        inventory = Path(td) / "v2-inventory"
        inventory.mkdir()
        for relative, text in files.items():
            target = inventory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return uncovered_amendments(adr_text, inventory)


SYNTH_HEADING = "Amendment (2026-12-01, #999): synthetic drift probe"
SYNTH_ADR = "# Fixture ADR\n\n## Amendment (2026-12-01, #999): synthetic drift probe\n\nbody\n"


# ── Section 1: inventory layout ──────────────────────────────────────────────

def test_inventory_required_files_exist():
    """docs/v2-inventory/README.md and rules.md both exist."""
    missing = [name for name in REQUIRED_INVENTORY_FILES if not (INVENTORY_DIR / name).is_file()]
    if missing:
        return False, f"missing under docs/v2-inventory/: {', '.join(missing)}"
    return True, ""


# ── Section 2: parser against the real ADR-0023 ──────────────────────────────

def test_parser_finds_real_amendments():
    """The parser reads the real ADR-0023 amendment headings, including the 2026-11-05 #462 one."""
    amendments = parse_amendments(_adr_text())
    if len(amendments) < MIN_AMENDMENT_COUNT:
        return False, f"only {len(amendments)} amendment headings parsed from ADR-0023 (expected {MIN_AMENDMENT_COUNT}+)"
    found = any(a["date"] == "2026-11-05" and "462" in a["issues"] for a in amendments)
    if not found:
        return False, "the 2026-11-05 #462 amendment was not parsed from ADR-0023"
    return True, ""


def test_real_inventory_references_every_amendment():
    """Every ADR-0023 amendment heading is referenced somewhere under docs/v2-inventory/."""
    missing = uncovered_amendments(_adr_text(), INVENTORY_DIR)
    if missing:
        return False, f"{len(missing)} amendment(s) not referenced in docs/v2-inventory/: " + " | ".join(missing)[:800]
    return True, ""


def test_appended_synthetic_heading_is_the_only_new_miss():
    """Appending one synthetic amendment to the real ADR adds exactly that heading to the misses."""
    real_adr = _adr_text()
    baseline = uncovered_amendments(real_adr, INVENTORY_DIR)
    with_probe = uncovered_amendments(real_adr + "\n## " + SYNTH_HEADING + "\n", INVENTORY_DIR)
    if with_probe != baseline + [SYNTH_HEADING]:
        return False, f"expected baseline misses plus the probe; got {len(with_probe)} misses vs {len(baseline)} baseline"
    return True, ""


# ── Section 3: synthetic fixtures ────────────────────────────────────────────

def test_fixture_missing_heading_fails():
    """A synthetic heading with no inventory reference is reported as missing."""
    missing = _uncovered_with_files(SYNTH_ADR, {"README.md": "Mentions 2026-11-05 and #462 only.\n"})
    if missing != [SYNTH_HEADING]:
        return False, f"expected the synthetic heading to be missing, got {missing!r}"
    return True, ""


def test_fixture_present_heading_passes():
    """A synthetic heading whose date and issue are both referenced passes."""
    missing = _uncovered_with_files(SYNTH_ADR, {"rules.md": "Decided 2026-12-01 under #999.\n"})
    if missing:
        return False, f"expected no misses, got {missing!r}"
    return True, ""


def test_fixture_date_without_issue_fails():
    """Only the date mentioned is not enough when the heading also carries an issue number."""
    missing = _uncovered_with_files(SYNTH_ADR, {"README.md": "Dated 2026-12-01, no issue.\n"})
    if missing != [SYNTH_HEADING]:
        return False, f"date-only mention should not cover the heading, got {missing!r}"
    return True, ""


def test_fixture_issue_without_date_fails():
    """Only the issue number mentioned is not enough when the heading also carries a date."""
    missing = _uncovered_with_files(SYNTH_ADR, {"README.md": "Tracked as #999, no date.\n"})
    if missing != [SYNTH_HEADING]:
        return False, f"issue-only mention should not cover the heading, got {missing!r}"
    return True, ""


def test_fixture_issue_prefix_is_not_a_substring_match():
    """#9990 in the inventory does not satisfy #999."""
    missing = _uncovered_with_files(SYNTH_ADR, {"README.md": "2026-12-01 tracked as #9990.\n"})
    if missing != [SYNTH_HEADING]:
        return False, f"#9990 wrongly satisfied #999, got {missing!r}"
    return True, ""


def test_fixture_reference_in_nested_file_counts():
    """A reference anywhere under the inventory dir, in a subdirectory, is enough."""
    missing = _uncovered_with_files(
        SYNTH_ADR, {"deep/nested/notes.md": "2026-12-01 and #999 recorded here.\n"}
    )
    if missing:
        return False, f"nested reference not counted, got {missing!r}"
    return True, ""


def test_fixture_date_only_amendment_needs_title():
    """A date-only amendment (no issues) is covered only by its title, not by the date alone."""
    adr = "## Amendment (2026-12-01): date-only title probe\n"
    # Date without title should not cover
    uncovered = _uncovered_with_files(adr, {"README.md": "Dated 2026-12-01 but no title.\n"})
    if uncovered != ["Amendment (2026-12-01): date-only title probe"]:
        return False, f"date-only amendment should not be covered by date alone, got {uncovered!r}"
    # Title should cover it
    covered = _uncovered_with_files(adr, {"README.md": "The date-only title probe is recorded.\n"})
    if covered:
        return False, f"date-only amendment should be covered by its title, got {covered!r}"
    return True, ""


def test_fixture_undated_heading_keyed_by_title():
    """An undated amendment is covered by its title text, and missing without it."""
    adr = "## Amendment: plain title probe\n"
    covered = _uncovered_with_files(adr, {"README.md": "The Plain Title Probe is recorded.\n"})
    if covered:
        return False, f"title mention (case-insensitive) should cover the heading, got {covered!r}"
    uncovered = _uncovered_with_files(adr, {"README.md": "Something else entirely.\n"})
    if uncovered != ["Amendment: plain title probe"]:
        return False, f"undated heading without its title should be missing, got {uncovered!r}"
    return True, ""


def test_fixture_level3_amendment_heading_counts():
    """A level-3 heading that names an amendment with a date and issue is parsed and checked."""
    adr = "## Section\n\n### Sub-rule (amendment 2026-12-03, #777)\n"
    missing = _uncovered_with_files(adr, {"README.md": "Nothing relevant.\n"})
    if missing != ["Sub-rule (amendment 2026-12-03, #777)"]:
        return False, f"level-3 amendment heading not checked, got {missing!r}"
    return True, ""


def test_fixture_non_amendment_and_fenced_headings_ignored():
    """Non-amendment headings and headings inside fenced code blocks are not checked."""
    adr = (
        "## Consequences 2026-12-01 #999\n"
        "```\n"
        "## Amendment (2026-12-02, #888): hidden in a fence\n"
        "```\n"
        "### Rules of thumb (not a heading we track)\n"
    )
    parsed = parse_amendments(adr)
    if parsed:
        return False, f"expected no amendment headings, parsed {[a['heading'] for a in parsed]!r}"
    if _uncovered_with_files(adr, {}):
        return False, "non-amendment content produced misses"
    return True, ""


def test_fixture_unclosed_fence_raises_error():
    """An ADR text with an unclosed ``` fence raises ValueError."""
    adr_unclosed = "## Amendment (2026-12-01, #999): test heading\n```\n## Fenced heading\n"
    try:
        parsed = parse_amendments(adr_unclosed)
        return False, f"expected ValueError for unclosed fence, got {len(parsed)} amendments"
    except ValueError as exc:
        if "Unclosed fence" in str(exc):
            return True, ""
        return False, f"ValueError raised but message missing 'Unclosed fence': {exc}"
    except Exception as exc:
        return False, f"expected ValueError, got {type(exc).__name__}: {exc}"


def test_fixture_missing_inventory_dir_fails():
    """A missing inventory directory reports every amendment as uncovered (no crash)."""
    with tempfile.TemporaryDirectory() as td:
        missing = uncovered_amendments(SYNTH_ADR, Path(td) / "does-not-exist")
    if missing != [SYNTH_HEADING]:
        return False, f"missing dir should leave the heading uncovered, got {missing!r}"
    return True, ""


def _check(h, label, fn):
    """Run one test function, recording a crash as a failure instead of aborting the suite."""
    try:
        passed, msg = fn()
    except Exception as exc:  # report, do not abort the suite
        passed, msg = False, f"{type(exc).__name__}: {exc}"
    h.test_result(label, passed, msg)


if __name__ == "__main__":
    h = Harness("V2 INVENTORY COVERAGE TEST SUITE")

    print("[Section 1] inventory layout")
    _check(h, "docs/v2-inventory/README.md and rules.md exist", test_inventory_required_files_exist)
    print()

    print("[Section 2] real ADR-0023 amendments against the real inventory")
    _check(h, "parser finds the real ADR-0023 amendments", test_parser_finds_real_amendments)
    _check(h, "every real ADR-0023 amendment is referenced under docs/v2-inventory/",
           test_real_inventory_references_every_amendment)
    _check(h, "an appended synthetic amendment is the only new miss against the real files",
           test_appended_synthetic_heading_is_the_only_new_miss)
    print()

    print("[Section 3] synthetic fixtures")
    _check(h, "missing heading fails", test_fixture_missing_heading_fails)
    _check(h, "present heading (date and issue) passes", test_fixture_present_heading_passes)
    _check(h, "date without issue does not cover the heading", test_fixture_date_without_issue_fails)
    _check(h, "issue without date does not cover the heading", test_fixture_issue_without_date_fails)
    _check(h, "#9990 does not satisfy #999", test_fixture_issue_prefix_is_not_a_substring_match)
    _check(h, "reference in a nested inventory file counts", test_fixture_reference_in_nested_file_counts)
    _check(h, "date-only amendment (no issues) is keyed by title", test_fixture_date_only_amendment_needs_title)
    _check(h, "undated heading is keyed by its title", test_fixture_undated_heading_keyed_by_title)
    _check(h, "level-3 amendment heading is checked", test_fixture_level3_amendment_heading_counts)
    _check(h, "non-amendment and fenced headings are ignored",
           test_fixture_non_amendment_and_fenced_headings_ignored)
    _check(h, "unclosed fence raises ValueError", test_fixture_unclosed_fence_raises_error)
    _check(h, "missing inventory dir leaves the heading uncovered", test_fixture_missing_inventory_dir_fails)
    print()

    h.summarize_and_exit()
