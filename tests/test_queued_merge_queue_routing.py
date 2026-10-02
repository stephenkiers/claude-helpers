#!/usr/bin/env python3
"""
Test suite for queued-merge.md staleness guard features (PR #232).

Tests behaviors in queued-merge.md related to queue-routed PR handling:
- Phase 1 records start time for staleness checking
- Phase 1 uses ownership-checked state directory
- Phase 4 reads and validates start time (freshness check)
- Phase 4 cleans up state files

Note: The route file itself is created by merge-and-cleanup.md Phase 1 before
invoking /queued-merge. This test suite focuses only on the staleness guard
behavior in queued-merge.md.

Run with: python3 tests/test_queued_merge_queue_routing.py
"""

import re
from _test_harness import REPO_ROOT, Harness

COMMANDS_DIR = REPO_ROOT / "commands"
QUEUED_MERGE_FILE = COMMANDS_DIR / "queued-merge.md"


def read_file(path) -> str:
    """Return a file's text, or empty string if missing."""
    try:
        return path.read_text()
    except OSError:
        return ""


def get_section_bounds(markdown_text: str, section_name: str) -> tuple[int, int]:
    """Return the start and end byte offsets of a section. Returns (-1, -1) if not found."""
    pattern = rf"^### {re.escape(section_name)}"
    match = re.search(pattern, markdown_text, re.MULTILINE)
    if not match:
        return -1, -1

    start = match.start()
    # Find the next ### at the same or higher level
    rest = markdown_text[match.end():]
    next_match = re.search(r"^### ", rest, re.MULTILINE)
    end = match.end() + next_match.start() if next_match else len(markdown_text)
    return start, end


def extract_phase_block(markdown_text: str, phase_name: str) -> str:
    """Extract the text content of a phase section."""
    start, end = get_section_bounds(markdown_text, phase_name)
    if start == -1:
        return ""
    return markdown_text[start:end]


QM = read_file(QUEUED_MERGE_FILE)

h = Harness("QUEUED-MERGE STALENESS GUARD TEST SUITE (PR #232)")
t = h.test_result

print()

# ============================================================================
# TEST 1: Phase 1 records start time for staleness checking
# ============================================================================
print("[Test 1] Phase 1 records start time with jq -n 'now'")

phase1_text = extract_phase_block(QM, "Phase 1 — Resolve PR and worktree")
if phase1_text:
    # Look for start time recording using jq -n 'now'
    has_jq_now = "jq -n 'now'" in phase1_text or 'jq -n "now"' in phase1_text or ("jq -n" in phase1_text and "now" in phase1_text)
    has_state_dir = "git rev-parse" in phase1_text and "git-common-dir" in phase1_text
    # More specific: look for the state filename pattern or the actual git-common-dir pattern with time/now write
    has_state_file = "queued-merge-state" in phase1_text or ("git-common-dir" in phase1_text and ("now" in phase1_text or ("jq" in phase1_text and ("started" in phase1_text.lower() or "start_time" in phase1_text.lower()))))

    has_start_time = has_jq_now and has_state_dir and has_state_file
    t("Phase 1 records start time to state directory",
      has_start_time,
      "Phase 1 should use 'jq -n 'now'' and store in git state directory via git-common-dir")
else:
    t("Phase 1 section found", False, "could not extract Phase 1 section")

print()

# ============================================================================
# TEST 2: Phase 1 uses git rev-parse --git-common-dir for state directory
# ============================================================================
print("[Test 2] Phase 1 uses ownership-checked state directory")

if phase1_text:
    has_git_common_dir = "git rev-parse" in phase1_text and "--git-common-dir" in phase1_text
    has_path_format = "--path-format=absolute" in phase1_text

    t("Phase 1 uses git rev-parse --git-common-dir --path-format=absolute",
      has_git_common_dir and has_path_format,
      "state directory must use ownership-checked git common dir pattern")
else:
    t("Phase 1 section found", False, "could not extract Phase 1 section")

print()

# ============================================================================
# TEST 3: Phase 4 reads start time file for freshness check
# ============================================================================
print("[Test 3] Phase 4 reads start time for staleness checking")

