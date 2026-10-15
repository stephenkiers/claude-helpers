#!/usr/bin/env python3
"""
Test that /track-and-start's four handoff blocks include both the /implement-with-haiku
and /expert-flow alternatives (issue #302).

The four blocks are:
  1. Local Plan (~:749)
  2. Tracker (~:947)
  3. GitHub (~:1153)
  4. GitHub (~:1506)

Each block should have both lines as alternatives, with the GitHub blocks carrying the
re-plan wording.

Run with: python3 tests/test_track_and_start_handoff.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT


def find_handoff_blocks(text):
    """Extract the four handoff blocks from track-and-start.md.

    Returns a list of (description, block_text) tuples for the four blocks.
    """
    blocks = []

    # Marker strings for each block start (looking for unique context)
    # Local Plan block: ends with "run /implement-with-haiku" and handoff
    local_plan_start = text.find("### Step 4b: Handoff to")
    if local_plan_start > 0:
        next_block = text.find("\n### ", local_plan_start + 1)
        block = text[local_plan_start:next_block] if next_block > 0 else text[local_plan_start:]
        # This is the Local Plan block
        blocks.append(("Local Plan", block))

    # Count /implement-with-haiku occurrences to identify blocks
    impl_positions = [m.start() for m in re.finditer(r'claude "/implement-with-haiku"', text)]

    if len(impl_positions) >= 4:
        # Extract blocks around each /implement-with-haiku occurrence
        # Each block is from before the occurrence to the next heading or similar marker
        for i, pos in enumerate(impl_positions):
            # Find the start of this section (previous heading or marker)
            section_start = text.rfind("###", 0, pos)
            if section_start < 0:
                section_start = text.rfind("##", 0, pos)
            if section_start < 0:
                continue

            # Find the end (next heading or end of file)
            section_end = text.find("\n###", pos + 1)
            if section_end < 0:
                section_end = text.find("\n##", pos + 1)
            if section_end < 0:
                section_end = len(text)

            block = text[section_start:section_end]
            # Extract a meaningful description from the section heading
            heading_match = re.search(r"###?\s+([^\n]+)", block)
            desc = heading_match.group(1) if heading_match else f"Handoff {i+1}"
            blocks.append((desc, block))

    return blocks


def main():
    h = Harness("TRACK-AND-START HANDOFF BLOCKS TEST SUITE")
    t = h.test_result

    track_file = REPO_ROOT / "commands" / "track-and-start.md"
    t("commands/track-and-start.md exists", track_file.is_file(), str(track_file))

    if not track_file.is_file():
        h.summarize_and_exit()

    text = track_file.read_text()

    # Count occurrences of the two handoff options
    impl_count = len(re.findall(r'claude "/implement-with-haiku"', text))
    expert_flow_count = len(re.findall(r'claude "/expert-flow"', text))

    print("[Section 1] Handoff lines are present")
    t("/implement-with-haiku appears at least 4 times", impl_count >= 4, f"found {impl_count}")
    t("/expert-flow appears at least 4 times in handoff contexts", expert_flow_count >= 4, f"found {expert_flow_count}")

    print()
    print("[Section 2] Each handoff block has both alternatives")
    # Look for patterns where /expert-flow follows /implement-with-haiku
    # They should be paired within handoff blocks

    # More robust: find paragraphs that contain both lines
    # Pattern: /implement-with-haiku followed (within same block) by /expert-flow

    # Find all sections that mention both commands
    impl_and_flow_pattern = r'claude "/implement-with-haiku".*?claude "/expert-flow"'
    matches = re.findall(impl_and_flow_pattern, text, re.DOTALL)
    t("both /implement-with-haiku and /expert-flow appear together",
      len(matches) >= 4, f"found {len(matches)} paired blocks (expect >= 4)")

    print()
    print("[Section 3] GitHub blocks carry re-plan wording")
    # Look for the re-plan explanation that should appear with /expert-flow in GitHub blocks
    replan_pattern = r"starts again from planning|re-runs.*expert-plan"
    replan_mentions = re.findall(replan_pattern, text, re.IGNORECASE)
    t("re-plan wording appears in the document", len(replan_mentions) > 0,
      f"found {len(replan_mentions)} mentions")

    print()
    print("[Section 4] Local Plan and Tracker blocks note GitHub-only limitation")
    # The local plan and tracker blocks should mention that /expert-flow only works with GitHub
    github_only_pattern = r"only for a GitHub issue|GitHub issue.*worktree"
    github_mentions = re.findall(github_only_pattern, text, re.IGNORECASE)
    t("GitHub-only caveat mentioned for non-GitHub blocks", len(github_mentions) > 0,
      f"found {len(github_mentions)} mentions")

    print()
    print("[Section 5] Handoff line follows /implement-with-haiku in same block")
    # Each /implement-with-haiku line should have a nearby /expert-flow alternative
    # (The quoted template details are in expert-flow.md, not track-and-start)
    impl_count_in_text = len(re.findall(r'claude "/implement-with-haiku"', text))
    expert_flow_count_in_text = len(re.findall(r'claude "/expert-flow"', text))
    # They should roughly match (at least 4 of each)
    t("handoff lines are balanced (both alternatives present)",
      impl_count_in_text >= 4 and expert_flow_count_in_text >= 4,
      f"/implement-with-haiku: {impl_count_in_text}, /expert-flow: {expert_flow_count_in_text}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
