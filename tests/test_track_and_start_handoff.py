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

    flow_lines = list(re.finditer(r'^cd <worktree-path> && claude "/expert-flow"$', text, re.MULTILINE))
    impl_lines = list(re.finditer(r'claude "/implement-with-haiku"', text))
    print("[Section 1] Exactly four bare /expert-flow handoff lines")
    t("exactly 4 bare `cd <worktree-path> && claude \"/expert-flow\"` lines", len(flow_lines) == 4,
      f"found {len(flow_lines)}")
    t("no /expert-flow handoff line carries an issue number or flags",
      re.search(r'claude "/expert-flow [^"]+"', text) is None)

    print()
    print("[Section 2] Each /expert-flow line is in its own handoff block, after an /implement-with-haiku line")
    for i, m in enumerate(flow_lines, 1):
        prev_impl = [x for x in impl_lines if x.start() < m.start()]
        prev_flow = flow_lines[i - 2].start() if i > 1 else 0
        t(f"block {i}: an /implement-with-haiku line sits between the previous /expert-flow line and this one",
          bool(prev_impl) and prev_impl[-1].start() > prev_flow)

    print()
    print("[Section 3] Intro sentence per block matches its kind (2 GitHub re-plan, 2 GitHub-only caveat)")
    intros = []
    for m in flow_lines:
        line_start = text.rfind("Or run the whole lifecycle with /expert-flow", 0, m.start())
        intros.append(text[line_start:m.start()] if line_start >= 0 else "")
    replan = [x for x in intros if "starts again from planning" in x and "/expert-plan and track" in x]
    caveat = [x for x in intros if "runs only for a GitHub issue" in x and "needs a GitHub remote" in x]
    t("all 4 blocks have an intro sentence", all(intros))
    t("exactly 2 blocks carry the re-plan wording", len(replan) == 2, f"found {len(replan)}")
    t("exactly 2 blocks carry the GitHub-only caveat", len(caveat) == 2, f"found {len(caveat)}")
    t("re-plan blocks are the last two (GitHub flows)",
      "starts again from planning" in intros[2] and "starts again from planning" in intros[3])

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