phase4_text = extract_phase_block(QM, "Phase 4 — Read result and report")
if phase4_text:
    # Look for start time file read
    has_start_read = ("START_TIME" in phase4_text or "start" in phase4_text.lower()) and "=" in phase4_text
    has_state_file_access = "queued-merge-state" in phase4_text or "$COMMON_DIR" in phase4_text or "git-common-dir" in phase4_text.lower()

    has_read = has_start_read or has_state_file_access
    t("Phase 4 accesses start time from state file",
      has_read,
      "Phase 4 should read start time to check freshness")
else:
    t("Phase 4 section found", False, "could not extract Phase 4 section")

print()

# ============================================================================
# TEST 4: Phase 4 performs staleness check (timestamp comparison)
# ============================================================================
print("[Test 4] Phase 4 checks result freshness/staleness")

if phase4_text:
    # Look for timestamp/staleness checking logic using stronger patterns
    # Stronger check: look for actual time-based comparison or specific staleness/freshness keywords
    has_timestamp_compare = bool(re.search(r'(timestamp|START_TIME|start.*time|QUEUED.*TIME)', phase4_text, re.IGNORECASE))
    has_stale_check = bool(re.search(r'(stale|fresh|age|elapsed|duration)\s', phase4_text, re.IGNORECASE)) or "stale" in phase4_text.lower() or "fresh" in phase4_text.lower()
    has_gate = "!=" in phase4_text or "==" in phase4_text or "exit" in phase4_text.lower() or "return" in phase4_text.lower()

    has_freshness = (has_timestamp_compare or has_stale_check) and has_gate
    t("Phase 4 performs staleness check",
      has_freshness,
      "Phase 4 should compare result timestamp against start time and gate on staleness (check for stale/fresh/age patterns)")
else:
    t("Phase 4 section found", False, "could not extract Phase 4 section")

print()

# ============================================================================
# TEST 5: Phase 4 removes start time file after consuming it
# ============================================================================
print("[Test 5] Phase 4 cleans up start time file")

if phase4_text:
    # Look for file deletion (rm, remove, or similar)
    has_rm = "rm" in phase4_text and ("queued-merge-state" in phase4_text or "$COMMON_DIR" in phase4_text or "git-common-dir" in phase4_text.lower())

    t("Phase 4 removes start time file",
      has_rm,
      "Phase 4 should rm the start time file after checking freshness")
else:
    t("Phase 4 section found", False, "could not extract Phase 4 section")

print()

# ============================================================================
# TEST 6: Phase 1 and Phase 4 use consistent state directory pattern
# ============================================================================
print("[Test 6] Phase 1 and Phase 4 use consistent state directory pattern")

if phase1_text and phase4_text:
    # Both should use git-common-dir
    phase1_uses_git_dir = "git rev-parse" in phase1_text and "--git-common-dir" in phase1_text
    phase4_uses_git_dir = "git rev-parse" in phase4_text and "--git-common-dir" in phase4_text or \
                          "$COMMON_DIR" in phase4_text or "queued-merge-state" in phase4_text

    consistent = (phase1_uses_git_dir and phase4_uses_git_dir) or \
                 ("$MC_STATE_DIR" in phase1_text and "$MC_STATE_DIR" in phase4_text)

    t("Phase 1 and Phase 4 use consistent state directory pattern",
      consistent,
      "Both phases should use the same ownership-checked state directory pattern")
else:
    t("Both Phase 1 and Phase 4 found", phase1_text and phase4_text,
      "could not extract both phases")

print()

# ============================================================================
# TEST 7: Phase 4 gates on staleness result
# ============================================================================
print("[Test 7] Phase 4 exits or stops on stale result")

if phase4_text:
    # Look for error handling on stale results
    has_error_on_stale = ("exit" in phase4_text.lower() or "return" in phase4_text.lower() or "die" in phase4_text.lower()) and \
                         ("stale" in phase4_text.lower() or "treating as missing" in phase4_text.lower() or "missing result" in phase4_text.lower())

    t("Phase 4 stops processing on stale/missing result",
      has_error_on_stale,
      "Phase 4 should exit with error when result is too stale or missing")
else:
    t("Phase 4 section found", False, "could not extract Phase 4 section")

print()

h.summarize_and_exit()
