#!/usr/bin/env python3
"""
Test suite for merge-and-cleanup.md queue routing feature (PR #232).

Tests the new queue-routing decision path in Phase 1:
- PLAN_RESULT mutual exclusivity (if/elif, not separate ifs)
- Queue route marker write when PLAN_RESULT==3 and conditions met
- Route file guards in Phase 3 and Phase 4
- Push gate execution ordering for queue-routed PRs

Run with: python3 tests/test_merge_and_cleanup_queue_routing.py
"""

import re
from _test_harness import REPO_ROOT, Harness

COMMANDS_DIR = REPO_ROOT / "commands"
MERGE_FILE = COMMANDS_DIR / "merge-and-cleanup.md"


def read_file(path) -> str:
    """Return a file's text, or empty string if missing."""
    try:
        return path.read_text()
    except OSError:
        return ""


def extract_bash_blocks(markdown_text: str) -> list[tuple[int, str]]:
    """
    Extract all bash code block contents from markdown.
    Returns a list of (start_line_num, block_text) tuples.
    """
    blocks = []
    pattern = r"```bash\n(.*?)\n```"
    for match in re.finditer(pattern, markdown_text, re.DOTALL):
        start_pos = match.start()
        line_num = markdown_text[:start_pos].count("\n") + 1
        block_text = match.group(1)
        blocks.append((line_num, block_text))
    return blocks


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


MERGE = read_file(MERGE_FILE)

h = Harness("MERGE-AND-CLEANUP QUEUE ROUTING TEST SUITE (PR #232)")
t = h.test_result

print()

# ============================================================================
# TEST 1: Phase 1 has if/elif structure for PLAN_RESULT checks (mutual exclusivity)
# ============================================================================
print("[Test 1] Phase 1 uses 'elif' for PLAN_RESULT mutual exclusivity")

bash_blocks = extract_bash_blocks(MERGE)
phase1_block = None
for line_num, block in bash_blocks:
    if "PLAN_RESULT=$?" in block:
        phase1_block = block
        break

if phase1_block:
    # Check for the mutual-exclusivity pattern
    has_elif = "elif [ $PLAN_RESULT -ne 0 ]; then" in phase1_block or "elif [ \"$PLAN_RESULT\" -ne 0 ]; then" in phase1_block
    t("Phase 1 PLAN_RESULT checks use elif (not separate if)",
      has_elif,
      "expected 'elif [ $PLAN_RESULT -ne 0 ]' to make checks mutually exclusive")
else:
    t("Phase 1 block found", False, "could not extract Phase 1 bash block")

print()

# ============================================================================
# TEST 2: Phase 1 writes queue route marker when routing to queue
# ============================================================================
print("[Test 2] Phase 1 writes route marker when PLAN_RESULT==3 (queue routing)")

if phase1_block:
    has_route_marker = 'printf \'queue\\n\' > "$MC_STATE_DIR/route"' in phase1_block
    t("Phase 1 writes queue route marker",
      has_route_marker,
      "expected 'printf 'queue\\n' > \"$MC_STATE_DIR/route\"' in Phase 1")
else:
    t("Phase 1 block found", False, "could not extract Phase 1 bash block")

print()

# ============================================================================
# TEST 3: Route marker write is guarded by PLAN_RESULT condition
# ============================================================================
print("[Test 3] Route marker write is inside PLAN_RESULT==3 condition block")

if phase1_block:
    # Verify route marker write comes AFTER the PLAN_RESULT check
    # (indicating it's guarded by the conditional)
    plan_result_idx = phase1_block.find('PLAN_RESULT=$?')
    route_marker_idx = phase1_block.find('printf \'queue\\n\'')

    # Also verify it's inside an if/elif structure
    has_if_elif = "if" in phase1_block and "elif" in phase1_block
    route_after_check = route_marker_idx > plan_result_idx if plan_result_idx >= 0 and route_marker_idx >= 0 else False

    t("Route marker write is guarded by PLAN_RESULT check",
      route_after_check and has_if_elif,
      "route marker must be written after PLAN_RESULT check inside if/elif structure")
else:
    t("Phase 1 block found", False, "could not extract Phase 1 bash block")

print()

