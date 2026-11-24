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

import hashlib
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


# ── Section 4: parity catch-up (#538) content checks on the inventory ───────

def _inventory_text(name):
    path = INVENTORY_DIR / name
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def _inventory_lines(name):
    text = _inventory_text(name)
    return [] if text is None else text.splitlines()


def test_parity_row_387_records_verify_receipt_as_not_ported():
    """The rules inventory marks #387 as not ported for the verify receipt."""
    rows = [line for line in _inventory_lines("rules.md") if "#387" in line]
    if not rows:
        return False, "no rules.md row mentions #387"
    if not any("not ported: verify receipt" in line.lower() for line in rows):
        return False, "the #387 row does not say 'not ported: verify receipt'"
    return True, ""


def test_parity_rows_42_and_2026_11_20_point_at_tests_and_headless():
    """The 2026-11-20 amendment and row 42 reference tests/ and headless behaviour."""
    lines = _inventory_lines("rules.md")
    dated = [line for line in lines if "2026-11-20" in line]
    if not dated:
        return False, "no rules.md line carries 2026-11-20"
    if not any("tests/" in line and "headless" in line.lower() for line in dated):
        return False, "the 2026-11-20 entry does not name tests/ and headless"
    row42 = [line for line in lines if re.match(r"^\W*42\b", line.strip().lstrip("|").strip() or "x")]
    if row42 and not any("superseded" in line.lower() for line in row42):
        return False, "row 42 is not marked superseded"
    return True, ""


def test_parity_no_resume_reasks_citations():
    """No inventory file cites the removed test_resume_reasks_* tests."""
    corpus = read_inventory(INVENTORY_DIR)
    if "test_resume_reasks_" in corpus:
        return False, "an inventory file still cites test_resume_reasks_*"
    return True, ""


def test_parity_table_rows_carry_issue_refs():
    """#490, #529 and #488 each appear on a table row of the rules inventory."""
    lines = _inventory_lines("rules.md")
    for issue in ("#490", "#529", "#488"):
        rows = [line for line in lines if issue in line and line.lstrip().startswith("|")]
        if not rows:
            return False, f"{issue} is not on any table row in rules.md"
    return True, ""


def test_parity_readme_drops_two_values_only_and_names_closed_sets():
    """README no longer says 'There are two values only' and gives closed-set examples."""
    text = _inventory_text("README.md")
    if text is None:
        return False, "README.md missing"
    if "There are two values only" in text:
        return False, "README still says 'There are two values only'"
    if "closed set" not in text.lower() and "closed-set" not in text.lower():
        return False, "README gives no closed-set examples"
    return True, ""


def test_parity_readme_cites_adr_0026_amendment_and_terms():
    """README cites ADR-0026 2026-11-23 and uses the feature parity and pre-parity terms."""
    text = _inventory_text("README.md")
    if text is None:
        return False, "README.md missing"
    if "ADR-0026 2026-11-23" not in text:
        return False, "README does not cite 'ADR-0026 2026-11-23'"
    for term in ("feature parity", "pre-parity"):
        if term not in text:
            return False, f"README does not use the term {term!r}"
    return True, ""



# ---------------------------------------------------------------------------
# ADR-0026 2026-11-23 drift checks (#538): row truth, closed disposition set, v1 content pin.
# Pure functions over text and a directory; no git command is run.
# ---------------------------------------------------------------------------

