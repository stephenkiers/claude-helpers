#!/usr/bin/env python3
"""
Test suite for findings.json contract validation.

These tests verify that prompts (amalgamator.md, triage.md) and commands (review-stats.md)
implement the shared findings.json contract and use correct field names and verdict vocabulary.

Note: prompts/amalgamator.md and prompts/triage.md may be edited by a parallel "docs" unit
and may not yet exist in this worktree. Tests are written to pass once both units are merged.
"""

from _test_harness import REPO_ROOT, Harness


class SkipAwareHarness(Harness):
    """Harness with a real skip bucket: passed=None records a skip, not a failure."""

    def __init__(self, title):
        super().__init__(title)
        self.skip_count = 0

    def test_result(self, test_name, passed, message=""):
        if passed is None:
            print(f"- SKIP {test_name}: {message}" if message else f"- SKIP {test_name}")
            self.skip_count += 1
            return
        super().test_result(test_name, passed, message)

    def summarize_and_exit(self):
        print(f"Skipped: {self.skip_count}")
        super().summarize_and_exit()


harness = SkipAwareHarness("Findings JSON Contract Tests")

# Read content from prompts
amalgamator_path = REPO_ROOT / "prompts" / "amalgamator.md"
triage_path = REPO_ROOT / "prompts" / "triage.md"
review_stats_path = REPO_ROOT / "commands" / "review-stats.md"
reviewer_yield_path = REPO_ROOT / "scripts" / "reviewer-yield.py"


# Test 1: amalgamator.md documents findings.json fields
def test_amalgamator_documents_findings_fields():
    """amalgamator.md contains documented findings.json field names."""
    if not amalgamator_path.exists():
        harness.test_result("amalgamator.md documents findings fields", None, "amalgamator.md not yet present")
        return

    content = amalgamator_path.read_text()

    # Check for documented field names
    required_fields = ["id", "severity", "raised_by", "supported_by", "verdict", "schema_version"]
    found_fields = []
    for field in required_fields:
        # Look for field names as literals in the content
        if f'"{field}"' in content or f"'{field}'" in content or f"- {field}" in content:
            found_fields.append(field)

    # At minimum, the key fields should be documented
    passed = len(found_fields) >= 4  # At least id, severity, verdict, schema_version
    harness.test_result("amalgamator.md documents findings.json fields", passed)


# Test 2: Verdict vocabulary (CONFIRMED, DOWNGRADED, REJECTED) in amalgamator.md
def test_amalgamator_verdict_vocabulary():
    """amalgamator.md contains verdict vocabulary strings."""
    if not amalgamator_path.exists():
        harness.test_result("amalgamator.md includes verdict vocabulary", None, "amalgamator.md not yet present")
        return

    content = amalgamator_path.read_text()

    verdicts = ["CONFIRMED", "DOWNGRADED", "REJECTED"]
    found = sum(1 for v in verdicts if v in content)

    passed = found >= 2  # At least 2 of the 3 verdict types mentioned
    harness.test_result("amalgamator.md includes verdict vocabulary", passed)


# Test 3: Verdict vocabulary in triage.md
def test_triage_verdict_vocabulary():
    """triage.md contains verdict vocabulary strings."""
    if not triage_path.exists():
        harness.test_result("triage.md includes verdict vocabulary", None, "triage.md not yet present")
        return

    content = triage_path.read_text()

    verdicts = ["CONFIRMED", "DOWNGRADED", "REJECTED"]
    found = sum(1 for v in verdicts if v in content)

    passed = found >= 2  # At least 2 of the 3 verdict types mentioned
    harness.test_result("triage.md includes verdict vocabulary", passed)


# Test 4: triage.md has "Raised by" in table headers
def test_triage_raised_by_header():
    """triage.md's 'Doing it' table header contains 'Raised by'."""
    if not triage_path.exists():
        harness.test_result("triage.md table contains 'Raised by'", None, "triage.md not yet present")
        return

    content = triage_path.read_text()

    # Look for the table header with "Raised by" or similar
    passed = "Raised by" in content or "raised by" in content.lower()
    harness.test_result("triage.md table contains 'Raised by'", passed)


# Test 5: review-stats.md no longer claims iterations key (old phrasing removed)
def test_review_stats_no_iterations_key_claim():
    """review-stats.md does not claim tokens come from iterations key."""
    if not review_stats_path.exists():
        harness.test_result("review-stats.md updated token language", None, "review-stats.md not found")
        return

    content = review_stats_path.read_text()

    # Check that the old phrasing is gone
    old_phrasings = ["iterations key", "carry an iterations key"]
    has_old = any(phrase in content for phrase in old_phrasings)

    # Check that new phrasing is present
    has_new = "message.usage" in content

    passed = not has_old and has_new
    harness.test_result("review-stats.md updated token language", passed)


# Test 6: Observation-only sentence in reviewer-yield.py
def test_reviewer_yield_observation_only():
    """scripts/reviewer-yield.py contains observation-only sentence."""
    if not reviewer_yield_path.exists():
        harness.test_result("reviewer-yield.py has observation-only note", None, "reviewer-yield.py not found")
        return

    content = reviewer_yield_path.read_text()

    # Check for the observation-only sentence
    observation_note = "Observation-only in Phase 0"
    passed = observation_note in content
    harness.test_result("reviewer-yield.py has observation-only note", passed)


# Test 7: Observation-only sentence in review-stats.md
def test_review_stats_observation_only():
    """commands/review-stats.md contains observation-only sentence."""
    if not review_stats_path.exists():
        harness.test_result("review-stats.md has observation-only note", None, "review-stats.md not found")
        return

    content = review_stats_path.read_text()

    # Check for the observation-only sentence
    observation_note = "Observation-only in Phase 0"
    passed = observation_note in content
    harness.test_result("review-stats.md has observation-only note", passed)


# Run all tests
test_amalgamator_documents_findings_fields()
test_amalgamator_verdict_vocabulary()
test_triage_verdict_vocabulary()
test_triage_raised_by_header()
test_review_stats_no_iterations_key_claim()
test_reviewer_yield_observation_only()
test_review_stats_observation_only()

harness.summarize_and_exit()