# ============================================================================
# TEST 4: Phase 3 has route file guard (skip if route is queue)
# ============================================================================
print("[Test 4] Phase 3 skips execution when route=queue")

phase3_text = extract_phase_block(MERGE, "Phase 3 —")
if phase3_text:
    # Look for skip guard that checks route file for "queue" value
    # Pattern: if [ -f "$ROUTE_FILE" ] && [ "$(cat "$ROUTE_FILE")" = "queue" ]
    skip_pattern = r'if\s*\[\s*-f\s*["\']?\$ROUTE_FILE'
    queue_check_pattern = r'cat\s*["\']?\$ROUTE_FILE.*=.*queue'
    has_route_check = bool(re.search(skip_pattern, phase3_text)) and bool(re.search(queue_check_pattern, phase3_text, re.DOTALL))

    t("Phase 3 has skip guard for route=queue",
      has_route_check,
      "expected pattern checking if route file contains 'queue' value")
else:
    t("Phase 3 section found", False, "could not extract Phase 3 section")

print()

# ============================================================================
# TEST 5: Phase 4 direct-route has route file guard
# ============================================================================
print("[Test 5] Phase 4 direct-route section has route file guard")

phase4_text = extract_phase_block(MERGE, "Phase 4 —")
if phase4_text:
    # Look for route guard that checks route file for "queue" value
    # Pattern: if [ -f "$ROUTE_FILE" ] && [ "$(cat "$ROUTE_FILE")" = "queue" ]; then exit 1; fi
    skip_pattern = r'if\s*\[\s*-f\s*["\']?\$ROUTE_FILE'
    queue_check_pattern = r'cat\s*["\']?\$ROUTE_FILE.*=.*queue'
    has_route_check = bool(re.search(skip_pattern, phase4_text)) and bool(re.search(queue_check_pattern, phase4_text, re.DOTALL))

    t("Phase 4 direct-route has skip guard for route=queue",
      has_route_check,
      "expected pattern checking if route file contains 'queue' value")
else:
    t("Phase 4 direct-route section found", False, "could not extract Phase 4 section")

print()

# ============================================================================
# TEST 6: Phase 4-Q reads and checks route file
# ============================================================================
print("[Test 6] Phase 4-Q reads and validates route file")

phase4q_text = extract_phase_block(MERGE, "Phase 4-Q")
if phase4q_text:
    # Check for route file read and validation
    has_route_read = 'ROUTE=' in phase4q_text or 'route' in phase4q_text.lower()
    has_route_validation = 'queue' in phase4q_text.lower() and 'route' in phase4q_text.lower()

    has_checks = has_route_read and has_route_validation
    t("Phase 4-Q reads and checks route file",
      has_checks,
      "Phase 4-Q should read route file and check if it equals 'queue'")
else:
    t("Phase 4-Q section found", False, "could not extract Phase 4-Q section")

print()

# ============================================================================
# TEST 7: Phase 1 clears state before writing route marker
# ============================================================================
print("[Test 7] Phase 1 clears previous state before writing route marker")

if phase1_block:
    # The route marker write should happen after any state clearing
    # Look for both the pattern that clears state and the route write
    has_state_clear = ('rm -f' in phase1_block or 'rm ' in phase1_block) and ('$MC_STATE_DIR' in phase1_block)
    has_route_write = 'printf \'queue\\n\' > "$MC_STATE_DIR/route"' in phase1_block

    # Verify clear happens before write
    if has_state_clear and has_route_write:
        clear_idx = phase1_block.find('rm')
        write_idx = phase1_block.find('printf \'queue\\n\'')
        clear_before_write = clear_idx < write_idx if clear_idx >= 0 and write_idx >= 0 else True

        t("Phase 1 clears state before writing route marker",
          clear_before_write,
          "state clearing should happen before route marker write")
    else:
        t("Phase 1 clears state and writes route marker",
          has_state_clear and has_route_write,
          "Phase 1 should clear state and write route marker")
else:
    t("Phase 1 block found", False, "could not extract Phase 1 bash block")

print()

# ============================================================================
# TEST 8: MC_STATE_DIR exists and is properly used
# ============================================================================
print("[Test 8] MC_STATE_DIR is used for all phase state storage")

