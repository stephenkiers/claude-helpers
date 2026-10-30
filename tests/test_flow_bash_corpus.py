#!/usr/bin/env python3
"""
Corpus test for the flow-ship / flow-merge Bash guard (#359, plan Step 9).

Runs the guard over every git/gh/eval/python3 command line in each agent's Skill closure
(tests/flow_corpus.py). Anything denied must be on the commented SKIP list below; a new
denial means a doc edit or a guard change broke a sanctioned command. Also pins the
sanctioned push and cleanup forms as explicitly allowed.

SKIP list (all are extraction artifacts, not real commands the guard should allow):
  - "git push -u" inline spans: prose fragments with no branch argument.
  - multi-line `python3 -c '` / `python3 -c "` openers: the body sits on following lines
    (guard is exercised on the one-line form in test_flow_guard_behaviour.py).
  - `<CHILD_WT>` placeholder lines in worktree-reference.md:408: angle-bracket placeholders
    plus a quoted `{` hit the non-quote-aware segment splitter (issue #403). Re-check when
    #403 lands; entries here should then be removable.

Run with: python3 tests/test_flow_bash_corpus.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness
from flow_corpus import CLOSURE_DOCS, corpus, load_guard

VERB = re.compile(r"(git|gh|eval|python3)\b")


def skipped(doc, line, cmd, rule):
    if cmd == "git push -u":
        return True
    if rule == "parse-error" and re.match(r"python3 -c ['\"]$", cmd):
        return True
    if rule == "parse-error" and re.search(r"\| python3 -c ['\"]$", cmd):
        return True
    if rule == "parse-error" and "<CHILD_WT>" in cmd and doc.endswith("worktree-reference.md"):
        return True
    return False


SANCTIONED = [
    "git push --force-with-lease --force-if-includes origin HEAD",
    "git -C /tmp/wt push --force-with-lease",
    "git push -u origin feature/x",
    "git push --force-with-lease=feature/x:" + "a" * 40 + " origin " + "b" * 40 + ":refs/heads/feature/x",
    "gh stack sync",
    'eval "$CHECK_CMD"',
]


def main():
    h = Harness("flow bash corpus")
    guard = load_guard()
    for agent in CLOSURE_DOCS:
        total = 0
        unexpected = []
        for doc, line, cmd in corpus(agent):
            if not VERB.match(cmd):
                continue
            total += 1
            blocked, _reason, rule = guard.check_command_for_agent(cmd, agent)
            if blocked and not skipped(doc, line, cmd, rule):
                unexpected.append(f"{doc}:{line} [{rule}] {cmd[:80]}")
        h.test_result(f"{agent}: corpus is non-empty", total > 20, f"total={total}")
        h.test_result(f"{agent}: no unexpected denials in closure docs",
                      not unexpected, "; ".join(unexpected))
        for cmd in SANCTIONED:
            blocked, reason, rule = guard.check_command_for_agent(cmd, agent)
            h.test_result(f"{agent}: allows {cmd[:60]}", not blocked, f"{rule}: {reason}")
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
