#!/usr/bin/env python3
"""
Differential corpus for the flow Bash guard's quote-aware splitter (#403).

Pins how scripts/flow-bash-guard.py classifies shell commands across the change from the
quote-blind splitter on main to the quote-aware splitter, the command-prefix normaliser and
the interpreter verb-pair check. Every entry runs through check_command() in-process.

Categories (each entry carries a comment naming the rule or path that decides it):
  MUST_BECOME_ALLOWED    blocked on unchanged main, allowed after the change
  MUST_STAY_ALLOWED      allowed on main and after (no regression)
  MUST_STAY_BLOCKED      blocked on main and after
  MUST_BECOME_BLOCKED    allowed on unchanged main, blocked after; each names the new rule
  FALSE_NEGATIVE_LEDGER  allowed after the change; known residuals, each with a justification

Pre-change side: before the splitter change, every entry was run through check_command() on the
unmodified base commit. MUST_BECOME_ALLOWED entries were blocked there, MUST_BECOME_BLOCKED entries
were allowed, and MUST_STAY and ledger entries matched their category. Sections 1 and 4 therefore
fail on the base commit and pass on the new guard. This file asserts the after side.

Run with: python3 tests/test_flow_bash_guard_quotes.py
"""

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT


GUARD_PATH = REPO_ROOT / "scripts" / "flow-bash-guard.py"


# (command, why this category holds: the rule or path that decides it)
MUST_BECOME_ALLOWED = [
    ('git commit -m "fix; tidy"', "quoted ; is not a split point"),
    ("git commit -m 'a(b'", "single-quoted ( is literal"),
    ('echo "a; b"', "quoted ; is not a split point"),
    ("echo 'x && y'", "single-quoted && is literal"),
    ('git commit -m "see (#403)"', "quoted ( and # (no fallback inside quotes)"),
    ('git commit -m "fix; refs #403"', "quoted ; and # (no fallback inside quotes)"),
    ('git commit -m "tidy (closes #403); docs"', "quoted ( and #, then a real ; split, no verb"),
    ("python3 -c 'import json; print(1)'", "quoted ; in an interpreter one-liner with no verb pair"),
    # Blocked on main only because shlex rejects the word. Bash reads the whole double-quoted
    # text as one argument (a" ; git push #), so no push runs. Verified with bash -c.
    ('echo "a\\" ; git push #"', "escaped \\\" stays inside the double-quoted word; no push runs"),
]

MUST_STAY_ALLOWED = [
    ("diff <(sort a) <(sort b)", "process substitution is a recursed group with no publishing verb"),
    ("while read -r f; do :; done < <(find . -name x)", "process substitution recursed; no verb"),
    ("IFS=$'\\n' read -r x", "$' normalised to '\\n'; no verb"),
    ("sort -t $'\\t' -k2 f", "$' normalised to '\\t'; no verb"),
    ("grep -n 'foo$' file", "$ inside single quotes is literal"),
    ("rg 'done$'", "$ inside single quotes is literal"),
]

MUST_STAY_BLOCKED = [
    ('echo "$(git push)"', "command substitution inside double quotes is recursed"),
    ('echo "`git push`"', "backtick inside double quotes is recursed"),
    ('bash -c "git push"', "WRAPPER_FORMS: bash -c"),
    ("sh -c 'a; git push'", "WRAPPER_FORMS: sh -c"),
    ('echo "x" && git push', "&& splits, then the publishing verb"),
    ('"git" push', "shlex unquotes the command word to git"),
    ('git "push"', "shlex unquotes the verb to push"),
    ('g"i"t push', "quotes inside the command word; shlex yields git"),
    ("cat <(git push)", "process substitution recursed into"),
    ('git commit -m "abc', "unterminated quote is unparseable"),
    ("git \\\npush", "backslash-newline removed; git push"),
    ("git pu\\\nsh", "backslash-newline removed inside the verb"),
    ('"git" \\\npush', "backslash-newline removed after a quoted word"),
    ("echo 'a\\' ; git push #'", "single quote closes after the backslash; word-start # triggers the fallback, where #' is unterminated"),
    ('echo "$(echo \')\' ; git push)"', "a quoted ) inside $( does not close the group; push recursed"),
    ("echo \\$'x' ; git push ; echo \\'", "escaped $ is literal; ; splits; git push"),
    ('echo "a\\$" ; git push ; echo "b\\$"', "escaped $ in double quotes keeps quote parity; ; splits"),
    ("echo \\$'x' ; git push ; echo \\' # c", "escaped $ kept by the fallback; quote-blind ; split"),
    ('echo "a" # c\ngit push', "newline splits after a comment; git push"),
    ("cat <<EOF\ndon't\nEOF\ngit push #'", "heredoc fallback; unterminated #' is unparseable"),
    ("echo hi # it's\ngit push #'", "comment fallback; unterminated #' is unparseable"),
    ("cat <<EOF\ngit push\nEOF", "heredoc body line inspected line by line, as on main"),
    ("python3 -c 'import os; os.system(\"git push\")'", "interpreter verb pair (git push); main blocks it only by its quote-blind split"),
]

