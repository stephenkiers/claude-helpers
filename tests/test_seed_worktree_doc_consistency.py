#!/usr/bin/env python3
"""
Test suite for doc consistency: seed-cache in track-and-start.md and
reason string in expert-flow.md (issue #275).

Verifies that every git worktree add is followed by a seed-cache call,
and that the new failure reason string appears in expert-flow.md.

Run with: python3 tests/test_seed_worktree_doc_consistency.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT


if __name__ == "__main__":
    h = Harness("SEED WORKTREE DOC CONSISTENCY TEST SUITE")
    test_result = h.test_result

    TRACK_AND_START = REPO_ROOT / "commands" / "track-and-start.md"
    EXPERT_FLOW = REPO_ROOT / "commands" / "expert-flow.md"

    # The new reason string that should appear in expert-flow.md
    NEW_REASON_STRING = "no check command configured — run /shipit once or add commands.check to .claude/project.yaml"

    print("[Section 1] track-and-start.md doc consistency")

    track_content = TRACK_AND_START.read_text()

    # Find all occurrences of "git worktree add"
    worktree_add_pattern = r'(?m)^git\s+worktree\s+add'
    worktree_adds = list(re.finditer(worktree_add_pattern, track_content))

    test_result(
        "track-and-start.md: contains git worktree add",
        len(worktree_adds) > 0,
        f"found {len(worktree_adds)} occurrences"
    )

    # For each worktree add, verify a seed-cache call follows in the same bash block
    for i, match in enumerate(worktree_adds):
        # Find the next closing triple backticks (end of bash block)
        block_start = track_content.rfind("```bash", 0, match.start())
        block_end = track_content.find("```", match.end())

        if block_start < 0 or block_end < 0:
            test_result(
                f"track-and-start.md: git worktree add #{i+1} is in a bash block",
                False,
                "Could not find bash block boundaries"
            )
            continue

        bash_block = track_content[block_start:block_end]

        # Check if seed-cache appears in the same bash block
        has_seed_cache = "seed-cache" in bash_block or "seed_cache" in bash_block

        test_result(
            f"track-and-start.md: git worktree add #{i+1} followed by seed-cache in same bash block",
            has_seed_cache,
            f"Block snippet: {bash_block[bash_block.find('git worktree add'):bash_block.find('git worktree add')+200]}"
        )

        # Verify it's the CLI command form (track seed-cache)
        if has_seed_cache:
            has_cli_form = "python3 -m scripts.workflow.cli track seed-cache" in bash_block
            test_result(
                f"track-and-start.md: git worktree add #{i+1} uses CLI form of seed-cache",
                has_cli_form,
                "Looking for 'python3 -m scripts.workflow.cli track seed-cache'"
            )

    print()
    print("[Section 2] expert-flow.md doc consistency")

    expert_flow_content = EXPERT_FLOW.read_text()

    test_result(
        "expert-flow.md: contains new failure reason string",
        NEW_REASON_STRING in expert_flow_content,
        f"Expected to find: {NEW_REASON_STRING}"
    )

    # Also verify it's in the verify section (contains "verify")
    verify_section_pattern = r'## verify'
    verify_match = re.search(verify_section_pattern, expert_flow_content)

    if verify_match:
        # Find the next ## section (to get the bounds of verify section)
        next_section = re.search(r'^##\s', expert_flow_content[verify_match.end():], re.MULTILINE)
        verify_end = verify_match.end() + next_section.start() if next_section else len(expert_flow_content)
        verify_section = expert_flow_content[verify_match.start():verify_end]

        test_result(
            "expert-flow.md: reason string is in verify section",
            NEW_REASON_STRING in verify_section,
            "Reason string not found in verify section"
        )

        # Verify the string is associated with resolve-check or check resolution
        has_check_context = ("resolve-check" in verify_section or
                           "check command" in verify_section or
                           "checks resolve" in verify_section)
        test_result(
            "expert-flow.md: reason string has check resolution context",
            has_check_context,
            "verify section should mention resolve-check or similar"
        )

    print()
    print("[Section 3] expert-flow.md: old 'run /shipit once by hand' text is removed")

    # The old text that should be gone
    old_shipit_text = "run /shipit once by hand to detect it"

    test_result(
        "expert-flow.md: old shipit detection text has been replaced",
        old_shipit_text not in expert_flow_content,
        f"Old text still present: {old_shipit_text}"
    )

    print()
    print("[Section 4] expert-flow.md: mentions cache seeding from main")

    seed_mention = "seed" in expert_flow_content.lower() and "main" in expert_flow_content.lower()

    test_result(
        "expert-flow.md: mentions seeding from main",
        seed_mention,
        "Should mention cache seeding from main worktree"
    )

    print()
    h.summarize_and_exit()
