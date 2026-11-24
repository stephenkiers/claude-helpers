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

    round2_doc_checks(h)

    print()
    h.summarize_and_exit()


# D1/D2: doc structure for the foreground wait protocol (read at test time from the shipped docs)
WAIT_SNIPPET_DOCS = [
    "prompts/join-barrier-pattern.md",
    "commands/implement-with-haiku.md",
    "commands/merge-and-cleanup.md",
    "commands/queued-merge.md",
]
WAIT_SNIPPET = re.compile(r'wait-files\.py"?\s+wait\b')
WAIT_OR_MARK = re.compile(r'wait-files\.py"?\s+(?:wait|mark)\b')
BASH_TIMEOUT_600000 = re.compile(r"\btimeout\b[^\n]{0,40}\b600000\b")
COREUTILS_TIMEOUT = re.compile(r"(?:^|[\s;&|(`])timeout\s+\d+\s")
SNIPPET_WINDOW = 15
BACKGROUND_LAUNCH = "run_in_background: true"
BACKGROUND_WAIT_PROXIMITY = 40
# "<doc>:<line>" -> reason. Empty: every background launch must sit near a wait or mark.
BACKGROUND_ALLOWLIST: dict = {}


def round2_doc_checks(h: Harness) -> None:
    for rel in WAIT_SNIPPET_DOCS:
        lines = (REPO_ROOT / rel).read_text().splitlines()
        snippet_idx = [i for i, line in enumerate(lines) if WAIT_SNIPPET.search(line)]
        h.test_result(rel + ": has wait-files.py wait snippets", bool(snippet_idx))
        no_timeout = []
        for i in snippet_idx:
            window = lines[max(0, i - SNIPPET_WINDOW): i + SNIPPET_WINDOW + 1]
            if not any(BASH_TIMEOUT_600000.search(w) for w in window):
                no_timeout.append(i + 1)
        h.test_result(rel + ": every wait snippet states Bash timeout 600000", not no_timeout,
                      "lines %s" % no_timeout if no_timeout else "")
        coreutils = [
            n + 1 for n, line in enumerate(lines)
            if COREUTILS_TIMEOUT.search(line) and "wait-files" in " ".join(lines[max(0, n - 2): n + 3])
        ]
        h.test_result(rel + ": no coreutils timeout wrapping wait-files.py", not coreutils,
                      "lines %s" % coreutils if coreutils else "")

    impl_path = "commands/implement-with-haiku.md"
    impl = (REPO_ROOT / impl_path).read_text().splitlines()
    launches = [i for i, line in enumerate(impl) if BACKGROUND_LAUNCH in line]
    h.test_result(impl_path + ": has background launches to check", bool(launches))
    wait_idx = [i for i, line in enumerate(impl) if WAIT_OR_MARK.search(line)]
    unwaited = []
    for i in launches:
        if "%s:%d" % (impl_path, i + 1) in BACKGROUND_ALLOWLIST:
            continue
        if not any(abs(j - i) <= BACKGROUND_WAIT_PROXIMITY for j in wait_idx):
            unwaited.append(i + 1)
    h.test_result(impl_path + ": every run_in_background: true launch sits near a wait or mark",
                  not unwaited, "lines %s" % unwaited if unwaited else "")
    h.test_result("background allowlist entries each carry a reason",
                  all(isinstance(v, str) and v.strip() for v in BACKGROUND_ALLOWLIST.values()))


if __name__ == "__main__":
    main()
