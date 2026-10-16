#!/usr/bin/env python3
"""
Test the four handoff blocks in commands/track-and-start.md (issue #302).

Asserts, against the text of the file:
  1. Exactly four bare `cd <worktree-path> && claude "/expert-flow"` lines exist, and none
     carries an issue number or flags.
  2. Each /expert-flow line sits in its own handoff block: an `claude "/implement-with-haiku"`
     line appears between the previous /expert-flow line (or file start) and this one.
  3. Each block has an intro sentence ("Or run the whole lifecycle with /expert-flow ...")
     located between the previous /expert-flow line and this one. A missing intro is a
     failure, not a silent match against an earlier block's intro. Two blocks carry the
     GitHub re-plan wording and two carry the GitHub-only caveat, with the re-plan blocks
     last.

Run with: python3 tests/test_track_and_start_handoff.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT


INTRO_MARKER = "Or run the whole lifecycle with /expert-flow"


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
    for i, m in enumerate(flow_lines):
        # Search only between the previous block's /expert-flow line and this block's line,
        # so a block missing its intro cannot borrow the previous block's intro.
        window_start = flow_lines[i - 1].end() if i > 0 else 0
        line_start = text.rfind(INTRO_MARKER, window_start, m.start())
        intros.append(text[line_start:m.start()] if line_start >= 0 else None)
    replan = [x for x in intros if x and "starts again from planning" in x and "/expert-plan and track" in x]
    caveat = [x for x in intros if x and "runs only for a GitHub issue" in x and "needs a GitHub remote" in x]
    t("all 4 blocks have an intro sentence", len(intros) == 4 and all(x is not None for x in intros))
    t("exactly 2 blocks carry the re-plan wording", len(replan) == 2, f"found {len(replan)}")
    t("exactly 2 blocks carry the GitHub-only caveat", len(caveat) == 2, f"found {len(caveat)}")
    t("re-plan blocks are the last two (GitHub flows)",
      len(intros) == 4 and intros[2] is not None and intros[3] is not None
      and "starts again from planning" in intros[2] and "starts again from planning" in intros[3])

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