PIN_FILE = INVENTORY_DIR / "v1-content-pins.md"
WATCHED_V1_FILES = ("commands/expert-flow.md", "prompts/flow-step-contract.md", "prompts/flow-reference.md")
DISPOSITION_PREFIXES = (
    "port, pinned by test",
    "port, test not named (backfill #",
    "landed in #",
    "landed as:",
    "re-pointed to ",
    "deferred to ",
    "not ported:",
    "dropped:",
    "refused by design (",
)
CHECKED_SECTIONS = ("1", "4")
PIN_TRIGGERS = ("pinned by test", "pinned by tests/", "pinned by `tests/")
SECTION_RE = re.compile(r"^##\s+(\d+)\.")
TEST_GROUP_RE = re.compile(r"`?(tests/[\w./-]+\.py)`?(?:::(\w+)|\s*\(([^)]*)\))?")
IDENT_RE = re.compile(r"^[A-Za-z_]\w*$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
REF_RE = re.compile(r"^#\d+$")
PIN_FAILURE_HELP = (
    "A watched v1 file changed without inventory review. Re-read it, add or update the docs/v2-inventory/rules.md "
    "rows it affects, then bump the pin in the same PR (ADR-0026 2026-11-23 amendment). A pin bump is a human-only "
    "edit: an automated worker must stop with NEEDS_HUMAN or Decision: FAILED and never re-hash the file."
)


def _table_rows(rules_text, sections=CHECKED_SECTIONS):
    """Yield (line_no, row_key, disposition) for each data row of a table under the given top-level sections."""
    section = None
    for line_no, line in enumerate(rules_text.splitlines(), 1):
        match = SECTION_RE.match(line)
        if match:
            section = match.group(1)
            continue
        if section not in sections or not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2 or set(cells[-1]) <= set("-: ") or cells[-1] == "Disposition":
            continue
        yield line_no, cells[0], cells[-1]


def check_row_truth(rules_text, repo_root):
    """Errors for table rows whose 'pinned by' citations name no tests/ path, a missing file, or an undefined test.

    Each `tests/<file>.py (<names>)` group is resolved against its own file. "Named" means defined: `def <name>` at a
    word boundary. Registration in the run list is not checked (Q2 = A).
    """
    errors = []
    sources = {}
    for line_no, key, disposition in _table_rows(rules_text, sections=("1", "2", "3", "4")):
        if not any(trigger in disposition for trigger in PIN_TRIGGERS):
            continue
        groups = TEST_GROUP_RE.findall(disposition)
        if not groups:
            errors.append(f"rules.md:{line_no} row {key}: says 'pinned by' but names no tests/ path")
            continue
        for rel, colon_name, paren_names in groups:
            path = Path(repo_root) / rel
            if not path.is_file():
                errors.append(f"rules.md:{line_no} row {key}: cited file {rel} does not exist")
                continue
            names = [colon_name] if colon_name else [n.strip() for n in paren_names.split(",")]
            names = [n for n in names if IDENT_RE.match(n)]
            if rel not in sources:
                sources[rel] = path.read_text(encoding="utf-8")
            for name in names:
                if not re.search(r"^\s*def " + re.escape(name) + r"\b", sources[rel], re.MULTILINE):
                    errors.append(f"rules.md:{line_no} row {key}: {name} is not defined in {rel}")
    return errors


def check_closed_set(rules_text):
    """Errors for §1 and §4 table rows whose disposition does not begin with a closed-set prefix (§2 and §3 excluded)."""
    return [
        f"rules.md:{line_no} row {key}: disposition {disposition[:60]!r} does not begin with a closed-set value"
        for line_no, key, disposition in _table_rows(rules_text)
        if not disposition.startswith(DISPOSITION_PREFIXES)
    ]


def parse_pins(pin_text):
    """The watched-files table of v1-content-pins.md as a list of (path, sha256, reviewed, ref)."""
    rows = []
    in_table = False
    for line in pin_text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")] if line.lstrip().startswith("|") else []
        if cells[:4] == ["path", "sha256", "reviewed", "ref"]:
            in_table = True
            continue
        if not in_table:
            continue
        if not cells:
            if rows:
                break
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        if len(cells) == 4:
            rows.append(tuple(cells))
    return rows


def check_content_pins(pin_text, repo_root, watched=WATCHED_V1_FILES):
    """Errors when the pin table's paths differ from `watched`, an entry is malformed, or a raw-byte sha256 differs."""
    rows = parse_pins(pin_text)
    errors = []
    paths = [row[0] for row in rows]
    if sorted(paths) != sorted(watched):
        errors.append(f"pin table lists {paths}, expected exactly {list(watched)}. {PIN_FAILURE_HELP}")
    for rel, digest, reviewed, ref in rows:
        if not ISO_DATE_RE.match(reviewed) or not REF_RE.match(ref):
            errors.append(f"pin for {rel} needs an ISO reviewed date and a #N ref (got {reviewed!r}, {ref!r})")
        path = Path(repo_root) / rel
        if not path.is_file():
            errors.append(f"pinned file {rel} does not exist. {PIN_FAILURE_HELP}")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != digest:
            errors.append(f"pin mismatch for {rel}: recorded {digest}, actual {actual}. {PIN_FAILURE_HELP}")
    return errors


def _rules_text():
    return (INVENTORY_DIR / "rules.md").read_text(encoding="utf-8")


def test_drift_real_rules_pass_row_truth():
    """Every 'pinned by' citation in the real rules.md resolves to a defined test in an existing file."""
    errors = check_row_truth(_rules_text(), REPO_ROOT)
    return (not errors), "; ".join(errors[:5])


def test_drift_real_rules_pass_closed_set():
    """Every §1/§4 disposition in the real rules.md begins with a closed-set value."""
    errors = check_closed_set(_rules_text())
    return (not errors), "; ".join(errors[:5])


def test_drift_row_truth_skips_prose_line():
    """rules.md line 7's prose 'pinned by test' wording is not a table row and produces no error."""
    text = "# v1 rules inventory\n\nA row says port, pinned by test when it is pinned.\n\n## 1. Rule table\n"
    errors = check_row_truth(text, REPO_ROOT)
    return (not errors), f"prose produced errors: {errors}"


def _synthetic_rules(disposition):
    return f"## 1. Rule table\n\n| a | b | Disposition |\n|---|---|---|\n| 1 | x | {disposition} |\n"


def test_drift_row_truth_synthetic_rows():
    """Synthetic rows: missing file, undefined function, no path, wrong-group function fail; valid forms pass."""
    real = "tests/test_flow_driver_gates.py"
    other = "tests/test_flow_driver_preflight.py"
    real_fn = "test_merge_gate_stops"
    other_fn = "test_resolve_main_worktree_parent_mismatch_is_a_stop"
    cases = [
        ("port, pinned by test (tests/test_nope.py::test_missing)", "tests/test_nope.py"),
        (f"port, pinned by test ({real}::test_no_such_function_here)", "test_no_such_function_here"),
        ("port, pinned by test", "no tests/ path"),
        (f"port, pinned by test {real} ({other_fn}) and {other} ({real_fn})", other_fn),
    ]
    for disposition, needle in cases:
        errors = check_row_truth(_synthetic_rules(disposition), REPO_ROOT)
        if not any(needle in err for err in errors):
            return False, f"{disposition!r} did not fail naming {needle!r}: {errors}"
    passing = [
        f"port, pinned by test {real} ({real_fn}) and {other} ({other_fn})",
        f"landed in #538 (code); pinned by `{real}` ({real_fn})",
    ]
    for disposition in passing:
        errors = check_row_truth(_synthetic_rules(disposition), REPO_ROOT)
        if errors:
            return False, f"{disposition!r} should pass: {errors}"
    return True, ""


def test_drift_closed_set_synthetic_rows():
    """Existing prefix forms pass; 'maybe later' fails; a §3 row is not checked."""
    for ok in ("dropped: no longer needed", "landed in #1 (x); no test yet", "landed as: a flag",
               "re-pointed to Layer 3 (moved)", "refused by design (unsafe)", "deferred to Layer 4 (#488)"):
        if check_closed_set(_synthetic_rules(ok)):
            return False, f"{ok!r} should pass the closed set"
    if not check_closed_set(_synthetic_rules("maybe later")):
        return False, "'maybe later' passed the closed set"
    section3 = "## 3. Overlapping-issue dispositions\n\n| a | Disposition |\n|---|---|\n| x | maybe later |\n"
    if check_closed_set(section3):
        return False, "§3 free text was checked"
    return True, ""


def test_drift_real_content_pins_match():
    """The real pin file lists exactly the three watched v1 files, each with a matching raw-byte sha256."""
    errors = check_content_pins(PIN_FILE.read_text(encoding="utf-8"), REPO_ROOT)
    return (not errors), "; ".join(errors)


def test_drift_wrong_pin_fails_with_human_only_message():
    """A synthetic wrong hash fails, and the message names the path, rules.md, the ADR, human-only and the stops."""
    text = PIN_FILE.read_text(encoding="utf-8")
    rows = parse_pins(text)
    if not rows:
        return False, "no pin rows parsed"
    target = rows[0]
    bad = text.replace(target[1], "0" * 64, 1)
    errors = check_content_pins(bad, REPO_ROOT)
    if not errors:
        return False, "a wrong hash passed"
    message = " ".join(errors)
    for needle in (target[0], "rules.md", "ADR-0026 2026-11-23", "human-only", "NEEDS_HUMAN", "Decision: FAILED"):
        if needle not in message:
            return False, f"failure message lacks {needle!r}"
    return True, ""


def test_drift_check_runs_no_git():
    """The drift check functions run no git command (a static scan of their source)."""
    import inspect
    for fn in (check_row_truth, check_closed_set, parse_pins, check_content_pins, _table_rows):
        if "subprocess" in inspect.getsource(fn) or "git " in inspect.getsource(fn):
            return False, f"{fn.__name__} references git or subprocess"
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

    print("[Section 4] parity catch-up (#538) inventory content")
    _check(h, "row for #387 says not ported: verify receipt", test_parity_row_387_records_verify_receipt_as_not_ported)
    _check(h, "2026-11-20 entry names tests/ and headless; row 42 superseded",
           test_parity_rows_42_and_2026_11_20_point_at_tests_and_headless)
    _check(h, "no test_resume_reasks_* citations remain", test_parity_no_resume_reasks_citations)
    _check(h, "#490, #529 and #488 are on table rows", test_parity_table_rows_carry_issue_refs)
    _check(h, "README drops 'There are two values only' and names closed sets",
           test_parity_readme_drops_two_values_only_and_names_closed_sets)
    _check(h, "README cites ADR-0026 2026-11-23 with parity terms",
           test_parity_readme_cites_adr_0026_amendment_and_terms)
    print()

    print("[Section 5] ADR-0026 2026-11-23 drift checks (#538)")
    _check(h, "real rules.md passes row truth", test_drift_real_rules_pass_row_truth)
    _check(h, "real rules.md passes the closed set", test_drift_real_rules_pass_closed_set)
    _check(h, "prose 'pinned by test' line is not a row", test_drift_row_truth_skips_prose_line)
    _check(h, "row truth: synthetic failing and passing rows", test_drift_row_truth_synthetic_rows)
    _check(h, "closed set: synthetic rows and §3 exclusion", test_drift_closed_set_synthetic_rows)
    _check(h, "real content pins match the watched v1 files", test_drift_real_content_pins_match)
    _check(h, "wrong pin fails with the human-only message", test_drift_wrong_pin_fails_with_human_only_message)
    _check(h, "drift check runs no git", test_drift_check_runs_no_git)
    print()

    h.summarize_and_exit()
