#!/usr/bin/env python3
"""Structural contract: step agents and the skills they run never end their turn to wait for
children (#532, ADR-0023 amendment 2026-11-23).

- No rostered command doc tells the model to end its turn while children are outstanding, or to
  use ScheduleWakeup to wait for them.
- File-wait sites use wait-files.py (mark and wait, with a deadline).
- Foreground sites launch with run_in_background: false.

Run with: python3 tests/test_step_agent_waits.py
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT  # noqa: E402
from test_flow_ask_sites import ROSTER  # noqa: E402

# expert-flow-v2 is a driver stub with no child waits of its own; expert-flow.md (the top-level
# orchestrator) is the documented carve-out and is not in the roster.
EXEMPT = {"commands/expert-flow-v2.md"}
EXTRA = ["commands/queued-merge.md", "prompts/flow-step-contract.md"]

FILE_WAIT_DOCS = {
    "commands/implement-with-haiku.md": ["round1", "fanout", "check"],
    "commands/merge-and-cleanup.md": ["merge:apply"],
    "commands/queued-merge.md": ["queued-merge:enqueue"],
}
FOREGROUND_DOCS = [
    "commands/expert-plan.md",
    "commands/expert-review.md",
    "prompts/expert-review-panel.md",
]

END_TURN = re.compile(r"(?i)\bend (?:your|the) turn\b[^.\n]*\b(?:while|until|to wait|and wait)\b")
NEGATED = re.compile(r"(?i)\b(?:never|not|don't|do not)\s+(?:\w+\s+){0,2}end (?:your|the) turn")


def main() -> None:
    h = Harness("STEP AGENT WAIT CONTRACT")
    docs = [r for r in ROSTER if r not in EXEMPT] + EXTRA
    for rel in docs:
        path = REPO_ROOT / rel
        if not path.is_file():
            h.test_result(rel + " exists", False, str(path))
            continue
        text = path.read_text()
        bad_end = []
        bad_wake = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            for m in END_TURN.finditer(line):
                prefix = line[max(0, m.start() - 40):m.end()]
                if not NEGATED.search(prefix):
                    bad_end.append(lineno)
            if "ScheduleWakeup" in line and not re.search(r"(?i)never (?:use )?`?ScheduleWakeup|is never used to wait", line):
                bad_wake.append(lineno)
        h.test_result(rel + ": no instruction to end the turn while children run", not bad_end,
                      "lines %s" % bad_end if bad_end else "")
        h.test_result(rel + ": ScheduleWakeup only appears negated", not bad_wake,
                      "lines %s" % bad_wake if bad_wake else "")

    for rel, phases in FILE_WAIT_DOCS.items():
        text = (REPO_ROOT / rel).read_text()
        h.test_result(rel + ": uses wait-files.py mark", "wait-files.py\" mark" in text)
        h.test_result(rel + ": uses wait-files.py wait with a deadline",
                      "wait-files.py\" wait" in text and "--deadline" in text)
        h.test_result(rel + ": launches in the background", "run_in_background: true" in text)
        for phase in phases:
            h.test_result(rel + ": names phase " + phase, phase in text)
        h.test_result(rel + ": no completion-notification wait",
                      not re.search(r"(?i)wait for (?:its|the) completion notification", text))

    for rel in FOREGROUND_DOCS:
        text = (REPO_ROOT / rel).read_text()
        h.test_result(rel + ": foreground sites launch with run_in_background: false",
                      "run_in_background: false" in text)

    contract = (REPO_ROOT / "prompts" / "flow-step-contract.md").read_text()
    h.test_result("flow-step-contract carries the never-end-turn rule",
                  "Never end your turn to wait for children" in contract)
    barrier = (REPO_ROOT / "prompts" / "join-barrier-pattern.md").read_text()
    h.test_result("join-barrier-pattern keeps the orchestrator carve-out",
                  "Carve-out: the top-level orchestrator" in barrier)
    h.test_result("join-barrier-pattern names the deadline hard stop",
                  "child-wait deadline: phase" in barrier)

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
