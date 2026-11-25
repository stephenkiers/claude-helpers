#!/usr/bin/env python3
"""Structural contract: step agents and the skills they run never end their turn to wait for
children (#532, ADR-0023 amendments 2026-11-23 and 2026-11-24).

- No rostered command doc tells the model to end its turn while children are outstanding, or to
  use ScheduleWakeup to wait for them.
- File-wait sites use wait-files.py (mark and wait, with a deadline).
- No site launches with run_in_background: false. The interactive Agent tool has no such
  parameter, so every child runs in the background and is collected with wait-files.py over its
  receipt file (Branch W, #553).

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

# Presence ratchet: the checks below assert a marker appears somewhere in the doc. They do not
# prove that every launch site is covered, so a new site must be reviewed by hand.
FILE_WAIT_DOCS = {
    "commands/implement-with-haiku.md": ["round1", "fanout", "check"],
    "commands/merge-and-cleanup.md": ["merge:apply"],
    "commands/queued-merge.md": ["queued-merge:enqueue"],
}
COLLECT_DOCS = [
    "commands/expert-plan.md",
    "commands/expert-review.md",
    "prompts/expert-review-panel.md",
]

END_TURN = re.compile(r"(?i)\bend (?:your|the) turn\b(?:[^.\n]|\n(?![ \t]*\n))*?\b(?:while|until|to wait|and wait)\b")
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
        for m in END_TURN.finditer(text):  # whole-text scan so a sentence wrapped across lines is seen
            if not NEGATED.search(text[max(0, m.start() - 40):m.end()]):
                bad_end.append(text.count("\n", 0, m.start()) + 1)
        for lineno, line in enumerate(text.splitlines(), start=1):
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

    for rel in COLLECT_DOCS:
        text = (REPO_ROOT / rel).read_text()
        h.test_result(rel + ": no site launches with run_in_background: false",
                      "run_in_background: false" not in text)
        h.test_result(rel + ": launches in the background", "run_in_background: true" in text)
        launches = text.count("run_in_background: true")
        receipt_lines = sum(1 for line in text.splitlines() if re.search(r"(?i)receipt", line))
        h.test_result(rel + ": names at least one receipt line per background launch",
                      receipt_lines >= launches,
                      "%d receipt lines for %d launches" % (receipt_lines, launches))

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
# Real wait or mark: a wait-files.py wait/mark invocation, or a Collect pointer that names the barrier
# pattern on the same line. Doc-level checks use only this stricter form.
WAIT_OR_MARK = re.compile(r'wait-files\.py"?\s+(?:wait|mark)\b|\*\*Collect:\*\*[^\n]*join-barrier-pattern\.md')
# implement-with-haiku.md alone also accepts its prose pointers to the Collecting units block.
WAIT_OR_MARK_IMPLEMENT = re.compile(WAIT_OR_MARK.pattern + r'|Collecting units|collected per `~/\.claude/prompts/join-barrier-pattern\.md`')
PROXIMITY_DOCS = ["commands/implement-with-haiku.md"] + COLLECT_DOCS
BASH_TIMEOUT_600000 = re.compile(r"\btimeout\b[^\n]{0,40}\b600000\b")
COREUTILS_TIMEOUT = re.compile(r"(?:^|[\s;&|(`])timeout\s+\d+\s")
SNIPPET_WINDOW = 15
BACKGROUND_LAUNCH = "run_in_background: true"
BACKGROUND_WAIT_PROXIMITY = 40
# (doc, content marker on the launch line) -> reason. Every other background launch must sit near a
# wait, mark or Collect. Keyed by content, not line number, so an edit above the line cannot orphan it.
BACKGROUND_ALLOWLIST: dict = {
    ("commands/expert-review.md", "every call with `run_in_background: true`, with a `Receipt file:` line"):
        "the orchestrator rule that states the launch convention, not a launch site",
}


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

    for impl_path in PROXIMITY_DOCS:
        impl = (REPO_ROOT / impl_path).read_text().splitlines()
        launches = [i for i, line in enumerate(impl) if BACKGROUND_LAUNCH in line]
        h.test_result(impl_path + ": has background launches to check", bool(launches))
        wait_re = WAIT_OR_MARK_IMPLEMENT if impl_path == "commands/implement-with-haiku.md" else WAIT_OR_MARK
        wait_idx = [i for i, line in enumerate(impl) if wait_re.search(line)]
        unwaited = []
        for i in launches:
            if any(doc == impl_path and marker in impl[i] for doc, marker in BACKGROUND_ALLOWLIST):
                continue
            if not any(abs(j - i) <= BACKGROUND_WAIT_PROXIMITY for j in wait_idx):
                unwaited.append(i + 1)
        h.test_result(impl_path + ": every run_in_background: true launch sits near a wait, mark or Collect",
                      not unwaited, "lines %s" % unwaited if unwaited else "")
    stale = [(doc, marker) for doc, marker in BACKGROUND_ALLOWLIST
             if not any(BACKGROUND_LAUNCH in line and marker in line
                        for line in (REPO_ROOT / doc).read_text().splitlines())]
    h.test_result("every background allowlist marker still sits on a launch line", not stale,
                  "stale: %s" % stale if stale else "")
    h.test_result("background allowlist entries each carry a reason",
                  all(isinstance(v, str) and v.strip() for v in BACKGROUND_ALLOWLIST.values()))


if __name__ == "__main__":
    main()