MUST_BECOME_BLOCKED = [
    ("echo hi; $'git' push", "tracker drops the unescaped $ before ' outside single quotes"),
    ("echo hi # x\n$'git' push", "fallback drops the unescaped $ before ' (word-start # triggers it)"),
    ("cat <<EOF\nx\nEOF\n$'git' push", "fallback drops the unescaped $ before ' (heredoc triggers it)"),
    ("GIT_TERMINAL_PROMPT=0 git push", "TRANSPARENT_PREFIXES: assignment"),
    ("A=1 B=2 git push", "TRANSPARENT_PREFIXES: assignment, repeated"),
    ("command git push", "TRANSPARENT_PREFIXES: bare command"),
    ("exec git push", "TRANSPARENT_PREFIXES: bare exec"),
    ("nohup git push", "TRANSPARENT_PREFIXES: bare nohup"),
    ("time git push", "TRANSPARENT_PREFIXES: bare time"),
    ("timeout 5 git push", "TRANSPARENT_PREFIXES: one-positional timeout"),
    ("nice -n 10 git push", "TRANSPARENT_PREFIXES: nice -n N"),
    ("=git push", "TRANSPARENT_PREFIXES: zsh-equals"),
    ("$(git status", "unclosed $( is unparseable (main inspects the open group and allows it)"),
    ('python3 -c \'x=["git","push"]\'', "interpreter verb pair: git push"),
    ('python3 -c \'x=["git","--no-pager","push"]\'', "interpreter verb pair with a global option (skipped by is_publishing_verb)"),
    ('python3 -c \'x=["git","-C",".","push"]\'', "interpreter verb pair with -C <dir>"),
    ("python3.12 -Ic 'x=[\"git\",\"push\"]'", "versioned interpreter with combined -Ic"),
    ('node -e \'x=["gh","pr","create"]\'', "node -e with gh pr create"),
    ('node --eval \'x=["git","push"]\'', "node --eval"),
    ('node -p \'x=["git","push"]\'', "node -p"),
    ("perl -E 'print \"git push\"'", "perl -E with a git push verb pair"),
    ('PYTHONPATH=. python3 -c \'x=["git","push"]\'', "assignment prefix, then interpreter -c"),
    ('timeout 5 python3 -c \'x=["git","push"]\'', "timeout prefix, then interpreter -c"),
]

# Allowed after the change. Each is a known residual, not a closed hole.
FALSE_NEGATIVE_LEDGER = [
    ('python3 -c \'x=["gi"+"t","push"]\'', "verb pair built by concatenation; no literal git token"),
    ("$'\\x67it' push", "escape-encoded ANSI-C word; $ dropped, \\x67 stays literal (main allows it too)"),
    ("exec -a x git push", "prefix option outside the Q2 set; normalisation stops at -a"),
    ("command -p git push", "prefix option outside the Q2 set; normalisation stops at -p"),
    ("timeout -s KILL 5 git push", "timeout with an option; only timeout <duration> is stripped"),
    ("nice -5 git push", "nice with a numeric option; only nice -n N and bare nice are stripped"),
    ("*(e:'git push':)", "zsh glob qualifier; the quoted text is one token that is not git"),
    ('python3 -c \'x=["git","-c","a=b","push"]\'', "git -c inside an interpreter program; the verb-pair check does not skip git's -c value"),
]


def load_guard():
    """Load scripts/flow-bash-guard.py as a module (its main() is guarded)."""
    spec = importlib.util.spec_from_file_location("flow_bash_guard", GUARD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_must_stay(h, guard, title, entries, expect_blocked):
    for command, why in entries:
        blocked, reason = guard.check_command(command)
        h.test_result(
            f"{title}: {command!r}",
            blocked == expect_blocked,
            f"expected {'blocked' if expect_blocked else 'allowed'}, got "
            f"{'blocked' if blocked else 'allowed'} ({reason}); decided by: {why}",
        )


def main():
    h = Harness("FLOW BASH GUARD QUOTE-AWARE CORPUS (#403)")
    guard = load_guard()

    print("[Section 1] Must become ALLOWED (blocked on main, allowed after)")
    run_must_stay(h, guard, "allowed", MUST_BECOME_ALLOWED, expect_blocked=False)

    print()
    print("[Section 2] Must stay ALLOWED (allowed on main and after)")
    run_must_stay(h, guard, "allowed", MUST_STAY_ALLOWED, expect_blocked=False)

    print()
    print("[Section 3] Must stay BLOCKED (blocked on main and after)")
    run_must_stay(h, guard, "blocked", MUST_STAY_BLOCKED, expect_blocked=True)

    print()
    print("[Section 4] Must become BLOCKED (allowed on main, blocked after)")
    run_must_stay(h, guard, "blocked", MUST_BECOME_BLOCKED, expect_blocked=True)

    print()
    print("[Section 5] False-negative ledger (allowed after; residuals with justification)")
    run_must_stay(h, guard, "residual allowed", FALSE_NEGATIVE_LEDGER, expect_blocked=False)

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