if phase1_block:
    # Count uses of MC_STATE_DIR
    state_dir_uses = phase1_block.count('$MC_STATE_DIR')
    t("Phase 1 uses MC_STATE_DIR for state storage",
      state_dir_uses > 0,
      f"expected MC_STATE_DIR references in Phase 1, found {state_dir_uses}")
else:
    t("Phase 1 block found", False, "could not extract Phase 1 bash block")

print()

# ============================================================================
# TEST 9: Verify Phase 1 PLAN_RESULT check precedes queue route write
# ============================================================================
print("[Test 9] PLAN_RESULT check precedes queue route marker write")

if phase1_block:
    plan_result_check_idx = phase1_block.find('PLAN_RESULT=$?')
    route_write_idx = phase1_block.find('printf \'queue\\n\'')

    precedes = plan_result_check_idx < route_write_idx if plan_result_check_idx >= 0 and route_write_idx >= 0 else False
    t("PLAN_RESULT checked before route marker write",
      precedes,
      "PLAN_RESULT=$? must precede the queue route marker write")
else:
    t("Phase 1 block found", False, "could not extract Phase 1 bash block")

print()

# ============================================================================
# TEST 10: Phase 1 queue-route marker is removed after Phase 4-Q completes
# ============================================================================
print("[Test 10] Queue-route marker is cleaned up after Phase 4-Q")

phase4q_text = extract_phase_block(MERGE, "Phase 4-Q")
if phase4q_text:
    # Look for cleanup that removes the entire state directory (which includes route file)
    # Pattern: rm -rf "$MC_STATE_DIR"
    cleanup_pattern = r'rm\s+-rf\s+"\$MC_STATE_DIR"'
    has_cleanup = bool(re.search(cleanup_pattern, phase4q_text))
    t("Phase 4-Q cleans up route marker file",
      has_cleanup,
      "Phase 4-Q should remove the entire state directory with: rm -rf \"$MC_STATE_DIR\"")
else:
    # If Phase 4-Q section extraction fails, test cannot be meaningfully run
    t("Phase 4-Q section found", False, "could not extract Phase 4-Q section")

print()

# ============================================================================
# TEST 11: Phase 1 Q1 routing predicate is properly structured
# ============================================================================
print("[Test 11] Phase 1 Q1 routing predicate correctly gates routing decision")

phase1_block = None
bash_blocks = extract_bash_blocks(MERGE)
for line_num, block in bash_blocks:
    if "PLAN_RESULT=$?" in block and "Q1 Predicate" in MERGE:
        phase1_block = block
        break

# Check the LIVE jq -e predicate invocation (not just its doc comment — a comment can drift
# from the code it describes, which is a doc-drift bug distinct from a logic bug).
live_predicate_match = re.search(
    r"jq -e '\.queue\.decision==\"refuse\" and \.queue\.detection\.state==\"configured\" and "
    r"\.queue\.pr_base!=null and \.queue\.pr_base==\.queue\.queue_base'",
    phase1_block or "",
)

if live_predicate_match:
    t("Phase 1 Q1 predicate (live jq invocation) has all routing conditions",
      True,
      "")
else:
    t("Phase 1 Q1 predicate (live jq invocation) has all routing conditions",
      False,
      "expected the live 'jq -e' invocation to check decision==\"refuse\" AND state==\"configured\" "
      "AND pr_base!=null AND pr_base==queue_base — not just the doc comment describing it")

print()

# ============================================================================
# TEST 12: Phase 1 PLAN_RESULT==3 boundary is pinned (not inverted, not off-by-one)
# ============================================================================
print("[Test 12] Phase 1 routing boundary is exactly 'PLAN_RESULT -eq 3'")

if phase1_block:
    has_exact_boundary = "if [ $PLAN_RESULT -eq 3 ]; then" in phase1_block
    t("Phase 1 uses exact 'if [ $PLAN_RESULT -eq 3 ]; then' boundary",
      has_exact_boundary,
      "expected the literal boundary check 'if [ $PLAN_RESULT -eq 3 ]; then' in Phase 1 "
      "(catches inversion to -ne, or drift to another comparison/value)")
else:
    t("Phase 1 block found", False, "could not extract Phase 1 bash block")

print()

h.summarize_and_exit()
