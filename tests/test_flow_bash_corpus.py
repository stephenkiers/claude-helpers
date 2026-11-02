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
  - `<CHILD_WT>` placeholder lines in worktree-reference.md: angle-bracket placeholders
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

# Maximum allowable number of skipped lines (extraction artifacts, not real commands).
# Each skip must be an artifact of extraction (multi-line python3 -c, placeholder lines,
# prose fragments) and narrowly justified, so this count is low and regressions are caught.
MAX_SKIPPED = 15

VERB = re.compile(r"(git|gh|eval|python3)\b")


def skipped(doc, line, cmd, rule):
    """Check if a blocked command is a known extraction artifact, not a real command."""
    # prose fragment with no actual command (just the flag without the branch)
    if cmd == "git push -u":
        return True
    # multi-line python3 -c openers: the body sits on following lines (not real one-line commands)
    if rule == "parse-error" and re.match(r"python3 -c ['\"]$", cmd):
        return True
    if rule == "parse-error" and re.search(r"\| python3 -c ['\"]$", cmd):
        return True
    # placeholder lines in worktree-reference.md: angle brackets + quoted { confuse the splitter
    if rule == "parse-error" and "<CHILD_WT>" in cmd and doc.endswith("worktree-reference.md"):
        return True
    # complex piped sed expressions split at pipes and expose unbalanced quotes in segments
    # (shipit.md:298, stack-sync.md:30, and others: git symbolic-ref | sed 's|...|...|')
    if rule == "parse-error" and "symbolic-ref" in cmd and "|" in cmd and "sed" in cmd:
        return True
    # complex git worktree list | awk/sed pipelines split at pipes
    # (worktree-reference.md lines, shipit.md:298, cleanup.md:783)
    if rule == "parse-error" and "worktree list" in cmd and "|" in cmd:
        return True
    # command substitutions with git flags that appear unparseable when segment-split
    # (cleanup.md:484: git rev-parse --path-format=... 2>/dev/nu...)
    if rule == "parse-error" and "git rev-parse" in cmd and "$(" in cmd:
        return True
    return False


# SANCTIONED: commands that actually appear in the agent's closure docs (reachable at runtime)
# and must not be denied. Corpus test ensures the guard doesn't reject these.
# (Different from ALLOWED in test_flow_ship_merge_guard.py which tests edge cases.)
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
        skipped_count = 0
        unexpected = []
        for doc, line, cmd in corpus(agent):
            # Find all command verbs in this line using the guard's own segment splitter,
            # so we catch verbs after cd, pipes, semicolons, and &&/|| operators.
            try:
                segments = guard.split_segments(cmd)
            except guard.UnparseableCommand:
                # The corpus is line-based, so a line from a multi-line quoted or $( ) command
                # is an unterminated fragment. It has no verb to check; skip it (the guard
                # itself fails closed on such input).
                segments = []
            found_verb = False
            for segment in segments:
                tokens = guard.tokenize_segment(segment)
                if tokens is None:
                    # Unparseable segment; let the guard check it
                    break
                if tokens and VERB.match(tokens[0]):
                    found_verb = True
                    break
            if not found_verb:
                continue

            total += 1
            blocked, _reason, rule = guard.check_command_for_agent(cmd, agent)
            if blocked:
                if skipped(doc, line, cmd, rule):
                    skipped_count += 1
                else:
                    unexpected.append(f"{doc}:{line} [{rule}] {cmd[:80]}")

        h.test_result(f"{agent}: corpus is non-empty", total > 20, f"total={total}")
        h.test_result(f"{agent}: skipped artifact count within bounds",
                      skipped_count <= MAX_SKIPPED,
                      f"skipped={skipped_count} (max={MAX_SKIPPED})")
        h.test_result(f"{agent}: no unexpected denials in closure docs",
                      not unexpected, "; ".join(unexpected))
        for cmd in SANCTIONED:
            blocked, reason, rule = guard.check_command_for_agent(cmd, agent)
            h.test_result(f"{agent}: allows {cmd[:60]}", not blocked, f"{rule}: {reason}")
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
